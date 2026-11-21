"""Coverage of the archived news for the replay: N24 for the D5 decision (decisions log R11) and the B12 table.

    python -m retrospective.count_coverage            # read-only; prints the report and writes data/retro/out/coverage.json

For every replay snapshot it takes the documents the retriever's time window allows (same query, same body truncation
as `src/retrieval/temporal_retriever.py`) and applies the election gate (`rejection_reason`, the code run 2 uses).
  * N24 / N168 = race clusters with at least one gate-admitted document at that cutoff (N24 decides, R11);
  * N24 with at least 3 distinct admitted documents per cluster (descriptive sensitivity, R11 item 6);
  * admitted documents per cluster (distribution), clusters without any, documents per feed, rejection reasons;
  * cross-check: snapshots for which the full retriever (which adds a minimum text score) also returns a document.
The database is opened read-only; nothing is written to it. No price, forecast or outcome is read.
"""
import json
import pathlib
import sqlite3
import sys
from collections import Counter, defaultdict

from src.common import load_config
from src.dataset.leakage_checks import check_evidence
from src.retrieval.index import eligible_documents
from src.retrieval.relevance import rejection_reason, relevance_profile
from src.retrieval.temporal_retriever import retrieve

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs' / 'config_retro.yaml'
GO, PARTIAL = 30, 10          # R11: config min_train_groups / min_validation_groups


def open_readonly(path):
    con = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def decision(n24, documents_imported=True):
    if not documents_imported or n24 < PARTIAL:
        return 'NO-GO'
    return 'GO' if n24 >= GO else 'PARTIAL'


def coverage(con, cfg):
    snaps = [dict(r) for r in con.execute('''SELECT s.snapshot_id, s.market_id, s.cutoff, s.horizon_hours,
        s.source_kind, m.event_group AS cluster, v.question, v.rules FROM snapshots s
        JOIN markets m USING(market_id) JOIN market_versions v USING(version_id) ORDER BY s.cutoff, s.market_id''')]
    max_chars = cfg['retrieval']['max_chars']
    horizon = lambda s: '24h' if round(s['horizon_hours']) == 24 else '168h'
    admitted = {h: defaultdict(set) for h in ('24h', '168h')}         # cluster -> admitted document ids
    clusters = {h: set() for h in ('24h', '168h')}
    reasons, per_feed, retriever_hits, unprofiled = Counter(), Counter(), Counter(), []
    for s in snaps:
        h = horizon(s)
        clusters[h].add(s['cluster'])
        profile = relevance_profile(s)
        if profile is None:
            unprofiled.append(s['market_id'])
        docs = eligible_documents(con, int(s['cutoff']), cfg['dataset']['lookback_days'], s['source_kind'])
        check_evidence(docs, int(s['cutoff']), s['source_kind'])
        gate_kind = 'national' if profile and not profile.startswith('election_') else 'race'
        for d in docs:
            visible = {**d, 'body': d['body'][:max_chars]}
            why = rejection_reason(s, visible)
            reasons[(h, gate_kind, why or 'admitted')] += 1
            if why is None:
                admitted[h][s['cluster']].add(d['document_id'])
                per_feed[(h, d['source'])] += 1
        if retrieve(con, s, cfg):
            retriever_hits[h] += 1
    report = {}
    for h in ('24h', '168h'):
        sizes = {c: len(admitted[h].get(c, ())) for c in sorted(clusters[h])}
        report[h] = dict(
            snapshots=sum(horizon(s) == h for s in snaps), clusters=len(clusters[h]),
            n_clusters_with_document=sum(v >= 1 for v in sizes.values()),
            n_clusters_with_3_documents=sum(v >= 3 for v in sizes.values()),
            clusters_without_document=[c for c, v in sizes.items() if v == 0],
            documents_per_cluster=dict(sorted(Counter(sizes.values()).items())),
            snapshots_with_retriever_document=retriever_hits[h],
            admitted_per_feed={f: n for (hh, f), n in sorted(per_feed.items()) if hh == h},
            reasons={f'{g}:{w}': n for (hh, g, w), n in sorted(reasons.items()) if hh == h})
    n_docs = con.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
    report['documents_in_database'] = n_docs
    report['unprofiled_snapshots'] = unprofiled
    report['N24'] = report['24h']['n_clusters_with_document']
    report['N168'] = report['168h']['n_clusters_with_document']
    report['R11_decision'] = decision(report['N24'], n_docs > 0)
    return report


def main():
    cfg = load_config(str(CONFIG))
    con = open_readonly(cfg['database'])
    try:
        report = coverage(con, cfg)
    finally:
        con.close()
    out = ROOT / 'data' / 'retro' / 'out'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'coverage.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    for h in ('24h', '168h'):
        r = report[h]
        print(f"{h}: {r['n_clusters_with_document']} of {r['clusters']} clusters with >=1 admitted document "
              f"(>=3: {r['n_clusters_with_3_documents']}); snapshots {r['snapshots']}, "
              f"retriever cross-check {r['snapshots_with_retriever_document']}")
        print('   documents per cluster:', r['documents_per_cluster'])
        print('   admitted per feed:', r['admitted_per_feed'])
        print('   reasons:', r['reasons'])
    print('documents in database', report['documents_in_database'], '| unprofiled snapshots',
          len(report['unprofiled_snapshots']))
    print(f"N24 = {report['N24']}  N168 = {report['N168']}  ->  R11 decision: {report['R11_decision']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
