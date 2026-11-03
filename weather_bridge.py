"""Nonblocking, location-scoped weather cache. Never calls a vehicle API."""
import math
import threading
import time

_lock = threading.Lock()
_cache = {}

def weather_for_fields(fields, provider=None, clock=time.time):
    try:
        lat, lon = float(fields['latitude']), float(fields['longitude'])
        if not math.isfinite(lat+lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return {'weather': '', 'status': 'no_location'}
    key = (round(lat, 2), round(lon, 2))
    now = clock()
    with _lock:
        entry = _cache.setdefault(key, {'weather': '', 'at': 0, 'retry': 0, 'pending': False})
        if not entry['pending'] and now >= entry['retry'] and (not entry['at'] or now-entry['at'] >= 600):
            entry['pending'] = True
            def fetch():
                try:
                    if provider is None:
                        import user_runner
                        data = user_runner._owner.get_weather(lat=lat, lon=lon)
                    else:
                        data = provider(lat=lat, lon=lon)
                    condition = str((data or {}).get('weather') or '')
                    with _lock:
                        if condition:
                            entry.update(weather=condition, at=clock())
                except Exception as error:
                    print('weather lookup failed: ' + type(error).__name__, flush=True)
                finally:
                    with _lock:
                        entry.update(pending=False, retry=clock()+60)
            threading.Thread(target=fetch, daemon=True).start()
        fresh = bool(entry['at'] and now-entry['at'] < 600)
        return {'weather': entry['weather'] if fresh else '',
                'status': 'ready' if fresh else 'loading' if entry['pending'] else 'unavailable'}
