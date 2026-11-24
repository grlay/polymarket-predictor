"""B5 evidence audit (decisions log R13): draw the documents a person labels as relevant / not relevant.

    python -m retrospective.audit_sample        # read-only; writes data/retro/out/audit_sheet.csv and audit_key.csv

Population = (market, document) pairs the forecasting agents were actually SHOWN (the evidence list stored with each
`single_agent_rag` forecast), so the audit measures the precision of the evidence the agents received:
  * replay: 24 h cutoff (primary horizon), replay database, one pair per (race cluster, document);
  * pilot: the two pilot markets at their pilot cutoffs, live database.
Sample: 40 replay pairs + 10 pilot pairs, drawn with random.Random(2605) from the lists sorted by id (fewer if the
population is smaller). The sheet shows only what the labeller needs: the market question, the cutoff, and the text the
agent saw (title + summary, cut to the retrieval limit). No price, forecast, system name, gate or outcome is shown.
The key file links each item to its ids and is not opened while labelling.
"""
import csv
import json
import pathlib
import random
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
RETRO_DB = ROOT / 'data' / 'retro' / 'retro.sqlite'
LIVE_DB = ROOT / 'data' / 'research' / 'research.sqlite'
OUT = ROOT / 'data' / 'retro' / 'out'
PILOT_MARKETS = ('6282802', '6723099')
N_REPLAY, N_PILOT, SEED, MAX_CHARS = 40, 10, 2605, 1800


def readonly(path):
    con = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def utc(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def shown_pairs(con, where, params=()):
    """(group, document_id) -> pair record for every document shown to single_agent_rag under `where`."""
    pairs = {}
    rows = con.execute(f'''SELECT f.evidence_json, s.cutoff, s.market_id, m.event_group, v.question
        FROM forecasts f JOIN snapshots s USING(snapshot_id) JOIN markets m USING(market_id)
        JOIN market_versions v USING(version_id)
        WHERE f.system='single_agent_rag' AND f.status='ok' AND {where}
        ORDER BY s.cutoff, s.market_id''', params).fetchall()
    for r in rows:
        for doc_id in json.loads(r['evidence_json']):
            key = (r['event_group'], doc_id)
            pairs.setdefault(key, dict(group=r['event_group'], market_id=r['market_id'], question=r['question'],
                                       cutoff=r['cutoff'], document_id=doc_id))
    return pairs


def attach_text(con, records):
    for rec in records:
        d = con.execute('SELECT title, body, source, published_at FROM documents WHERE document_id=?',
                        (rec['document_id'],)).fetchone()
        rec.update(title=d['title'], text=d['body'][:MAX_CHARS], source=d['source'], published=utc(d['published_at']))
    return records


def draw(pairs, n, rng):
    keys = sorted(pairs)
    return [pairs[k] for k in (rng.sample(keys, n) if len(keys) > n else keys)]


def build(retro_db=RETRO_DB, live_db=LIVE_DB, out=OUT):
    rng = random.Random(SEED)
    retro = readonly(retro_db)
    try:
        replay = attach_text(retro, draw(shown_pairs(retro, "ROUND(s.horizon_hours)=24"), N_REPLAY, rng))
    finally:
        retro.close()
    for r in replay:
        r['cohort'] = 'replay'
    pilot = []
    if pathlib.Path(live_db).exists():
        live = readonly(live_db)
        try:
            marks = ','.join('?' * len(PILOT_MARKETS))
            pilot = attach_text(live, draw(shown_pairs(live, f"s.market_id IN ({marks}) AND s.mode LIKE '%_pilot' "
                                                             "AND s.cutoff % 86400 = 17940", PILOT_MARKETS), N_PILOT, rng))
        finally:
            live.close()
    for r in pilot:
        r['cohort'] = 'pilot'
    items = replay + pilot
    order = list(range(len(items)))
    rng.shuffle(order)                        # the labeller sees replay and pilot items mixed
    out.mkdir(parents=True, exist_ok=True)
    with open(out / 'audit_sheet.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['item', 'market_question', 'forecast_time', 'source', 'published', 'title', 'text_shown',
                    'label (R / N / U)', 'note'])
        for i, idx in enumerate(order, 1):
            it = items[idx]
            w.writerow([i, it['question'], utc(it['cutoff']), it['source'], it['published'], it['title'], it['text'], '', ''])
    with open(out / 'audit_key.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['item', 'cohort', 'group', 'market_id', 'document_id'])
        for i, idx in enumerate(order, 1):
            it = items[idx]
            w.writerow([i, it['cohort'], it['group'], it['market_id'], it['document_id']])
    return dict(replay=len(replay), pilot=len(pilot), total=len(items))


def main():
    print(json.dumps(build()))
    print('Sheet: data/retro/out/audit_sheet.csv (open in Excel). Do not open audit_key.csv until labelling is done.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
