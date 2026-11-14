"""
Domain validation — make sure a website (and anything scraped from it) belongs
to the queried legal entity and not to its parent group.

Two signals are combined:

* Brreg hierarchy: ``overordnetEnhet`` is followed up to ``MAX_ANCESTOR_DEPTH``
  levels. Any domain registered by an ancestor is never attributed to the
  subsidiary.
* Name matching: the domain label must match the entity's own name via
  :func:`entity_matcher.is_same_company`. ``overordnetEnhet`` is often unset
  for ordinary AS subsidiaries, so this is the main safeguard: ``equinor.com``
  does not match "Equinor Energy AS".

A domain listed in the entity's own Brreg ``hjemmeside`` is trusted even if its
name differs (e.g. a brand domain), unless it is an ancestor's domain or looks
like a shorter group brand of the entity's name.
"""

from __future__ import annotations

import copy
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

from brreg_client import BASE_URL, Enhet, lookup_enhet
from entity_matcher import is_same_company, normalise_name
from schema import ATS_DOMAINS, SOCIAL_PLATFORMS, CompanyProfile

log = logging.getLogger(__name__)

MAX_ANCESTOR_DEPTH = 3

_SOURCED_FACT_FIELDS = ("website", "ceo_name", "employee_count", "latest_revenue_nok")
_TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "com.au", "co.nz", "com.br"}
_SOCIAL_PREFIX_SEGMENTS = {"company", "school", "showcase", "c", "user", "channel", "pages"}
_NORDIC_ASCII = str.maketrans({"ø": "o", "æ": "ae", "å": "a", "ö": "o", "ä": "a", "é": "e"})

# Parent lookups are shared between many subsidiaries; cache by org number.
_ancestor_cache: dict[str, Enhet | None] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def registrable_domain(url: str | None) -> str | None:
    """Return ``example.no`` for ``https://www.news.example.no/x`` (or ``None``)."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if "//" not in url:
        url = "//" + url
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    labels = host.split(".")
    if len(labels) < 2 or not all(labels):
        return None
    n = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-n:])


def _ascii(text: str) -> str:
    text = text.lower().translate(_NORDIC_ASCII)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _label_matches_name(label: str, name: str) -> bool:
    """Strict check that a domain label is the entity's name, not a part of it."""
    name = _ascii(name)
    variants = {label, label.replace("-", " "), label.replace("_", " ")}
    if any(is_same_company(v, name) for v in variants):
        return True
    # "akersolutions" vs "aker solutions"
    flat_name = normalise_name(name).replace(" ", "")
    return any(is_same_company(v.replace(" ", ""), flat_name) for v in variants)


def _is_shorter_brand_of(label: str, name: str) -> bool:
    """True if *label* equals the first tokens of *name* (e.g. equinor -> Equinor Energy)."""
    tokens = normalise_name(_ascii(name)).split()
    flat = label.replace("-", "").replace("_", "")
    return any("".join(tokens[:k]) == flat for k in range(1, len(tokens)))


CONTENT_MISMATCH_REASON = "Name or org number missing from page content"


def _compact(text: str) -> str:
    """Lowercase alphanumerics only (spaces, dots, dashes, underscores removed)."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


_NORDIC_SPELLINGS = {"ø": ("o", "oe"), "æ": ("ae",), "å": ("a", "aa")}


def _name_handles(name: str) -> set[str]:
    """Every exact handle spelling of *name*: each Nordic letter in either common
    spelling (mixed too: "brodreneaase"), with and without its legal-form suffix
    ("telenor", "telenorasa"). Bounded: only the name's own letters expand."""
    spellings = {name.lower()}
    for letter, options in _NORDIC_SPELLINGS.items():
        for _ in range(min(name.lower().count(letter), 4)):
            spellings = {s.replace(letter, o, 1) for s in spellings for o in options}
    out: set[str] = set()
    for spelled in spellings:
        spelled = unicodedata.normalize("NFKD", spelled).encode("ascii", "ignore").decode()
        out.add(_compact(normalise_name(spelled)))
        out.add(_compact(spelled))
    out.discard("")
    return out

# Legal-form suffixes dropped when matching a trading name on the registered site.
_LEGAL_SUFFIX_RE = re.compile(r"\s+(asa|as|a/s|sa|ans|da|enk|nuf|ba|sf|ks)$")
_MIN_TRADING_NAME_LEN = 5


def _fold_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class DomainValidator:
    """Decides which domains may be attributed to one specific entity."""

    def __init__(self, enhet: Enhet, ancestors: list[Enhet] | None = None) -> None:
        self.enhet = enhet
        self.ancestors = ancestors or []
        self.ancestor_domains: set[str] = {
            d for a in self.ancestors if (d := registrable_domain(a.hjemmeside))
        }
        self.registered_domain = registrable_domain(enhet.hjemmeside)
        # Domains whose fetched page text named neither the entity nor its org number.
        self.content_rejected: set[str] = set()
        # Domains whose fetched page text DID name the entity (exact name / org number).
        self.content_verified: set[str] = set()

    @classmethod
    async def for_entity(cls, enhet: Enhet) -> "DomainValidator":
        """Build a validator, following ``overordnetEnhet`` to collect ancestors."""
        ancestors: list[Enhet] = []
        seen = {enhet.organisasjonsnummer}
        parent_id = enhet.overordnetEnhet
        while parent_id and parent_id not in seen and len(ancestors) < MAX_ANCESTOR_DEPTH:
            seen.add(parent_id)
            if parent_id not in _ancestor_cache:
                try:
                    _ancestor_cache[parent_id] = await lookup_enhet(parent_id)
                except Exception as e:
                    # Not cached: a transient failure should be retried next time.
                    log.warning("[%s] Parent lookup %s failed: %s", enhet.organisasjonsnummer, parent_id, e)
                    break
            parent = _ancestor_cache[parent_id]
            if parent is None:
                break
            ancestors.append(parent)
            parent_id = parent.overordnetEnhet
        return cls(enhet, ancestors)

    # -- decisions ---------------------------------------------------------

    def text_names_entity(self, text: str | None) -> bool:
        """True if *text* contains the exact Brreg name (case-insensitive) or the
        9-digit organisation number (also when written as ``923 609 016``)."""
        if not text:
            return False
        if _fold_text(self.enhet.navn) in _fold_text(text):
            return True
        orgnr = _digits_only(self.enhet.organisasjonsnummer)
        if len(orgnr) != 9:
            return False
        # Digits may be spaced/dotted (923 609 016, 923.609.016) but not part of a longer number.
        pattern = r"(?<!\d)" + r"[\s. ]?".join(orgnr[i:i + 3] for i in (0, 3, 6)) + r"(?!\d)"
        return re.search(pattern, text) is not None

    def entity_mention(self, text: str | None, context: int = 40) -> str | None:
        """Quote of where *text* names the entity, for evidence only.

        Returns ``None`` exactly when :meth:`text_names_entity` is False; the
        decision itself is always made by :meth:`text_names_entity`."""
        if not self.text_names_entity(text):
            return None
        flat = " ".join(text.split())
        name_rx = r"\s+".join(re.escape(tok) for tok in self.enhet.navn.split())
        orgnr = _digits_only(self.enhet.organisasjonsnummer)
        patterns = [name_rx]
        if len(orgnr) == 9:
            patterns.append(r"(?<!\d)" + r"[\s. ]?".join(orgnr[i:i + 3] for i in (0, 3, 6)) + r"(?!\d)")
        for rx in patterns:
            if m := re.search(rx, flat, re.IGNORECASE):
                start, end = max(0, m.start() - context), min(len(flat), m.end() + context)
                return flat[start:end].strip()
        return self.enhet.navn  # casefold-only match (e.g. 'ß'): name is on the page

    def page_names_entity(self, url: str | None, text: str | None) -> bool:
        """Site-level grounding: :meth:`text_names_entity`, or - only on the entity's
        own Brreg-registered domain whose label already IS the entity's name - the
        name without its legal-form suffix ("102 Asia Mat" for "102 ASIA MAT AS").

        News items and job postings keep the strict :meth:`text_names_entity`."""
        if self.text_names_entity(text):
            return True
        domain = registrable_domain(url)
        if not text or not domain or domain != self.registered_domain:
            return False
        if domain in self.ancestor_domains or not _label_matches_name(domain.split(".")[0], self.enhet.navn):
            return False
        trading = _LEGAL_SUFFIX_RE.sub("", _fold_text(self.enhet.navn))
        if trading == _fold_text(self.enhet.navn) or len(trading) < _MIN_TRADING_NAME_LEN:
            return False
        return re.search(r"(?<!\w)" + re.escape(trading) + r"(?!\w)", _fold_text(text)) is not None

    def verify_content(self, url: str, text: str | None) -> Verdict:
        """Grounding check on a fetched page. A failing domain is rejected for good
        (``check`` refuses it afterwards) and one reason line is logged."""
        domain = registrable_domain(url)
        if self.page_names_entity(url, text):
            if domain:
                self.content_verified.add(domain)
            return Verdict(True, "page content names the entity")
        if domain:
            self.content_rejected.add(domain)
        log.info("[%s] Rejected %s: %s", self.enhet.organisasjonsnummer, url, CONTENT_MISMATCH_REASON)
        return Verdict(False, CONTENT_MISMATCH_REASON)

    def check(self, url: str | None) -> Verdict:
        domain = registrable_domain(url)
        if domain is None:
            return Verdict(False, "not a valid URL")
        if domain in self.content_rejected:
            return Verdict(False, CONTENT_MISMATCH_REASON)
        if domain in self.ancestor_domains:
            return Verdict(False, f"{domain} belongs to a parent group")

        label = domain.split(".")[0]
        if _label_matches_name(label, self.enhet.navn):
            return Verdict(True, "domain matches entity name")

        if domain == self.registered_domain:
            if _is_shorter_brand_of(label, self.enhet.navn):
                # e.g. 1818.no for "1818 OPPLYSNINGSTJENESTEN AS" - but also equinor.com
                # for "EQUINOR ENERGY AS". Only accepted once a fetched page of the
                # domain names this exact entity (legal name or org number).
                if domain in self.content_verified:
                    return Verdict(True, "listed as hjemmeside in Brreg; page names the entity")
                return Verdict(False, f"registered site {domain} looks like a parent/group brand")
            return Verdict(True, "listed as hjemmeside in Brreg")

        return Verdict(False, f"{domain} does not match entity name")

    def check_social(self, url: str | None) -> Verdict:
        """Strict: accept a social profile only if its handle IS the entity's legal name.

        The handle and the Brreg name are compared after removing case, spaces and
        punctuation, folding Norwegian letters (both ``å``->``a`` and ``å``->``aa``
        spellings), with the legal-form suffix optional. No fuzzy matching: a handle
        with anything extra or missing - ``telenornorge`` for "TELENOR ASA" (that is
        Telenor Norge AS), ``equinor`` for "EQUINOR ENERGY AS" - is refused, as is a
        handle matching a known parent's name. Recall is traded for precision.
        """
        try:
            path = urlparse(url if "//" in (url or "") else "//" + (url or "")).path
        except ValueError:
            return Verdict(False, "not a valid URL")
        segments = [s for s in path.split("/") if s]
        while segments and segments[0].lower() in _SOCIAL_PREFIX_SEGMENTS:
            segments.pop(0)
        if not segments:
            return Verdict(False, "no profile handle")
        handle = _compact(segments[0].lstrip("@"))
        if not handle:
            return Verdict(False, "no profile handle")

        for anc in self.ancestors:
            if handle in _name_handles(anc.navn):
                return Verdict(False, f"handle {handle!r} belongs to parent {anc.navn}")
        if handle in _name_handles(self.enhet.navn):
            return Verdict(True, "handle is the entity's exact name")
        return Verdict(False, f"handle {handle!r} is not the exact entity name")

    def needs_grounding(self, url: str | None) -> bool:
        """True for the entity's own Brreg-registered site when it is held back only
        because it looks like a group brand: it may be fetched, and :meth:`check`
        accepts it once a page of it names the entity (see ``content_verified``)."""
        domain = registrable_domain(url)
        return bool(
            domain
            and domain == self.registered_domain
            and domain not in self.ancestor_domains
            and domain not in self.content_rejected
            and domain not in self.content_verified
            and _is_shorter_brand_of(domain.split(".")[0], self.enhet.navn)
        )

    def may_fetch(self, url: str | None) -> bool:
        """Accepted, or the registered site awaiting content grounding."""
        return self.check(url).accepted or self.needs_grounding(url)

    @property
    def registered_fetch_url(self) -> str | None:
        """The Brreg ``hjemmeside`` as a URL if it may be fetched (see :meth:`may_fetch`)."""
        if self.registered_domain and self.may_fetch(self.enhet.hjemmeside):
            return _with_scheme(self.enhet.hjemmeside)
        return None

    @property
    def registered_site_url(self) -> str | None:
        """The Brreg ``hjemmeside`` as a URL, only if it validates for this entity."""
        host = self.registered_domain
        if host and self.check(self.enhet.hjemmeside).accepted:
            return _with_scheme(self.enhet.hjemmeside)
        return None

    # -- clean-up ----------------------------------------------------------

    def clean_intel(self, intel: dict) -> dict:
        """Drop facts attributed from a parent domain or to an unverified website.

        Fills ``website`` from the Brreg registry if it is empty and valid.
        Works on the ``intelligence_profile`` dict form (live or cached).
        """
        intel = copy.deepcopy(intel)
        for field in _SOURCED_FACT_FIELDS:
            fact = intel.get(field) or {}
            src = registrable_domain(fact.get("source_url"))
            if src and src in self.ancestor_domains:
                log.info("[%s] Dropped %s: sourced from parent domain %s",
                         self.enhet.organisasjonsnummer, field, src)
                intel[field] = {"fact_value": None, "source_url": None, "retrieval_date": None}

        # News must come from the entity's own validated domains.
        kept_news = []
        for item in intel.get("news") or []:
            verdict = self.check(item.get("url"))
            if verdict.accepted:
                kept_news.append(item)
            else:
                log.info("[%s] Dropped news %r: %s", self.enhet.organisasjonsnummer,
                         item.get("url"), verdict.reason)
        intel["news"] = kept_news

        # Page-grounded news / job postings: never from a parent's site, never from
        # a domain whose content failed grounding; news must be on a validated domain.
        for key, own_domain_only in (("company_news", True), ("job_postings", False)):
            kept = []
            for item in intel.get(key) or []:
                reason = self._activity_reject_reason(item, own_domain_only)
                if reason:
                    log.info("[%s] Dropped %s %r: %s", self.enhet.organisasjonsnummer,
                             key, item.get("url"), reason)
                else:
                    kept.append(item)
            if key in intel or kept:
                intel[key] = kept

        # Socials must not be a parent/group's accounts.
        socials = intel.get("socials") or {}
        for platform in SOCIAL_PLATFORMS:
            value = (socials.get(platform) or {}).get("fact_value")
            if isinstance(value, str) and value:
                verdict = self.check_social(value)
                if not verdict.accepted:
                    log.info("[%s] Dropped %s profile %s: %s", self.enhet.organisasjonsnummer,
                             platform, value, verdict.reason)
                    socials[platform] = {"fact_value": None, "source_url": None, "retrieval_date": None}
        if socials:
            intel["socials"] = socials

        # Careers/jobs: same domain rules as news - must be this entity's own site,
        # never a parent's (the classic parent/subsidiary jobs mix-up).
        careers = intel.get("careers") or {}
        cvalue = careers.get("fact_value")
        if isinstance(cvalue, str) and cvalue:
            src = registrable_domain(careers.get("source_url"))
            verdict = self.check(cvalue)
            if not verdict.accepted or (src and src in self.ancestor_domains):
                log.info("[%s] Dropped careers %s: %s", self.enhet.organisasjonsnummer,
                         cvalue, verdict.reason if not verdict.accepted else "sourced from parent domain")
                intel["careers"] = {"fact_value": None, "source_url": None, "retrieval_date": None}

        site = intel.get("website") or {}
        value = site.get("fact_value")
        if isinstance(value, str) and value and not self.check(value).accepted:
            log.info("[%s] Dropped website %s: %s", self.enhet.organisasjonsnummer,
                     value, self.check(value).reason)
            intel["website"] = {"fact_value": None, "source_url": None, "retrieval_date": None}
            value = None

        registered = self.registered_site_url
        if not value and registered:
            intel["website"] = {
                "fact_value": registered,
                "source_url": f"{BASE_URL}/{self.enhet.organisasjonsnummer}",
                "retrieval_date": str(date.today()),
            }
        return intel

    def _activity_reject_reason(self, item: dict, own_domain_only: bool) -> str | None:
        """Why a company_news / job_postings item may not be attributed, or None.

        The item must be linked from a page on a validated own domain. Its URL must
        be on a validated own domain too (news), or - for jobs hosted on an
        applicant-tracking system - at least not on a parent's / rejected domain."""
        source = self.check(item.get("source_url"))
        if not source.accepted:
            return f"source page rejected: {source.reason}"
        url = item.get("url")
        if own_domain_only:
            verdict = self.check(url)
            return None if verdict.accepted else verdict.reason
        domain = registrable_domain(url)
        if domain is None:
            return "not a valid URL"
        if domain in self.ancestor_domains:
            return f"{domain} belongs to a parent group"
        if domain in self.content_rejected:
            return CONTENT_MISMATCH_REASON
        if domain not in ATS_DOMAINS and not self.check(url).accepted:
            return f"{domain} is neither a validated own domain nor a known applicant-tracking system"
        return None

    def sanitize_profile(self, profile: CompanyProfile) -> CompanyProfile:
        data = self.clean_intel(profile.model_dump())
        return CompanyProfile.model_validate(data)

    def purge_cached(self, cached: dict | None) -> dict | None:
        """Remove previously cached facts that fail validation.

        Without this the diff engine would "preserve" a parent's website cached
        by an earlier run whenever the new extraction returns null.
        """
        if not cached or "intelligence_profile" not in cached:
            return cached
        cached = dict(cached)
        cached["intelligence_profile"] = self.clean_intel(cached["intelligence_profile"])
        return cached


def _with_scheme(url: str) -> str:
    url = url.strip()
    return url if "://" in url else "https://" + url
