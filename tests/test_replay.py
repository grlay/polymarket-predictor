"""Replay database (B3), snapshots at the two past cutoffs (B7) and the --systems runner (decisions log R10)."""
import csv
import gzip
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from retrospective.build_retro_db import CUTOFFS, build as build_records, rules_flags, ts
from scripts.run_demo import demo_config, populate
from src.agents.orchestrator import run_agents
from src.common import load_config, timestamp
from src.dataset.snapshots import build, frame
from src.db.loaders import connect, insert

C168, C24 = ts(CUTOFFS['168h']), ts(CUTOFFS['24h'])
GA_SEN = 'Will the Democrats win the Georgia Senate race in 2026?'
RULES = ('This market will resolve according to the winner of the 2026 Georgia Senate election currently scheduled '
         'for November 3, 2026. If the nominee withdraws, the market will be updated to the replacement nominee.')


def gamma_market(mid, question, outcomes=('Yes', 'No'), created='2025-07-01T12:00:00.123456Z', rules=RULES):
    return dict(id=mid, question=question, conditionId=f'0xcond{mid}', outcomes=json.dumps(list(outcomes)),
                clobTokenIds=json.dumps([f'tok{mid}_{o}' for o in outcomes]), createdAt=created, description=rules)


class FakeRaw:
    """A raw folder shaped like the VM's: gzip Gamma pages + per-market price windows."""

    def __init__(self, root, markets, histories):
        self.raw, self.out, self.selection = root / 'raw', root / 'out', root / 'selection.csv'
        (self.raw / 'gamma').mkdir(parents=True)
        (self.raw / 'prices').mkdir()
        event = dict(id='e1', title='Georgia Senate Election Winner', endDate='2026-11-04T04:59:00Z', markets=markets)
        (self.raw / 'gamma' / 'events_0.json.gz').write_bytes(gzip.compress(json.dumps({'events': [event]}).encode()))
        for (mid, label), points in histories.items():
            body = json.dumps({'history': [{'t': t, 'p': p} for t, p in points]}).encode()
            (self.raw / 'prices' / f'{mid}_{label}.json.gz').write_bytes(gzip.compress(body))
        with open(self.selection, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['market_id', 'question', 'cluster', 'eligible_168h', 'eligible_24h'])
            w.writeheader()
            for m in markets:
                w.writerow(dict(market_id=m['id'], question=m['question'], cluster='event_e1',
                                eligible_168h='yes', eligible_24h='yes'))

    def build(self):
        return build_records(self.raw, self.out, self.selection)

    def records(self, name):
        return [json.loads(line) for line in (self.out / f'{name}.jsonl').read_text(encoding='utf-8').splitlines()]


class RulesFlagTests(unittest.TestCase):
    def test_boilerplate_is_not_flagged(self):
        self.assertEqual(rules_flags(RULES), [])
        self.assertEqual(rules_flags('If the results are not confirmed by July 31, 2027, this resolves to Other.'), [])

    def test_added_material_is_flagged(self):
        self.assertEqual(rules_flags(RULES + '\n\nUpdate: the candidate withdrew.'), ['added_heading'])
        self.assertEqual(rules_flags(RULES + ' Clarification (Oct 30): recounts count.'), ['added_heading'])
        self.assertEqual(rules_flags('Seats scheduled as of October 31, 2026 are included.'), ['date_2026-10-31'])
        self.assertEqual(rules_flags('Recount ordered on 2026-11-05.'), ['date_2026-11-05'])


class BuildRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.histories = {('101', '168h'): [(C168 - 600, .41), (C168 - 30, .43), (C168 + 5, .99)],
                          ('101', '24h'): [(C24 - 300, .47)],
                          ('102', '168h'): [(C168 - 600, .58)], ('102', '24h'): [(C24 - 300, .52)]}
        self.fake = FakeRaw(Path(self.tmp.name), [gamma_market('101', GA_SEN),
                                                  gamma_market('102', GA_SEN.replace('Democrats', 'Republicans'),
                                                               outcomes=('No', 'Yes'))], self.histories)

    def tearDown(self):
        self.tmp.cleanup()

    def test_yes_token_follows_the_outcome_labels_not_the_position(self):
        self.fake.build()
        rows = {r['market_id']: r for r in self.fake.records('markets')}
        self.assertEqual((rows['101']['yes_token'], rows['101']['yes_index']), ('tok101_Yes', 0))
        self.assertEqual((rows['102']['yes_token'], rows['102']['no_token'], rows['102']['yes_index']),
                         ('tok102_Yes', 'tok102_No', 1))
        self.assertEqual(rows['101']['event_group'], 'event_e1')

    def test_versions_use_creation_time_and_the_event_deadline(self):
        self.fake.build()
        v = self.fake.records('market_versions')[0]
        self.assertEqual(v['available_at'], timestamp('2025-07-01T12:00:00Z'))
        self.assertEqual(v['scheduled_end'], timestamp('2026-11-04T04:59:00Z'))
        self.assertEqual((v['provenance'], v['liquidity'], v['volume']), ('archive_verified', None, None))
        self.assertTrue(v['raw_ref'].endswith('events_0.json.gz'))

    def test_prices_are_available_60s_after_their_point_and_never_after_the_cutoff(self):
        summary, _ = self.fake.build()
        p101 = [p for p in self.fake.records('prices') if p['market_id'] == '101']
        self.assertEqual([p['timestamp'] for p in p101], [C168 - 600, C168 - 30, C24 - 300])  # C168+5 dropped
        self.assertTrue(all(p['available_at'] == p['timestamp'] + 60 for p in p101))
        self.assertEqual({p['price_kind'] for p in p101}, {'history_unspecified'})
        self.assertEqual(summary['prices'], 5)

    def test_question_must_match_the_committed_selection(self):
        rows = list(csv.DictReader(open(self.fake.selection, encoding='utf-8')))
        rows[0]['question'] = 'Something else?'
        with open(self.fake.selection, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        with self.assertRaisesRegex(ValueError, 'question differs'):
            self.fake.build()


class ReplaySnapshotTests(unittest.TestCase):
    """B7: the unchanged builder, run on the replay config, gives research snapshots at exactly 168 h and 24 h."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        histories = {('101', '168h'): [(C168 - 600, .41), (C168 - 30, .43)],   # .43 is available only at C168+30
                     ('101', '24h'): [(C24 - 300, .47)],
                     ('102', '168h'): [(C168 - 3000, .58)],                     # 50 min old: stale
                     ('102', '24h'): [(C24 - 300, .99)]}                        # outside the 0.02-0.98 band
        self.fake = FakeRaw(root, [gamma_market('101', GA_SEN),
                                   gamma_market('102', GA_SEN.replace('Democrats', 'Republicans'))], histories)
        self.fake.build()
        self.cfg = deepcopy(load_config('configs/config_retro.yaml'))
        self.cfg['database'] = str(root / 'retro.sqlite')
        self.con = connect(self.cfg['database'])
        for name in ('markets', 'market_versions', 'prices'):
            for r in self.fake.records(name):
                insert(self.con, name, r)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_snapshots_at_both_cutoffs_with_reasons_for_exclusions(self):
        result = build(self.con, self.cfg)
        snaps = {(s['market_id'], s['cutoff']): s for s in
                 (dict(r) for r in self.con.execute('SELECT * FROM snapshots'))}
        self.assertEqual(set(snaps), {('101', C168), ('101', C24)})
        self.assertEqual(snaps[('101', C168)]['market_price'], .41)   # the later point was not yet available
        self.assertEqual({s['mode'] for s in snaps.values()}, {'retrospective'})
        self.assertEqual({round(s['horizon_hours']) for s in snaps.values()}, {168, 24})
        reasons = {(e['market_id'], e['cutoff']): e['reason'] for e in result['excluded']}
        self.assertEqual(reasons, {('102', C168): 'stale_price', ('102', C24): 'price_range'})
        self.assertFalse(frame(self.con).empty)   # research snapshots, not pilot ones

    def test_replay_config_pins_the_model_and_the_frozen_date(self):
        llm = self.cfg['llm']
        self.assertEqual(llm['model'], 'gemini-3.5-flash-lite')
        self.assertTrue(llm['allow_retrospective'])
        self.assertLess(timestamp(llm['model_frozen_before']), C168)
        self.assertNotEqual(self.cfg['database'], load_config('configs/config.yaml')['database'])


class SystemsOptionTests(unittest.TestCase):
    def run_demo(self, question=None, systems=None, retrieve_docs=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = demo_config(tmp.name)
        con = connect(cfg['database'])
        self.addCleanup(con.close)
        populate(con, cfg, groups=1)
        build(con, cfg)
        data = frame(con).head(1).copy()
        if question:
            data['question'] = question
        if retrieve_docs is not None:
            with patch('src.agents.orchestrator.retrieve', return_value=retrieve_docs) as retrieve:
                result = run_agents(con, data, cfg, systems=systems)
            return con, result, retrieve, cfg, data
        return con, run_agents(con, data, cfg, systems=systems), None, cfg, data

    def systems_stored(self, con):
        return sorted(r[0] for r in con.execute("SELECT system FROM forecasts WHERE status='ok'"))

    def test_run1_no_retrieval_alone_needs_no_documents(self):
        con, result, retrieve, *_ = self.run_demo(question=GA_SEN, systems=['llm_no_retrieval'], retrieve_docs=[])
        retrieve.assert_not_called()
        self.assertEqual(result['failures'], [])
        self.assertEqual(self.systems_stored(con), ['llm_no_retrieval'])

    def test_all_systems_still_refuse_a_gated_market_without_documents(self):
        con, result, *_ = self.run_demo(question=GA_SEN, retrieve_docs=[])
        self.assertRegex(result['failures'][0]['error'], 'insufficient_relevant_evidence.*election_senate_georgia')
        self.assertEqual(self.systems_stored(con), [])

    def test_run2_evidence_systems_skip_multi_no_social(self):
        run2 = ['news_agent', 'base_rate_agent', 'single_agent_rag', 'single_agent_kx', 'multi_unverified',
                'multi_verified']
        con, result, *_ = self.run_demo(systems=run2)
        self.assertEqual(result['failures'], [])
        self.assertEqual(self.systems_stored(con), sorted(run2))

    def test_a_finished_subset_is_not_forecast_again(self):
        con, first, _, cfg, data = self.run_demo(systems=['llm_no_retrieval'])
        second = run_agents(con, data, cfg, systems=['llm_no_retrieval'])
        self.assertEqual(second['run_id'], first['run_id'])
        self.assertEqual(second['calls'], 0)
        self.assertEqual(self.systems_stored(con), ['llm_no_retrieval'])

    def test_unknown_system_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'Unknown or empty systems: multi_magic'):
            self.run_demo(systems=['multi_magic'])


if __name__ == '__main__':
    unittest.main()
