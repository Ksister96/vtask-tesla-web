# -*- coding: utf-8 -*-
"""行程切段纯函数（v1，2026-10-05）。

输入轨迹点：{"ts": 毫秒, "lat": 纬度, "lon": 经度, "spd": km/h|None, "gear": "P/D/R/N"|None, "odo": 公里}
规则（docs/行驶回顾功能方案_20261005.md §4.2）：
- gear P(None 视同 P)↔D/R 切换切段；相邻点间隔 >20 分钟切段；跨天切段
- 行程 = 连续非 P 的点 ≥2 个；里程 = 末 odo − 首 odo（公里）
- downsample：等距抽稀到 ≤n 点，保留首末点
"""
from datetime import datetime


def split_trips(points, gap_min=20):
    pts = [p for p in points if p.get("ts") and p.get("lat") is not None and p.get("lon") is not None]
    pts.sort(key=lambda q: q["ts"])
    trips = []
    cur = []

    def flush():
        moving = [q for q in cur if q.get("gear") not in (None, "P")]
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
        elif prev.get("gear") in (None, "P") and p.get("gear") not in (None, "P"):
            flush()
            cur = [p]
        elif prev.get("gear") not in (None, "P") and p.get("gear") in (None, "P"):
            cur.append(p)
            flush()
            cur = []
        else:
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
    spds = [p.get("spd") for p in mv if p.get("spd") is not None]
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
        "max_kmh": round(max(spds), 1) if spds else None,
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
    if len(pwrs) >= 2:
        kwh_pos = kwh_neg = 0.0
        for a, b in zip(pwrs, pwrs[1:]):
            dt_h = min((b["ts"] - a["ts"]) / 3600000.0, 60 / 3600.0)
            if a["pwr"] > 0:
                kwh_pos += a["pwr"] * dt_h
            else:
                kwh_neg += -a["pwr"] * dt_h
        d["consumed_kwh"] = round(kwh_pos, 2)
        d["regen_kwh"] = round(kwh_neg, 2)
        if dist and dist > 0.3 and kwh_pos > 0.02:
            d["wh_per_km"] = round(max(0.0, kwh_pos - kwh_neg) * 1000 / dist)
        if kwh_pos > 0.02 and kwh_neg > 0.01:
            d["regen_rate"] = round(kwh_neg / kwh_pos * 100, 1)
    d["power_series"] = [[p["ts"], p.get("pwr")] for p in mv if p.get("pwr") is not None]
    return d
