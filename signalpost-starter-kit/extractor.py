"""
Extractor — orchestrates web search, scraping, and LLM extraction to build
a CompanyProfile for a Norwegian company identified by its Brreg Enhet.

Pipeline:
  1. DuckDuckGo search  → find website + latest news URLs (via DDGS in threadpool)
  2. domain validation  → keep only domains that belong to this entity (not its parent)
  3. trafilatura        → fetch each URL: plain text, outgoing links, page date metadata
  4. Gemini             → extract structured facts + news/social candidates via instructor
  5. validation         → every news/social candidate is checked against strict
                          schema rules and grounded in the scraped pages
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from urllib.parse import urlparse

import httpx
import instructor
import trafilatura
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from lxml import html as lxml_html
from openai import AsyncOpenAI, AuthenticationError, PermissionDeniedError
from pydantic import ValidationError

from brreg_client import Enhet
from domain_validator import DomainValidator, registrable_domain
from schema import (
    PLATFORM_HOSTS,
    SOCIAL_PLATFORMS,
    CompanyExtraction,
    CompanyProfile,
    NewsCandidate,
    NewsItem,
    Socials,
    SourcedFact,
    normalise_platform,
    normalise_social_url,
)

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.5-flash-lite"
DDG_MAX_RESULTS = 6  # extra headroom: unrelated/parent domains are filtered out

MAX_NEWS_ITEMS = 10
MAX_PROMPT_CHARS = 12_000  # text + links for all pages; keeps latency / 503s in check
PAGE_TEXT_CHARS = 3_000
PAGE_LINKS_SHOWN = 40

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
   company's website, leadership, figures, news or social accounts to it; if
   the text is about the group rather than this company, set the value to null.

NEWS (news_candidates) - include an item ONLY if ALL of these hold:
9.  It is an actual press release, news article or blog post published by or
    about THIS company, with its own article page. The url must be copied
    exactly from a LINKS list or be the SOURCE page itself.
10. title is the article's headline copied verbatim from the page.
11. It has a definitive PUBLICATION date. Set published_date (YYYY-MM-DD) and
    date_type = "published" only when the page labels the date as the
    publication date or shows it as the article's dateline. Copy the text that
    shows the date and its label into date_evidence (e.g. "Published 12 March
    2026").
12. NEVER use a modified, updated, last-updated, revised, oppdatert or endret
    date. If a PAGE METADATA line shows modified=..., that date is NOT a
    publication date. If the only date available is a modified/updated one, or
    there is no date at all, OMIT the item entirely - do not guess.
13. NEVER output UI elements or navigation as news: "See more news", "Read
    more", "Load more", "All news", "Newsroom", "Press releases", category,
    archive or listing pages, product pages, service pages, career pages.
14. Return at most 5 of the most recent items. An empty list is the correct
    answer when no item meets every rule above.

SOCIALS (social_candidates):
15. Include only the company's OWN official profile pages (linkedin, facebook,
    instagram, x, youtube, tiktok), copied exactly from a LINKS list. Never
    include share/follow buttons, personal profiles, or the parent/group's
    accounts. An empty list is correct when unsure.
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
# Search + page fetching
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


@dataclass
class Page:
    """One fetched page: readable text plus what plain-text extraction drops."""

    url: str
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute url)
    published: str | None = None  # from page metadata (YYYY-MM-DD)
    modified: str | None = None   # from page metadata (YYYY-MM-DD) - never a publish date


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_JSONLD_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_JSONLD_MODIFIED_RE = re.compile(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_PUBLISHED_KEYS = {
    "article:published_time", "og:article:published_time", "datepublished", "pubdate",
    "publish-date", "publication_date", "dc.date.issued", "dcterms.created",
}
_MODIFIED_KEYS = {
    "article:modified_time", "og:updated_time", "datemodified", "last-modified",
    "dcterms.modified", "dc.date.modified",
}


def _extract_page_dates(html: str) -> tuple[str | None, str | None]:
    """Read published/modified dates from <meta> tags and JSON-LD, kept apart."""
    published = modified = None
    for tag in _META_TAG_RE.findall(html):
        attrs = {m[0].lower(): (m[1] or m[2]) for m in _ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
               or attrs.get("http-equiv") or "").lower()
        m = _ISO_DATE_RE.search(attrs.get("content", ""))
        if not m:
            continue
        if key in _PUBLISHED_KEYS and not published:
            published = m.group(1)
        elif key in _MODIFIED_KEYS and not modified:
            modified = m.group(1)
    if not published and (m := _JSONLD_PUBLISHED_RE.search(html)):
        published = m.group(1)
    if not modified and (m := _JSONLD_MODIFIED_RE.search(html)):
        modified = m.group(1)
    return published, modified


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return unique (anchor text, absolute http(s) URL) pairs from the page."""
    try:
        try:
            doc = lxml_html.fromstring(html)
        except ValueError:  # str with an encoding declaration
            doc = lxml_html.fromstring(html.encode("utf-8"))
        doc.make_links_absolute(base_url, resolve_base_href=True)
    except Exception:
        return []
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip().split("#")[0]
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        links.append((" ".join(a.text_content().split())[:160], href))
    return links


def _fetch_page_sync(url: str) -> Page:
    """Fetch a URL with trafilatura; keep text, links and date metadata."""
    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        return Page(url)
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    ) or ""
    published, modified = _extract_page_dates(downloaded)
    return Page(url, text, _extract_links(downloaded, url), published, modified)


async def _fetch_page(url: str) -> Page:
    """Async wrapper: run trafilatura with a strict 5-second timeout."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, partial(_fetch_page_sync, url)),
            timeout=5.0,
        )
    except TimeoutError:
        return Page(url)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_NEWSY_PATH_RE = re.compile(r"/(news|nyhet|press|artikl|article|blog|stor(?:y|ies)|20\d\d)", re.IGNORECASE)
_SOCIAL_HOSTS = {h for hosts in PLATFORM_HOSTS.values() for h in hosts}


def _select_links(page: Page, limit: int = PAGE_LINKS_SHOWN) -> list[tuple[str, str]]:
    """Social profile links first, then same-site links that look like articles."""
    own = registrable_domain(page.url)
    socials, articles = [], []
    for text, href in page.links:
        dom = registrable_domain(href)
        if dom in _SOCIAL_HOSTS:
            socials.append((text, href))
        elif dom == own and text and _NEWSY_PATH_RE.search(urlparse(href).path):
            articles.append((text, href))
    return (socials[:12] + articles)[:limit]


def _build_user_message(
    enhet: Enhet,
    search_results: list[dict],
    scraped: list[Page],
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
    lines.append("=== SCRAPED PAGES ===")
    for page in scraped:
        if not (page.text or page.links):
            continue
        lines.append(f"\n--- SOURCE: {page.url} ---")
        if page.published or page.modified:
            lines.append(
                f"PAGE METADATA: published={page.published or 'unknown'}, "
                f"modified={page.modified or 'unknown'} (modified is NOT a publication date)"
            )
        # Cap each individual page payload to stay within limits
        lines.append(page.text[:PAGE_TEXT_CHARS])
        links = _select_links(page)
        if links:
            lines.append("LINKS (anchor text -> url):")
            lines.extend(f"- {text or '(no text)'} -> {href}" for text, href in links)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validating the LLM's news / social candidates
# ---------------------------------------------------------------------------

def _canon(url: str) -> str:
    """Comparison key for URLs: no fragment, no trailing slash, lowercase host."""
    p = urlparse(url.strip())
    return f"{p.scheme}://{(p.hostname or '').lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def _page_haystack(page: Page) -> str:
    """Folded page text plus link anchor texts, for grounding checks."""
    return _fold(page.text + " " + " ".join(t for t, _ in page.links))


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    return f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"


def _validate_news(
    cand: NewsCandidate,
    pages: list[Page],
    known_urls: set[str],
    validator: DomainValidator,
    today: str,
) -> tuple[NewsItem | None, str]:
    """Turn one LLM candidate into a NewsItem, or explain why it was rejected."""
    try:
        item = NewsItem(
            title=cand.title, url=cand.url, published_date=cand.published_date,
            date_type=cand.date_type, date_evidence=cand.date_evidence,
            summary=cand.summary, retrieval_date=today,
        )
    except ValidationError as e:
        return None, _first_error(e)

    if _canon(item.url) not in known_urls:
        return None, "url does not appear on any scraped page"
    verdict = validator.check(item.url)
    if not verdict.accepted:
        return None, f"url domain rejected: {verdict.reason}"

    # Grounding: headline and date wording must really be on a scraped page
    # (news cards often exist only as link anchors, so those count too).
    title, evidence = _fold(item.title), _fold(item.date_evidence)
    if not any(title in _page_haystack(p) for p in pages):
        return None, "title not found in scraped pages"
    source_page = next((p for p in pages if evidence in _page_haystack(p)), None)
    if source_page is None:
        return None, "date_evidence not found verbatim in scraped pages"

    # Cross-check against the article page's own metadata, if we fetched it.
    meta_page = next((p for p in pages if _canon(p.url) == _canon(item.url)), None)
    if meta_page:
        if meta_page.published and meta_page.published != item.published_date:
            if meta_page.modified == item.published_date:
                return None, "date equals the page's modified date, not its published date"
            try:  # page metadata is authoritative for the publish date
                item = NewsItem.model_validate({**item.model_dump(), "published_date": meta_page.published})
            except ValidationError as e:
                return None, _first_error(e)
        elif not meta_page.published and meta_page.modified == item.published_date:
            return None, "only a modified date is known for this page"

    return item.model_copy(update={"source_url": source_page.url}), ""


def _build_profile(
    extraction: CompanyExtraction,
    enhet: Enhet,
    pages: list[Page],
    validator: DomainValidator,
) -> CompanyProfile:
    """Convert the lenient LLM output into a strictly validated CompanyProfile."""
    today = date.today().isoformat()
    oid = enhet.organisasjonsnummer

    known_urls = {_canon(p.url) for p in pages} | {_canon(u) for p in pages for _, u in p.links}

    # -- news --------------------------------------------------------------
    news: dict[str, NewsItem] = {}
    for cand in extraction.news_candidates:
        item, why = _validate_news(cand, pages, known_urls, validator, today)
        if item is None:
            log.info("[%s] Rejected news %r: %s", oid, (cand.title or cand.url)[:70], why)
        else:
            news.setdefault(_canon(item.url), item)
    news_items = sorted(news.values(), key=lambda n: n.published_date, reverse=True)[:MAX_NEWS_ITEMS]

    # -- socials: canonical profile URLs that were really linked from a page ---
    linked: dict[str, str] = {}  # canonical profile url -> page it was linked from
    for p in pages:
        for _, href in p.links:
            for platform, hosts in PLATFORM_HOSTS.items():
                dom = registrable_domain(href)
                if dom in hosts:
                    try:
                        linked.setdefault(normalise_social_url(platform, href), p.url)
                    except ValueError:
                        pass

    socials: dict[str, SourcedFact] = {}
    for cand in extraction.social_candidates:
        platform = normalise_platform(cand.platform)
        if platform not in SOCIAL_PLATFORMS or platform in socials:
            continue
        try:
            url = normalise_social_url(platform, cand.url)
        except ValueError as e:
            log.info("[%s] Rejected %s link: %s", oid, platform, e)
            continue
        if url not in linked:
            log.info("[%s] Rejected %s link %s: not linked from any scraped page", oid, platform, url)
            continue
        verdict = validator.check_social(url)
        if not verdict.accepted:
            log.info("[%s] Rejected %s link %s: %s", oid, platform, url, verdict.reason)
            continue
        socials[platform] = SourcedFact(fact_value=url, source_url=linked[url], retrieval_date=today)

    return CompanyProfile(
        website=extraction.website,
        ceo_name=extraction.ceo_name,
        employee_count=extraction.employee_count,
        latest_revenue_nok=extraction.latest_revenue_nok,
        summary_of_changes=extraction.summary_of_changes,
        news=news_items,
        socials=Socials(**socials),
    )


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
        Fields the LLM cannot verify from scraped text will have ``fact_value=None``;
        ``news`` and ``socials`` contain only items that passed strict validation.

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
        return validator.sanitize_profile(CompanyProfile(
            summary_of_changes=(
                f"No web source could be verified as belonging to {enhet.navn} "
                "(candidate domains matched a parent group or another entity); "
                "web-derived fields left empty."
            )
        ))
    urls = accepted
    accepted_set = set(accepted)
    search_results = [r for r in search_results if r.get("href") in accepted_set]

    # ------------------------------------------------------------------
    # 2. Fetch and scrape all URLs concurrently
    # ------------------------------------------------------------------
    pages: list[Page] = await asyncio.gather(*[_fetch_page(url) for url in urls])

    if not any(p.text or p.links for p in pages):
        return validator.sanitize_profile(CompanyProfile(
            summary_of_changes=f"No readable content could be retrieved from the verified sites of {enhet.navn}."
        ))

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

    raw_user_message = _build_user_message(enhet, search_results, pages, validator.ancestors)
    truncated_message = raw_user_message[:MAX_PROMPT_CHARS]

    global _llm_disabled_reason
    try:
        extraction: CompanyExtraction = await client.chat.completions.create(
            model=GEMINI_MODEL,
            response_model=CompanyExtraction,
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

    # ------------------------------------------------------------------
    # 4. Strict validation of news/socials, then drop anything attributed
    #    from a parent domain or an unverified site
    # ------------------------------------------------------------------
    profile = _build_profile(extraction, enhet, pages, validator)
    return validator.sanitize_profile(profile)


# ---------------------------------------------------------------------------
# Quick smoke-test  (run with:  python extractor.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
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
