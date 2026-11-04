# Operational runbook

## First working session

Install the lock file, run unit tests and the synthetic demo using README commands. Do not start by changing
every configuration field. The demo is isolated in `artifacts/demo`; research uses `data/research` and
`artifacts/research`; the included real connectivity pilot uses `data/live_pilot`.

Use `python -m scripts...` from the project root, not `python scripts/file.py`. Module invocation ensures
imports resolve consistently on Windows and Linux. Jupyter is optional and not a runtime dependency.

## Test live access cheaply

```bash
python -m scripts.run_ingest --config configs/live_pilot.yaml --stages markets,quotes,news
python -m scripts.build_dataset --config configs/live_pilot.yaml --at now
python -m scripts.run_baselines --config configs/live_pilot.yaml
```

The pilot config caps market count and retries. Exit code 2 from ingestion means some stages failed:
read `artifacts/live_pilot/ingestion_latest.json` and the SQLite pipeline_events table. Successful raw
responses remain saved. A source returning zero records is visible as zero, not treated as a dataset.

Newly created markets may have no two-sided book. The collector now discovers by recent volume; this is
a convenience for the pilot. Freeze a defensible as-of selection policy for the research cohort.
Do not treat the first few accessible contracts as representative of every prediction market.

## Configure a real LLM

1. Copy `.env.example` to `.env`; enter a provider's actual `/v1`-style base URL, API key and model ID.
2. The adapter sends POST `/chat/completions` with messages, model, temperature and max_tokens. It expects
   `choices[0].message.content` containing JSON. Confirm your provider supports that contract and these fields.
3. If your provider uses a different API schema, add a named adapter with contract tests; do not pretend
   the generic endpoint works with every provider. No credentials or account were supplied with this package.
4. Set the selected model in config for a stable experiment and a small call limit for the first pilot.
5. Generate fresh snapshots, then `python -m scripts.run_agents --config configs/live_pilot.yaml --limit 3`.
6. Inspect probabilities, citations, raw cached outputs, returned model name and token usage before scaling.

The complete comparison set costs approximately nine distinct calls per snapshot at K=3. The first
single-agent RAG result is reused as the first repeated-call member. The no-social variant reuses cached
work only when inputs are truly identical. Cache hits consume no new API request budget.

`max_calls_per_run` bounds requests, not money, tokens or a monthly provider account. No automatic paid
request is made by installation or the synthetic demo. A real `run_agents` command is an explicit paid-call
operation when your provider charges for its API. Record actual costs and latency from a pilot.

## Collection schedule and honest timing

Use one scheduled collector on the CUHK server or your computer. For a pilot, a five-minute poll for quotes
and a 15–60-minute poll for feeds is a reasonable starting point; adjust to the chosen horizons/rate limits.
Configure this in your own scheduler after verifying server policy and storage. This package does not
silently install cron jobs or background services.

Run markets/quotes/news before build_dataset, then agents promptly. Fixed-horizon snapshots become eligible
only when the project already held metadata, prices and evidence at the horizon. You cannot recreate last
week's first observations by running the collector today. The default maximum prospective generation delay
is 900 seconds; change it only as a protocol decision. Report actual latency.

With new daily collection, use pilot `--at now` while validating infrastructure. For the primary 24/168-hour
horizons, run the scheduled snapshot builder and forecast soon after a horizon passes. Old stale requests
are refused unless explicitly configured as a model-frozen archival replay. Existing complete run outputs
are skipped on resume. Failed partial outputs remain auditable and successful API calls can be reused.

## Freeze, train and evaluate

Finalize model/prompts, filters, event grouping, sources, horizons, train_end and validation_end in config.
Choose dates with enough mature labels before each fit time. Copy the config for a new experiment rather
than mutating the original after collecting forecasts. Keep a dated decision log.

```bash
python -m scripts.freeze_protocol
python -m scripts.run_agents
python -m scripts.fit_calibrator --run-id YOUR_RUN_ID
python -m scripts.run_eval --run-id YOUR_RUN_ID
```

`fit_calibrator` refuses missing dates, missing forecasts, source/config mismatch and too few independent
groups. It writes tuning scores, fitted models and split manifests. Minimum group thresholds are guards,
not a power analysis. Do not lower them simply to make the command succeed and then claim reliable results.

For an openly exploratory retrospective analysis, `run_eval --allow-exploratory` labels the report accordingly.
It does not waive timestamp checks or permit using test data for training. Freeze the final test design
before seeing its outcomes. Commit/publish the protocol hash for an external timestamp.

Forecasting output is retained even when no outcomes have resolved. Evaluation then exits with an explicit
message. Run resolution collection later; do not rerun the original forecasts using newer evidence.

## Common failures

| Message or symptom | Meaning | Correct action |
|---|---|---|
| HTTP 403 | Endpoint/access restriction or unsupported path | Check official endpoint and permitted access; keep failure logged |
| HTTP 429/503 | Rate limit or transient provider issue | Reduce polling/request rate; bounded retry; inspect provider status |
| Empty history | Missing history/resolution/window mismatch | Inspect raw response and documented grain; do not fabricate fine data |
| Empty/one-sided book | No valid midpoint | Exclude at that cutoff; retain exclusion count |
| metadata_not_available_asof | Metadata was first observed later | Collect prospectively or obtain a verified earlier archive |
| price_not_available_asof | No admissible earlier price | Do not substitute a future quote |
| stale_price | Last eligible quote exceeds threshold | Refresh collection or retain a documented missing forecast |
| Historical LLM run refused | Cutoff is old relative to generation | Use prospective collection or a documented frozen-model archival design |
| Too few complete groups | Not enough valid resolved training data | Accumulate data; narrow the model/scope with supervisor agreement |
| Config/source changed | Run provenance mismatch | Start a new versioned experiment; do not splice results together |
| Invented citation | LLM cited unavailable evidence | Keep failure; inspect prompt/provider; revise only before final testing |

## Reproduce reports and dashboard

```bash
python -m scripts.run_demo
python -m src.dashboard.app artifacts/demo/artifacts/RUN_PREFIX/evaluation.json --output artifacts/demo/report.html
```

Use the output directory from demo_summary.json rather than guessing RUN_PREFIX. Open the HTML locally.
This is a simple static experiment report, not the full Week 10 live/history interactive demonstration.
`notebooks/eda.ipynb` and `notebooks/results.ipynb` are optional guided analyses; the actual pipeline remains
in importable source modules and scripts.

## GitHub handoff

The project was built from scratch; it is not connected to or modifying your existing repository. If you
already have HX2605 code, copy this into a new branch and reconcile files before replacing anything.

For a new empty repository, from the extracted project folder:

```bash
git init
git add .
git status --short
git diff --cached --stat
git commit -m "Add reproducible HX2605 forecasting framework"
git branch -M main
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

Inspect staged files before the commit. `.env`, data/, artifacts/, .venv/ and caches should be absent.
Only compact sanitized verification reports under examples/ are intended for source control. Do not publish
source text whose license does not allow redistribution. The public reproducibility package can instead
contain record IDs, timestamps, hashes, source URLs, collection instructions and synthetic fixtures.

No repository was pushed, no cron task installed and no message sent to your supervisor by this package.

## Scheduled collector (data collection only)

`configs/collect.yaml` drives collection; `config.yaml` drives forecasting. They are separate so that
editing the watchlist never changes a forecasting run identity. Both use `data/research/research.sqlite`.

```powershell
.\.venv\Scripts\python.exe -m scripts.run_collect --profile discovery     # watchlist markets (tag based)
.\.venv\Scripts\python.exe -m scripts.run_collect --profile quotes        # order-book midpoints
.\.venv\Scripts\python.exe -m scripts.run_collect --profile rss           # news feeds
.\.venv\Scripts\python.exe -m scripts.run_collect --profile gdelt         # event queries (rate limited)
.\.venv\Scripts\python.exe -m scripts.run_collect --profile resolutions   # final labels, batched
powershell -ExecutionPolicy Bypass -File scripts\schedule_collector.ps1     # install Windows tasks
powershell -ExecutionPolicy Bypass -File scripts\schedule_collector.ps1 -Remove
```

Cadence installed by the script: quotes 10 min, rss 15 min, discovery 3 h, gdelt 1 h, resolutions 1 h.
Keep the laptop plugged in with sleep set to "Never": a missed poll cannot be recovered afterwards.

- **Health:** `artifacts/research/collector_history.jsonl` has one line per run (ok / failed / zero-result counts
  per stage, sample errors). A failed request is reported under `failed`; a successful search with no articles is
  reported under `zero_results`. Uncaught crashes go to `artifacts/research/collector_errors.log`.
  For `resolutions`, `zero_results` is normal (nothing new settled).
- **Exit code 2** means failures worth reading (for quotes: at least 25% failed).
- **Raw archive:** one gzip file per profile per UTC day under `data/research/raw/<date>/`. Every database
  `raw_ref` is `path#offset+length`; read one with `src.ingestion.http.read_archive(raw_ref)`.
- **Locks:** a profile skips its run if the previous one is still active (stale after 30 min).
- **Measured on 2026-11-04** (150 tracked markets): about 0.8 KB per quote book compressed, about 2 MB per
  discovery run, 8 feeds about 0.1 MB per run. With `max_tracked: 300` expect roughly 60-65 MB/day, about
  7-8 GB for four months including SQLite. A quote run over 300 markets takes about 5 minutes. Polling quotes
  every 15 min instead of 10 reduces volume by about a third.
- **Labels:** `/v2/resolutions` records have `status`, `price`, `last_update_timestamp` (no payouts array).
  `proposed` is not a label. Price `0` means the second outcome won, `10**18` the first, anything else is
  excluded. At most 20 conditions per request.

### Checking on the collector

```powershell
.\.venv\Scripts\python.exe -m scripts.collector_status                    # alive? gaps? counts? disk?
.\.venv\Scripts\python.exe -m scripts.backup_db --dest E:\hx2605_backup   # also scheduled daily at 03:00
```

`collector_status` reads `collector_history.jsonl` and the database (read-only). A profile is STALE when its last
run is older than 2.5 intervals; it also lists the longest gaps of the last 7 days (laptop asleep or offline),
open markets with no quote in 30 minutes, unresolved markets past their deadline, and projected disk days left.
Exit code 2 means something needs attention. Run it after waking the laptop and once a day.

`backup_db` takes a consistent online copy of the SQLite file (compressed, 14 kept) and copies each finished
UTC day of the raw archive once. Point `--dest` at a different drive: a backup on the same disk does not protect
against disk failure. Gaps cannot be backfilled, so this is the only protection for what is already collected.

## Forecasting with Gemini (checked against Google's docs on 2026-11-04; not yet run with a real key)

```
LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-3.8-flash
LLM_API_KEY=<your key>
```

- `gemini-3.8-flash` is listed as stable. Thinking cannot be disabled (low/medium/high only); output price includes
  thinking tokens, so `max_output_tokens` is 8192 and a truncated answer is reported, not parsed. Set
  `llm.reasoning_effort: low` to bound cost and latency.
- Not found in the docs: temperature guidance for this model. Temperature is configurable (`null` omits it). In the
  pilot, check that the K repeated calls actually differ; if the model ignores temperature they will not.
- List price at time of writing: $0.75 / $3.75 per million input / output tokens until 31 Dec 2026, doubling on
  1 Jan 2027. Free-tier content is used by Google to improve its products; paid-tier content is not.
- Rough budget (assumptions, not measurements): 8-10 calls per snapshot at about 4k input and 2k output tokens is
  roughly $0.1 per snapshot. Measure real tokens in the 3-snapshot pilot (`prompt_tokens`, `completion_tokens` and
  `response_models` are in `agents_latest.json`) before scaling.
- `python -m scripts.run_agents --limit 3` forecasts the three NEWEST eligible snapshots; snapshots older than
  `llm.max_forecast_delay_seconds` are skipped and reported as `skipped_stale`.
- Stage-level failures are expected and logged (HTTP, bad output, no relevant evidence). A programming error stops
  the run with a traceback instead of being logged as a failed forecast.
