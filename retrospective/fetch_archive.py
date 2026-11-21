"""B4 (decisions log R11; retrospective spec D5): archived news feeds from the Wayback Machine for the replay.

Run on the VM (Wayback blocks the laptop's network). Each stage can be rerun; nothing already saved is fetched again.

    python -m retrospective.fetch_archive cdx      # 15 index requests: distinct versions of each live feed in the window
    python -m retrospective.fetch_archive fetch    # download every listed capture (~1 request / 2 s; resumable)
    python -m retrospective.fetch_archive parse    # offline: captures -> data/retro/out/documents.jsonl + report

Then import with the replay config:
    python -m scripts.import_data --config configs/config_retro.yaml --kind documents --file data/retro/out/documents.jsonl

Rules (D5): sources = the live collector's feeds (configs/collect.yaml); window 2026-09-28 to the 24 h cutoff;
available_at = the time of the capture actually SERVED (Wayback may redirect to a nearby capture; a capture served from
after the cutoff is dropped); published_at = the item's pubDate; items whose pubDate is later than the capture are
dropped (done by the live parser); provenance archive_verified; observed_at = download time; raw_ref = saved file +
Wayback URL. Items are parsed by the live collector's own function, so archived and live documents are identical in form.
Captures are processed oldest first, and the database keeps an identical item once, with its first capture time.
"""
import gzip
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

import yaml

from src.ingestion.news import rss_records

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / 'data' / 'retro' / 'raw' / 'wayback'
OUT = ROOT / 'data' / 'retro' / 'out'
FEEDS_FILE = ROOT / 'configs' / 'collect.yaml'
WINDOW_FROM = '20260928000000'
LAST_CUTOFF = '20261103045900'          # 2026-11-03T04:59:00Z, the 24 h cutoff
UA = {'User-Agent': 'HX2605-academic-research/0.1'}
PAUSE, WAIT, TRIES = 2.0, 60, 5
SERVED = re.compile(r'/web/(\d{14})id_/')


def wb_time(stamp):
    return int(datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp())


def feeds():
    return yaml.safe_load(FEEDS_FILE.read_text(encoding='utf-8'))['news']['rss_urls']


def slug(url):
    return re.sub(r'[^A-Za-z0-9]+', '_', re.sub(r'^https?://', '', url)).strip('_')[:80]


def http_get(url, opener=urllib.request.urlopen, sleep=time.sleep):
    """(status, final_url, body). Waits and retries on 429/5xx/network errors; other HTTP errors return their status."""
    last = None
    for attempt in range(TRIES):
        try:
            with opener(urllib.request.Request(url, headers=UA), timeout=90) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                return exc.code, url, b''
            last = exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = type(exc).__name__
        sleep(WAIT)
    return f'gave_up_{last}', url, b''


def stage_cdx(raw=RAW, get=http_get, sleep=time.sleep):
    (raw / 'cdx').mkdir(parents=True, exist_ok=True)
    report = {}
    for feed in feeds():
        path = raw / 'cdx' / f'{slug(feed)}.json.gz'
        if path.exists():
            rows = json.loads(gzip.decompress(path.read_bytes()))
        else:
            query = urllib.parse.urlencode({'url': re.sub(r'^https?://', '', feed), 'from': WINDOW_FROM,
                                            'to': LAST_CUTOFF, 'output': 'json', 'filter': 'statuscode:200',
                                            'collapse': 'digest', 'fl': 'timestamp,original,digest'})
            status, _, body = get('https://web.archive.org/cdx/search/cdx?' + query)
            sleep(PAUSE)
            if status != 200:
                report[feed] = f'index_failed_{status}'
                continue
            rows = json.loads(body) if body.strip() else []
            path.write_bytes(gzip.compress(json.dumps(rows).encode('utf-8')))
        report[feed] = max(len(rows) - 1, 0)    # first row is the header
        print(f'{report[feed]!s:>6}  {feed}', flush=True)
    return report


def captures(raw=RAW):
    """(feed, requested timestamp, original url) for every capture listed by the saved index files."""
    out = []
    for feed in feeds():
        path = raw / 'cdx' / f'{slug(feed)}.json.gz'
        if not path.exists():
            continue
        rows = json.loads(gzip.decompress(path.read_bytes()))
        head = rows[0] if rows else []
        for row in rows[1:]:
            rec = dict(zip(head, row))
            out.append((feed, rec['timestamp'], rec['original']))
    return out


def stage_fetch(raw=RAW, get=http_get, sleep=time.sleep):
    log_path = raw / 'fetch_log.jsonl'
    done = set()
    if log_path.exists():
        for line in log_path.read_text(encoding='utf-8').splitlines():
            rec = json.loads(line)
            if rec['status'] == 200:
                done.add((rec['feed'], rec['requested']))
    todo = [c for c in captures(raw) if (c[0], c[1]) not in done]
    print(f'captures listed {len(captures(raw))}; already saved {len(done)}; to fetch {len(todo)}', flush=True)
    counts = Counter()
    with open(log_path, 'a', encoding='utf-8') as log:
        for i, (feed, stamp, original) in enumerate(todo, 1):
            url = f'https://web.archive.org/web/{stamp}id_/{original}'
            status, final, body = get(url)
            served = SERVED.search(final)
            rec = dict(feed=feed, requested=stamp, served=served.group(1) if served else stamp, status=status,
                       url=final, downloaded=int(time.time()))
            if status == 200:
                path = raw / slug(feed) / f'{stamp}.xml.gz'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(body))
                rec['path'] = path.relative_to(raw).as_posix()
            log.write(json.dumps(rec) + '\n')
            log.flush()
            counts[status] += 1
            if i % 100 == 0:
                print(f'{i}/{len(todo)} {dict(counts)}', flush=True)
            sleep(PAUSE)
    print('fetch done', dict(counts), flush=True)
    return counts


def stage_parse(raw=RAW, out=OUT):
    """Saved captures -> documents.jsonl, oldest served capture first. Returns the per-feed report."""
    log_path = raw / 'fetch_log.jsonl'
    if not log_path.exists():
        raise SystemExit('No fetch_log.jsonl: run the fetch stage first')
    saved = {}
    for line in log_path.read_text(encoding='utf-8').splitlines():
        rec = json.loads(line)
        if rec['status'] == 200 and rec.get('path'):
            saved[(rec['feed'], rec['requested'])] = rec     # a later successful retry replaces an earlier one
    report = {f: Counter() for f in feeds()}
    seen, documents = set(), []
    for rec in sorted(saved.values(), key=lambda r: (r['served'], r['feed'])):
        r = report.setdefault(rec['feed'], Counter())
        if rec['served'] > LAST_CUTOFF:
            r['capture_served_after_cutoff'] += 1
            continue
        captured = wb_time(rec['served'])
        try:
            xml = gzip.decompress((raw / rec['path']).read_bytes())
            items = list(rss_records(xml, captured, ref=f"{rec['path']} {rec['url']}"))
        except Exception as exc:          # an archived HTML error page or broken XML: count it, never guess
            r['capture_unparsable_' + type(exc).__name__] += 1
            continue
        r['captures_used'] += 1
        total_items = len(re.findall(rb'<item[\s>]|<entry[\s>]', xml))
        r['items_dropped_pubdate_after_capture_or_no_date'] += max(total_items - len(items), 0)
        for doc in items:
            key = (doc['url'], doc['title'], doc['body'])
            if key in seen:
                continue                  # identical item already stored with an earlier capture time
            seen.add(key)
            doc.update(provenance='archive_verified', observed_at=int(rec['downloaded']))
            documents.append(doc)
            r['documents'] += 1
    out.mkdir(parents=True, exist_ok=True)
    with open(out / 'documents.jsonl', 'w', encoding='utf-8', newline='\n') as f:
        for doc in documents:
            f.write(json.dumps(doc, sort_keys=True) + '\n')
    summary = {feed: dict(c) for feed, c in report.items()}
    (out / 'archive_report.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    return summary, len(documents)


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else ''
    if stage == 'cdx':
        report = stage_cdx()
        print('index total', sum(v for v in report.values() if isinstance(v, int)),
              '| failed:', {k: v for k, v in report.items() if not isinstance(v, int)} or 'none')
    elif stage == 'fetch':
        stage_fetch()
    elif stage == 'parse':
        summary, n = stage_parse()
        for feed, c in summary.items():
            print(f"{c.get('documents', 0):>6} docs  {c.get('captures_used', 0):>5} captures  {feed}  "
                  + ', '.join(f'{k} {v}' for k, v in c.items() if k not in ('documents', 'captures_used')))
        print('documents total', n)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
