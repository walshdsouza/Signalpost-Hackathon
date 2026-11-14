"""Synthesis: sourced 'what it does / what changed / what remains unknown'."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synthesis import attach_synthesis, build_synthesis  # noqa: E402

ORG = "923177035"
E_URL = f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}"
R_URL = E_URL + "/roller"
A_URL = f"https://data.brreg.no/regnskapsregisteret/regnskap/{ORG}"
EMPTY = {"fact_value": None, "source_url": None, "retrieval_date": None}


def fact(v, src, d="2026-11-15"):
    return {"fact_value": v, "source_url": src, "retrieval_date": d}


def acct(end, rev, result=None, typ="SELSKAP", valuta="NOK"):
    item = {"regnskapstype": typ, "valuta": valuta, "regnskapsperiode": {"tilDato": end},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {}}}}
    if rev is not None:
        item["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"]["sumDriftsinntekter"] = rev
    if result is not None:
        item["resultatregnskapResultat"]["aarsresultat"] = result
    return item


def record(form="AS", status="complete", **intel):
    base = {k: EMPTY for k in ("website", "ceo_name", "employee_count", "latest_revenue_nok", "careers")}
    base.update(news=[], company_news=[], job_postings=[], summary_of_changes="LLM text",
                socials={k: EMPTY for k in ("linkedin", "facebook", "instagram", "x", "youtube", "tiktok")})
    base.update(intel)
    return {"orgnr": ORG, "status": status, "fallback_reason": None,
            "brreg_metadata": {"navn": "102 ASIA MAT AS", "organisasjonsform": {"kode": form, "beskrivelse": "Aksjeselskap"},
                               "naeringskode1": {"kode": "47.110", "beskrivelse": "Butikkhandel med bredt vareutvalg"},
                               "forretningsadresse": {"poststed": "DRAMMEN"}},
            "intelligence_profile": base}


def context(entity=None, roles=None, accounts=None, ok=None):
    return {"urls": {"entity": E_URL, "roles": R_URL, "accounts": A_URL},
            "entity": entity, "roles": roles, "accounts": accounts,
            "ok": ok or {"entity": True, "roles": True, "accounts": True}}


ENTITY = {"stiftelsesdato": "2019-07-11",
          "aktivitet": ["Å selge asiatisk mat i butikk og nettbutikk", "med hjemkjøring."],
          "historiskeNavn": [{"navn": "VERDIBASERT ENDRING AS", "fraDato": "2019-08-09 11:19:42",
                              "tilDato": "2021-05-08 11:22:39"}]}
ROLES = {"rollegrupper": [{"type": {"kode": "DAGL"}, "sistEndret": "2023-04-02",
                           "roller": [{"type": {"kode": "DAGL"}, "person": {"navn": {"fornavn": "Quoc", "etternavn": "Le"}}}]}]}


def full():
    rec = record(
        website=fact("https://102asiamat.no", "https://102asiamat.no/"),
        ceo_name=fact("Quoc Trung Le", R_URL), employee_count=fact(8, E_URL),
        latest_revenue_nok=fact(9326623, A_URL),
        company_news=[{"title": "Ny butikk i Drammen", "url": "https://102asiamat.no/nyheter/ny",
                       "published_date": "2026-10-01", "retrieval_date": "2026-11-15"}],
        job_postings=[{"title": "Butikkmedarbeider", "url": "https://102asiamat.no/karriere/1", "retrieval_date": "2026-11-15"}],
        socials={"facebook": fact("https://facebook.com/102asiamat", "https://102asiamat.no/")},
    )
    ctx = context(ENTITY, ROLES, [acct("2024-12-31", 8000000.0), acct("2025-12-31", 9326623.0, result=412000.0)])
    return rec, ctx


def texts(section):
    return " | ".join(s["text"] for s in section)


def test_what_it_does_is_sourced_from_registry_and_site():
    rec, ctx = full()
    s = build_synthesis(rec, ctx, today="2026-11-15")
    does = texts(s["what_it_does"])
    assert "102 ASIA MAT AS (org.nr 923177035) is registered as Aksjeselskap (AS), based in Drammen, founded 2019-07-11." in does
    assert "Å selge asiatisk mat i butikk og nettbutikk med hjemkjøring." in does
    assert "Industry: Butikkhandel med bredt vareutvalg (NACE 47.110)." in does
    assert "Daglig leder (CEO): Quoc Trung Le." in does and "Registered employees: 8." in does
    assert "Website: https://102asiamat.no (verified" in does


def test_what_changed_revenue_result_rename_role_news_jobs():
    rec, ctx = full()
    ch = texts(build_synthesis(rec, ctx, today="2026-11-15")["what_changed"])
    assert "Revenue rose 17% from NOK 8 000 000 (FY2024) to NOK 9 326 623 (FY2025)." in ch
    assert "FY2025 annual result: profit of NOK 412 000." in ch
    assert "Previously named VERDIBASERT ENDRING AS (until 2021-05-08)." in ch
    assert "Daglig leder registration last changed in Brreg on 2023-04-02." in ch
    assert "2026-10-01: Ny butikk i Drammen" in ch
    assert "Hiring: 1 open position (Butikkmedarbeider)." in ch


def test_every_statement_has_a_source_and_summary_replaces_llm_text():
    rec, ctx = full()
    out = attach_synthesis(rec, ctx)
    syn = out["intelligence_profile"]["synthesis"]
    for st in syn["what_it_does"] + syn["what_changed"]:
        assert st["source_url"] and st["source_url"].startswith("https://"), st
        assert st["retrieval_date"]
    assert out["intelligence_profile"]["summary_of_changes"] == syn["summary"] != "LLM text"
    assert A_URL in syn["sources"] and "https://102asiamat.no/nyheter/ny" in syn["sources"]
    assert syn["unknown"] == []  # everything found


def test_bankruptcy_liquidation_and_loss():
    rec = record()
    ent = {"konkurs": True, "konkursdato": "2026-07-08", "underAvvikling": True, "underAvviklingDato": "2026-09-30"}
    ch = texts(build_synthesis(rec, context(ent, {}, [acct("2025-12-31", None, result=-22033.0)]))["what_changed"])
    assert "Declared bankrupt on 2026-07-08." in ch and "Under liquidation since 2026-09-30." in ch
    assert "FY2025 annual result: loss of NOK 22 033." in ch


def test_unknown_reasons_are_specific():
    rec = record(form="FLI")
    unk = {u["field"]: u["reason"] for u in build_synthesis(rec, context({}, {"rollegrupper": []}, None))["unknown"]}
    assert "No active daglig leder" in unk["ceo_name"]
    assert "No employee count is registered" in unk["employee_count"]
    assert "(FLI) only has to file above certain thresholds" in unk["latest_revenue_nok"]
    assert "No website is registered" in unk["website"]
    assert "news" not in unk and "job_postings" not in unk  # no site: not claimed as checked


def test_revenue_unknown_reasons_usd_bank_empty_failed():
    def reason(accounts, ok=None):
        unk = build_synthesis(record(), context({}, {}, accounts, ok))["unknown"]
        return next(u["reason"] for u in unk if u["field"] == "latest_revenue_nok")
    assert reason([acct("2025-12-31", 6e10, valuta="USD")]) == "Accounts are reported in USD, not NOK."
    assert "does not publish this account type" in reason({"_unsupported": True})
    assert reason([acct("2025-12-31", None)]) == "The filed accounts report no operating revenue."
    assert "could not be retrieved" in reason(None, {"entity": True, "roles": True, "accounts": False})
    assert reason(None) == "No annual accounts are available in Regnskapsregisteret."  # AS must file


def test_group_accounts_and_flat_revenue():
    ctx = context({}, {}, [acct("2024-12-31", 100.0), acct("2025-12-31", 100.0),
                           acct("2025-12-31", 9e9, typ="KONSERN")])
    ch = texts(build_synthesis(record(), ctx)["what_changed"])
    assert "Revenue was flat from NOK 100 (FY2024) to NOK 100 (FY2025)." in ch


def test_news_limited_and_deduplicated_newest_first():
    items = [{"title": f"N{i}", "url": f"https://a.no/n/{i}", "published_date": f"2026-0{i}-01"} for i in range(1, 6)]
    rec = record(company_news=items[:3], news=items[2:])
    ch = [s for s in build_synthesis(rec, context({}, {}, None))["what_changed"] if s["text"][:4] == "2026"]
    assert [s["text"] for s in ch] == ["2026-05-01: N5", "2026-04-01: N4", "2026-03-01: N3"]


def test_missing_registry_record_and_never_raises():
    rec = {"orgnr": "1", "status": "fallback", "fallback_reason": "Organisation not found in Brreg (404).",
           "brreg_metadata": {"navn": None}, "intelligence_profile": {}}
    syn = build_synthesis(rec, None)
    assert syn["unknown"][0]["reason"].startswith("Organisation not found") and syn["what_it_does"] == []
    broken = {"orgnr": "1", "brreg_metadata": {"navn": "X"}, "intelligence_profile": {"website": "not-a-dict"}}
    assert attach_synthesis(broken, None) is not None  # degrades, never raises


def test_socials_unknown_reason_depends_on_what_was_checked():
    site = fact("https://102asiamat.no", "https://102asiamat.no/")
    llm = {u["field"]: u["reason"] for u in build_synthesis(record(website=site), context({}, {}, None))["unknown"]}
    assert "exact legal name" in llm["socials"]
    no_llm = {u["field"]: u["reason"] for u in
              build_synthesis(record(status="fallback", website=site), context({}, {}, None))["unknown"]}
    assert "were not checked" in no_llm["socials"]


def test_unchanged_name_history_and_zero_revenue_are_not_changes():
    ent = {"historiskeNavn": [{"navn": "102 asia  mat AS", "tilDato": "2015-08-18 10:00:00"},
                              {"navn": "OLD NAME AS", "tilDato": "2012-05-05 10:00:00"}]}
    ch = texts(build_synthesis(record(), context(ent, {}, [acct("2023-12-31", 0.0), acct("2024-12-31", 0.0)]))["what_changed"])
    assert "Previously named OLD NAME AS (until 2012-05-05)." in ch
    assert "102 asia" not in ch.lower().replace("102 asia mat as (org", "")
    assert "Revenue" not in ch
    up = texts(build_synthesis(record(), context({}, {}, [acct("2023-12-31", 0.0), acct("2024-12-31", 5000.0)]))["what_changed"])
    assert "Revenue went from NOK 0 (FY2023) to NOK 5 000 (FY2024)." in up
