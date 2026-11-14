"""
Synthesis — a sourced explanation of each company: what it does, what changed,
and what remains unknown.

Built deterministically (no LLM) AFTER every verification step, only from:
  * the Brreg registry bodies already fetched by ``official_facts`` (context), and
  * facts already in the verified profile (website, news, job postings ...).

Every statement carries the URL it rests on; nothing is inferred beyond the
sources. The plain-text ``summary_of_changes`` is regenerated from the same
statements, replacing free LLM prose (which could contain unsourced numbers).
"""

from __future__ import annotations

import logging
import re
from datetime import date

log = logging.getLogger(__name__)

ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_URL = ENTITY_URL + "/roller"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"

# Forms that must always file annual accounts (Regnskapsloven); others only above thresholds.
ALWAYS_FILE_ACCOUNTS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}
MAX_NEWS_STATEMENTS = 3
MAX_ACTIVITY_CHARS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nok(n) -> str:
    return f"NOK {int(round(n)):,}".replace(",", " ")


def _stmt(text: str, source_url: str | None, retrieval_date: str, when: str | None = None) -> dict:
    out = {"text": text, "source_url": source_url, "retrieval_date": retrieval_date}
    if when:
        out["date"] = when
    return out


def _value(fact) -> object:
    return fact.get("fact_value") if isinstance(fact, dict) else None


def _joined(lines) -> str | None:
    if not lines:
        return None
    text = " ".join(" ".join(str(x).split()) for x in lines if x).strip()
    if len(text) > MAX_ACTIVITY_CHARS:
        text = text[:MAX_ACTIVITY_CHARS].rsplit(" ", 1)[0] + " …"
    return text or None


def _own_accounts(accounts) -> list[tuple[str, dict]]:
    """Own-entity (SELSKAP) accounts as (period_end, record), oldest first."""
    if not isinstance(accounts, list):
        return []
    rows = []
    for item in accounts:
        if isinstance(item, dict) and item.get("regnskapstype") == "SELSKAP":
            end = (item.get("regnskapsperiode") or {}).get("tilDato")
            if end:
                rows.append((end, item))
    return sorted(rows, key=lambda r: r[0])


def _revenue(item: dict):
    return (((item.get("resultatregnskapResultat") or {}).get("driftsresultat") or {})
            .get("driftsinntekter") or {}).get("sumDriftsinntekter")


def _dagl_changed(roles) -> str | None:
    if not isinstance(roles, dict):
        return None
    for group in roles.get("rollegrupper") or []:
        if (group.get("type") or {}).get("kode") == "DAGL" and group.get("sistEndret"):
            return str(group["sistEndret"])[:10]
    return None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _what_it_does(orgnr, brreg, intel, entity, urls, today) -> list[dict]:
    out = []
    name = brreg.get("navn") or orgnr
    form = (brreg.get("organisasjonsform") or {}).get("beskrivelse")
    code = (brreg.get("organisasjonsform") or {}).get("kode")
    addr = brreg.get("forretningsadresse") or {}
    place = addr.get("poststed") or addr.get("kommune")
    founded = (entity or {}).get("stiftelsesdato")
    head = f"{name} (org.nr {orgnr}) is registered as {form}{f' ({code})' if code else ''}" if form         else f"{name} (org.nr {orgnr}) is a registered Norwegian entity"
    parts = [head]
    if place:
        parts.append(f"based in {place.title()}")
    if founded:
        parts.append(f"founded {founded}")
    out.append(_stmt(", ".join(parts) + ".", urls["entity"], today))

    activity = _joined((entity or {}).get("aktivitet")) or _joined((entity or {}).get("vedtektsfestetFormaal"))
    if activity:
        out.append(_stmt(f"Registered activity (Brreg, Norwegian): “{activity}”", urls["entity"], today))

    nace = brreg.get("naeringskode1") or {}
    if nace.get("beskrivelse"):
        out.append(_stmt(f"Industry: {nace['beskrivelse']} (NACE {nace.get('kode')}).", urls["entity"], today))

    ceo, emp = intel.get("ceo_name") or {}, intel.get("employee_count") or {}
    if _value(ceo):
        out.append(_stmt(f"Daglig leder (CEO): {_value(ceo)}.", ceo.get("source_url"), ceo.get("retrieval_date") or today))
    if _value(emp) is not None:
        out.append(_stmt(f"Registered employees: {_value(emp)}.", emp.get("source_url"), emp.get("retrieval_date") or today))
    rev = intel.get("latest_revenue_nok") or {}
    if _value(rev) is not None:
        out.append(_stmt(f"Latest reported revenue: {_nok(_value(rev))}.", rev.get("source_url"),
                         rev.get("retrieval_date") or today))

    site = intel.get("website") or {}
    if _value(site):
        how = ("registered in Brreg" if "brreg.no" in (site.get("source_url") or "")
               else "verified: the site shows the company's legal name or org number")
        out.append(_stmt(f"Website: {_value(site)} ({how}).", site.get("source_url"), site.get("retrieval_date") or today))
    return out


def _same_name(a: str, b: str) -> bool:
    return " ".join(str(a).casefold().split()) == " ".join(str(b).casefold().split())


def _what_changed(intel, entity, roles, accounts, urls, today, current_name: str = "") -> list[dict]:
    out = []
    e = entity or {}
    if e.get("konkurs"):
        out.append(_stmt(f"Declared bankrupt{(' on ' + e['konkursdato']) if e.get('konkursdato') else ''}.",
                         urls["entity"], today, e.get("konkursdato")))
    if e.get("underAvvikling"):
        out.append(_stmt(f"Under liquidation{(' since ' + e['underAvviklingDato']) if e.get('underAvviklingDato') else ''}.",
                         urls["entity"], today, e.get("underAvviklingDato")))
    if e.get("underTvangsavviklingEllerTvangsopplosning"):
        out.append(_stmt("Under compulsory liquidation or dissolution.", urls["entity"], today))

    history = [h for h in (e.get("historiskeNavn") or []) if isinstance(h, dict) and h.get("navn")
               and not _same_name(h["navn"], current_name)]
    history.sort(key=lambda h: str(h.get("tilDato") or ""), reverse=True)
    for h in history[:2]:
        until = str(h.get("tilDato") or "")[:10] or None
        out.append(_stmt(f"Previously named {h['navn']}{(' (until ' + until + ')') if until else ''}.",
                         urls["entity"], today, until))

    own = [(end, item) for end, item in _own_accounts(accounts) if item.get("valuta") == "NOK"]
    with_rev = [(end, _revenue(item)) for end, item in own if _revenue(item) is not None]
    if len(with_rev) >= 2 and (with_rev[-2][1] or with_rev[-1][1]):
        (p_end, prev), (l_end, last) = with_rev[-2], with_rev[-1]
        if prev:
            pct = (last - prev) / abs(prev) * 100
            trend = "rose" if pct > 0.5 else "fell" if pct < -0.5 else "was flat"
            change = f" {abs(pct):.0f}%" if trend != "was flat" else ""
            text = (f"Revenue {trend}{change} from {_nok(prev)} (FY{p_end[:4]}) "
                    f"to {_nok(last)} (FY{l_end[:4]}).")
        else:
            text = f"Revenue went from {_nok(prev)} (FY{p_end[:4]}) to {_nok(last)} (FY{l_end[:4]})."
        out.append(_stmt(text, urls["accounts"], today, l_end))
    if own:
        l_end, item = own[-1]
        result = (item.get("resultatregnskapResultat") or {}).get("aarsresultat")
        if result is not None:
            word = "profit" if result >= 0 else "loss"
            out.append(_stmt(f"FY{l_end[:4]} annual result: {word} of {_nok(abs(result))}.", urls["accounts"], today, l_end))

    changed = _dagl_changed(roles)
    if changed:
        out.append(_stmt(f"Daglig leder registration last changed in Brreg on {changed}.", urls["roles"], today, changed))

    news = list(intel.get("company_news") or [])
    seen = {n.get("url") for n in news}
    news += [n for n in intel.get("news") or [] if n.get("url") not in seen]
    news.sort(key=lambda n: n.get("published_date") or "", reverse=True)
    for n in news[:MAX_NEWS_STATEMENTS]:
        out.append(_stmt(f"{n.get('published_date')}: {n.get('title')}", n.get("url"),
                         n.get("retrieval_date") or today, n.get("published_date")))

    jobs = intel.get("job_postings") or []
    if jobs:
        titles = ", ".join(j.get("title") or "" for j in jobs[:3])
        more = f" and {len(jobs) - 3} more" if len(jobs) > 3 else ""
        out.append(_stmt(f"Hiring: {len(jobs)} open position{'s' if len(jobs) != 1 else ''} ({titles}{more}).",
                         jobs[0].get("url"), jobs[0].get("retrieval_date") or today))
    return out


def _unknown(brreg, intel, ctx, llm_ran: bool) -> list[dict]:
    out = []
    ok = (ctx or {}).get("ok") or {}
    accounts = (ctx or {}).get("accounts")
    form = ((brreg.get("organisasjonsform") or {}).get("kode") or "").upper()
    has_site = bool(_value(intel.get("website") or {}))

    def add(field, reason):
        out.append({"field": field, "reason": reason})

    if not has_site:
        add("website", "No website is registered in Brreg and no site was found that shows the company's "
                       "legal name or org number.")
    if not _value(intel.get("ceo_name") or {}):
        add("ceo_name", "Brreg roles could not be retrieved." if ok.get("roles") is False
            else "No active daglig leder (CEO) is registered in Brreg.")
    if _value(intel.get("employee_count") or {}) is None:
        add("employee_count", "Brreg could not be retrieved." if ok.get("entity") is False
            else "No employee count is registered in Brreg.")
    if _value(intel.get("latest_revenue_nok") or {}) is None:
        own = _own_accounts(accounts)
        if ok.get("accounts") is False:
            reason = "The accounts register (Regnskapsregisteret) could not be retrieved."
        elif isinstance(accounts, dict) and accounts.get("_unsupported"):
            reason = "Brreg's accounts API does not publish this account type (e.g. banks and insurers)."
        elif own and not any(item.get("valuta") == "NOK" for _, item in own):
            reason = f"Accounts are reported in {own[-1][1].get('valuta')}, not NOK."
        elif own:
            reason = "The filed accounts report no operating revenue."
        elif form in ALWAYS_FILE_ACCOUNTS:
            reason = "No annual accounts are available in Regnskapsregisteret."
        else:
            reason = (f"No annual accounts are filed; this organisation form ({form or 'unknown'}) "
                      "only has to file above certain thresholds.")
        add("latest_revenue_nok", reason)
    if has_site:
        if not intel.get("company_news") and not intel.get("news"):
            add("news", "No dated news article that names the company was found on its verified website.")
        if not intel.get("job_postings"):
            add("job_postings", "No open job posting that names the company was found.")
    socials = intel.get("socials") or {}
    if not any(_value(v) for v in socials.values()):
        add("socials", "No social profile whose handle is the company's exact legal name was found."
            if llm_ran and has_site else "Social profiles were not checked (no verified website or no LLM).")
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_synthesis(record: dict, context: dict | None, today: str | None = None) -> dict:
    """``{"what_it_does": [...], "what_changed": [...], "unknown": [...], "sources": [...], "summary": str}``."""
    today = today or date.today().isoformat()
    orgnr = str(record.get("orgnr") or "")
    brreg = record.get("brreg_metadata") or {}
    intel = record.get("intelligence_profile") or {}
    ctx = context or {}
    urls = ctx.get("urls") or {
        "entity": ENTITY_URL.format(org=orgnr), "roles": ROLES_URL.format(org=orgnr),
        "accounts": ACCOUNTS_URL.format(org=orgnr),
    }
    llm_ran = record.get("status") == "complete"

    if not brreg.get("navn"):
        unknown = [{"field": "all", "reason": record.get("fallback_reason") or "Organisation not found in Brreg."}]
        return {"what_it_does": [], "what_changed": [], "unknown": unknown, "sources": [],
                "summary": f"No registry record could be retrieved for {orgnr}. {unknown[0]['reason']}"}

    does = _what_it_does(orgnr, brreg, intel, ctx.get("entity"), urls, today)
    changed = _what_changed(intel, ctx.get("entity"), ctx.get("roles"), ctx.get("accounts"), urls, today,
                            brreg.get("navn") or "")
    unknown = _unknown(brreg, intel, ctx, llm_ran)
    sources = list(dict.fromkeys(s["source_url"] for s in does + changed if s.get("source_url")))

    summary = " ".join(s["text"] for s in does[:2])
    if changed:
        summary += " Recent: " + " ".join(s["text"] for s in changed[:2])
    else:
        summary += " No recorded changes were found in the sources checked."
    if unknown:
        labels = {"website": "website", "ceo_name": "CEO", "employee_count": "employee count",
                  "latest_revenue_nok": "revenue", "news": "news", "job_postings": "open jobs", "socials": "social profiles"}
        summary += " Unknown: " + ", ".join(labels.get(u["field"], u["field"]) for u in unknown) + "."
    return {"what_it_does": does, "what_changed": changed, "unknown": unknown,
            "sources": sources, "summary": re.sub(r"\s+", " ", summary).strip()}


def attach_synthesis(record: dict, context: dict | None) -> dict:
    """Add ``intelligence_profile.synthesis`` and regenerate ``summary_of_changes``. Never raises."""
    try:
        syn = build_synthesis(record, context)
        intel = dict(record.get("intelligence_profile") or {})
        intel["synthesis"] = syn
        intel["summary_of_changes"] = syn["summary"]
        return {**record, "intelligence_profile": intel}
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Synthesis skipped (%s: %s)", record.get("orgnr"), type(e).__name__, e)
        return record
