# -*- coding: utf-8 -*-
"""行驶轨迹采集守护（v1，2026-10-05）。方案：docs/行驶回顾功能方案_20261005.md

用法：python track_watcher_multi.py [间隔秒，默认 60]

铁律（方案 §6）：
- 绝不调 wake_up——只在车辆已在线时取点（vehicle_data 对睡眠车返回 408 且不唤醒，天然安全）
- 每用户每轮至多 1 次 vehicle_data?endpoints=drive_state;vehicle_state（限速 60 次/分/车，余量巨大）
- token 每轮重读（轮换不失效）；点存 users/{uid}/tracks/YYYYMM.jsonl
- 日志：BASE/track_watch.log；单实例：BASE/track_watch.pid
"""
import json
import os
import shutil
import threading
import ssl
import sys
import time
import urllib.error
import urllib.request

import track_split

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
USERS = os.path.join(BASE, "users")
FLEET = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"
CTX = ssl.create_default_context()          # 特斯拉域名严格校验
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with open(os.path.join(BASE, "track_watch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def http_json(url, tok, timeout=20):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def user_ids():
    try:
        return [u for u in sorted(os.listdir(USERS))
                if os.path.isdir(os.path.join(USERS, u)) and not u.startswith("_")]
    except Exception:
        return []


def vid_of(uid):
    try:
        acct = json.load(open(os.path.join(USERS, uid, "account.json"), encoding="utf-8-sig"))
        return (acct.get("vehicles") or [{}])[0].get("id")
    except Exception:
        return None


def tok_of(uid):
    try:
        return json.load(open(os.path.join(USERS, uid, "tokens.json"),
                              encoding="utf-8-sig")).get("access_token")
    except Exception:
        return None


def tracks_file(uid):
    d = os.path.join(USERS, uid, "tracks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, time.strftime("%Y%m") + ".jsonl")


def last_ts(uid):
    """当日最后一点的时间戳，防重复写。"""
    p = tracks_file(uid)
    if not os.path.exists(p):
        return 0
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 600))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
            if lines:
                return json.loads(lines[-1]).get("ts", 0)
    except Exception:
        pass
    return 0


_bad_until = {}   # uid -> 冷却截止（401/403 账号 24h 内不再轮询，防日志刷屏）
_last_moving = {}   # uid -> 上一轮是否行驶中（用于补行程收尾点）
_trip_cur = {}      # uid -> 当前行驶段轨迹点（P 档收尾时结算）


def poll_user(uid):
    if _bad_until.get(uid, 0) > time.time():
        return
    vid = vid_of(uid)
    tok = tok_of(uid)
    if not vid or not tok:
        return
    try:
        # endpoints 串 = 主应用同款（分号必须 %3B 编码：2026-10-07 实测 cn 网关对裸分号
        # 只下发 location_data，drive_state/vehicle_state 段全被吞 → gear/odo/速度全空）
        d = http_json(f"{FLEET}/api/1/vehicles/{vid}/vehicle_data?endpoints=location_data%3Bcharge_state%3Bdrive_state%3Bvehicle_state%3Bclimate_state%3Bgui_settings%3Bvehicle_config", tok)
    except urllib.error.HTTPError as e:
        if e.code in (408, 504):
            return  # 车睡着：vehicle_data 不唤醒，静默跳过（零成本）
        if e.code in (401, 403):
            # 2026-10-07 从 24h 降到 10min：主应用会刷新 token 落盘，本进程每轮重读
            # tokens.json，10 分钟后即用新 token 恢复。24h 冷却曾致整程轨迹漏采
            # （2026-10-07 早高峰用户开车时处于冷却期，路线全空）。
            _bad_until[uid] = time.time() + 600
            log(f"{uid} token 无效({e.code})，冷却10min等新token")
            return
        log(f"{uid} HTTP {e.code}")
        return
    except Exception as e:
        log(f"{uid} 网络/证书错误: {e}")
        return
    resp = (d or {}).get("response") or {}
    ds = resp.get("drive_state") or {}
    vs = resp.get("vehicle_state") or {}
    lat, lon = ds.get("latitude"), ds.get("longitude")
    ld = resp.get("location_data") or {}
    if lat is None:
        loc = ld.get("location") if isinstance(ld, dict) else None
        if loc:
            lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is None or lon is None:
        return
    # 行驶中刷新 VIN 档案（定位+胎压）：行车中 /api/state 走遥测帧（遥测无 Location、
    # cn 固件不推 tpms 值），本采集器是唯一拿得到新数据的通道——不刷档案，
    # 仪表盘位置条冻结在原地、胎压只剩旧值（2026-10-07 用户行车实测）
    try:
        acct = json.load(open(os.path.join(USERS, uid, "account.json"), encoding="utf-8-sig"))
        vin = ((acct.get("vehicles") or [{}])[0].get("vin") or "").strip()
        if vin:
            ap = os.path.join(USERS, "vehicles", vin + ".json")
            try:
                arch = json.load(open(ap, encoding="utf-8"))
            except Exception:
                arch = {}
            arch["latitude"] = round(float(lat), 6)
            arch["longitude"] = round(float(lon), 6)
            arch["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            tp = vs.get("tire_pressure") or {}
            if not tp:
                fl, fr = vs.get("tpms_pressure_fl"), vs.get("tpms_pressure_fr")
                rl, rr = vs.get("tpms_pressure_rl"), vs.get("tpms_pressure_rr")
                if not (fl is None and fr is None and rl is None and rr is None):
                    tp = {"fl": fl, "fr": fr, "rl": rl, "rr": rr}
            if tp:
                arch["tire_pressure"] = tp
            tmp = ap + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(arch, f, ensure_ascii=False, indent=1)
            os.replace(tmp, ap)
    except Exception:
        pass
    pt = {"ts": int(ds.get("timestamp") or time.time() * 1000),
          "lat": round(float(lat), 6), "lon": round(float(lon), 6),
          "spd": ds.get("speed"), "gear": ds.get("shift_state"),
          "odo": round((vs.get("odometer") or 0) * 1.609344, 2)}  # 英里→公里
    ds2 = resp.get("drive_state") or {}
    cs = resp.get("charge_state") or {}
    cl = resp.get("climate_state") or {}
    if ds2.get("power") is not None:
        pt["pwr"] = round(float(ds2.get("power")), 1)   # kW：行驶为正/动能回收为负
    if cs.get("battery_level") is not None:
        pt["bat"] = round(float(cs.get("battery_level")), 2)
    if cs.get("battery_range") is not None:
        pt["rng"] = round(float(cs.get("battery_range")) * 1.609344, 1)   # 英里→公里
    if cl.get("inside_temp") is not None:
        pt["tin"] = round(float(cl.get("inside_temp")), 1)
    if cl.get("outside_temp") is not None:
        pt["tout"] = round(float(cl.get("outside_temp")), 1)
    moving = (ds.get("shift_state") in ("D", "R", "N")) or ((ds.get("speed") or 0) > 1)
    if not moving and not _last_moving.get(uid):
        return   # 驻车中且上一轮也非行驶：不写点（否则充电一晚上写上千个重复点），行程分段不受影响
    _last_moving[uid] = moving   # 行驶→驻车的第一个静止点要写（收尾行程），其余驻车点跳过
    if pt["ts"] <= last_ts(uid):
        return  # 重复点不写
    with open(tracks_file(uid), "a", encoding="utf-8") as f:
        f.write(json.dumps(pt, ensure_ascii=False) + "\n")
    log(f"{uid} +点 gear={pt['gear']} odo={pt['odo']}km")

    # 行程缓冲：行驶中累积，P 档收尾结算为行程详情
    if moving:
        _trip_cur.setdefault(uid, []).append(pt)
    else:
        seg = _trip_cur.pop(uid, None)
        if seg and len(seg) >= 2:
            seg.append(pt)
            threading.Thread(target=settle_trip, args=(uid, seg), daemon=True).start()
    vc = resp.get("vehicle_config") or {}
    ec = vc.get("exterior_color")
    if ec:
        mp = os.path.join(USERS, uid, "vehicle_meta.json")
        try:
            json.dump({"exterior_color": ec, "ts": int(time.time() * 1000)}, open(mp, "w", encoding="utf-8"))
        except Exception:
            pass
    return bool(moving)   # 主循环据此在行驶中加密采样间隔


def regeo(lat, lon):
    """高德逆地理：坐标 → 地址。失败返回 None。"""
    key = ""
    try:
        key = open(os.path.join(BASE, "amap_key.txt"), encoding="utf-8-sig").read().strip()
    except Exception:
        pass
    if not key:
        return None
    try:
        url = "https://restapi.amap.com/v3/geocode/regeo?location=%s,%s&key=%s" % (lon, lat, key)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        if str(d.get("status")) == "1":
            return ((d.get("regeocode") or {}).get("formatted_address") or None)
    except Exception:
        pass
    return None


def settle_trip(uid, seg):
    """P 档收尾：把当前行驶段结算为行程详情（构建器与 trip_detail 即时补算共用 track_split.build_detail）。"""
    moving = [p for p in seg if p.get("gear") in ("D", "R", "N") or (p.get("spd") or 0) > 1]
    if len(moving) < 2:
        return
    o0, o1 = moving[0].get("odo"), moving[-1].get("odo")
    _dist = round(o1 - o0, 2) if (o0 is not None and o1 is not None) else None
    _dur_s = (moving[-1]["ts"] - moving[0]["ts"]) / 1000.0
    if not _dist or _dist < 0.3 or _dur_s < 120:
        return   # 挪车/极短移动不算行程
    detail = track_split.build_detail(moving)
    # 起终点逆地理
    for tag, p in (("start", moving[0]), ("end", moving[-1])):
        addr = regeo(p["lat"], p["lon"])
        if addr:
            detail[tag + "_addr"] = addr
    ddir = os.path.join(USERS, uid, "trip_details")
    os.makedirs(ddir, exist_ok=True)
    fp = os.path.join(ddir, detail["id"] + ".json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False)
    # 测试页副本（公开静态）
    try:
        tdir = os.path.join(BASE, "static", "test")
        os.makedirs(tdir, exist_ok=True)
        shutil.copy(fp, os.path.join(tdir, "trip_detail_latest.json"))
    except Exception:
        pass
    log("%s 行程结算 %s %.1fkm %d分钟" % (uid, detail["id"], _dist, _dur_s / 60))


def cleanup_old():
    """删两个月前的轨迹月文件（当月+上月保留），每天清一次即可。"""
    keep = {time.strftime("%Y%m"), time.strftime("%Y%m", time.localtime(time.time() - 30 * 86400))}
    for uid in user_ids():
        d = os.path.join(USERS, uid, "tracks")
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".jsonl") and fn[:6].isdigit() and fn[:6] not in keep:
                try:
                    os.remove(os.path.join(d, fn))
                    log(f"{uid} 清理旧轨迹 {fn}")
                except Exception:
                    pass


def main():
    pidfile = os.path.join(BASE, "track_watch.pid")
    try:
        old = open(pidfile).read().strip() if os.path.exists(pidfile) else ""
        if old.isdigit() and int(old) != os.getpid():
            os.kill(int(old), 0)  # 探活
            print("已有采集进程在跑(pid %s)，退出" % old)
            return
    except Exception:
        pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    log(f"watcher start interval={INTERVAL}s pid={os.getpid()} base={BASE}")
    last_clean = ""
    while True:
        try:
            today = time.strftime("%Y%m%d")
            if today != last_clean:
                cleanup_old()
                last_clean = today
            any_moving = False
            for uid in user_ids():
                if poll_user(uid):
                    any_moving = True
        except Exception as e:
            log("loop err: %s" % e)
        # 行驶中加密到 20s（路线更顺滑）；驻车 60s 基线（API 限速 60次/分/车，配额与顺滑度平衡）
        time.sleep(10 if any_moving else INTERVAL)


if __name__ == "__main__":
    main()
