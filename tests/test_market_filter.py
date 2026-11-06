"""--market-id: build and forecast only the named markets; failures are loud, never silent skips."""
import subprocess
import sys
import tempfile
import unittest

from scripts.run_demo import demo_config, populate
from src.agents.orchestrator import SYSTEMS, run_agents
from src.common import ROOT
from src.dataset.snapshots import build, frame
from src.db.loaders import connect

TARGET, OTHER = 'SYNTHETIC_0001_0', 'SYNTHETIC_0002_1'


class MarketFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = demo_config(self.tmp.name)
        self.con = connect(self.cfg['database'])
        populate(self.con, self.cfg, groups=3)  # 6 markets, 2 horizons each = 12 snapshots

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def forecast_markets(self):
        return {r[0] for r in self.con.execute(
            'SELECT DISTINCT s.market_id FROM forecasts f JOIN snapshots s USING(snapshot_id)')}

    # --- build ---
    def test_build_only_the_requested_markets(self):
        self.assertEqual(build(self.con, self.cfg, market_ids=[TARGET])['eligible'], 2)
        self.assertEqual(set(frame(self.con).market_id), {TARGET})
        self.assertEqual(build(self.con, self.cfg, market_ids=[TARGET, OTHER])['eligible'], 4)
        self.assertEqual(set(frame(self.con).market_id), {TARGET, OTHER})

    def test_filtered_build_agrees_with_the_full_build(self):
        build(self.con, self.cfg, market_ids=[TARGET])
        first = set(frame(self.con).snapshot_id)
        build(self.con, self.cfg)  # identical snapshot ids, so nothing conflicts and nothing is duplicated
        everything = frame(self.con)
        self.assertEqual(len(everything), 12)
        self.assertTrue(first <= set(everything.snapshot_id))

    def test_unknown_market_is_an_error_and_builds_nothing(self):
        with self.assertRaisesRegex(ValueError, 'Unknown market_id.*NOPE'):
            build(self.con, self.cfg, market_ids=[TARGET, 'NOPE'])
        self.assertEqual(len(frame(self.con)), 0)

    def test_empty_market_list_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            build(self.con, self.cfg, market_ids=[])

    # --- forecast ---
    def test_forecast_only_the_requested_market(self):
        build(self.con, self.cfg)
        result = run_agents(self.con, frame(self.con), self.cfg, market_ids=[TARGET])
        self.assertEqual(result['failures'], [])
        self.assertEqual(result['market_ids'], [TARGET])
        self.assertEqual(self.forecast_markets(), {TARGET})
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], 2 * len(SYSTEMS))

    def test_requested_market_without_a_snapshot_is_reported_and_the_others_still_run(self):
        build(self.con, self.cfg)
        result = run_agents(self.con, frame(self.con), self.cfg, market_ids=[TARGET, 'NOPE'])
        self.assertEqual(self.forecast_markets(), {TARGET})
        self.assertEqual([f['market_id'] for f in result['failures']], ['NOPE'])
        self.assertIn('NoSnapshot', result['failures'][0]['error'])

    def test_all_requested_markets_missing_stops_the_run(self):
        build(self.con, self.cfg)
        with self.assertRaisesRegex(ValueError, 'No snapshots for the requested market ids.*NOPE'):
            run_agents(self.con, frame(self.con), self.cfg, market_ids=['NOPE'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], 0)

    def test_limit_is_applied_after_the_market_filter(self):
        build(self.con, self.cfg)
        data = frame(self.con)
        run_agents(self.con, data, self.cfg, limit=1, market_ids=[TARGET])
        done = {r[0] for r in self.con.execute('SELECT DISTINCT snapshot_id FROM forecasts')}
        newest = data[data.market_id == TARGET].sort_values('cutoff', ascending=False).snapshot_id.iloc[0]
        self.assertEqual(done, {newest})

    def test_without_a_filter_everything_is_forecast_as_before(self):
        build(self.con, self.cfg)
        result = run_agents(self.con, frame(self.con), self.cfg)
        self.assertIsNone(result['market_ids'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], 12 * len(SYSTEMS))

    def test_empty_market_list_is_an_error_for_forecasting_too(self):
        build(self.con, self.cfg)
        with self.assertRaisesRegex(ValueError, 'empty'):
            run_agents(self.con, frame(self.con), self.cfg, market_ids=[])


class CommandLineTests(unittest.TestCase):
    def test_both_commands_offer_the_option(self):
        for script in ('scripts.build_dataset', 'scripts.run_agents'):
            with self.subTest(script=script):
                out = subprocess.run([sys.executable, '-m', script, '--help'], cwd=ROOT, capture_output=True,
                                     text=True, timeout=120)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn('--market-id', out.stdout)


if __name__ == '__main__':
    unittest.main()
