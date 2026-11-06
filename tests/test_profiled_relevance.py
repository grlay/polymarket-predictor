"""Hand-written evidence profiles (shared-spec Layer 3, stage 1): matching, gate, as-of retrieval, forecast skip."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_demo import demo_config, populate
from src.agents.orchestrator import run_agents
from src.common import forecast_identity, profiles_digest
from src.dataset.snapshots import build, frame
from src.db.loaders import add_document, connect
from src.retrieval.relevance import (compile_profile, load_profiles, rejection_reason, relevance_profile,
                                     require_relevant_evidence, term_pattern)
from src.retrieval.temporal_retriever import retrieve

RUBIO = 'Marco Rubio out as Secretary of State by November 15, 2026?'
BLOCKADE = 'US announces end of Iranian blockade by November 15, 2026?'
CUTOFF = 1_800_000_000
DAY = 86_400


def snapshot(question, cutoff=CUTOFF):
    return dict(question=question, rules='Resolves on an official announcement.', cutoff=cutoff, source_kind='real')


def doc(title, body='', published=CUTOFF - DAY):
    return dict(document_id=title, content_hash=title, title=title, body=body, source='example.test',
                source_type='news', content_kind='article', published_at=published, available_at=published,
                observed_at=published, provenance='prospective', source_kind='real')


class ProfileFileTests(unittest.TestCase):
    def test_shipped_profiles_match_every_variant_of_the_two_pilot_storylines(self):
        for question in (RUBIO, 'Will Marco Rubio leave the Trump administration before 2027?',
                         'Marco Rubio out as Secretary of State by November 30, 2026?'):
            self.assertEqual(relevance_profile({'question': question}), 'rubio_departure', question)
        for question in (BLOCKADE, 'US announces end of Iranian blockade by December 31, 2026?',
                         'Iran blockade lifted before Iran-Oman Hormuz agreement?'):
            self.assertEqual(relevance_profile({'question': question}), 'iran_blockade_end', question)

    def test_other_markets_stay_unprofiled(self):
        for question in ('Will the Fed cut interest rates?', 'Will Marco Rubio be the 2028 Republican nominee?',
                         'US-Iran Hormuz Agreement by November 30?', 'Will the USS Theodore Roosevelt arrive in CENTCOM by November 15, 2026?'):
            with self.subTest(question=question):
                self.assertIsNone(relevance_profile({'question': question}))
                self.assertIsNone(rejection_reason(snapshot(question), doc('Rubio to leave cabinet')))

    def test_house_gate_keeps_priority(self):
        question = 'Will the Democratic Party control the House after the 2026 Midterm elections?'
        self.assertEqual(relevance_profile({'question': question}), 'us_house_control')

    def test_profile_file_is_part_of_the_forecast_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = forecast_identity(demo_config(tmp))
        self.assertIsNotNone(profiles_digest())
        self.assertEqual(identity['relevance_profiles'], profiles_digest())

    def test_malformed_profiles_are_errors_not_silently_ignored(self):
        good = dict(id='p', subtype='s', question_regex='x', window_days=30, entities=['a'], institution_terms=[],
                    action_terms=['b'])
        compile_profile(good)
        for change, message in ((dict(entities=[]), 'at least one entity'),
                                (dict(institution_terms=[], action_terms=[]), 'institution or action'),
                                (dict(window_days=0), 'positive window_days'),
                                (dict(id='us_house_control'), 'reserved'),
                                (dict(entities=['*']), 'Empty relevance term')):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, message):
                    compile_profile({**good, **change})
        with self.assertRaisesRegex(ValueError, 'missing: window_days'):
            compile_profile({k: v for k, v in good.items() if k != 'window_days'})

    def test_duplicate_ids_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'p.yaml'
            entry = ('  - {id: p, subtype: s, question_regex: x, window_days: 30, entities: [a], '
                     'institution_terms: [b], action_terms: []}\n')
            path.write_text('profiles:\n' + entry + entry, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                load_profiles(path)
            self.assertEqual(load_profiles(Path(tmp) / 'absent.yaml'), [])


class TermTests(unittest.TestCase):
    def test_whole_words_unless_a_trailing_star(self):
        self.assertIsNone(term_pattern('end').search('Senate to endorse the bill'))
        self.assertIsNotNone(term_pattern('end').search('The end of the blockade'))
        self.assertIsNone(term_pattern('depart').search('The State Department said'))
        self.assertIsNone(term_pattern('quit').search('It was quite clear'))
        self.assertIsNotNone(term_pattern('resign*').search('His resignation was announced'))
        self.assertIsNotNone(term_pattern('Iran*').search("Iran's navy"))
        self.assertIsNone(term_pattern('Iran*').search('Iraq and Syria'))
        self.assertIsNotNone(term_pattern('shake-up').search('A cabinet SHAKE-UP is expected'))


class GateTests(unittest.TestCase):
    def reason(self, question, title, body='', published=CUTOFF - DAY):
        return rejection_reason(snapshot(question), doc(title, body, published))

    def test_rubio_relevant_documents_are_accepted(self):
        for title in ('Rubio Dismisses Reports He Will Leave His Jobs to Run for President',
                      "State Department: Reports of Rubio mulling exit 'baseless hoax'",
                      'White House denies Hegseth and Rubio to leave jobs after Republican election losses',
                      'Rubio to step down as national security adviser, sources say'):
            with self.subTest(title=title):
                self.assertIsNone(self.reason(RUBIO, title))

    def test_rubio_near_topic_and_unrelated_documents_are_rejected_with_one_reason(self):
        self.assertEqual(self.reason(RUBIO, 'Rubio Says Elections in Venezuela Would Ideally Be Held Next Year'),
                         'no_institution_or_action')
        self.assertEqual(self.reason(RUBIO, 'Rubio places additional sanctions on Cuba, targeting illicit shipping network'),
                         'no_institution_or_action')
        self.assertEqual(self.reason(RUBIO, 'Rubio meets State Department staff in Washington'),
                         'no_institution_or_action')  # "department" must not count as the action "depart"
        self.assertEqual(self.reason(RUBIO, 'Fed minutes show the cabinet of governors split on rates'), 'no_entity')

    def test_incidental_body_text_does_not_qualify_a_document(self):
        """False positives seen on the 546 documents collected on the VM (all were accepted through one generic term)."""
        self.assertEqual(self.reason(RUBIO, 'Rubio places additional sanctions on Cuba',
                                     'The new measures leave Havana with fewer shipping options.'), 'no_institution_or_action')
        self.assertEqual(self.reason(BLOCKADE, 'U.S., Russia and Ukraine to resume peace talks in Abu Dhabi next week',
                                     'Witkoff also discussed Iran and a possible ceasefire; sanctions could be lifted.'),
                         'no_institution_or_action')
        self.assertEqual(self.reason(BLOCKADE, 'Why Pete Hegseth could fall through thin ice',
                                     'The Iran war and the Strait of Hormuz crisis have strained the Pentagon.'),
                         'no_institution_or_action')
        # but a document that really discusses the blockade passes through its body
        self.assertIsNone(self.reason(BLOCKADE, 'After midterm defeat, Trump faces mostly bad options in Iran',
                                      'Options include ending the naval blockade or striking again.'))

    def test_blockade_relevant_and_irrelevant_documents(self):
        for title in ('Iran demands US lift naval blockade before talks resume',
                      'Strait of Hormuz reopening hinges on deal, Oman says',
                      'Tehran says US ends port blockade',
                      'Iranian tankers wait as shipping through Hormuz stays shut'):
            with self.subTest(title=title):
                self.assertIsNone(self.reason(BLOCKADE, title))
        self.assertEqual(self.reason(BLOCKADE, 'Iran national team wins friendly'), 'no_institution_or_action')
        # Two real headlines the first version of the profile wrongly accepted (generic terms like negotiat*)
        self.assertEqual(self.reason(BLOCKADE, "Iran's chief negotiator says Americans voted 'no to war'"),
                         'no_institution_or_action')
        self.assertEqual(self.reason(BLOCKADE, 'Iran weighs in on midterms: Americans said NO TO WAR'),
                         'no_institution_or_action')
        self.assertEqual(self.reason(BLOCKADE, 'Cuba blockade eased as US announces new rules'), 'no_entity')

    def test_body_text_can_supply_the_entity_and_term(self):
        self.assertIsNone(self.reason(RUBIO, 'Cabinet changes expected', 'People close to Rubio say he may resign.'))

    def test_time_rules_and_their_order(self):
        title = 'Rubio to leave cabinet, report says'
        self.assertEqual(self.reason(RUBIO, title, published=CUTOFF + 1), 'after_cutoff')
        self.assertEqual(self.reason(RUBIO, title, published=CUTOFF - 31 * DAY), 'outside_window')
        self.assertIsNone(self.reason(RUBIO, title, published=CUTOFF - 30 * DAY))  # exactly on the window edge
        self.assertEqual(self.reason(RUBIO, 'Fed minutes released', published=CUTOFF - 31 * DAY), 'outside_window')

    def test_unprofiled_market_is_untouched(self):
        self.assertIsNone(rejection_reason(snapshot('Will the Fed cut interest rates?'), doc('Anything at all')))


class RetrievalTests(unittest.TestCase):
    """Real database, so as-of availability (late arrival, changed content) is exercised, not mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 't.sqlite')
        self.cfg = dict(retrieval=dict(include_social=True, top_k=8, min_score=.001, max_per_domain=8, max_chars=1800),
                        dataset=dict(lookback_days=30))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add(self, url, title, published, observed, body=''):
        add_document(self.con, dict(url=url, title=title, body=body, source='news.example', source_type='news',
                                    published_at=published, available_at=observed, observed_at=observed,
                                    provenance='prospective', raw_ref='ref', source_kind='real', content_kind='headline'))

    def titles(self, cutoff=CUTOFF, question=RUBIO):
        return sorted(d['title'] for d in retrieve(self.con, snapshot(question, cutoff), self.cfg))

    def test_only_relevant_documents_survive(self):
        self.add('https://n/1', 'Rubio dismisses reports he will leave his jobs', CUTOFF - 7200, CUTOFF - 3600)
        self.add('https://n/2', 'Rubio says elections in Venezuela could be held next year', CUTOFF - 7200, CUTOFF - 3600)
        self.add('https://n/3', 'Fed minutes show rates steady', CUTOFF - 7200, CUTOFF - 3600)
        self.assertEqual(self.titles(), ['Rubio dismisses reports he will leave his jobs'])

    def test_late_arrival_is_not_evidence_at_the_cutoff(self):
        self.add('https://n/early', 'Rubio to leave cabinet, early report', CUTOFF - 7200, CUTOFF - 3600)
        self.add('https://n/late', 'Rubio to leave cabinet, late report', CUTOFF - 3600, CUTOFF + 600)  # published before, seen after
        self.assertEqual(self.titles(), ['Rubio to leave cabinet, early report'])
        self.assertEqual(len(self.titles(cutoff=CUTOFF + 1200)), 2)

    def test_changed_content_is_versioned_as_of_the_cutoff(self):
        self.add('https://n/same', 'Rubio to leave cabinet, report says', CUTOFF - 3600, CUTOFF - 1800)
        self.add('https://n/same', 'Corrected: Rubio will not leave cabinet', CUTOFF - 3600, CUTOFF + 600)
        self.assertEqual(self.titles(), ['Rubio to leave cabinet, report says'])  # the correction was not visible yet
        self.assertEqual(self.titles(cutoff=CUTOFF + 1200),
                         ['Corrected: Rubio will not leave cabinet', 'Rubio to leave cabinet, report says'])

    def test_no_relevant_evidence_means_the_forecast_is_refused(self):
        self.add('https://n/3', 'Fed minutes show rates steady', CUTOFF - 7200, CUTOFF - 3600)
        self.assertEqual(self.titles(), [])
        with self.assertRaisesRegex(ValueError, 'insufficient_relevant_evidence.*rubio_departure'):
            require_relevant_evidence(snapshot(RUBIO), [])
        require_relevant_evidence(snapshot('Will the Fed cut interest rates?'), [])  # unprofiled: no requirement


class ForecastSkipTests(unittest.TestCase):
    def test_profiled_market_without_evidence_makes_no_llm_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = demo_config(tmp)
            con = connect(cfg['database'])
            try:
                populate(con, cfg, groups=1)
                build(con, cfg)
                data = frame(con).head(1).copy()
                data['question'] = RUBIO
                with patch('src.agents.orchestrator.retrieve', return_value=[]), \
                        patch('src.agents.llm_client.LLMClient.predict') as predict:
                    result = run_agents(con, data, cfg)
                    predict.assert_not_called()
                self.assertEqual(result['calls'], 0)
                self.assertRegex(result['failures'][0]['error'], 'insufficient_relevant_evidence.*rubio_departure')
                self.assertEqual(con.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], 0)
            finally:
                con.close()


if __name__ == '__main__':
    unittest.main()
