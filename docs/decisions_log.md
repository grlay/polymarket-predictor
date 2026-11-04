# Design decision log

| Date | Decision | Reason / consequence |
|---|---|---|
| 2026-10-27 | Implement the requested package without placing trades | Matches the planning report's research scope |
| 2026-10-27 | Distinguish implementation completion from empirical completion | Missing credentials, clean history and mature labels cannot be fabricated |
| 2026-10-27 | Use SQLite, immutable JSON and CSV manifests | Fits a single-machine student project and supports auditing |
| 2026-10-27 | Keep observation and availability separate | Publication dates and present metadata are insufficient for historical replay |
| 2026-10-27 | Default historical API downloads to unverified provenance | Prevents silent look-ahead from a successful API response |
| 2026-10-27 | Implement Data API v2 and explicitly selectable legacy history separately | Current docs describe migration; silent endpoint switching would alter semantics |
| 2026-10-27 | Exclude settlement points and ambiguous binary mappings | Prevents labels masquerading as input prices |
| 2026-10-27 | Move discovery from newest ID to recent trading volume | First real pilot found empty books on newly created markets; research selection still needs freezing |
| 2026-10-27 | Use prospective RSS summaries as baseline news source | Real pilot succeeded; GDELT returned 503 and is not treated as guaranteed access |
| 2026-10-27 | Use stateless sparse retrieval before trying dense embeddings | Avoids extra cost and future-corpus vocabulary/IDF fitting; leaves semantic retrieval as a measurable extension |
| 2026-10-27 | Keep market prices out of independent agent contexts | Makes AI incremental information clearer and reduces anchoring |
| 2026-10-27 | Include repeated-call single-agent comparator | Extra LLM calls alone could explain an apparent multi-agent gain |
| 2026-10-27 | Primary aggregation is regularized logistic stacking | Simple, inspectable and easier to fit with limited labelled data |
| 2026-10-27 | Add Bayesian logistic correction as a sensitivity model | Meets the proposed Bayesian direction without mislabelling ordinary logistic regression |
| 2026-10-27 | Use event-balanced loss and group-purged time splits | Repeated horizons/contracts are not independent observations |
| 2026-10-27 | Require label maturity at fit boundaries | An earlier forecast date alone does not make its future outcome a valid training label |
| 2026-10-27 | Include a synthetic offline backend with strict labels | Enables reproducible end-to-end testing without paid APIs; cannot support scientific conclusions |
| 2026-10-27 | Leave actual categories and split dates for informed selection | Available market coverage and observation windows have not yet been established |
| 2026-10-27 | Version config, prompts and source code in run identity | Avoids mixing predictions produced by materially different experiments |
| 2026-11-05 | Separate `configs/collect.yaml` from `config.yaml` | Watchlist edits must not change the forecasting run identity |
| 2026-11-05 | Discover markets by Gamma event tag slugs (draft watchlist) instead of top volume | `politics` alone returned non-US and tweet-count markets; scope is US politics/government; watchlist awaiting supervisor review |
| 2026-11-05 | Cap sub-markets per event (4) and markets per deadline date (25) | 80% of candidates ended 31 Dec / 1 Jan; their cutoffs fall in one week and cannot be split chronologically; the caps are logged as exclusions |
| 2026-11-05 | Resolution labels read from `/v2/resolutions` `status`/`price`; `proposed` is never a label | The earlier adapter expected a `payouts` array the live API does not return, so no label would ever have been stored |
| 2026-11-05 | Raw responses bundled per profile per day (gzip members, byte-range `raw_ref`) | One file per response would create millions of files over the collection period |
| 2026-11-05 | GDELT: 6 s spacing, long backoff, throttle text treated as failure, empty body as zero results | GDELT answers 429 or a text body when throttled; failures and empty searches must be distinguishable |
| 2026-11-05 | Track up to 300 markets (was 150) | A quote is under 1 KB, and selection caps can be applied at analysis time; markets never quoted cannot be recovered |
| 2026-11-05 | Run identity = forecast settings (LLM, retrieval, mode), prompts, model and forecasting code only | Paths, split dates, budgets and collector code must not orphan forecasts or void a freeze; hashes are now identical on Windows and Linux |
| 2026-11-05 | `run_agents --limit N` takes the newest eligible snapshots; stale snapshots are skipped and counted, not failed | Live forecasts must be generated within the delay window; the old behaviour picked the oldest snapshots and failed every one |
| 2026-11-05 | `--at` pilot snapshots are marked `*_pilot` and excluded from fitting and evaluation; training uses only the run's own dataset configuration | Protocol item 3: pilots must never be pooled with fixed-horizon research snapshots |
| 2026-11-05 | Agent context carries ISO UTC dates plus the deadline instead of epoch integers | Models misread raw epoch seconds |
| 2026-11-05 | Repeated-call baseline samples at `k_temperature` (0.7) | At temperature 0 the repeated calls are identical, which makes the specialization comparison unfair |
| 2026-11-05 | LLM client retries transient errors and unusable output within bounds; truncated output is an error; one clean stop when the call budget is spent | A single 429 or malformed reply must not permanently lose a time-critical forecast |
| 2026-11-05 | Market price age limit 900 s for the research config (was 21600 s) | The benchmark price must be as fresh as the evidence the agents see |

Append dated decisions when changing market scope, prompts, model, data access, timestamp rules or
statistical methods. Record changes before looking at affected test outcomes. Retain prior versions in Git.
