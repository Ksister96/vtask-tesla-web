# -*- coding: utf-8 -*-
"""get_weather(lat, lon) 跟车查询：open-meteo 主源、就近缓存，不打外网。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import run


def _meteo_body(code=51):
    return json.dumps({
        "current": {
            "temperature_2m": 18.2,
            "apparent_temperature": 17.4,
            "relative_humidity_2m": 80,
            "weather_code": code,
            "wind_speed_10m": 12.2,
        },
        "daily": {"temperature_2m_max": [21.2], "temperature_2m_min": [14.6]},
    }).encode()


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class GetWeatherLatLonTests(unittest.TestCase):
    def test_lat_lon_uses_open_meteo_and_nearby_cache(self):
        urls = []

        def fake_urlopen(req, timeout=None):
            urls.append(req.full_url)
            self.assertLessEqual(timeout, 5)
            return _Resp(_meteo_body())

        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "weather-cache.json")
            with mock.patch.object(run, "WEATHER_CACHE", cache), \
                    mock.patch.object(run.urllib.request, "urlopen", fake_urlopen):
                first = run.get_weather(lat=31.23, lon=121.47)
                second = run.get_weather(lat=31.25, lon=121.49)
                third = run.get_weather(lat=39.90, lon=116.40)
        self.assertEqual(first["weather"], "小毛毛雨")
        self.assertEqual(first["temp"], "18")
        self.assertEqual(second, first)
        self.assertEqual(third["weather"], "小毛毛雨")
        self.assertEqual(len(urls), 2)
        self.assertIn("latitude=31.23", urls[0])
        self.assertIn("longitude=121.47", urls[0])
        self.assertIn("timezone=Asia%2FShanghai", urls[0])
        self.assertIn("latitude=39.9", urls[1])

    def test_both_sources_fail_fast(self):
        def fake_urlopen(req, timeout=None):
            raise OSError("down")

        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "weather-cache.json")
            with mock.patch.object(run, "WEATHER_CACHE", cache), \
                    mock.patch.object(run.urllib.request, "urlopen", fake_urlopen):
                self.assertIsNone(run.get_weather(lat=31.2, lon=121.5))

    def test_chat_weather_follows_archive_coordinates(self):
        os.environ.setdefault("OWNER_BASE", os.path.dirname(os.path.abspath(__file__)))
        os.environ.setdefault("MULTI_BASE", os.path.dirname(os.path.abspath(__file__)))
        import user_runner as ur
        calls = []

        def fake_get_weather(city="Shanghai", lat=None, lon=None):
            calls.append((lat, lon))
            return {"weather": "小雨", "temp": "9", "feels": "7", "city": "上海", "humidity": "70"}

        original = ur._owner.get_weather
        original_archive = ur._vin_archive
        try:
            ur._owner.get_weather = fake_get_weather
            ur._vin_archive = lambda vin: {"latitude": 30.1234, "longitude": 120.5678}
            ur._WX_CITY_CACHE.clear()
            got = ur._weather_for_car("VIN1")
        finally:
            ur._owner.get_weather = original
            ur._vin_archive = original_archive
        self.assertEqual(calls, [(30.1234, 120.5678)])
        self.assertEqual(got["weather"], "小雨")


if __name__ == "__main__":
    unittest.main()
