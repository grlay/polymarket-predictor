"""Inspect saved snapshots without API calls, ingestion or database writes."""
import argparse
import sqlite3
from pathlib import Path

from src.common import load_config
from src.dataset.snapshots import frame
from src.retrieval.index import eligible_documents
from src.retrieval.relevance import rejection_reason, relevance_profile
from src.retrieval.temporal_retriever import retrieve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='configs/live_pilot.yaml')
    parser.add_argument('--all', action='store_true', help='Inspect every cutoff instead of latest')
    args = parser.parse_args()
    cfg = load_config(args.config)
    uri = Path(cfg['database']).resolve().as_uri() + '?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        data = frame(con, pilot=None)
        if data.empty:
            print('No saved snapshots.'); return
        selected = data if args.all else data[data.cutoff == data.cutoff.max()]
        for snapshot in selected.to_dict('records'):
            docs = retrieve(con, snapshot, cfg)
            profile = relevance_profile(snapshot)
            print('\nQUESTION:', snapshot['question'])
            print('EVENT GROUP:', snapshot['event_group'])
            print('PROFILE:', profile or 'unprofiled: lexical ranking only')
            print('STATUS:', 'CANDIDATES_REQUIRE_REVIEW' if docs else 'INSUFFICIENT_RELEVANT_EVIDENCE')
            print('RETRIEVED DOCUMENTS:', len(docs))
            for d in docs:
                print('-', d['source'], '|', d['title'])
            candidates = eligible_documents(con, int(snapshot['cutoff']), cfg['dataset']['lookback_days'], snapshot['source_kind'], cfg['retrieval']['include_social'])
            for d in candidates:
                visible = {**d, 'body': d['body'][:cfg['retrieval']['max_chars']]}
                reason = rejection_reason(snapshot, visible)
                if reason:
                    print('REJECTED:', reason, '|', d['title'])
    finally:
        con.close()


if __name__ == '__main__':
    main()
