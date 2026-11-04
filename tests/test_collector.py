import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts.run_collect import acquire_lock, unhealthy
from scripts.run_ingest import summarize
from src.common import iso
from src.db.loaders import connect, insert, rows
from src.ingestion import data_api, discovery, news
from src.ingestion.http import HTTPClient, read_archive

NOW = 1_800_000_000
DAY = 86400


def raw_market(i, days=5, **kw):
    base = dict(id=str(i), conditionId=f'0xc{i}', question=f'Will thing {i} happen?', description='Rules text',
                outcomes='["Yes", "No"]', clobTokenIds=f'["y{i}", "n{i}"]', outcomePrices='["0.4", "0.6"]',
                endDate=iso(NOW + days * DAY), active=True, closed=False, liquidityNum=5000, volumeNum=5000)
    base.update(kw)
    return base


def event(eid, markets, tags=('trump',)):
    return {'id': eid, 'tags': [{'slug': t} for t in tags], 'markets': markets}


SETTINGS = dict(gamma_base='https://g.test', tag_slugs=['trump'], page_size=100, max_pages=1,
                min_days_to_end=1.1, max_days_to_end=75, min_liquidity=1000, min_volume=1000,
                min_price=.02, max_price=.98, max_markets_per_event=4, max_markets_per_discovery=50,
                max_tracked=10, exclude_tag_slugs=['sports'],
                exclude_question_patterns=[r'(?i)\btweets?\b'], refresh_version_seconds=21600)


class FakeGamma:
    def __init__(self, payload, observed=NOW):
        self.payload, self.observed = payload, observed

    def get(self, url, params=None, **kwargs):
        return self.payload, self.observed, 'ref'


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(self.tmp.name + '/db.sqlite')
        self.cfg = {'polymarket': dict(SETTINGS)}

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_filters_and_event_cap(self):
        payload = [
            event('1', [raw_market(1), raw_market(2, closed=True), raw_market(3, days=200),
                        raw_market(4, question='Elon tweets how many?'), raw_market(5, liquidityNum=10),
                        raw_market(6, outcomePrices='["0.995", "0.005"]'), raw_market(7, days=0.5)]),
            event('2', [raw_market(10 + i, liquidityNum=5000 + i) for i in range(6)]),
            event('3', [raw_market(20)], tags=('sports', 'trump')),
        ]
        stored = discovery.collect_events(FakeGamma(payload), self.con, self.cfg)
        self.assertEqual(stored, 1 + 4)  # event 1: only market 1; event 2 capped at 4; event 3 excluded tag
        ids = {r['market_id'] for r in rows(self.con, 'SELECT market_id FROM markets')}
        self.assertEqual(ids, {'1', '12', '13', '14', '15'})  # highest-liquidity four of event 2
        summary = json.loads(rows(self.con, "SELECT detail_json FROM pipeline_events WHERE stage='gamma'")[0]['detail_json'])
        for reason in ('not_open', 'deadline_window', 'question_pattern', 'liquidity', 'price_range', 'excluded_tag'):
            self.assertIn(reason, summary['excluded'])
        self.assertEqual(summary['excluded']['per_event_cap'], 2)

    def test_deadline_day_cap_keeps_time_spread(self):
        year_end = [event(str(100 + i), [raw_market(100 + i, days=30, liquidityNum=9000 + i)]) for i in range(6)]
        early = [event('9', [raw_market(9, days=10, liquidityNum=1500)])]
        cfg = {'polymarket': {**SETTINGS, 'max_markets_per_deadline_day': 2}}
        discovery.collect_events(FakeGamma(year_end + early), self.con, cfg)
        ids = {r['market_id'] for r in rows(self.con, 'SELECT market_id FROM markets')}
        self.assertEqual(ids, {'105', '104', '9'})  # two most liquid on the crowded date, plus the early one
        summary = json.loads(rows(self.con, "SELECT detail_json FROM pipeline_events WHERE stage='gamma'")[0]['detail_json'])
        self.assertEqual(summary['excluded']['per_deadline_day_cap'], 4)

    def test_event_group_and_category_from_event_tags(self):
        discovery.collect_events(FakeGamma([event('77', [raw_market(1)], tags=('politics', 'trump'))]), self.con, self.cfg)
        market = rows(self.con, 'SELECT * FROM markets')[0]
        version = rows(self.con, 'SELECT * FROM market_versions')[0]
        self.assertEqual(market['event_group'], '77')
        self.assertEqual(version['category'], 'trump')

    def test_unchanged_version_not_restored_until_refresh(self):
        payload = [event('1', [raw_market(1)])]
        count = lambda: self.con.execute('SELECT COUNT(*) FROM market_versions').fetchone()[0]
        discovery.collect_events(FakeGamma(payload, NOW), self.con, self.cfg)
        discovery.collect_events(FakeGamma(payload, NOW + 600), self.con, self.cfg)
        self.assertEqual(count(), 1)
        discovery.collect_events(FakeGamma(payload, NOW + 7 * 3600), self.con, self.cfg)
        self.assertEqual(count(), 2)  # periodic refresh of volume/liquidity
        changed = [event('1', [raw_market(1, description='Rules text, amended')])]
        discovery.collect_events(FakeGamma(changed, NOW + 7 * 3600 + 60), self.con, self.cfg)
        self.assertEqual(count(), 3)  # a rule change is always stored

    def test_tracked_and_unresolved_queries(self):
        discovery.collect_events(FakeGamma([event('1', [raw_market(1, days=3)])]), self.con, self.cfg)
        self.assertEqual([m['market_id'] for m in discovery.tracked_markets(self.con, self.cfg, now=NOW + DAY)], ['1'])
        self.assertEqual(discovery.tracked_markets(self.con, self.cfg, now=NOW + 4 * DAY), [])
        self.assertEqual(len(discovery.unresolved_markets(self.con, self.cfg, now=NOW + 4 * DAY)), 1)
        self.assertEqual(discovery.unresolved_markets(self.con, self.cfg, now=NOW + 200 * DAY), [])


# Real /v2/resolutions records captured from the live API on 2026-11-04.
RESOLVED_NO = {'question_id': '0x05e9', 'condition_id': '0xc1', 'status': 'resolved', 'extended_review': False,
               'was_disputed': False, 'proposed_price': '0', 'reproposed_price': '69', 'price': '0',
               'last_update_timestamp': '1786220838'}
PROPOSED_YES = {'condition_id': '0xc1', 'status': 'proposed', 'proposed_price': '1000000000000000000',
                'reproposed_price': '69', 'price': '69', 'last_update_timestamp': '1793803273'}


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(self.tmp.name + '/db.sqlite')
        self.market = dict(market_id='1', condition_id='0xc1', event_group='e', yes_token='y', no_token='n',
                           yes_index=0, source_kind='real')
        insert(self.con, 'markets', self.market)
        self.cfg = {'polymarket': {'data_base': 'https://d.test'}}

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def collect(self, rows_):
        return data_api.collect_resolution(FakeGamma({'data': rows_}, NOW + 99), self.con, self.cfg, self.market)

    def test_parse_both_outcomes_and_index_order(self):
        yes_first, no_first = {'yes_index': 0}, {'yes_index': 1}
        self.assertEqual(data_api.parse_resolution(RESOLVED_NO, yes_first)[0], 0)
        self.assertEqual(data_api.parse_resolution({**RESOLVED_NO, 'price': str(10 ** 18)}, yes_first)[0], 1)
        self.assertEqual(data_api.parse_resolution({**RESOLVED_NO, 'price': str(10 ** 18)}, no_first)[0], 0)
        self.assertEqual(data_api.parse_resolution(RESOLVED_NO, no_first)[0], 1)

    def test_proposed_is_not_a_label_and_split_is_excluded(self):
        self.assertIsNone(data_api.parse_resolution(PROPOSED_YES, {'yes_index': 0}))
        with self.assertRaises(ValueError):
            data_api.parse_resolution({**RESOLVED_NO, 'price': str(5 * 10 ** 17)}, {'yes_index': 0})

    def test_collect_stores_only_final_labels(self):
        self.assertEqual(self.collect([PROPOSED_YES]), 0)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM resolutions').fetchone()[0], 0)
        self.assertEqual(self.collect([RESOLVED_NO]), 1)
        row = rows(self.con, 'SELECT * FROM resolutions')[0]
        self.assertEqual((row['outcome'], row['resolved_at'], row['label_available_at']), (0, 1786220838, NOW + 99))
        self.assertEqual(self.collect([RESOLVED_NO]), 0)  # recheck is a no-op
        with self.assertRaises(ValueError):
            self.collect([{**RESOLVED_NO, 'price': str(10 ** 18)}])  # a changed label is quarantined


    def test_batch_isolates_one_markets_problem(self):
        second = dict(self.market, market_id='2', condition_id='0xc2', yes_token='y2', no_token='n2')
        insert(self.con, 'markets', second)
        split = {**RESOLVED_NO, 'condition_id': '0xc2', 'price': str(5 * 10 ** 17)}
        client = FakeGamma({'data': [RESOLVED_NO, split]}, NOW + 99)
        with self.assertRaisesRegex(ValueError, '1 stored; 1 problem'):
            data_api.collect_resolutions(client, self.con, self.cfg, [self.market, second])
        self.assertEqual([r['market_id'] for r in rows(self.con, 'SELECT * FROM resolutions')], ['1'])


class GdeltTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(self.tmp.name + '/db.sqlite')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    class Seq:
        def __init__(self, *texts):
            self.texts = list(texts)

        def get(self, *args, **kwargs):
            return self.texts.pop(0), NOW, 'ref'

    def test_parse_distinguishes_empty_from_failure(self):
        self.assertEqual(news.parse_gdelt(''), [])
        self.assertEqual(news.parse_gdelt('{}'), [])
        with self.assertRaises(news.GdeltError):
            news.parse_gdelt('Please limit requests to one every 5 seconds.')
        with self.assertRaises(news.GdeltError):
            news.parse_gdelt('{broken')

    def test_throttle_text_is_retried_then_succeeds(self):
        good = json.dumps({'articles': [{'url': 'https://a.test/x', 'title': 'Shutdown looms', 'domain': 'a.test',
                                         'seendate': '20260101T000000Z'}]})
        with patch('src.ingestion.news.time.sleep') as sleeper:
            n = news.collect_gdelt(self.Seq('Please limit requests to one every 5 seconds.', good), self.con, 'q')
        self.assertEqual(n, 1)
        sleeper.assert_called_once()

    def test_empty_result_is_zero_not_error_and_garbage_raises(self):
        self.assertEqual(news.collect_gdelt(self.Seq(''), self.con, 'q'), 0)
        with self.assertRaises(news.GdeltError):
            news.collect_gdelt(self.Seq('Invalid query syntax'), self.con, 'q')


class FakeResponse(io.BytesIO):
    status = 200


class HTTPTests(unittest.TestCase):
    def client(self, tmp):
        return HTTPClient({'http': dict(timeout_seconds=1, retries=2, min_interval_seconds=0, user_agent='t'),
                           'data_dir': tmp})

    def test_raw_responses_bundled_per_day_and_individually_readable(self):
        bodies = [b'{"a": 1}', b'{"b": 2}']
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen',
                side_effect=[FakeResponse(bodies[0]), FakeResponse(bodies[1])]):
            client = self.client(tmp)
            (p1, _, ref1), (p2, _, ref2) = client.get('https://x.test/a'), client.get('https://x.test/b')
            self.assertEqual((p1, p2), ({'a': 1}, {'b': 2}))
            self.assertEqual(ref1.split('#')[0], ref2.split('#')[0])  # same daily bundle file
            self.assertNotEqual(ref1, ref2)
            self.assertTrue(ref1.split('#')[0].endswith('.jsonl.gz'))
            self.assertEqual(read_archive(ref1)['payload'], {'a': 1})
            self.assertEqual(read_archive(ref2)['payload'], {'b': 2})
            self.assertEqual(len([f for f in Path(tmp).rglob('*') if f.is_file()]), 1)

    def test_429_honours_retry_after_then_succeeds(self):
        limited = urllib.error.HTTPError('https://x.test/a', 429, 'Too Many', {'Retry-After': '3'}, None)
        with tempfile.TemporaryDirectory() as tmp, patch('src.ingestion.http.time.sleep') as sleeper, \
                patch('urllib.request.urlopen', side_effect=[limited, FakeResponse(b'{"ok": true}')]):
            payload, _, _ = self.client(tmp).get('https://x.test/a')
            self.assertEqual(payload, {'ok': True})
            self.assertIn(3, [c.args[0] for c in sleeper.call_args_list])

    def test_exhausted_retries_raise_visible_error(self):
        err = urllib.error.HTTPError('https://x.test/a', 503, 'x', {}, None)
        with tempfile.TemporaryDirectory() as tmp, patch('src.ingestion.http.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[err, err, err]):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
                self.client(tmp).get('https://x.test/a')


class RunnerTests(unittest.TestCase):
    def test_lock_blocks_overlap_and_replaces_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'a.lock'
            self.assertTrue(acquire_lock(lock))
            self.assertFalse(acquire_lock(lock))
            old = time.time() - 7200
            os.utime(lock, (old, old))
            self.assertTrue(acquire_lock(lock))

    def test_summary_and_health_rules(self):
        report = {'started_at': 1, 'finished_at': 2,
                  'successes': [{'stage': 'quotes', 'identity': str(i), 'result': 1} for i in range(9)]
                  + [{'stage': 'gdelt', 'identity': 'q', 'result': 0}],
                  'failures': [{'stage': 'quotes', 'identity': 'x', 'error': 'e'}]}
        summary = summarize(report)
        self.assertEqual(summary['zero_results'], {'gdelt': 1})
        self.assertFalse(unhealthy(summary, 'quotes'))  # 1 closed book of 10 is normal
        self.assertTrue(unhealthy({'failed': {'rss': 1}, 'ok': {}}, 'rss'))


if __name__ == '__main__':
    unittest.main()
