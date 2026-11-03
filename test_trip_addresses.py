import unittest

from trip_addresses import needs_refresh, refresh_addresses


class TripAddressTest(unittest.TestCase):
    def test_placeholder_coordinate_and_district_only_need_refresh(self):
        for value in (None, "", "未知位置", "31.1234,121.5678", "宝山区"):
            self.assertTrue(needs_refresh(value), value)
        self.assertFalse(needs_refresh("宝山区友谊路街道同济路"))

    def test_repairs_both_addresses_from_coordinates(self):
        detail = {
            "start_addr": "未知位置", "start_coord": [31.1, 121.1],
            "end_addr": "", "end_coord": [31.2, 121.2],
        }
        calls = []

        def resolver(lat, lon):
            calls.append((lat, lon))
            return "宝山区友谊路" if lat == 31.1 else "杨浦区四平路"

        changed = refresh_addresses(detail, resolver)
        self.assertEqual(changed, ["start_addr", "end_addr"])
        self.assertEqual(detail["start_addr"], "宝山区友谊路")
        self.assertEqual(detail["end_addr"], "杨浦区四平路")
        self.assertEqual(calls, [(31.1, 121.1), (31.2, 121.2)])

    def test_keeps_good_or_unresolved_values(self):
        detail = {
            "start_addr": "宝山区友谊路", "start_coord": [31.1, 121.1],
            "end_addr": "未知位置", "end_coord": [31.2, 121.2],
        }
        changed = refresh_addresses(detail, lambda lat, lon: "")
        self.assertEqual(changed, [])
        self.assertEqual(detail["start_addr"], "宝山区友谊路")
        self.assertEqual(detail["end_addr"], "未知位置")


if __name__ == "__main__":
    unittest.main()
