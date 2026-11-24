"""B6 label checks (decisions log R15): consistency per event, demo markets by the D2 rule, reproducible sample."""
import tempfile
import unittest
from pathlib import Path

from retrospective import fetch_labels as fl
from src.db.loaders import connect, insert


def sel_row(mid, event, rule, question, price):
    return dict(market_id=mid, event_id=event, event_title=f'{event} title ', rule=rule, question=question,
                price_168h=price, eligible_168h='yes', eligible_24h='yes')


class LabelCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 't.sqlite')
        self.sel = {}
        rows = [('1', 'ga', 'senate_race', 'D win GA Senate?', '0.48'), ('2', 'ga', 'senate_race', 'R win GA Senate?', '0.52'),
                ('3', 'ne', 'senate_race', 'R win NE Senate?', '0.70'), ('4', 'ne', 'senate_race', 'Ind win NE Senate?', '0.30'),
                ('5', 'az', 'governor_race', 'D win AZ Gov?', '0.45'), ('6', 'az', 'governor_race', 'R win AZ Gov?', '0.55')]
        rows += [(str(i), f'e{i}', 'governor_race', f'Q{i}?', '0.9') for i in range(7, 20)]
        for mid, ev, rule, q, p in rows:
            self.sel[mid] = sel_row(mid, ev, rule, q, p)
            insert(self.con, 'markets', dict(market_id=mid, condition_id='c' + mid, event_group=ev, yes_token='y',
                                             no_token='n', yes_index=0, source_kind='real'))
        self.outcomes = {'1': 1, '2': 0, '3': 0, '4': 0, '5': 0, '6': 1}
        self.outcomes.update({str(i): 1 for i in range(7, 20)})
        for mid, y in self.outcomes.items():
            insert(self.con, 'resolutions', dict(market_id=mid, outcome=y, resolved_at=1, label_available_at=2,
                                                 observed_at=2, source='polymarket_data_v2', status='final', raw_ref='r'))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_demo_markets_follow_the_d2_rule(self):
        self.assertEqual(fl.demo_markets(self.sel), ['1', '5'])       # closest to 0.5; GA tie 0.48/0.52 -> lower id

    def test_consistency_and_cross_check_list(self):
        r = fl.checks(self.con, self.sel)
        self.assertEqual((r['labels'], r['missing'], r['events_with_several_yes']), (19, [], []))
        self.assertEqual([t for t, _ in r['events_with_no_yes']], ['ne title'])   # winner had no eligible market
        self.assertEqual([m for m, _, _ in r['cross_check'][:2]], ['1', '5'])
        self.assertEqual(len(r['cross_check']), 12)
        self.assertEqual(r['cross_check'], fl.checks(self.con, self.sel)['cross_check'])  # reproducible

    def test_two_yes_in_one_event_is_reported(self):
        self.con.execute("UPDATE resolutions SET outcome=1 WHERE market_id='2'")
        self.con.commit()
        self.assertEqual(fl.checks(self.con, self.sel)['events_with_several_yes'], [('ga title', ['1', '2'])])


if __name__ == '__main__':
    unittest.main()
