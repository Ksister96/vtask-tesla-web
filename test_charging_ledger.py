import json
import os
import tempfile
import unittest

from charging_ledger import (confirm_session_cost, decorate_sessions, latest_card_candidate, latest_pending,
                             mark_pending_notified, parse_charge_cost_text,
                             mark_station_lookup_attempt, save_station_match, session_id)


class ChargingLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_charge_is_pending_and_never_gets_home_price(self):
        raw = [{"start": 1000, "end": 2000, "kwh": 20, "vin": "VIN1"}]
        item = decorate_sessions(self.users, "u1", "VIN1", raw)[0]
        self.assertEqual(item["price_status"], "pending")
        self.assertIsNone(item["unit_price"])
        self.assertIsNone(item["total_cost"])

    def test_confirm_total_remembers_station_price_for_next_session(self):
        raw = [{"start": 1000, "end": 2000, "kwh": 20, "vin": "VIN1"}]
        station = {"poi_id": "POI-1", "name": "测试快充站", "address": "测试路1号",
                   "lat": 31.2, "lon": 121.5, "distance_m": 80}
        save_station_match(self.users, "u1", "VIN1", 1000, station)
        sid = session_id("VIN1", 1000)
        confirm_session_cost(self.users, "u1", "VIN1", raw, sid, total_cost=50)
        item = decorate_sessions(self.users, "u1", "VIN1", raw)[0]
        self.assertEqual(item["price_status"], "confirmed")
        self.assertEqual(item["unit_price"], 2.5)
        self.assertEqual(item["total_cost"], 50)

        save_station_match(self.users, "u1", "VIN1", 3000, station)
        next_item = decorate_sessions(self.users, "u1", "VIN1", [
            {"start": 3000, "end": 4000, "kwh": 10, "vin": "VIN1"}
        ])[0]
        self.assertEqual(next_item["price_status"], "remembered")
        self.assertEqual(next_item["unit_price"], 2.5)
        self.assertEqual(next_item["total_cost"], 25)
        self.assertEqual(latest_pending([item, next_item])["id"], next_item["id"])

    def test_chat_cost_parser_is_specific(self):
        self.assertEqual(parse_charge_cost_text("这次充电58元"), 58)
        self.assertEqual(parse_charge_cost_text("刚才超充 38.50 块"), 38.5)
        self.assertIsNone(parse_charge_cost_text("帮我开空调到22度"))
        self.assertIsNone(parse_charge_cost_text("今天花了58元"))

    def test_free_charge_does_not_overwrite_remembered_station_price(self):
        raw = [{"start": 1000, "end": 2000, "kwh": 20, "vin": "VIN1"}]
        station = {"poi_id": "POI-1", "lat": 31.2, "lon": 121.5}
        save_station_match(self.users, "u1", "VIN1", 1000, station)
        sid = session_id("VIN1", 1000)
        confirm_session_cost(self.users, "u1", "VIN1", raw, sid, total_cost=50)
        confirm_session_cost(self.users, "u1", "VIN1", raw, sid, total_cost=0)
        with open(os.path.join(self.users, 'u1', 'charge_places.json'), encoding='utf-8') as source:
            self.assertEqual(json.load(source)['amap:POI-1']['unit_price'], 2.5)

    def test_rich_card_prompt_migrates_from_old_text_notification_once(self):
        folder = os.path.join(self.users, "u1")
        os.makedirs(folder)
        with open(os.path.join(folder, "charge_ledger.json"), "w", encoding="utf-8") as f:
            json.dump({"s1": {"pending_notified": True}}, f)
        self.assertTrue(mark_pending_notified(self.users, "u1", "s1"))
        self.assertFalse(mark_pending_notified(self.users, "u1", "s1"))

    def test_failed_station_lookup_is_persisted_for_retry_throttling(self):
        mark_station_lookup_attempt(self.users, "u1", "VIN1", 1000)
        item = decorate_sessions(self.users, "u1", "VIN1", [
            {"start": 1000, "end": 2000, "kwh": 10, "vin": "VIN1"}
        ])[0]
        self.assertGreater(item["station_lookup_at"], 0)

    def test_confirmed_latest_session_still_gets_a_migration_card(self):
        sessions = [{"id": "old", "end": 1000, "price_status": "confirmed"},
                    {"id": "new", "end": 2000, "price_status": "confirmed"}]
        self.assertEqual(latest_card_candidate(sessions)["id"], "new")


if __name__ == "__main__":
    unittest.main()
