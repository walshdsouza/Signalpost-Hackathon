"""Offline tests for strict news/social extraction rules.

Run with:  python tests/test_news_extraction.py   (or pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from brreg_client import Enhet, Organisasjonsform  # noqa: E402
from diff_engine import apply_diff_and_reconciliation  # noqa: E402
from domain_validator import DomainValidator  # noqa: E402
from extractor import Page, _build_profile, _extract_page_dates, _select_links  # noqa: E402
from schema import (  # noqa: E402
    CompanyExtraction, NewsCandidate, NewsItem, SocialCandidate, SourcedFact,
    normalise_social_url,
)

GOOD = dict(
    title="Aker Solutions wins major subsea contract offshore Norway",
    url="https://www.akersolutions.com/news/subsea-contract-2026",
    published_date="2026-03-10",
    date_type="published",
    date_evidence="Published 10 March 2026",
)


def _rejects(**overrides):
    try:
        NewsItem(**{**GOOD, **overrides})
    except ValidationError:
        return True
    return False


# ---------------------------------------------------------------- schema

def test_valid_item_accepted():
    assert NewsItem(**GOOD).date_type == "published"


def test_ui_labels_rejected():
    for label in ["See more news", "Read more", "Read more »", "Load more", "Newsroom",
                  "All news", "Les mer", "Se alle nyheter", "View all press releases", "Nyheter"]:
        assert _rejects(title=label), label


def test_real_headlines_not_mistaken_for_labels():
    for title in ["Read the full report: Aker Solutions record year",
                  "Aker Solutions sees more demand for subsea services"]:
        assert not _rejects(title=title), title


def test_modified_or_updated_dates_rejected():
    assert _rejects(date_type="modified")
    assert _rejects(date_type="Last updated")
    assert _rejects(date_type="updated")
    assert _rejects(date_type="unknown")
    assert _rejects(date_evidence="Last updated 10 March 2026")
    assert _rejects(date_evidence="Oppdatert 10. mars 2026")
    assert _rejects(date_evidence="Sist endret 10.03.2026")


def test_date_must_be_real_past_date():
    assert _rejects(published_date="")
    assert _rejects(published_date="10 March 2026")
    assert _rejects(published_date="2026-02-30")
    assert _rejects(published_date="2999-01-01")
    assert _rejects(published_date="1980-01-01")
    assert _rejects(date_evidence="")


def test_listing_and_non_article_urls_rejected():
    for url in ["https://www.akersolutions.com/", "https://www.akersolutions.com/news",
                "https://www.akersolutions.com/nyheter/", "https://www.akersolutions.com/press",
                "https://www.akersolutions.com/products/subsea", "not a url"]:
        assert _rejects(url=url), url


def test_socials_must_be_profiles():
    assert normalise_social_url("linkedin", "https://no.linkedin.com/company/aker-solutions?trk=x") \
        == "https://www.linkedin.com/company/aker-solutions"
    assert normalise_social_url("twitter", "https://twitter.com/AkerSolutions/status/1") \
        == "https://twitter.com/AkerSolutions"
    for platform, url in [("linkedin", "https://www.linkedin.com/in/jane-doe"),
                          ("facebook", "https://www.facebook.com/sharer/sharer.php?u=x"),
                          ("x", "https://twitter.com/intent/tweet?url=x"),
                          ("instagram", "https://instagram.com/"),
                          ("facebook", "https://example.com/akersolutions")]:
        try:
            normalise_social_url(platform, url)
        except ValueError:
            continue
        raise AssertionError(f"accepted {platform} {url}")


# ------------------------------------------------- extraction / grounding

FORM = Organisasjonsform(kode="ASA", beskrivelse="Allmennaksjeselskap")
AKER = Enhet(organisasjonsnummer="913748100", navn="AKER SOLUTIONS ASA", organisasjonsform=FORM,
             hjemmeside="www.akersolutions.com")
VALIDATOR = DomainValidator(AKER)

ARTICLE = "https://www.akersolutions.com/news/subsea-contract-2026"
HOME = Page(
    url="https://www.akersolutions.com",
    text=("Latest news\nAker Solutions wins major subsea contract offshore Norway\n"
          "Published 10 March 2026\nSee more news\n"
          "Aker Solutions starts new yard expansion\nLast updated 5 May 2026"),
    links=[
        ("Aker Solutions wins major subsea contract offshore Norway", ARTICLE),
        ("Aker Solutions starts new yard expansion", "https://www.akersolutions.com/news/yard-expansion"),
        ("See more news", "https://www.akersolutions.com/news"),
        ("LinkedIn", "https://www.linkedin.com/company/aker-solutions"),
        ("Share", "https://www.facebook.com/sharer/sharer.php?u=x"),
        ("Group", "https://www.linkedin.com/company/aker"),
    ],
)


def _profile(news=(), socials=(), pages=(HOME,)):
    ext = CompanyExtraction(news_candidates=list(news), social_candidates=list(socials))
    return _build_profile(ext, AKER, list(pages), VALIDATOR)


def cand(**kw):
    return NewsCandidate(**{**GOOD, **kw})


def test_valid_news_kept_with_source():
    p = _profile(news=[cand()])
    assert [n.url for n in p.news] == [ARTICLE]
    assert p.news[0].source_url == HOME.url


def test_llm_ui_label_and_modified_candidates_dropped():
    p = _profile(news=[
        cand(title="See more news", url="https://www.akersolutions.com/news"),
        cand(title="Aker Solutions starts new yard expansion",
             url="https://www.akersolutions.com/news/yard-expansion",
             published_date="2026-05-05", date_type="published",
             date_evidence="Last updated 5 May 2026"),
        cand(title="Aker Solutions starts new yard expansion",
             url="https://www.akersolutions.com/news/yard-expansion",
             published_date="2026-05-05", date_type="modified", date_evidence="5 May 2026"),
    ])
    assert p.news == []


def test_hallucinated_url_title_or_evidence_dropped():
    assert _profile(news=[cand(url="https://www.akersolutions.com/news/made-up-story")]).news == []
    assert _profile(news=[cand(title="Aker Solutions announces record profits this quarter")]).news == []
    assert _profile(news=[cand(date_evidence="Published 11 March 2026")]).news == []


def test_news_card_only_present_in_link_anchor_is_grounded():
    page = Page(
        url="https://www.akersolutions.com", text="Welcome",
        links=[("10 March 2026" + GOOD["title"] + "A summary sentence", ARTICLE)],
    )
    p = _profile(news=[cand(date_evidence="10 March 2026")], pages=(page,))
    assert [n.url for n in p.news] == [ARTICLE]


def test_news_on_foreign_domain_dropped():
    page = Page(url="https://www.akersolutions.com", text=HOME.text,
                links=HOME.links + [("Aker Solutions wins major subsea contract offshore Norway",
                                     "https://www.other-site.com/news/x-1")])
    p = _profile(news=[cand(url="https://www.other-site.com/news/x-1")], pages=(page,))
    assert p.news == []


def test_article_page_metadata_overrides_and_checks_dates():
    art = Page(url=ARTICLE, text="Published 10 March 2026 " + GOOD["title"],
               links=[], published="2026-03-09", modified="2026-03-10")
    # LLM picked the modified date -> rejected
    assert _profile(news=[cand()], pages=(HOME, art)).news == []
    art_ok = Page(url=ARTICLE, text=art.text, published="2026-03-10", modified="2026-04-01")
    assert len(_profile(news=[cand()], pages=(HOME, art_ok)).news) == 1
    only_mod = Page(url=ARTICLE, text=art.text, published=None, modified="2026-03-10")
    assert _profile(news=[cand()], pages=(HOME, only_mod)).news == []


def test_socials_validated_against_links_and_group_brand():
    p = _profile(socials=[
        SocialCandidate(platform="linkedin", url="https://www.linkedin.com/company/aker-solutions"),
        SocialCandidate(platform="facebook", url="https://www.facebook.com/sharer/sharer.php?u=x"),
        SocialCandidate(platform="instagram", url="https://www.instagram.com/akersolutions"),  # not linked
    ])
    assert p.socials.linkedin.fact_value == "https://www.linkedin.com/company/aker-solutions"
    assert p.socials.linkedin.source_url == HOME.url
    assert p.socials.facebook.fact_value is None
    assert p.socials.instagram.fact_value is None


def test_subsidiary_does_not_get_group_linkedin():
    sub = Enhet(organisasjonsnummer="2", navn="AKER SOLUTIONS SUBSEA AS", organisasjonsform=FORM)
    v = DomainValidator(sub)
    assert not v.check_social("https://www.linkedin.com/company/aker-solutions").accepted
    assert v.check_social("https://www.linkedin.com/company/aker-solutions-subsea").accepted


def test_validator_drops_parent_news_and_socials_from_cache():
    parent = Enhet(organisasjonsnummer="1", navn="AKER ASA", organisasjonsform=FORM,
                   hjemmeside="www.akerasa.com")
    sub = Enhet(organisasjonsnummer="2", navn="AKER SOLUTIONS SUBSEA AS", organisasjonsform=FORM,
                overordnetEnhet="1")
    v = DomainValidator(sub, [parent])
    cached = {"intelligence_profile": {
        "news": [dict(GOOD, url="https://www.akerasa.com/news/x-1")],
        "socials": {"linkedin": {"fact_value": "https://www.linkedin.com/company/aker-asa",
                                 "source_url": None, "retrieval_date": None}},
    }}
    out = v.purge_cached(cached)["intelligence_profile"]
    assert out["news"] == []
    assert out["socials"]["linkedin"]["fact_value"] is None


def test_page_date_metadata_keeps_published_and_modified_apart():
    html = ('<meta property="article:published_time" content="2026-03-09T08:00:00Z">'
            '<meta property="article:modified_time" content="2026-04-01T10:00:00Z">')
    assert _extract_page_dates(html) == ("2026-03-09", "2026-04-01")
    ld = '<script type="application/ld+json">{"dateModified":"2026-05-01","datePublished":"2026-02-02"}</script>'
    assert _extract_page_dates(ld) == ("2026-02-02", "2026-05-01")


def test_link_selection_prefers_socials_and_articles():
    shown = [href for _, href in _select_links(HOME)]
    assert "https://www.linkedin.com/company/aker-solutions" in shown
    assert ARTICLE in shown


# ------------------------------------------------------------------ diff

def _rec(news, linkedin=None):
    return {"intelligence_profile": {
        "website": {}, "ceo_name": {}, "employee_count": {}, "latest_revenue_nok": {},
        "news": news,
        "socials": {"linkedin": {"fact_value": linkedin, "source_url": None, "retrieval_date": None}},
    }}


def test_diff_accumulates_news_and_preserves_socials():
    old = _rec([dict(GOOD, url="https://x.no/a", published_date="2026-01-01")], linkedin="https://www.linkedin.com/company/x")
    new = _rec([dict(GOOD, url="https://x.no/b", published_date="2026-02-01")])
    merged, summary = apply_diff_and_reconciliation(old, new)
    urls = [n["url"] for n in merged["intelligence_profile"]["news"]]
    assert urls == ["https://x.no/b", "https://x.no/a"]  # newest first, old kept
    assert merged["intelligence_profile"]["socials"]["linkedin"]["fact_value"].endswith("/company/x")
    assert "Discovered 1 new news item" in summary


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
