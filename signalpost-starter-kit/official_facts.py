"""
Official Brreg facts — CEO, employee count and revenue for the exact legal entity.

These come straight from Norway's official registers, keyed by organisation
number, so they cannot belong to a different company. They take precedence
over anything the LLM extracted from web pages.

  * ceo_name           ← Enhetsregisteret roles, active "DAGL" (daglig leder)
  * employee_count     ← Enhetsregisteret ``antallAnsatte`` (only when registered)
  * latest_revenue_nok ← Regnskapsregisteret, the entity's own accounts
                         ("SELSKAP", not group/"KONSERN"), latest period,
                         NOK only — a USD filer gets no revenue rather than a
                         mislabelled one.

``source_url`` is the public API URL that contains the value verbatim.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx

from schema import CompanyProfile, SourcedFact

log = logging.getLogger(__name__)

ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_URL = ENTITY_URL + "/roller"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"

_TIMEOUT = 15.0
_ATTEMPTS = 3


async def _get_json(client: httpx.AsyncClient, url: str):
    """GET JSON; ``None`` on 404/no content. Retries transient failures."""
    for attempt in range(_ATTEMPTS):
        try:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code in (204, 404, 410):
                return None
            if r.status_code == 500 and "ikke er stottet" in r.text:
                return {"_unsupported": True}  # e.g. bank/insurance accounts: permanent
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == _ATTEMPTS - 1:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Pure parsers (unit-tested)
# ---------------------------------------------------------------------------

def parse_employees(entity: dict | None) -> int | None:
    if not isinstance(entity, dict) or not entity.get("harRegistrertAntallAnsatte"):
        return None
    n = entity.get("antallAnsatte")
    return n if isinstance(n, int) and n >= 0 else None


def parse_ceo(roles: dict | None) -> str | None:
    """Name of the single active daglig leder, else ``None``."""
    if not isinstance(roles, dict):
        return None
    names = []
    for group in roles.get("rollegrupper") or []:
        for role in group.get("roller") or []:
            if (role.get("type") or {}).get("kode") != "DAGL":
                continue
            if role.get("avregistrert") or role.get("fratraadt"):
                continue
            n = (role.get("person") or {}).get("navn") or {}
            name = " ".join(filter(None, [n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn")]))
            if name:
                names.append(name)
    # Exactly one active CEO; ambiguous or company-held roles are not claimed.
    return names[0] if len(set(names)) == 1 else None


def parse_revenue_nok(accounts) -> int | None:
    """Latest-period own-entity revenue in NOK, else ``None``."""
    if not isinstance(accounts, list):
        return None
    best: tuple[str, int] | None = None
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if item.get("regnskapstype") != "SELSKAP" or item.get("valuta") != "NOK":
            continue
        period_end = (item.get("regnskapsperiode") or {}).get("tilDato") or ""
        rev = (((item.get("resultatregnskapResultat") or {}).get("driftsresultat") or {})
               .get("driftsinntekter") or {}).get("sumDriftsinntekter")
        if rev is None or not period_end:
            continue
        if best is None or period_end > best[0]:
            best = (period_end, int(round(float(rev))))
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Fetch + apply
# ---------------------------------------------------------------------------

async def fetch_official(orgnr: str) -> tuple[dict[str, SourcedFact], dict]:
    """Official facts for *orgnr* plus the raw Brreg context used by the synthesis.

    ``context`` = ``{"urls": {...}, "entity": body|None, "roles": body|None,
    "accounts": body|None, "ok": {"entity": bool, ...}}``; ``ok`` is False only
    when the request itself failed (a 404 is a successful "nothing there").
    Each endpoint fails independently; a failure just omits that fact.
    """
    today = date.today().isoformat()
    urls = {
        "entity": ENTITY_URL.format(org=orgnr),
        "roles": ROLES_URL.format(org=orgnr),
        "accounts": ACCOUNTS_URL.format(org=orgnr),
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_get_json(client, u) for u in urls.values()), return_exceptions=True
        )
    context: dict = {"urls": urls, "ok": {}, "retrieval_date": today}
    for key, res in zip(urls, results):
        if isinstance(res, Exception):
            log.warning("[%s] Brreg %s fetch failed: %s", orgnr, key, res)
            context[key], context["ok"][key] = None, False
        else:
            context[key], context["ok"][key] = res, True

    facts: dict[str, SourcedFact] = {}
    for field, value, key in (
        ("ceo_name", parse_ceo(context["roles"]), "roles"),
        ("employee_count", parse_employees(context["entity"]), "entity"),
        ("latest_revenue_nok", parse_revenue_nok(context["accounts"]), "accounts"),
    ):
        if value is not None:
            facts[field] = SourcedFact(fact_value=value, source_url=urls[key], retrieval_date=today)
    return facts, context


async def fetch_official_facts(orgnr: str) -> dict[str, SourcedFact]:
    """Return the official facts available for *orgnr* (missing ones omitted)."""
    facts, _ = await fetch_official(orgnr)
    return facts


def apply_official_facts(profile: CompanyProfile, facts: dict[str, SourcedFact]) -> CompanyProfile:
    """Official values replace web/LLM values; absent ones leave the field as is."""
    return profile.model_copy(update=facts) if facts else profile


async def enrich_with_official(orgnr: str, profile: CompanyProfile) -> tuple[CompanyProfile, dict | None]:
    """Never raises: on any failure the profile is returned unchanged with no context."""
    try:
        facts, context = await fetch_official(orgnr)
        return apply_official_facts(profile, facts), context
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Official facts skipped: %s", orgnr, e)
        return profile, None


async def enrich_with_official_facts(orgnr: str, profile: CompanyProfile) -> CompanyProfile:
    """Never raises: on any failure the profile is returned unchanged."""
    profile, _ = await enrich_with_official(orgnr, profile)
    return profile
