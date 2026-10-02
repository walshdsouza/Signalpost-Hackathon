"""
Pydantic schema for a Norwegian company intelligence profile.

Each data point is wrapped in a SourcedFact so that the origin URL and
retrieval date travel alongside the value — making provenance first-class.
"""

from __future__ import annotations

from datetime import date
from typing import Union

from pydantic import BaseModel, Field


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
# Main profile model
# ---------------------------------------------------------------------------

class CompanyProfile(BaseModel):
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
    """

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
