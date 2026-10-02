"""
Entity matcher — fuzzy company name comparison using rapidfuzz.
"""

from __future__ import annotations

import re

from rapidfuzz.fuzz import token_sort_ratio

# ---------------------------------------------------------------------------
# Corporate suffix stripping
# ---------------------------------------------------------------------------

# Suffixes are matched as whole words at the end of the string (case-insensitive).
# Extend this list as needed for your target markets.
_SUFFIXES: list[str] = [
    "allmennaksjeselskap",
    "aksjeselskap",
    "enkeltpersonforetak",
    "ansvarlig selskap",
    "asa",
    "as",
    "da",
    "enk",
    "sa",
    "ltd",
    "llc",
    "inc",
    "plc",
    "gmbh",
    "ag",
    "bv",
    "nv",
    "ab",
    "oy",
]

# Build a single compiled pattern: word-boundary + suffix + end-of-string.
# Sorted by length (longest first) so "asa" doesn't shadow "aksjeselskap".
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in sorted(_SUFFIXES, key=len, reverse=True)) + r")\s*$",
    re.IGNORECASE,
)


def _normalise(name: str) -> str:
    """Lowercase, strip corporate suffixes, and collapse whitespace."""
    name = name.lower().strip()
    # Remove suffix repeatedly in case of stacked suffixes (e.g. "Acme AS ASA")
    while True:
        cleaned = _SUFFIX_RE.sub("", name).strip().rstrip(",").strip()
        if cleaned == name:
            break
        name = cleaned
    # Collapse internal whitespace
    return re.sub(r"\s+", " ", name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MATCH_THRESHOLD = 95


def is_same_company(scraped_name: str, official_name: str, threshold: int = MATCH_THRESHOLD) -> bool:
    """Compare a scraped company name against an official registry name.

    Both names are normalised (lowercased, corporate suffixes removed) before
    comparison so that e.g. ``"Equinor AS"`` and ``"EQUINOR ASA"`` still match.

    Args:
        scraped_name:  Company name as found in the scraped source.
        official_name: Canonical company name from the official registry (e.g. Brreg).
        threshold:     Minimum ``token_sort_ratio`` score (0-100) to consider a
                       match. Defaults to :data:`MATCH_THRESHOLD` (95).

    Returns:
        ``True`` if the fuzzy similarity score is >= *threshold*, ``False`` otherwise.
    """
    a = _normalise(scraped_name)
    b = _normalise(official_name)
    score = token_sort_ratio(a, b)
    return score >= threshold


# ---------------------------------------------------------------------------
# Quick smoke-test  (run with:  python entity_matcher.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases: list[tuple[str, str, bool]] = [
        # Should match
        ("Equinor AS",        "EQUINOR ASA",            True),
        ("equinor",           "Equinor ASA",             True),
        ("Aker Solutions ASA", "Aker Solutions AS",      True),
        ("DNB Bank  ASA",     "DNB Bank ASA",            True),
        # Should NOT match
        ("Equinor AS",        "Aker BP ASA",             False),
        ("Telenor",           "Telia Company AB",        False),
    ]

    header = f"{'Scraped':<25} {'Official':<25} {'Expected':>8} {'Got':>5} {'Pass':>5}"
    print(header)
    print("-" * len(header))

    all_pass = True
    for scraped, official, expected in cases:
        result = is_same_company(scraped, official)
        ok = result == expected
        all_pass = all_pass and ok
        print(f"{scraped:<25} {official:<25} {str(expected):>8} {str(result):>5} {'OK' if ok else 'FAIL':>5}")

    print()
    print("All tests passed." if all_pass else "Some tests FAILED.")
