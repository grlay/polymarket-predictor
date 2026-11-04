"""Data v2 history, cursor validation, settlement exclusion, final labels."""
from src.common import digest, probability, timestamp
from src.db.loaders import insert


def normalize_history(payload, market_id, observed, ref, start, end):
    if not isinstance(payload.get('data'), list):
        raise ValueError('Data v2 history schema changed')
    records = []
    for row in payload['data']:
        grain = int(row['resolution_seconds'])
        t = timestamp(row['timestamp'])
        # Settlement points may be shifted/clamped by API; never use as forecasts.
        if grain <= 0 or not start <= t < end:
            continue
        record = dict(market_id=market_id, timestamp=t, available_at=observed,
                      observed_at=observed, price=probability(row['price']),
                      price_kind='history_unspecified', resolution_seconds=grain,
                      bid=None, ask=None, provenance='retrospective_unverified', raw_ref=ref)
        record['price_id'] = digest(record)
        records.append(record)
    return records


def collect_history(client, con, cfg, market, start, end):
    total = 0
    # v2 bounds explicit windows to 15 days; use nonoverlapping windows.
    for left in range(start, end, 15 * 86400):
        right = min(end, left + 15 * 86400)
        cursor, seen = None, set()
        for _ in range(100):
            params = {'token_id': market['yes_token'], 'start': left, 'end': right, 'cursor': cursor}
            payload, observed, ref = client.get(cfg['polymarket']['data_base'] + '/prices-history', params)
            for record in normalize_history(payload, market['market_id'], observed, ref, left, right):
                total += int(insert(con, 'prices', record))
            paging = payload.get('pagination', {})
            cursor = paging.get('next_cursor')
            if not cursor:
                if paging.get('has_more'):
                    raise ValueError('Pagination claims more data without cursor')
                break
            if cursor in seen:
                raise ValueError('Repeated cursor; refusing infinite pagination')
            seen.add(cursor)
        else:
            raise ValueError('History exceeded 100-page safety limit')
    return total


ONE = 10 ** 18  # UMA price scale: 0 -> second outcome wins, 1e18 -> first outcome wins, 0.5e18 -> split


def parse_resolution(row, market):
    """Return (yes_won, resolved_at) for a final binary record, None if not final; raise if excluded.

    Verified against live /v2/resolutions records (Nov 2026): fields are status, price,
    last_update_timestamp. There is no payouts array. A 'proposed' record is NOT a label.
    """
    if str(row.get('status', '')).lower() != 'resolved':
        return None
    price = int(row['price'])
    if price == ONE:
        winner = 0
    elif price == 0:
        winner = 1
    else:
        raise ValueError('Split/non-binary resolution excluded from binary evaluation')
    return int(winner == market['yes_index']), timestamp(int(row['last_update_timestamp']))


def store_resolution(con, market, records, observed, ref):
    """Store the final binary label for one market from its /resolutions records. Returns 1 if new."""
    finals = set()
    for row in records:
        if row.get('condition_id') != market['condition_id']:
            continue
        parsed = parse_resolution(row, market)
        if parsed:
            finals.add(parsed)
    if not finals:
        return 0
    if len({outcome for outcome, _ in finals}) > 1:
        raise ValueError('Conflicting final resolution records: quarantine and review')
    outcome, resolved = max(finals, key=lambda x: x[1])
    record = dict(market_id=market['market_id'], outcome=outcome, resolved_at=resolved,
                  label_available_at=observed, observed_at=observed,
                  source='polymarket_data_v2', status='final', raw_ref=ref)
    # Rechecking an existing label must agree, but does not change first observation.
    old = con.execute('SELECT outcome FROM resolutions WHERE market_id=?', (market['market_id'],)).fetchone()
    if old:
        if old['outcome'] != outcome:
            raise ValueError('Resolution changed: quarantine and review')
        return 0
    return int(insert(con, 'resolutions', record))


def collect_resolution(client, con, cfg, market):
    payload, observed, ref = client.get(cfg['polymarket']['data_base'] + '/resolutions',
                                         {'condition': market['condition_id']})
    if not isinstance(payload.get('data'), list):
        raise ValueError('Data v2 resolutions schema changed')
    return store_resolution(con, market, payload['data'], observed, ref)


def collect_resolutions(client, con, cfg, markets, batch=20):  # API accepts at most 20 conditions
    """Batch lookup (the endpoint accepts comma-separated condition ids; verified live Nov 2026).

    One market's problem (split outcome, conflicting label) never blocks the others; problems are
    raised together at the end so the run is reported as failed and the labels that did arrive are kept.
    """
    stored, problems = 0, []
    for start in range(0, len(markets), batch):
        chunk = markets[start:start + batch]
        payload, observed, ref = client.get(cfg['polymarket']['data_base'] + '/resolutions',
                                             {'condition': ','.join(m['condition_id'] for m in chunk)})
        if not isinstance(payload.get('data'), list):
            raise ValueError('Data v2 resolutions schema changed')
        for market in chunk:
            try:
                stored += store_resolution(con, market, payload['data'], observed, ref)
            except ValueError as exc:
                problems.append(f"market {market['market_id']}: {exc}")
    if problems:
        raise ValueError(f'{stored} stored; {len(problems)} problem(s): ' + '; '.join(problems[:5]))
    return stored
