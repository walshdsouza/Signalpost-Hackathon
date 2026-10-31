"""Offline tests: careers field validation, retry/backoff, concurrency limit."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from openai import APIStatusError, AuthenticationError  # noqa: E402

import extractor  # noqa: E402
import main  # noqa: E402
from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from diff_engine import apply_diff_and_reconciliation  # noqa: E402
from domain_validator import DomainValidator  # noqa: E402
from extractor import Page, _build_profile  # noqa: E402
from schema import CompanyExtraction, validate_careers_url  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")
PARENT = Enhet(organisasjonsnummer="1", navn="AKER ASA", organisasjonsform=FORM, hjemmeside="www.akerasa.com")
SUB = Enhet(organisasjonsnummer="2", navn="AKER SOLUTIONS SUBSEA AS", organisasjonsform=FORM,
            hjemmeside="www.akersolutionssubsea.com", overordnetEnhet="1")
V = DomainValidator(SUB, [PARENT])

OWN_JOBS = "https://www.akersolutionssubsea.com/careers"
PARENT_JOBS = "https://www.akerasa.com/careers"
HOME = Page(
    url="https://www.akersolutionssubsea.com", text="Welcome",
    links=[("Careers", OWN_JOBS), ("Group careers", PARENT_JOBS),
           ("Jobs", "https://www.other-site.com/jobs")],
)


def _careers(url, pages=(HOME,)):
    return _build_profile(CompanyExtraction(careers_url=url), SUB, list(pages), V).careers


# ------------------------------------------------------------------ careers

def test_own_careers_page_accepted_with_source():
    c = _careers(OWN_JOBS)
    assert c.fact_value == OWN_JOBS and c.source_url == HOME.url


def test_parent_and_foreign_careers_rejected():
    assert _careers(PARENT_JOBS).fact_value is None
    assert _careers("https://www.other-site.com/jobs").fact_value is None


def test_hallucinated_or_non_careers_url_rejected():
    assert _careers("https://www.akersolutionssubsea.com/careers/made-up").fact_value is None
    assert _careers("https://www.akersolutionssubsea.com").fact_value is None
    try:
        validate_careers_url("https://www.akersolutionssubsea.com/about")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_sanitize_and_purge_drop_parent_careers():
    cached = {"intelligence_profile": {"careers": {
        "fact_value": PARENT_JOBS, "source_url": None, "retrieval_date": "2026-01-01"}}}
    assert V.purge_cached(cached)["intelligence_profile"]["careers"]["fact_value"] is None


def test_careers_link_is_shown_to_llm():
    shown = [h for _, h in extractor._select_links(HOME)]
    assert OWN_JOBS in shown


def test_diff_preserves_cached_careers():
    old = {"intelligence_profile": {"careers": {"fact_value": OWN_JOBS, "source_url": None, "retrieval_date": None}}}
    new = {"intelligence_profile": {"careers": {"fact_value": None, "source_url": None, "retrieval_date": None}}}
    merged, _ = apply_diff_and_reconciliation(old, new)
    assert merged["intelligence_profile"]["careers"]["fact_value"] == OWN_JOBS


# -------------------------------------------------------------------- retry

def _status_error(code):
    req = httpx.Request("POST", "https://x")
    return APIStatusError("boom", response=httpx.Response(code, request=req), body=None)


def test_retryable_classification():
    assert extractor._is_retryable_llm_error(_status_error(429))
    assert extractor._is_retryable_llm_error(_status_error(503))
    assert not extractor._is_retryable_llm_error(_status_error(400))
    req = httpx.Request("POST", "https://x")
    auth = AuthenticationError("bad", response=httpx.Response(401, request=req), body=None)
    assert not extractor._is_retryable_llm_error(auth)
    try:  # wrapped (as instructor does) still detected
        try:
            raise _status_error(429)
        except APIStatusError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as wrapped:
        assert extractor._is_retryable_llm_error(wrapped)


def test_retry_recovers_then_gives_up():
    extractor.BACKOFF_BASE = 0.001
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(429)
        return "ok"

    out = asyncio.run(extractor._retry_async(
        flaky, attempts=5, retryable=extractor._is_retryable_llm_error, what="t"))
    assert out == "ok" and calls["n"] == 3

    calls["n"] = 0

    async def always():
        calls["n"] += 1
        raise _status_error(429)

    try:
        asyncio.run(extractor._retry_async(
            always, attempts=4, retryable=extractor._is_retryable_llm_error, what="t"))
        raise AssertionError("expected error")
    except APIStatusError:
        assert calls["n"] == 4

    calls["n"] = 0

    async def fatal():
        calls["n"] += 1
        raise _status_error(400)

    try:
        asyncio.run(extractor._retry_async(
            fatal, attempts=4, retryable=extractor._is_retryable_llm_error, what="t"))
    except APIStatusError:
        assert calls["n"] == 1  # non-retryable: no retries


def test_fetch_page_retries_empty_then_succeeds():
    extractor.BACKOFF_BASE = 0.001
    seq = [Page("u"), Page("u"), Page("u", "hello")]
    orig = extractor._fetch_page_sync
    extractor._fetch_page_sync = lambda url: seq.pop(0)
    try:
        page = asyncio.run(extractor._fetch_page("https://x.no"))
    finally:
        extractor._fetch_page_sync = orig
    assert page.text == "hello" and not seq


# -------------------------------------------------------------- concurrency

def test_batch_concurrency_capped_at_five():
    assert main._CONCURRENCY == 5
    state = {"now": 0, "peak": 0}

    async def fake_enrich(orgnr):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return None, main.CompanyProfile(summary_of_changes="x"), "fake", None

    orig = (main._enrich, main.get_cached_profile, main.save_company_snapshot)
    main._enrich = fake_enrich
    main.get_cached_profile = lambda o: None
    main.save_company_snapshot = lambda *a, **k: None
    try:
        out = asyncio.run(main.run_batch([str(i) for i in range(20)]))
    finally:
        main._enrich, main.get_cached_profile, main.save_company_snapshot = orig
    assert len(out) == 21 and state["peak"] == 5  # 20 companies + _run_report


# --------------------------------------------------------------------- cost

def test_usage_cost_math_and_run_report():
    from types import SimpleNamespace as NS
    extractor.reset_usage()
    extractor.GEMINI_INPUT_USD_PER_M, extractor.GEMINI_OUTPUT_USD_PER_M = 0.10, 0.40
    extractor.record_usage("1", NS(usage=NS(prompt_tokens=10_000, completion_tokens=500)))
    extractor.record_usage("1", NS(usage=NS(prompt_tokens=2_000, completion_tokens=100)))
    extractor.record_usage("2", NS(usage=None))  # missing usage must not crash
    c1 = extractor.get_company_usage("1")
    assert (c1["llm_calls"], c1["prompt_tokens"], c1["completion_tokens"]) == (2, 12_000, 600)
    assert abs(c1["cost_usd"] - (12_000 * 0.10 + 600 * 0.40) / 1e6) < 1e-12
    assert extractor.get_total_usage()["llm_calls"] == 3
    assert extractor.get_company_usage("nope")["cost_usd"] == 0

    async def fake_enrich(orgnr):
        extractor.record_usage(orgnr, NS(usage=NS(prompt_tokens=1_000_000, completion_tokens=0)))
        return None, main.CompanyProfile(summary_of_changes="x"), "fake", None

    orig = (main._enrich, main.get_cached_profile, main.save_company_snapshot)
    main._enrich, main.get_cached_profile, main.save_company_snapshot = (
        fake_enrich, lambda o: None, lambda *a, **k: None)
    try:
        out = asyncio.run(main.run_batch(["a", "b"]))
    finally:
        main._enrich, main.get_cached_profile, main.save_company_snapshot = orig
    assert abs(out["_run_report"]["total_cost_usd"] - 0.20) < 1e-9
    assert abs(out["a"]["operations"]["cost_usd"] - 0.10) < 1e-9


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
