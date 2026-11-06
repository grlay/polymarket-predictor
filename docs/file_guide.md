# File-by-file guide

All Python source is executable implementation, not placeholder pseudocode. Files named `__init__.py`
mark importable packages. Configuration and documentation are separate from generated data/model artifacts.

## Root and configuration

| File | Purpose |
|---|---|
| README.md | First-run instructions, scope, limitations and links to detailed documents |
| requirements.lock | Fully pinned runtime dependency closure for the selected Python range |
| .env.example | Safe credential/model template; real values belong only in ignored .env |
| .gitignore | Excludes private credentials, runtime data, virtual environments and generated artifacts |
| configs/config.yaml | Main research paths, APIs, dates, horizons, retrieval, model, budgets, training and evaluation settings |
| configs/live_pilot.yaml | Small, bounded real-source connectivity/ingestion experiment |
| configs/market_filters.yaml | As-of liquidity/volume/category/price/rule filters |
| configs/relevance_profiles.yaml | Hand-written evidence profiles per storyline (entities, institution/action terms, window); hashed into the forecast run identity |
| configs/prompts/news_agent.md | Evidence analyst instructions and structured-output contract |
| configs/prompts/base_rate_agent.md | Reference-class estimate without retrieved evidence or market price |
| configs/prompts/verifier.md | Audit/revision instructions applied to drafts and the same evidence |
| configs/prompts/llm_no_retrieval.md | Question-only LLM comparator prompt |
| configs/prompts/single_agent_rag.md | Single-agent evidence-grounded comparator prompt |
| .github/workflows/tests.yml | Optional GitHub CI: install pins and run offline tests on Python 3.10 and 3.12 |

## Documents

| File | Purpose |
|---|---|
| docs/framework.md | Critical feasibility assessment; architecture; data flow; agent design; training; interpretation |
| docs/research_question.md | Primary question, estimand, hypotheses, scope and novelty boundary |
| docs/data_spec.md | Tables, fields, UTC meanings, provenance, joins, import contracts and lineage |
| docs/protocol.md | Proposed analysis rules and instructions for freezing the final experiment |
| docs/decisions_log.md | Dated design changes and rationale |
| docs/references.md | Primary sources and the starting related-work matrix |
| docs/runbook.md | Commands, credentials, scheduling, error recovery and GitHub handoff |
| docs/week7_status.md | Honest week-by-week implementation/evidence/dependency matrix and recovery plan |
| docs/file_guide.md | This file |

## Core data and retrieval modules

| File under src/ | Purpose / input → output |
|---|---|
| common.py | UTC parsing, canonical JSON, hashing, config/.env loading, source fingerprint and runtime versions |
| ingestion/http.py | HTTPS GET → bounded retry, provider response, observation timestamp and immutable raw archive |
| ingestion/gamma.py | Gamma market pages → explicit token mapping and versioned market metadata; selection audit |
| ingestion/clob.py | Current order book → two-sided midpoint; explicit legacy history collector |
| ingestion/data_api.py | Data v2 history/resolutions → grain-aware records, cursor checks and final binary labels |
| ingestion/news.py | RSS/Atom or GDELT results → timestamped summaries/headlines with explicit content kinds; items from a known aggregator (Google News) are attributed to the publisher named in the `<source>` tag |
| ingestion/social.py | Public Reddit feed or licensed JSONL export → social/evidence records |
| ingestion/external.py | Economic/financial release CSV → versioned release evidence observed at import time |
| db/schema.sql | Canonical relational schema, keys, references, constraints and lookup indexes |
| db/loaders.py | Database connection, immutable/idempotent inserts, document deduplication and event logging |
| dataset/market_selection.py | As-of market state + filters → eligibility or explicit exclusion reason |
| dataset/snapshots.py | Market versions/prices → horizon or pilot snapshots; exportable labelled research frame |
| dataset/splits.py | Snapshot frame → chronological group-disjoint train/validation/test/purged cohorts and event weights |
| dataset/leakage_checks.py | Evidence, price, metadata, labels and groups → fail-closed validity checks |
| retrieval/embedder.py | Text → stateless sparse hashed vectors; no future vocabulary/IDF fitting |
| retrieval/index.py | SQLite corpus + cutoff → eligible documents only |
| retrieval/relevance.py | Question + document → House-control gate or a profile gate (entity AND institution/action term, inside the window); one rejection reason per document; a profiled market with no relevant evidence is refused before any LLM call |
| retrieval/temporal_retriever.py | Eligible documents + question → ranked, deduplicated, source-capped evidence packet |
| features/market_features.py | Eligible price/metadata history → lag movement, spread, missingness and liquidity/volume features |
| features/text_features.py | Retrieved packet → auditable coverage/source/age features for analysis |

The text-coverage feature function is supplied for diagnostics; the default primary logistic model does
not use these extra features. Adding them changes the experiment and must be documented before testing.

## Agent and model modules

| File under src/ | Purpose |
|---|---|
| agents/llm_client.py | Generic chat-completions adapter; schema checks; bounded call budget; cache; explicitly synthetic test backend |
| agents/news_agent.py | Applies versioned news-agent prompt to the frozen evidence packet |
| agents/base_rate_agent.py | Applies reference-class prompt to question/rules only |
| agents/verifier.py | Reviews initial forecasts against the same evidence; retains originals |
| agents/orchestrator.py | Builds allowlisted contexts, executes all systems, resumes/caches, enforces timing, logs run lineage and failures |
| baselines/market_only.py | Returns the as-of market probability |
| baselines/llm_no_retrieval.py | Runs the no-retrieval comparator |
| baselines/single_agent_rag.py | Runs one evidence-grounded agent |
| baselines/single_agent_kx.py | Repeats that same agent K times and averages probabilities |
| baselines/stat_logreg.py | Training-only scaling and regularized logistic model; feature transformation and coefficient export |
| models/calibrator.py | Six controlled logistic stacking variants and Bayesian logistic correction with Laplace covariance |
| models/cv.py | Optional expanding-window temporal/group-purged CV generator |
| models/training.py | Joins logged forecasts, checks provenance, chooses regularization on validation, fits and saves model artifacts |
| evaluation/metrics.py | Validated Brier score and clipped log loss with explicit weights |
| evaluation/calibration.py | Reliability-bin data and saved calibration plots |
| evaluation/stats_tests.py | Paired event-cluster bootstrap confidence intervals for score differences |
| evaluation/ablations.py | Predefined system contrasts and their scientific interpretation |
| evaluation/report.py | Held-out inference, coverage/fallback analysis, scoring, contrasts and report artifacts |
| dashboard/app.py | Local static HTML report renderer; a modest demonstration, not a full production dashboard |

## Entry points

| File under scripts/ | Purpose |
|---|---|
| run_ingest.py | Run selected public collection stages; retain successes and failures separately |
| import_data.py | Import licensed evidence/releases or explicitly reviewed archival table records |
| build_dataset.py | Build and export strict as-of snapshots; `--at now` is a pilot mode; `--market-id ID...` builds only the named markets (an unknown id is an error) |
| run_baselines.py | Produce market/50% forecasts without an LLM; fit market models only when labels/dates suffice |
| run_agents.py | Run frozen agent/baseline comparisons with optional small pilot limit; `--market-id ID...` forecasts only the named markets (applied before `--limit`; a requested market without a snapshot is reported as a failure) |
| freeze_protocol.py | Hash configuration, prompts, code and protocol; refuse overwriting an existing freeze |
| fit_calibrator.py | Fit models for one explicit forecast run; no test tuning |
| run_eval.py | Score the untouched test cohort or explicitly labelled exploratory analysis |
| run_demo.py | Generate and execute the entire isolated synthetic integration demonstration |

## Tests and notebooks

| File | Purpose |
|---|---|
| tests/test_metrics.py | Known scores, probability validation, calibration endpoints, paired/group-level uncertainty |
| tests/test_no_lookahead.py | Future evidence/labels, fake availability, settlement rejection, temporal/group purging and weights |
| tests/test_ingestion.py | YES/NO mapping, history settlement filtering, order books, RSS timestamps and timezone validation |
| tests/test_agents.py | Citation/schema checks, label/price exclusion, cache behavior, call budgets and mock isolation |
| tests/test_snapshots.py | Snapshot idempotence, stale prices and temporal retrieval |
| tests/test_models.py | Bayesian model numerical behavior and single-class logistic refusal |
| tests/test_contracts.py | Chat adapter response fixture, cyclic pagination rejection and document-version preservation |
| tests/test_pipeline.py | Complete integration: populate, build, forecast, resume, fit seven models and evaluate |
| tests/test_profiled_relevance.py | Hand-written profiles: matching, term rules, gate reasons and their order, late-arrival and changed-content retrieval as of the cutoff, refusal without evidence |
| tests/test_market_filter.py | `--market-id`: filtered build/forecast, unknown and missing markets, limit order, command-line options |
| notebooks/eda.ipynb | Guided inspection of actual database coverage, timestamps and snapshot counts |
| notebooks/results.ipynb | Load one experiment's manifest/scores; inspect comparisons and their limitations |

## Generated files and their meaning

| Location | Purpose |
|---|---|
| data/research/raw/ | Immutable API response envelopes with request URL, observation timestamp and payload |
| data/research/research.sqlite | Structured research tables; rebuildable from retained source records/imports |
| data/research/processed/snapshots.csv | Exported snapshot/label frame; labels never automatically enter agent prompts |
| data/research/processed/build_report.json | Eligibility counts and exclusions for the latest build |
| data/research/cache/llm/ | Original structured LLM requests/results, identity hashes and provider usage |
| artifacts/research/ingestion_latest.json | Latest collection success/failure summary |
| artifacts/research/agents_latest.json | Forecast run ID, newly made calls, cache hits and failures |
| artifacts/research/protocol_lock.json | Local experiment freeze; publish/commit externally for a stronger timestamp |
| artifacts/research/RUN_PREFIX/run_metadata.json | Code/prompt/config identity and runtime package versions |
| artifacts/research/RUN_PREFIX/*_manifest.csv | Exact train, validation, test and purged snapshot cohorts |
| artifacts/research/RUN_PREFIX/models.joblib | Fitted estimators; only load trusted local artifacts |
| artifacts/research/RUN_PREFIX/training_manifest.json | Fit/tuning counts, selected parameters, coefficients and status |
| artifacts/research/RUN_PREFIX/test_predictions.csv | Held-out per-snapshot predictions, coverage and labels when available |
| artifacts/research/RUN_PREFIX/evaluation.json | Scores, paired contrasts, coverage, calibration bins and limitations |
| artifacts/research/RUN_PREFIX/calibration.png | Static reliability visualization |
| artifacts/demo/ | Synthetic equivalents; never merge into real research data |
| data/live_pilot/ | Small real collection made during construction; not an adequate final cohort |
| examples/ | Compact shipped test evidence and explicitly labelled demo/pilot summaries |

`data/interim` is not required by this implementation: normalized intermediates live in SQLite. It can be
added if a later transformation needs a separate materialized stage. Empty folders and speculative
infrastructure are not prerequisites for reproducibility.
