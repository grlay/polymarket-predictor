"""Bounded GET client with immutable (gzip) raw-response archive and visible failures."""
import gzip
import json
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from src.common import digest, utcnow

RETRYABLE = (429, 500, 502, 503, 504)


def archive(path, record):
    """Append one raw response as its own gzip member; return its (offset, length).

    Concatenated gzip members form a valid gzip file, and every response stays individually
    retrievable through the byte range kept in raw_ref. This avoids millions of tiny files.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    member = gzip.compress(json.dumps(record, sort_keys=True, allow_nan=False).encode('utf-8') + b'\n', 6)
    offset = path.stat().st_size if path.exists() else 0
    with path.open('ab') as stream:
        stream.write(member)
    return offset, len(member)


def read_archive(ref):
    """Read a response by raw_ref ('path#offset+length', or a plain single-response file)."""
    path, _, span = str(ref).partition('#')
    if span:
        offset, length = (int(x) for x in span.split('+'))
        with open(path, 'rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
    else:
        data = Path(path).read_bytes()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return json.loads(data.decode('utf-8'))


class HTTPClient:
    def __init__(self, cfg, bucket='ingest'):
        self.settings = cfg['http']
        self.raw_dir = Path(cfg['data_dir']) / 'raw'
        self.bucket = bucket  # one archive file per bucket per UTC day (no cross-process interleaving)
        self.last_request = 0.0

    def get(self, url, params=None, json_response=True, retries=None, min_interval=None, backoff=None):
        """retries/min_interval/backoff override the config for one call (e.g. rate-limited GDELT)."""
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise ValueError('Only HTTPS source URLs are supported')
        retries = self.settings['retries'] if retries is None else retries
        interval = self.settings['min_interval_seconds'] if min_interval is None else min_interval
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
        full = url + (('?' if '?' not in url else '&') + query if query else '')
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(retries + 1):
            pause = interval - (time.monotonic() - self.last_request)
            if pause > 0:
                time.sleep(pause)
            self.last_request = time.monotonic()
            req = urllib.request.Request(full, headers={'User-Agent': self.settings['user_agent']})
            wait = backoff if backoff is not None else min(2 ** attempt, 4)
            try:
                with urllib.request.urlopen(req, timeout=self.settings['timeout_seconds']) as response:
                    raw = response.read(10_000_001)
                    if len(raw) > 10_000_000:
                        raise ValueError('Source response exceeded 10 MB bound')
                    observed = utcnow()
                    text = raw.decode('utf-8')
                    payload = json.loads(text) if json_response else text
                    day = datetime.fromtimestamp(observed, timezone.utc).strftime('%Y-%m-%d')
                    path = self.raw_dir / day / f'{self.bucket}.jsonl.gz'
                    offset, length = archive(path, {'url': full, 'observed_at': observed,
                        'status': response.status, 'content_digest': digest(text), 'payload': payload})
                    return payload, observed, f'{path}#{offset}+{length}'
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE or attempt == retries:
                    raise RuntimeError(f'HTTP {exc.code} from {host}') from None
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                time.sleep(min(int(retry_after), 60) if retry_after and retry_after.isdigit() else wait)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == retries:
                    raise RuntimeError(f'Network failure at {host}: {type(exc).__name__}') from None
                time.sleep(wait)
        raise RuntimeError('Exhausted HTTP retries')
