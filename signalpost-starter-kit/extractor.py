"""
Extractor — orchestrates web search, scraping, and LLM extraction to build
a CompanyProfile for a Norwegian company identified by its Brreg Enhet.

Pipeline:
  1. DuckDuckGo search  → find website + latest news URLs (via DDGS in threadpool)
  2. trafilatura        → fetch & strip each URL to plain text (concurrent)
  3. Gemini 1.5 Flash   → extract structured CompanyProfile via instructor
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from functools import partial

import httpx
import instructor
import trafilatura
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from openai import AsyncOpenAI, AuthenticationError, PermissionDeniedError

from brreg_client import Enhet
from domain_validator import DomainValidator
from schema import CompanyProfile, SourcedFact

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.5-flash-lite"
DDG_MAX_RESULTS = 6  # extra headroom: unrelated/parent domains are filtered out

# Set once the API rejects our key, so the rest of the batch skips the
# search/scrape work instead of repeating a call that cannot succeed.
_llm_disabled_reason: str | None = None

SYSTEM_PROMPT = """You are a precise Norwegian company intelligence analyst.

Your task is to extract factual information about a company ONLY from the
source text provided. Follow these rules without exception:

1. ONLY extract facts that are explicitly stated in the provided text.
2. If a fact is not clearly present in the text, set fact_value to null.
3. Never infer, guess, or hallucinate data.
4. For source_url, use the URL of the page where you found the fact.
   If you cannot determine which page contained a specific fact, set it to null.
5. For retrieval_date, always use today's date in YYYY-MM-DD format.
6. For employee_count and latest_revenue_nok, return integers only (no
   currency symbols, commas, or text). Return null if not found.
7. For summary_of_changes, write a brief factual description of what was
   found and what gaps remain.
8. The company may be a subsidiary of a larger group. Only attribute facts
   that are about the named company itself. Never attribute a parent or sister
   company's website, leadership, figures or accounts to it; if the text is
   about the group rather than this company, set the value to null.
"""


# ---------------------------------------------------------------------------
# LLM availability
# ---------------------------------------------------------------------------

_PLACEHOLDER_KEYS = {"your_key_here", "your_api_key", "your-api-key", "changeme", "xxx"}


def _get_api_key() -> str | None:
    """Return the configured key, or ``None`` if unset, blank or a placeholder."""
    key = os.environ.get("GEMINI_API_KEY", "").strip().strip("\"'")
    if not key or key.lower() in _PLACEHOLDER_KEYS:
        return None
    return key


def llm_unavailable_reason() -> str | None:
    """Return why LLM extraction cannot run right now, or ``None`` if it can.

    Purely local: no network access, safe to call before any search/scrape.
    """
    if _llm_disabled_reason:
        return _llm_disabled_reason
    if not _get_api_key():
        return "GEMINI_API_KEY is not set"
    return None


async def validate_llm_key() -> None:
    """Probe the API once so an invalid key is caught before any web work.

    Only a definitive rejection (400/401/403) disables the LLM for the run;
    timeouts, rate limits and 5xx are inconclusive and leave it enabled, since
    per-company extraction already falls back on failure.
    """
    global _llm_disabled_reason
    key = _get_api_key()
    if not key:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(
                GEMINI_BASE_URL + "models",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError:
        return
    if resp.status_code in (400, 401, 403):
        _llm_disabled_reason = "GEMINI_API_KEY was rejected by the API"


def _is_auth_error(exc: BaseException | None) -> bool:
    """True if *exc* (or anything it was raised from) is an API key rejection.

    Gemini's OpenAI-compatible endpoint reports a bad key as HTTP 400
    ("Please pass a valid API key"), which instructor wraps, so the message
    is checked as well as the 401/403 exception classes.
    """
    while exc is not None:
        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            return True
        text = str(exc).lower()
        if "api key" in text or "api_key_invalid" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ddg_search_sync(query: str, max_results: int) -> list[dict]:
    """Synchronous DuckDuckGo search — runs in a thread pool."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"Search backend failed: {e}")
        return []


async def _ddg_search(query: str, max_results: int = DDG_MAX_RESULTS) -> list[dict]:
    """Async wrapper: run DDGS.text in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_ddg_search_sync, query, max_results))


def _fetch_text_sync(url: str) -> tuple[str, str]:
    """Fetch a URL with trafilatura and return (url, plain_text)."""
    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        return url, ""
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    ) or ""
    return url, text


async def _fetch_text(url: str) -> tuple[str, str]:
    """Async wrapper: run trafilatura with a strict 5-second timeout."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, partial(_fetch_text_sync, url)),
            timeout=5.0
        )
    except TimeoutError:
        return url, ""


def _build_user_message(
    enhet: Enhet,
    search_results: list[dict],
    scraped: list[tuple[str, str]],
    parents: list[Enhet] | None = None,
) -> str:
    """Assemble the user message sent to the LLM."""
    lines: list[str] = [
        f"Company: {enhet.navn}",
        f"Organisation number: {enhet.organisasjonsnummer}",
        f"Today's date: {date.today().isoformat()}",
    ]
    if parents:
        lines.append("Parent organisation(s) - NOT this company: " + "; ".join(p.navn for p in parents))
    lines += [
        "",
        "=== SEARCH RESULTS (title | url | snippet) ===",
    ]
    for r in search_results:
        lines.append(f"- {r.get('title', '')} | {r.get('href', '')} | {r.get('body', '')}")

    lines.append("")
    lines.append("=== SCRAPED PAGE TEXT ===")
    for url, text in scraped:
        if text:
            lines.append(f"\n--- SOURCE: {url} ---")
            # Cap each individual page payload to stay within limits
            lines.append(text[:3_000])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

async def extract_company_profile(
    enhet: Enhet,
    validator: DomainValidator | None = None,
) -> CompanyProfile:
    """Build a CompanyProfile for a Norwegian company using web search + LLM.

    Args:
        enhet: A validated :class:`~brreg_client.Enhet` from the Brreg API.
        validator: Domain validator for *enhet*; built from Brreg if omitted.
            Only domains it accepts are scraped or shown to the LLM.

    Returns:
        A :class:`~schema.CompanyProfile` populated from open-source web data.
        Fields the LLM cannot verify from scraped text will have ``fact_value=None``.

    Raises:
        EnvironmentError: If the LLM is unavailable (no key, or key rejected
            earlier in this run). Checked before any search or scraping.
        openai.APIError: On LLM API failures.
    """
    reason = llm_unavailable_reason()
    if reason:
        raise EnvironmentError(f"{reason}. Add a valid key to your .env file.")
    api_key = _get_api_key()

    # ------------------------------------------------------------------
    # 1. Search DuckDuckGo for the company's website + recent news
    # ------------------------------------------------------------------
    search_query = f"{enhet.navn} official website Norway"
    search_results = await _ddg_search(search_query, max_results=DDG_MAX_RESULTS)
    urls = [r["href"] for r in search_results if r.get("href")]

    # Guardrail: If search is rate-limited, construct fallback URLs
    if not urls:
        print(f"\nSearch blocked. Attempting domain fallback for {enhet.navn}...")
        clean_name = enhet.navn.lower().replace(" ", "").replace("asa", "").replace("as", "")
        urls = [f"https://www.{clean_name}.com", f"https://www.{clean_name}.no/en"]

    # ------------------------------------------------------------------
    # 1b. Validate domains: only the entity's own sites may be scraped
    # ------------------------------------------------------------------
    if validator is None:
        validator = await DomainValidator.for_entity(enhet)

    registered = validator.registered_site_url
    candidates = list(dict.fromkeys(([registered] if registered else []) + urls))
    accepted: list[str] = []
    for url in candidates:
        verdict = validator.check(url)
        if verdict.accepted:
            accepted.append(url)
        else:
            log.info("[%s] Rejected %s: %s", enhet.organisasjonsnummer, url, verdict.reason)

    if not accepted:
        return CompanyProfile(
            summary_of_changes=(
                f"No web source could be verified as belonging to {enhet.navn} "
                "(candidate domains matched a parent group or another entity); "
                "web-derived fields left empty."
            )
        )
    urls = accepted
    search_results = [r for r in search_results if r.get("href") in set(accepted)]
    # ------------------------------------------------------------------
    # 2. Fetch and scrape all URLs concurrently
    # ------------------------------------------------------------------
    scraped: list[tuple[str, str]] = await asyncio.gather(
        *[_fetch_text(url) for url in urls]
    )

    # ------------------------------------------------------------------
    # 3. Build prompt and call Gemini via instructor
    # ------------------------------------------------------------------
    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url=GEMINI_BASE_URL,
        timeout=60.0,
        max_retries=3,  # Gives Google time to recover without crashing immediately
    )
    client = instructor.from_openai(openai_client)

    raw_user_message = _build_user_message(enhet, search_results, scraped, validator.ancestors)
    
    # Cap scraped context to 6,000 characters to prevent slow processing and 503 spikes
    truncated_message = raw_user_message[:6000] if raw_user_message else "No web text extracted."

    global _llm_disabled_reason
    try:
        profile: CompanyProfile = await client.chat.completions.create(
            model=GEMINI_MODEL,
            response_model=CompanyProfile,
            max_retries=3,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": truncated_message},
            ],
        )
    except Exception as e:
        if _is_auth_error(e):
            _llm_disabled_reason = "GEMINI_API_KEY was rejected by the API"
        raise

    # Drop anything the LLM attributed from a parent domain or an unverified site
    return validator.sanitize_profile(profile)


# ---------------------------------------------------------------------------
# Quick smoke-test  (run with:  python extractor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from brreg_client import lookup_enhet

    async def _demo() -> None:
        # Equinor ASA — org number 923609016
        enhet = await lookup_enhet("923609016")
        if enhet is None:
            print("Could not find organisation in Brreg.")
            return

        print(f"Extracting profile for: {enhet.navn} ({enhet.organisasjonsnummer})")
        print("Searching, scraping, and calling Gemini — this may take ~15 s ...\n")

        profile = await extract_company_profile(enhet)
        print(json.dumps(profile.model_dump(), indent=2, ensure_ascii=False))

    asyncio.run(_demo())