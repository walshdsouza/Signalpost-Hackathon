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
import random
import re
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from urllib.parse import urljoin, urlparse

import httpx
import instructor
import trafilatura
from trafilatura import downloads as trafilatura_downloads
from dotenv import load_dotenv
from ddgs import DDGS
from lxml import html as lxml_html
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
)
from pydantic import ValidationError

from brreg_client import Enhet
from domain_validator import CONTENT_MISMATCH_REASON, DomainValidator, registrable_domain
from schema import (
    ATS_DOMAINS,
    PLATFORM_HOSTS,
    SOCIAL_PLATFORMS,
    CompanyExtraction,
    CAREERS_HINT_RE,
    CompanyNewsItem,
    CompanyProfile,
    JobPosting,
    NewsCandidate,
    NewsItem,
    article_url_problem,
    is_ui_label,
    Socials,
    SourcedFact,
    normalise_platform,
    normalise_social_url,
    validate_careers_url,
)

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3.5-flash-lite"
DDG_MAX_RESULTS = 6  # extra headroom: unrelated/parent domains are filtered out
# ddgs metasearch backends, tried in order. "auto" kept hitting rate-limited
# engines (duckduckgo/wikipedia) and returned nothing; bing was the most reliable.
SEARCH_BACKENDS = os.getenv("SEARCH_BACKENDS", "bing, brave, duckduckgo, mojeek, yahoo")
SEARCH_TIMEOUT = 8          # seconds per backend request
SEARCH_ATTEMPTS = 2
SEARCH_CONCURRENCY = 3      # simultaneous searches across the batch (rate-limit friendly)
_search_semaphores: dict[int, asyncio.Semaphore] = {}  # one per event loop

MAX_NEWS_ITEMS = 10
MAX_PROMPT_CHARS = 12_000  # text + links for all pages; keeps latency / 503s in check
PAGE_TEXT_CHARS = 3_000
PAGE_LINKS_SHOWN = 40

# USD per 1M tokens. Override via env if Google's price list changes - the
# defaults are Gemini Flash-Lite-tier list prices and must be checked against
# https://ai.google.dev/gemini-api/docs/pricing for GEMINI_MODEL.
def _price(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


GEMINI_INPUT_USD_PER_M = _price("GEMINI_INPUT_USD_PER_M", 0.10)
GEMINI_OUTPUT_USD_PER_M = _price("GEMINI_OUTPUT_USD_PER_M", 0.40)

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

CAREERS (careers_url):
16. Set careers_url to the URL of THIS company's own careers / open-positions
    page, copied exactly from a LINKS list or the SOURCE page itself. Never use
    the parent/group's careers or jobs page, a generic job-board search, or a
    page for another entity - subsidiaries often link to the group's jobs page.
    Use null when unsure.
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
# Cost tracking (token usage reported by the Gemini API -> USD)
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    """Accumulated token usage and cost for one scope (a company or the run)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.prompt_tokens * GEMINI_INPUT_USD_PER_M
            + self.completion_tokens * GEMINI_OUTPUT_USD_PER_M
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 8),
        }


# Single event loop, no awaits between updates -> plain dicts are safe.
_usage_by_org: dict[str, Usage] = {}
_usage_total = Usage()


def reset_usage() -> None:
    _usage_by_org.clear()
    global _usage_total
    _usage_total = Usage()


def record_usage(orgnr: str, completion) -> None:
    """Add the ``usage`` block of a raw API response to the company and run totals."""
    usage = getattr(completion, "usage", None)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    out = int(getattr(usage, "completion_tokens", 0) or 0)
    for u in (_usage_by_org.setdefault(orgnr, Usage()), _usage_total):
        u.calls += 1
        u.prompt_tokens += prompt
        u.completion_tokens += out


def get_company_usage(orgnr: str) -> dict:
    return _usage_by_org.get(orgnr, Usage()).as_dict()


def get_total_usage() -> dict:
    return _usage_total.as_dict()


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

LLM_ATTEMPTS = 5
SCRAPE_ATTEMPTS = 3
BACKOFF_BASE = 1.0   # seconds; delay = base * 2**attempt (+ jitter), capped
BACKOFF_CAP = 30.0


def _backoff_delay(attempt: int) -> float:
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt)) * random.uniform(0.75, 1.25)


class TransientFetchError(Exception):
    """A scrape/search returned nothing; worth retrying (rate limit, blip)."""


def _is_retryable_llm_error(exc: BaseException | None) -> bool:
    """True for 429 / 5xx / timeouts / connection errors anywhere in the cause chain."""
    if _is_auth_error(exc):
        return False
    while exc is not None:
        if isinstance(exc, (APITimeoutError, APIConnectionError, httpx.TransportError)):
            return True
        if isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _retry_async(factory, *, attempts: int, retryable, what: str):
    """Await ``factory()`` up to *attempts* times with exponential backoff + jitter.

    Only exceptions for which ``retryable(exc)`` is true are retried; the last
    error is re-raised once attempts are exhausted.
    """
    for attempt in range(attempts):
        try:
            return await factory()
        except Exception as e:
            if attempt == attempts - 1 or not retryable(e):
                raise
            delay = _backoff_delay(attempt)
            log.warning("%s failed (%s: %s); retry %d/%d in %.1fs",
                        what, type(e).__name__, str(e)[:100], attempt + 1, attempts - 1, delay)
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Search + page fetching
# ---------------------------------------------------------------------------

def _ddg_search_sync(query: str, max_results: int) -> list[dict]:
    """Synchronous DuckDuckGo search — runs in a thread pool."""
    try:
        with DDGS(timeout=SEARCH_TIMEOUT) as ddgs:
            return list(ddgs.text(query, max_results=max_results, backend=SEARCH_BACKENDS))
    except Exception as e:
        print(f"Search backend failed: {e}")
        return []


async def _ddg_search(query: str, max_results: int = DDG_MAX_RESULTS) -> list[dict]:
    """Async wrapper: run DDGS.text in the default executor, retrying empty
    (usually rate-limited) results with exponential backoff."""
    loop = asyncio.get_running_loop()
    sem = _search_semaphores.setdefault(id(loop), asyncio.Semaphore(SEARCH_CONCURRENCY))

    async def attempt() -> list[dict]:
        async with sem:
            results = await loop.run_in_executor(None, partial(_ddg_search_sync, query, max_results))
        if not results:
            raise TransientFetchError("empty search result")
        return results

    try:
        return await _retry_async(
            attempt, attempts=SEARCH_ATTEMPTS,
            retryable=lambda e: isinstance(e, TransientFetchError), what=f"search {query[:40]!r}",
        )
    except TransientFetchError:
        return []


@dataclass
class Page:
    """One fetched page: readable text plus what plain-text extraction drops."""

    url: str
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute url)
    published: str | None = None  # from page metadata (YYYY-MM-DD)
    modified: str | None = None   # from page metadata (YYYY-MM-DD) - never a publish date
    raw_text: str = ""            # all visible text incl. footer/header (for entity grounding only)
    headline: str = ""            # og:title, else first <h1>
    json_ld: list[dict] = field(default_factory=list)  # flattened JSON-LD objects
    offsite_redirect: str | None = None  # final URL when the fetch left the domain (page dropped)


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


def _visible_text(html: str) -> str:
    """All visible text of a page, including footers/headers that trafilatura drops
    (that is where the legal name / org number usually sits)."""
    try:
        try:
            doc = lxml_html.fromstring(html)
        except ValueError:
            doc = lxml_html.fromstring(html.encode("utf-8"))
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        title = " ".join(doc.xpath("//title/text()"))
        return f"{title} {doc.text_content()}"
    except Exception:
        return ""


def _flatten_json_ld(data) -> list[dict]:
    if isinstance(data, list):
        return [o for item in data for o in _flatten_json_ld(item)]
    if not isinstance(data, dict):
        return []
    return [data] + _flatten_json_ld(data.get("@graph", []))


def _extract_structure(html: str) -> tuple[str, list[dict]]:
    """Headline (og:title, else first <h1>) and flattened JSON-LD objects."""
    try:
        try:
            doc = lxml_html.fromstring(html)
        except ValueError:
            doc = lxml_html.fromstring(html.encode("utf-8"))
    except Exception:
        return "", []
    headline = ""
    og = doc.xpath("//meta[@property='og:title' or @name='og:title']/@content")
    if og and og[0].strip():
        headline = og[0]
    else:
        h1 = doc.xpath("//h1")
        if h1:
            headline = h1[0].text_content()
    objects: list[dict] = []
    for script in doc.xpath("//script[@type='application/ld+json']"):
        try:
            objects += _flatten_json_ld(json.loads(script.text_content(), strict=False))
        except (ValueError, TypeError):
            continue
    return " ".join(headline.split()), objects


def _download(url: str) -> tuple[str | None, str | None]:
    """(html, offsite_final_url). A fetch whose redirects end on another
    registrable domain is dropped: its content belongs to a third party."""
    response = trafilatura_downloads.fetch_response(url, decode=True)
    if not response or not response.data:
        return None, None
    final = urljoin(url, getattr(response, "url", None) or url)  # may be relative ("/")
    if registrable_domain(final) != registrable_domain(url):
        log.info("Dropped %s: redirected off-site to %s", url, final)
        return None, final
    if not trafilatura_downloads._is_suitable_response(url, response, trafilatura_downloads.Extractor()):
        return None, None
    return response.html, None


def _fetch_page_sync(url: str) -> Page:
    """Fetch a URL with trafilatura; keep text, links and date metadata."""
    downloaded, offsite = _download(url)
    if downloaded is None:
        return Page(url, offsite_redirect=offsite)
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    ) or ""
    published, modified = _extract_page_dates(downloaded)
    headline, json_ld = _extract_structure(downloaded)
    return Page(url, text, _extract_links(downloaded, url), published, modified,
                _visible_text(downloaded), headline, json_ld)


async def _fetch_page(url: str) -> Page:
    """Async wrapper: trafilatura with a 5-second timeout per attempt; empty
    results and timeouts are retried with exponential backoff."""
    loop = asyncio.get_running_loop()

    async def attempt() -> Page:
        try:
            page = await asyncio.wait_for(
                loop.run_in_executor(None, partial(_fetch_page_sync, url)),
                timeout=5.0,
            )
        except TimeoutError:
            raise TransientFetchError("timeout") from None
        if page.offsite_redirect:
            return page  # deterministic: retrying would redirect off-site again
        if not (page.text or page.links):
            raise TransientFetchError("no content (blocked or rate limited)")
        return page

    try:
        return await _retry_async(
            attempt, attempts=SCRAPE_ATTEMPTS,
            retryable=lambda e: isinstance(e, TransientFetchError), what=f"fetch {url[:60]}",
        )
    except TransientFetchError:
        return Page(url)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

_NEWSY_PATH_RE = re.compile(r"/(news|nyhet|press|artikl|article|blog|stor(?:y|ies)|20\d\d)", re.IGNORECASE)
_SOCIAL_HOSTS = {h for hosts in PLATFORM_HOSTS.values() for h in hosts}


def _select_links(page: Page, limit: int = PAGE_LINKS_SHOWN) -> list[tuple[str, str]]:
    """Social profile links first, then same-site links that look like articles."""
    own = registrable_domain(page.url)
    socials, careers, articles = [], [], []
    for text, href in page.links:
        dom = registrable_domain(href)
        if dom in _SOCIAL_HOSTS:
            socials.append((text, href))
        elif dom == own and CAREERS_HINT_RE.search(urlparse(href).path):
            careers.append((text, href))
        elif dom == own and text and _NEWSY_PATH_RE.search(urlparse(href).path):
            articles.append((text, href))
    return (socials[:12] + careers[:5] + articles)[:limit]


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
            title=_strip_leading_date(cand.title), url=cand.url, published_date=cand.published_date,
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


def _grounding_text(page: Page) -> str:
    """Everything on a page that may carry the legal name / org number."""
    return f"{page.raw_text} {page.text} {' '.join(t for t, _ in page.links)}"


def _is_grounded(page: Page, validator: DomainValidator) -> bool:
    return validator.page_names_entity(page.url, _grounding_text(page))


def verify_pages(pages: list[Page], validator: DomainValidator) -> list[Page]:
    """Keep only pages whose domain is grounded in the entity's name / org number.

    Pages with no content are left alone (nothing to ground, nothing reaches the LLM).
    """
    by_domain: dict[str | None, list[Page]] = {}
    for p in pages:
        by_domain.setdefault(registrable_domain(p.url), []).append(p)
    for domain, group in by_domain.items():
        fetched = [p for p in group if p.text or p.links]
        if not fetched:
            continue
        grounded = next((p for p in fetched if _is_grounded(p, validator)), None)
        if grounded is not None:
            validator.verify_content(grounded.url, _grounding_text(grounded))  # records verification
        else:
            validator.verify_content(fetched[0].url, None)  # records rejection + logs reason
    return [p for p in pages if validator.check(p.url).accepted]


# --- Grounding fallback crawl ---------------------------------------------
# When no fetched page of a domain names the entity, the legal name / org number
# often sits on an "about" or "contact" page (or in a legal/privacy page linked
# from the footer). Before rejecting the domain we look there - same domain only,
# at most GROUNDING_MAX_DEPTH link layers and GROUNDING_MAX_FETCHES extra fetches
# per domain. The match itself is unchanged: validator.page_names_entity.

GROUNDING_MAX_DEPTH = 2
GROUNDING_MAX_FETCHES = 4
GROUNDING_FETCH_TIMEOUT = 5.0
# Probed on the site root when the fetched pages link to no about/contact/legal page.
GROUNDING_FALLBACK_PATHS = ("/om-oss", "/kontakt", "/about", "/contact")
# Higher-priority hints first: about/contact, then legal pages usually linked from the footer.
_GROUNDING_HINTS = (
    re.compile(r"\b(om[\s_-]?oss|about|who[\s_-]?we[\s_-]?are|hvem[\s_-]?(er[\s_-]?)?vi|"
               r"selskapet|company|firma|kontakt|contact)", re.IGNORECASE),
    re.compile(r"\b(impressum|personvern|privacy|cookie|vilk[aå]r|terms|legal|juridisk)", re.IGNORECASE),
)
_NON_HTML_RE = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.IGNORECASE)


def _grounding_candidates(pages: list[Page], domain: str, seen: set[str]) -> list[str]:
    """Same-domain about/contact/legal links from *pages*, best first, not yet in *seen*."""
    ranked: list[tuple[int, int, str]] = []
    picked: set[str] = set()
    for page in pages:
        for text, href in page.links:
            if registrable_domain(href) != domain:
                continue
            path = urlparse(href).path
            if _NON_HTML_RE.search(path):
                continue
            key = _canon(href)
            if key in seen or key in picked:
                continue
            haystack = f"{path.replace('/', ' ')} {text}"
            rank = next((i for i, rx in enumerate(_GROUNDING_HINTS) if rx.search(haystack)), None)
            if rank is not None:
                picked.add(key)
                ranked.append((rank, len(ranked), href))
    return [href for _, _, href in sorted(ranked)]


async def _fetch_page_once(url: str) -> Page:
    """Single-attempt fetch for grounding probes: a missing /about page is a 404,
    not a transient error, so it is not worth the retry/backoff cycle."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, partial(_fetch_page_sync, url)),
            timeout=GROUNDING_FETCH_TIMEOUT,
        )
    except Exception:
        return Page(url)


async def _find_grounding_page(
    domain: str, fetched: list[Page], validator: DomainValidator, fetch,
) -> Page | None:
    """Breadth-first search for a same-domain page that names the entity."""
    seen = {_canon(p.url) for p in fetched}
    budget = GROUNDING_MAX_FETCHES
    layer = fetched
    for depth in range(1, GROUNDING_MAX_DEPTH + 1):
        candidates = _grounding_candidates(layer, domain, seen)
        if candidates:
            # Leave room for the next layer (an about page linking to contact/legal).
            layers_left = GROUNDING_MAX_DEPTH - depth + 1
            candidates = candidates[:-(-budget // layers_left)]
        elif depth == 1:
            # Nothing promising linked: probe the usual paths on the site root.
            root = urlparse(fetched[0].url)
            base = f"{root.scheme or 'https'}://{root.hostname}"
            candidates = [base + path for path in GROUNDING_FALLBACK_PATHS
                          if _canon(base + path) not in seen][:budget]
        if not candidates:
            return None
        budget -= len(candidates)
        seen.update(_canon(c) for c in candidates)
        layer = [p for p in await asyncio.gather(*[fetch(c) for c in candidates])
                 if (p.text or p.links) and registrable_domain(p.url) == domain]
        for page in layer:
            if _is_grounded(page, validator):
                log.info("[%s] Grounded %s via %s", validator.enhet.organisasjonsnummer, domain, page.url)
                return page
        if budget <= 0:
            return None
    return None


async def ground_pages(pages: list[Page], validator: DomainValidator, fetch=None) -> list[Page]:
    """:func:`verify_pages`, but a domain whose fetched pages do not name the entity
    first gets a bounded look at its about/contact pages. The page that names the
    entity is added to the result (it is the entity's own site and useful context)."""
    fetch = fetch or _fetch_page_once
    by_domain: dict[str, list[Page]] = {}
    for p in pages:
        dom = registrable_domain(p.url)
        if dom and (p.text or p.links):
            by_domain.setdefault(dom, []).append(p)
    pending = {
        dom: group for dom, group in by_domain.items()
        if validator.may_fetch(group[0].url)
        and not any(_is_grounded(p, validator) for p in group)
    }
    if pending:
        found = await asyncio.gather(*[
            _find_grounding_page(dom, group, validator, fetch) for dom, group in pending.items()
        ])
        pages = pages + [p for p in found if p is not None]
    return verify_pages(pages, validator)


# ---------------------------------------------------------------------------
# Synthesis: company news + open job postings from the company's own site
# ---------------------------------------------------------------------------
# Deterministic (no LLM). From the grounded pages we find news and careers hubs
# (/news, /nyheter, /careers, /karriere ... or the links pointing there), collect
# the individual article / posting links and fetch each one. GOLDEN RULE: an item
# is kept only if ITS OWN page passes validator.text_names_entity (exact Brreg
# name or org number). Everything is same-domain, except postings hosted on a
# known applicant-tracking system that the company's own site links to.

ACTIVITY_MAX_HUBS = 2         # listing pages per kind (news / jobs)
ACTIVITY_MAX_ITEMS = 5        # individual pages fetched per kind
ACTIVITY_MAX_NEWS = 5
ACTIVITY_MAX_JOBS = 10
NEWS_HUB_PATHS = ("/nyheter", "/news")
JOBS_HUB_PATHS = ("/karriere", "/careers", "/ledige-stillinger")
# Hard ceiling on extra fetches per company: news hubs + nested sub-hubs + job
# hubs (or probes) + item pages. Every URL is fetched at most once.
ACTIVITY_MAX_FETCHES = (ACTIVITY_MAX_HUBS + max(ACTIVITY_MAX_HUBS, len(NEWS_HUB_PATHS))
                        + max(ACTIVITY_MAX_HUBS, len(JOBS_HUB_PATHS)) + 2 * ACTIVITY_MAX_ITEMS)

_NEWS_HUB_RE = re.compile(
    r"^(news|nyheter|nyhet|aktuelt|nytt|presse|press|pressemeldinger|press-releases|"
    r"newsroom|media|blogg?|artikler|articles|stories|insights)$", re.IGNORECASE)
_JOB_CUE_RE = re.compile(
    r"(søknadsfrist|soknadsfrist|søk på stillingen|søk stillingen|send søknad|søk her|"
    r"apply now|apply for this|apply here|application deadline|ansettelsesform|"
    r"stillingsprosent|employment type|arbeidssted|tiltredelse|job type|full[- ]time|heltid|deltid)",
    re.IGNORECASE)
_JOB_CLOSED_RE = re.compile(
    r"(stillingen er besatt|stillingen er ikke lenger ledig|søknadsfristen er (?:utløpt|gått ut)|"
    r"position has been filled|no longer accepting applications|this (?:job|position) (?:is|has) (?:closed|expired)|"
    r"job (?:has )?expired)", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "feb": 2, "februar": 2, "february": 2,
    "mar": 3, "mars": 3, "march": 3, "apr": 4, "april": 4, "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "des": 12, "desember": 12, "dec": 12, "december": 12,
}
_MONTH_RX = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_RX = (
    r"(?:(?P<y1>\d{4})-(?P<m1>\d{1,2})-(?P<d1>\d{1,2})"
    r"|(?P<d2>\d{1,2})[./](?P<m2>\d{1,2})[./](?P<y2>\d{4})"
    rf"|(?P<d3>\d{{1,2}})\.?\s+(?P<mn3>{_MONTH_RX})\.?\s+(?P<y3>\d{{4}})"
    rf"|(?P<mn4>{_MONTH_RX})\.?\s+(?P<d4>\d{{1,2}}),?\s+(?P<y4>\d{{4}}))"
)
_PUBLISHED_TEXT_RE = re.compile(
    rf"\b(?:publisert|published|posted|publiseringsdato)\b\s*(?:on|den|:)?\s*:?\s*{_DATE_RX}", re.IGNORECASE)
_DEADLINE_TEXT_RE = re.compile(
    rf"\b(?:søknadsfrist|soknadsfrist|application deadline|deadline|frist)\b\s*:?\s*{_DATE_RX}", re.IGNORECASE)


_LEADING_DATE_RE = re.compile(rf"^\s*{_DATE_RX}\s*[-–—|:·]?\s*", re.IGNORECASE)


def _strip_leading_date(title: str) -> str:
    """'13 November 2026Ex. dividend ...' -> 'Ex. dividend ...' (card text glued to the date).
    Keeps the title unchanged if nothing would remain."""
    stripped = _LEADING_DATE_RE.sub("", title or "", count=1)
    return stripped if len(stripped.strip()) >= 3 else title


def _iso_from_match(m: re.Match) -> str | None:
    g = m.groupdict()
    for y, mo, d in (("y1", "m1", "d1"), ("y2", "m2", "d2"), ("y3", "mn3", "d3"), ("y4", "mn4", "d4")):
        if g.get(y):
            month = g[mo]
            month = _MONTHS.get(month.lower().rstrip(".")) if not month.isdigit() else int(month)
            try:
                return date(int(g[y]), int(month), int(g[d])).isoformat()
            except (TypeError, ValueError):
                return None
    return None


def _labelled_date(rx: re.Pattern, text: str) -> tuple[str, str] | None:
    """(YYYY-MM-DD, verbatim evidence) for the first labelled date in *text*."""
    m = rx.search(" ".join(text.split()))
    if not m:
        return None
    iso = _iso_from_match(m)
    return (iso, m.group(0)) if iso else None


def _ld_date(value) -> str | None:
    m = _ISO_DATE_RE.search(str(value or ""))
    return m.group(1) if m else None


def _ld_type(obj: dict) -> set[str]:
    t = obj.get("@type")
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _ld_location(obj: dict) -> str | None:
    locs = obj.get("jobLocation")
    for loc in (locs if isinstance(locs, list) else [locs]):
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion")]
            text = ", ".join(str(p) for p in parts if p)
            if text:
                return text
        elif isinstance(addr, str) and addr.strip():
            return addr.strip()
    return None


def _path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.lower().split("/") if s]


def _is_ats(url: str) -> bool:
    return registrable_domain(url) in ATS_DOMAINS


def _is_news_hub(text: str, href: str) -> bool:
    segs = _path_segments(href)
    last = segs[-1].rsplit(".", 1)[0] if segs else ""
    return bool(_NEWS_HUB_RE.match(last)) or (len(segs) <= 2 and bool(_NEWS_HUB_RE.match(_fold(text))))


_JOB_HUB_SEGMENT_RE = re.compile(
    r"^(jobs?|careers?|karriere|jobb|jobbe?-hos-oss|stillinger|ledige-stillinger|ledige-jobber|"
    r"vacancies|positions|open-positions|join-us|work-with-us|companyadverts)$", re.IGNORECASE)


def _is_job_hub(text: str, href: str) -> bool:
    """A careers listing page (not one posting)."""
    segs = _path_segments(href)
    last = segs[-1].rsplit(".", 1)[0] if segs else ""
    if _JOB_HUB_SEGMENT_RE.match(last):
        return True
    if _is_ats(href):
        return len(segs) <= 1
    return not segs[1:] and bool(CAREERS_HINT_RE.search(f"{last} {text}"))


def _looks_like_headline(text: str) -> bool:
    return len(text) >= 12 and len(text.split()) >= 3 and not is_ui_label(text)


@dataclass
class _Lead:
    url: str
    anchor: str
    source: str  # company-owned page that links (directly or via an ATS hub) to url


def _hub_links(site: list[Page], domain_ok, is_hub) -> list[_Lead]:
    seen: set[str] = set()
    hubs: list[_Lead] = []
    for page in site:
        for text, href in page.links:
            key = _canon(href)
            if key in seen or not domain_ok(href) or not is_hub(text, href):
                continue
            seen.add(key)
            hubs.append(_Lead(href, text, page.url))
    return sorted(hubs, key=lambda h: len(_path_segments(h.url)))


_NEWS_SECTION_WORD_RE = re.compile(r"(nyhet|news|press|reportasj|artikl|article|blog|aktuelt|media)", re.IGNORECASE)


def _slug_tokens(href: str) -> list[str]:
    segs = _path_segments(href)
    return [t for t in re.split(r"[-_.]+", segs[-1]) if t] if segs else []


def _is_news_subhub(href: str) -> bool:
    """A short section slug such as /aktuelt/nyheter-og-reportasjer (a listing, not an article)."""
    last = "-".join(_slug_tokens(href))
    return bool(last) and len(_slug_tokens(href)) <= 3 and not re.search(r"\d", last) and bool(
        _NEWS_SECTION_WORD_RE.search(last))


def _article_score(href: str) -> int:
    """Article-likeness of a URL: long slugs, ids and years in the path."""
    score = len(_slug_tokens(href))
    if re.search(r"\d", "".join(_slug_tokens(href))) or re.search(r"/20\d\d/", urlparse(href).path):
        score += 3
    return score


def _news_leads(sources: list[tuple[Page, str, bool]], domain: set[str], skip: set[str]) -> list[_Lead]:
    """Article links: from news hubs (below the hub's path), or newsy-path links
    with a headline anchor anywhere on the site. Hub links and article-like
    URLs first."""
    ranked: list[tuple[int, int, _Lead]] = []
    seen = set(skip)
    for page, owner, from_hub in sources:
        hub_path = urlparse(page.url).path.rstrip("/") + "/"
        for text, href in page.links:
            key = _canon(href)
            if key in seen or registrable_domain(href) not in domain or article_url_problem(href):
                continue
            path = urlparse(href).path
            if _is_news_hub(text, href) or _is_news_subhub(href) or _is_job_hub(text, href):
                continue
            if from_hub:
                if not (path.startswith(hub_path) or _NEWSY_PATH_RE.search(path)):
                    continue
                if not (_looks_like_headline(text) or _article_score(href) >= 4):
                    continue
            elif not (_NEWSY_PATH_RE.search(path) and _looks_like_headline(text) and len(_path_segments(href)) >= 2):
                continue
            seen.add(key)
            ranked.append((0 if from_hub else 1, -_article_score(href), _Lead(href, text, owner)))
    return [lead for *_, lead in sorted(ranked, key=lambda r: (r[0], r[1]))]


def _job_leads(sources: list[tuple[Page, str]], domain: set[str], skip: set[str]) -> list[_Lead]:
    leads, seen = [], set(skip)
    for page, owner in sources:
        hub_host = (urlparse(page.url).hostname or "").lower()
        for text, href in page.links:
            key = _canon(href)
            if key in seen:
                continue
            host = (urlparse(href).hostname or "").lower()
            if _is_job_hub(text, href):
                continue
            if registrable_domain(href) in domain:
                # Own site: a careers path below the hub (/karriere/<posting>).
                if not CAREERS_HINT_RE.search(urlparse(href).path):
                    continue
            elif _is_ats(href):
                # On an ATS board only follow the same host (one company's board).
                if _is_ats(page.url) and host != hub_host:
                    continue
            else:
                continue
            seen.add(key)
            leads.append(_Lead(href, text, owner))
    # Postings on an ATS or with an id / long slug first; career-info pages last.
    return sorted(leads, key=lambda lead: (not _is_ats(lead.url), -_article_score(lead.url)))


def _news_item(page: Page, lead: _Lead, evidence: str, today: str) -> CompanyNewsItem:
    """Build a news item from its own fetched page (raises ValidationError/ValueError)."""
    if page.published:
        published, date_evidence = page.published, f"datePublished {page.published} (page metadata)"
    elif found := _labelled_date(_PUBLISHED_TEXT_RE, page.raw_text or page.text):
        published, date_evidence = found
    else:
        raise ValueError("no publication date on the article page")
    title = page.headline if _looks_like_headline(page.headline) else lead.anchor
    return CompanyNewsItem(
        title=title, url=lead.url, published_date=published, date_type="published",
        date_evidence=date_evidence, source_url=lead.source, entity_evidence=evidence,
        retrieval_date=today,
    )


def _job_posting(page: Page, lead: _Lead, evidence: str, navn: str, today: str) -> JobPosting:
    """Build a posting from its own fetched page (raises ValidationError/ValueError)."""
    text = f"{page.raw_text} {page.text}"
    ld = next((o for o in page.json_ld if "jobposting" in _ld_type(o)), None)
    deadline = _labelled_date(_DEADLINE_TEXT_RE, text)
    # A specific open vacancy carries JobPosting data, or an application deadline
    # alongside posting cues. Role descriptions, internship/graduate programme and
    # "life at X" pages have neither and are not claimed as open postings.
    if ld is None and not (deadline and _JOB_CUE_RE.search(text)):
        raise ValueError("page does not look like a job posting (no JobPosting data or application deadline)")
    if _JOB_CLOSED_RE.search(text):
        raise ValueError("posting is closed")
    valid_through = _ld_date(ld.get("validThrough")) if ld else None
    if not valid_through and deadline:
        valid_through = deadline[0]
    if valid_through and valid_through < today:
        raise ValueError(f"posting expired ({valid_through})")
    title = " ".join(str((ld or {}).get("title") or page.headline or lead.anchor).split())
    if _fold(title) == _fold(navn):
        title = lead.anchor  # the page heading is just the company name
    return JobPosting(
        title=title, url=lead.url, source_url=lead.source, entity_evidence=evidence,
        date_posted=_ld_date(ld.get("datePosted")) if ld else None,
        valid_through=valid_through, location=_ld_location(ld) if ld else None,
        retrieval_date=today,
    )


async def crawl_site_activity(
    pages: list[Page], validator: DomainValidator, fetch=None,
) -> tuple[list[CompanyNewsItem], list[JobPosting]]:
    """Company news and open job postings, each grounded on its own page."""
    fetch = fetch or _fetch_page_once
    oid = validator.enhet.organisasjonsnummer
    today = date.today().isoformat()
    site = [p for p in pages if (p.text or p.links) and validator.check(p.url).accepted]
    if not site:
        return [], []
    own = {registrable_domain(p.url) for p in site}
    cache: dict[str, Page] = {_canon(p.url): p for p in pages if p.text or p.links}

    async def get(url: str) -> Page:
        key = _canon(url)
        if key not in cache:
            cache[key] = await fetch(url)
        return cache[key]

    def own_url(href: str) -> bool:
        return registrable_domain(href) in own

    # -- 1. hubs: linked listing pages, else probe the usual paths ---------------
    news_hubs = _hub_links(site, own_url, _is_news_hub)[:ACTIVITY_MAX_HUBS]
    job_hubs = _hub_links(site, lambda h: own_url(h) or _is_ats(h), _is_job_hub)[:ACTIVITY_MAX_HUBS]
    root = urlparse(site[0].url)
    base = f"{root.scheme or 'https'}://{root.hostname}"
    if not news_hubs:
        news_hubs = [_Lead(base + p, "", site[0].url) for p in NEWS_HUB_PATHS]
    if not job_hubs:
        job_hubs = [_Lead(base + p, "", site[0].url) for p in JOBS_HUB_PATHS]
    hub_pages = await asyncio.gather(*[get(h.url) for h in news_hubs + job_hubs])
    fetched_news_hubs = [(p, h) for p, h in zip(hub_pages[:len(news_hubs)], news_hubs) if p.text or p.links]
    fetched_job_hubs = [(p, h) for p, h in zip(hub_pages[len(news_hubs):], job_hubs) if p.text or p.links]

    # One nested level: /aktuelt -> /aktuelt/nyheter-og-reportasjer.
    hub_keys = {_canon(h.url) for h in news_hubs + job_hubs} | {_canon(p.url) for p in site}
    sub_hubs: list[_Lead] = []
    for page, _ in fetched_news_hubs:
        prefix = urlparse(page.url).path.rstrip("/") + "/"
        for text, href in page.links:
            if (own_url(href) and urlparse(href).path.startswith(prefix) and _is_news_subhub(href)
                    and _canon(href) not in hub_keys | {_canon(s.url) for s in sub_hubs}):
                sub_hubs.append(_Lead(href, text, page.url))
    sub_hubs = sub_hubs[:ACTIVITY_MAX_HUBS]
    if sub_hubs:
        sub_pages = await asyncio.gather(*[get(h.url) for h in sub_hubs])
        fetched_news_hubs += [(p, h) for p, h in zip(sub_pages, sub_hubs) if p.text or p.links]
        hub_keys |= {_canon(h.url) for h in sub_hubs}

    # A link's "source" is the company-owned page it was found on (for an ATS
    # board: the own page that linked the board).
    news_sources = [(p, p.url, True) for p, _ in reversed(fetched_news_hubs)] + [
        (p, p.url, False) for p in site]
    job_sources = [(p, p.url) for p in site] + [
        (p, h.source if _is_ats(p.url) else p.url) for p, h in fetched_job_hubs]

    # -- 2. individual items: fetch each and apply the golden rule ---------------
    news_leads = _news_leads(news_sources, own, hub_keys)[:ACTIVITY_MAX_ITEMS]
    job_leads = _job_leads(job_sources, own, hub_keys)[:ACTIVITY_MAX_ITEMS]
    item_pages = await asyncio.gather(*[get(lead.url) for lead in news_leads + job_leads])

    news: list[CompanyNewsItem] = []
    jobs: list[JobPosting] = []
    for i, (lead, page) in enumerate(zip(news_leads + job_leads, item_pages)):
        kind = "news" if i < len(news_leads) else "job"
        if not (page.text or page.links):
            reason = f"redirected off-site to {page.offsite_redirect}" if page.offsite_redirect else "no content"
            log.info("[%s] Skipped %s %s: %s", oid, kind, lead.url, reason)
            continue
        evidence = validator.entity_mention(_grounding_text(page))
        if evidence is None:  # GOLDEN RULE: the item's own page must name the entity
            log.info("[%s] Rejected %s %s: %s", oid, kind, lead.url, CONTENT_MISMATCH_REASON)
            continue
        try:
            if kind == "news":
                news.append(_news_item(page, lead, evidence, today))
            else:
                jobs.append(_job_posting(page, lead, evidence, validator.enhet.navn, today))
        except (ValidationError, ValueError) as e:
            why = _first_error(e) if isinstance(e, ValidationError) else str(e)
            log.info("[%s] Rejected %s %s: %s", oid, kind, lead.url, why)

    news.sort(key=lambda n: n.published_date, reverse=True)
    return news[:ACTIVITY_MAX_NEWS], jobs[:ACTIVITY_MAX_JOBS]


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

    # -- careers: same gates as news (linked on a scraped page + own domain) ---
    careers = SourcedFact()
    if extraction.careers_url:
        try:
            curl = validate_careers_url(extraction.careers_url)
            if _canon(curl) not in known_urls:
                raise ValueError("url does not appear on any scraped page")
            verdict = validator.check(curl)
            if not verdict.accepted:
                raise ValueError(f"url domain rejected: {verdict.reason}")
            src = next((p for p in pages if _canon(p.url) == _canon(curl)
                        or _canon(curl) in {_canon(u) for _, u in p.links}), None)
            careers = SourcedFact(fact_value=curl, source_url=src.url if src else None, retrieval_date=today)
        except ValueError as e:
            log.info("[%s] Rejected careers link %s: %s", oid, extraction.careers_url, e)

    # Website must sit on a domain whose fetched content named the entity.
    website = extraction.website
    site_dom = registrable_domain(website.fact_value) if website and website.fact_value else None
    if site_dom and site_dom not in {registrable_domain(p.url) for p in pages if p.text or p.links}:
        log.info("[%s] Rejected website %s: %s", oid, website.fact_value, CONTENT_MISMATCH_REASON)
        website = SourcedFact()

    return CompanyProfile(
        website=website,
        ceo_name=extraction.ceo_name,
        employee_count=extraction.employee_count,
        latest_revenue_nok=extraction.latest_revenue_nok,
        summary_of_changes=extraction.summary_of_changes,
        news=news_items,
        socials=Socials(**socials),
        careers=careers,
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

    registered = validator.registered_fetch_url
    candidates = list(dict.fromkeys(([registered] if registered else []) + urls))
    accepted: list[str] = []
    for url in candidates:
        verdict = validator.check(url)
        if verdict.accepted or validator.needs_grounding(url):
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

    # 2b. Content verification: a domain stays only if one of its pages names the
    # entity (exact Brreg name or org number). If the fetched pages do not, the
    # domain's about/contact pages are checked first (bounded crawl). Otherwise it
    # is rejected, the website becomes null and the Brreg baseline takes over.
    pages = await ground_pages(pages, validator)
    if not any(p.text or p.links for p in pages):
        return validator.sanitize_profile(CompanyProfile(summary_of_changes=CONTENT_MISMATCH_REASON))

    # ------------------------------------------------------------------
    # 3. Build prompt and call Gemini via instructor
    # ------------------------------------------------------------------
    openai_client = AsyncOpenAI(
        api_key=api_key,
        base_url=GEMINI_BASE_URL,
        timeout=60.0,
        max_retries=0,  # retries are handled by _retry_async (exponential backoff)
    )
    client = instructor.from_openai(openai_client)

    raw_user_message = _build_user_message(enhet, search_results, pages, validator.ancestors)
    truncated_message = raw_user_message[:MAX_PROMPT_CHARS]

    # Synthesis crawl (news + jobs) runs while the LLM call is in flight.
    activity_task = asyncio.create_task(_safe_crawl_site_activity(pages, validator))

    global _llm_disabled_reason
    try:
        extraction, completion = await _retry_async(
            lambda: client.chat.completions.create_with_completion(
                model=GEMINI_MODEL,
                response_model=CompanyExtraction,
                max_retries=2,  # instructor: re-asks on schema-validation failures only
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": truncated_message},
                ],
            ),
            attempts=LLM_ATTEMPTS,
            retryable=_is_retryable_llm_error,
            what=f"[{enhet.organisasjonsnummer}] Gemini call",
        )
        record_usage(enhet.organisasjonsnummer, completion)
    except Exception as e:
        activity_task.cancel()
        if _is_auth_error(e):
            _llm_disabled_reason = "GEMINI_API_KEY was rejected by the API"
        raise

    # ------------------------------------------------------------------
    # 4. Strict validation of news/socials, then drop anything attributed
    #    from a parent domain or an unverified site
    # ------------------------------------------------------------------
    profile = _build_profile(extraction, enhet, pages, validator)
    try:
        verified_news = await verify_news_entity(profile.news, pages, validator)
    except Exception as e:  # noqa: BLE001 - never ship unverified news
        log.warning("[%s] News entity check failed (%s); dropping LLM news", enhet.organisasjonsnummer, e)
        verified_news = []
    company_news, job_postings = await activity_task
    profile = profile.model_copy(update={
        "news": verified_news, "company_news": company_news, "job_postings": job_postings})
    return validator.sanitize_profile(profile)


async def verify_news_entity(
    news: list[NewsItem], pages: list[Page], validator: DomainValidator, fetch=None,
) -> list[NewsItem]:
    """GOLDEN RULE for LLM news, as for ``company_news``: each article's OWN page
    must name the entity (exact Brreg name or org number). Pages already scraped
    are reused; the rest are fetched once. Unreachable pages are dropped."""
    if not news:
        return []
    fetch = fetch or _fetch_page_once
    oid = validator.enhet.organisasjonsnummer
    have = {_canon(p.url): p for p in pages if p.text or p.links}

    async def own_page(url: str) -> Page:
        return have.get(_canon(url)) or await fetch(url)

    article_pages = await asyncio.gather(*[own_page(n.url) for n in news])
    kept = []
    for item, page in zip(news, article_pages):
        if not (page.text or page.links):
            log.info("[%s] Rejected news %s: article page could not be fetched", oid, item.url)
        elif not validator.text_names_entity(_grounding_text(page)):
            log.info("[%s] Rejected news %s: %s on the article page", oid, item.url, CONTENT_MISMATCH_REASON)
        else:
            kept.append(item)
    return kept


async def _safe_crawl_site_activity(pages: list[Page], validator: DomainValidator):
    """crawl_site_activity that never fails the profile (synthesis is best-effort)."""
    try:
        return await crawl_site_activity(pages, validator)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Site activity crawl failed: %s", validator.enhet.organisasjonsnummer, e)
        return [], []


# ---------------------------------------------------------------------------
# CLI  (run with:  python extractor.py <domain | orgnr> [--orgnr N] [--name NAME])
# ---------------------------------------------------------------------------

async def resolve_enhet(
    target: str, orgnr: str | None = None, name: str | None = None
) -> Enhet | None:
    """Resolve a CLI target (org number or domain) to a Brreg Enhet.

    * ``target`` is a 9-digit org number → direct Brreg lookup.
    * ``target`` is a domain → ``--orgnr`` if given, else a Brreg name search
      (``--name``, or the domain's leading label, e.g. ``telenor.com`` →
      ``telenor``) preferring a hit whose registered hjemmeside is that domain.
      If the resolved entity has no hjemmeside in Brreg, the given domain is
      used as its website so the domain validator trusts it.
    """
    from brreg_client import lookup_enhet, search_enheter
    from domain_validator import registrable_domain

    digits = re.sub(r"\s", "", target)
    if re.fullmatch(r"\d{9}", digits):
        return await lookup_enhet(digits)

    domain = registrable_domain(target if "://" in target else f"https://{target}")
    if not domain:
        raise ValueError(f"Not an org number or domain: {target!r}")

    if orgnr:
        enhet = await lookup_enhet(re.sub(r"\s", "", orgnr))
    else:
        label = domain.split(".")[0]
        query = name or label
        wanted = {_fold(query), _fold(label)}

        def score(h: Enhet) -> int:
            reg = registrable_domain(h.hjemmeside)
            s = 0
            if reg == domain:
                s += 100
            elif reg and reg.split(".")[0] == label:  # telenor.com ↔ telenor.no
                s += 50
            bare = re.sub(r"\s+(asa|as|a/s|sa|ab)$", "", _fold(h.navn))
            if bare in wanted:
                s += 30
            if h.organisasjonsform.kode in {"ASA", "AS"}:
                s += 5
            return s

        hits = sorted(await search_enheter(query), key=score, reverse=True)
        # Require a website or exact-name match; never guess from a loose hit.
        enhet = hits[0] if hits and score(hits[0]) >= 30 else None
    if enhet is not None and not enhet.hjemmeside:
        enhet = enhet.model_copy(update={"hjemmeside": domain})
    return enhet


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Extract a company profile.")
    parser.add_argument("target", help="Company domain (e.g. telenor.com) or 9-digit org number")
    parser.add_argument("--orgnr", help="Org number to use when target is a domain")
    parser.add_argument("--name", help="Company name to search Brreg with when target is a domain")
    args = parser.parse_args()

    async def _run() -> int:
        enhet = await resolve_enhet(args.target, args.orgnr, args.name)
        if enhet is None:
            print(f"Could not find organisation in Brreg for {args.target!r}.", file=sys.stderr)
            return 1

        print(f"Extracting profile for: {enhet.navn} ({enhet.organisasjonsnummer})", file=sys.stderr)
        print(f"Registered website: {enhet.hjemmeside or '-'}\n", file=sys.stderr)

        profile = await extract_company_profile(enhet)
        print(json.dumps(profile.model_dump(), indent=2, ensure_ascii=False))
        return 0

    sys.exit(asyncio.run(_run()))
