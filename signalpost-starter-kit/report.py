"""
HTML report — renders the pipeline's JSON output as one self-contained page.

    python report.py output_profiles.json            # -> output_profiles.html
    python report.py output_profiles.json --html r.html

``main.py`` also writes it automatically next to the JSON output.

Every claim is shown with its provenance: official Brreg facts are marked
"Brreg", web-derived ones "Web" with a link to the exact page they came from.
All scraped text is HTML-escaped and only http(s) links are rendered.
No external assets: the file works offline and can be attached or emailed.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SOCIAL_LABELS = {
    "linkedin": "LinkedIn", "facebook": "Facebook", "instagram": "Instagram",
    "x": "X", "youtube": "YouTube", "tiktok": "TikTok",
}
FACT_LABELS = (
    ("website", "Website"),
    ("ceo_name", "CEO (daglig leder)"),
    ("employee_count", "Employees"),
    ("latest_revenue_nok", "Revenue"),
    ("careers", "Careers page"),
)


# ---------------------------------------------------------------------------
# Small safe helpers
# ---------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url) -> str | None:
    """Only absolute http(s) URLs are ever put in an href."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    return url if parsed.scheme in ("http", "https") and parsed.netloc else None


def link(url, text=None, cls="") -> str:
    href = safe_url(url)
    label = esc(text if text is not None else url)
    if not href:
        return label
    c = f' class="{cls}"' if cls else ""
    return f'<a{c} href="{esc(href)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def host(url) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except (ValueError, AttributeError):
        return ""


def fmt_int(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else esc(n)


def fmt_nok(n) -> str:
    if not isinstance(n, int):
        return esc(n)
    if abs(n) >= 1_000_000_000:
        short = f"{n / 1e9:.1f} bn"
    elif abs(n) >= 1_000_000:
        short = f"{n / 1e6:.1f} m"
    elif abs(n) >= 1_000:
        short = f"{n / 1e3:.0f} k"
    else:
        short = str(n)
    return f'NOK {short} <span class="muted">({fmt_int(n)})</span>'


def is_official(source_url) -> bool:
    return host(source_url or "").endswith("brreg.no")


def has_value(fact) -> bool:
    return isinstance(fact, dict) and fact.get("fact_value") not in (None, "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _badge(source_url) -> str:
    if is_official(source_url):
        return '<span class="badge official" title="Official registry data (Brønnøysund)">Brreg</span>'
    return '<span class="badge web" title="Found on the company\'s own verified website">Web</span>'


def _fact_row(key: str, label: str, fact: dict) -> str:
    value = fact.get("fact_value")
    if key == "employee_count":
        shown = fmt_int(value)
    elif key == "latest_revenue_nok":
        shown = fmt_nok(value)
    elif key in ("website", "careers"):
        shown = link(value, host(value) or value)
    else:
        shown = esc(value)
    src = fact.get("source_url")
    when = fact.get("retrieval_date")
    source = link(src, "source", "src") if safe_url(src) else ""
    meta = " · ".join(x for x in (source, esc(when) if when else "") if x)
    return (f'<div class="fact"><dt>{esc(label)}</dt>'
            f'<dd>{shown} {_badge(src)}<span class="meta">{meta}</span></dd></div>')


def evidence_match(evidence: str, name: str, orgnr: str) -> str | None:
    """The exact text in *evidence* that names the entity (legal name or org number)."""
    if not evidence:
        return None
    flat = " ".join(evidence.split())
    tokens = [re.escape(t) for t in (name or "").split()]
    if tokens:
        m = re.search(r"\s*".join(tokens), flat, re.IGNORECASE)
        if m:
            return m.group(0)
    digits = re.sub(r"\D", "", orgnr or "")
    if len(digits) == 9:
        m = re.search(r"(?<!\d)" + r"[\s.]?".join((digits[:3], digits[3:6], digits[6:])) + r"(?!\d)", flat)
        if m:
            return f"org.nr {m.group(0)}"
    return None


def _news_list(items: list[dict], title: str, name: str = "", orgnr: str = "") -> str:
    if not items:
        return ""
    rows = []
    for n in items:
        named = evidence_match(n.get("entity_evidence") or "", name, orgnr)
        ev = f'<div class="ev">Article page names “{esc(named)}”</div>' if named else ""
        rows.append(
            f'<li><time>{esc(n.get("published_date"))}</time> '
            f'{link(n.get("url"), n.get("title") or n.get("url"))}{ev}</li>')
    return f'<section><h4>{esc(title)} <span class="count">{len(items)}</span></h4><ul class="items">{"".join(rows)}</ul></section>'


def _synthesis(syn) -> str:
    """What it does / what changed / what remains unknown, each line with its source."""
    if not isinstance(syn, dict):
        return ""
    blocks = []
    for key, title in (("what_it_does", "What it does"), ("what_changed", "What changed")):
        rows = [f'<li>{esc(s.get("text"))} {link(s.get("source_url"), "source", "src") if safe_url(s.get("source_url")) else ""}</li>'
                for s in syn.get(key) or [] if isinstance(s, dict)]
        if rows:
            blocks.append(f'<div class="syn"><h5>{title}</h5><ul>{"".join(rows)}</ul></div>')
    unknown = [u for u in syn.get("unknown") or [] if isinstance(u, dict)]
    if unknown:
        rows = "".join(
            (f'<li><span class="state">{esc(str(u["state"]).replace("_", " "))}</span> ' if u.get("state") else "<li>")
            + f'{esc(u.get("reason"))}</li>' for u in unknown)
        blocks.append(f'<div class="syn unk"><h5>Still unknown</h5><ul>{rows}</ul></div>')
    if not blocks:
        return ""
    return f'<details class="profile" open><summary>Profile summary</summary>{"".join(blocks)}</details>'


def _jobs_list(items: list[dict]) -> str:
    if not items:
        return ""
    rows = []
    for j in items:
        bits = [esc(x) for x in (j.get("location"),) if x]
        if j.get("valid_through"):
            bits.append(f"apply by {esc(j['valid_through'])}")
        extra = f' <span class="muted">· {" · ".join(bits)}</span>' if bits else ""
        rows.append(f'<li>{link(j.get("url"), j.get("title") or j.get("url"))}{extra}</li>')
    return f'<section><h4>Open positions <span class="count">{len(items)}</span></h4><ul class="items">{"".join(rows)}</ul></section>'


def _address(b: dict) -> str:
    a = b.get("forretningsadresse") or {}
    street = ", ".join(a.get("adresse") or [])
    town = " ".join(x for x in (a.get("postnummer"), a.get("poststed")) if x)
    return ", ".join(x for x in (street, town) if x)


def _company_card(orgnr: str, rec: dict) -> tuple[str, dict]:
    b = rec.get("brreg_metadata") or {}
    p = rec.get("intelligence_profile") or {}
    name = b.get("navn") or orgnr
    form = (b.get("organisasjonsform") or {}).get("kode") or ""
    industry = (b.get("naeringskode1") or {}).get("beskrivelse") or ""

    facts = [(k, l, p.get(k) or {}) for k, l in FACT_LABELS]
    found = [(k, l, f) for k, l, f in facts if has_value(f)]
    socials = [(SOCIAL_LABELS.get(k, k), v) for k, v in (p.get("socials") or {}).items() if has_value(v)]
    news = p.get("news") or []
    cnews = p.get("company_news") or []
    jobs = p.get("job_postings") or []

    # News found by both paths is shown once (company_news carries entity evidence).
    cnews_urls = {n.get("url") for n in cnews}
    news = [n for n in news if n.get("url") not in cnews_urls]
    all_news = sorted(cnews + news, key=lambda n: n.get("published_date") or "", reverse=True)

    signals = len(found) + len(socials) + len(all_news) + len(jobs)
    stats = {
        "facts": {k for k, _, _ in found}, "socials": bool(socials),
        "news": bool(all_news), "jobs": bool(jobs), "signals": signals,
        "fallback": rec.get("status") == "fallback",
    }

    fact_html = "".join(_fact_row(k, l, f) for k, l, f in found) or \
        '<p class="muted empty">No verified facts beyond the registry entry.</p>'
    social_html = ""
    if socials:
        social_html = '<div class="socials">' + "".join(
            link(v.get("fact_value"), label, "chip") for label, v in socials) + "</div>"

    status = ""
    if rec.get("status") == "fallback":
        status = f'<p class="warn">Partial profile: {esc(rec.get("fallback_reason"))}</p>'
    brreg_link = link(f"https://virksomhet.brreg.no/nb/oppslag/enheter/{orgnr}", "Brreg", "src")

    subtitle = " · ".join(esc(x) for x in (form, industry, _address(b)) if x)
    search = esc(f"{name} {orgnr} {industry} {_address(b)}".lower())
    card = f"""
<article class="card" data-search="{search}" data-signals="{signals}" data-name="{esc(name.lower())}"
  data-web="{int(bool(stats['news'] or stats['jobs'] or stats['socials'] or 'website' in stats['facts']))}">
  <header>
    <div>
      <h3>{esc(name)}</h3>
      <p class="sub">Org.nr {esc(orgnr)} · {brreg_link}{(' · ' + subtitle) if subtitle else ''}</p>
    </div>
    <span class="score" title="Verified signals for this company">{signals}</span>
  </header>
  {status}
  <dl class="facts">{fact_html}</dl>
  {social_html}
  {_news_list(all_news, "News", name, orgnr)}
  {_jobs_list(jobs)}
  {_synthesis(p.get("synthesis"))}
</article>"""
    return card, stats


def render_report(data: dict, generated: str | None = None) -> str:
    run = data.get("_run_report") or {}
    companies = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
    rendered = [_company_card(k, v) for k, v in companies.items()]
    cards = [c for c, _ in rendered]
    stats = [s for _, s in rendered]
    total = len(companies)

    def pct(n: int) -> str:
        return f"{(100 * n / total):.0f}%" if total else "0%"

    coverage = [
        ("Website", sum("website" in s["facts"] for s in stats)),
        ("CEO", sum("ceo_name" in s["facts"] for s in stats)),
        ("Employees", sum("employee_count" in s["facts"] for s in stats)),
        ("Revenue", sum("latest_revenue_nok" in s["facts"] for s in stats)),
        ("Socials", sum(s["socials"] for s in stats)),
        ("News", sum(s["news"] for s in stats)),
        ("Open jobs", sum(s["jobs"] for s in stats)),
    ]
    bars = "".join(
        f'<div class="bar"><span class="bl">{esc(label)}</span>'
        f'<span class="track"><span class="fill" style="width:{pct(n)}"></span></span>'
        f'<span class="bn">{n} <span class="muted">({pct(n)})</span></span></div>'
        for label, n in coverage)
    fallbacks = sum(s["fallback"] for s in stats)
    generated = generated or datetime.now().strftime("%Y-%m-%d %H:%M")
    run_bits = [f"{total} companies", f"generated {esc(generated)}"]
    if run.get("runtime_seconds") is not None:
        run_bits.append(f"runtime {esc(run['runtime_seconds'])} s")
    if run.get("total_cost_usd") is not None:
        run_bits.append(f"LLM cost ${esc(run['total_cost_usd'])}")
    if fallbacks:
        run_bits.append(f"{fallbacks} partial")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signalpost Company Report</title>
<style>
:root {{
  --bg:#f6f7f9; --panel:#fff; --text:#16181d; --muted:#626a77; --line:#e3e6eb;
  --accent:#1f5eff; --official:#0f7b4d; --official-bg:#e3f5ec; --web:#2a5bd7; --web-bg:#e6edfc;
  --warn:#8a5a00; --warn-bg:#fff4dc; --chip:#eef0f4;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#0f1115; --panel:#171a21; --text:#e7e9ee; --muted:#9aa3b2; --line:#2a2f3a;
    --accent:#6f9bff; --official:#5fd39b; --official-bg:#16352a; --web:#8fb0ff; --web-bg:#1c2846;
    --warn:#f2c26b; --warn-bg:#3a2e14; --chip:#232834;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
.lede {{ color:var(--muted); margin:0 0 20px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }}
.overview {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
.overview h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 10px; }}
.bar {{ display:grid; grid-template-columns:84px 1fr 92px; align-items:center; gap:10px; font-size:13px; margin:6px 0; }}
.track {{ height:8px; background:var(--chip); border-radius:99px; overflow:hidden; }}
.fill {{ display:block; height:100%; background:var(--accent); border-radius:99px; }}
.bn {{ text-align:right; font-variant-numeric:tabular-nums; }}
.legend p {{ margin:6px 0; font-size:13.5px; }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:0 0 16px; }}
.controls input[type=search] {{ flex:1 1 260px; padding:10px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--text); font:inherit; }}
.controls select, .controls label {{ font:inherit; font-size:14px; color:var(--text); }}
.controls select {{ padding:9px 10px; border-radius:8px; border:1px solid var(--line); background:var(--panel); }}
#shown {{ color:var(--muted); font-size:13px; margin-left:auto; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:14px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; min-width:0; }}
.card header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.card h3 {{ font-size:16px; margin:0; overflow-wrap:anywhere; }}
.sub {{ color:var(--muted); font-size:12.5px; margin:2px 0 10px; overflow-wrap:anywhere; }}
.score {{ flex:none; min-width:30px; height:30px; padding:0 8px; border-radius:99px; display:grid; place-items:center;
  background:var(--chip); font-weight:600; font-size:13px; }}
.facts {{ margin:0; }}
.fact {{ display:grid; grid-template-columns:132px 1fr; gap:8px; padding:5px 0; border-top:1px solid var(--line); }}
.fact dt {{ color:var(--muted); font-size:13px; }}
.fact dd {{ margin:0; overflow-wrap:anywhere; }}
.meta {{ display:block; font-size:12px; color:var(--muted); }}
.badge {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 7px; border-radius:99px; margin-left:4px; vertical-align:1px; }}
.official {{ color:var(--official); background:var(--official-bg); }}
.web {{ color:var(--web); background:var(--web-bg); }}
.socials {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 2px; }}
.chip {{ background:var(--chip); padding:3px 10px; border-radius:99px; font-size:13px; }}
section h4 {{ font-size:13px; margin:14px 0 6px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
.count {{ background:var(--chip); color:var(--text); border-radius:99px; padding:0 7px; font-size:11px; margin-left:4px; }}
.items {{ list-style:none; margin:0; padding:0; }}
.items li {{ padding:5px 0; border-top:1px solid var(--line); font-size:14px; overflow-wrap:anywhere; }}
.items time {{ color:var(--muted); font-variant-numeric:tabular-nums; font-size:12.5px; margin-right:4px; }}
.ev {{ color:var(--muted); font-size:12px; font-style:italic; }}
.profile {{ margin-top:14px; border-top:1px solid var(--line); padding-top:8px; }}
.profile summary {{ cursor:pointer; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
.syn h5 {{ margin:10px 0 4px; font-size:12.5px; }}
.syn ul {{ margin:0; padding-left:18px; font-size:13.5px; }}
.syn li {{ margin:3px 0; overflow-wrap:anywhere; }}
.unk li {{ color:var(--muted); }}
.state {{ display:inline-block; font-size:11px; padding:0 6px; border-radius:99px; background:var(--chip); color:var(--text); margin-right:4px; }}
.summary {{ color:var(--muted); font-size:13px; margin:12px 0 0; }}
.muted {{ color:var(--muted); }}
.empty {{ font-size:13px; margin:6px 0; }}
.warn {{ background:var(--warn-bg); color:var(--warn); padding:6px 10px; border-radius:8px; font-size:13px; }}
.src {{ font-size:12px; }}
.none {{ text-align:center; color:var(--muted); padding:40px 0; display:none; }}
@media (max-width:720px) {{
  .overview {{ grid-template-columns:1fr; }}
  .grid {{ grid-template-columns:1fr; }}
  .fact {{ grid-template-columns:1fr; gap:0; }}
  #shown {{ margin-left:0; width:100%; }}
}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Signalpost company report</h1>
  <p class="lede">{" · ".join(run_bits)}</p>

  <div class="overview">
    <div class="panel"><h2>Coverage</h2>{bars}</div>
    <div class="panel legend"><h2>How to read this</h2>
      <p><span class="badge official">Brreg</span> Official registry data for this exact organisation number.</p>
      <p><span class="badge web">Web</span> Found on the company's own website, on a page that shows its legal name or org number. Each claim links to that page.</p>
      <p>Company news shows the legal name or org number found on the article page itself. Social profiles are listed only when the handle is the company's exact legal name.</p>
      <p>The number on each card counts that company's verified signals.</p>
    </div>
  </div>

  <div class="controls">
    <input type="search" id="q" placeholder="Search by name, org.nr, industry or place" aria-label="Search companies">
    <select id="sort" aria-label="Sort companies">
      <option value="signals">Most signals first</option>
      <option value="name">Name A–Z</option>
    </select>
    <label><input type="checkbox" id="web"> Only companies with web data</label>
    <span id="shown" aria-live="polite"></span>
  </div>

  <div class="grid" id="grid">{"".join(cards)}</div>
  <p class="none" id="none">No companies match.</p>
</div>
<script>
(function () {{
  var grid = document.getElementById('grid'), q = document.getElementById('q'),
      sort = document.getElementById('sort'), web = document.getElementById('web'),
      shown = document.getElementById('shown'), none = document.getElementById('none');
  var cards = Array.prototype.slice.call(grid.children);
  function apply() {{
    var term = q.value.trim().toLowerCase(), n = 0;
    cards.sort(function (a, b) {{
      if (sort.value === 'name') return a.dataset.name.localeCompare(b.dataset.name);
      return (b.dataset.signals - a.dataset.signals) || a.dataset.name.localeCompare(b.dataset.name);
    }});
    cards.forEach(function (c) {{
      var ok = (!term || c.dataset.search.indexOf(term) !== -1) && (!web.checked || c.dataset.web === '1');
      c.style.display = ok ? '' : 'none';
      if (ok) n++;
      grid.appendChild(c);
    }});
    shown.textContent = n + ' of ' + cards.length + ' shown';
    none.style.display = n ? 'none' : 'block';
  }}
  q.addEventListener('input', apply); sort.addEventListener('change', apply); web.addEventListener('change', apply);
  apply();
}})();
</script>
</body>
</html>
"""


def write_report(json_path: str | Path, html_path: str | Path | None = None) -> Path:
    json_path = Path(json_path)
    html_path = Path(html_path) if html_path else json_path.with_suffix(".html")
    if html_path.resolve() == json_path.resolve():  # never overwrite the data file
        html_path = json_path.with_name(json_path.name + ".html")
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    html_path.write_text(render_report(data), encoding="utf-8")
    return html_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Signalpost JSON output as an HTML report.")
    parser.add_argument("json_file", help="Output JSON written by main.py")
    parser.add_argument("--html", help="Report path (default: JSON path with .html)")
    args = parser.parse_args(argv)
    try:
        out = write_report(args.json_file, args.html)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Cannot build report: {e}", file=sys.stderr)
        return 1
    print(f"Report written -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
