"""B11 election evidence gate (decisions log R4 + R4a, 2026-11-18) for the 2026 midterm replay markets."""
import csv
import re
import unittest
from pathlib import Path

from src.retrieval.relevance import (BALANCE, HOUSE, RACE_CANDIDATES, SENATE_CONTROL, compile_profile, election_race,
                                     rejection_reason, relevance_profile, require_relevant_evidence)

ROOT = Path(__file__).resolve().parents[1]
SELECTION = ROOT / 'retrospective' / 'selection.csv'
CUTOFF = 1_793_681_940   # 2026-11-03T04:59:00Z, the 24 h replay cutoff
DAY = 86_400

GA_SEN = 'Will the Democrats win the Georgia Senate race in 2026?'
TX_SEN = 'Will the Republicans win the Texas Senate race in 2026?'
GA_GOV = 'Will the Democrats win the Georgia governor race in 2026?'
OH_GOV = 'Will the Democrats win the Ohio governor race in 2026?'
MI_GOV = 'Will the Republicans win the Michigan governor race in 2026?'
AZ_GOV = 'Will the Democrats win the Arizona governor race in 2026?'
FL_GOV = 'Will the Republicans win the Florida governor race in 2026?'
CA_BECERRA = 'Will Xavier Becerra win the California Governor Election in 2026?'
ME_SEN = 'Will the Democrats win the Maine Senate race in 2026?'
NC_SEN = 'Will the Democrats win the North Carolina Senate race in 2026?'
AR_SEN = 'Will the Democrats win the Arkansas Senate race in 2026?'
SEN_CONTROL = 'Will the Republican Party control the Senate after the 2026 Midterm elections?'
HOUSE_CONTROL = 'Will the Democratic Party control the House after the 2026 Midterm elections?'
BOP = '2026 Balance of Power: R Senate, D House'


def snapshot(question, cutoff=CUTOFF):
    return dict(question=question, rules='Resolves on the certified result.', cutoff=cutoff, source_kind='real')


def doc(title, body='', published=CUTOFF - DAY, available=None):
    return dict(document_id=title, content_hash=title, title=title, body=body, source='example.test',
                source_type='news', content_kind='article', published_at=published,
                available_at=published if available is None else available, observed_at=published,
                provenance='archive_verified', source_kind='real')


def reason(question, title, body='', **kw):
    return rejection_reason(snapshot(question), doc(title, body, **kw))


def eligible_rows():
    with SELECTION.open(encoding='utf-8') as f:
        return [r for r in csv.DictReader(f) if 'yes' in (r['eligible_168h'], r['eligible_24h'])]


class CoverageTests(unittest.TestCase):
    def test_every_eligible_replay_market_has_a_gate(self):
        """No replay forecast may use an unprofiled market (report Ch 5)."""
        rows = eligible_rows()
        self.assertEqual(len(rows), 117)
        for r in rows:
            with self.subTest(question=r['question']):
                self.assertIsNotNone(relevance_profile({'question': r['question']}))

    def test_gate_type_follows_the_selection_rule(self):
        expected = {'national_control': {HOUSE, SENATE_CONTROL, BALANCE}}
        for r in eligible_rows():
            profile = relevance_profile({'question': r['question']})
            if r['rule'] == 'national_control':
                self.assertIn(profile, expected['national_control'], r['question'])
            else:
                office = 'senate' if r['rule'] == 'senate_race' else 'governor'
                self.assertTrue(profile.startswith(f'election_{office}_'), (r['question'], profile))
                state = election_race(r['question'])[0]
                self.assertTrue(r['event_title'].strip().startswith(state + ' '), (r['event_title'], state))

    def test_candidate_names_are_exactly_those_in_the_eligible_questions(self):
        """R4a: no hand-collected nominee lists; names come only from the markets' own questions."""
        found = {}
        for r in eligible_rows():
            race = election_race(r['question'])
            if not race:
                continue
            who = re.match(r'Will (.+?) win the', r['question']).group(1)
            if not re.match(r'(the |an? )', who, re.IGNORECASE):
                found.setdefault((race[0], race[1]), set()).add(who)
        self.assertEqual({k: set(v) for k, v in RACE_CANDIDATES.items()}, found)

    def test_other_questions_get_no_election_gate(self):
        for question in ('Will Marco Rubio be the 2028 Republican nominee?', 'Will the Fed cut interest rates?',
                         'Will the Democrats win the Georgia Senate race in 2028?',
                         'US announces end of Iranian blockade by November 15, 2026?'):
            with self.subTest(question=question):
                self.assertFalse(str(relevance_profile({'question': question})).startswith('election_'))
        self.assertEqual(relevance_profile({'question': HOUSE_CONTROL}), HOUSE)
        self.assertEqual(relevance_profile({'question': SEN_CONTROL}), SENATE_CONTROL)
        self.assertEqual(relevance_profile({'question': BOP}), BALANCE)

    def test_election_profile_ids_are_reserved(self):
        good = dict(id='p', subtype='s', question_regex='x', window_days=30, entities=['a'], institution_terms=[],
                    action_terms=['b'])
        for reserved in (SENATE_CONTROL, BALANCE, 'election_senate_texas'):
            with self.assertRaisesRegex(ValueError, 'reserved'):
                compile_profile({**good, 'id': reserved})


class RaceGateTests(unittest.TestCase):
    """The examples of the approved proposal (invented headlines), in R4a's check order."""

    def test_admitted(self):
        for question, title in ((GA_SEN, 'Georgia Senate race tightens in final week, poll finds'),
                                (MI_GOV, 'Michigan governor candidates spar in final debate'),
                                (CA_BECERRA, 'Steve Hilton holds rally in Fresno'),
                                (ME_SEN, "Democrats' Senate hopes run through Maine and North Carolina, polls show"),
                                (NC_SEN, "Democrats' Senate hopes run through Maine and North Carolina, polls show")):
            with self.subTest(question=question, title=title):
                self.assertIsNone(reason(question, title))

    def test_rejected_with_one_reason(self):
        self.assertEqual(reason(TX_SEN, 'Texas Senate votes on property tax bill'), 'no_election_context')
        self.assertEqual(reason(GA_GOV, "Georgia's parliament debates new election law"), 'no_office')
        self.assertEqual(reason(OH_GOV, 'Ohio governor signs state budget'), 'no_election_context')
        self.assertEqual(reason(AZ_GOV, 'Lessons from the 2022 Arizona governor race'), 'different_election_year')
        self.assertEqual(reason(FL_GOV, 'Florida governor declares emergency as hurricane nears'), 'no_election_context')
        self.assertEqual(reason(GA_SEN, 'Ossoff leads in final poll'), 'no_race_entity')   # known recall cost

    def test_time_checks_come_first(self):
        title = 'Georgia Senate race tightens in final week, poll finds'
        self.assertEqual(reason(GA_SEN, title, published=CUTOFF + 1860), 'after_cutoff')
        self.assertEqual(reason(GA_SEN, title, available=CUTOFF + 1), 'after_cutoff')
        self.assertIsNone(reason(GA_SEN, title, published=CUTOFF))
        self.assertIsNone(reason(GA_SEN, title, published=CUTOFF - 30 * DAY))
        self.assertEqual(reason(GA_SEN, title, published=CUTOFF - 30 * DAY - 1), 'outside_window')

    def test_whole_word_state_names(self):
        self.assertEqual(reason('Will the Democrats win the Kansas Senate race in 2026?',
                                'Arkansas Senate race heats up'), 'no_race_entity')
        self.assertIsNone(reason(AR_SEN, 'Arkansas Senate race heats up'))
        self.assertEqual(reason('Will the Democrats win the New Mexico governor race in 2026?',
                                'Mexico governor race draws crowds'), 'no_race_entity')

    def test_election_words_are_whole_words(self):
        # "racial", "pollution", "votes", "debates" (legislative verb) are not election context
        self.assertEqual(reason(TX_SEN, 'Texas Senate debates racial justice bill'), 'no_election_context')
        self.assertEqual(reason(TX_SEN, 'Texas senator warns on racial bias'), 'no_election_context')
        self.assertEqual(reason(TX_SEN, 'Texas Senate committee studies pollution'), 'no_election_context')

    def test_office_forms(self):
        self.assertIsNone(reason(GA_GOV, "Georgia's gubernatorial candidates make final pitch"))
        self.assertIsNone(reason(GA_GOV, "Georgia governor's race: early voting turnout hits record"))
        self.assertIsNone(reason(GA_SEN, 'Sen. Ossoff campaigns across Georgia'))
        self.assertEqual(reason(GA_SEN, 'Georgia governor race: final poll'), 'no_office')

    def test_body_counts_but_only_the_text_the_agent_sees(self):
        self.assertIsNone(reason(GA_SEN, 'Final week in Georgia', 'The Senate race is within the margin of error.'))

    def test_current_year_mentions_are_fine(self):
        self.assertIsNone(reason(GA_SEN, 'Georgia 2026 Senate race: what to watch'))
        self.assertEqual(reason(GA_SEN, 'Georgia Senate race echoes the 2020 election'), 'different_election_year')


class NationalGateTests(unittest.TestCase):
    def test_senate_control(self):
        self.assertIsNone(reason(SEN_CONTROL, 'Republicans favoured to keep Senate majority in midterms'))
        self.assertEqual(reason(SEN_CONTROL, 'Senate passes defense bill'), 'missing_us_senate_context')
        self.assertEqual(reason(SEN_CONTROL, 'Republicans in the Senate block judicial nominee'), 'missing_election_context')
        self.assertEqual(reason(SEN_CONTROL, 'Republicans eye Senate majority in 2028 elections'),
                         'different_headline_election_year')

    def test_balance_of_power_admits_either_chamber(self):
        self.assertIsNone(reason(BOP, 'Republicans favoured to keep Senate majority in midterms'))
        self.assertIsNone(reason(BOP, 'Democrats lead in race for House control, polls show'))
        self.assertEqual(reason(BOP, 'Fed holds rates steady'), 'missing_us_congress_context')

    def test_house_gate_unchanged(self):
        self.assertIsNone(reason(HOUSE_CONTROL, 'Democrats lead in race for House control, polls show'))


class NoEvidenceTests(unittest.TestCase):
    def test_a_gated_race_without_documents_makes_no_llm_call(self):
        with self.assertRaisesRegex(ValueError, 'insufficient_relevant_evidence.*election_senate_georgia'):
            require_relevant_evidence(snapshot(GA_SEN), [])


if __name__ == '__main__':
    unittest.main()
