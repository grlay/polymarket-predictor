"""Read-only check of one daily pilot cutoff, per model series. Prints a short verdict; changes nothing.

Run after the 900 s window has closed (cron: 05:20 UTC or later). Examples:
    .venv/bin/python -m scripts.pilot_check                      # today's cutoff (UTC date)
    .venv/bin/python -m scripts.pilot_check --date 2026-11-10 --expect gemini-3.6-flash=6723099,6282802
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

NAMES = {'6723099': 'Rubio', '6282802': 'blockade'}
# Series from 2026-11-11 (decisions log, 2026-11-10). DeepSeek is expected only when it was switched on.
DEFAULT_EXPECT = ['gemini-3.5-flash-lite=6723099,6282802', 'gemini-3.6-flash=6723099',
                  'deepseek-v4-pro=6723099,6282802']
SYSTEMS = 8
WINDOW_SECONDS = 900


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d'))
    p.add_argument('--db', default='data/research/research.sqlite')
    p.add_argument('--expect', nargs='+', default=DEFAULT_EXPECT, help='model=market,market ...')
    a = p.parse_args()

    day = dt.date.fromisoformat(a.date)
    cut = int(dt.datetime(day.year, day.month, day.day, 4, 59, tzinfo=dt.timezone.utc).timestamp())
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    rows = con.execute(
        '''select r.model, s.market_id, round(s.horizon_hours,1), f.status, f.generated_at
           from forecasts f join snapshots s using(snapshot_id) join forecast_runs r using(run_id)
           where s.cutoff=?''', (cut,)).fetchall()

    expected = {}
    for item in a.expect:
        model, markets = item.split('=')
        expected[model] = markets.split(',')
    problems = []
    seen = sorted({(r[0], r[1]) for r in rows} | {(m, k) for m, ks in expected.items() for k in ks})
    for model, market in seen:
        mine = [r for r in rows if r[0] == model and r[1] == market]
        ok = [r for r in mine if r[3] == 'ok']
        delays = [r[4] - cut for r in mine]
        span = f'{min(delays)}-{max(delays)} s' if delays else '-'
        hz = sorted({r[2] for r in mine})
        print(f'{model:24} {NAMES.get(market, market):9} {len(ok)}/{SYSTEMS} ok  rows {len(mine)}  '
              f'after cutoff {span}  horizon {hz or "-"}')
        if model in expected and market in expected[model] and len(ok) != SYSTEMS:
            problems.append(f'{model} {NAMES.get(market, market)} {len(ok)}/{SYSTEMS}')
        # generated_at is when a row was written, i.e. when its snapshot FINISHED. The 900 s rule applies to the
        # START of generation (orchestrator.too_old), so a late finish is reported, not flagged.
        late = [d for d in delays if d > WINDOW_SECONDS]
        if late:
            print(f'    note: {len(late)} rows finished after {WINDOW_SECONDS} s (allowed: the limit is on the start)')

    for name in ('lite', 'ref', 'deepseek'):
        log = Path(f'artifacts/research/pilot_{name}.log')
        if log.exists():
            text = log.read_text(encoding='utf-8', errors='replace')
            print(f'{log.name}: HTTP 429 mentions {text.count("HTTP 429")}, HTTP 503 {text.count("HTTP 503")} '
                  '(whole file, not only today)')

    verdict = 'OK' if not problems else 'ATTENTION: ' + '; '.join(problems)
    print(f'PILOT CHECK {a.date} (run at {now}): {verdict}')
    return 0 if not problems else 1


if __name__ == '__main__':
    raise SystemExit(main())
