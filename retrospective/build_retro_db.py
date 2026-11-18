"""B3 (decisions log R10, 2026-11-19): build the replay database records from files saved on 13 Nov. No web requests.

    python -m retrospective.build_retro_db            # writes data/retro/out/*.jsonl + rules_flags.csv, prints a summary

Then import (order matters: markets first), with the replay config only:
    python -m scripts.import_data --config configs/config_retro.yaml --kind markets --file data/retro/out/markets.jsonl
    python -m scripts.import_data --config configs/config_retro.yaml --kind market_versions --archive-verified --file data/retro/out/market_versions.jsonl
    python -m scripts.import_data --config configs/config_retro.yaml --kind prices --archive-verified --file data/retro/out/prices.jsonl

Inputs (git-ignored, on the VM): data/retro/raw/gamma/events_*.json.gz (Gamma event pages) and
data/retro/raw/prices/<market_id>_<168h|24h>.json.gz (1-minute history of the hour before each cutoff).
Markets = those eligible at either cutoff in retrospective/selection.csv (committed fb2095a). Values follow the
retrospective spec (docs/retrospective_collection.md WP2, D4):
  * YES token taken from the `outcomes` list, never by position;
  * text version: question, rules = market description, deadline = the event end date (log 2026-11-13),
    available_at = market creation time (WP2 fallback; no archived market page was checked: deadline_verified = no);
  * price: one row per history point, available_at = point time + 60 s (conservative), history_unspecified.
Rules text is TODAY's text, so every market whose rules contain an added heading (Update:/Clarification/Note:/Edit)
or a date between the first cutoff and the end of November other than election day is listed in rules_flags.csv
for review BEFORE any snapshot is built.
"""
import csv
import gzip
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / 'data' / 'retro' / 'raw'
OUT = ROOT / 'data' / 'retro' / 'out'
SELECTION = ROOT / 'retrospective' / 'selection.csv'
DEADLINE = '2026-11-04T04:59:00Z'
CUTOFFS = {'168h': '2026-10-28T04:59:00Z', '24h': '2026-11-03T04:59:00Z'}
FIDELITY_SECONDS = 60
MONTHS = {m: i for i, m in enumerate(('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'), 1)}
HEADING = re.compile(r'(?im)^\s*(?:update|clarification|note|edit(?:ed)?)\b\s*[:(\-]|\bclarif(?:y|ied|ication)\b')
DATE = re.compile(r'(?i)\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),?\s+(20\d{2})\b|\b(20\d{2})-(\d{2})-(\d{2})\b')
FLAG_FROM, FLAG_TO, ELECTION_DAY = (datetime(2026, 10, 28).date(), datetime(2026, 11, 30).date(),
                                    datetime(2026, 11, 3).date())


def ts(iso):
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def rel(path):
    path = pathlib.Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def rules_flags(text):
    """Reasons a rules text may contain material added after the first cutoff (empty list = nothing found)."""
    reasons = []
    if HEADING.search(text):
        reasons.append('added_heading')
    for m in DATE.finditer(text):
        if m.group(1):
            day = datetime(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))).date()
        else:
            day = datetime(int(m.group(4)), int(m.group(5)), int(m.group(6))).date()
        if FLAG_FROM <= day <= FLAG_TO and day != ELECTION_DAY:
            reasons.append(f'date_{day.isoformat()}')
    return reasons


def load_pages(raw=RAW):
    """market_id -> (market dict, event dict, page path). A market listed twice must be identical."""
    found = {}
    pages = sorted((raw / 'gamma').glob('events_*.json.gz'))
    if not pages:
        raise SystemExit(f'No Gamma pages under {raw / "gamma"}: run this on the VM')
    for page in pages:
        data = json.loads(gzip.decompress(page.read_bytes()).decode('utf-8'))
        events = data if isinstance(data, list) else data.get('events') or []
        for e in events:
            for m in e.get('markets') or []:
                key = str(m['id'])
                if key in found and found[key][0] != m:
                    raise SystemExit(f'Market {key} differs between pages')
                found.setdefault(key, (m, e, page))
    return found


def market_records(sel, market, event, page, observed):
    outcomes, tokens = json.loads(market['outcomes']), json.loads(market['clobTokenIds'])
    if sorted(outcomes) != ['No', 'Yes'] or len(tokens) != 2:
        raise ValueError(f"{sel['market_id']}: not a Yes/No market")
    yes = outcomes.index('Yes')
    if not event.get('endDate') or ts(event['endDate']) != ts(DEADLINE):
        raise ValueError(f"{sel['market_id']}: event end date {event.get('endDate')} is not the deadline")
    if market['question'].strip() != sel['question'].strip():
        raise ValueError(f"{sel['market_id']}: question differs from selection.csv")
    mid = sel['market_id']
    row = dict(market_id=mid, condition_id=market['conditionId'], event_group=sel['cluster'],
               yes_token=tokens[yes], no_token=tokens[1 - yes], yes_index=yes, source_kind='real')
    version = dict(version_id=f'retro:{mid}:v1', market_id=mid, available_at=ts(market['createdAt']),
                   observed_at=observed, question=market['question'].strip(), rules=market.get('description') or '',
                   scheduled_end=ts(DEADLINE), category='politics', is_open=1, liquidity=None, volume=None,
                   provenance='archive_verified', raw_ref=rel(page))
    return row, version


def price_records(mid, raw=RAW):
    """All saved history points of both windows for one market, deduplicated by time."""
    out, missing = {}, []
    for label, cutoff in CUTOFFS.items():
        path = raw / 'prices' / f'{mid}_{label}.json.gz'
        if not path.exists():
            missing.append(label)
            continue
        observed = int(path.stat().st_mtime)
        for p in json.loads(gzip.decompress(path.read_bytes()).decode('utf-8')).get('history', []):
            t = int(p['t'])
            if t > ts(cutoff):
                continue
            out.setdefault(t, dict(price_id=f'retro:{mid}:{t}:{FIDELITY_SECONDS}', market_id=mid, timestamp=t,
                                   available_at=t + FIDELITY_SECONDS, observed_at=observed, price=float(p['p']),
                                   price_kind='history_unspecified', resolution_seconds=FIDELITY_SECONDS,
                                   bid=None, ask=None, provenance='archive_verified', raw_ref=rel(path)))
    return [out[t] for t in sorted(out)], missing


def eligible_selection(path=SELECTION):
    with open(path, encoding='utf-8') as f:
        return [r for r in csv.DictReader(f) if 'yes' in (r['eligible_168h'], r['eligible_24h'])]


def build(raw=RAW, out=OUT, selection=SELECTION):
    rows = eligible_selection(selection)
    pages = load_pages(raw)
    markets, versions, prices, flags, no_prices = [], [], [], [], []
    for sel in rows:
        if sel['market_id'] not in pages:
            raise SystemExit(f"Market {sel['market_id']} is not in the saved Gamma pages")
        market, event, page = pages[sel['market_id']]
        row, version = market_records(sel, market, event, page, int(page.stat().st_mtime))
        points, missing = price_records(sel['market_id'], raw)
        markets.append(row)
        versions.append(version)
        prices.extend(points)
        if missing:
            no_prices.append((sel['market_id'], missing))
        for reason in rules_flags(version['rules']):
            flags.append(dict(market_id=sel['market_id'], question=version['question'], reason=reason))
    out.mkdir(parents=True, exist_ok=True)
    for name, records in (('markets', markets), ('market_versions', versions), ('prices', prices)):
        with open(out / f'{name}.jsonl', 'w', encoding='utf-8', newline='\n') as f:
            for r in records:
                f.write(json.dumps(r, sort_keys=True) + '\n')
    with open(out / 'rules_flags.csv', 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['market_id', 'question', 'reason'])
        writer.writeheader()
        writer.writerows(flags)
    created = sorted(v['available_at'] for v in versions)
    summary = dict(markets=len(markets), versions=len(versions), prices=len(prices),
                   markets_missing_a_price_file=no_prices, rules_flags=len(flags),
                   markets_with_rules_flags=len({f['market_id'] for f in flags}),
                   text_available_from=iso(created[0]) if created else None,
                   text_available_latest=iso(created[-1]) if created else None,
                   texts_created_after_first_cutoff=sum(c > ts(CUTOFFS['168h']) for c in created))
    return summary, flags


def main():
    summary, flags = build()
    print(json.dumps(summary, indent=1))
    for f in flags:
        print('FLAG', f['market_id'], f['reason'], f['question'])
    print('Review data/retro/out/rules_flags.csv before importing.' if flags else 'No rules-text flags.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
