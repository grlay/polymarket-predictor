"""The pilot config may differ from the research config in exactly three keys, and its filters only in the price band."""
import unittest
from pathlib import Path

import yaml

from src.common import ROOT, forecast_identity, load_config


class PilotConfigTests(unittest.TestCase):
    def setUp(self):
        self.research = load_config('configs/config.yaml')
        self.pilot = load_config('configs/config_pilot.yaml')

    def test_only_the_three_intended_keys_differ(self):
        def flat(cfg, prefix=''):
            out = {}
            for key, value in cfg.items():
                if isinstance(value, dict):
                    out.update(flat(value, prefix + key + '.'))
                else:
                    out[prefix + key] = value
            return out
        a, b = flat(self.research), flat(self.pilot)
        differing = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        self.assertEqual(differing, {'config_path', 'market_filters', 'llm.concurrency', 'llm.max_calls_per_run'})

    def test_forecast_identity_is_unchanged_so_pilot_and_research_share_a_run(self):
        self.assertEqual(forecast_identity(self.research), forecast_identity(self.pilot))

    def test_pilot_still_marks_snapshots_as_pilot_through_the_mode(self):
        self.assertEqual(self.pilot['mode'], 'prospective')  # '_pilot' is appended by build() whenever --at is used
        self.assertEqual(self.pilot['dataset'], self.research['dataset'])

    def test_pilot_filters_differ_only_in_the_price_band(self):
        research = yaml.safe_load(Path(self.research['market_filters']).read_text(encoding='utf-8'))
        pilot = yaml.safe_load(Path(self.pilot['market_filters']).read_text(encoding='utf-8'))
        self.assertTrue(Path(self.pilot['market_filters']).samefile(ROOT / 'configs' / 'market_filters_pilot.yaml'))
        differing = {k for k in set(research) | set(pilot) if research.get(k) != pilot.get(k)}
        self.assertEqual(differing, {'min_price', 'max_price'})
        self.assertLess(pilot['min_price'], research['min_price'])
        self.assertGreater(pilot['max_price'], research['max_price'])


if __name__ == '__main__':
    unittest.main()
