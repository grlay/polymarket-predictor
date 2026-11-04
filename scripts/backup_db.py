"""Daily backup of the research database (and closed raw-archive days) to a second folder.

    python -m scripts.backup_db --dest "E:\\hx2605_backup"      # a DIFFERENT drive protects against disk failure
    python -m scripts.backup_db                                  # default: data/backups (same disk: corruption only)

The SQLite online-backup API gives a consistent copy while the collector keeps writing. Raw archive
files for past UTC days never change, so they are copied once and then skipped.
"""
import argparse
import gzip
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.common import load_config, utcnow


def backup(cfg, dest, keep=14, include_raw=True, now=None):
    now = utcnow() if now is None else now
    source, dest = Path(cfg['database']), Path(dest)
    if not source.exists():
        raise FileNotFoundError(f'No database to back up: {source}')
    dest.mkdir(parents=True, exist_ok=True)
    name = 'research-' + datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%d-%H%M')
    tmp, out = dest / (name + '.tmp'), dest / (name + '.sqlite.gz')
    reader = sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True, timeout=60)
    writer = sqlite3.connect(str(tmp))
    try:
        reader.backup(writer)
        if writer.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup failed its integrity check; previous backups were kept')
    finally:
        writer.close()
        reader.close()
    with tmp.open('rb') as src, gzip.open(out, 'wb', compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    tmp.unlink()
    old = sorted(dest.glob('research-*.sqlite.gz'))[:-keep] if keep else []
    for path in old:
        path.unlink()
    copied = 0
    if include_raw:
        today = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')
        raw = Path(cfg['data_dir']) / 'raw'
        for day in sorted(raw.iterdir()) if raw.exists() else []:
            if not day.is_dir() or day.name >= today:
                continue  # today's files are still being appended to
            for file in day.iterdir():
                target = dest / 'raw' / day.name / file.name
                if not target.exists() or target.stat().st_size != file.stat().st_size:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
                    copied += 1
    history = Path(cfg['artifact_dir']) / 'collector_history.jsonl'
    if history.exists():
        shutil.copy2(history, dest / 'collector_history.jsonl')
    return {'backup': str(out), 'size_mb': round(out.stat().st_size / 1e6, 2), 'pruned': len(old), 'raw_files_copied': copied}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='configs/collect.yaml')
    parser.add_argument('--dest', help='backup folder (default: <data_dir>/backups)')
    parser.add_argument('--keep', type=int, default=14, help='database backups to keep')
    parser.add_argument('--no-raw', action='store_true', help='skip the raw archive')
    args = parser.parse_args()
    cfg = load_config(args.config)
    result = backup(cfg, args.dest or Path(cfg['data_dir']) / 'backups', args.keep, not args.no_raw)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
