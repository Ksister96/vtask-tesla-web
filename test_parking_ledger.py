import json
import os
import tempfile
import unittest
import parking_ledger as ledger

class ParkingTests(unittest.TestCase):
    def test_memory_home_zero_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, 'u', 'trip_details')
            os.makedirs(folder)
            for tid in ('20261101-080000', '20261101-100000', '20261101-120000'):
                with open(os.path.join(folder, tid+'.json'), 'w') as f:
                    json.dump(dict(id=tid, duration_min=60, start_coord=[31,121], end_coord=[31,121], end_addr='地点'), f)
            rows = [s for s in ledger.sessions(root, 'u') if not s['ongoing']]
            self.assertEqual(len(rows), 2)
            ledger.update(root, 'u', rows[-1]['id'], cost=10)
            rows = [s for s in ledger.sessions(root, 'u') if not s['ongoing']]
            self.assertEqual(rows[0]['estimate'], 10)
            ledger.update(root, 'u', rows[0]['id'], cost=0)
            self.assertEqual(next(s for s in ledger.sessions(root, 'u') if s['id'] == rows[0]['id'])['cost'], 0)
            ledger.update(root, 'u', rows[0]['id'], home=True)
            self.assertTrue(all(s['home'] for s in ledger.sessions(root, 'u')))
            self.assertEqual(sum(s['cost'] or 0 for s in ledger.sessions(root, 'u')), 10)
            ledger.update(root, 'u', '', cost=12, name='手动停车', start=1761955200000, end=1761958800000)
            self.assertTrue(any(s.get('manual') and s['cost'] == 12 for s in ledger.sessions(root, 'u')))
            manual = next(s for s in ledger.sessions(root, 'u') if s.get('manual'))
            ledger.update(root, 'u', manual['id'], cost=15)
            self.assertEqual(next(s for s in ledger.sessions(root, 'u') if s['id'] == manual['id'])['cost'], 15)
            self.assertEqual(ledger.sessions(root, 'other'), [])
            with self.assertRaises(ValueError):
                ledger.update(root, 'u', rows[0]['id'], cost=-1)

    def test_reference_uses_multiple_paid_samples_and_ignores_free(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, 'u', 'trip_details')
            os.makedirs(folder)
            for hour in (8, 10, 12, 14):
                tid = '20261101-%02d0000' % hour
                with open(os.path.join(folder, tid+'.json'), 'w') as f:
                    json.dump(dict(id=tid, duration_min=60, start_coord=[31,121], end_coord=[31,121]), f)
            rows = sorted((s for s in ledger.sessions(root, 'u') if not s['ongoing']), key=lambda s: s['start'])
            ledger.update(root, 'u', rows[0]['id'], cost=10)
            ledger.update(root, 'u', rows[1]['id'], cost=20)
            self.assertEqual(next(s for s in ledger.sessions(root, 'u') if s['id'] == rows[2]['id'])['estimate'], 15)
            ledger.update(root, 'u', rows[2]['id'], cost=0)
            self.assertEqual(next(s for s in ledger.sessions(root, 'u') if s['id'] == rows[2]['id'])['rate'], 15)

if __name__ == '__main__':
    unittest.main()
