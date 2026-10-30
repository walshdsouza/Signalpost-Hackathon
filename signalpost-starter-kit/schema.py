"""
Pydantic schema for a Norwegian company intelligence profile.

Each data point is wrapped in a SourcedFact so that the origin URL and
retrieval date travel alongside the value — making provenance first-class.

News items and social profiles carry strict validators: a news item must be a
real article/press release with an explicit *published* date, and a social link
must be a company profile page (not a share button or a bare homepage).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Union
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Building block
# ---------------------------------------------------------------------------

class SourcedFact(BaseModel):
    """A single piece of intelligence with full provenance metadata.

    Attributes:
        fact_value:     The actual data point (string, integer, or null when unknown).
        source_url:     The URL from which this fact was retrieved, or null.
        retrieval_date: ISO-8601 date (YYYY-MM-DD) when the fact was last fetched.
    """

    fact_value: Union[str, int, None] = Field(
        default=None,
        description="The data value; str for text, int for counts/amounts, null if unknown.",
    )
    source_url: str | None = Field(
        default=None,
        description="Canonical URL of the source page this fact was extracted from.",
    )
    retrieval_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Date the fact was retrieved, formatted as YYYY-MM-DD.",
        examples=[str(date.today())],
    )


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

MIN_NEWS_DATE = date(2000, 1, 1)

# Whole-title menu / button / navigation labels (compared after normalisation).
_UI_LABELS = {
    "news", "latest news", "all news", "more news", "see more news", "see all news",
    "view all news", "view all", "see all", "see more", "read more", "learn more",
    "load more", "show more", "continue reading", "read the article", "read article",
    "click here", "newsroom", "news room", "news archive", "archive", "media",
    "press", "press releases", "press release", "press room", "pressroom",
    "media centre", "media center", "subscribe", "contact us", "all articles",
    "nyheter", "alle nyheter", "flere nyheter", "siste nyheter", "se alle nyheter",
    "se flere nyheter", "les mer", "les mer her", "vis mer", "se mer", "last mer",
    "les saken", "pressemeldinger", "pressemelding", "presse", "pressesenter",
    "nyhetsarkiv", "nyhetsbrev", "kontakt oss", "alle artikler",
}
# Short strings shaped like "See more news", "Read all press releases", "Les mer om ...".
_UI_LABEL_RE = re.compile(
    r"^(?:see|read|show|view|load|get|click|browse|go to|les|vis|se|last)\b.*"
    r"\b(?:more|all|news|articles?|stories|releases|here|mer|alle|nyheter|flere|saker|her)\b",
    re.IGNORECASE,
)
_UI_LABEL_MAX_WORDS = 6

# URL path segments that identify listing / navigation / non-article pages.
_LISTING_SEGMENTS = {
    "news", "nyheter", "press", "presse", "media", "pressemeldinger", "pressemelding",
    "newsroom", "pressroom", "press-releases", "pressreleases", "blog", "articles",
    "artikler", "arkiv", "archive", "nyhetsarkiv", "stories", "latest",
}
_NON_ARTICLE_SEGMENTS = {
    "products", "product", "produkter", "produkt", "services", "service", "tjenester",
    "shop", "store", "careers", "karriere", "jobs", "about", "about-us", "om-oss",
    "contact", "kontakt", "login", "search",
}

# What the date evidence must NOT say: modified / updated / revised timestamps.
_NOT_PUBLISHED_RE = re.compile(
    r"\b(?:modified|updated|last\s+update[d]?|revised|edited|"
    r"oppdatert|endret|sist\s+endret|redigert|revidert)\b",
    re.IGNORECASE,
)
_NOT_PUBLISHED_TYPES = {"modified", "updated", "update", "edited", "revised", "changed", "unknown", ""}

_PUBLISHED_TYPES = {"published", "publication", "publisert", "published_at", "date_published"}


def _normalise_label(text: str) -> str:
    """Lowercase and strip punctuation/arrows so 'See more news »' == 'see more news'."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", text.lower()).split())


def is_ui_label(text: str) -> bool:
    """True if *text* looks like a menu item / button label rather than a headline."""
    norm = _normalise_label(text or "")
    if not norm:
        return True
    if norm in _UI_LABELS:
        return True
    return len(norm.split()) <= _UI_LABEL_MAX_WORDS and bool(_UI_LABEL_RE.match(norm))


class NewsItem(BaseModel):
    """A single real news article or press release published by the company.

    Constructing one raises ``ValidationError`` unless the item has a headline
    that is not a UI label, a direct article URL (not a listing/section page),
    and a definitive *published* date that is not a modified/updated date.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Verbatim headline of the article / press release.")
    url: str = Field(description="Direct URL of this specific article (not a listing page).")
    published_date: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Original publication date, YYYY-MM-DD. NEVER a modified/updated date.",
    )
    date_type: str = Field(
        description="What kind of date published_date is. Must be exactly 'published'.",
    )
    date_evidence: str = Field(
        description=(
            "Verbatim text from the page that shows the date together with its "
            "label, e.g. 'Published 12 March 2026'."
        ),
    )
    summary: str | None = Field(default=None, description="One-sentence summary, or null.")
    source_url: str | None = Field(default=None, description="Page the item was found on.")
    retrieval_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("title")
    @classmethod
    def _title_is_headline(cls, v: str) -> str:
        v = " ".join(v.split())
        if is_ui_label(v):
            raise ValueError(f"title {v!r} is a UI/navigation label, not a headline")
        if len(v) < 12 or len(v.split()) < 3:
            raise ValueError(f"title {v!r} is too short to be a headline")
        return v

    @field_validator("url")
    @classmethod
    def _url_is_article(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"url {v!r} is not an absolute http(s) URL")
        segments = [s for s in parsed.path.lower().split("/") if s]
        if not segments:
            raise ValueError(f"url {v!r} is a site root, not an article")
        if segments[0] in _NON_ARTICLE_SEGMENTS:
            raise ValueError(f"url {v!r} is a {segments[0]!r} page, not news")
        if segments[-1].rsplit(".", 1)[0] in _LISTING_SEGMENTS:
            raise ValueError(f"url {v!r} is a news listing page, not an article")
        return v

    @field_validator("date_type")
    @classmethod
    def _date_is_published(cls, v: str) -> str:
        norm = v.strip().lower().replace(" ", "_")
        if norm in _NOT_PUBLISHED_TYPES or _NOT_PUBLISHED_RE.search(v):
            raise ValueError(f"date_type {v!r} is not a publish date (modified/updated/unknown)")
        if norm not in _PUBLISHED_TYPES:
            raise ValueError(f"date_type {v!r} must be 'published'")
        return "published"

    @field_validator("date_evidence")
    @classmethod
    def _evidence_is_a_published_label(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 4 or not any(c.isdigit() for c in v):
            raise ValueError("date_evidence must quote the date text from the page")
        if _NOT_PUBLISHED_RE.search(v):
            raise ValueError(f"date_evidence {v!r} describes a modified/updated date")
        return v

    @field_validator("published_date")
    @classmethod
    def _date_is_real_and_not_future(cls, v: str) -> str:
        d = date.fromisoformat(v)  # rejects 2026-02-30 etc.
        if d > date.today():
            raise ValueError(f"published_date {v} is in the future")
        if d < MIN_NEWS_DATE:
            raise ValueError(f"published_date {v} is implausibly old")
        return v


class NewsCandidate(BaseModel):
    """Lenient shape the LLM fills in. Each candidate is validated into a
    :class:`NewsItem` in Python, so one bad item never fails the whole response."""

    title: str = ""
    url: str = ""
    published_date: str = ""
    date_type: str = Field(default="", description="'published' only; otherwise omit the item.")
    date_evidence: str = ""
    summary: str | None = None


# ---------------------------------------------------------------------------
# Socials
# ---------------------------------------------------------------------------

SOCIAL_PLATFORMS = ("linkedin", "facebook", "instagram", "x", "youtube", "tiktok")

_PLATFORM_HOSTS = {
    "linkedin": ("linkedin.com",),
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "x": ("x.com", "twitter.com"),
    "youtube": ("youtube.com",),
    "tiktok": ("tiktok.com",),
}
PLATFORM_HOSTS = _PLATFORM_HOSTS
_PLATFORM_ALIASES = {"twitter": "x", "x/twitter": "x", "yt": "youtube", "fb": "facebook"}
# First path segment that means "this is not a profile page" (share buttons, login...).
_NON_PROFILE_SEGMENTS = {
    "share", "sharer", "sharer.php", "intent", "login", "signup", "dialog", "plugins",
    "home", "explore", "hashtag", "search", "watch", "embed", "tr", "policies", "policy",
    "privacy", "legal", "help", "about", "i", "pages", "groups", "events", "p", "reel",
    "company-beta", "feed", "jobs", "pub",
}
_LINKEDIN_COMPANY_SEGMENTS = {"company", "school", "showcase"}


def normalise_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    return _PLATFORM_ALIASES.get(p, p)


def normalise_social_url(platform: str, url: str) -> str:
    """Return the canonical profile URL or raise ``ValueError``.

    Rejects: wrong host for the platform, share/intent/login links, personal
    LinkedIn profiles, and bare homepages without a handle.
    """
    platform = normalise_platform(platform)
    if platform not in _PLATFORM_HOSTS:
        raise ValueError(f"unsupported platform {platform!r}")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(
        host == h or host.endswith("." + h) for h in _PLATFORM_HOSTS[platform]
    ):
        raise ValueError(f"{url!r} is not a {platform} URL")
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise ValueError(f"{url!r} has no profile handle")
    first = segments[0].lower()
    if platform == "linkedin":
        if first not in _LINKEDIN_COMPANY_SEGMENTS or len(segments) < 2:
            raise ValueError(f"{url!r} is not a LinkedIn company page")
    elif first in _NON_PROFILE_SEGMENTS:
        raise ValueError(f"{url!r} is not a profile page ({first!r})")
    elif platform == "x" and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", segments[0]):
        raise ValueError(f"{url!r} has no valid X handle")
    # Canonical form: https, no query/fragment, only the segments that identify the profile.
    if platform == "linkedin" or (platform == "youtube" and first in {"channel", "c", "user"}):
        keep = segments[:2]
    else:
        keep = segments[:1]
    # Collapse regional/mobile hosts (no., nb-no., m., ...) to the platform's main host.
    canonical_host = next(h for h in _PLATFORM_HOSTS[platform] if host == h or host.endswith("." + h))
    if platform == "linkedin":
        canonical_host = "www." + canonical_host
    return f"https://{canonical_host}/" + "/".join(keep)


class Socials(BaseModel):
    """Official social-media profiles of the company (one SourcedFact each)."""

    linkedin: SourcedFact = Field(default_factory=SourcedFact)
    facebook: SourcedFact = Field(default_factory=SourcedFact)
    instagram: SourcedFact = Field(default_factory=SourcedFact)
    x: SourcedFact = Field(default_factory=SourcedFact)
    youtube: SourcedFact = Field(default_factory=SourcedFact)
    tiktok: SourcedFact = Field(default_factory=SourcedFact)

    @model_validator(mode="after")
    def _urls_are_profiles(self) -> "Socials":
        for platform in SOCIAL_PLATFORMS:
            value = getattr(self, platform).fact_value
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{platform} must be a URL string")
                normalise_social_url(platform, value)
        return self


class SocialCandidate(BaseModel):
    """Lenient shape the LLM fills in; validated by :func:`normalise_social_url`."""

    platform: str = Field(description="linkedin, facebook, instagram, x, youtube or tiktok")
    url: str = Field(description="Profile URL copied exactly from the LINKS section.")


# ---------------------------------------------------------------------------
# Main profile model
# ---------------------------------------------------------------------------

class _ProfileFacts(BaseModel):
    """The core SourcedFact fields shared by the output and LLM-response models."""

    website: SourcedFact = Field(
        default_factory=SourcedFact,
        description="Company's primary website URL.",
    )
    ceo_name: SourcedFact = Field(
        default_factory=SourcedFact,
        description="Full name of the current CEO / daglig leder.",
    )
    employee_count: SourcedFact = Field(
        default_factory=SourcedFact,
        description="Total number of employees (integer).",
    )
    latest_revenue_nok: SourcedFact = Field(
        default_factory=SourcedFact,
        description="Latest annual revenue in Norwegian Kroner (NOK, integer).",
    )
    summary_of_changes: str = Field(
        default="",
        description=(
            "Human-readable summary of the profile's current state: "
            "what data was found, what is missing, and any notable changes."
        ),
    )


class CompanyProfile(_ProfileFacts):
    """Aggregated open-source intelligence profile for a Norwegian company.

    Every intelligence field is a :class:`SourcedFact` so consumers can
    always trace a value back to its origin and know how fresh it is.

    Attributes:
        website:            The company's primary website URL.
        ceo_name:           Full name of the current CEO / general director.
        employee_count:     Number of employees (integer when known).
        latest_revenue_nok: Most recently published revenue figure in NOK.
        summary_of_changes: Free-text narrative describing the profile's
                            current state, what was found, and any gaps.
        news:               Validated news items (strict :class:`NewsItem`).
        socials:            Validated official social profiles.
    """

    news: list[NewsItem] = Field(
        default_factory=list,
        description="Real news articles / press releases with definitive published dates.",
    )
    socials: Socials = Field(default_factory=Socials)


class CompanyExtraction(_ProfileFacts):
    """What the LLM returns. News and socials arrive as lenient candidates and
    are validated item-by-item into :class:`CompanyProfile`."""

    news_candidates: list[NewsCandidate] = Field(
        default_factory=list,
        description="Up to 5 real news items; omit anything that is not a dated article.",
    )
    social_candidates: list[SocialCandidate] = Field(
        default_factory=list,
        description="The company's own official social profiles taken from LINKS.",
    )


# ---------------------------------------------------------------------------
# Quick smoke-test  (run with:  python schema.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    today = str(date.today())

    profile = CompanyProfile(
        website=SourcedFact(
            fact_value="https://www.equinor.com",
            source_url="https://www.equinor.com",
            retrieval_date=today,
        ),
        ceo_name=SourcedFact(
            fact_value="Anders Opedal",
            source_url="https://www.equinor.com/about-us/our-leadership",
            retrieval_date=today,
        ),
        employee_count=SourcedFact(
            fact_value=22000,
            source_url="https://www.equinor.com/investors/annual-report",
            retrieval_date=today,
        ),
        latest_revenue_nok=SourcedFact(
            fact_value=1_121_000_000_000,
            source_url="https://www.equinor.com/investors/annual-report",
            retrieval_date=today,
        ),
        summary_of_changes=(
            "Initial profile populated for Equinor ASA. "
            "All four core facts sourced from public web pages. "
            "No prior snapshot exists; this is the baseline."
        ),
    )

    print(json.dumps(profile.model_dump(), indent=2, ensure_ascii=False))
