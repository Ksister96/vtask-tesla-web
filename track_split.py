# -*- coding: utf-8 -*-
"""行程切段纯函数（v1，2026-10-05）。

输入轨迹点：{"ts": 毫秒, "lat": 纬度, "lon": 经度, "spd": km/h|None, "gear": "P/D/R/N"|None, "odo": 公里}
规则（docs/行驶回顾功能方案_20261005.md §4.2）：
- 明确 P 档停车超过3分钟或轨迹不连续才切段；未知档位不视作停车
- 相邻点间隔 >20 分钟切段；跨天切段
- 行程 = 连续非 P 的点 ≥2 个；里程 = 末 odo − 首 odo（公里）
- downsample：等距抽稀到 ≤n 点，保留首末点
"""
from datetime import datetime
import math
import statistics


ENERGY_SIGN_VERSION = 2


def can_resume(stop, point):
    """Resume only a short, spatially continuous stop; never join across midnight."""
    dt = point["ts"] - stop["ts"]
    if not 0 <= dt < 180000:
        return False
    if datetime.fromtimestamp(stop["ts"] / 1000).date() != datetime.fromtimestamp(point["ts"] / 1000).date():
        return False
    if stop.get("odo") is None or point.get("odo") is None or not 0 <= point["odo"] - stop["odo"] <= 0.5:
        return False
    try:
        dy = (point["lat"] - stop["lat"]) * 111320
        dx = (point["lon"] - stop["lon"]) * 111320 * math.cos(math.radians(stop["lat"]))
        return math.hypot(dx, dy) <= 500
    except (KeyError, TypeError):
        return False


def split_trips(points, gap_min=20):
    pts = [p for p in points if p.get("ts") and p.get("lat") is not None and p.get("lon") is not None]
    pts.sort(key=lambda q: q["ts"])
    trips = []
    cur = []
    stop = None

    def flush():
        moving = [q for q in cur if q.get("gear") in ("D", "R", "N") or (q.get("spd") or 0) > 1]
        if len(moving) < 2:
            return
        o0, o1 = moving[0].get("odo"), moving[-1].get("odo")
        km = round(max(0.0, o1 - o0), 1) if (o0 is not None and o1 is not None) else None
        trips.append({
            "t0": moving[0]["ts"], "t1": moving[-1]["ts"],
            "min": int(round((moving[-1]["ts"] - moving[0]["ts"]) / 60000)),
            "km": km,
            "start": {"lat": moving[0]["lat"], "lon": moving[0]["lon"]},
            "end": {"lat": moving[-1]["lat"], "lon": moving[-1]["lon"]},
            "n": len(moving),
            "pts": moving,
        })

    for p in pts:
        if not cur:
            cur = [p]
            continue
        prev = cur[-1]
        gap = (p["ts"] - prev["ts"]) > gap_min * 60000
        cross_day = (datetime.fromtimestamp(prev["ts"] / 1000).date()
                     != datetime.fromtimestamp(p["ts"] / 1000).date())
        if gap or cross_day:
            flush()
            cur = [p]
            stop = None
        else:
            moving = p.get("gear") in ("D", "R", "N") or (p.get("spd") or 0) > 1
            if moving and stop:
                if not can_resume(stop, p):
                    flush()
                    cur = []
                stop = None
            elif not moving and p.get("gear") == "P":
                stop = stop or p
            cur.append(p)
    flush()
    return trips


def downsample(pts, n=100):
    """等距抽稀到 ≤n 点，保留首末。pts: [[lon, lat], ...] 或任意列表。"""
    if len(pts) <= n:
        return list(pts)
    step = (len(pts) - 1) / (n - 1)
    out = [pts[round(i * step)] for i in range(n - 1)]
    out.append(pts[-1])
    return out


def apply_sampling_quality(detail, expected_interval_s=1.0):
    """Attach sampling quality while retaining the recorded speed peak.

    coverage counts at most two expected intervals per gap. Long gaps therefore
    lower the score instead of being mistaken for continuously observed time.
    """
    keys = ("sample_interval_median_s", "sample_interval_p95_s", "sample_gap_max_s",
            "sample_coverage_pct", "speed_sample_points", "power_sample_points",
            "max_kmh_sampled", "max_kmh_reliable", "max_kmh")
    before = {key: detail.get(key) for key in keys}
    speed = [point for point in (detail.get("speed_series") or [])
             if isinstance(point, (list, tuple)) and len(point) >= 2
             and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float))]
    power = [point for point in (detail.get("power_series") or [])
             if isinstance(point, (list, tuple)) and len(point) >= 2 and point[1] is not None]
    speed.sort(key=lambda point: point[0])
    gaps = [max(0.0, (speed[i][0] - speed[i - 1][0]) / 1000.0)
            for i in range(1, len(speed)) if speed[i][0] > speed[i - 1][0]]
    duration_s = (speed[-1][0] - speed[0][0]) / 1000.0 if len(speed) >= 2 else 0.0
    median_gap = statistics.median(gaps) if gaps else None
    ordered = sorted(gaps)
    p95_gap = ordered[int((len(ordered) - 1) * 0.95)] if ordered else None
    max_gap = max(gaps) if gaps else None
    observed_s = sum(min(gap, expected_interval_s * 2) for gap in gaps)
    coverage = min(100.0, observed_s / duration_s * 100) if duration_s > 0 else 0.0
    sampled_max = round(max((point[1] for point in speed)), 1) if speed else None
    reliable = bool(len(speed) >= 3 and coverage >= 80 and p95_gap is not None
                    and p95_gap <= expected_interval_s * 3)
    detail.update({
        "sample_interval_median_s": round(median_gap, 2) if median_gap is not None else None,
        "sample_interval_p95_s": round(p95_gap, 2) if p95_gap is not None else None,
        "sample_gap_max_s": round(max_gap, 2) if max_gap is not None else None,
        "sample_coverage_pct": round(coverage, 1),
        "speed_sample_points": len(speed),
        "power_sample_points": len(power),
        "max_kmh_sampled": sampled_max,
        "max_kmh_reliable": reliable,
        "max_kmh": sampled_max,
    })
    return any(before[key] != detail.get(key) for key in keys)


def _energy_metrics(power_series, distance_km):
    """按“正数=放电、负数=回收”的统一口径积分功率序列。"""
    kwh_pos = kwh_neg = 0.0
    for a, b in zip(power_series, power_series[1:]):
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
                and len(a) >= 2 and len(b) >= 2):
            continue
        try:
            dt_h = min(max(0.0, (float(b[0]) - float(a[0])) / 3600000.0), 60 / 3600.0)
            power = float(a[1])
        except (TypeError, ValueError):
            continue
        if power > 0:
            kwh_pos += power * dt_h
        else:
            kwh_neg += -power * dt_h
    out = {"consumed_kwh": round(kwh_pos, 2), "regen_kwh": round(kwh_neg, 2)}
    if distance_km and distance_km > 0.3 and kwh_pos > 0.02:
        out["wh_per_km"] = round(max(0.0, kwh_pos - kwh_neg) * 1000 / distance_km)
    if kwh_pos > 0.02 and kwh_neg > 0.01:
        out["regen_rate"] = round(kwh_neg / kwh_pos * 100, 1)
    return out


def repair_battery_power_detail(detail):
    """迁移旧行程：Tesla PackCurrent 负数是放电，旧版曾反向解释。"""
    if (not isinstance(detail, dict) or detail.get("power_source") != "battery"
            or int(detail.get("energy_sign_version") or 0) >= ENERGY_SIGN_VERSION):
        return False
    repaired = []
    for point in detail.get("power_series") or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2 or point[1] is None:
            continue
        try:
            repaired.append([point[0], -float(point[1])])
        except (TypeError, ValueError):
            continue
    if len(repaired) < 2:
        return False
    detail["power_series"] = repaired
    for key in ("consumed_kwh", "regen_kwh", "wh_per_km", "regen_rate"):
        detail.pop(key, None)
    detail.update(_energy_metrics(repaired, detail.get("distance_km")))
    detail["energy_sign_version"] = ENERGY_SIGN_VERSION
    apply_sampling_quality(detail)
    return True


def build_detail(mv, tid=None):
    """行驶点序列 → 行程详情 dict（track_watcher P档结算 与 trip_detail 即时补算 共用，字段必须一致 2026-10-11）。
    mv: 已过滤行驶点（gear D/R/N 或 spd>1），≥2 点。
    能耗口径对齐车机：wh_per_km=净消耗(放电−回收)/里程；consumed_kwh=放电毛量；
    采样缺口>60s 按 60s 计（防漏采把功率按长时间积分高估）；
    短途阈值放宽到 0.02kWh（原 0.1 曾把 3km 内短途的能耗/回收率全挡成 --）。"""
    import time as _t
    o0, o1 = mv[0].get("odo"), mv[-1].get("odo")
    dist = round(o1 - o0, 2) if (o0 is not None and o1 is not None) else None
    dur_s = (mv[-1]["ts"] - mv[0]["ts"]) / 1000.0
    t0 = _t.localtime(mv[0]["ts"] / 1000)
    t1 = _t.localtime(mv[-1]["ts"] / 1000)
    d = {
        "id": tid or _t.strftime("%Y%m%d-%H%M%S", t0),
        "date": _t.strftime("%Y-%m-%d", t0),
        "start_time": _t.strftime("%H:%M:%S", t0),
        "end_time": _t.strftime("%H:%M:%S", t1),
        "duration_min": round(dur_s / 60),
        "distance_km": dist,
        "avg_kmh": round(dist / (dur_s / 3600.0), 1) if (dist and dur_s > 60) else None,
        "start_coord": [mv[0]["lat"], mv[0]["lon"]],
        "end_coord": [mv[-1]["lat"], mv[-1]["lon"]],
        "speed_series": [[p["ts"], p.get("spd")] for p in mv if p.get("spd") is not None],
        "route": [[p["lat"], p["lon"]] for p in mv],
    }
    bats = [p.get("bat") for p in mv if p.get("bat") is not None]
    rngs = [p.get("rng") for p in mv if p.get("rng") is not None]
    tins = [p.get("tin") for p in mv if p.get("tin") is not None]
    touts = [p.get("tout") for p in mv if p.get("tout") is not None]
    if len(bats) >= 2:
        d["battery_change"] = [bats[0], bats[-1]]
    if len(rngs) >= 2:
        d["range_change_km"] = round(rngs[-1] - rngs[0], 1)
        if dist and dist > 0.3 and rngs[0] - rngs[-1] > 0.5:
            d["range_consumed_km"] = round(rngs[0] - rngs[-1], 1)
            d["range_efficiency"] = round(dist / (rngs[0] - rngs[-1]) * 100, 1)
    if tins:
        d["inside_temp_avg"] = round(sum(tins) / len(tins), 1)
    if touts:
        d["outside_temp_avg"] = round(sum(touts) / len(touts), 1)
    pwrs = [p for p in mv if p.get("pwr") is not None]
    d["power_series"] = [[p["ts"], -p["pwr"] if p.get("power_source") == "battery" else p["pwr"]]
                         for p in pwrs]
    if len(d["power_series"]) >= 2:
        d.update(_energy_metrics(d["power_series"], dist))
    if any(p.get("power_source") == "battery" for p in pwrs):
        d["power_source"] = "battery"
        d["energy_sign_version"] = ENERGY_SIGN_VERSION
    apply_sampling_quality(d)
    return d
