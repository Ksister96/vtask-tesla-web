import threading
import unittest
import weather_bridge as weather

class WeatherTests(unittest.TestCase):
    def setUp(self):
        weather._cache.clear()

    def test_nonblocking_lookup_deduplicates_and_returns_rain(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def provider(**coords):
            calls.append(coords); entered.set(); release.wait(2)
            return {'weather': '毛毛雨'}
        fields = {'latitude': 31.2, 'longitude': 121.5}
        try:
            self.assertEqual(weather.weather_for_fields(fields, provider)['status'], 'loading')
            self.assertTrue(entered.wait(1))
            self.assertEqual(weather.weather_for_fields(fields, provider)['status'], 'loading')
        finally:
            release.set()
        # Wait on the cache lock without external network or vehicle calls.
        for _ in range(100):
            with weather._lock:
                ready = not weather._cache[(31.2, 121.5)]['pending']
            if ready: break
            done.wait(.01)
        result = weather.weather_for_fields(fields, provider)
        self.assertEqual(result, {'status': 'ready', 'weather': '毛毛雨'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(weather.weather_for_fields({})['status'], 'no_location')

if __name__ == '__main__': unittest.main()
