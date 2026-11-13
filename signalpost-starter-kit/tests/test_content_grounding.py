"""Offline tests for strict website grounding (name / org number in page text).

Run with:  python tests/test_content_grounding.py   (or pytest)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from domain_validator import CONTENT_MISMATCH_REASON, DomainValidator  # noqa: E402
from extractor import GROUNDING_MAX_FETCHES, Page, ground_pages, verify_pages  # noqa: E402
from schema import CompanyProfile  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")


def validator(orgnr="923609016", navn="AKER SOLUTIONS ASA", hjemmeside=None):
    return DomainValidator(Enhet(organisasjonsnummer=orgnr, navn=navn, organisasjonsform=FORM,
                                 hjemmeside=hjemmeside))


def test_exact_name_case_insensitive():
    assert validator().text_names_entity("Welcome to Aker Solutions ASA, an engineering firm")


def test_org_number_plain_and_spaced():
    v = validator()
    assert v.text_names_entity("Org.nr 923609016")
    assert v.text_names_entity("Org.nr. 923 609 016 MVA")


def test_org_number_not_part_of_longer_number():
    assert not validator().text_names_entity("ref 19236090161")


def test_foreign_lookalike_rejected():
    v = validator()
    assert not v.text_names_entity("Aker Solutions Inc. Houston, Texas - all rights reserved")
    assert not v.text_names_entity("")
    assert not v.text_names_entity(None)


def test_verify_content_rejects_domain_and_logs_reason():
    v = validator()
    verdict = v.verify_content("https://akersolutions.com", "unrelated text")
    assert not verdict.accepted and verdict.reason == CONTENT_MISMATCH_REASON
    assert not v.check("https://www.akersolutions.com/news").accepted


def test_verify_pages_drops_ungrounded_domain():
    v = validator()
    good = Page("https://akersolutions.com/", text="x", raw_text="Aker Solutions ASA | Org 923609016")
    bad = Page("https://akersolutions.us/", text="Houston office", raw_text="Aker Solutions Inc")
    kept = verify_pages([good, bad], v)
    assert [p.url for p in kept] == ["https://akersolutions.com/"]


def test_verify_pages_uses_footer_text_not_only_main_text():
    v = validator()
    page = Page("https://akersolutions.com/", text="Our services", raw_text="footer: Aker Solutions ASA")
    assert verify_pages([page], v) == [page]


def test_rejected_registry_site_not_refilled_as_baseline():
    v = validator(hjemmeside="akersolutions.com")
    assert v.registered_site_url == "https://akersolutions.com"
    v.verify_content("https://akersolutions.com", "some other company")
    assert v.registered_site_url is None
    assert v.sanitize_profile(CompanyProfile()).website.fact_value is None


def test_empty_pages_do_not_reject_domain():
    v = validator(hjemmeside="akersolutions.com")
    assert verify_pages([Page("https://akersolutions.com/")], v)
    assert v.registered_site_url == "https://akersolutions.com"


# --- Phase 1: about/contact fallback crawl -----------------------------------

class FakeSite:
    """Offline stand-in for _fetch_page_once: url -> Page, records every fetch."""

    def __init__(self, pages):
        self.pages = {u.rstrip("/"): p for u, p in pages.items()}
        self.fetched = []

    async def __call__(self, url):
        self.fetched.append(url)
        return self.pages.get(url.rstrip("/"), Page(url))


def _ground(pages, v, site):
    return asyncio.run(ground_pages(pages, v, fetch=site))


HOME_NO_NAME = Page("https://akersolutions.com/", text="Engineering for energy",
                    links=[("Om oss", "https://akersolutions.com/om-oss"),
                           ("Products", "https://akersolutions.com/products")])


def test_fallback_grounds_via_linked_about_page():
    v = validator()
    about = Page("https://akersolutions.com/om-oss", text="Om oss",
                 raw_text="Aker Solutions ASA, org.nr. 923 609 016")
    site = FakeSite({about.url: about})
    kept = _ground([HOME_NO_NAME], v, site)
    assert [p.url for p in kept] == [HOME_NO_NAME.url, about.url]
    assert site.fetched[0] == about.url  # linked about page is tried first
    assert v.check("https://akersolutions.com").accepted


def test_fallback_probes_contact_path_when_not_linked():
    v = validator()
    home = Page("https://akersolutions.com/", text="Welcome", links=[("x", "https://other.no/")])
    contact = Page("https://akersolutions.com/kontakt", text="Kontakt", raw_text="Org nr 923609016")
    site = FakeSite({contact.url: contact})
    kept = _ground([home], v, site)
    assert contact.url in [p.url for p in kept]


def test_fallback_second_layer_reaches_contact_from_about():
    v = validator()
    home = Page("https://akersolutions.com/", text="Hi",
                links=[("About us", "https://akersolutions.com/about-us")])
    about = Page("https://akersolutions.com/about-us", text="Our story",
                 links=[("Legal notice", "https://akersolutions.com/legal")])
    legal = Page("https://akersolutions.com/legal", text="Legal", raw_text="AKER SOLUTIONS ASA")
    site = FakeSite({about.url: about, legal.url: legal})
    assert legal.url in [p.url for p in _ground([home], v, site)]


def test_fallback_does_not_loosen_matching():
    """A partial / foreign name on the about page still fails: domain rejected."""
    v = validator()
    about = Page("https://akersolutions.com/om-oss", text="Aker Solutions Inc., Houston. Aker Solutions")
    site = FakeSite({about.url: about})
    assert _ground([HOME_NO_NAME], v, site) == []
    assert not v.check("https://akersolutions.com").accepted


def test_fallback_ignores_other_domains_and_is_bounded():
    v = validator()
    links = [("About", "https://parent-group.com/about")] + [
        (f"Contact {i}", f"https://akersolutions.com/contact-{i}") for i in range(20)
    ]
    home = Page("https://akersolutions.com/", text="Hi", links=links)
    # Every page links back to more about/contact pages: would loop without bounds.
    looping = {f"https://akersolutions.com/contact-{i}": Page(
        f"https://akersolutions.com/contact-{i}", text="Contact", links=links) for i in range(20)}
    site = FakeSite({"https://parent-group.com/about": Page("https://parent-group.com/about",
                                                            raw_text="AKER SOLUTIONS ASA")} | looping)
    assert _ground([home], v, site) == []
    assert len(site.fetched) <= GROUNDING_MAX_FETCHES
    assert all(u.startswith("https://akersolutions.com/") for u in site.fetched)


def test_no_fallback_fetch_when_homepage_already_grounded():
    v = validator()
    home = Page("https://akersolutions.com/", text="x", raw_text="Aker Solutions ASA")
    site = FakeSite({})
    assert _ground([home], v, site) == [home]
    assert site.fetched == []


def test_no_fallback_for_empty_or_already_rejected_domains():
    v = validator()
    site = FakeSite({})
    assert _ground([Page("https://akersolutions.com/")], v, site)  # empty page: untouched
    v.verify_content("https://akersolutions.com", None)
    assert _ground([HOME_NO_NAME], v, site) == []
    assert site.fetched == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
