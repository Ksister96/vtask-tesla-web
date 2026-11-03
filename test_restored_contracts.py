"""Offline regression checks for the miniapp API and destructive-operation gates."""
import json
import os
import tempfile
import unittest
from unittest import mock

from test_charge_chat_card import CAR_WEB as web


class RestoredContractsTests(unittest.TestCase):
    def handler(self, path, auth=None, body=None):
        handler = object.__new__(web.Handler)
        handler.path = path
        handler.headers = {}
        handler._auth = mock.Mock(return_value=auth)
        handler._parse_json = mock.Mock(return_value=body or {})
        handler._json = mock.Mock()
        handler._send = mock.Mock()
        return handler

    def test_get_routes_exist_and_require_authentication(self):
        for path in ('/api/charge_sessions', '/api/parking_sessions', '/api/weather', '/api/trip_list'):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_GET()
                self.assertEqual(handler._json.call_args.args[0], 401)
                handler._send.assert_not_called()

    def test_update_routes_exist_and_require_authentication(self):
        for path in ('/api/charge_session/update', '/api/parking_session/update'):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_POST()
                self.assertEqual(handler._json.call_args.args[0], 401)

    def test_charge_history_is_returned_without_private_coordinates(self):
        handler = self.handler('/api/charge_sessions', {'uid': 'u'})
        session = {'id': 'charge-1', 'start': 1, 'end': 2, 'kwh': 20,
                   'price_status': 'confirmed', 'total_cost': 50, 'unit_price': 2.5,
                   'vin': 'VIN', 'latitude': 31.2, 'longitude': 121.5}
        handler._charge_sessions_data = mock.Mock(return_value=[session])
        handler._ensure_charge_card = mock.Mock()
        with mock.patch.object(web, 'get_account', return_value={'vehicles': [{'vin': 'VIN'}]}):
            handler.do_GET()
        status, result = handler._json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(result['sessions'][0]['total_cost'], 50)
        for field in ('vin', 'latitude', 'longitude'):
            self.assertNotIn(field, result['sessions'][0])

    def test_charge_update_preserves_bill_and_price_memory(self):
        with tempfile.TemporaryDirectory() as users:
            folder = os.path.join(users, 'u', 'charge_sessions')
            os.makedirs(folder)
            raw = {'start': 1000, 'end': 200000, 'kwh': 20, 'vin': 'VIN'}
            with open(os.path.join(folder, '202611.jsonl'), 'w', encoding='utf8') as target:
                target.write(json.dumps(raw) + '\n')
            sid = web.decorate_sessions(users, 'u', 'VIN', [raw])[0]['id']
            handler = self.handler('/api/charge_session/update', {'uid': 'u'},
                                   {'id': sid, 'total_cost': 50})
            handler._ensure_charge_card = mock.Mock()
            handler._read_telemetry_state = mock.Mock(return_value=None)
            with mock.patch.object(web, 'USERS_DIR', users), mock.patch.object(
                    web, 'get_account', return_value={'vehicles': [{'vin': 'VIN'}]}):
                handler.do_POST()
            self.assertEqual(handler._json.call_args.args[0], 200)
            item = web.decorate_sessions(users, 'u', 'VIN', [raw])[0]
            self.assertEqual(item['price_status'], 'confirmed')
            self.assertEqual(item['total_cost'], 50)
            self.assertEqual(item['unit_price'], 2.5)

    def test_protected_unbind_is_rejected_without_touching_files(self):
        uid = next(iter(web.PROTECTED_UNBIND_UIDS))
        handler = self.handler('/api/unbind', {'uid': uid, 'name': 'test'}, {'confirm': 'UNBIND'})
        with mock.patch.object(web, '_unbind_user_files') as delete, mock.patch.object(web, 'log_line'):
            handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 403)
        delete.assert_not_called()

    def test_unconfirmed_unbind_is_rejected_without_touching_files(self):
        handler = self.handler('/api/unbind', {'uid': 'test', 'name': 'test'})
        with mock.patch.object(web, '_unbind_user_files') as delete:
            handler.do_POST()
        self.assertEqual(handler._json.call_args.args[0], 400)
        delete.assert_not_called()
