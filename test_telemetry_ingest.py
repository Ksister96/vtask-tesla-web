import unittest
from unittest import mock

import telemetry_ingest


class TelemetryIngestTest(unittest.TestCase):
    def test_unchanged_observation_still_refreshes_ingest_time(self):
        state = {"vin": "VIN", "fields": {"battery_level": 80.0}, "last_update": 1}
        record = {"vin": "VIN", "data": {"BatteryLevel": {"doubleValue": 80.0}}}
        with mock.patch.object(telemetry_ingest, "load_state", return_value=state), \
             mock.patch.object(telemetry_ingest, "save_state") as save:
            changed = telemetry_ingest.handle_record(record)
        self.assertFalse(changed)
        save.assert_called_once_with(state)

    def test_vehicle_timestamp_is_preserved_when_present(self):
        state = {"vin": "VIN", "fields": {}}
        record = {
            "vin": "VIN",
            "createdAt": "2023-11-14T22:13:20Z",
            "data": {"Locked": {"booleanValue": True}},
        }
        with mock.patch.object(telemetry_ingest, "load_state", return_value=state), \
             mock.patch.object(telemetry_ingest, "save_state"):
            telemetry_ingest.handle_record(record)
        self.assertEqual(state["vehicle_ts"], 1700000000)

    def test_uppercase_nested_created_at_is_preserved(self):
        state = {"vin": "VIN", "fields": {}}
        record = {
            "vin": "VIN",
            "data": {
                "CreatedAt": "2023-11-14T22:13:20Z",
                "Locked": {"booleanValue": True},
            },
        }
        with mock.patch.object(telemetry_ingest, "load_state", return_value=state), \
             mock.patch.object(telemetry_ingest, "save_state"):
            telemetry_ingest.handle_record(record)
        self.assertEqual(state["vehicle_ts"], 1700000000)

    def test_unchanged_frames_are_disk_throttled(self):
        state = {"vin": "VIN", "fields": {"battery_level": 80.0}, "last_update": 100}
        record = {"vin": "VIN", "data": {"BatteryLevel": {"doubleValue": 80.0}}}
        with mock.patch.object(telemetry_ingest, "load_state", return_value=state), \
             mock.patch.object(telemetry_ingest, "save_state") as save, \
             mock.patch.object(telemetry_ingest.time, "time", return_value=101):
            telemetry_ingest.handle_record(record)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
