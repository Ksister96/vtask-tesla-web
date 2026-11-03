import unittest

from state_observability import build_state_meta, epoch_seconds, uid_tag


class StateObservabilityTest(unittest.TestCase):
    def test_epoch_seconds_accepts_seconds_milliseconds_and_iso(self):
        self.assertEqual(epoch_seconds(1700000000), 1700000000)
        self.assertEqual(epoch_seconds(1700000000000), 1700000000)
        self.assertEqual(epoch_seconds(1700000000000000000), 1700000000)
        self.assertEqual(epoch_seconds("2023-11-14T22:13:20Z"), 1700000000)

    def test_meta_reports_each_stage_and_freshness(self):
        meta = build_state_meta(
            {"_vehicle_ts": 100, "_ingest_ts": 101},
            "telemetry", True, publish_ts=102, stale_after_seconds=30)
        self.assertEqual(meta["vehicle_ts"], 100000)
        self.assertEqual(meta["ingest_ts"], 101000)
        self.assertEqual(meta["publish_ts"], 102000)
        self.assertEqual(meta["age_ms"], 2000)
        self.assertFalse(meta["stale"])

    def test_old_or_last_known_state_is_stale(self):
        self.assertTrue(build_state_meta(
            {"_frame_ts": 10}, "telemetry", True,
            publish_ts=50, stale_after_seconds=30)["stale"])
        self.assertTrue(build_state_meta(
            {"_frame_ts": 49}, "last_state", False,
            publish_ts=50)["stale"])

    def test_missing_vehicle_time_remains_explicitly_missing(self):
        meta = build_state_meta(
            {"_vehicle_ts": None, "_frame_ts": 100, "_ingest_ts": 101},
            "telemetry", True, publish_ts=102)
        self.assertIsNone(meta["vehicle_ts"])
        self.assertEqual(meta["ingest_ts"], 101000)
        self.assertEqual(meta["age_ms"], 1000)

    def test_uid_tag_is_stable_and_does_not_expose_the_input(self):
        tag = uid_tag("user@example.com")
        self.assertEqual(tag, uid_tag("user@example.com"))
        self.assertEqual(len(tag), 12)
        self.assertNotIn("user", tag)


if __name__ == "__main__":
    unittest.main()
