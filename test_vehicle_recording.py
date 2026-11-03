import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import charging_history
import recover_charge_history
import repair_trip_history
import repair_trip_energy
import track_split
from vehicle_measurements import normalize

with mock.patch.object(sys, "argv", ["track_watcher_multi.py"]):
    watcher = importlib.import_module("track_watcher_multi")


def point(ts, gear="D", odo=1, spd=0):
    return {"ts": ts, "gear": gear, "odo": odo, "spd": spd, "lat": 30, "lon": 120}


class RecordingTests(unittest.TestCase):
    def test_climate_power_types_preserve_real_off_and_missing_observations(self):
        for value in (False, 'HvacPowerStateOff', 'false', 'off', 0):
            self.assertIs(normalize({'hvac_power': value})['is_climate_on'], False)
        for value in (True, 'HvacPowerStateOn', 'true', 'on', 1):
            self.assertIs(normalize({'hvac_power': value})['is_climate_on'], True)
        self.assertIs(normalize({'is_climate_on': True})['is_climate_on'], True)
        self.assertNotIn('is_climate_on', normalize({}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(watcher, "USERS", self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        os.makedirs(os.path.join(self.tmp.name, "u"))
        with open(os.path.join(self.tmp.name, "u", "account.json"), "w") as f:
            json.dump({"vehicles": [{"vin": "VIN"}]}, f)
        watcher._charge_sess.clear()
        watcher._trip_cur.clear()
        watcher._trip_stop.clear()
        watcher._trip_loaded.clear()
        watcher._last_moving.clear()

    def test_dc_idle_is_charging_but_complete_wins(self):
        f = normalize({"charging_state": "Idle", "dc_charging_power": 50,
                       "charger_voltage": 1.65, "charger_actual_current": 32,
                       "shift_state": "ShiftStateD"})
        self.assertTrue(f["charging"])
        self.assertEqual(f["shift_state"], "D")
        self.assertIsNone(f["charger_voltage"])
        self.assertIsNone(f["charger_actual_current"])
        self.assertFalse(normalize(dict(f, charging_state="Complete"))["charging"])

    def test_system_trip_message_persists_detail_card(self):
        fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=lambda *_: None)
        with mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}):
            watcher.sys_msg("u", "🚗 行程完成：12.3 km · 20 分钟",
                            {"type": "trip_detail", "trip_id": "20261101-080000"})
        with open(os.path.join(self.tmp.name, "u", "chat.json"), encoding="utf-8") as source:
            messages = json.load(source)
        self.assertEqual(messages[-1]["card"],
                         {"type": "trip_detail", "trip_id": "20261101-080000"})

    def test_dense_sampling_keeps_peak_and_sparse_sampling_marks_it_unreliable(self):
        dense = {"speed_series": [[0, 20], [1000, 80], [2000, 40]],
                 "power_series": [[0, 5], [1000, 20], [2000, -4]]}
        track_split.apply_sampling_quality(dense)
        self.assertTrue(dense["max_kmh_reliable"])
        self.assertEqual(dense["max_kmh"], 80)
        self.assertEqual(dense["sample_coverage_pct"], 100)
        self.assertEqual(dense["power_sample_points"], 3)

        sparse = {"speed_series": [[0, 20], [10000, 80], [20000, 40]], "power_series": []}
        track_split.apply_sampling_quality(sparse)
        self.assertFalse(sparse["max_kmh_reliable"])
        self.assertEqual(sparse["max_kmh"], 80)
        self.assertEqual(sparse["max_kmh_sampled"], 80)
        self.assertEqual(sparse["sample_coverage_pct"], 20)
        self.assertEqual(sparse["sample_gap_max_s"], 10)

        empty = {"speed_series": [], "power_series": []}
        track_split.apply_sampling_quality(empty)
        self.assertIsNone(empty["max_kmh"])
        self.assertFalse(empty["max_kmh_reliable"])

    def test_charge_survives_restart_and_does_not_count_duplicate_snapshot(self):
        cs = {"charging_state": "Idle", "charger_power": 60, "dc_charging_power": 60,
              "fast_charger_present": True, "latitude": 30.1, "longitude": 120.2,
              "sample_ts": 1000000}
        watcher._track_charging("u", cs)
        watcher._track_charging("u", dict(cs, sample_ts=1060000))
        watcher._charge_sess.clear()  # process restart
        watcher._track_charging("u", dict(cs, sample_ts=1060000))
        self.assertEqual(watcher._charge_sess["u"]["kwh"], 1)
        watcher._track_charging("u", dict(cs, sample_ts=1120000))
        watcher._track_charging("u", {"charging_state": "Complete", "sample_ts": 1180000})
        records = charging_history.read_sessions(self.tmp.name, "u", "VIN", 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kwh"], 2)
        self.assertTrue(records[0]["estimated"])
        self.assertEqual(records[0]["charge_type"], "dc_fast")
        self.assertEqual(records[0]["max_dc_power"], 60)
        self.assertEqual(records[0]["latitude"], 30.1)

    def test_real_energy_increment_is_preferred(self):
        for ts, energy in ((1000000, 1), (1060000, 2), (1120000, 3)):
            watcher._track_charging("u", {"charging": True, "charger_power": 60,
                                          "charge_energy_added": energy, "sample_ts": ts})
        self.assertEqual(watcher._charge_sess["u"]["kwh"], 3)
        self.assertFalse(watcher._charge_sess["u"].get("estimated", False))

    def test_d_zero_speed_and_unknown_gear_do_not_end_trip(self):
        for p in (point(1000000, spd=20), point(1010000, gear=None), point(1020000)):
            watcher._track_trip("u", p)
        self.assertEqual(len(watcher._trip_cur["u"]), 2)
        self.assertNotIn("u", watcher._trip_stop)

    def test_short_park_resumes_and_long_park_splits(self):
        pts = [point(1000000), point(1010000, odo=1.1), point(1020000, "P", 1.1),
               point(1080000, odo=1.2), point(1100000, "P", 1.2), point(1300000, odo=1.3), point(1310000, odo=1.4)]
        trips = track_split.split_trips(pts)
        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[0]["n"], 3)

    def test_restart_preserves_trip_and_pending_stop(self):
        watcher._track_trip("u", point(1000000, spd=20))
        watcher._track_trip("u", point(1010000, "P"))
        watcher._trip_cur.clear()
        watcher._trip_stop.clear()
        watcher._restore_trip("u")
        self.assertEqual(len(watcher._trip_cur["u"]), 1)
        self.assertEqual(watcher._trip_stop["u"]["ts"], 1010000)

    def test_telemetry_poll_records_normalized_d_gear_and_battery_power(self):
        import time
        now = time.time()
        state = {"last_update": now, "fields": {"shift_state": "ShiftStateD", "speed": 0,
                 "latitude": 30, "longitude": 120, "odometer": 100,
                 "pack_voltage": 400, "pack_current": -25},
                 "field_ts": {"pack_voltage": now, "pack_current": now}}
        with open(os.path.join(self.tmp.name, "telemetry_state_VIN.json"), "w") as f:
            json.dump(state, f)
        with mock.patch.object(watcher, "TELEMETRY_DIR", self.tmp.name), \
             mock.patch.object(watcher, "http_json", side_effect=AssertionError("unexpected network")):
            watcher.poll_user("u")
        p = watcher._trip_cur["u"][0]
        self.assertEqual(p["gear"], "D")
        self.assertEqual(p["pwr"], -10)
        self.assertEqual(p["power_source"], "battery")

    def test_battery_current_sign_is_normalized_for_consumption_and_regen(self):
        pts = []
        for index, power in enumerate((-10, -10, 5, 5)):
            p = point(1000000 + index * 60000, odo=1 + index, spd=30)
            p.update({"pwr": power, "power_source": "battery"})
            pts.append(p)
        detail = track_split.build_detail(pts)
        self.assertEqual(detail["power_series"], [[1000000, 10], [1060000, 10], [1120000, -5], [1180000, -5]])
        self.assertEqual(detail["consumed_kwh"], 0.33)
        self.assertEqual(detail["regen_kwh"], 0.08)
        self.assertEqual(detail["wh_per_km"], 83)
        self.assertEqual(detail["regen_rate"], 25.0)
        self.assertEqual(detail["energy_sign_version"], 2)

    def test_old_battery_detail_is_repaired_only_once(self):
        detail = {"distance_km": 3, "power_source": "battery",
                  "power_series": [[1000000, -10], [1060000, -10], [1120000, 5], [1180000, 5]],
                  "consumed_kwh": 0.08, "regen_kwh": 0.33, "wh_per_km": 0}
        self.assertTrue(track_split.repair_battery_power_detail(detail))
        self.assertEqual(detail["consumed_kwh"], 0.33)
        self.assertEqual(detail["regen_kwh"], 0.08)
        self.assertEqual(detail["wh_per_km"], 83)
        self.assertFalse(track_split.repair_battery_power_detail(detail))

    def test_energy_repair_tool_dry_run_then_applies_with_backup(self):
        folder = os.path.join(self.tmp.name, "users", "u", "trip_details")
        os.makedirs(folder)
        path = os.path.join(folder, "20261101-000000.json")
        original = {"distance_km": 3, "power_source": "battery",
                    "power_series": [[1000000, -10], [1060000, -10], [1120000, 5], [1180000, 5]],
                    "consumed_kwh": 0.08, "regen_kwh": 0.33, "wh_per_km": 0}
        with open(path, "w") as target:
            json.dump(original, target)
        self.assertEqual(repair_trip_energy.run(self.tmp.name, {"20261101"}, False), 1)
        with open(path) as source:
            self.assertEqual(json.load(source)["wh_per_km"], 0)
        self.assertEqual(repair_trip_energy.run(self.tmp.name, {"20261101"}, True), 1)
        with open(path) as source:
            self.assertEqual(json.load(source)["wh_per_km"], 83)
        backups = list(os.walk(os.path.join(self.tmp.name, "trip_energy_backups")))
        self.assertTrue(any("20261101-000000.json" in files for _, _, files in backups))

    def test_old_measurements_are_unknown_instead_of_reused(self):
        fields = {"charging_state": "Charging", "charger_voltage": 220, "charger_actual_current": 32,
                  "pack_voltage": 400, "pack_current": 25}
        f = normalize(fields, {key: 100 for key in fields}, now=1000)
        self.assertIsNone(f["charger_voltage"])
        self.assertIsNone(f["pack_current"])

    def test_historical_raw_d_is_not_a_stop_and_speed_is_converted(self):
        pts = [point(1000000, "ShiftStateD", 1, 10), point(1010000, "ShiftStateD", 1.1, 0),
               point(1100000, "ShiftStateD", 1.2, 20)]
        fixed = repair_trip_history.corrected(pts)
        self.assertEqual(len(track_split.split_trips(fixed)), 1)
        self.assertAlmostEqual(fixed[0]["spd"], 16.09, places=2)

    def test_other_vehicle_charge_records_are_excluded(self):
        for uid, vin in (("old", "VIN"), ("other", "OTHER")):
            folder = os.path.join(self.tmp.name, uid)
            os.makedirs(os.path.join(folder, "charge_sessions"))
            with open(os.path.join(folder, "account.json"), "w") as f:
                json.dump({"vehicles": [{"vin": vin}]}, f)
            with open(os.path.join(folder, "charge_sessions", "202610.jsonl"), "w") as f:
                f.write(json.dumps({"start": 1000000, "end": 2000000, "kwh": 5}) + "\n")
        records = charging_history.read_sessions(self.tmp.name, "u", "VIN", 0)
        self.assertEqual(len(records), 1)

    def test_charge_log_recovery_requires_observed_end_and_caps_gaps(self):
        import hashlib
        tag = hashlib.sha256(b"VIN").hexdigest()[:8]
        records = [{"vin": "VIN", "time": "2026-10-29T21:" + t + "+08:00",
                    "data": {"DCChargingPower": {"doubleValue": p}}}
                   for t, p in (("00:00", 60), ("01:00", 60), ("02:00", 60), ("03:00", 0))]
        closed = recover_charge_history.recover(records, "2026-10-29", tag)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["kwh"], 3)
        self.assertTrue(closed[0]["estimated"])
        self.assertEqual(recover_charge_history.recover(records[:-1], "2026-10-29", tag), [])
        self.assertEqual(recover_charge_history.recover([records[0], records[-1]], "2026-10-29", tag), [])


if __name__ == "__main__":
    unittest.main()
