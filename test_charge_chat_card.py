import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock


SPEC = importlib.util.spec_from_file_location("car_web_multi", os.path.join(os.path.dirname(__file__), "car-web-multi.py"))
CAR_WEB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAR_WEB)


class ChargeChatCardTests(unittest.TestCase):
    def test_web_telemetry_does_not_override_normalized_climate_false(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {'TELEMETRY_STATE_DIR': folder}):
                path = os.path.join(folder, 'telemetry_state_TESTVIN.json')
                for fields in ({'hvac_power': False}, {'is_climate_on': True}, {'hvac_power': 'HvacPowerStateOff'}):
                    with open(path, 'w') as target:
                        json.dump({'fields': fields, 'last_update': 1}, target)
                    result = CAR_WEB.Handler._read_telemetry_state(None, 'TESTVIN')['fields']
                    self.assertEqual(result['is_climate_on'], fields.get('is_climate_on', False))

    def test_silence_hallucinations_are_not_treated_as_speech(self):
        for text in ("", "我", "我。", " 嗯 ", "啊！", "呃", "[语音未能识别，请重发或改用文字]"):
            self.assertTrue(CAR_WEB.is_empty_voice_transcript(text), text)
        for text in ("我要开空调", "查电量", "锁车"):
            self.assertFalse(CAR_WEB.is_empty_voice_transcript(text), text)

    def test_old_voice_failure_placeholders_are_hidden_with_their_reply(self):
        messages = [
            {"who": "me", "text": "锁车"},
            {"who": "car", "text": "已锁车"},
            {"who": "me", "text": "[语音未能识别，请重发或改用文字]"},
            {"who": "car", "text": "❌ 未识别到可执行的车辆指令"},
            {"who": "sys", "text": "🚗 行程完成"},
        ]
        self.assertEqual(CAR_WEB.visible_chat_messages(messages), [messages[0], messages[1], messages[4]])

    def test_card_is_persisted_once_using_chat_as_source_of_truth(self):
        with tempfile.TemporaryDirectory() as users_dir:
            previous = CAR_WEB.USERS_DIR
            CAR_WEB.USERS_DIR = users_dir
            try:
                self.assertTrue(CAR_WEB.append_charge_card_once("u1", "待确认", "session-1"))
                self.assertFalse(CAR_WEB.append_charge_card_once("u1", "重复提醒", "session-1"))
                with open(CAR_WEB.chat_path("u1"), encoding="utf-8") as source:
                    messages = json.load(source)
            finally:
                CAR_WEB.USERS_DIR = previous

        cards = [message.get("card") for message in messages if message.get("card")]
        self.assertEqual(cards, [{"type": "charge_cost", "session_id": "session-1"}])

    def test_old_trip_message_is_hydrated_with_matching_detail(self):
        with tempfile.TemporaryDirectory() as users_dir:
            previous = CAR_WEB.USERS_DIR
            CAR_WEB.USERS_DIR = users_dir
            try:
                detail_dir = os.path.join(users_dir, "u1", "trip_details")
                os.makedirs(detail_dir)
                trip_id = "20261101-080000"
                with open(os.path.join(detail_dir, trip_id + ".json"), "w", encoding="utf-8") as target:
                    json.dump({"duration_min": 20, "distance_km": 12.3}, target)
                completed = datetime.strptime(trip_id, "%Y%m%d-%H%M%S") + timedelta(minutes=20)
                messages = [{"who": "sys", "text": "🚗 行程完成：12.3 km · 20 分钟",
                             "ts": completed.strftime("%Y-%m-%d %H:%M:%S")}]
                CAR_WEB.hydrate_trip_chat_cards("u1", messages)
            finally:
                CAR_WEB.USERS_DIR = previous

        self.assertEqual(messages[0]["card"], {"type": "trip_detail", "trip_id": trip_id})


if __name__ == "__main__":
    unittest.main()
