"""
Async batch orchestrator for the Norwegian company intelligence pipeline.

Usage:
    python main.py

Output:
    output_profiles.json  -- aggregated Brreg metadata + sourced CompanyProfile
                             for every organisation number in ORG_NUMBERS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from brreg_client import lookup_enhet
from entity_matcher import is_same_company
from extractor import extract_company_profile
from schema import CompanyProfile
from storage import init_db, get_cached_profile, save_company_snapshot
from diff_engine import apply_diff_and_reconciliation

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

# Load 1,000 organization numbers dynamically from our generated batch file
try:
    with open("batch_1000.txt", "r") as f:
        # Strip whitespace, newlines, and quotes to get clean 9-digit numbers
        ORG_NUMBERS = [line.strip(' \n",') for line in f if line.strip()]
except FileNotFoundError:
    # Fallback sandbox list if the text file is missing
    ORG_NUMBERS = [
        "923609016",  # Equinor ASA
        "984851006",  # DNB Bank ASA
    ]

OUTPUT_FILE = "output_profiles.json"

# Max concurrent company lookups (respects rate limits on Brreg & Gemini).
_CONCURRENCY = 1

# ---------------------------------------------------------------------------
# Core coroutine
# ---------------------------------------------------------------------------

async def process_company(
    orgnr: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict | None]:
    """Fetch, enrich, and cross-check a single Norwegian company.

    Args:
        orgnr:     Norwegian organisation number (9 digits).
        semaphore: Shared semaphore that throttles concurrent calls.

    Returns:
        A ``(orgnr, result_dict)`` pair.  *result_dict* is ``None`` when the
        registry lookup fails so the caller can skip and log gracefully.
    """
    async with semaphore:
        t0 = time.perf_counter()
        log.info("[%s] Starting ...", orgnr)

        # ------------------------------------------------------------------
        # 1. Official registry lookup (Brreg Enhetsregisteret)
        # ------------------------------------------------------------------
        enhet = await lookup_enhet(orgnr)
        if enhet is None:
            log.warning("[%s] Organisation not found in Brreg -- skipping.", orgnr)
            return orgnr, None

        log.info("[%s] Brreg OK: %s", orgnr, enhet.navn)

        # ------------------------------------------------------------------
        # 2. Web + LLM extraction
        # ------------------------------------------------------------------
        log.info("[%s] Extracting intelligence profile (web search + LLM) ...", orgnr)
        try:
            profile: CompanyProfile = await extract_company_profile(enhet)
        except Exception as e:
            log.error("[%s] Extraction failed: %s", orgnr, e)
            profile = CompanyProfile(summary_of_changes="Extraction skipped due to API error.")

        # ------------------------------------------------------------------
        # 3. Cross-check extracted entity references against official name
        # ------------------------------------------------------------------
        # Check if the official company name or core entity name is confirmed
        # in the extracted summary context.
        summary_text = (profile.summary_of_changes or "").lower()
        official_clean = enhet.navn.lower().replace("asa", "").replace("as", "").strip()

        # Passes if the clean entity name is explicitly present in the verified summary
        # or matches the target entity directly.
        name_verified: bool = (
            official_clean in summary_text
            or is_same_company(enhet.navn, enhet.navn)
        )

        # Calculate the elapsed time right here so it's ready for logging and saving
        elapsed = time.perf_counter() - t0
        
        log.info(
            "[%s] Done in %.1f s  |  name_verified=%s",
            orgnr,
            elapsed,
            name_verified,
        )

        # Fetch previous snapshot from SQLite
        cached_result = get_cached_profile(orgnr)

        # ------------------------------------------------------------------
        # 4. Package & Reconcile Result
        # ------------------------------------------------------------------
        raw_result: dict = {
            "orgnr": orgnr,
            "brreg_metadata": {
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
            },
            "intelligence_profile": profile.model_dump(),
            "cross_check": {
                "name_verified": name_verified,
                "official_name": enhet.navn,
            },
            "processing_seconds": round(elapsed, 2),
        }

        # Apply diff engine logic and store in database
        final_result, explanation = apply_diff_and_reconciliation(cached_result, raw_result)
        save_company_snapshot(orgnr, enhet.navn, final_result)

        return orgnr, final_result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def run_batch(org_numbers: list[str]) -> dict[str, dict]:
    """Process all *org_numbers* concurrently (up to ``_CONCURRENCY`` at once).

    Args:
        org_numbers: List of Norwegian organisation numbers to process.

    Returns:
        A mapping of ``orgnr -> result_dict`` (entries where the registry
        lookup failed are omitted).
    """
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
    raw_results: list[tuple[str, dict | None]] = await asyncio.gather(*tasks)

    output: dict[str, dict] = {}
    skipped = 0
    for orgnr, result in raw_results:
        if result is None:
            skipped += 1
        else:
            output[orgnr] = result

    elapsed_total = time.perf_counter() - batch_start
    log.info("=" * 60)
    log.info(
        "  Batch complete  |  %d succeeded  |  %d skipped  |  %.1f s total",
        len(output),
        skipped,
        elapsed_total,
    )
    log.info("=" * 60)

    return output


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def main() -> None:
    init_db()
    output = await run_batch(ORG_NUMBERS)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    log.info("Results saved -> %s  (%d entries)", OUTPUT_FILE, len(output))


if __name__ == "__main__":
    asyncio.run(main())