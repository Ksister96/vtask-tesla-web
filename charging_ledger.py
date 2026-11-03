"""Mutable charging ledger metadata layered over immutable watcher sessions."""
import hashlib
import json
import os
import re
import threading
import time


_locks = {}
_locks_guard = threading.Lock()


def _lock(uid):
    with _locks_guard:
        return _locks.setdefault(str(uid), threading.Lock())


def _path(users, uid, name):
    return os.path.join(users, str(uid), name)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def session_id(vin, start):
    raw = "%s:%s" % (str(vin or "").strip().upper(), int(float(start or 0)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def station_key(station):
    poi = str((station or {}).get("poi_id") or "").strip()
    if poi:
        return "amap:" + poi
    lat, lon = (station or {}).get("lat"), (station or {}).get("lon")
    if lat is None or lon is None:
        return ""
    return "geo:%.3f,%.3f" % (float(lat), float(lon))


def save_station_match(users, uid, vin, start, station):
    sid = session_id(vin, start)
    key = station_key(station)
    with _lock(uid):
        path = _path(users, uid, "charge_ledger.json")
        ledger = _load(path)
        meta = dict(ledger.get(sid) or {})
        meta.update({
            "station_key": key,
            "station_name": str((station or {}).get("name") or "").strip()[:80],
            "station_address": str((station or {}).get("address") or "").strip()[:160],
            "station_distance_m": (station or {}).get("distance_m"),
            "station_lookup_at": int(time.time() * 1000),
        })
        places = _load(_path(users, uid, "charge_places.json"))
        remembered = places.get(key) if key else None
        if remembered and float(remembered.get("unit_price") or 0) > 0 and not meta.get("price_status"):
            meta["unit_price"] = round(float(remembered["unit_price"]), 4)
            meta["price_status"] = "remembered"
        ledger[sid] = meta
        _save(path, ledger)
    return meta


def mark_station_lookup_attempt(users, uid, vin, start):
    sid = session_id(vin, start)
    with _lock(uid):
        path = _path(users, uid, "charge_ledger.json")
        ledger = _load(path)
        meta = dict(ledger.get(sid) or {})
        meta["station_lookup_at"] = int(time.time() * 1000)
        ledger[sid] = meta
        _save(path, ledger)


def decorate_sessions(users, uid, vin, raw_sessions):
    ledger = _load(_path(users, uid, "charge_ledger.json"))
    out = []
    for raw in raw_sessions:
        q = dict(raw)
        sid = session_id(vin or q.get("vin"), q.get("start"))
        meta = dict(ledger.get(sid) or {})
        q.update(meta)
        q["id"] = sid
        q["price_status"] = q.get("price_status") or "pending"
        unit = q.get("unit_price")
        kwh = float(q.get("kwh") or 0)
        q["unit_price"] = round(float(unit), 4) if unit is not None else None
        q["total_cost"] = round(float(q.get("total_cost")), 2) if q.get("total_cost") is not None else (
            round(kwh * float(unit), 2) if unit is not None else None)
        out.append(q)
    return out


def confirm_cost(users, uid, vin, sid, total_cost=None, unit_price=None, remember=True):
    with _lock(uid):
        path = _path(users, uid, "charge_ledger.json")
        ledger = _load(path)
        if sid not in ledger:
            ledger[sid] = {}
        meta = dict(ledger[sid])
        kwh = float(meta.get("kwh") or 0)
        # kWh is supplied by caller after validating the immutable session.
        if total_cost is not None:
            total = float(total_cost)
            if not (0 <= total < 100000) or kwh <= 0:
                raise ValueError("费用或充电量无效")
            unit = total / kwh
        elif unit_price is not None:
            unit = float(unit_price)
            if not (0 < unit < 100):
                raise ValueError("单价无效")
            total = unit * kwh
        else:
            raise ValueError("缺少费用")
        meta.update({"unit_price": round(unit, 4), "total_cost": round(total, 2), "price_status": "confirmed"})
        meta.pop("kwh", None)
        ledger[sid] = meta
        _save(path, ledger)
        key = meta.get("station_key")
        if remember and key and total > 0:
            places_path = _path(users, uid, "charge_places.json")
            places = _load(places_path)
            places[key] = {"unit_price": round(unit, 4), "station_name": meta.get("station_name") or ""}
            _save(places_path, places)
        return meta


def confirm_session_cost(users, uid, vin, raw_sessions, sid, total_cost=None, unit_price=None, remember=True):
    target = next((q for q in raw_sessions if session_id(vin or q.get("vin"), q.get("start")) == sid), None)
    if not target:
        raise KeyError("未找到充电记录")
    with _lock(uid):
        path = _path(users, uid, "charge_ledger.json")
        ledger = _load(path)
        meta = dict(ledger.get(sid) or {})
        meta["kwh"] = float(target.get("kwh") or 0)
        ledger[sid] = meta
        _save(path, ledger)
    return confirm_cost(users, uid, vin, sid, total_cost, unit_price, remember)


def latest_pending(sessions):
    candidates = [q for q in sessions if q.get("price_status") in ("pending", "remembered")]
    return max(candidates, key=lambda q: q.get("end") or q.get("start") or 0) if candidates else None


def latest_card_candidate(sessions):
    """Prefer an actionable bill, otherwise return the latest confirmed bill for card migration."""
    pending = latest_pending(sessions)
    if pending is not None:
        return pending
    return max(sessions, key=lambda q: q.get("end") or q.get("start") or 0) if sessions else None


def mark_pending_notified(users, uid, sid):
    """Return True once for the rich-card prompt (separate from the old text prompt)."""
    with _lock(uid):
        path = _path(users, uid, "charge_ledger.json")
        ledger = _load(path)
        meta = dict(ledger.get(sid) or {})
        if meta.get("charge_card_v2_notified"):
            return False
        meta["charge_card_v2_notified"] = True
        ledger[sid] = meta
        _save(path, ledger)
        return True


_COST_RE = re.compile(r"(?:这次|本次|刚才|今天)?\s*(?:充电|电费|快充|超充)[^0-9]{0,12}([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|块)")


def parse_charge_cost_text(text):
    match = _COST_RE.search(str(text or ""))
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 < value < 100000 else None
