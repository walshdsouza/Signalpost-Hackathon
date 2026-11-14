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


# Fields reported in ``availability``, in display order.
AVAILABILITY_FIELDS = ("website", "ceo_name", "employee_count", "latest_revenue_nok",
                       "news", "job_postings", "careers", "socials")
NO_SITE = "Not checked: no verified company website."


def _assess(brreg, intel, ctx, record) -> dict[str, dict]:
    """``{field: {"state", "reason"}}`` for every field that has no value.

    States follow the output contract: ``not_available`` (looked, nothing there),
    ``not_applicable`` (the question does not apply to this organisation form),
    ``failed`` (the lookup or extraction step broke). ``blocked`` / ``ambiguous``
    are never guessed.
    """
    out: dict[str, dict] = {}
    ok = (ctx or {}).get("ok") or {}
    roles, accounts = (ctx or {}).get("roles"), (ctx or {}).get("accounts")
    form = ((brreg.get("organisasjonsform") or {}).get("kode") or "").upper()
    has_site = bool(_value(intel.get("website") or {}))
    reason_text = record.get("fallback_reason") or ""
    web_failed = reason_text.startswith("Web extraction failed")
    llm_issue = record.get("status") == "fallback" and not web_failed and bool(reason_text)

    def add(field, state, reason):
        out[field] = {"state": state, "reason": reason}

    if not has_site:
        if web_failed:
            add("website", "failed", reason_text)
        else:
            add("website", "not_available", "No website is registered in Brreg and no site was found that "
                                            "shows the company's legal name or org number.")

    if not _value(intel.get("ceo_name") or {}):
        holders = [r for g in (roles or {}).get("rollegrupper") or [] for r in g.get("roller") or []
                   if (r.get("type") or {}).get("kode") == "INNH"]
        if ok.get("roles") is False:
            add("ceo_name", "failed", "Brreg roles could not be retrieved.")
        elif form == "ENK" and holders:
            add("ceo_name", "not_applicable", "Sole proprietorship run by its registered owner (innehaver); "
                                              "no daglig leder role.")
        else:
            add("ceo_name", "not_available", "No active daglig leder (CEO) is registered in Brreg.")

    if _value(intel.get("employee_count") or {}) is None:
        if ok.get("entity") is False:
            add("employee_count", "failed", "Brreg could not be retrieved.")
        else:
            add("employee_count", "not_available", "No employee count is registered in Brreg.")

    if _value(intel.get("latest_revenue_nok") or {}) is None:
        own = _own_accounts(accounts)
        if ok.get("accounts") is False:
            add("latest_revenue_nok", "failed", "The accounts register (Regnskapsregisteret) could not be retrieved.")
        elif isinstance(accounts, dict) and accounts.get("_unsupported"):
            add("latest_revenue_nok", "not_available",
                "Brreg's accounts API does not publish this account type (e.g. banks and insurers).")
        elif own and not any(item.get("valuta") == "NOK" for _, item in own):
            add("latest_revenue_nok", "not_available", f"Accounts are reported in {own[-1][1].get('valuta')}, not NOK.")
        elif own:
            add("latest_revenue_nok", "not_available", "The filed accounts report no operating revenue.")
        elif form in ALWAYS_FILE_ACCOUNTS:
            add("latest_revenue_nok", "not_available", "No annual accounts are available in Regnskapsregisteret.")
        else:
            add("latest_revenue_nok", "not_applicable",
                f"No annual accounts are filed; this organisation form ({form or 'unknown'}) "
                "only has to file above certain thresholds.")

    if not intel.get("company_news") and not intel.get("news"):
        add("news", "not_available", "No dated news article that names the company was found on its verified "
                                     "website." if has_site else NO_SITE)
    if not intel.get("job_postings"):
        add("job_postings", "not_available", "No open job posting that names the company was found."
            if has_site else NO_SITE)

    # Careers link and social profiles come from the LLM step.
    for field, found in (("careers", _value(intel.get("careers") or {})),
                         ("socials", any(_value(v) for v in (intel.get("socials") or {}).values()))):
        if found:
            continue
        if not has_site:
            add(field, "not_available", NO_SITE)
        elif llm_issue:
            add(field, "failed", f"Not checked: {reason_text.rstrip('.')}.")
        elif field == "socials":
            add(field, "not_available", "No social profile whose handle is the company's exact legal name was found.")
        else:
            add(field, "not_available", "No careers page on the company's verified site was found.")
    return out


def _availability(assessed: dict[str, dict]) -> dict[str, dict]:
    return {f: assessed.get(f) or {"state": "available"} for f in AVAILABILITY_FIELDS}


def _unknown(assessed: dict[str, dict]) -> list[dict]:
    """Missing fields worth stating; items that were merely not checked for lack of a
    website are covered by the website entry and not repeated."""
    return [{"field": f, **assessed[f]} for f in AVAILABILITY_FIELDS
            if f in assessed and not (f != "website" and assessed[f]["reason"] == NO_SITE)
            and f != "careers"]


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

    if not brreg.get("navn"):
        reason = record.get("fallback_reason") or "Organisation not found in Brreg."
        state = "not_available" if "404" in reason or "not found" in reason.lower() else "failed"
        unknown = [{"field": "all", "state": state, "reason": reason}]
        return {"what_it_does": [], "what_changed": [], "unknown": unknown, "sources": [],
                "availability": {f: {"state": state, "reason": reason} for f in AVAILABILITY_FIELDS},
                "summary": f"No registry record could be retrieved for {orgnr}. {reason}"}

    does = _what_it_does(orgnr, brreg, intel, ctx.get("entity"), urls, today)
    changed = _what_changed(intel, ctx.get("entity"), ctx.get("roles"), ctx.get("accounts"), urls, today,
                            brreg.get("navn") or "")
    assessed = _assess(brreg, intel, ctx, record)
    unknown = _unknown(assessed)
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
            "availability": _availability(assessed),
            "sources": sources, "summary": re.sub(r"\s+", " ", summary).strip()}


def attach_synthesis(record: dict, context: dict | None) -> dict:
    """Add ``intelligence_profile.synthesis`` and regenerate ``summary_of_changes``. Never raises."""
    try:
        syn = build_synthesis(record, context)
        intel = dict(record.get("intelligence_profile") or {})
        intel["synthesis"] = syn
        intel["availability"] = syn.get("availability", {})
        intel["summary_of_changes"] = syn["summary"]
        return {**record, "intelligence_profile": intel}
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Synthesis skipped (%s: %s)", record.get("orgnr"), type(e).__name__, e)
        return record
