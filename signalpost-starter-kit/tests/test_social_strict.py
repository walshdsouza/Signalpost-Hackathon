"""Strict social-profile attribution: the handle must BE the entity's legal name."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from domain_validator import DomainValidator  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")


def v(navn, parent=None):
    ancestors = [Enhet(organisasjonsnummer="9", navn=parent, organisasjonsform=FORM)] if parent else []
    return DomainValidator(Enhet(organisasjonsnummer="1", navn=navn, organisasjonsform=FORM), ancestors)


def ok(val, url):
    return val.check_social(url).accepted


def test_subsidiary_accounts_rejected_for_parent_entity():
    t = v("TELENOR ASA")
    assert not ok(t, "https://facebook.com/telenornorge")
    assert not ok(t, "https://instagram.com/telenornorge")
    assert not ok(t, "https://twitter.com/TelenorNorge")
    assert not ok(t, "https://youtube.com/user/TelenorNorway")


def test_exact_name_accepted_with_or_without_suffix():
    t = v("TELENOR ASA")
    assert ok(t, "https://facebook.com/telenor")
    assert ok(t, "https://www.linkedin.com/company/telenor-asa")
    assert ok(t, "https://youtube.com/@Telenor")


def test_real_sample_handles_still_accepted():
    assert ok(v("1313 SELAH AS"), "https://facebook.com/1313SELAH")
    assert ok(v("102 ASIA MAT AS"), "https://instagram.com/102asiamat")
    assert ok(v("123VINDUSPUSS AS"), "https://facebook.com/123vinduspuss")
    assert ok(v("EQUINOR ASA"), "https://www.linkedin.com/company/equinor")
    assert ok(v("AKER SOLUTIONS ASA"), "https://www.linkedin.com/company/aker-solutions")


def test_group_brand_rejected_for_subsidiary():
    assert not ok(v("EQUINOR ENERGY AS"), "https://www.linkedin.com/company/equinor")
    assert not ok(v("AKER SOLUTIONS SUBSEA AS"), "https://www.linkedin.com/company/aker-solutions")


def test_no_fuzzy_or_partial_matches():
    e = v("EQUINOR ASA")
    assert not ok(e, "https://facebook.com/equinr")            # typo
    assert not ok(e, "https://facebook.com/equinorjobs")       # extra word
    assert not ok(e, "https://facebook.com/equinor.careers")
    assert not ok(v("DNB BANK ASA"), "https://facebook.com/dnb")  # abbreviation not proven


def test_norwegian_letters_both_spellings():
    b = v("BRØDRENE ÅSE AS")
    assert ok(b, "https://facebook.com/brodreneaase")
    assert ok(b, "https://facebook.com/brodrenease")
    assert ok(b, "https://facebook.com/broedreneaase")
    assert not ok(b, "https://facebook.com/brodrene")


def test_parent_name_handle_rejected():
    assert not ok(v("AKER SOLUTIONS SUBSEA AS", parent="AKER ASA"), "https://linkedin.com/company/aker")


def test_malformed_urls():
    t = v("TELENOR ASA")
    assert not ok(t, "https://facebook.com/")
    assert not ok(t, "https://linkedin.com/company/")
    assert not ok(t, None)
