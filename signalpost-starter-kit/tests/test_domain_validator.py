"""Offline tests for subsidiary-vs-parent domain validation.

Run with:  python tests/test_domain_validator.py   (or pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from domain_validator import DomainValidator, registrable_domain  # noqa: E402
from schema import CompanyProfile, SourcedFact  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")


def enhet(orgnr, navn, hjemmeside=None, parent=None):
    return Enhet(
        organisasjonsnummer=orgnr, navn=navn, organisasjonsform=FORM,
        hjemmeside=hjemmeside, overordnetEnhet=parent,
    )


PARENT = enhet("923609016", "EQUINOR ASA", "www.equinor.com")


def test_registrable_domain():
    assert registrable_domain("https://www.news.equinor.com/a") == "equinor.com"
    assert registrable_domain("equinor.com") == "equinor.com"
    assert registrable_domain("http://shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("ingen") is None
    assert registrable_domain(None) is None


def test_parent_domain_rejected_when_brreg_lists_parent():
    v = DomainValidator(enhet("990888213", "EQUINOR ENERGY AS", parent="923609016"), [PARENT])
    assert not v.check("https://www.equinor.com/news").accepted
    assert not v.check("https://investor.equinor.com").accepted


def test_parent_domain_rejected_by_name_even_without_brreg_parent():
    # overordnetEnhet is often unset; the name check must still reject it.
    v = DomainValidator(enhet("990888213", "EQUINOR ENERGY AS"))
    assert not v.check("https://www.equinor.com").accepted
    assert v.check("https://www.equinorenergy.no").accepted


def test_subsidiary_listing_parent_site_in_brreg_is_not_used():
    sub = enhet("990888213", "EQUINOR ENERGY AS", hjemmeside="www.equinor.com", parent="923609016")
    v = DomainValidator(sub, [PARENT])
    assert v.registered_site_url is None
    # ...and without a known parent, a shorter group brand is still refused
    v = DomainValidator(enhet("990888213", "EQUINOR ENERGY AS", hjemmeside="www.equinor.com"))
    assert v.registered_site_url is None


def test_own_registered_site_accepted():
    v = DomainValidator(enhet("1", "AKER SOLUTIONS ASA", hjemmeside="https://akersolutions.com"))
    assert v.registered_site_url == "https://akersolutions.com"
    assert v.check("https://www.akersolutions.com/news").accepted
    v = DomainValidator(enhet("2", "FOO BAR AS", hjemmeside="brandname.no"))  # unrelated brand domain
    assert v.registered_site_url == "https://brandname.no"


def test_norwegian_letters_and_unrelated_domains():
    v = DomainValidator(enhet("3", "BRØDRENE ÅSE AS"))
    assert v.check("https://brodrenease.no").accepted  # ø/å fold to ascii
    assert not v.check("https://www.proff.no/selskap/x").accepted
    assert not v.check("https://linkedin.com/company/x").accepted


def test_sanitize_drops_parent_sourced_facts_and_fills_registry_website():
    sub = enhet("990888213", "EQUINOR ENERGY AS", parent="923609016")
    v = DomainValidator(sub, [PARENT])
    profile = CompanyProfile(
        website=SourcedFact(fact_value="https://www.equinor.com", source_url="https://www.equinor.com", retrieval_date="2026-01-01"),
        ceo_name=SourcedFact(fact_value="Anders Opedal", source_url="https://www.equinor.com/leaders", retrieval_date="2026-01-01"),
        employee_count=SourcedFact(fact_value=1000, source_url="https://example.org/x", retrieval_date="2026-01-01"),
    )
    clean = v.sanitize_profile(profile)
    assert clean.website.fact_value is None
    assert clean.ceo_name.fact_value is None
    assert clean.employee_count.fact_value == 1000  # third-party source untouched


def test_purge_cached_removes_parent_website():
    sub = enhet("990888213", "EQUINOR ENERGY AS", parent="923609016")
    v = DomainValidator(sub, [PARENT])
    cached = {"intelligence_profile": {"website": {"fact_value": "https://www.equinor.com", "source_url": None, "retrieval_date": None}}}
    out = v.purge_cached(cached)
    assert out["intelligence_profile"]["website"]["fact_value"] is None
    assert cached["intelligence_profile"]["website"]["fact_value"] == "https://www.equinor.com"  # input untouched


def test_registry_website_fills_empty_profile():
    v = DomainValidator(enhet("1", "AKER SOLUTIONS ASA", hjemmeside="akersolutions.com"))
    out = v.sanitize_profile(CompanyProfile())
    assert out.website.fact_value == "https://akersolutions.com"
    assert "data.brreg.no" in out.website.source_url


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
