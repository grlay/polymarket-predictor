"""Is the collector alive and is the data complete? Read-only report.

    python -m scripts.collector_status                 # human-readable
    python -m scripts.collector_status --json          # machine-readable

Exit code 0 = healthy, 2 = something needs attention (a stale profile, never run, or low disk).
Run it after waking the laptop, and once a day.
"""
import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.run_collect import INTERVAL_MINUTES, unhealthy
from src.common import load_config, utcnow

GRACE = 2.5          # STALE when the last run is older than GRACE x the profile's interval
WINDOW_DAYS = 7
QUOTE_FRESH_SECONDS = 1800
MIN_FREE_GB = 2.0
MIN_DAYS_LEFT = 14


def stamp(value):
    return datetime.fromtimestamp(int(value), timezone.utc).strftime('%Y-%m-%d %H:%M')


def read_history(path):
    runs = []
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue  # a torn last line must not break the report
    return runs


def find_gaps(times, interval_seconds, factor=GRACE):
    """Consecutive run times further apart than factor x interval: [(start, end)]. Each is a missed stretch."""
    times = sorted(times)
    return [(a, b) for a, b in zip(times, times[1:]) if b - a > factor * interval_seconds]


def profile_report(profile, runs, now, window_days=WINDOW_DAYS):
    interval = INTERVAL_MINUTES[profile] * 60
    mine = sorted((r for r in runs if r.get('profile') == profile), key=lambda r: r['started_at'])
    window = [r for r in mine if r['started_at'] >= now - window_days * 86400]
    last_day = [r for r in mine if r['started_at'] >= now - 86400]
    last = mine[-1]['started_at'] if mine else None
    if last is None:
        status = 'NEVER'
    elif now - last > GRACE * interval:
        status = 'STALE'
    else:
        status = 'OK'
    gaps = find_gaps([r['started_at'] for r in window] + ([now] if window else []), interval)
    gaps.sort(key=lambda g: g[0] - g[1])  # longest first
    return {'profile': profile, 'status': status, 'last_run': last, 'age_minutes': None if last is None else (now - last) // 60,
            'runs_24h': len(last_day), 'expected_24h': 24 * 60 // INTERVAL_MINUTES[profile],
            'unhealthy_runs_24h': sum(unhealthy(r, profile) for r in last_day),
            'missed_stretches_7d': len(gaps),
            'longest_gaps': [{'from': stamp(a), 'to': stamp(b), 'hours': round((b - a) / 3600, 1)} for a, b in gaps[:3]]}


def db_report(path, now):
    path = Path(path)
    if not path.exists():
        return {'error': f'database not found: {path}'}
    con = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    one = lambda sql, *p: con.execute(sql, p).fetchone()[0]
    try:
        report = {t: one(f'SELECT COUNT(*) FROM {t}') for t in
                  ('markets', 'market_versions', 'prices', 'documents', 'resolutions', 'snapshots')}
        report['events'] = one('SELECT COUNT(DISTINCT event_group) FROM markets')
        report['latest_quote'] = one('SELECT MAX(observed_at) FROM prices')
        report['latest_document'] = one('SELECT MAX(observed_at) FROM documents')
        tracked = '''SELECT m.market_id, MAX(v.scheduled_end) AS end_ FROM markets m JOIN market_versions v USING(market_id)
            WHERE m.source_kind='real' AND v.is_open=1 AND v.available_at=(SELECT MAX(available_at)
            FROM market_versions WHERE market_id=m.market_id) GROUP BY m.market_id HAVING MAX(v.scheduled_end)>?'''
        ids = [r['market_id'] for r in con.execute(tracked, (now,))]
        fresh = {r[0] for r in con.execute('SELECT DISTINCT market_id FROM prices WHERE observed_at>=?',
                                           (now - QUOTE_FRESH_SECONDS,))}
        report['open_markets'] = len(ids)
        report['open_without_fresh_quote'] = len([i for i in ids if i not in fresh])
        days = {}
        for r in con.execute(tracked, (now,)):
            days[r['end_'] // 86400] = days.get(r['end_'] // 86400, 0) + 1
        report['largest_deadline_day_share'] = round(max(days.values()) / sum(days.values()), 2) if days else None
        report['unresolved_past_deadline'] = one('''SELECT COUNT(*) FROM (SELECT m.market_id FROM markets m
            JOIN market_versions v USING(market_id) WHERE m.market_id NOT IN (SELECT market_id FROM resolutions)
            GROUP BY m.market_id HAVING MAX(v.scheduled_end)<=?)''', now)
        return report
    finally:
        con.close()


def disk_report(data_dir, first_run, now):
    data_dir = Path(data_dir)
    used = sum(f.stat().st_size for f in data_dir.rglob('*') if f.is_file()) if data_dir.exists() else 0
    free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.anchor or '.').free
    days = (now - first_run) / 86400 if first_run else 0
    per_day = used / days if days >= 1 else None
    return {'data_gb': round(used / 1e9, 3), 'free_gb': round(free / 1e9, 1),
            'growth_mb_per_day': None if per_day is None else round(per_day / 1e6, 1),
            'days_left': None if not per_day else int(free / per_day)}


def build_report(cfg, now=None):
    now = utcnow() if now is None else now
    runs = read_history(Path(cfg['artifact_dir']) / 'collector_history.jsonl')
    first = min((r['started_at'] for r in runs), default=None)
    profiles = [profile_report(p, runs, now) for p in INTERVAL_MINUTES]
    data, disk = db_report(cfg['database'], now), disk_report(cfg['data_dir'], first, now)
    problems = [f"{p['profile']} is {p['status']}" for p in profiles if p['status'] != 'OK']
    if disk['free_gb'] < MIN_FREE_GB:
        problems.append(f"only {disk['free_gb']} GB free")
    if disk['days_left'] is not None and disk['days_left'] < MIN_DAYS_LEFT:
        problems.append(f"disk full in about {disk['days_left']} days")
    if 'error' in data:
        problems.append(data['error'])
    elif data['open_markets'] and data['open_without_fresh_quote'] > 0.25 * data['open_markets']:
        problems.append(f"{data['open_without_fresh_quote']} of {data['open_markets']} open markets have no quote in 30 min")
    return {'generated_at': now, 'profiles': profiles, 'data': data, 'disk': disk, 'problems': problems}


def render(report):
    lines = [f"HX2605 collector status  ({stamp(report['generated_at'])} UTC)", '']
    lines.append(f"{'profile':12s} {'status':6s} {'last run (UTC)':17s} {'age':>7s} {'runs 24h':>10s} {'bad runs':>9s} {'gaps 7d':>8s}")
    for p in report['profiles']:
        last = '-' if p['last_run'] is None else stamp(p['last_run'])
        age = '-' if p['age_minutes'] is None else f"{p['age_minutes']}m"
        lines.append(f"{p['profile']:12s} {p['status']:6s} {last:17s} {age:>7s} "
                     f"{str(p['runs_24h']) + '/' + str(p['expected_24h']):>10s} {p['unhealthy_runs_24h']:>9d} {p['missed_stretches_7d']:>8d}")
        for gap in p['longest_gaps']:
            lines.append(f"{'':12s}   gap {gap['from']} -> {gap['to']} ({gap['hours']} h)")
    data = report['data']
    lines.append('')
    if 'error' in data:
        lines.append(data['error'])
    else:
        lines.append(f"markets {data['markets']} ({data['events']} events, {data['open_markets']} open) | quotes {data['prices']} | "
                     f"documents {data['documents']} | labels {data['resolutions']} | snapshots {data['snapshots']}")
        lines.append(f"latest quote {stamp(data['latest_quote']) if data['latest_quote'] else '-'} | latest document "
                     f"{stamp(data['latest_document']) if data['latest_document'] else '-'}")
        lines.append(f"open markets without a quote in the last 30 min: {data['open_without_fresh_quote']} | "
                     f"unresolved past deadline: {data['unresolved_past_deadline']} | "
                     f"largest share on one deadline date: {data['largest_deadline_day_share']}")
    disk = report['disk']
    growth = 'n/a (under a day of history)' if disk['growth_mb_per_day'] is None else f"{disk['growth_mb_per_day']} MB/day, about {disk['days_left']} days left"
    lines.append(f"disk: data {disk['data_gb']} GB, free {disk['free_gb']} GB, growth {growth}")
    lines.append('')
    lines.append('ATTENTION: ' + '; '.join(report['problems']) if report['problems'] else 'All checks passed.')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='configs/collect.yaml')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    report = build_report(load_config(args.config))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 2 if report['problems'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
