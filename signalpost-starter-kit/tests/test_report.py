"""HTML report: escaping, link safety, provenance badges, robustness."""

import json
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import report  # noqa: E402

EMPTY_FACT = {"fact_value": None, "source_url": None, "retrieval_date": None}


def fact(value, src, when="2026-11-15"):
    return {"fact_value": value, "source_url": src, "retrieval_date": when}


def record(**profile):
    base = {k: EMPTY_FACT for k in ("website", "ceo_name", "employee_count", "latest_revenue_nok", "careers")}
    base.update(summary_of_changes="", news=[], company_news=[], job_postings=[],
                socials={k: EMPTY_FACT for k in ("linkedin", "facebook", "instagram", "x", "youtube", "tiktok")})
    base.update(profile)
    return {
        "orgnr": "982463718", "status": "complete", "fallback_reason": None,
        "brreg_metadata": {"navn": "TELENOR ASA", "organisasjonsnummer": "982463718",
                           "organisasjonsform": {"kode": "ASA", "beskrivelse": "x"},
                           "naeringskode1": {"kode": "1", "beskrivelse": "Telekom"},
                           "forretningsadresse": {"adresse": ["Snarøyveien 30"], "postnummer": "1360",
                                                  "poststed": "FORNEBU"}},
        "intelligence_profile": base,
    }


class _Checker(HTMLParser):
    """Collects hrefs and checks that every opened tag that needs closing is closed."""
    VOID = {"meta", "input", "br", "img", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.hrefs, self.scripts = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
        if tag == "script":
            self.scripts += 1
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}>, open: {self.stack[-3:]}"
        self.stack.pop()


def parse(html_text):
    c = _Checker()
    c.feed(html_text)
    c.close()
    assert c.stack == [], f"unclosed tags: {c.stack}"
    return c


def test_full_record_renders_with_badges_and_values():
    rec = record(
        website=fact("https://www.telenor.no/", "https://www.telenor.no/"),
        ceo_name=fact("Benedicte Schilbred Fasmer", "https://data.brreg.no/enhetsregisteret/api/enheter/982463718/roller"),
        employee_count=fact(369, "https://data.brreg.no/enhetsregisteret/api/enheter/982463718"),
        latest_revenue_nok=fact(355000000, "https://data.brreg.no/regnskapsregisteret/regnskap/982463718"),
        company_news=[{"title": "Telenor Q3", "url": "https://www.telenor.no/news/q3", "published_date": "2026-10-20",
                       "entity_evidence": "© Telenor ASA"}],
        job_postings=[{"title": "Engineer", "url": "https://www.telenor.no/jobb/1", "valid_through": "2026-12-01",
                       "location": "Fornebu"}],
    )
    out = report.render_report({"982463718": rec, "_run_report": {"runtime_seconds": 3.2}})
    c = parse(out)
    assert "Benedicte Schilbred Fasmer" in out and "NOK 355.0 m" in out and "369" in out
    assert out.count('class="badge official"') == 3 + 1  # three Brreg facts + legend
    assert "Telenor Q3" in out and "names “Telenor ASA”" in out and "apply by 2026-12-01" in out
    assert "https://virksomhet.brreg.no/nb/oppslag/enheter/982463718" in c.hrefs
    assert c.scripts == 1


def test_scraped_text_is_escaped_and_unsafe_links_not_rendered():
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    rec = record(
        ceo_name=fact(evil, "javascript:alert(3)"),
        company_news=[{"title": evil, "url": "javascript:alert(4)", "published_date": "2026-01-01",
                       "entity_evidence": evil}],
        socials={"facebook": fact("data:text/html,<b>x</b>", "https://x.no")},
    )
    out = report.render_report({"982463718": rec})
    c = parse(out)
    assert "<script>alert(1)" not in out and "onerror=alert(2)>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert not any(h and not h.startswith(("http://", "https://")) for h in c.hrefs)
    assert c.scripts == 1  # only the report's own script


def test_news_deduplicated_and_sorted_newest_first():
    shared = {"title": "Shared", "url": "https://a.no/n/1", "published_date": "2026-01-01"}
    rec = record(company_news=[shared],
                 news=[dict(shared), {"title": "Newer", "url": "https://a.no/n/2", "published_date": "2026-05-01"}])
    out = report.render_report({"1": rec})
    assert out.count(">Shared</a>") == 1
    assert out.index("Newer") < out.index("Shared")


def test_empty_fallback_and_missing_fields_do_not_crash():
    bare = {"orgnr": "1", "status": "fallback", "fallback_reason": "Brreg lookup failed",
            "brreg_metadata": {"navn": None, "organisasjonsnummer": "1"}, "intelligence_profile": {}}
    out = report.render_report({"1": bare, "_run_report": {}})
    parse(out)
    assert "Partial profile: Brreg lookup failed" in out and "1 partial" in out
    parse(report.render_report({}))  # no companies at all


def test_coverage_counts():
    rec = record(ceo_name=fact("A B", "https://data.brreg.no/x"))
    out = report.render_report({"1": rec, "2": record()})
    assert '<span class="bl">CEO</span>' in out and "1 <span class=\"muted\">(50%)</span>" in out


def test_formatting():
    assert report.fmt_nok(355000000).startswith("NOK 355.0 m")
    assert report.fmt_nok(1645874).startswith("NOK 1.6 m")
    assert report.fmt_nok(0).startswith("NOK 0")
    assert report.fmt_int(21182) == "21\u202f182"
    assert report.safe_url(" https://a.no/x ") == "https://a.no/x"
    assert report.safe_url("JavaScript:alert(1)") is None and report.safe_url("//evil.com") is None


def test_write_report_paths(tmp_path):
    data_file = tmp_path / "out.json"
    data_file.write_text(json.dumps({"1": record()}), encoding="utf-8")
    assert report.write_report(data_file) == tmp_path / "out.html"
    odd = tmp_path / "out.html"  # JSON saved with .html name: never overwritten
    odd.write_text(json.dumps({"1": record()}), encoding="utf-8")
    written = report.write_report(odd)
    assert written == tmp_path / "out.html.html" and json.loads(odd.read_text(encoding="utf-8"))
    assert report.main([str(tmp_path / "missing.json")]) == 1


def test_evidence_shows_only_the_matched_name():
    ev = "s and conditionsSite infoCopyright 2026 Equinor ASAEquinor and Shell p"
    assert report.evidence_match(ev, "EQUINOR ASA", "923609016") == "Equinor ASA"
    assert report.evidence_match("Org.nr 923 609 016 | x", "EQUINOR ASA", "923609016") == "org.nr 923 609 016"
    assert report.evidence_match("nothing here", "EQUINOR ASA", "923609016") is None
    assert report.evidence_match("", "EQUINOR ASA", "923609016") is None


def test_llm_summary_not_shown():
    out = report.render_report({"1": record(summary_of_changes="CEO name was not found.")})
    assert "CEO name was not found" not in out
