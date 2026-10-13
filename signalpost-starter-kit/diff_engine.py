"""
Diff Engine: Detects changes across runs, generates explanations,
and preserves verified cached facts when new extractions return nulls.
"""

from __future__ import annotations

from typing import Any


TRACKED_FACT_FIELDS = [
    "website",
    "ceo_name",
    "employee_count",
    "latest_revenue_nok",
]


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
        old_fact = cached_intel.get(field) or {}
        new_fact = new_intel.get(field) or {}

        old_val = old_fact.get("fact_value")
        new_val = new_fact.get("fact_value")

        # 1. New value obtained where previously empty
        if old_val is None and new_val is not None:
            changes.append(f"Discovered {field}: set to '{new_val}'.")

        # 2. Value changed between runs
        elif old_val is not None and new_val is not None and old_val != new_val:
            changes.append(f"Updated {field}: changed from '{old_val}' to '{new_val}'.")

        # 3. Resilience fallback: API returned null, but cache has verified data
        elif old_val is not None and new_val is None:
            reconciled_intel[field] = old_fact
            changes.append(f"Preserved cached {field} ('{old_val}') due to unverified/null new extraction.")

    if not changes:
        summary = "All fields match existing cache. Zero changes detected."
    else:
        summary = " ".join(changes)

    reconciled_intel["summary_of_changes"] = summary
    reconciled_profile = dict(new_profile)
    reconciled_profile["intelligence_profile"] = reconciled_intel

    return reconciled_profile, summary