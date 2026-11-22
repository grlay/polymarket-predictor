"""B4 archived news (fetch_archive) and the N24 coverage count (count_coverage); decisions log R11."""
import gzip
import json
import tempfile
import unittest
import urllib.error
from copy import deepcopy
from pathlib import Path

from retrospective import fetch_archive as fa
from retrospective.count_coverage import coverage, decision, open_readonly
from src.common import load_config
from src.dataset.snapshots import build
from src.db.loaders import add_document, connect, insert
from tests.test_replay import C168, C24, FakeRaw, GA_SEN, gamma_market

FEED = 'https://rss.politico.com/politics-news.xml'


def rss(*items):
    body = ''.join(f'<item><title>{t}</title><link>{u}</link><description>{d}</description>'
                   f'<pubDate>{p}</pubDate></item>' for t, u, d, p in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw, self.out = Path(self.tmp.name) / 'raw', Path(self.tmp.name) / 'out'
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, requested, xml, served=None, feed=FEED):
        path = self.raw / fa.slug(feed) / f'{requested}.xml.gz'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(xml))
        served = served or requested
        self.log.append(dict(feed=feed, requested=requested, served=served, status=200, downloaded=1_800_000_000,
                             url=f'https://web.archive.org/web/{served}id_/{feed}', path=path.relative_to(self.raw).as_posix()))

    def parse(self):
        (self.raw / 'fetch_log.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.log), encoding='utf-8')
        summary, n = fa.stage_parse(self.raw, self.out)
        docs = [json.loads(l) for l in (self.out / 'documents.jsonl').read_text(encoding='utf-8').splitlines()]
        return summary[FEED], docs

    def test_identical_item_keeps_its_first_capture_and_an_edit_is_a_new_version(self):
        a = ('Georgia Senate race tightens', 'https://p/1', 'Poll finds a close race.', 'Sat, 31 Oct 2026 10:00:00 GMT')
        edited = ('Georgia Senate race tightens further', 'https://p/1', 'Poll finds a close race.', a[3])
        self.capture('20261031120000', rss(a))
        self.capture('20261101120000', rss(a, edited))
        report, docs = self.parse()
        self.assertEqual([(d['title'], d['available_at']) for d in docs],
                         [(a[0], fa.wb_time('20261031120000')), (edited[0], fa.wb_time('20261101120000'))])
        self.assertEqual({d['provenance'] for d in docs}, {'archive_verified'})
        self.assertEqual({d['content_kind'] for d in docs}, {'feed_summary'})
        self.assertEqual(report['documents'], 2)
        self.assertTrue(docs[0]['raw_ref'].startswith('rss_politico_com_politics_news_xml/20261031120000.xml.gz https://web.archive.org/web/'))

    def test_served_time_is_used_and_later_than_cutoff_is_dropped(self):
        item = ('Senate race poll', 'https://p/2', 'x', 'Sat, 31 Oct 2026 10:00:00 GMT')
        self.capture('20261102000000', rss(item), served='20261102060000')       # redirected, still before cutoff
        self.capture('20261103040000', rss(('Late item', 'https://p/3', 'y', 'Tue, 03 Nov 2026 03:00:00 GMT')),
                     served='20261103051500')                                     # served after the 24 h cutoff
        report, docs = self.parse()
        self.assertEqual([(d['title'], d['available_at']) for d in docs],
                         [('Senate race poll', fa.wb_time('20261102060000'))])
        self.assertEqual(report['capture_served_after_cutoff'], 1)

    def test_feed_sent_gzip_compressed_is_unpacked_and_counted(self):
        item = ('Georgia Senate race poll', 'https://p/5', 'x', 'Sat, 31 Oct 2026 10:00:00 GMT')
        self.capture('20261031120000', gzip.compress(rss(item)))          # what Politico and BBC World served
        report, docs = self.parse()
        self.assertEqual([d['title'] for d in docs], ['Georgia Senate race poll'])
        self.assertEqual(report['captures_content_gzip'], 1)
        self.assertNotIn('capture_unparsable_ParseError', report)

    def test_item_dated_after_its_capture_and_broken_captures_are_counted_not_used(self):
        self.capture('20261031120000', rss(('Future', 'https://p/4', 'z', 'Sun, 01 Nov 2026 10:00:00 GMT')))
        self.capture('20261031130000', b'<html>Wayback error page</html><p>')
        report, docs = self.parse()
        self.assertEqual(docs, [])
        self.assertEqual(report['items_dropped_pubdate_after_capture_or_no_date'], 1)
        self.assertEqual(sum(v for k, v in report.items() if k.startswith('capture_unparsable')), 1)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_then_fetch_records_the_served_capture_and_resumes(self):
        calls = []

        def get(url):
            calls.append(url)
            if '/cdx/' in url:
                if 'politico' in url:
                    return 200, url, json.dumps([['timestamp', 'original', 'digest'],
                                                 ['20261031120000', 'https://rss.politico.com/politics-news.xml', 'A'],
                                                 ['20261101120000', 'https://rss.politico.com/politics-news.xml', 'B']]).encode()
                return 200, url, b'[]'
            return 200, url.replace('20261031120000', '20261031120500'), rss()

        report = fa.stage_cdx(self.raw, get=get, sleep=lambda s: None)
        self.assertEqual(report[FEED], 2)
        self.assertEqual(sum(1 for c in calls if '/cdx/' in c), len(fa.feeds()))
        fa.stage_fetch(self.raw, get=get, sleep=lambda s: None)
        log = [json.loads(l) for l in (self.raw / 'fetch_log.jsonl').read_text().splitlines()]
        self.assertEqual([(r['requested'], r['served']) for r in log],
                         [('20261031120000', '20261031120500'), ('20261101120000', '20261101120000')])
        before = len(calls)
        fa.stage_cdx(self.raw, get=get, sleep=lambda s: None)   # index files are reused
        fa.stage_fetch(self.raw, get=get, sleep=lambda s: None)   # nothing left to fetch
        self.assertEqual(len(calls), before)

    def test_http_get_waits_on_429_and_gives_up_cleanly(self):
        attempts, waits = [], []

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return 'https://web.archive.org/web/20261031120000id_/x'
            def read(self): return b'ok'

        def opener(req, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(req.full_url, 429, 'Too Many', {}, None)
            return Response()

        self.assertEqual(fa.http_get('https://web.archive.org/x', opener=opener, sleep=waits.append)[0], 200)
        self.assertEqual(waits, [fa.WAIT, fa.WAIT])

        def always_429(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, 'Too Many', {}, None)
        self.assertEqual(fa.http_get('https://web.archive.org/x', opener=always_429, sleep=lambda s: None)[0], 'gave_up_429')

        def forbidden(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, 'Forbidden', {}, None)
        self.assertEqual(fa.http_get('https://web.archive.org/x', opener=forbidden, sleep=lambda s: None)[0], 403)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        histories = {('101', '168h'): [(C168 - 600, .41)], ('101', '24h'): [(C24 - 300, .47)],
                     ('201', '168h'): [(C168 - 600, .30)], ('201', '24h'): [(C24 - 300, .35)]}
        fake = FakeRaw(root, [gamma_market('101', GA_SEN),
                              gamma_market('201', 'Will the Democrats win the Texas Senate race in 2026?')], histories)
        fake.build()
        self.cfg = deepcopy(load_config('configs/config_retro.yaml'))
        self.cfg['database'] = str(root / 'retro.sqlite')
        con = connect(self.cfg['database'])
        for name in ('markets', 'market_versions', 'prices'):
            for r in fake.records(name):
                if name == 'markets' and r['market_id'] == '201':
                    r['event_group'] = 'event_tx'          # a second race cluster
                insert(con, name, r)
        build(con, self.cfg)
        self.con = con

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add(self, title, when):
        add_document(self.con, dict(url='https://p/' + title, title=title, body='', source='rss.politico.com',
                                    source_type='news', published_at=when, available_at=when, observed_at=C24 + 10**6,
                                    provenance='archive_verified', raw_ref='ref', source_kind='real',
                                    content_kind='headline'))

    def report(self):
        ro = open_readonly(self.cfg['database'])
        try:
            return coverage(ro, self.cfg)
        finally:
            ro.close()

    def test_counts_clusters_per_cutoff_with_reasons(self):
        self.add('Georgia Senate race tightens, poll finds', C168 - 86400)   # visible at both cutoffs
        self.add('Georgia Senate candidates meet in final debate', C24 - 86400)  # only at 24 h
        self.add('Texas Senate votes on property tax bill', C24 - 86400)    # state legislature: rejected
        r = self.report()
        self.assertEqual((r['N24'], r['N168']), (1, 1))
        self.assertEqual(r['24h']['clusters_without_document'], ['event_tx'])
        self.assertEqual(r['24h']['documents_per_cluster'], {0: 1, 2: 1})
        self.assertEqual(r['24h']['n_clusters_with_3_documents'], 0)
        reasons = r['24h']['reasons']
        self.assertEqual(reasons['race:admitted'], 2)              # both Georgia headlines, Georgia market
        self.assertEqual(reasons['race:no_election_context'], 1)   # Texas legislature headline, Texas market
        self.assertEqual(reasons['race:no_race_entity'], 3)        # other state's headlines, each market
        self.assertEqual(r['unprofiled_snapshots'], [])
        self.assertEqual(r['R11_decision'], 'NO-GO')    # 1 cluster < 10

    def test_decision_thresholds_are_the_r11_ones(self):
        self.assertEqual([decision(n) for n in (0, 9, 10, 29, 30, 59)],
                         ['NO-GO', 'NO-GO', 'PARTIAL', 'PARTIAL', 'GO', 'GO'])
        self.assertEqual(decision(40, documents_imported=False), 'NO-GO')

    def test_the_database_is_not_written(self):
        before = self.con.execute('SELECT COUNT(*) FROM pipeline_events').fetchone()[0]
        self.report()
        ro = open_readonly(self.cfg['database'])
        with self.assertRaises(Exception):
            ro.execute("INSERT INTO pipeline_events(created_at,stage,status,detail_json) VALUES(1,'x','y','{}')")
        ro.close()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM pipeline_events').fetchone()[0], before)


if __name__ == '__main__':
    unittest.main()
