"""B9 replay scoring (R5, R6, R9, R16) on synthetic frames."""
import unittest

import numpy as np
import pandas as pd

from retrospective import score_replay as sr


def frame(n_clusters=30, seed=1, better=None, horizon=24):
    rng = np.random.default_rng(seed)
    rows = []
    for c in range(n_clusters):
        for m in range(2):
            y = int(rng.random() < .5)
            base = .3 + .4 * y
            r = dict(snapshot_id=f'{horizon}-{c}-{m}', market_id=f'{c}-{m}', event_group=f'g{c}', outcome=y,
                     horizon=horizon, market=base, half=.5, uniform=.5)
            for s in sr.AGENTS + sr.COMPONENTS:
                r[s] = float(np.clip(base + rng.normal(0, .15), .01, .99))
            if better:
                r[better] = .9 if y else .1           # a clearly better system
            rows.append(r)
    f = pd.DataFrame(rows)
    f['fixed_50_50'] = 1 / (1 + np.exp(-(.5 * sr.logit(f.market) + .5 * sr.logit(f.multi_verified))))
    return f


class WeightingTests(unittest.TestCase):
    def test_equal_weight_per_cluster_not_per_market(self):
        f = pd.DataFrame(dict(event_group=['a', 'a', 'a', 'b'], market_id=['1', '2', '3', '4'],
                              outcome=[1, 1, 1, 1], p=[1.0, 1.0, 1.0, 0.0]))
        # per market mean: 0,0,0 in cluster a -> 0; cluster b -> 1; equal weight -> 0.5 (not 0.25)
        self.assertAlmostEqual(sr.weighted(f, 'p', sr.brier_rows), 0.5)


class SelectionTests(unittest.TestCase):
    def test_clear_winner_wins(self):
        f = frame(better='multi_verified')
        t = sr.select_t3(f, sr.table(f, sr.CANDIDATES))
        self.assertEqual((t['provisional'], t['winner']), ('multi_verified', 'multi_verified'))

    def test_tie_goes_to_fewest_calls(self):
        f = frame()
        for s in sr.CANDIDATES:                        # identical forecasts: all tie
            f[s] = f['single_agent_kx']
        t = sr.select_t3(f, sr.table(f, sr.CANDIDATES))
        self.assertEqual(t['winner'], 'single_agent_rag')
        self.assertEqual(sr.CALLS[t['winner']], 1)


class ContaminationTests(unittest.TestCase):
    def test_warning_rule(self):
        self.assertTrue(sr.contamination({'llm_no_retrieval': {'ci95': [-0.02, 0.01]}})['warning'])   # includes 0
        self.assertTrue(sr.contamination({'llm_no_retrieval': {'ci95': [-0.05, -0.01]}})['warning'])  # better
        self.assertFalse(sr.contamination({'llm_no_retrieval': {'ci95': [0.01, 0.05]}})['warning'])   # worse


class ScoreTests(unittest.TestCase):
    def test_report_shape_and_cohorts(self):
        f = pd.concat([frame(horizon=24), frame(n_clusters=12, seed=2, horizon=168)])
        f.loc[f.index[:3], 'single_agent_rag'] = np.nan          # 3 rows without an agent forecast
        r = sr.score(f)
        self.assertEqual(r['24h']['snapshots_total'], 60)
        self.assertEqual(r['24h']['snapshots_A'], 57)
        self.assertEqual(r['24h']['cohort_B']['market']['snapshots'], 60)
        self.assertIn('fixed_50_50', r['24h']['cohort_A'])
        self.assertNotIn('ci95', r['24h']['cohort_A']['market'])
        self.assertEqual(len(r['24h']['cohort_A']['multi_verified']['ci95']), 2)
        self.assertIn('winner', r['T3'])
        self.assertIn('\\toprule', sr.latex(r))


if __name__ == '__main__':
    unittest.main()
