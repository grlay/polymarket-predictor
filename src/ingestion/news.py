"""RSS/Atom and GDELT discovery. Headline-only records are explicitly labelled."""
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from src.common import timestamp
from src.db.loaders import add_document


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    parser = TextParser()
    parser.feed(text or '')
    return unescape(' '.join(parser.parts)).strip()


# Aggregator feeds link to the aggregator, not the publisher. The publisher is named in an RSS <source url="...">
# element. Only listed aggregators are re-attributed, so every ordinary feed keeps its link host unchanged.
AGGREGATOR_HOSTS = {'news.google.com'}


def publisher_host(url, fields):
    """Host used as the document source: the link host, or the publisher for a known aggregator."""
    host = urlsplit(url).netloc
    if host not in AGGREGATOR_HOSTS:
        return host
    node = fields.get('source')
    if node is not None:
        origin = urlsplit(node.get('url') or '')
        if origin.scheme in ('http', 'https') and origin.netloc:
            return origin.netloc
    return host


def rss_records(xml, observed, ref):
    root = ET.fromstring(xml)
    for item in list(root.findall('.//item')) + list(root.findall('{http://www.w3.org/2005/Atom}entry')):
        fields = {node.tag.split('}')[-1]: node for node in item}
        def value(key):
            node = fields.get(key)
            return '' if node is None else (node.text or '')
        link_node = fields.get('link')
        url = value('link') or (link_node.get('href', '') if link_node is not None else '')
        when = value('pubDate') or value('published') or value('updated')
        if not url or not when:
            continue
        try:
            published = timestamp(when)
        except ValueError:
            dt = parsedate_to_datetime(when)
            if dt.tzinfo is None:
                continue
            published = int(dt.timestamp())
        if published > observed:
            continue
        body = plain(value('description') or value('summary') or value('content'))
        yield dict(url=url, title=plain(value('title')), body=body,
                   source=publisher_host(url, fields), source_type='news', published_at=published,
                   available_at=observed, observed_at=observed, provenance='prospective',
                   raw_ref=ref, source_kind='real', content_kind='feed_summary' if body else 'headline')


def collect_rss(client, con, url):
    payload, observed, ref = client.get(url, json_response=False)
    return sum(int(add_document(con, r)) for r in rss_records(payload, observed, ref))


GDELT_URL = 'https://api.gdeltproject.org/api/v2/doc/doc'


class GdeltError(RuntimeError):
    """A failed GDELT request. Distinct from a successful request that found no articles."""


def parse_gdelt(text):
    """Return the article list. [] means a genuine empty result; failures raise GdeltError."""
    body = text.strip()
    if not body:
        return []  # GDELT answers an empty body when nothing matches
    if body[0] != '{':
        # e.g. "Please limit requests to one every 5 seconds" or a query-syntax error, sent with HTTP 200
        raise GdeltError('GDELT non-JSON reply: ' + body[:100].replace(chr(10), ' '))
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise GdeltError('GDELT invalid JSON: ' + body[:100].replace(chr(10), ' ')) from exc
    return data.get('articles', [])


def collect_gdelt(client, con, query, attempts=3, wait=15):
    """GDELT allows ~1 request per 5 s and signals throttling with HTTP 429 or a text body."""
    last = None
    for attempt in range(attempts):
        text, observed, ref = client.get(GDELT_URL,
            {'query': query, 'mode': 'artlist', 'format': 'json', 'maxrecords': 100, 'timespan': '1d'},
            json_response=False, retries=3, min_interval=6, backoff=wait)
        try:
            articles = parse_gdelt(text)
            break
        except GdeltError as exc:
            last = exc
            if 'limit requests' not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(wait)
    else:
        raise last
    n = 0
    for article in articles:
        try:
            # GDELT seendate is discovery time, not guaranteed original publication time.
            seen = int(datetime.strptime(article['seendate'], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp())
            url, title = article['url'], article['title']
        except (KeyError, ValueError):
            continue
        if seen > observed:
            continue
        n += int(add_document(con, dict(url=url, title=title, body='',
            source=article.get('domain') or urlsplit(url).netloc, source_type='news',
            published_at=seen, available_at=observed, observed_at=observed,
            provenance='prospective', raw_ref=ref, source_kind='real', content_kind='headline')))
    return n
