# Retrospective collection: work plan and specification

Version 2, 2026-11-12. Version 1 was dated 2026-11-10. Version 2 confirms D1 to D4 and D6 (see `docs/decisions_log.md`, rows of 2026-11-12)
and folds in the WP0 probe results (`docs/retrospective_feasibility.md`).
Written for: the teammate who builds the retrospective cohort, and the coding assistant they use.
Owner of the repository and of every final decision: the project owner. This file replaces `docs/retrospective_brief.md`.

Items marked **[OWNER CONFIRMS]** are proposals. Do not act on them until the owner has confirmed them in writing,
for example in a chat message or a comment on your pull request.

---

## 0. Read this first (including the coding assistant)

**Read in this order:** this file, then `docs/data_spec.md`, `docs/protocol.md` (items 4 and 16), `src/db/schema.sql`,
`scripts/import_data.py`, `src/dataset/snapshots.py`, `src/dataset/leakage_checks.py` and `configs/relevance_profiles.yaml`.

**Hard rules. They are not optional, and none may be "worked around":**

1. **Never touch the live research database** (`data/research/research.sqlite`) or the VM.
   - Work only in a separate database: `data/retro/retro.sqlite`.
   - For the fidelity test (WP8), open the owner's backup copy read-only.
2. **Do not edit anything under `src/`, `configs/`, `tests/` or `scripts/`.**
   - Put all your code, configs and notes in a new folder, `retrospective/`.
   - If shared code seems to need a change (for example a bug in `src/ingestion/clob.py`), write the problem and the
     proposed fix in `retrospective/NOTES.md` and tell the owner. The owner makes shared-code changes.
   - Why it matters: `configs/relevance_profiles.yaml` and the code under `src/agents` are hashed into the identity of
     every forecast run. Editing them, even harmlessly, would split the live pilot (running daily to 15 Nov) into two
     runs.
3. **Work on a branch and send a pull request** (`git checkout -b retro/collection`). Never push to `main`; the VM pulls
   `main`.
4. **Commit no data**: no databases, no raw downloads, no `.env`, no API keys. `data/` and `*.sqlite*` are already in
   `.gitignore`. Keep raw files under `data/retro/raw/` and hand them to the owner as a zip at the end.
5. **No LLM calls.** Your job is the inputs. The owner runs the forecasts on the US VM, because Gemini is blocked from
   Hong Kong.
6. **Never change a timestamp so that a record fits a cutoff.** If a record is not admissible, exclude it and count it.
   An honest gap is a result. A patched one is misconduct.
7. **Log every choice before you look at outcomes.** Write it in `retrospective/decisions_retro.md` (format in section 7).
8. **Be polite to public servers.**
   - Use a descriptive User-Agent (`HX2605-academic-research/0.1`).
   - Send at most 1 request every 2 s to Polymarket, and at most 1 every 10 s to the Internet Archive.
   - On HTTP 429, back off and do not retry in a tight loop.
   - Log every failure, with its date, endpoint and status code.

**Stop and ask the owner** (do not guess) when:
- a rule above blocks you;
- a record's timestamp cannot be justified;
- the market list or an exclusion would change after you have seen prices or outcomes;
- a source needs a login or payment;
- anything would require editing shared files.

---

## 1. Why this exists, and what counts as success

The project tests whether retrieval-grounded LLM forecasts beat Polymarket prices on US politics markets.

**The two cohorts:**
- **Prospective cohort:** collected live since 2026-11-06. It is the clean test, but most of its markets resolve after the
  report deadline (**Wed 2026-12-02**).
- **Retrospective cohort:** rebuilds the same inputs for markets that have already resolved. That gives the report a
  scored sample and a measured comparison of the two collection methods.

A replay can never prove foresight, so every retrospective score is labelled **exploratory**.

The report section this feeds is "retrospective vs prospective collection: method, data quality, challenges".
**Measured numbers are worth as much as a score:** coverage, gaps, timestamp error, effort and refusals.

| Level | What it delivers | Enough for the report? |
|---|---|---|
| **Minimum** (must) | Feasibility note; prices, metadata and labels for the selected markets in `retro.sqlite`; price fidelity (WP8a); the evidence result, whether positive or negative, with numbers | Yes: a documented, honest feasibility study |
| **Target** | Minimum, plus archived news evidence imported, snapshots built, and document fidelity (WP8b) | Yes: the owner can run exploratory replay forecasts |
| **Stretch** | A second archive source (Common Crawl News), or volume and liquidity at the cutoff from trade history | Only if the target is done early |

---

## 2. What is already known (probe of 2026-11-09, read-only, from a laptop)

**Closed midterm markets can be listed.** This request returned 50 events, including:
- "Which party will win the House in 2026?"
- "Which party will win the Senate in 2026?"
- "Balance of Power: 2026 Midterms"
- many state Senate and governor races.

```
GET https://gamma-api.polymarket.com/events?closed=true&end_date_min=2026-11-02T00:00:00Z&end_date_max=2026-11-05T00:00:00Z&order=volume&ascending=false
```

Sports events are mixed into the results. Each market carries `clobTokenIds`, `outcomes`, `closedTime` and
`umaResolutionStatus`.

**Price history:**
- This form **works** for closed markets:
  ```
  GET https://clob.polymarket.com/prices-history?market=<YES token>&interval=max&fidelity=60
  ```
  It returned about 570 points per market, from 2026-10-09 to resolution. The median spacing was 3601 s and the
  largest gap about 5 h.
- The `startTs`/`endTs`/`fidelity` form, which the repo's `legacy_history` uses, returned **HTTP 400**. The cause is
  unknown.
- What each point means (last trade, midpoint or something else) is unknown.

**Evidence archive:** the Wayback Machine CDX API answered **HTTP 429** to the first request for each feed. Coverage is
unknown. This is the main risk of the whole task.

**Deadlines:** Gamma shows the **current** end date. The spec needs the deadline that was visible at the cutoff (see WP2).

**Update from the WP0 probe (2026-11-10; full note in `docs/retrospective_feasibility.md`). Verdict: PARTIAL GO.**
- **Prices:** the HTTP 400 meant "interval is too long". `startTs`/`endTs` ranges of up to 7 days work, and with
  `fidelity=1` they return **1-minute points**. `interval=max` reaches back only to about 2026-10-10. The 900 s
  price-age rule can therefore stay as it is.
- **Deadline:** Gamma shows end date `2026-11-04T04:59:00Z` (midnight US Eastern at the end of election day) on the
  House, Senate and Alabama Senate markets.
- **News:** `web.archive.org` returns 429 from the laptop (Hong Kong) but answers from the VM (US): 14 of 15 feeds.
  Coverage before the 168 h cutoff is good (four political feeds); before the 24 h cutoff only Politico is fresh.
  Government feeds have no captures.

---

## 3. Fixed design

These are the parts the owner must confirm before WP1. They are fixed in advance so that nothing can be tuned to outcomes.

**D1. Cutoffs (CONFIRMED 2026-11-12).** Use two cutoffs: **2026-10-28T04:59:00Z** (168 h) and **2026-11-03T04:59:00Z** (24 h).
- They are the protocol horizons `[24, 168]` (the same as the prospective cohort), measured back from the markets' end
  date **2026-11-04T04:59:00Z** (midnight US Eastern at the end of election day), as shown by Gamma.
- The end date is the market's deadline, not its settlement time; settlement can come later and does not matter here.
- Whether that end date was visible before each cutoff is checked per market in WP2 (archived market page); where it
  cannot be checked, the market stays in and is reported as `deadline_verified=no`.
- Build the snapshots with `--at`, so they are labelled `retrospective_pilot` and are never pooled with fixed-horizon
  research snapshots.
- Version 1 used 2026-10-27 and 2026-11-02, counted from the start of election day. That is replaced: the protocol counts
  back from the market's own deadline.

**D2. Market selection rule (CONFIRMED 2026-11-12), applied mechanically and logged before any import:**
1. **Candidate events:** the events returned by the Gamma query in section 2, with pagination until it is exhausted.
   Keep an event only if its title is about one of:
   - control of the House;
   - control of the Senate;
   - "Balance of Power";
   - a 2026 US Senate race;
   - a 2026 governor race.

   Drop everything else (sports, House-district races, other topics), and record each dropped event with its reason.
2. **Candidate markets:** every binary market in those events whose Gamma end date is 2026-11-04T04:59:00Z. A market
   with any other end date is listed and excluded with the reason "different deadline".
3. **Eligibility, at each cutoff separately:** the research filter's price band **0.02 to 0.98** and the 900 s price-age
   rule, applied by `build_dataset` exactly as for the prospective cohort.
   - A market inside the band at one cutoff but not the other contributes one snapshot. Safe seats drop out at both
     cutoffs; that is intended, because there is nothing left to forecast.
   - `selection.csv` records the YES price at **both** cutoffs and whether each would pass.
   - Version 1 used 0.05 to 0.95 at the 24 h cutoff only. That band had no basis and is dropped.
4. **No cap and no random draw.** Every eligible market is used. Replays use Gemini 3.5 Flash-Lite (500 free requests per
   day), so cost no longer limits the count, and more races mean more independent clusters.
5. **Freeze:** once the list is written, nothing is dropped or added afterwards. Excluded or failed markets are reported,
   never silently removed.

The rule uses prices that were public at the cutoff, never outcomes. The election results are publicly known, so the
mechanical rule is what protects the study from cherry-picking.

**D3. Event clusters for scoring (CONFIRMED 2026-11-12):**
- One cluster called "national control" holds the House, Senate and Balance-of-Power events.
- Each state race is its own cluster.

Write this as a CSV: `retrospective/clusters.csv` with columns `market_id,cluster`. The effective sample size is the
number of clusters, not the number of markets. Say this in every write-up.

**D4. Price admission:** each history point becomes one `prices` row.
- Download with `startTs`/`endTs` ranges of at most 7 days and `fidelity=1` (1-minute points; WP0).
- `price_kind='history_unspecified'`, until WP8a establishes what the points mean.
- `resolution_seconds` = the fidelity in seconds (60 for `fidelity=1`).
- `timestamp` = the point's time.
- `available_at = timestamp + resolution_seconds`. This conservative choice assumes the point summarises the bucket
  that starts at its timestamp. If WP3 shows the timestamp marks the end of the bucket, use `available_at = timestamp`
  and log why.
- `observed_at` = your download time.
- `provenance='archive_verified'`.
- `raw_ref` = the path of the saved raw response.
- The price-age rule stays at **900 s**, as in the live cohort. Version 1 allowed 7200 s because only hourly points
  seemed available; 1-minute points remove the need for that deviation.

**D5. Evidence:**
- **Sources:** the same feeds the live collector reads (`configs/collect.yaml`, `news.rss_urls`, 15 feeds). Using the
  same sources is what makes the comparison fair.
- **Route:** archived captures of those feed URLs, Wayback first.
- **Field values:**
  - `available_at` = the time of the first capture that contains the item (never the article's publication time);
  - `published_at` = the item's `pubDate`;
  - `body` = the feed's summary text (`content_kind='feed_summary'`), exactly as the live collector stores it;
  - `raw_ref` = the saved capture file plus the full Wayback URL.
- **Exclusion:** drop any item whose `pubDate` is later than the capture time, and count it.
- **Window:** from 2026-09-28 (30 days before the first cutoff, the configured lookback) to 2026-11-03T04:59:00Z.
- **Route:** run Wayback requests from the US VM; the laptop's network gets HTTP 429 (WP0).

**D6. Labels (CONFIRMED 2026-11-12):**
- `outcome` = Polymarket's final resolution, taken only when `umaResolutionStatus` shows it as resolved (a proposal is
  not final).
- Cross-check each label against an official or AP result. Put the URL in `source`, and save a copy of the page.
- `resolved_at` = the resolution time.
- `label_available_at` = your observation time (conservative; the spec allows this).
- `raw_ref` = the saved Gamma response.
- A Gamma `outcomePrices` value of 1 or 0 is **not** a label on its own.
- A market that is not final by 20 Nov stays unlabelled and counted. Never fill it in.

**D7. Relevance profiles:** the forecasting pipeline only accepts a document for a market that has a relevance profile.
- Draft one profile per selected **storyline** in `retrospective/relevance_profiles_retro.yaml`, using the same format as
  `configs/relevance_profiles.yaml`.
- Write each from the market **question text only**, with **one template** for all races:
  - entities = the state name plus the candidates named in the question or the event's market list;
  - institution terms = "Senate", "governor" or "House" as fitting;
  - action terms = a fixed list such as "election", "race", "poll*", "campaign*", "vote*", "ballot*", "candidate*".

  A single template is justifiable. Hand-tuning each race is not.
- **Do not copy them into `configs/`.** The owner merges them after the pilot ends (15 Nov).

---

## 4. Setup

```bash
git checkout main && git pull
git checkout -b retro/collection
python -m unittest discover -s tests        # must pass before you start; note the count
mkdir retrospective
```

1. Create `retrospective/config_retro.yaml` as a copy of `configs/config.yaml`, changing only these keys:
   - `data_dir: data/retro`
   - `artifact_dir: artifacts/retro`
   - `database: data/retro/retro.sqlite`
   - `mode: retrospective`
   - `market_filters: retrospective/market_filters_retro.yaml`
   - `llm.allow_retrospective: true` and `llm.model_frozen_before: 2026-03-31` (replay model `gemini-3.5-flash-lite`,
     knowledge cutoff March 2026; used only by the owner on the VM)
   - `dataset.max_price_age_seconds` stays 900 (D4)
2. Create `retrospective/market_filters_retro.yaml` as a copy of `configs/market_filters.yaml`, changing only:
   - `min_liquidity: 0`
   - `min_volume: 0`

   Liquidity and volume at a past cutoff are unknown. Today's final values must not be used to filter the past
   (`docs/data_spec.md`). The selection rule D2 does the filtering instead. Log this.
3. The database is created automatically on the first import (`src/db/loaders.py` runs the schema).

---

## 5. Work packages

Each work package lists its steps, its outputs, and the condition for calling it done.

### WP0. Feasibility probe (due Fri 13 Nov)
1. **Price route.** Repeat the `interval=max` request for 3 markets. Then find out why the `startTs`/`endTs` form returns 400:
   - vary the range length (1, 7 and 30 days);
   - vary `fidelity` (1, 10, 60);
   - try with and without `interval`.

   Also find out whether a finer fidelity (1 or 10 minutes) works with `interval=max` or with a short range.
   Record each request and its status in a table.
2. **Evidence route.** For each of the 15 feeds, ask the Wayback CDX API for captures from 2026-09-27 to 2026-11-10:
   ```
   GET https://web.archive.org/cdx/search/cdx?url=<feed URL>&from=20260927&to=20261110&output=json&fl=timestamp,statuscode,digest
   ```
   - Send at most 1 request every 10 s. If you get 429, wait at least 60 s and then try once more.
   - If it still fails, also try the availability API (`https://archive.org/wayback/available?url=...&timestamp=...`).
   - Report per feed: the number of captures, captures per day, the largest gap, and whether captures exist before each
     cutoff.
3. **Second source.** Only if Wayback coverage is poor: one hour, strictly time-boxed, to check whether Common Crawl News
   (CC-NEWS) has the late-October files and how big they are. Do not download them.

**Output:** `retrospective/feasibility_note.md`, 1 to 2 pages, containing:
- the two tables above;
- a **go / partial / no-go** decision for evidence;
- the candidate market list from D2, which needs prices only.

**Done when** the owner has the note by Fri 13 Nov. A no-go is an acceptable result if it is backed by the numbers.

### WP1. Market selection (after the owner confirms D1 to D3)
1. Run the D2 query and save the raw responses under `data/retro/raw/gamma/`.
2. Apply D2 in a script: `retrospective/select_markets.py`.
3. Write two files:
   - `retrospective/selection.csv`: every candidate, with the YES price at the 24 h cutoff, included yes or no, and the
     reason;
   - `retrospective/clusters.csv`.
4. Add a row to `retrospective/decisions_retro.md` **before** looking at any label.

**Done when** the list is frozen and the owner has acknowledged it.

### WP2. Markets and metadata
1. **`markets` rows:** `market_id`, `condition_id`, `event_group` (the Gamma event id), `yes_token`, `no_token`,
   `yes_index` and `source_kind='real'`.
   - Map YES explicitly from `outcomes` and `clobTokenIds`. **Never assume the first token is YES.**
2. **One `market_versions` row per market:** `question`, `rules` (the description text), `category`, `scheduled_end`,
   `is_open=1`, `liquidity=null`, `volume=null`, `provenance='archive_verified'` and `raw_ref`.
3. **Deadline and rules evidence:** search the Wayback Machine for a capture of the market's Polymarket page dated before
   the first cutoff.
   - **If one exists,** take `scheduled_end` and the rules from it, and set `available_at` to the capture time.
   - **If none exists,** use today's Gamma values, set `available_at` to the market's `createdAt`, and mark
     `deadline_verified=no` in the review log (section 6). Report how many markets fall in each group. This is a stated
     limitation, not a reason to drop the market.
4. Import the markets first, then the versions:
   ```
   python -m scripts.import_data --config retrospective/config_retro.yaml --kind markets --file retrospective/out/markets.jsonl
   python -m scripts.import_data --config retrospective/config_retro.yaml --kind market_versions --archive-verified --file retrospective/out/market_versions.jsonl
   ```

### WP3. Prices
1. For each selected market, download the history (the finest fidelity that WP0 showed works) and save each raw response.
2. Convert it to `prices` JSONL according to D4, with a stable `price_id`, for example `retro:<market_id>:<timestamp>:<fidelity>`.
3. Import it:
   ```
   python -m scripts.import_data --config retrospective/config_retro.yaml --kind prices --archive-verified --file retrospective/out/prices.jsonl
   ```
4. Report, per market: the point count, the largest gap within 48 h before each cutoff, and the age of the last point at
   each cutoff.

### WP4. Evidence (only if WP0 says go or partial)
1. For each feed, download every capture inside the D5 window in raw form (the `id_` URL form:
   `https://web.archive.org/web/<timestamp>id_/<feed URL>`). Save each file.
2. Parse the RSS or Atom items:
   - the URL, title and summary;
   - `pubDate`;
   - the first capture time containing the item.
3. Build `documents` JSONL according to D5, with these field values:
   - `source` = the feed's domain;
   - `source_type='news'`;
   - `source_kind='real'`;
   - `provenance='archive_verified'`;
   - `observed_at` = the download time.
4. Import it:
   ```
   python -m scripts.import_data --config retrospective/config_retro.yaml --kind documents --file retrospective/out/documents.jsonl
   ```
5. Report, per feed: the items recovered, the items excluded (and why), and the captures before each cutoff.

### WP5. Labels
1. Fetch the final state of each selected market and save it.
2. Apply D6 and build the `resolutions` JSONL.
3. Import it **last**:
   ```
   python -m scripts.import_data --config retrospective/config_retro.yaml --kind resolutions --archive-verified --file retrospective/out/resolutions.jsonl
   ```
4. List every unresolved or disputed market.

### WP6. Relevance profiles (draft only)
1. Write `retrospective/relevance_profiles_retro.yaml` according to D7, using the single template.
2. Run a dry check of how many imported documents each profile would accept before each cutoff. Use a script in
   `retrospective/`; do not change `src/retrieval/relevance.py`.
3. Report the counts. Do not tune terms to raise them. If a profile accepts nothing, report that.

### WP7. Snapshots
For each cutoff in D1, build the snapshots:

```
python -m scripts.build_dataset --config retrospective/config_retro.yaml --at 2026-10-28T04:59:00Z --market-id <ids from selection.csv>
python -m scripts.build_dataset --config retrospective/config_retro.yaml --at 2026-11-03T04:59:00Z --market-id <ids from selection.csv>
```

Report the built and excluded snapshots, and the reason for each exclusion (the `pipeline_events` table records them).
**Expected:** every candidate market produces a snapshot at each cutoff where it passes D2 step 3, or a stated reason why
not (`price_range`, `stale_price`, ...).

### WP8. Fidelity test (B2: how good is retrospective data?)
Use the owner's backup of the live database (`research-*.sqlite.gz`). Open it **read-only**:

```python
sqlite3.connect('file:<path>?mode=ro', uri=True)
```

The comparison window is from 2026-11-06T17:00:00Z to the backup's date.

**8a. Prices.**
1. **Sample:** 20 markets from the backup that have live quotes over the whole window and were still open at the end of
   the window. Draw them with `random.Random(2605)` from the list sorted by `market_id`.
2. Fetch their history through the WP3 route, into **separate files**. Never write to either database.
3. At each history timestamp, compare against two values:
   - the live midpoint stored nearest in time (at most 5 min away);
   - the live last-trade value, if the backup stores one.
4. **Report:** the median, 90th percentile and maximum absolute difference, and which live quantity the history matches
   best. That match answers the open question about what the history points mean (D4).

**8b. Documents.** For the same 15 feeds, over the same window, compare the Wayback items with the live documents
(match on URL):
- **Recall:** the share of live items that appear in some capture.
- **Additions:** archive items that the live collector never stored.
- **Delay:** first capture time minus live `observed_at`.
  - A positive delay means the archive knew later, so retrospective `available_at` is conservative.
  - A negative delay means the live collector was late.
- **Edits:** the share of matched items whose title or summary differs.

**Output:** `retrospective/fidelity_report.md`, with one table and one figure per part.

Say plainly that fidelity measured on 6 to 10 Nov may differ from fidelity in late October (archive coverage changes
over time).

### WP9. Write-up for the report
`retrospective/report_sections.md`, about 4 to 5 pages, covering:
- method;
- data quality (all numbers from WP0 to WP8);
- challenges and failures (with dates);
- limits;
- what a full retrospective cohort would need.

Use plain past tense. Write "exploratory" for any score and "fidelity" for WP8. Never write "accuracy" or "backtest
performance".

---

## 6. Review log (required for every `archive_verified` record)

The import flag `--archive-verified` is an **attestation** that a person checked the timestamps. Keep
`retrospective/review_log.md`, with one section per source type (Gamma metadata, CLOB history, each feed, labels).
Each section states:
- how each timestamp was set and why it is justified;
- what was spot-checked (at least 5 records per source, by hand, against the saved raw file);
- what could not be verified (for example, the count of markets with `deadline_verified=no`).

---

## 7. Decision log format

Use `retrospective/decisions_retro.md`, as a table with the columns: `Date | Decision | Reason | Known before outcomes? (yes/no)`.

Write a row before acting on each of these:
- confirmation of D1 to D7;
- every exclusion;
- every change of route;
- every deviation from this file.

The owner copies the rows into `docs/decisions_log.md` when merging.

---

## 8. Timeline (all dates HKT)

| By | You deliver | The owner does |
|---|---|---|
| Wed 11 Nov | Setup done, tests pass, branch pushed | Confirms D1 to D7, or changes them |
| **Fri 13 Nov** | **WP0 feasibility note** (hard checkpoint) | Decides go / partial / no-go |
| Tue 17 Nov | WP1 to WP3 imported (markets, metadata, prices) | Checks the selection and a sample of records |
| **Fri 20 Nov** | WP4 to WP7: evidence, labels, profile drafts, snapshots, plus the review log and the raw zip | Merges the PR; adds the profiles after the pilot; runs the replay forecasts on the VM (21 to 24 Nov) |
| Tue 24 Nov | WP8 fidelity report | Scores the replays (exploratory) |
| Sat 28 Nov | WP9 report sections | Edits them into the report (due Wed 2 Dec) |

If the evidence side is a no-go on 13 Nov, the plan becomes: WP1 to WP3, WP5 and WP8a, plus a written negative result
for the evidence, with its numbers. That is still a complete, reportable contribution.

---

## 9. Acceptance checklist (the owner checks each item before merging)

1. `python -m unittest discover -s tests` passes with the same count as at setup.
2. No file outside `retrospective/` and `docs/retrospective_collection.md` changed (`git diff --stat main`).
3. No data, database, raw file, `.env` or key is committed.
4. Every imported price, version and document is `archive_verified`, with a `raw_ref` that points to a file in the raw zip.
5. Leakage holds: no snapshot uses a record whose `available_at` is after its cutoff. `build_dataset` enforces this; check
   that it raised no `Future ...` errors.
6. The live database was never opened for writing (the backup copy was opened with `mode=ro`).
7. `selection.csv`, `clusters.csv`, the review log and the decision log exist, and their rows predate the label import.
8. Every excluded market or record is counted, with its reason. The numbers in the write-up match the files.

---

## 10. Limits to state in the report (known in advance)

- **Correlation:** midterm markets are strongly correlated (one national swing drives many races). The effective sample
  is the number of clusters, perhaps 10 or fewer.
- **A different population:** election markets are not the government-action markets of the prospective cohort.
  Results do not transfer automatically.
- **History semantics:** retrospective prices are 1-minute history of unconfirmed meaning. The prospective benchmark is a
  live 5-minute midpoint (protocol item 4).
- **Stale news at 24 h:** before the 3 Nov cutoff only Politico has a fresh capture, so the 24 h replay sees older news
  than the live collector would have, against a fresh price (WP0).
- **Deadlines and rules:** these could not be verified for markets without an early page capture.
- **Archive coverage** varies by feed and by date. Feed summaries are short, and article text is not used.
- **Contamination:** the model used for the replays must have a documented knowledge cutoff before 2026-10-28. Otherwise
  its scores may reflect knowledge of the results. Gemini 3.5 Flash-Lite's documented cutoff is March 2026; this is a
  vendor statement, not checked empirically (decision log, 2026-11-12).
