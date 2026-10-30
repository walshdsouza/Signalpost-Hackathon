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
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

from brreg_client import BASE_URL, Enhet, lookup_enhet
from entity_matcher import is_same_company, normalise_name
from schema import CompanyProfile

log = logging.getLogger(__name__)

MAX_ANCESTOR_DEPTH = 3

_SOURCED_FACT_FIELDS = ("website", "ceo_name", "employee_count", "latest_revenue_nok")
_TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "com.au", "co.nz", "com.br"}
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

    def check(self, url: str | None) -> Verdict:
        domain = registrable_domain(url)
        if domain is None:
            return Verdict(False, "not a valid URL")
        if domain in self.ancestor_domains:
            return Verdict(False, f"{domain} belongs to a parent group")

        label = domain.split(".")[0]
        if _label_matches_name(label, self.enhet.navn):
            return Verdict(True, "domain matches entity name")

        if domain == self.registered_domain:
            if _is_shorter_brand_of(label, self.enhet.navn):
                return Verdict(False, f"registered site {domain} looks like a parent/group brand")
            return Verdict(True, "listed as hjemmeside in Brreg")

        return Verdict(False, f"{domain} does not match entity name")

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
