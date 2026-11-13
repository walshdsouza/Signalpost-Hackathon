"""Offline tests: off-site redirect rule, page-grounded company news and job postings.

Run with:  python tests/test_site_activity.py   (or pytest)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

import extractor  # noqa: E402
from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from diff_engine import apply_diff_and_reconciliation  # noqa: E402
from domain_validator import DomainValidator  # noqa: E402
from extractor import (  # noqa: E402
    ACTIVITY_MAX_FETCHES, Page, crawl_site_activity, ground_pages,
)
from schema import JobPosting  # noqa: E402

FORM = Organisasjonsform(kode="AS", beskrivelse="Aksjeselskap")
NAME = "NORDLYS BYGG AS"
ORGNR = "912345678"
SITE = "https://nordlysbygg.no"
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()
FOOTER = f"Nordlys Bygg AS · Org.nr. 912 345 678"


def validator(parent=None):
    enhet = Enhet(organisasjonsnummer=ORGNR, navn=NAME, organisasjonsform=FORM,
                  overordnetEnhet="1" if parent else None)
    return DomainValidator(enhet, [parent] if parent else [])


class FakeSite:
    """Offline stand-in for _fetch_page_once: url -> Page, records every fetch."""

    def __init__(self, *pages):
        self.pages = {p.url.rstrip("/"): p for p in pages}
        self.fetched = []

    async def __call__(self, url):
        self.fetched.append(url)
        return self.pages.get(url.rstrip("/"), Page(url))


def crawl(pages, v, site):
    return asyncio.run(crawl_site_activity(pages, v, fetch=site))


HOME = Page(f"{SITE}/", text="Vi bygger boliger", raw_text=FOOTER, links=[
    ("Nyheter", f"{SITE}/nyheter"),
    ("Karriere", f"{SITE}/karriere"),
])
NEWS_HUB = Page(f"{SITE}/nyheter", text="Nyheter", links=[
    ("Nordlys Bygg vinner kontrakt i Tromsø", f"{SITE}/nyheter/kontrakt-tromso"),
    ("Se alle nyheter", f"{SITE}/nyheter"),
])
ARTICLE = Page(f"{SITE}/nyheter/kontrakt-tromso", text="Vi har vunnet en stor kontrakt.",
               raw_text=FOOTER, headline="Nordlys Bygg vinner kontrakt i Tromsø", published="2026-03-12")
JOB_HUB = Page(f"{SITE}/karriere", text="Ledige stillinger", links=[
    ("Prosjektleder Tromsø", f"{SITE}/karriere/prosjektleder-tromso"),
    ("Tømrer", "https://nordlysbygg.teamtailor.com/jobs/123-tomrer"),
])
JOB = Page(f"{SITE}/karriere/prosjektleder-tromso", text="Søknadsfrist: snarest", raw_text=FOOTER,
           headline="Prosjektleder Tromsø",
           json_ld=[{"@type": "JobPosting", "title": "Prosjektleder", "datePosted": "2026-10-01",
                     "validThrough": f"{FUTURE}T23:59", "jobLocation": {"address": {"addressLocality": "Tromsø"}}}])
ATS_JOB = Page("https://nordlysbygg.teamtailor.com/jobs/123-tomrer", text="Apply now for Tømrer",
               raw_text="Tømrer - Nordlys Bygg AS", headline="Tømrer")


# --- off-site redirect rule ----------------------------------------------------

class _Resp:
    def __init__(self, url, html="<html><body><a href='/x'>x</a><p>Nordlys Bygg AS</p></body></html>"):
        self.url, self.html, self.data, self.status = url, html, html.encode(), 200


def _with_response(final_url, fn):
    orig = extractor.trafilatura_downloads.fetch_response
    calls = []

    def fake(url, **_):
        calls.append(url)
        return _Resp(final_url)

    extractor.trafilatura_downloads.fetch_response = fake
    try:
        return fn(), calls
    finally:
        extractor.trafilatura_downloads.fetch_response = orig


def test_offsite_redirect_page_is_dropped_unparsed():
    page, _ = _with_response("https://thirdparty.com/about",
                             lambda: extractor._fetch_page_sync(f"{SITE}/om-oss"))
    assert page.offsite_redirect == "https://thirdparty.com/about"
    assert not (page.text or page.links or page.raw_text)


def test_offsite_redirect_is_not_retried():
    page, calls = _with_response("https://thirdparty.com/",
                                 lambda: asyncio.run(extractor._fetch_page(f"{SITE}/")))
    assert page.offsite_redirect and len(calls) == 1


def test_same_domain_redirect_keeps_content():
    page, _ = _with_response("https://www.nordlysbygg.no/om-oss",
                             lambda: extractor._fetch_page_sync(f"{SITE}/om-oss"))
    assert page.offsite_redirect is None and "Nordlys Bygg AS" in page.raw_text


def test_relative_final_url_is_same_site():
    """Live regression: urllib3 can report the final URL as '/' (relative)."""
    page, _ = _with_response("/", lambda: extractor._fetch_page_sync(f"{SITE}/"))
    assert page.offsite_redirect is None and page.links


def test_nested_news_subhub_and_section_pages():
    """Live regression (veidekke.no): /aktuelt -> /aktuelt/nyheter-og-reportasjer -> article;
    section pages such as /aktuelt/pressebilder are not treated as articles."""
    home = Page(f"{SITE}/", text="Hei", raw_text=FOOTER, links=[("Aktuelt", f"{SITE}/aktuelt/")])
    aktuelt = Page(f"{SITE}/aktuelt/", text="Aktuelt", links=[
        ("Nyheter og reportasjer", f"{SITE}/aktuelt/nyheter-og-reportasjer/"),
        ("Pressebilder", f"{SITE}/aktuelt/pressebilder/")])
    sub = Page(f"{SITE}/aktuelt/nyheter-og-reportasjer/", text="Nyheter", links=[
        ("", f"{SITE}/aktuelt/nyheter-og-reportasjer/nordlys-bygger-ny-skole-i-alta/")])
    article = Page(f"{SITE}/aktuelt/nyheter-og-reportasjer/nordlys-bygger-ny-skole-i-alta/",
                   text="Skole", raw_text=FOOTER, headline="Nordlys bygger ny skole i Alta",
                   published="2026-09-01")
    site = FakeSite(aktuelt, sub, article)
    news, _ = crawl([home], validator(), site)
    assert [n.url for n in news] == [article.url]
    assert news[0].source_url == sub.url  # section pages are listings, never items


def test_offsite_redirect_cannot_ground_a_domain():
    """/om-oss redirecting to a third party that names the entity must not count."""
    v = validator()
    home = Page(f"{SITE}/", text="Hei", links=[("Om oss", f"{SITE}/om-oss")])
    site = FakeSite(Page(f"{SITE}/om-oss", offsite_redirect="https://thirdparty.com/x"))
    assert asyncio.run(ground_pages([home], v, fetch=site)) == []
    assert not v.check(SITE).accepted


# --- company news ---------------------------------------------------------------

def test_news_item_from_hub_grounded_on_its_own_page():
    news, _ = crawl([HOME], validator(), FakeSite(NEWS_HUB, ARTICLE, JOB_HUB))
    assert len(news) == 1
    item = news[0]
    assert item.url == ARTICLE.url and item.published_date == "2026-03-12"
    assert item.source_url == NEWS_HUB.url
    assert "912 345 678" in item.entity_evidence


def test_news_golden_rule_article_must_name_entity():
    article = Page(ARTICLE.url, text="Vi har vunnet en stor kontrakt.", headline=ARTICLE.headline,
                   published="2026-03-12", raw_text="Nordlys Bygg Gruppen")  # not the exact name
    news, _ = crawl([HOME], validator(), FakeSite(NEWS_HUB, article))
    assert news == []


def test_news_needs_a_publication_date_not_modified():
    article = Page(ARTICLE.url, text="Artikkel", raw_text=FOOTER + " Oppdatert 12.03.2026",
                   headline=ARTICLE.headline, modified="2026-03-12")
    news, _ = crawl([HOME], validator(), FakeSite(NEWS_HUB, article))
    assert news == []


def test_news_date_from_published_label_in_text():
    article = Page(ARTICLE.url, text="Artikkel", headline=ARTICLE.headline,
                   raw_text=f"Publisert 12. mars 2026 {FOOTER}")
    news, _ = crawl([HOME], validator(), FakeSite(NEWS_HUB, article))
    assert news[0].published_date == "2026-03-12"
    assert news[0].date_evidence == "Publisert 12. mars 2026"


def test_news_hub_probed_when_not_linked():
    home = Page(f"{SITE}/", text="Hei", raw_text=FOOTER, links=[("x", "https://other.no/")])
    site = FakeSite(NEWS_HUB, ARTICLE)
    news, _ = crawl([home], validator(), site)
    assert f"{SITE}/nyheter" in site.fetched and [n.url for n in news] == [ARTICLE.url]


def test_news_never_follows_other_domains():
    hub = Page(NEWS_HUB.url, text="Nyheter", links=[
        ("Konsernet vinner stor kontrakt i Bergen", "https://nordlysgruppen.no/nyheter/bergen")])
    site = FakeSite(hub)
    news, _ = crawl([HOME], validator(), site)
    assert news == [] and not any("nordlysgruppen" in u for u in site.fetched)


# --- job postings -------------------------------------------------------------------

def test_jobs_from_own_site_and_linked_ats():
    _, jobs = crawl([HOME], validator(), FakeSite(NEWS_HUB, ARTICLE, JOB_HUB, JOB, ATS_JOB))
    by_url = {j.url: j for j in jobs}
    own = by_url[JOB.url]
    assert own.title == "Prosjektleder" and own.valid_through == FUTURE
    assert own.location == "Tromsø" and own.source_url == JOB_HUB.url
    ats = by_url[ATS_JOB.url]
    assert ats.source_url == JOB_HUB.url and "Nordlys Bygg AS" in ats.entity_evidence


def test_job_golden_rule_posting_must_name_entity():
    ats_job = Page(ATS_JOB.url, text="Apply now for Tømrer", headline="Tømrer", raw_text="Some Other Firm AS")
    _, jobs = crawl([HOME], validator(), FakeSite(JOB_HUB, ats_job))
    assert ATS_JOB.url not in {j.url for j in jobs}


def test_closed_or_expired_postings_rejected():
    expired = Page(JOB.url, text="x", raw_text=FOOTER, headline="Prosjektleder",
                   json_ld=[{"@type": "JobPosting", "title": "Prosjektleder", "validThrough": PAST}])
    filled = Page(ATS_JOB.url, text="Apply now. Stillingen er besatt.", raw_text=FOOTER, headline="Tømrer")
    _, jobs = crawl([HOME], validator(), FakeSite(JOB_HUB, expired, filled))
    assert jobs == []


def test_job_page_without_posting_signals_rejected():
    page = Page(JOB.url, text="Vi er et godt sted å jobbe.", raw_text=FOOTER, headline="Livet hos oss")
    _, jobs = crawl([HOME], validator(), FakeSite(JOB_HUB, page))
    assert JOB.url not in {j.url for j in jobs}


def test_ats_board_only_follows_its_own_host():
    home = Page(f"{SITE}/", text="Hei", raw_text=FOOTER,
                links=[("Ledige stillinger", "https://nordlysbygg.teamtailor.com/jobs")])
    board = Page("https://nordlysbygg.teamtailor.com/jobs", text="Jobs", links=[
        ("Elektriker", "https://nordlysbygg.teamtailor.com/jobs/7-elektriker"),
        ("Other company job", "https://othercorp.teamtailor.com/jobs/9-sjafor")])
    posting = Page("https://nordlysbygg.teamtailor.com/jobs/7-elektriker", text="Apply now",
                   raw_text="Elektriker hos Nordlys Bygg AS", headline="Elektriker")
    site = FakeSite(board, posting)
    _, jobs = crawl([home], validator(), site)
    assert [j.url for j in jobs] == [posting.url] and jobs[0].source_url == home.url
    assert not any("othercorp" in u for u in site.fetched)


def test_job_posting_schema_rules():
    base = dict(url=JOB.url, source_url=SITE, entity_evidence=FOOTER)
    for bad_title in ("Ledige stillinger", "Careers", "Apply now", "Les mer"):
        try:
            JobPosting(title=bad_title, **base)
            raise AssertionError(f"accepted {bad_title!r}")
        except ValidationError:
            pass
    try:
        JobPosting(title="Prosjektleder", valid_through=PAST, **base)
        raise AssertionError("accepted expired posting")
    except ValidationError:
        pass
    assert JobPosting(title="Prosjektleder", valid_through=FUTURE, **base)


# --- bounds, attribution clean-up, diff ---------------------------------------------

def test_crawl_is_bounded():
    links = [(f"Nyheter {i}", f"{SITE}/nyheter-{i}") for i in range(10)]
    links += [(f"Nordlys Bygg nyhet nummer {i}", f"{SITE}/2026/sak-{i}") for i in range(30)]
    links += [(f"Stilling nummer {i}", f"{SITE}/karriere/stilling-{i}") for i in range(30)]
    home = Page(f"{SITE}/", text="Hei", raw_text=FOOTER, links=links)
    site = FakeSite()
    crawl([home], validator(), site)
    assert len(site.fetched) <= ACTIVITY_MAX_FETCHES
    assert len(site.fetched) == len(set(site.fetched))  # nothing fetched twice


def test_nothing_crawled_for_ungrounded_site():
    v = validator()
    v.verify_content(SITE, None)  # domain failed grounding
    site = FakeSite(NEWS_HUB, ARTICLE)
    assert crawl([HOME], v, site) == ([], []) and site.fetched == []


def test_clean_intel_drops_parent_and_unvalidated_items():
    parent = Enhet(organisasjonsnummer="1", navn="NORDLYS GRUPPEN AS", organisasjonsform=FORM,
                   hjemmeside="nordlysgruppen.no")
    v = validator(parent)
    item = {"title": "x", "url": f"{SITE}/nyheter/a", "source_url": SITE}
    intel = {
        "company_news": [item, {**item, "url": "https://nordlysgruppen.no/nyheter/a"}],
        "job_postings": [
            {"url": "https://nordlysgruppen.no/jobb/1", "source_url": SITE},
            {"url": "https://randomjobs.com/1", "source_url": SITE},
            {"url": "https://nordlysbygg.teamtailor.com/jobs/1", "source_url": SITE},
            {"url": f"{SITE}/karriere/1", "source_url": "https://nordlysgruppen.no/karriere"},
        ],
    }
    out = v.clean_intel(intel)
    assert [n["url"] for n in out["company_news"]] == [f"{SITE}/nyheter/a"]
    assert [j["url"] for j in out["job_postings"]] == ["https://nordlysbygg.teamtailor.com/jobs/1"]


def test_entity_mention_agrees_with_text_names_entity():
    v = validator()
    for text in (FOOTER, "nordlys bygg as", "orgnr 912345678", "Nordlys Bygg", "1912345678", "", None):
        assert (v.entity_mention(text) is not None) == v.text_names_entity(text), text


def test_diff_accumulates_company_news_but_not_jobs():
    old = {"intelligence_profile": {
        "company_news": [{"url": "a", "published_date": "2026-01-01"}],
        "job_postings": [{"url": "old-job"}]}}
    new = {"intelligence_profile": {
        "company_news": [{"url": "b", "published_date": "2026-02-01"}],
        "job_postings": [{"url": "new-job"}]}}
    merged, summary = apply_diff_and_reconciliation(old, new)
    intel = merged["intelligence_profile"]
    assert [n["url"] for n in intel["company_news"]] == ["b", "a"]
    assert [j["url"] for j in intel["job_postings"]] == ["new-job"]
    assert "1 new company news item" in summary and "1 new job posting" in summary


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
