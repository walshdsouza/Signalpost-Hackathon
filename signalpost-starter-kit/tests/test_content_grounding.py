"""Offline tests for strict website grounding (name / org number in page text).

Run with:  python tests/test_content_grounding.py   (or pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from domain_validator import CONTENT_MISMATCH_REASON, DomainValidator  # noqa: E402
from extractor import Page, verify_pages  # noqa: E402
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


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
