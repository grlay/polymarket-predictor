"""WP1: mechanical market selection for the retrospective midterm cohort (decisions log, 2026-11-12).

Read-only public GETs (Polymarket Gamma and CLOB), at most one request every 2 s. Never reads outcome fields
(`outcomePrices`, `umaResolutionStatus`, winner flags): selection uses only titles, questions, deadlines and prices that
were public at the cutoffs. Raw responses are saved under data/retro/raw/ (git-ignored) so every row can be traced.

    python -m retrospective.select_markets events   # page closed events, classify, list candidate markets
    python -m retrospective.select_markets prices   # YES price at both cutoffs, band check, write selection.csv

Outputs (retrospective/, committed so the Git time proves they predate any label):
    events_classified.csv   every event returned, kept or dropped, with the rule that decided it
    selection.csv           every candidate market with its price at each cutoff and the eligibility verdict
    clusters.csv            market_id -> cluster
"""
import csv
import datetime as dt
import gzip
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
RAW = ROOT.parent / 'data' / 'retro' / 'raw'
UA = {'User-Agent': 'HX2605-academic-research/0.1'}
GAMMA = 'https://gamma-api.polymarket.com/events'
CLOB = 'https://clob.polymarket.com/prices-history'

DEADLINE = '2026-11-04T04:59:00Z'
CUTOFFS = {'168h': '2026-10-28T04:59:00Z', '24h': '2026-11-03T04:59:00Z'}
BAND = (0.02, 0.98)          # research filter configs/market_filters.yaml
MAX_AGE = 900                # dataset.max_price_age_seconds
WINDOW = 3600                # history requested for the hour before each cutoff

STATES = ('Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|'
          'Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
          'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|'
          'North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|'
          'Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming')
# Ordered rules on the EVENT TITLE only. First match decides. Drops come first so that, for example, a House-district
# or margin event is never kept by a broader rule below it.
RULES = [
    ('drop', 'house_district', r'(?i)\bdistrict\b|\b[A-Z]{2}-\d{1,2}\b'),
    ('drop', 'margin_or_share', r'(?i)\bmargin\b|vote share|\bby how much\b|popular vote|turnout|\bseats?\b.*\bhow many\b|how many seats'),
    ('drop', 'primary', r'(?i)\bprimary\b|\bnominee\b'),
    ('keep', 'national_control', r'(?i)\bbalance of power\b|which party (will )?wins? the (house|senate)|(house|senate) control|control of the (house|senate)'),
    ('keep', 'senate_race', rf'(?i)\b({STATES})\b.*\bsenate\b|\bsenate\b.*\b({STATES})\b'),
    ('keep', 'governor_race', rf'(?i)\b({STATES})\b.*\bgovern(or|atorial)\b|\bgovern(or|atorial)\b.*\b({STATES})\b'),
]
LOG = []


def get(url, name):
    time.sleep(2)
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    except Exception as e:  # network error: logged, not retried in a loop
        body, status = str(e).encode(), 'ERR'
    path = RAW / (name + '.gz')      # raw bytes kept exactly, gzip-compressed (Gamma pages are ~4.5 MB each)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body))
    LOG.append({'at': at, 'url': url, 'status': status, 'bytes': len(body), 'file': str(path.relative_to(RAW))})
    return status, body


def ts(iso):
    return int(dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())


def norm_end(value):
    if not value:
        return ''
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def classify(title):
    for action, rule, pattern in RULES:
        if re.search(pattern, title):
            return action, rule
    return 'drop', 'not_in_scope'


def cluster(rule, event_id):
    return 'national_control' if rule == 'national_control' else f'event_{event_id}'


def stage_events():
    # /events refuses offsets above 2000 (HTTP 422, 2026-11-12), so page with /events/keyset and `after_cursor`.
    events, cursor, page_no = [], None, 0
    while True:
        params = {'closed': 'true', 'end_date_min': '2026-11-02T00:00:00Z',
                  'end_date_max': '2026-11-05T00:00:00Z', 'limit': 100}
        if cursor:
            params['after_cursor'] = cursor
        status, body = get(f'{GAMMA}/keyset?{urllib.parse.urlencode(params)}', f'gamma/events_{page_no:04d}.json')
        if status != 200:
            raise SystemExit(f'Gamma returned {status} on page {page_no}: {body[:200]!r}')
        data = json.loads(body.decode('utf-8'))
        page = data.get('events') or []
        events += page
        print(f'page {page_no}: {len(page)} events (total {len(events)})', flush=True)
        cursor = data.get('next_cursor')
        if not page or not cursor:
            break
        page_no += 1
    ids = [e['id'] for e in events]
    if len(ids) != len(set(ids)):
        raise SystemExit(f'duplicate events across pages: {len(ids) - len(set(ids))}')
    rows, candidates = [], []
    for e in events:
        action, rule = classify(e.get('title') or '')
        rows.append({'event_id': e['id'], 'title': e.get('title'), 'n_markets': len(e.get('markets') or []),
                     'action': action, 'rule': rule})
        if action != 'keep':
            continue
        for m in e.get('markets') or []:
            outcomes = json.loads(m.get('outcomes') or '[]')
            tokens = json.loads(m.get('clobTokenIds') or '[]')
            reason = ''
            if sorted(outcomes) != ['No', 'Yes'] or len(tokens) != 2:
                reason = 'not_yes_no'
            elif norm_end(m.get('endDate')) != DEADLINE:
                reason = 'different_deadline'
            candidates.append({'market_id': str(m['id']), 'event_id': e['id'], 'event_title': e.get('title'),
                               'rule': rule, 'cluster': cluster(rule, e['id']), 'question': m.get('question'),
                               'end_date': norm_end(m.get('endDate')),
                               'yes_token': tokens[outcomes.index('Yes')] if not reason or reason == 'different_deadline' else '',
                               'pre_price_exclusion': reason})
    write_csv(ROOT / 'events_classified.csv', rows)
    # Short review file for a human check of the title rules (titles only, no prices, no outcomes).
    near = re.compile(r'(?i)senate|governor|gubernatorial|house|congress|midterm|balance of power')
    with open(ROOT / 'review_titles.txt', 'w', encoding='utf-8') as f:
        for heading, subset in (('KEPT', [r for r in rows if r['action'] == 'keep']),
                                ('DROPPED BUT POLITICAL-LOOKING', [r for r in rows if r['action'] != 'keep'
                                                                   and near.search(r['title'] or '')])):
            f.write(f'== {heading} ({len(subset)})\n')
            for r in sorted(subset, key=lambda r: (r['rule'], r['title'] or '')):
                f.write(f"{r['rule']:<17} {r['event_id']:>7}  {r['title']}\n")
    (RAW / 'candidates.json').write_text(json.dumps(candidates, indent=1), encoding='utf-8')
    kept = [r for r in rows if r['action'] == 'keep']
    by_rule = ', '.join('%s %d' % (k, sum(r['rule'] == k for r in kept))
                        for k in ('national_control', 'senate_race', 'governor_race'))
    usable = sum(not c['pre_price_exclusion'] for c in candidates)
    print('events %d; kept %d (%s); candidate markets %d; usable %d'
          % (len(events), len(kept), by_rule, len(candidates), usable))


def price_at(token, cutoff_iso, tag):
    cutoff = ts(cutoff_iso)
    q = urllib.parse.urlencode({'market': token, 'startTs': cutoff - WINDOW, 'endTs': cutoff, 'fidelity': 1})
    status, body = get(f'{CLOB}?{q}', f'prices/{tag}.json')
    if status != 200:
        return None, None, f'http_{status}'
    points = [p for p in json.loads(body.decode('utf-8')).get('history', []) if p['t'] <= cutoff]
    if not points:
        return None, None, 'price_not_available_asof'
    last = max(points, key=lambda p: p['t'])
    return float(last['p']), cutoff - int(last['t']), ''


def stage_prices():
    candidates = json.loads((RAW / 'candidates.json').read_text(encoding='utf-8'))
    rows = []
    for c in candidates:
        row = {k: c[k] for k in ('market_id', 'event_id', 'event_title', 'rule', 'cluster', 'question', 'end_date')}
        row['pre_price_exclusion'] = c['pre_price_exclusion']
        for label, cutoff in CUTOFFS.items():
            price, age, reason = (None, None, c['pre_price_exclusion']) if c['pre_price_exclusion'] else \
                price_at(c['yes_token'], cutoff, f'{c["market_id"]}_{label}')
            if not reason and age > MAX_AGE:
                reason = 'stale_price'
            if not reason and not BAND[0] <= price <= BAND[1]:
                reason = 'price_range'
            row[f'price_{label}'] = '' if price is None else f'{price:.4f}'
            row[f'age_s_{label}'] = '' if age is None else age
            row[f'eligible_{label}'] = 'yes' if not reason else 'no'
            row[f'reason_{label}'] = reason
        rows.append(row)
        print(row['market_id'], row['eligible_168h'], row['eligible_24h'], flush=True)
    write_csv(ROOT / 'selection.csv', rows)
    used = [r for r in rows if 'yes' in (r['eligible_168h'], r['eligible_24h'])]
    write_csv(ROOT / 'clusters.csv', [{'market_id': r['market_id'], 'cluster': r['cluster']} for r in used])
    n168 = sum(r['eligible_168h'] == 'yes' for r in rows)
    n24 = sum(r['eligible_24h'] == 'yes' for r in rows)
    print('candidates %d; eligible at 168h %d; at 24h %d; markets used %d; clusters %d'
          % (len(rows), n168, n24, len(used), len({r['cluster'] for r in used})))


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == '__main__':
    stage = sys.argv[1] if len(sys.argv) > 1 else ''
    if stage not in ('events', 'prices'):
        raise SystemExit(__doc__)
    try:
        stage_events() if stage == 'events' else stage_prices()
    finally:
        log = RAW / f'request_log_{stage}.json'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(LOG, indent=1), encoding='utf-8')
