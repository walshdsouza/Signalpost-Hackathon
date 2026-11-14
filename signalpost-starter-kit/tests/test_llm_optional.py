"""The LLM is optional: website grounding and the news/jobs crawl survive a missing or failing LLM."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extractor  # noqa: E402
from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from domain_validator import DomainValidator  # noqa: E402
from extractor import Page  # noqa: E402
from schema import CompanyNewsItem, JobPosting  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")
ENHET = Enhet(organisasjonsnummer="923177035", navn="102 ASIA MAT AS", organisasjonsform=FORM,
              hjemmeside="102asiamat.no")
HOME = Page("https://102asiamat.no", text="Velkommen", raw_text="102 Asia Mat AS, org.nr 923 177 035")
NEWS = CompanyNewsItem(title="102 Asia Mat åpner ny butikk i Drammen", url="https://102asiamat.no/nyheter/ny-butikk",
                       published_date="2026-10-01", date_type="published", date_evidence="Publisert 1. oktober 2026",
                       source_url=HOME.url, entity_evidence="102 Asia Mat AS", retrieval_date="2026-11-15")
JOB = JobPosting(title="Butikkmedarbeider", url="https://102asiamat.no/karriere/butikkmedarbeider-12",
                 source_url=HOME.url, entity_evidence="102 Asia Mat AS", retrieval_date="2026-11-15")


def _wire(monkeypatch, key):
    monkeypatch.setenv("GEMINI_API_KEY", key)
    monkeypatch.setattr(extractor, "_llm_disabled_reason", None)

    async def no_search(query, max_results=6):
        return []

    async def fetch(url):
        return HOME if url.rstrip("/") == HOME.url else Page(url)

    async def crawl(pages, validator):
        return [NEWS], [JOB]

    monkeypatch.setattr(extractor, "_ddg_search", no_search)
    monkeypatch.setattr(extractor, "_fetch_page", fetch)
    monkeypatch.setattr(extractor, "_fetch_page_once", fetch)
    monkeypatch.setattr(extractor, "_safe_crawl_site_activity", crawl)


def _run():
    return asyncio.run(extractor.extract_company_profile_with_status(ENHET, DomainValidator(ENHET)))


def test_no_key_still_returns_grounded_site_news_and_jobs(monkeypatch):
    _wire(monkeypatch, "")
    profile, issue = _run()
    assert issue == "GEMINI_API_KEY is not set"
    assert profile.website.fact_value == "https://102asiamat.no"
    assert [n.url for n in profile.company_news] == [NEWS.url]
    assert [j.url for j in profile.job_postings] == [JOB.url]
    assert profile.news == [] and profile.socials.facebook.fact_value is None  # LLM-only fields stay empty
    assert "GEMINI_API_KEY is not set" in profile.summary_of_changes


def test_llm_failure_keeps_crawl_results(monkeypatch):
    _wire(monkeypatch, "test-key")

    class Boom:
        class chat:
            class completions:
                @staticmethod
                async def create_with_completion(**kw):
                    raise RuntimeError("model overloaded")

    monkeypatch.setattr(extractor.instructor, "from_openai", lambda client: Boom)
    profile, issue = _run()
    assert issue and issue.startswith("LLM extraction failed (RuntimeError: model overloaded")
    assert [n.url for n in profile.company_news] == [NEWS.url]
    assert [j.url for j in profile.job_postings] == [JOB.url]
    assert profile.website.fact_value == "https://102asiamat.no"


def test_ungrounded_site_still_rejected_without_llm(monkeypatch):
    _wire(monkeypatch, "")

    async def fetch(url):
        return Page(url, text="Welcome to some other shop")  # names neither the entity nor its org nr

    monkeypatch.setattr(extractor, "_fetch_page", fetch)
    monkeypatch.setattr(extractor, "_fetch_page_once", fetch)
    profile, issue = _run()
    assert profile.website.fact_value is None
    assert profile.company_news == [] and profile.job_postings == []
