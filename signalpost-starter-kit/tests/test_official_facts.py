"""Official Brreg facts: CEO, employees and revenue parsing and precedence."""

import asyncio

import official_facts as of
from schema import CompanyProfile, SourcedFact


def _role(kode, fornavn="Kari", etternavn="Nordmann", **extra):
    return {"type": {"kode": kode}, "person": {"navn": {"fornavn": fornavn, "etternavn": etternavn}}, **extra}


def _roles(*roles):
    return {"rollegrupper": [{"type": {"kode": "X"}, "roller": list(roles)}]}


def _acct(typ, valuta, til, rev):
    return {
        "regnskapstype": typ, "valuta": valuta,
        "regnskapsperiode": {"fraDato": til[:4] + "-01-01", "tilDato": til},
        "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": rev}}},
    }


# --- CEO -------------------------------------------------------------------

def test_ceo_active_daglig_leder():
    body = _roles(_role("DAGL", "Anders", "Opedal"), _role("LEDE", "Jon", "Erik"))
    assert of.parse_ceo(body) == "Anders Opedal"


def test_ceo_includes_middle_name():
    body = _roles({"type": {"kode": "DAGL"}, "person": {"navn": {
        "fornavn": "Benedicte", "mellomnavn": "Schilbred", "etternavn": "Fasmer"}}})
    assert of.parse_ceo(body) == "Benedicte Schilbred Fasmer"


def test_ceo_ignores_resigned_and_deregistered():
    assert of.parse_ceo(_roles(_role("DAGL", fratraadt=True))) is None
    assert of.parse_ceo(_roles(_role("DAGL", avregistrert=True))) is None


def test_ceo_ambiguous_is_not_claimed():
    body = _roles(_role("DAGL", "A", "One"), _role("DAGL", "B", "Two"))
    assert of.parse_ceo(body) is None


def test_ceo_chair_only_is_not_ceo():
    assert of.parse_ceo(_roles(_role("LEDE"))) is None
    assert of.parse_ceo(None) is None
    assert of.parse_ceo({}) is None


# --- Employees -------------------------------------------------------------

def test_employees_only_when_registered():
    assert of.parse_employees({"harRegistrertAntallAnsatte": True, "antallAnsatte": 369}) == 369
    assert of.parse_employees({"harRegistrertAntallAnsatte": True, "antallAnsatte": 0}) == 0
    assert of.parse_employees({"harRegistrertAntallAnsatte": False, "antallAnsatte": 0}) is None
    assert of.parse_employees({"antallAnsatte": 12}) is None
    assert of.parse_employees(None) is None


# --- Revenue ---------------------------------------------------------------

def test_revenue_latest_own_entity_nok():
    accounts = [
        _acct("KONSERN", "NOK", "2025-12-31", 77292000000.0),
        _acct("SELSKAP", "NOK", "2024-12-31", 678000000.0),
        _acct("SELSKAP", "NOK", "2025-12-31", 355000000.0),
    ]
    assert of.parse_revenue_nok(accounts) == 355000000


def test_revenue_non_nok_is_dropped():
    assert of.parse_revenue_nok([_acct("SELSKAP", "USD", "2025-12-31", 6.7e10)]) is None


def test_revenue_group_only_is_dropped():
    assert of.parse_revenue_nok([_acct("KONSERN", "NOK", "2025-12-31", 1e9)]) is None


def test_revenue_missing_or_malformed():
    assert of.parse_revenue_nok(None) is None
    assert of.parse_revenue_nok([]) is None
    assert of.parse_revenue_nok([{"regnskapstype": "SELSKAP", "valuta": "NOK"}]) is None


def test_revenue_zero_is_a_value():
    assert of.parse_revenue_nok([_acct("SELSKAP", "NOK", "2025-12-31", 0.0)]) == 0


# --- Precedence ------------------------------------------------------------

def test_official_overrides_llm_and_keeps_other_fields():
    llm = CompanyProfile(
        ceo_name=SourcedFact(fact_value="Wrong Person", source_url="https://x.no", retrieval_date="2026-01-01"),
        website=SourcedFact(fact_value="https://x.no", source_url="https://x.no", retrieval_date="2026-01-01"),
    )
    official = {"ceo_name": SourcedFact(fact_value="Right Person",
                                        source_url=of.ROLES_URL.format(org="1"), retrieval_date="2026-11-15")}
    out = of.apply_official_facts(llm, official)
    assert out.ceo_name.fact_value == "Right Person"
    assert out.website.fact_value == "https://x.no"


def test_no_official_facts_leaves_profile():
    p = CompanyProfile(ceo_name=SourcedFact(fact_value="Kept"))
    assert of.apply_official_facts(p, {}).ceo_name.fact_value == "Kept"


def test_enrich_never_raises(monkeypatch):
    async def boom(_):
        raise RuntimeError("network down")
    monkeypatch.setattr(of, "fetch_official_facts", boom)
    p = CompanyProfile(ceo_name=SourcedFact(fact_value="Kept"))
    assert asyncio.run(of.enrich_with_official_facts("1", p)).ceo_name.fact_value == "Kept"
