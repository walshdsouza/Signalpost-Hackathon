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

### Cost Efficiency

- **API key short-circuit.** The Gemini key is probed once at start-up, and a rejected key disables the LLM for the rest of the run. Every company then returns its Brreg baseline immediately, with no search, scraping or LLM calls spent on a key that cannot work.
- **No LLM call on empty content.** If no verified domain remains, or the scraped pages return no readable text or links, the agent returns a baseline profile without calling the LLM.
- Prompts are capped in size, and only validated domains are ever scraped.
- **Automated Cost Reporting:** The pipeline dynamically extracts `usage_metadata` from the Gemini API and calculates the exact third-party USD cost based on token volume, appending it to the final machine-readable `_run_report`.

### Fault Tolerance

- **Bounded concurrency.** Companies are processed concurrently behind an `asyncio.Semaphore` limited to **5**, which keeps large batches inside Brreg, search and Gemini rate limits.
- **Exponential backoff.** A custom async retry loop with jitter (delay doubling per attempt, capped at 30 s) wraps the Gemini call (5 attempts; HTTP 429, 5xx, timeouts and connection errors, including errors wrapped by `instructor`), page fetches and web search (3 attempts). Authentication errors and other 4xx responses are not retried.
- **Graceful degradation.** Every input always yields a result record. A failed lookup or extraction falls back to the Brreg baseline and is flagged `status: "fallback"` with a reason.

### Testing Rigor

The offline unit suite needs no network or API key:

| Test file | Covers |
| --- | --- |
| `tests/test_domain_validator.py` | Registrable-domain parsing, parent-domain rejection (with and without a Brreg parent), subsidiaries registered with a parent's site, Norwegian characters and unrelated domains, sanitising and purging cached parent-sourced facts. |
| `tests/test_news_extraction.py` | Rejection of UI labels, modified/updated dates, future dates, listing and non-article URLs; grounding of headlines, URLs and date evidence; page-metadata date cross-checks; social profile validation and group-brand rejection; news accumulation across runs. |

`tests/test_careers_and_retry.py` additionally covers the careers/jobs validation, retry classification and backoff behaviour, and the concurrency limit.

```bash
python tests/test_domain_validator.py
python tests/test_news_extraction.py
python tests/test_careers_and_retry.py
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
