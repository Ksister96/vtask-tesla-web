"""Parking estimates inferred from consecutive trips; user-confirmed costs are authoritative."""
import glob
import hashlib
import json
import math
import os
import threading
import time
import uuid
import statistics
from datetime import datetime

_lock = threading.RLock()

def load(path):
    try:
        with open(path, encoding='utf-8') as source:
            return json.load(source)
    except (OSError, ValueError):
        return {}

def distance(a, b):
    if not a or not b or len(a) != 2 or len(b) != 2:
        return float('inf')
    lat = math.radians((float(a[0]) + float(b[0])) / 2)
    return math.hypot((float(a[0])-float(b[0]))*111320,
                      (float(a[1])-float(b[1]))*111320*math.cos(lat))

def sessions(users, uid):
    folder = os.path.join(users, uid)
    trips = []
    for path in sorted(glob.glob(os.path.join(folder, 'trip_details', '*.json'))):
        d = load(path)
        try:
            start = datetime.strptime(d['id'], '%Y%m%d-%H%M%S').timestamp()
            series = d.get('speed_series') or []
            end = float(series[-1][0])/1000 if series else start + float(d['duration_min'])*60
            trips.append((start, end, d))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    ledger = load(os.path.join(folder, 'parking_ledger.json'))
    records, places = ledger.get('records', {}), ledger.get('places', {})
    out = []
    pairs = list(zip(trips, trips[1:]))
    if trips:
        pairs.append((trips[-1], (time.time(), 0, {'id': 'ongoing', 'start_coord': trips[-1][2].get('end_coord')})))
    for prev, nxt in pairs:
        start, end = prev[1], nxt[0]
        coord = prev[2].get('end_coord')
        if end-start < 300 or distance(coord, nxt[2].get('start_coord')) > 150:
            continue
        sid = hashlib.sha256((prev[2]['id']+':'+nxt[2]['id']).encode()).hexdigest()[:20]
        meta = records.get(sid, {})
        matches = [(distance(coord, p.get('coord')), key, p) for key, p in places.items()]
        matches.sort(key=lambda p: p[0])
        match = min(matches, default=(float('inf'), '', {}), key=lambda p: p[0])
        ambiguous = len(matches) > 1 and matches[1][0] < 80 and matches[1][0] - match[0] < 30
        key, place = (match[1], match[2]) if match[0] <= 80 and not ambiguous else ('', {})
        if meta.get('separate'):
            key, place = sid, places.get(sid, {})
        start, end = meta.get('start', start*1000)/1000, meta.get('end', end*1000)/1000
        home = bool(place.get('home'))
        cost = meta.get('cost')
        samples = [r['rate'] for r in records.values() if r.get('place_key') == key and r.get('rate', 0) > 0]
        rate = statistics.median(samples) if samples else place.get('rate')
        ongoing = nxt[2]['id'] == 'ongoing'
        uncertain = end-start > 48*3600 or ambiguous
        out.append(dict(id=sid, start=start*1000, end=end*1000,
                        hours=round((end-start)/3600, 4), coord=coord,
                        place_key=key, name=meta.get('name') or prev[2].get('end_addr') or '未知停车地点',
                        home=home, cost=cost, ongoing=ongoing, uncertain=uncertain,
                        rate=rate, estimate=None if home or ongoing or uncertain or cost is not None or rate is None
                        else round(rate*(end-start)/3600, 2)))
    out.extend(dict(r, id=sid, manual=True, home=False, ongoing=False, uncertain=False,
                    hours=(r['end']-r['start'])/3600000, estimate=None)
               for sid, r in records.items() if r.get('manual'))
    return sorted(out, key=lambda s: s['start'], reverse=True)

def update(users, uid, sid, cost=None, home=None, name=None, start=None, end=None, remember=True, separate=False):
    with _lock:
        path = os.path.join(users, uid, 'parking_ledger.json')
        ledger = load(path)
        records, places = ledger.setdefault('records', {}), ledger.setdefault('places', {})
        if not sid:
            sid = 'manual-' + uuid.uuid4().hex[:20]
            records[sid] = {'manual': True, 'name': '手动停车', 'start': 0, 'end': 0}
        session = next((s for s in sessions(users, uid) if s['id'] == sid), None)
        if not session and not records.get(sid, {}).get('manual'):
            raise ValueError('停车记录不存在')
        session = session or {'start': 0, 'end': 0, 'place_key': '', 'coord': None, 'ongoing': False}
        if session.get('ongoing') and cost is not None:
            raise ValueError('停留尚未结束，请用手动补记填写已完成的停车')
        meta = records.setdefault(sid, {})
        if name is not None:
            if not str(name).strip():
                raise ValueError('请填写停车地点')
            meta['name'] = str(name).strip()[:100]
        a, b = float(start if start is not None else session['start']), float(end if end is not None else session['end'])
        if not math.isfinite(a+b) or a <= 0 or b <= a or b > time.time()*1000+60000:
            raise ValueError('请检查停车起止时间')
        if start is not None or end is not None or cost is not None:
            meta.update(start=a, end=b)
        if separate:
            meta['separate'] = True
        key = sid if meta.get('separate') else session.get('place_key') or sid
        place = places.setdefault(key, {'coord': session.get('coord')})
        if home is not None:
            if not isinstance(home, bool):
                raise ValueError('家中标记无效')
            place['home'] = home
        if cost is not None:
            amount = float(cost)
            if not math.isfinite(amount) or not 0 <= amount <= 100000:
                raise ValueError('请输入有效停车费')
            meta['cost'] = round(amount, 2)
            meta.pop('rate', None)
            meta.pop('place_key', None)
            if amount > 0 and remember and b-a <= 48*3600000 and not place.get('home') and session.get('coord'):
                meta.update(place_key=key, rate=round(amount/((b-a)/3600000), 6))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path+'.tmp', 'w', encoding='utf-8') as target:
            json.dump(ledger, target, ensure_ascii=False)
        os.replace(path+'.tmp', path)
