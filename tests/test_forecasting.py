import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import pandas as pd

from scripts.run_demo import demo_config, populate
from src.agents import orchestrator
from src.agents.llm_client import BudgetExhausted, LLMClient
from src.agents.orchestrator import context_for, plan, run_agents
from src.baselines import single_agent_kx
from src.common import ANALYSIS_CODE, FORECAST_CODE
from src.dataset.snapshots import build, frame
from src.db.loaders import connect
from src.models.training import joined_forecasts

GOOD = json.dumps({'probability': .6, 'evidence_ids': [], 'rationale': 'r', 'flags': []})


def reply(content=GOOD, finish='stop', usage=None, model='fixture-model'):
    body = {'model': model, 'choices': [{'message': {'content': content}, 'finish_reason': finish}],
            'usage': usage or {'prompt_tokens': 10, 'completion_tokens': 5}}
    return io.BytesIO(json.dumps(body).encode())


def http_error(code, body=b'{"error": "x"}'):
    return urllib.error.HTTPError('https://f.test/v1/chat/completions', code, 'err', {'Retry-After': '1'}, io.BytesIO(body))


class ContextTests(unittest.TestCase):
    def test_dates_are_readable_and_deadline_is_given(self):
        snapshot = dict(question='q', rules='r', cutoff=1_800_000_000, horizon_hours=24.0)
        doc = dict(document_id='d', title='t', body='b', source='s', source_type='news', content_kind='x',
                   published_at=1_799_990_000, available_at=1_799_999_000)
        context = context_for(snapshot, [doc])
        self.assertEqual(context['cutoff_utc'], '2027-01-15T08:00:00Z')
        self.assertEqual(context['deadline_utc'], '2027-01-16T08:00:00Z')
        self.assertEqual(context['hours_to_deadline'], 24.0)
        self.assertEqual(context['documents'][0]['published_utc'], '2027-01-15T05:13:20Z')
        self.assertNotIn('cutoff', context)  # no raw epoch integers for the model to misread
        self.assertNotIn('published_at', context['documents'][0])

    def test_no_deadline_when_horizon_unknown(self):
        self.assertNotIn('deadline_utc', context_for(dict(question='q', rules='r', cutoff=1), []))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'llm': {'max_forecast_delay_seconds': 900, 'allow_retrospective': False}}
        self.now = 10_000
        self.rows = [dict(snapshot_id=s, cutoff=c, source_kind='real') for s, c in
                     [('old', 1_000), ('a', 9_800), ('b', 9_900), ('done', 9_950), ('c', 9_100)]]

    def test_stale_skipped_not_failed_and_newest_first(self):
        ready, skipped = plan(self.rows, {'done'}, self.cfg, self.now)
        self.assertEqual([r['snapshot_id'] for r in ready], ['b', 'a', 'c'])
        self.assertEqual(skipped, 1)  # only 'old' is too old; 'c' is 900 s old: still inside the window

    def test_limit_takes_newest_eligible_not_oldest(self):
        ready, _ = plan(self.rows, set(), self.cfg, self.now, limit=2)
        self.assertEqual([r['snapshot_id'] for r in ready], ['done', 'b'])

    def test_synthetic_and_retrospective_are_never_stale(self):
        synthetic = [dict(snapshot_id='s', cutoff=1, source_kind='synthetic')]
        self.assertEqual(plan(synthetic, set(), self.cfg, self.now)[1], 0)
        self.cfg['llm']['allow_retrospective'] = True
        self.assertEqual(plan(self.rows, set(), self.cfg, self.now)[1], 0)


class ClientTests(unittest.TestCase):
    def client(self, tmp, **llm):
        cfg = demo_config(tmp)
        cfg['mode'] = 'prospective'
        cfg['llm'].update({'provider': 'chat_completions', 'model': 'fixture-model', 'max_calls_per_run': 20, **llm})
        with patch.dict('os.environ', {'LLM_API_KEY': 'TEST_NOT_A_REAL_KEY', 'LLM_API_BASE': 'https://fixture.test/v1'}):
            return LLMClient(cfg)

    def ask(self, client, **kwargs):
        return client.predict('prompt', {'question': 'q', 'documents': []}, 'role', **kwargs)

    def test_transient_http_errors_are_retried_with_backoff(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep') as sleeper, \
                patch('urllib.request.urlopen', side_effect=[http_error(429), http_error(503), reply()]):
            client = self.client(tmp)
            self.assertEqual(self.ask(client)['probability'], .6)
            self.assertEqual(client.calls, 3)  # every attempt spends budget
            self.assertEqual(sleeper.call_count, 2)

    def test_client_error_is_not_retried_and_shows_provider_message(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[http_error(400, b'{"error": "unsupported parameter"}')]):
            client = self.client(tmp)
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400.*unsupported parameter'):
                self.ask(client)
            self.assertEqual(client.calls, 1)

    def test_malformed_then_valid_answer_is_retried_and_only_valid_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[reply('not json'), reply('{"probability": 2}'), reply()]):
            client = self.client(tmp)
            self.assertEqual(self.ask(client)['probability'], .6)
            self.assertEqual(client.calls, 3)
            self.assertEqual(len(list((client.cache_dir).glob('*.json'))), 1)

    def test_truncated_output_is_reported_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[reply('{"probab', finish='length') for _ in range(3)]):
            with self.assertRaisesRegex(ValueError, 'truncated.*max_output_tokens'):
                self.ask(self.client(tmp))

    def test_tokens_and_models_are_accounted_including_retries(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[reply('bad', usage={'prompt_tokens': 7, 'completion_tokens': 3}),
                                                              reply(usage={'prompt_tokens': 10, 'completion_tokens': 5}, model='m-2')]):
            client = self.client(tmp)
            self.ask(client)
            self.assertEqual((client.prompt_tokens, client.completion_tokens), (17, 8))
            self.assertEqual(client.response_models, {'fixture-model', 'm-2'})

    def test_request_carries_temperature_override_and_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', side_effect=[reply(), reply()]) as opener:
            client = self.client(tmp, temperature=0, reasoning_effort='low')
            self.ask(client)
            self.ask(client, replicate=1, temperature=.7)
            first, second = (json.loads(c.args[0].data) for c in opener.call_args_list)
            self.assertEqual((first['temperature'], first['reasoning_effort']), (0, 'low'))
            self.assertEqual(second['temperature'], .7)

    def test_temperature_omitted_when_configured_null(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen', side_effect=[reply()]) as opener:
            self.ask(self.client(tmp, temperature=None))
            self.assertNotIn('temperature', json.loads(opener.call_args.args[0].data))

    def test_budget_counts_attempts_and_raises_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('src.agents.llm_client.time.sleep'), \
                patch('urllib.request.urlopen', side_effect=[http_error(503) for _ in range(5)]):
            client = self.client(tmp, max_calls_per_run=2, retries=5)
            with self.assertRaises(BudgetExhausted):
                self.ask(client)
            self.assertEqual(client.calls, 2)


class RepeatedCallTests(unittest.TestCase):
    class Stub:
        def __init__(self):
            self.seen = []

        def predict(self, prompt, context, role, replicate=0, temperature=None):
            self.seen.append((replicate, temperature))
            return dict(probability=.5, evidence_ids=[], rationale='r', flags=[])

    def test_members_are_sampled_and_distinct_replicates(self):
        cfg = {'llm': {'prompt_dir': demo_config.__globals__['ROOT'] / 'configs' / 'prompts', 'k': 3, 'k_temperature': .7}}
        stub = self.Stub()
        single_agent_kx.forecast(stub, {'documents': []}, cfg)
        self.assertEqual(stub.seen, [(1, .7), (2, .7), (3, .7)])

    def test_null_k_temperature_falls_back_to_configured_temperature(self):
        cfg = {'llm': {'prompt_dir': demo_config.__globals__['ROOT'] / 'configs' / 'prompts', 'k': 2, 'k_temperature': None}}
        stub = self.Stub()
        single_agent_kx.forecast(stub, {'documents': []}, cfg)
        self.assertEqual([t for _, t in stub.seen], [None, None])


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = demo_config(self.tmp.name)
        self.con = connect(self.cfg['database'])
        populate(self.con, self.cfg, groups=3)
        build(self.con, self.cfg)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_run_identity_ignores_paths_and_split_dates_but_not_forecast_settings(self):
        first = run_agents(self.con, frame(self.con), self.cfg)['run_id']
        self.cfg['training'].update(train_end='2025-02-01T00:00:00Z', validation_end='2025-03-01T00:00:00Z')
        self.cfg['config_path'] = '/somewhere/else/config.yaml'
        self.cfg['llm']['max_calls_per_run'] = 777
        self.assertEqual(run_agents(self.con, frame(self.con), self.cfg)['run_id'], first)
        self.cfg['retrieval']['top_k'] = 3
        self.assertNotEqual(run_agents(self.con, frame(self.con), self.cfg)['run_id'], first)

    def test_code_scopes_exclude_collector_and_database_code(self):
        for scope in (FORECAST_CODE, ANALYSIS_CODE):
            for path in scope:
                self.assertFalse(path.startswith(('src/ingestion', 'src/db', 'scripts', 'src/dashboard')), path)

    def test_budget_exhaustion_is_one_clear_failure_and_resumable(self):
        self.cfg['llm']['max_calls_per_run'] = 12
        result = run_agents(self.con, frame(self.con), self.cfg)
        budget = [f for f in result['failures'] if 'BudgetExhausted' in f['error']]
        self.assertEqual(len(budget), 1)
        self.assertEqual(len(result['failures']), 1)
        self.cfg['llm']['max_calls_per_run'] = 10_000
        resumed = run_agents(self.con, frame(self.con), self.cfg)
        self.assertEqual(resumed['failures'], [])
        total = self.con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]
        self.assertEqual(total, len(frame(self.con)) * len(orchestrator.SYSTEMS))

    def test_programming_errors_stop_the_run_instead_of_being_logged_as_failures(self):
        with patch('src.agents.orchestrator.news_agent.forecast', side_effect=TypeError('bug')):
            with self.assertRaises(TypeError):
                run_agents(self.con, frame(self.con), self.cfg)

    def test_limit_forecasts_the_newest_snapshots(self):
        data = frame(self.con)
        result = run_agents(self.con, data, self.cfg, limit=2)
        done = {r[0] for r in self.con.execute('SELECT DISTINCT snapshot_id FROM forecasts')}
        newest = set(data.sort_values('cutoff', ascending=False).snapshot_id.head(2))
        self.assertEqual(done, newest)
        self.assertEqual(result['skipped_stale'], 0)

    def test_concurrent_run_gives_the_same_forecasts(self):
        data = frame(self.con)
        self.cfg['llm']['concurrency'] = 3
        result = run_agents(self.con, data, self.cfg)
        self.assertEqual(result['failures'], [])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], len(data) * len(orchestrator.SYSTEMS))

    def test_pilot_snapshots_never_enter_the_training_frame(self):
        research = frame(self.con)
        run_id = run_agents(self.con, research, self.cfg)['run_id']
        build(self.con, self.cfg, at=int(research.cutoff.iloc[0]))
        pilots = frame(self.con, pilot=True)
        self.assertGreater(len(pilots), 0)
        self.assertTrue(set(pilots.snapshot_id).isdisjoint(frame(self.con).snapshot_id))
        self.assertEqual(len(frame(self.con, pilot=None)), len(research) + len(pilots))
        joined = joined_forecasts(self.con, run_id)
        self.assertEqual(set(joined.snapshot_id), set(research.snapshot_id))


if __name__ == '__main__':
    unittest.main()
