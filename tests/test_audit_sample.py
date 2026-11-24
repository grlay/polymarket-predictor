"""B5 audit sample (decisions log R13): population = documents shown to the agents; sheet shows nothing revealing."""
import csv
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from retrospective import audit_sample
from src.common import load_config
from src.dataset.snapshots import build
from src.db.loaders import add_document, connect, insert
from tests.test_replay import C168, C24, FakeRaw, GA_SEN, gamma_market


class AuditSampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        fake = FakeRaw(root, [gamma_market('101', GA_SEN)],
                       {('101', '168h'): [(C168 - 600, .41)], ('101', '24h'): [(C24 - 300, .47)]})
        fake.build()
        cfg = deepcopy(load_config('configs/config_retro.yaml'))
        self.db = root / 'retro.sqlite'
        con = connect(self.db)
        for name in ('markets', 'market_versions', 'prices'):
            for r in fake.records(name):
                insert(con, name, r)
        cfg['database'] = str(self.db)
        build(con, cfg)
        ids = []
        for i in range(60):
            add_document(con, dict(url=f'https://n/{i}', title=f'Georgia Senate race item {i}', body='x' * 3000,
                                   source='www.politico.com', source_type='news', published_at=C24 - 3600,
                                   available_at=C24 - 3600, observed_at=C24, provenance='archive_verified',
                                   raw_ref='ref', source_kind='real', content_kind='feed_summary'))
        ids = [r[0] for r in con.execute('SELECT document_id FROM documents ORDER BY document_id')]
        insert(con, 'forecast_runs', dict(run_id='r', created_at=1, config_json='{}', config_hash='h', model='m',
                                          provider='p', prompt_hash='x', source_kind='real'))
        snaps = {round(r[1]): r[0] for r in con.execute('SELECT snapshot_id, horizon_hours FROM snapshots')}
        for h, chosen in ((24, ids[:50]), (168, ids[50:])):
            for system in ('single_agent_rag', 'news_agent'):
                insert(con, 'forecasts', dict(forecast_id=f'{h}{system}', run_id='r', snapshot_id=snaps[h], system=system,
                                              probability=.31, status='ok', result_json='{}',
                                              evidence_json=json.dumps(chosen), generated_at=C24 + 60))
        con.close()
        self.ids = ids

    def tearDown(self):
        self.tmp.cleanup()

    def test_sample_rule_and_sheet_contents(self):
        out = Path(self.tmp.name) / 'out'
        result = audit_sample.build(self.db, Path(self.tmp.name) / 'absent.sqlite', out)
        self.assertEqual(result, dict(replay=40, pilot=0, total=40))
        key = list(csv.DictReader(open(out / 'audit_key.csv', encoding='utf-8')))
        self.assertTrue({k['document_id'] for k in key} <= set(self.ids[:50]))     # 24 h pairs only
        sheet = list(csv.reader(open(out / 'audit_sheet.csv', encoding='utf-8-sig')))
        header, rows = sheet[0], sheet[1:]
        self.assertEqual(len(rows), 40)
        text = ' '.join(header + [c for r in rows for c in r]).lower()
        for leak in ('0.31', '0.47', 'single_agent', 'news_agent', 'outcome', 'price', 'gate', 'replay', 'pilot'):
            self.assertNotIn(leak, text, leak)
        self.assertTrue(all(len(r[6]) == audit_sample.MAX_CHARS for r in rows))   # text cut as the agent saw it

    def test_the_draw_is_reproducible(self):
        a, b = Path(self.tmp.name) / 'a', Path(self.tmp.name) / 'b'
        audit_sample.build(self.db, Path(self.tmp.name) / 'absent.sqlite', a)
        audit_sample.build(self.db, Path(self.tmp.name) / 'absent.sqlite', b)
        self.assertEqual((a / 'audit_key.csv').read_text(), (b / 'audit_key.csv').read_text())


if __name__ == '__main__':
    unittest.main()
