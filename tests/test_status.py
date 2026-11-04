import gzip
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup_db import backup
from scripts.collector_status import build_report, find_gaps, profile_report, render
from scripts.run_collect import INTERVAL_MINUTES
from src.db.loaders import connect, insert

NOW = 1_800_000_000
MIN = 60


def run(profile, started, failed=None, ok=None):
    return {'profile': profile, 'started_at': started, 'finished_at': started + 5, 'ok': ok or {profile: 1},
            'failed': failed or {}, 'zero_results': {}, 'sample_failures': []}


class StatusTests(unittest.TestCase):
    def test_find_gaps_ignores_normal_jitter(self):
        times = [0, 600, 1200, 1900, 5000, 5600]  # 10-minute interval; one 51-minute hole
        self.assertEqual(find_gaps(times, 600), [(1900, 5000)])

    def test_status_ok_stale_never(self):
        fresh = [run('quotes', NOW - i * 10 * MIN) for i in range(1, 50)]
        self.assertEqual(profile_report('quotes', fresh, NOW)['status'], 'OK')
        stale = [run('quotes', NOW - 3 * 3600 - i * 10 * MIN) for i in range(50)]
        report = profile_report('quotes', stale, NOW)
        self.assertEqual(report['status'], 'STALE')
        self.assertGreaterEqual(report['longest_gaps'][0]['hours'], 3)  # the open gap up to now is reported
        self.assertEqual(profile_report('rss', fresh, NOW)['status'], 'NEVER')

    def test_unhealthy_runs_counted_per_profile_rule(self):
        runs = [run('rss', NOW - 100, failed={'rss': 1}), run('rss', NOW - 1000)]
        self.assertEqual(profile_report('rss', runs, NOW)['unhealthy_runs_24h'], 1)

    def test_full_report_on_small_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'database': tmp + '/db.sqlite', 'data_dir': tmp + '/data', 'artifact_dir': tmp + '/art'}
            con = connect(cfg['database'])
            insert(con, 'markets', dict(market_id='1', condition_id='c', event_group='e', yes_token='y', no_token='n',
                                         yes_index=0, source_kind='real'))
            version = dict(market_id='1', available_at=NOW - 5000, observed_at=NOW - 5000, question='q', rules='r',
                           scheduled_end=NOW + 86400 * 3, category='x', is_open=1, liquidity=1., volume=1.,
                           provenance='prospective', raw_ref='r', version_id='v1')
            insert(con, 'market_versions', version)
            con.close()
            Path(cfg['artifact_dir']).mkdir()
            report = build_report(cfg, NOW)
            self.assertEqual(report['data']['open_markets'], 1)
            self.assertEqual(report['data']['open_without_fresh_quote'], 1)
            self.assertEqual(report['data']['largest_deadline_day_share'], 1.0)
            self.assertTrue(any('quotes is NEVER' in p for p in report['problems']))
            self.assertIn('ATTENTION', render(report))

    def test_missing_database_is_a_problem_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_report({'database': tmp + '/none.sqlite', 'data_dir': tmp, 'artifact_dir': tmp}, NOW)
            self.assertTrue(any('database not found' in p for p in report['problems']))

    def test_scheduler_script_matches_interval_constants(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts' / 'schedule_collector.ps1').read_text(encoding='utf-8')
        found = {m.group(1): int(m.group(2)) for m in re.finditer(r"Name = '(\w+)';.*?Minutes = (\d+)", script)}
        self.assertEqual(found, INTERVAL_MINUTES)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = {'database': str(t / 'db.sqlite'), 'data_dir': str(t / 'data'), 'artifact_dir': str(t / 'art')}
        con = connect(self.cfg['database'])
        insert(con, 'markets', dict(market_id='1', condition_id='c', event_group='e', yes_token='y', no_token='n',
                                     yes_index=0, source_kind='real'))
        con.close()
        for day in ('2027-01-01', '2027-01-02'):
            (t / 'data' / 'raw' / day).mkdir(parents=True)
            (t / 'data' / 'raw' / day / 'quotes.jsonl.gz').write_bytes(b'x' * 10)
        self.dest = t / 'backup'
        self.now = 1_798_848_000 + 3600  # 2027-01-02 01:00 UTC, so 2027-01-02 is still "today"

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_is_restorable_and_copies_only_closed_raw_days(self):
        result = backup(self.cfg, self.dest, now=self.now)
        restored = Path(self.tmp.name) / 'restored.sqlite'
        restored.write_bytes(gzip.decompress(Path(result['backup']).read_bytes()))
        con = sqlite3.connect(restored)
        try:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM markets').fetchone()[0], 1)
        finally:
            con.close()
        self.assertTrue((self.dest / 'raw' / '2027-01-01' / 'quotes.jsonl.gz').exists())
        self.assertFalse((self.dest / 'raw' / '2027-01-02').exists())
        self.assertEqual(result['raw_files_copied'], 1)

    def test_second_backup_skips_copied_raw_and_prunes_old(self):
        backup(self.cfg, self.dest, keep=1, now=self.now)
        second = backup(self.cfg, self.dest, keep=1, now=self.now + 3600)
        self.assertEqual(second['raw_files_copied'], 0)
        self.assertEqual(second['pruned'], 1)
        self.assertEqual(len(list(self.dest.glob('research-*.sqlite.gz'))), 1)

    def test_missing_database_refused(self):
        with self.assertRaises(FileNotFoundError):
            backup({**self.cfg, 'database': self.tmp.name + '/none.sqlite'}, self.dest)


if __name__ == '__main__':
    unittest.main()
