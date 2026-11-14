# Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Run the starter on 100 companies before submitting. Larger local tests, including 1,000 or more companies, are encouraged but their precomputed profiles are not submitted or scored.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

```bash
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local test batch of any size. Use 100 rows for the recommended smoke test before trying a larger batch.

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# Use the 100-company batch as your smoke test.
cp entry-companies.jsonl smoke-companies.jsonl

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

# You may test at larger scale locally, but Builderr supplies the official batch for scoring.
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together for a larger local test. The 100-row smoke test above is practice only; Builderr supplies the companies for every official run.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Production-Ready Architecture

### Data Integrity

- **Subsidiary isolation.** Brreg's parent hierarchy is resolved up front, and a `DomainValidator` checks every website, news URL, social profile and careers/jobs link against the entity's own name and its parents' domains. A subsidiary can never be credited with its parent's site, accounts, news or job listings. The same checks run on previously cached snapshots, so a parent's data cannot be carried forward by the diff engine.
- **Strict Pydantic schemas.** The LLM only returns lenient *candidates*. Each candidate is validated item by item into a strict model (`NewsItem`, normalised social URLs, careers URL), so one bad item never fails a whole response. Unsupported values become `null`, not guesses.
- **Grounded `YYYY-MM-DD` dates.** A news item is kept only if it has a real, non-future `YYYY-MM-DD` publication date, its `date_type` is `published` (modified/updated/*oppdatert* dates are rejected), and its headline and date wording appear verbatim on a scraped page. Page metadata cross-checks the date. Navigation labels such as "See more news" or "Read more" are rejected, so UI text is never reported as news.
- **Strict content grounding.** A domain is kept only if one of its fetched pages contains the exact Brreg name (case-insensitive) or the 9-digit organisation number (also written as `923 609 016`). When the homepage does not, a bounded same-domain crawl checks the site's about/contact/legal pages (at most 2 link layers and 4 extra fetches per domain) before the domain is rejected. The matching rule itself is never loosened.
- **Official registry facts.** CEO (the single active *daglig leder*), employee count (only when registered) and revenue (the entity's own accounts - never group/*konsern* figures - latest year, NOK only) come from Brreg's Enhetsregisteret and Regnskapsregisteret, keyed by organisation number. They override web/LLM values and link to the API record that contains them.
- **Exact-name social profiles.** A social profile is kept only if it is linked from the company's verified site **and** its handle is the exact legal name (case, spacing, punctuation, Norwegian letters and the AS/ASA suffix aside). `telenornorge` is not credited to TELENOR ASA; `equinor` is not credited to EQUINOR ENERGY AS.
- **News pages must name the entity.** Both LLM news and crawled company news are kept only if the article's own page shows the exact legal name or organisation number. Listing, archive, paginated and utility URLs (`/page/2`, `?filter=`, subscription, newsletter) are never news. A publication date read from page text must be a label ("Published 3 November 2026", "Publisert:"), never prose such as "the announcement published on 21 October", and a text date phrase that repeats across several articles of a site is treated as boilerplate and rejected.
- **Off-site redirects are dropped.** Every fetch, including homepages, records where its redirects end. If the final URL is on a different registrable domain, the page is discarded unparsed and never used for grounding or extraction, so a third-party site can never vouch for the entity.

### Company news and job postings

`CompanyProfile` carries two page-grounded synthesis fields, built by a deterministic crawler (no LLM) from the company's own grounded site:

| Field | Item model | Fields |
| --- | --- | --- |
| `company_news` | `CompanyNewsItem` (extends `NewsItem`) | `title`, `url`, `published_date` (`YYYY-MM-DD`), `date_type` (`published`), `date_evidence`, `summary`, `source_url`, `entity_evidence`, `retrieval_date` |
| `job_postings` | `JobPosting` | `title`, `url`, `source_url`, `entity_evidence`, `date_posted`, `valid_through`, `location`, `retrieval_date` |

- `url` is the item's own page; `source_url` is the company-owned page that links to it; `entity_evidence` quotes the text on the item's own page that names the entity.
- **Golden rule.** An item is kept only if its own page, fetched on its own URL, contains the exact Brreg name or organisation number - the same check used for website grounding.
- **Discovery.** The crawler follows news hubs (`/nyheter`, `/news`, `/aktuelt`, `/presse` ...) and careers hubs (`/karriere`, `/careers`, `/ledige-stillinger` ...) linked from the grounded pages, probing the usual paths when none are linked, with one nested listing level (e.g. `/aktuelt/nyheter-og-reportasjer`). It is capped at 17 extra fetches per company, never fetches a URL twice and runs concurrently with the LLM call.
- **News rules.** Every `NewsItem` rule applies: a real headline (not a UI label), a direct article URL, and a publication date from page metadata or a labelled date in the text (`Publisert 12. mars 2026`) - never a modified/updated date, never in the future. At most 5 items, newest first.
- **Job rules.** The page must be a specific open vacancy: schema.org `JobPosting` data, or an application deadline (*søknadsfrist* / "application deadline") together with posting cues. Role descriptions, internship/graduate programme and "life at" pages are not postings. Generic labels such as "Ledige stillinger" are rejected, and postings marked filled or past their deadline are dropped. At most 10 postings.
- **Domains.** News must sit on a validated own domain. Postings may also be hosted on a known applicant-tracking system (Teamtailor, Webcruiter, Jobbnorge, Recman, HR Manager, ...) when the company's own site links to them; on an ATS board only that company's own board host is followed. Items on a parent's domain or on a domain that failed grounding are dropped, including from cached snapshots.
- **Across runs.** `company_news` accumulates by URL like `news`; `job_postings` always reflects the latest run, so a closed posting is never carried forward.

### Synthesis: what it does, what changed, what remains unknown

Every profile carries `intelligence_profile.synthesis`, built deterministically (no LLM) after all verification, only from the Brreg registry records and the already-verified facts. Each statement has a `source_url` and `retrieval_date` (plus a `date` where one applies):

- **`what_it_does`** - legal form, location and founding date; the registered activity quoted verbatim from Brreg; NACE industry; CEO, employees and revenue; the verified website.
- **`what_changed`** - bankruptcy / liquidation (with dates), previous names, revenue change between the last two fiscal years and the latest annual result (own-entity accounts, NOK), when the *daglig leder* registration last changed, the latest dated news, and open positions.
- **`unknown`** - each missing field with the specific reason: e.g. *no active daglig leder registered*, *accounts reported in USD, not NOK*, *this organisation form only has to file accounts above certain thresholds*, *no site was found that shows the legal name or org number*.

`summary_of_changes` is regenerated from these statements, so the free-text summary never contains an unsourced claim.

**Availability states.** `intelligence_profile.availability` gives every field (`website`, `ceo_name`, `employee_count`, `latest_revenue_nok`, `news`, `job_postings`, `careers`, `socials`) one of the contract states with a reason: `available`; `not_available` (the source was checked and has nothing - e.g. no employee count registered); `not_applicable` (does not apply - e.g. revenue for an association below the filing threshold, CEO for a sole proprietorship run by its owner); `failed` (the lookup or LLM step could not run). A field that could not be checked for lack of a verified website says so explicitly. `blocked` and `ambiguous` are never guessed.

```json
"what_changed": [
  {"text": "Previously named STATOIL ASA (until 2018-05-16).",
   "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
   "retrieval_date": "2026-11-15", "date": "2018-05-16"}
]
```

### HTML report

Every run also writes a self-contained HTML report next to the JSON (`--output out.json` -> `out.html`; `python report.py out.json` rebuilds it). It shows coverage per field, one card per company with each claim's source link, the sourced synthesis (what it does / what changed / still unknown) and a **Brreg** (official registry) or **Web** (company's own verified site) badge, the legal name found on each news page, and search / sort / filter. It works offline, follows light/dark mode, fits phone screens, and escapes all scraped text. A report failure never fails the run.

### 100-company smoke test

`reports/` holds a full run on 100 companies drawn at random (seed `20261115`) from the 1,000-company batch: input `smoke-100.txt`, results `smoke-100.json`, report `smoke-100.html`. Command: `python main.py reports/smoke-100.txt --output reports/smoke-100.json`.

| Result | Value |
| --- | --- |
| Records | 100 / 100, 0 fallbacks, 0 crashes |
| Mix | 47 AS, 35 associations (FLI), 12 sole proprietorships, 6 other |
| Revenue (Brreg) / CEO / website | 37 / 36 / 11 companies |
| Runtime | 466 s (concurrency 5) |
| LLM cost | $0.0032 (6 Gemini calls; most entities have no verifiable website) |

Expected cost per 100 companies: about $0.003-0.01 for a mix like this, up to about $0.07 for 100 large companies with content-rich sites (one Gemini Flash-Lite call per company with a verified site).

### Run with Docker

```bash
docker build -t signalpost .
docker run --rm -e GEMINI_API_KEY=<key> -v "$PWD/data:/data" signalpost /data/input_orgs.jsonl --output /data/out.json
# -> /data/out.json and /data/out.html
```

- **Pinned dependencies.** `requirements.lock` holds the exact 78-package set verified on Linux + CPython 3.13 (the `python:3.13-slim` base): clean install, full test suite and live end-to-end runs. The Dockerfile installs with `pip install -c requirements.lock .`, so a new upstream release cannot change what runs. Without Docker: `pip install -c requirements.lock .`
- **Nothing local is baked in.** `.dockerignore` keeps `.env` (pass the key with `-e`), the snapshot cache, virtualenvs and build artifacts out of the image. With no arguments the image prints usage.

### Cost Efficiency

- **API key short-circuit.** The Gemini key is probed once at start-up, and a rejected key disables the LLM for the rest of the run. Every company then returns its Brreg baseline immediately, with no search, scraping or LLM calls spent on a key that cannot work.
- **No LLM call on empty content.** If no verified domain remains, or the scraped pages return no readable text or links, the agent returns a baseline profile without calling the LLM.
- Prompts are capped in size, and only validated domains are ever scraped.
- **Automated Cost Reporting:** The pipeline dynamically extracts `usage_metadata` from the Gemini API and calculates the exact third-party USD cost based on token volume, appending it to the final machine-readable `_run_report`.

### Fault Tolerance

- **Bounded concurrency.** Companies are processed concurrently behind an `asyncio.Semaphore` limited to **5**, which keeps large batches inside Brreg, search and Gemini rate limits.
- **Exponential backoff.** A custom async retry loop with jitter (delay doubling per attempt, capped at 30 s) wraps the Gemini call (5 attempts; HTTP 429, 5xx, timeouts and connection errors, including errors wrapped by `instructor`) and page fetches (3 attempts). Web search uses the `ddgs` metasearch library with an ordered backend list (Bing first; override with `SEARCH_BACKENDS`), at most 3 concurrent searches and 2 attempts. Authentication errors and other 4xx responses are not retried.
- **The LLM is optional.** Brreg facts, website grounding and the deterministic news/jobs crawl run without it. With no `GEMINI_API_KEY`, a rejected key, or a failed Gemini call, every strictly verified registry and site result is still returned; only the LLM extras (social profiles, careers link, LLM news) are missing, and the record is flagged `status: "fallback"` with the reason.
- **Graceful degradation.** Every input always yields a result record. A failed registry lookup or web extraction falls back to the Brreg baseline and is flagged `status: "fallback"` with a reason.
- **Flexible input.** `python main.py <batch>` accepts plain text (one org number per line), JSONL (`{"orgnr": "923609016"}`; also `organisasjonsnummer` / `organisation_number`), a JSON array or CSV lines, and spaced numbers (`923 609 016`). Anything that is not a 9-digit org number is skipped; duplicates are dropped.

### Testing Rigor

The offline unit suite needs no network or API key:

| Test file | Covers |
| --- | --- |
| `tests/test_domain_validator.py` | Registrable-domain parsing, parent-domain rejection (with and without a Brreg parent), subsidiaries registered with a parent's site, Norwegian characters and unrelated domains, sanitising and purging cached parent-sourced facts. |
| `tests/test_news_extraction.py` | Rejection of UI labels, modified/updated dates, future dates, listing and non-article URLs; grounding of headlines, URLs and date evidence; page-metadata date cross-checks; social profile validation and group-brand rejection; news accumulation across runs. |
| `tests/test_content_grounding.py` | Exact name / org-number matching, rejection of ungrounded domains, footer text, the about/contact fallback crawl (linked pages, path probes, second layer), unchanged matching strictness and fetch bounds. |
| `tests/test_site_activity.py` | Off-site redirect dropping (no parse, no retry, no grounding); `company_news` and `job_postings` discovery, the golden rule on each item's own page, publication dates, closed/expired postings, ATS boards, crawl bounds, parent-domain clean-up and diff behaviour. |

`tests/test_careers_and_retry.py` additionally covers the careers/jobs validation, retry classification and backoff behaviour, and the concurrency limit.

```bash
python tests/test_domain_validator.py
python tests/test_news_extraction.py
python tests/test_careers_and_retry.py
python tests/test_content_grounding.py
python tests/test_site_activity.py
```

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.
