"""
Diff Engine: Detects changes across runs, generates explanations,
and preserves verified cached facts when new extractions return nulls.
"""

from __future__ import annotations

from typing import Any

from schema import SOCIAL_PLATFORMS

TRACKED_FACT_FIELDS = [
    "website",
    "ceo_name",
    "employee_count",
    "latest_revenue_nok",
    "careers",
]

MAX_TRACKED_NEWS = 20


def _diff_fact(label: str, old_fact: dict, new_fact: dict) -> tuple[dict, str | None]:
    """Compare one SourcedFact; return (fact to keep, change description)."""
    old_val = old_fact.get("fact_value")
    new_val = new_fact.get("fact_value")

    # 1. New value obtained where previously empty
    if old_val is None and new_val is not None:
        return new_fact, f"Discovered {label}: set to '{new_val}'."

    # 2. Value changed between runs
    if old_val is not None and new_val is not None and old_val != new_val:
        return new_fact, f"Updated {label}: changed from '{old_val}' to '{new_val}'."

    # 3. Resilience fallback: API returned null, but cache has verified data
    if old_val is not None and new_val is None:
        return old_fact, f"Preserved cached {label} ('{old_val}') due to unverified/null new extraction."

    return new_fact, None


def _merge_news(old_items: list[dict], new_items: list[dict]) -> tuple[list[dict], int]:
    """Append newly found articles to the cached ones, keyed by URL.

    Items seen before are kept (news does not disappear because one run missed
    it); a re-found item takes the fresh copy. Returns (merged, number_added).
    """
    merged = {item["url"]: item for item in old_items if item.get("url")}
    added = 0
    for item in new_items:
        if not item.get("url"):
            continue
        if item["url"] not in merged:
            added += 1
        merged[item["url"]] = item
    ordered = sorted(merged.values(), key=lambda i: i.get("published_date", ""), reverse=True)
    return ordered[:MAX_TRACKED_NEWS], added


def apply_diff_and_reconciliation(
    cached_profile: dict | None,
    new_profile: dict,
) -> tuple[dict, str]:
    """Compare new extraction against cached state and construct change explanation.

    Returns:
        tuple[reconciled_profile, explanation_summary]
    """
    if not cached_profile:
        return new_profile, "Initial baseline record created. No prior snapshot available."

    cached_intel = cached_profile.get("intelligence_profile", {})
    new_intel = new_profile.get("intelligence_profile", {})

    changes: list[str] = []
    reconciled_intel = dict(new_intel)

    for field in TRACKED_FACT_FIELDS:
        fact, change = _diff_fact(field, cached_intel.get(field) or {}, new_intel.get(field) or {})
        reconciled_intel[field] = fact
        if change:
            changes.append(change)

    # Social profiles: same rules as facts, one per platform.
    old_socials = cached_intel.get("socials") or {}
    new_socials = new_intel.get("socials") or {}
    reconciled_socials = dict(new_socials)
    for platform in SOCIAL_PLATFORMS:
        fact, change = _diff_fact(
            f"{platform} profile", old_socials.get(platform) or {}, new_socials.get(platform) or {}
        )
        reconciled_socials[platform] = fact
        if change:
            changes.append(change)
    reconciled_intel["socials"] = reconciled_socials

    # News: accumulate by URL.
    merged_news, added = _merge_news(cached_intel.get("news") or [], new_intel.get("news") or [])
    reconciled_intel["news"] = merged_news
    if added:
        changes.append(f"Discovered {added} new news item{'s' if added != 1 else ''}.")

    if not changes:
        summary = "All fields match existing cache. Zero changes detected."
    else:
        summary = " ".join(changes)

    reconciled_intel["summary_of_changes"] = summary
    reconciled_profile = dict(new_profile)
    reconciled_profile["intelligence_profile"] = reconciled_intel

    # Registry lookup failed this run (placeholder metadata): keep the last
    # known registry data rather than overwriting it with an empty stub.
    new_meta = new_profile.get("brreg_metadata") or {}
    cached_meta = cached_profile.get("brreg_metadata") or {}
    if not new_meta.get("navn") and cached_meta.get("navn"):
        reconciled_profile["brreg_metadata"] = cached_meta
        reconciled_profile["cross_check"] = cached_profile.get(
            "cross_check", new_profile.get("cross_check")
        )
        changes.append("Preserved cached Brreg metadata because the registry lookup failed.")
        summary = " ".join(changes)
        reconciled_intel["summary_of_changes"] = summary

    return reconciled_profile, summary
