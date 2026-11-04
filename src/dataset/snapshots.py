import json
from pathlib import Path

import pandas as pd
import yaml

from src.common import canonical, digest, timestamp, utcnow
from src.dataset.leakage_checks import check_snapshot
from src.dataset.market_selection import exclusion_reason
from src.db.loaders import insert, rows, event
from src.features.market_features import market_features


def make_snapshot(con, market, cutoff, cfg, filters, pilot=False):
    versions = rows(con, '''SELECT * FROM market_versions WHERE market_id=? AND available_at<=?
        ORDER BY available_at DESC,version_id DESC LIMIT 1''', (market['market_id'], cutoff))
    if not versions:
        raise ValueError('metadata_not_available_asof')
    metadata = versions[0]
    prices = rows(con, '''SELECT * FROM prices WHERE market_id=? AND timestamp<=? AND available_at<=?
        AND resolution_seconds>0 AND provenance IN ('prospective','archive_verified','synthetic')
        ORDER BY timestamp DESC,available_at DESC,price_id DESC LIMIT 1''', (market['market_id'], cutoff, cutoff))
    if not prices:
        raise ValueError('price_not_available_asof')
    price = prices[0]
    if cutoff-price['timestamp'] > cfg['dataset']['max_price_age_seconds']:
        raise ValueError('stale_price')
    if con.execute('SELECT 1 FROM resolutions WHERE market_id=? AND resolved_at<=?', (market['market_id'], cutoff)).fetchone():
        raise ValueError('already_resolved')
    reason = exclusion_reason(metadata, price['price'], filters)
    if reason:
        raise ValueError(reason)
    # --at pilots carry their own mode so they are never pooled with fixed-horizon research snapshots.
    mode = cfg['mode'] + ('_pilot' if pilot else '')
    config_hash = digest({'dataset': cfg['dataset'], 'filters': filters, 'mode': mode})
    snap = dict(snapshot_id=digest([market['market_id'], cutoff, config_hash]),
                market_id=market['market_id'], version_id=metadata['version_id'], cutoff=cutoff,
                horizon_hours=(metadata['scheduled_end']-cutoff)/3600,
                market_price=price['price'], price_id=price['price_id'],
                features_json=canonical(market_features(con, market['market_id'], cutoff, price, metadata)),
                mode=mode, source_kind=market['source_kind'], config_hash=config_hash)
    check_snapshot(snap, metadata, price)
    insert(con, 'snapshots', snap)
    return snap


def build(con, cfg, at=None):
    filters = yaml.safe_load(Path(cfg['market_filters']).read_text())
    total, excluded = 0, []
    for market in rows(con, 'SELECT * FROM markets ORDER BY market_id'):
        if at is not None:
            cutoffs = [timestamp(at)]
        else:
            # Candidate horizons derive from versioned deadlines. Validate that the same
            # deadline was visible at that cutoff before accepting the candidate.
            versions = rows(con, 'SELECT * FROM market_versions WHERE market_id=?', (market['market_id'],))
            candidates = {(v['scheduled_end']-int(h*3600), v['scheduled_end'])
                          for v in versions for h in cfg['dataset']['horizons_hours']}
            cutoffs = []
            for cutoff, deadline in sorted(candidates):
                visible = rows(con, '''SELECT scheduled_end FROM market_versions WHERE market_id=?
                    AND available_at<=? ORDER BY available_at DESC,version_id DESC LIMIT 1''', (market['market_id'], cutoff))
                if visible and visible[0]['scheduled_end'] == deadline:
                    cutoffs.append(cutoff)
        for cutoff in sorted(set(cutoffs)):
            if cutoff > utcnow():
                continue
            if cfg['dataset'].get('start') and cutoff < timestamp(cfg['dataset']['start']):
                continue
            if cfg['dataset'].get('end') and cutoff >= timestamp(cfg['dataset']['end']):
                continue
            try:
                make_snapshot(con, market, cutoff, cfg, filters, pilot=at is not None)
                total += 1
            except ValueError as exc:
                excluded.append({'market_id': market['market_id'], 'cutoff': cutoff, 'reason': str(exc)})
    event(con, 'build_dataset', 'complete', {'eligible': total, 'excluded': excluded})
    return {'eligible': total, 'excluded': excluded}


def frame(con, pilot=False):
    """Snapshots as a DataFrame. pilot=False: fixed-horizon research snapshots only; True: --at pilots only; None: all."""
    records = rows(con, '''SELECT s.*,m.event_group,v.question,v.rules,v.category,
        r.outcome,r.resolved_at,r.label_available_at FROM snapshots s
        JOIN markets m USING(market_id) JOIN market_versions v USING(version_id)
        LEFT JOIN resolutions r ON r.market_id=s.market_id ORDER BY cutoff,snapshot_id''')
    records = [r for r in records if pilot is None or r['mode'].endswith('_pilot') == pilot]
    for row in records:
        row.update(json.loads(row.pop('features_json')))
    return pd.DataFrame(records)
