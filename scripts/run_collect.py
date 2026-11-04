"""Scheduled collector entry point: one profile per run, a lock against overlap, a health log.

    python -m scripts.run_collect --profile quotes        (every 10 min)
    python -m scripts.run_collect --profile rss           (every 15 min)
    python -m scripts.run_collect --profile discovery     (every 3 h; heaviest download)
    python -m scripts.run_collect --profile gdelt         (hourly)
    python -m scripts.run_collect --profile resolutions   (hourly)

Collection only: no LLM, no forecasting, no credentials. Exit 0 = healthy, 2 = failures worth a look.
"""
import argparse
import json
import os
import time
import traceback
from pathlib import Path

from scripts.run_ingest import run, summarize
from src.common import load_config, utcnow

PROFILES = {'discovery': {'markets'}, 'quotes': {'quotes'}, 'rss': {'rss'}, 'gdelt': {'gdelt'},
            'resolutions': {'resolutions'}}
# Minutes between runs. Single source of truth for collector_status; schedule_collector.ps1 must match
# (tests/test_status.py checks this).
INTERVAL_MINUTES = {'quotes': 10, 'rss': 15, 'discovery': 180, 'gdelt': 60, 'resolutions': 60}
STALE_LOCK_SECONDS = 1800


def acquire_lock(path):
    """Exclusive lock file; a lock older than STALE_LOCK_SECONDS is a crashed run and is replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - path.stat().st_mtime < STALE_LOCK_SECONDS:
                return False
            path.unlink(missing_ok=True)
    return False


def unhealthy(summary, profile):
    """Quotes always see a few closed books; flag them only when a large share fails."""
    failed, ok = summary['failed'], summary['ok']
    if profile == 'quotes':
        total = failed.get('quotes', 0) + ok.get('quotes', 0)
        return total > 0 and failed.get('quotes', 0) / total >= 0.25
    return bool(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='configs/collect.yaml')
    parser.add_argument('--profile', choices=sorted(PROFILES), required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    artifacts = Path(cfg['artifact_dir'])
    artifacts.mkdir(parents=True, exist_ok=True)
    lock = Path(cfg['data_dir']) / f'collector_{args.profile}.lock'
    if not acquire_lock(lock):
        print(f'{args.profile}: previous run still active; skipping')
        return 0
    try:
        report = run(cfg, PROFILES[args.profile], bucket=args.profile)
        summary = {'profile': args.profile, **summarize(report)}
        with (artifacts / 'collector_history.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(summary, sort_keys=True) + '\n')
        print(json.dumps(summary, indent=2))
        return 2 if unhealthy(summary, args.profile) else 0
    except Exception:
        # A crash before/after the stage loop must still leave a trace when run by a scheduler.
        with (artifacts / 'collector_errors.log').open('a', encoding='utf-8') as log:
            log.write(f'--- {utcnow()} {args.profile}\n{traceback.format_exc()}\n')
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
