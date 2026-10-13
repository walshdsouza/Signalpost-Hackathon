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
import os
from datetime import date
from functools import partial

import instructor
import trafilatura
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from openai import AsyncOpenAI

from brreg_client import Enhet
from schema import CompanyProfile, SourcedFact

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.5-flash-lite"
DDG_MAX_RESULTS = 3

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
"""


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


def _build_user_message(enhet: Enhet, search_results: list[dict], scraped: list[tuple[str, str]]) -> str:
    """Assemble the user message sent to the LLM."""
    lines: list[str] = [
        f"Company: {enhet.navn}",
        f"Organisation number: {enhet.organisasjonsnummer}",
        f"Today's date: {date.today().isoformat()}",
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

async def extract_company_profile(enhet: Enhet) -> CompanyProfile:
    """Build a CompanyProfile for a Norwegian company using web search + LLM.

    Args:
        enhet: A validated :class:`~brreg_client.Enhet` from the Brreg API.

    Returns:
        A :class:`~schema.CompanyProfile` populated from open-source web data.
        Fields the LLM cannot verify from scraped text will have ``fact_value=None``.

    Raises:
        EnvironmentError: If ``GEMINI_API_KEY`` is not set in the environment / .env.
        openai.APIError: On LLM API failures.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY is not set. Add it to your .env file.")

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

    raw_user_message = _build_user_message(enhet, search_results, scraped)
    
    # Cap scraped context to 6,000 characters to prevent slow processing and 503 spikes
    truncated_message = raw_user_message[:6000] if raw_user_message else "No web text extracted."

    profile: CompanyProfile = await client.chat.completions.create(
        model=GEMINI_MODEL,
        response_model=CompanyProfile,
        max_retries=3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": truncated_message},
        ],
    )

    return profile


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