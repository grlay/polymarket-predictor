import argparse
import json
from pathlib import Path

from src.common import load_config, timestamp, utcnow, write_json
from src.db.loaders import connect
from src.dataset.snapshots import build, frame


def main():
    parser = argparse.ArgumentParser(description='Build strict point-in-time snapshots; no outcomes required.')
    parser.add_argument('--config', default='configs/config.yaml')
    parser.add_argument('--at', help='now or explicit timezone-aware cutoff; omitted uses configured horizons')
    args = parser.parse_args()
    cfg = load_config(args.config)
    con = connect(cfg['database'])
    at = utcnow() if args.at == 'now' else (timestamp(args.at) if args.at else None)
    result = build(con, cfg, at)
    target = Path(cfg['data_dir'])/'processed'
    target.mkdir(parents=True, exist_ok=True)
    frame(con, pilot=None).to_csv(target/'snapshots.csv', index=False)
    write_json(target/'build_report.json', result)
    print(json.dumps(result, indent=2))
    con.close()
    return 0 if result['eligible'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
