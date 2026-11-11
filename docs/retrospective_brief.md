# Retrospective cohort: brief for approach B

> **SUPERSEDED (2026-11-12).** Kept for the record only. The current specification is `docs/retrospective_collection.md` (version 2). Do not follow this file: its cutoffs, price route and evidence status are out of date.

Draft of 2026-11-09. Audience: the person building the retrospective cohort (and any coding assistant they use).
Read this file, then `docs/data_spec.md`, `docs/protocol.md` and `docs/framework.md`. Nothing here changes the research protocol.

## 1. Why this exists
The project asks whether retrieval-grounded multi-agent forecasts beat raw and recalibrated Polymarket prices on US politics and
government markets (primary metric: event-weighted Brier; event-cluster bootstrap; see `docs/protocol.md`).

The **prospective** cohort is collected live on a VM since 2026-11-06 (prices every 5 min, news feeds every 15 min, with exact
local timestamps). It is the clean test, but most of its markets resolve in December and January, after the Term 1 report
(**due Wed 2026-12-02, before 13:00 HKT**; presentation Mon 2026-12-07, 16:00 to 16:30 HKT).

The **retrospective** cohort rebuilds the same inputs for markets that have already resolved, so the pipeline can be scored on a
real sample and so the report can compare the two collection modes honestly. It is **exploratory**: a replay cannot support a
clean foresight claim (protocol item 16), and retrospective prices have different semantics from the live midpoint (protocol item 4).

The report section this feeds is "retrospective vs prospective: challenges and differences". Measured numbers
(coverage, gaps, timestamp error, effort) are as valuable as a score.

## 2. The two tasks
**B1. Reconstruct the 3 Nov 2026 midterm markets.** Start with a small, reviewed set (about 5 to 10 events): which party wins
the House, which party wins the Senate, and a few state Senate or governor races. For each, rebuild point-in-time inputs at
cutoffs 24 h and 168 h before the scheduled deadline: market metadata, price history, news evidence, and the final resolution.

**B2. Fidelity test on 6 Nov to now.** For a period where the live record also exists, reconstruct the same inputs through the
retrospective route and compare them with what the live collector actually stored: price differences at matching times, whether
the archive route finds the documents the live collector saw (and what it misses or adds), and timestamp error (archive capture
time versus the collector's `observed_at`). This measures how good retrospective data is. Ask the project owner for the latest
database backup (`research-*.sqlite.gz`, about 143 MB). Do not ask for the VM's live file.

## 3. What a first probe found (2026-11-09, read-only requests from a laptop)
- **Closed midterm markets exist and are listable.** Gamma `GET /events?closed=true&end_date_min=2026-11-02T00:00:00Z&end_date_max=2026-11-05T00:00:00Z&order=volume&ascending=false`
  returned 50 events, including "Which party will win the House in 2026?" (9 markets), "Which party will win the Senate in 2026?" (9),
  "Balance of Power: 2026 Midterms" (5), and many state Senate and governor events (Texas, Michigan, Maine, Ohio, Iowa, Kansas, California and others).
  Each market carries `clobTokenIds`, `closedTime` and `umaResolutionStatus`. Sports markets are in the same list: filter them out.
- **Price history works for closed markets in one request form.** CLOB `GET /prices-history?market=<yes token>&interval=max&fidelity=60`
  returned about 570 points per market for the House and Texas Senate markets, from 2026-10-09 to the resolution time, with a median gap of
  3601 s and a maximum gap of about 5 h. The form with `startTs`, `endTs` and `fidelity` (the one `legacy_history` in `src/ingestion/clob.py` uses) returned
  **HTTP 400** for both `fidelity=60` and `fidelity=10`. The cause was not investigated: find out whether it is the range length, the fidelity value or a changed API.
- **Semantics are unknown.** The adapter labels these points `price_kind='history_unspecified'`. Work out whether a point is a last trade, a
  midpoint or something else before comparing it with the live midpoint, and say so in the report.
- **The Wayback index could not be assessed.** The CDX endpoint answered **HTTP 429** to the first request for each of five feeds, so there are
  no coverage numbers yet. Retry slowly (one request every 10 s or slower, a descriptive User-Agent), try the availability API, and consider
  other archives (for example Common Crawl News). A refusal is itself a finding for the report: record dates, endpoints and responses.
- **Deadline problem.** Gamma shows the CURRENT end date. `docs/data_spec.md` requires the deadline that was visible at the cutoff and says that updating an end date
  today cannot create valid old snapshots. Decide how to document the deadline for each market (for example creation metadata or an archived page) and state any
  market where it cannot be verified.

## 4. Rules the pipeline enforces (do not work around them)
- **Provenance.** Only `prospective`, `archive_verified` and `synthetic` records reach snapshots and evidence (`src/dataset/snapshots.py`, `src/retrieval/index.py`, `src/dataset/leakage_checks.py`).
  History fetched now is `retrospective_unverified` and stays excluded. To use it, import it with `python -m scripts.import_data --kind prices --archive-verified` and
  a real `raw_ref`; `--archive-verified` is an **attestation**, not a switch (`docs/data_spec.md`). Write down why each timestamp claim is justified (a review log) and keep the raw responses.
- **Timestamps.** UTC epoch seconds, timezone-aware on input. `available_at` is the earliest time this exact version could be known. For a price it must not be the fetch time if
  you attest it was public earlier, and for a document it should be the archive's capture time, not the article's publication time. Never rewrite a timestamp so that a record "fits".
- **No labels from prices.** Gamma's final `outcomePrices` are never labels. A resolution needs an official source or capture, `resolved_at <= label_available_at`, and the right YES mapping. A UMA "proposed" record is not final.
- **Evidence.** Published and available at or before the cutoff. The code does not fetch arbitrary article URLs.
- **Model contamination.** Historical replays are refused unless `llm.allow_retrospective` is true and `llm.model_frozen_before` is at or before the cutoff. The model's knowledge cutoff is
  not documented on Google's model page, so find a documented date or bracket it, and label any result exploratory.
- **Separate database.** Use a copy of `configs/config.yaml` with its own `database:` path (for example `data/retro/retro.sqlite`). Never write retrospective rows into the prospective
  database, and never pool the two cohorts. Use one writer per database file.
- **Scope.** Do not change `configs/market_filters.yaml`, the collector, the evidence profiles or the protocol. Social media is out of scope. Do not widen the market scope to inflate counts.

## 5. Practical constraints
- **Gemini is not reachable from Hong Kong IPs.** Data reconstruction can run on a laptop. Forecasting runs must happen on the US VM, so coordinate with the project owner. Use your own API key for tests; never share or commit `.env`.
- **Public APIs only, politely.** No login, no paywalled content, no hammering. Respect rate limits and robots rules. Set a descriptive User-Agent. Log every request failure with its date.
- **Record every decision** in `docs/decisions_log.md` before you look at outcomes: which markets, which cutoffs, which sources, which exclusions and why.

## 6. Deliverables and dates (HKT, internal)
| By | Deliverable |
|---|---|
| Fri 2026-11-13 | **Feasibility note (1 to 2 pages):** which price-history request form works and why the other returned 400; evidence-archive options tried with coverage numbers for 25 Oct to 4 Nov (captures per feed, spacing); a go or no-go and a list of markets to use |
| Fri 2026-11-20 | Reconstructed inputs for the chosen markets in the separate database, with import logs, saved raw responses (`raw_ref` targets) and the review log |
| Tue 2026-11-24 | Fidelity report (B2): price error, document overlap, timestamp error, with a table and a figure; inputs ready for the replay forecasts |
| Sat 2026-11-28 | Written sections for the report: method, data quality, challenges, limits |

If the 13 Nov note says the evidence side is not feasible, say so plainly. A documented negative result is acceptable. A silent gap is not.

## 7. Acceptance checklist (the project owner will check these)
1. Every imported price and document is `archive_verified` with a `raw_ref` that points to a saved file; anything else is excluded and counted.
2. No record is used with an `available_at` after the cutoff it supports. The leakage checks pass.
3. The prospective database is untouched: table counts before and after are identical.
4. `python -m unittest discover -s tests` still passes (132 tests at the time of writing).
5. Every choice has a dated row in `docs/decisions_log.md`. No `.env`, key or personal data is committed.
6. Any replay forecast respects `model_frozen_before`, or is clearly labelled exploratory.
7. The review log states, for each source, how its timestamps were justified and what could not be verified.

## 8. Known limits to state in the report
Midterm markets are strongly correlated (one national result drives many races), so the effective sample is far smaller than the market count. They are election markets, a different
population from the prospective cohort (government actions). Archive coverage may differ by date, so fidelity on 6 Nov onward does not prove equal fidelity for late October.
