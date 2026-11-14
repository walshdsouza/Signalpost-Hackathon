"""
Async batch orchestrator for the Norwegian company intelligence pipeline.

Usage:
    python main.py input_orgs.txt [--output output_profiles.json]

Output:
    JSON file (default output_profiles.json) -- aggregated Brreg metadata +
    sourced CompanyProfile for every organisation number in the input file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from brreg_client import Enhet, lookup_enhet
from entity_matcher import is_same_company
from extractor import (
    extract_company_profile_with_status,
    get_company_usage,
    GEMINI_MODEL,
    get_total_usage,
    llm_unavailable_reason,
    reset_usage,
    validate_llm_key,
)
from schema import CompanyProfile
from storage import init_db, get_cached_profile, save_company_snapshot
from diff_engine import apply_diff_and_reconciliation
from domain_validator import DomainValidator
from official_facts import enrich_with_official
from synthesis import attach_synthesis

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_FILE = "output_profiles.json"
RUN_REPORT_KEY = "_run_report"


_ORGNR_KEYS = ("orgnr", "organisasjonsnummer", "organisation_number", "organization_number",
               "org_number", "orgNr", "orgNumber", "org", "id")
_ORGNR_RE = re.compile(r"(?<!\d)(\d{3})[\s.]?(\d{3})[\s.]?(\d{3})(?!\d)")


def _orgnrs_from_json(value) -> list[str]:
    if isinstance(value, dict):
        for key in _ORGNR_KEYS:
            if key in value:
                return _orgnrs_from_json(value[key])
        return []
    if isinstance(value, list):
        return [o for item in value for o in _orgnrs_from_json(item)]
    if isinstance(value, (str, int)):
        return ["".join(m.groups()) for m in _ORGNR_RE.finditer(str(value))]
    return []


def load_target_organisations(path: str | Path) -> list[str]:
    """Read organisation numbers from *path*.

    Accepts plain text (one per line), JSONL (``{"orgnr": "923609016"}`` and
    similar keys), a JSON array, CSV-ish lines and spaced numbers
    (``923 609 016``). Every result is a 9-digit org number; blank lines and
    anything without one are skipped. Duplicates are dropped, order preserved.
    """
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    orgs: list[str] = []
    stripped = text.strip()
    if stripped.startswith("["):  # whole file is one JSON array
        try:
            orgs = _orgnrs_from_json(json.loads(stripped))
        except json.JSONDecodeError:
            orgs = []
    if not orgs:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line[0] in "[{":
                try:
                    orgs.extend(_orgnrs_from_json(json.loads(line.rstrip(","))))
                    continue
                except json.JSONDecodeError:
                    pass
            match = _ORGNR_RE.search(line)
            if match:
                orgs.append("".join(match.groups()))
    return list(dict.fromkeys(orgs))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich Norwegian companies from a batch of organisation numbers. "
                    "Writes the JSON results and an HTML report next to them (same name, .html).",
    )
    parser.add_argument(
        "input_file",
        help="Batch file: one org number per line, JSONL ({\"orgnr\": ...}), a JSON array or CSV.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_FILE,
        help="Path of the JSON results file (default: %(default)s).",
    )
    return parser.parse_args(argv)

# Max concurrent company lookups (asyncio.Semaphore; respects Brreg/Gemini/search rate limits).
_CONCURRENCY = 5

# ---------------------------------------------------------------------------
# Core coroutine
# ---------------------------------------------------------------------------

def _brreg_metadata(enhet: Enhet | None, orgnr: str) -> dict:
    """Deterministic registry block; a null-filled stub if Brreg gave us nothing."""
    if enhet is None:
        return {
            "navn": None,
            "organisasjonsnummer": orgnr,
            "organisasjonsform": None,
            "naeringskode1": None,
            "forretningsadresse": None,
            "hjemmeside": None,
            "overordnetEnhet": None,
        }
    return {
        "navn": enhet.navn,
        "organisasjonsnummer": enhet.organisasjonsnummer,
        "organisasjonsform": {
            "kode": enhet.organisasjonsform.kode,
            "beskrivelse": enhet.organisasjonsform.beskrivelse,
        },
        "naeringskode1": (
            {
                "kode": enhet.naeringskode1.kode,
                "beskrivelse": enhet.naeringskode1.beskrivelse,
            }
            if enhet.naeringskode1
            else None
        ),
        "forretningsadresse": (
            {
                "adresse": enhet.forretningsadresse.adresse,
                "postnummer": enhet.forretningsadresse.postnummer,
                "poststed": enhet.forretningsadresse.poststed,
                "land": enhet.forretningsadresse.land,
                "kommune": enhet.forretningsadresse.kommune,
            }
            if enhet.forretningsadresse
            else None
        ),
        "hjemmeside": enhet.hjemmeside,
        "overordnetEnhet": enhet.overordnetEnhet,
    }


def _build_result(
    orgnr: str,
    enhet: Enhet | None,
    profile: CompanyProfile,
    fallback_reason: str | None,
    elapsed: float,
) -> dict:
    """Package one company's output record (same shape for success and fallback)."""
    official_name = enhet.navn if enhet else None

    # Cross-check extracted entity references against official name
    name_verified = False
    if enhet is not None:
        summary_text = (profile.summary_of_changes or "").lower()
        official_clean = enhet.navn.lower().replace("asa", "").replace("as", "").strip()
        name_verified = (
            official_clean in summary_text
            or is_same_company(enhet.navn, enhet.navn)
        )

    return {
        "orgnr": orgnr,
        "status": "fallback" if fallback_reason else "complete",
        "fallback_reason": fallback_reason,
        "brreg_metadata": _brreg_metadata(enhet, orgnr),
        "intelligence_profile": profile.model_dump(),
        "cross_check": {
            "name_verified": name_verified,
            "official_name": official_name,
        },
        "processing_seconds": round(elapsed, 2),
        "operations": get_company_usage(orgnr),
    }


async def _enrich(
    orgnr: str,
) -> tuple[Enhet | None, CompanyProfile, str | None, DomainValidator | None]:
    """Run registry lookup + LLM extraction, degrading to a base profile.

    Returns ``(enhet, profile, fallback_reason, validator)``; *fallback_reason*
    is ``None`` only when the full pipeline succeeded. Never raises for expected
    failures. *validator* is ``None`` when the registry lookup failed.
    """
    # 1. Official registry lookup (Brreg Enhetsregisteret)
    try:
        enhet = await lookup_enhet(orgnr)
    except Exception as e:
        reason = f"Brreg lookup failed ({type(e).__name__}: {e})."
        log.warning("[%s] %s Returning base profile.", orgnr, reason)
        return None, CompanyProfile(summary_of_changes=reason), reason, None

    if enhet is None:
        reason = "Organisation not found in Brreg (404)."
        log.warning("[%s] %s Returning base profile.", orgnr, reason)
        return None, CompanyProfile(summary_of_changes=reason), reason, None

    log.info("[%s] Brreg OK: %s", orgnr, enhet.navn)

    # Trusted registry hierarchy: which domains belong to parent groups?
    validator = await DomainValidator.for_entity(enhet)

    # 2. Web extraction (only validated domains are scraped). Website grounding and
    #    the news/jobs crawl run even when the LLM is unavailable or fails; the
    #    record is then marked fallback with that reason.
    log.info("[%s] Extracting intelligence profile (web search + site crawl + LLM) ...", orgnr)
    try:
        profile, llm_issue = await extract_company_profile_with_status(enhet, validator)
        return enhet, profile, (f"{llm_issue}." if llm_issue else None), validator
    except Exception as e:
        reason = f"Web extraction failed ({type(e).__name__}: {e})."
        log.error("[%s] %s Returning Brreg baseline.", orgnr, reason)
        return enhet, validator.sanitize_profile(CompanyProfile(summary_of_changes=reason)), reason, validator


async def process_company(
    orgnr: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict]:
    """Fetch, enrich, and cross-check a single Norwegian company.

    Always returns a result record so every requested organisation appears in
    the output, even if the registry or the LLM is unavailable.

    Args:
        orgnr:     Norwegian organisation number (9 digits).
        semaphore: Shared semaphore that throttles concurrent calls.

    Returns:
        An ``(orgnr, result_dict)`` pair. See ``status`` / ``fallback_reason``
        in the record to tell a full result from a base profile.
    """
    async with semaphore:
        t0 = time.perf_counter()
        log.info("[%s] Starting ...", orgnr)

        try:
            enhet, profile, fallback_reason, validator = await _enrich(orgnr)
        except Exception as e:  # last line of defence for the gather loop
            fallback_reason = f"Unexpected error ({type(e).__name__}: {e})."
            log.exception("[%s] %s", orgnr, fallback_reason)
            enhet, profile, validator = None, CompanyProfile(summary_of_changes=fallback_reason), None

        # Official registry facts (CEO, employees, revenue) for the exact entity.
        # Applied on every path, including LLM fallbacks.
        official_context = None
        if enhet is not None:
            profile, official_context = await enrich_with_official(enhet.organisasjonsnummer, profile)

        elapsed = time.perf_counter() - t0
        raw_result = _build_result(orgnr, enhet, profile, fallback_reason, elapsed)

        # Reconcile with the previous snapshot (also for fallback runs, so
        # cached facts are preserved and state keeps being tracked).
        try:
            cached_result = get_cached_profile(orgnr)
            if validator is not None:
                # Don't let the diff engine "preserve" a parent's site cached by an earlier run
                cached_result = validator.purge_cached(cached_result)
            final_result, _explanation = apply_diff_and_reconciliation(cached_result, raw_result)
            # Sourced synthesis of the final (reconciled) record: does / changed / unknown.
            final_result = attach_synthesis(final_result, official_context)
            company_name = (
                (enhet.navn if enhet else None)
                or final_result.get("brreg_metadata", {}).get("navn")
                or orgnr
            )
            save_company_snapshot(orgnr, company_name, final_result)
        except Exception as e:
            log.error("[%s] State tracking failed (%s: %s); returning unreconciled result.",
                      orgnr, type(e).__name__, e)
            final_result = attach_synthesis(raw_result, official_context)

        log.info(
            "[%s] Done in %.1f s  |  status=%s",
            orgnr,
            elapsed,
            final_result["status"],
        )
        return orgnr, final_result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_batch(org_numbers: list[str]) -> dict[str, dict]:
    """Process all *org_numbers* concurrently (up to ``_CONCURRENCY`` at once).

    Args:
        org_numbers: List of Norwegian organisation numbers to process.

    Returns:
        A mapping of ``orgnr -> result_dict`` with one entry per organisation
        number; failed lookups yield a base profile rather than being omitted.
    """
    reset_usage()
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    batch_start = time.perf_counter()

    log.info("=" * 60)
    log.info(
        "  Signalpost batch  |  %d companies  |  concurrency=%d",
        len(org_numbers),
        _CONCURRENCY,
    )
    log.info("  Started at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    tasks = [process_company(orgnr, semaphore) for orgnr in org_numbers]
    raw_results: list[tuple[str, dict]] = await asyncio.gather(*tasks)

    output: dict[str, dict] = dict(raw_results)
    usage = get_total_usage()
    fallbacks = sum(1 for r in output.values() if r["status"] == "fallback")

    elapsed_total = time.perf_counter() - batch_start
    log.info("=" * 60)
    log.info(
        "  Batch complete  |  %d companies  |  %d fallback  |  %.1f s total",
        len(output),
        fallbacks,
        elapsed_total,
    )
    log.info(
        "  LLM usage       |  %d calls  |  %d prompt + %d completion tokens  |  total_cost_usd=$%.6f",
        usage["llm_calls"], usage["prompt_tokens"], usage["completion_tokens"], usage["cost_usd"],
    )
    log.info("=" * 60)

    # Run report: under a reserved key so per-company records stay keyed by orgnr.
    output[RUN_REPORT_KEY] = {
        "companies": len(raw_results),
        "fallbacks": fallbacks,
        "runtime_seconds": round(elapsed_total, 2),
        "llm_model": GEMINI_MODEL,
        "llm_calls": usage["llm_calls"],
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_cost_usd": usage["cost_usd"],
    }
    return output


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        org_numbers = load_target_organisations(args.input_file)
    except OSError as e:
        log.error("Cannot read input file %s: %s", args.input_file, e)
        return 1
    if not org_numbers:
        log.error("Input file %s contains no organisation numbers.", args.input_file)
        return 1

    init_db()

    # Validate the key once up front so a bad key never costs a search/scrape.
    await validate_llm_key()
    unavailable = llm_unavailable_reason()
    if unavailable:
        log.warning("LLM disabled for this run (%s); all companies get Brreg baseline.", unavailable)

    output = await run_batch(org_numbers)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    log.info("Results saved -> %s  (%d companies)", args.output, len(output) - 1)

    # Human-readable HTML report next to the JSON; never fails the run.
    try:
        from report import write_report
        log.info("Report saved  -> %s", write_report(args.output))
    except Exception as e:  # noqa: BLE001
        log.warning("HTML report skipped (%s: %s)", type(e).__name__, e)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
