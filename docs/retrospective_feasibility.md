# Retrospective cohort: feasibility note (WP0)

Date: 2026-11-10. This note answers the WP0 questions in `docs/retrospective_collection.md`. It records only read-only
probes of public endpoints. Nothing was imported, and no LLM was called.

**Verdict: PARTIAL GO.**
- **Prices:** solved, at 1-minute resolution.
- **News evidence:** usable from 4 to 5 political news feeds.
- **Weak:** coverage in the final days before the election (the 24 h cutoff), the government feeds, and the news side of
  the fidelity test.

## 1. Cutoffs (revised from the draft)

Gamma gives the midterm markets the end date **2026-11-04T04:59:00Z**: midnight US Eastern time at the end of election
day (3 markets checked: House, Senate, Alabama Senate). The protocol's standard horizons therefore give:

| Horizon | Cutoff (UTC) | Meaning |
|---|---|---|
| 168 h | 2026-10-28T04:59:00Z | One week before the end of election day |
| 24 h | 2026-11-03T04:59:00Z | Midnight Eastern time at the start of election day, before polls open |

This replaces the draft's own anchor (27 Oct and 2 Nov). The protocol's horizon rule is used unchanged.

The end date is the one shown today. Confirming that the same end date was visible before each cutoff is a WP2 task.

## 2. Price history (CLOB `prices-history`)

Probed from a laptop on 2026-11-10, 11:43 to 11:49 UTC: 55 requests over 3 resolved markets.

| Request form | Result |
|---|---|
| `interval=max`, `fidelity=60` | 200: about 540 points, hourly, 2026-10-10 to resolution, largest gap 5 h |
| `interval=max`, `fidelity=10` or `1` | 200: about 3,350 points, every 10 min (1 is treated as 10), largest gap about 70 min |
| `interval=1w`, `fidelity=1` | 400: "minimum 'fidelity' for '1w' range is 5" |
| `interval=1d`, `fidelity=1` | 200, but 0 points (closed market) |
| `startTs`/`endTs` over 1 or 7 days, `fidelity=1` | **200: 1-minute points** (7 days: 9,765 points, median gap 60 s, largest 9 min) |
| `startTs`/`endTs` over 1 or 7 days, `fidelity=10` or `60` | 200: 10-minute or hourly points |
| `startTs`/`endTs` over 30 days, any fidelity | **400: "'startTs' and 'endTs' interval is too long"** |

**Findings:**
1. The earlier HTTP 400 was caused by the **range length**, not a broken API. Ranges of 7 days or less work.
2. With 1-minute points, the protocol's 900 s price-freshness rule can be kept unchanged. The draft's 7,200 s deviation
   is no longer needed.
3. `interval=max` reaches back only to about 2026-10-10, even for markets created in 2025. That covers both cutoffs and
   their 30-day lookback for prices only partly. The lookback matters for the lag features; fetch them in 7-day windows.
4. What a point means (last trade or midpoint) is still unknown. The price fidelity test (WP8a) will measure it against
   the live midpoints.

## 3. News archive (Wayback Machine CDX index)

- **From the laptop (Hong Kong network), 2026-11-10, 11:51 to 12:08 UTC:**
  - every `web.archive.org` request returned **HTTP 429**: CDX for 7 feeds with 3 attempts each and 60 s pauses, the
    timemap and the calendar page;
  - only `archive.org/wayback/available` answered (200, no snapshot listed).

  This looks like a block on that network address, not ordinary throttling. It is recorded as a failure.
- **From the US VM the same evening:** 15 feeds, 1 request every 10 s. 14 answered with 200; the Guardian answered
  **403**, which usually means the site has opted out of the archive.
- Window queried: 2026-09-27 to 2026-11-10. "Versions" = distinct content digests among HTTP-200 copies.

| Feed (live collector list) | Versions | Before 28 Oct | 28 Oct to 3 Nov | Latest before 3 Nov 04:59Z | 6 to 10 Nov |
|---|---|---|---|---|---|
| BBC World | 272 | 234 | 38 | 02 Nov 00:18 | 0 |
| NYT Politics | 1540 | 1535 | 5 | 01 Nov 03:10 | 0 |
| Al Jazeera (all) | 963 | 952 | 5 | 31 Oct 08:09 | 2 |
| Politico | 50 | 32 | 9 | **03 Nov 04:21** | 17 |
| BBC US & Canada | 5 | 5 | 0 | 27 Oct 22:56 | 0 |
| Axios | 301 | 301 | 0 | 19 Oct 08:18 (stops) | 0 |
| Federal Reserve | 5 | 2 | 3 | 29 Oct 22:51 | 0 |
| CBS Politics | 1 | 1 | 0 | 16 Oct 06:34 | 0 |
| The Hill, White House, Federal Register, DOJ, Defense, ABC | 0 | 0 | 0 | none | 0 |
| The Guardian US politics | refused (403) | | | | |

**Findings:**
1. **The 168 h cutoff is well covered.** The latest copies before the cutoff were:
   - BBC World: 1 min before;
   - Al Jazeera: about 5 h before;
   - BBC US: 6 h before;
   - NYT Politics: 10 h before.

   These are the political sources an election market needs.
2. **The 24 h cutoff is weaker.** Only Politico has a copy within the last hour. The other gaps are 29 h (BBC World),
   50 h (NYT) and 69 h (Al Jazeera). A feed shows only its latest headlines, roughly the last 1 to 2 days, so a replay
   misses most news from the final 1 to 3 days, while the market price it is compared with is fresh to the minute.
   This tilts the comparison against the system at that cutoff. It must be reported, and the gap per feed is a
   measured result.
3. **Government feeds have no archive coverage.** That does not matter for election markets, but it means a
   retrospective cohort of executive-action markets (the prospective population) could not be built this way.
4. **News fidelity (WP8b) will be thin.** Only Politico (17) and Al Jazeera (2) have copies from 6 to 10 Nov. This may
   be **index lag**: the CDX index can trail the copies themselves. Re-query around 2026-11-20; how much the count grows
   is itself a measure of the lag.
5. **Evidence is headline plus feed summary**, the same as the live collector stores. This keeps the comparison fair,
   but the content is thin.

**Not done** (time-boxed out): Common Crawl News. It is only worth trying if the 24 h cutoff gap matters in the results.

## 4. Market list

Not drawn yet. It waits for the owner's confirmation of the cutoffs (section 1) and the selection rule (D2 in
`docs/retrospective_collection.md`). The Gamma query returned at least 1,000 closed events, most of them sports, so it
must be paged further.

## 5. Model for the replays

Gemini 3.5 Flash-Lite: knowledge cutoff **March 2026**, according to the owner's reading of the official model page on
2026-11-10. The URL and a screenshot are still to be recorded. It has 500 requests per day (free tier). About 450 calls
(25 markets x 2 cutoffs x about 9) fit in one day.

DeepSeek V4 Pro has no published knowledge cutoff, so it is not used for replays. The 3.6 Flash free tier (20 per day)
is too small.

## 6. Raw material

- **Laptop probes:** `data/retro/wp0_laptop/`. Includes the raw responses, the request logs and the probe scripts.
  Gitignored.
- **VM probes:** `~/wp0_wayback/` on the VM. Includes the CDX responses and `summary.json`. The VM run's request log was
  written there.

Copy the VM files to `data/retro/wp0_vm/` on the laptop before relying on them in the report.
