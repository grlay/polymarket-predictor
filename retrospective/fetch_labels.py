"""B6 (decisions log R15; retrospective spec D6): final labels for the replay markets, imported LAST.

    python -m retrospective.fetch_labels           # VM: fetch + store labels in the replay DB, then print the checks

Labels come from Polymarket's Data API v2 resolution records, read by the live collector's own code
(`collect_resolutions` -> `store_resolution`): only a record with status 'resolved' is a label, a split or conflicting
record raises, and an existing label can never change. `label_available_at` = the time we observed it. Every raw
response is archived by the HTTP client and referenced in `raw_ref`.

Checks printed (R15, the reduced form of D6's cross-check):
  1. consistency: within each event, at most one selected market resolves YES; events with none are listed;
  2. hand cross-check list: the two D2 demo race markets plus 10 markets drawn with random.Random(2605), each with
     its label, for comparison with an AP or official result by the student.
"""
import csv
import json
import pathlib
import random
import sys
from collections import defaultdict

from src.common import load_config
from src.db.loaders import connect, rows
from src.ingestion.data_api import collect_resolutions
from src.ingestion.http import HTTPClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs' / 'config_retro.yaml'
SELECTION = ROOT / 'retrospective' / 'selection.csv'
N_RANDOM, SEED = 10, 2605


def selection():
    with open(SELECTION, encoding='utf-8') as f:
        return {r['market_id']: r for r in csv.DictReader(f) if 'yes' in (r['eligible_168h'], r['eligible_24h'])}


def demo_markets(sel):
    """D2 rule (decisions log 2026-11-13): Senate and Governor race market closest to 0.5 at 168 h, ties -> lower id.
    (The archived-document fallback of D2 concerns the demo, not this cross-check, and is not applied here.)"""
    out = []
    for rule in ('senate_race', 'governor_race'):
        cands = [r for r in sel.values() if r['rule'] == rule and r['price_168h']]
        best = min(cands, key=lambda r: (abs(float(r['price_168h']) - 0.5), int(r['market_id'])))
        out.append(best['market_id'])
    return out


def checks(con, sel):
    labels = {r['market_id']: r for r in rows(con, 'SELECT market_id, outcome, resolved_at FROM resolutions')}
    by_event = defaultdict(list)
    for mid, r in sel.items():
        by_event[(r['event_id'], r['event_title'].strip())].append(mid)
    missing = sorted(m for m in sel if m not in labels)
    several, none = [], []
    for (eid, title), mids in sorted(by_event.items()):
        yes = [m for m in mids if m in labels and labels[m]['outcome'] == 1]
        if len(yes) > 1:
            several.append((title, yes))
        elif not yes and all(m in labels for m in mids):
            none.append((title, [sel[m]['question'] for m in mids]))
    demo = demo_markets(sel)
    rest = sorted(m for m in sel if m not in demo)
    sample = demo + random.Random(SEED).sample(rest, N_RANDOM)
    cross = [(m, sel[m]['question'], ('YES' if labels[m]['outcome'] else 'NO') if m in labels else 'missing')
             for m in sample]
    return dict(labels=len(labels), markets=len(sel), missing=missing, events=len(by_event),
                events_with_several_yes=several, events_with_no_yes=none, cross_check=cross)


def main():
    cfg = load_config(str(CONFIG))
    con = connect(cfg['database'])
    sel = selection()
    markets = rows(con, 'SELECT * FROM markets ORDER BY market_id')
    if {m['market_id'] for m in markets} != set(sel):
        raise SystemExit('Replay DB markets differ from selection.csv')
    error = None
    try:
        stored = collect_resolutions(HTTPClient(cfg, bucket='labels'), con, cfg, markets)
    except ValueError as exc:            # problems are listed; labels that did arrive are kept
        stored, error = None, str(exc)
    report = checks(con, sel)
    con.close()
    print('newly stored', stored, '| error', error or 'none')
    print(f"labels {report['labels']} of {report['markets']} markets; missing {report['missing'] or 'none'}")
    print(f"events {report['events']}; with more than one YES: {report['events_with_several_yes'] or 'none'}")
    print('events with no YES among the selected markets (expected where the winner had no eligible market):')
    for title, qs in report['events_with_no_yes']:
        print('   ', title, '|', '; '.join(qs))
    print('HAND CROSS-CHECK (compare with AP / official results; first two = D2 demo markets):')
    for mid, q, lab in report['cross_check']:
        print(f'    {mid}  {lab:<7} {q}')
    (ROOT / 'data' / 'retro' / 'out').mkdir(parents=True, exist_ok=True)
    (ROOT / 'data' / 'retro' / 'out' / 'labels_report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    return 1 if error or report['missing'] or report['events_with_several_yes'] else 0


if __name__ == '__main__':
    sys.exit(main())
