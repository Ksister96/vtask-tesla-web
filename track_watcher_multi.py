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
import re
import shutil
import threading
import ssl
import sys
import time
import urllib.error
import urllib.request

import track_split
from vehicle_measurements import normalize as normalize_measurements

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
TELEMETRY_DIR = os.environ.get("TELEMETRY_STATE_DIR", "/opt/tesla-fleet/telemetry_states")
USERS = os.path.join(BASE, "users")
FLEET = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"
CTX = ssl.create_default_context()          # 特斯拉域名严格校验
INTERVAL = int(sys.argv[1]) if len(sys.argv) > 1 else 60
MOVING_INTERVAL = max(0.5, float(os.environ.get("TRACK_MOVING_INTERVAL", "1")))


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    try:
        with open(os.path.join(BASE, "track_watch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def sys_msg(uid, text, card=None):
    """系统消息：写入用户聊天流（who=sys，2026-10-23 聊天框通知中心）。
    与主应用 append_chat 跨进程并发，靠 .chat.lock flock 互斥防原子替换互踩。"""
    import fcntl
    text = str(text or "").strip()
    if not text:
        return
    cp = os.path.join(USERS, uid, "chat.json")
    try:
        with open(os.path.join(USERS, uid, ".chat.lock"), "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                try:
                    messages = json.load(open(cp, encoding="utf-8"))
                    if not isinstance(messages, list):
                        messages = []
                except Exception:
                    messages = []
                message = {"who": "sys", "text": text,
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                if (isinstance(card, dict) and card.get("type") == "trip_detail"
                        and re.fullmatch(r"\d{8}-\d{6}", str(card.get("trip_id") or ""))):
                    message["card"] = {"type": "trip_detail", "trip_id": str(card["trip_id"])}
                messages.append(message)
                messages = messages[-100:]   # 与主应用 CHAT_MAX 对齐
                tmp = cp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(messages, f, ensure_ascii=False, indent=1)
                os.replace(tmp, cp)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as e:
        log(f"{uid} sys_msg err: {e}")


def charge_watch(uid, cs):
    """充电完成 / 低电量 → 聊天流系统消息（2026-10-23）。
    状态存 users/<uid>/charge_watch.json 跨轮记忆；泊车驻车轮次也会走到这里
    （poll_user 在驻车早退之前调用本函数，充电过夜同样能感知）。"""
    try:
        cur = (cs or {}).get("charging_state") or ""
        soc = (cs or {}).get("battery_level")
        pf = os.path.join(USERS, uid, "charge_watch.json")
        try:
            prev = json.load(open(pf, encoding="utf-8"))
            if not isinstance(prev, dict):
                prev = {}
        except Exception:
            prev = {}
        pstate = prev.get("state") or ""
        today = time.strftime("%Y%m%d")
        msg = None
        new = {"state": cur, "since": prev.get("since"), "low_day": prev.get("low_day") or ""}
        if cur == "Charging" and pstate != "Charging":
            new["since"] = time.time()
        if pstate == "Charging" and cur == "Complete":
            m = int(max(0, time.time() - (new.get("since") or time.time())) // 60)
            dur = f"，用时 {m // 60}小时{m % 60:02d}分" if m >= 60 else (f"，用时 {m} 分钟" if m > 0 else "")
            rng = (cs or {}).get("battery_range")
            rng_s = f"，续航约 {round(float(rng) * 1.609344)} km" if rng else ""
            msg = f"🔌 充电完成：当前 {soc}%{rng_s}{dur}"
        if soc is not None and cur != "Charging" and soc < 20 and new["low_day"] != today:
            msg = f"🔋 电量只剩 {soc}%，记得找桩充电"
            new["low_day"] = today
        if new != prev:
            tmp = pf + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False)
            os.replace(tmp, pf)
        if msg:
            sys_msg(uid, msg)
            log(f"{uid} 车况提醒: {msg}")
    except Exception as e:
        log(f"{uid} charge_watch err: {e}")


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


def vin_of(uid):
    try:
        with open(os.path.join(USERS, uid, "account.json"), encoding="utf-8-sig") as f:
            acct = json.load(f)
        return ((acct.get("vehicles") or [{}])[0].get("vin") or "").strip()
    except Exception:
        return ""


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
_charge_sess = {}   # uid -> 进行中充电会话（服务端记账：小程序关着也不漏）
_trip_stop = {}     # uid -> first confirmed stationary P point
_trip_loaded = set()
_trip_vins = {}


def _trip_checkpoint(uid):
    path = os.path.join(USERS, uid, "trip_active.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"vin": vin_of(uid), "points": _trip_cur.get(uid, []), "stop": _trip_stop.get(uid)}, f)
    os.replace(path + ".tmp", path)


def _restore_trip(uid):
    vin = vin_of(uid)
    if uid in _trip_loaded and _trip_vins.get(uid) == vin:
        return
    if uid in _trip_loaded:
        _trip_cur.pop(uid, None)
        _trip_stop.pop(uid, None)
        _last_moving.pop(uid, None)
    _trip_loaded.add(uid)
    _trip_vins[uid] = vin
    try:
        with open(os.path.join(USERS, uid, "trip_active.json"), encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("vin") == vin_of(uid) and saved.get("points"):
            _trip_cur[uid] = saved["points"]
            _last_moving[uid] = True
            if saved.get("stop"):
                _trip_stop[uid] = saved["stop"]
    except (OSError, ValueError):
        pass


def _charge_checkpoint(uid, sess):
    path = os.path.join(USERS, uid, "charge_active.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"vin": vin_of(uid), "session": sess}, f)
    os.replace(path + ".tmp", path)


def _track_trip(uid, pt):
    moving = pt.get("gear") in ("D", "R", "N") or (pt.get("spd") or 0) > 1
    stop = _trip_stop.get(uid)
    if moving:
        previous = (_trip_cur.get(uid) or [None])[-1]
        if previous and (pt["ts"] - previous["ts"] > 1200000 or
                         time.strftime("%Y%m%d", time.localtime(pt["ts"] / 1000)) !=
                         time.strftime("%Y%m%d", time.localtime(previous["ts"] / 1000))):
            _finish_trip(uid)
        if stop and not track_split.can_resume(stop, pt):
            _finish_trip(uid)
        _trip_stop.pop(uid, None)
        _trip_cur.setdefault(uid, []).append(pt)
    elif pt.get("gear") == "P" and uid in _trip_cur:
        _trip_stop.setdefault(uid, pt)
    _trip_checkpoint(uid)
    # Missing gear is an unknown observation, never a parking event.


def _finish_trip(uid):
    seg = _trip_cur.pop(uid, None)
    _trip_stop.pop(uid, None)
    _trip_checkpoint(uid)
    if seg and len(seg) >= 2:
        threading.Thread(target=settle_trip, args=(uid, seg), daemon=True).start()
_loc_ok = {}        # uid -> 该授权是否含 vehicle_location scope;False=用降级端点(去 location_data)
_rest_try = {}      # uid -> 上次 REST 兜底尝试时间(节流用)
_rest_ok = {}       # uid -> 上次 REST 兜底成功时间(行驶期快采/驻车期慢采的依据)
_EP_FULL = "location_data%3Bcharge_state%3Bdrive_state%3Bvehicle_state%3Bclimate_state%3Bgui_settings%3Bvehicle_config"
_EP_NOLOC = "charge_state%3Bdrive_state%3Bvehicle_state%3Bclimate_state%3Bgui_settings%3Bvehicle_config"


def _settle_charge(uid, sess, end_ts):
    """充电结束落账：≥2分钟且≥0.1度才记（与小程序本地阈值一致）。"""
    dur = sess["last"] - sess["start"]
    if dur < 120000 or sess["kwh"] < 0.1:
        return
    rec = {"start": sess["start"], "end": end_ts, "kwh": round(sess["kwh"], 2),
           "soc_start": sess.get("soc0"), "soc_end": sess.get("soc"),
           "max_power": round(sess["max_pwr"], 1), "vin": vin_of(uid),
           "estimated": sess.get("estimated", False)}
    if sess.get("lat") is not None and sess.get("lon") is not None:
        rec["latitude"] = round(float(sess["lat"]), 6)
        rec["longitude"] = round(float(sess["lon"]), 6)
    rec["max_ac_power"] = round(float(sess.get("max_ac_pwr") or 0), 1)
    rec["max_dc_power"] = round(float(sess.get("max_dc_pwr") or 0), 1)
    rec["charge_type"] = "dc_fast" if rec["max_dc_power"] > 1 or sess.get("fast_charger_present") else "ac"
    vin = vin_of(uid).upper()
    d = os.path.join(USERS, "vehicles", vin, "charge_sessions") if vin and vin.isalnum() else os.path.join(USERS, uid, "charge_sessions")
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, time.strftime("%Y%m", time.localtime(end_ts / 1000)) + ".jsonl")
    seen = False   # 同 start 去重（进程重启可能重复开段）
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    try:
                        if abs(json.loads(line).get("start", 0) - rec["start"]) < 60000:
                            seen = True
                            break
                    except Exception:
                        pass
        except Exception:
            pass
    if not seen:
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("%s 充电结算 %.2fkWh %.1f小时 峰值%skW" % (uid, rec["kwh"], dur / 3600000.0, rec["max_power"]))


def _track_charging(uid, cs):
    """充电会话记账（2026-10-26 移植到遥测版）：Charging/Starting 累积，结束/断档超30分钟落账。
    遥测口径无 charge_energy_added，cs["charger_power"]（ac/dc 取大者）按 功率×时间 积分。"""
    st = cs.get("charging_state")
    sess = _charge_sess.get(uid)
    if sess and sess.get("vin", vin_of(uid)) != vin_of(uid):
        _charge_sess.pop(uid, None)
        sess = None
    if sess is None:
        try:
            with open(os.path.join(USERS, uid, "charge_active.json"), encoding="utf-8") as f:
                saved = json.load(f)
            if saved.get("vin") == vin_of(uid):
                sess = saved.get("session")
                if sess:
                    _charge_sess[uid] = sess
        except (OSError, ValueError):
            pass
    now = int(cs.get("sample_ts") or time.time() * 1000)
    if sess and now <= sess["last"]:
        return
    ended = str(st).lower() in ("complete", "disconnected", "stopped", "nopower")
    if not ended and (cs.get("charging") is True or st in ("Charging", "Starting") or (cs.get("charger_power") or 0) > 1):
        if sess and now - sess["last"] > 1800000:
            _settle_charge(uid, sess, sess["last"])
            sess = None
        if not sess:
            sess = _charge_sess[uid] = {"start": now, "base": cs.get("charge_energy_added"),
                                        "kwh": 0.0, "last": now, "max_pwr": 0.0,
                                        "max_ac_pwr": 0.0, "max_dc_pwr": 0.0,
                                        "soc0": cs.get("battery_level"), "vin": vin_of(uid)}
        added = cs.get("charge_energy_added")
        pwr = cs.get("charger_power") or 0
        if added is not None:
            # Tesla's counter already covers this charging session, including
            # energy delivered before our first observation or process restart.
            sess["kwh"] = max(0.0, float(added))
            sess["estimated"] = False
        elif pwr > 0:
            sess["kwh"] += pwr * min(120000, max(0, now - sess["last"])) / 3600000.0
            sess["estimated"] = True
        sess["last"] = now
        sess["max_pwr"] = max(sess["max_pwr"], pwr)
        sess["max_ac_pwr"] = max(float(sess.get("max_ac_pwr") or 0), float(cs.get("ac_charging_power") or 0))
        sess["max_dc_pwr"] = max(float(sess.get("max_dc_pwr") or 0), float(cs.get("dc_charging_power") or 0))
        if cs.get("latitude") is not None and cs.get("longitude") is not None:
            sess["lat"], sess["lon"] = cs["latitude"], cs["longitude"]
        if cs.get("fast_charger_present"):
            sess["fast_charger_present"] = True
        sess["soc"] = cs.get("battery_level")
        _charge_checkpoint(uid, sess)
    elif sess:   # Complete/Disconnected/Stopped…（含车睡过头、下次唤醒才看到）一律结算
        _charge_sess.pop(uid, None)
        _settle_charge(uid, sess, sess["last"])
        _charge_checkpoint(uid, None)


def poll_user(uid):
    _restore_trip(uid)
    stop = _trip_stop.get(uid)
    if stop and time.time() * 1000 - stop["ts"] >= 180000:
        _finish_trip(uid)
    elif _trip_cur.get(uid) and time.time() * 1000 - _trip_cur[uid][-1]["ts"] > 1200000:
        _finish_trip(uid)
    if _bad_until.get(uid, 0) > time.time():
        return
    vin = vin_of(uid)
    # 主路径(v2,2026-10-25):遥测驱动。车辆主动推送,零唤醒零配额;
    # 遥测静默=车辆休眠,此时绝不调 vehicle_data(旧版 60s 轮询会重置休眠计时器,车永远在线)。
    if vin:
        try:
            with open(os.path.join(TELEMETRY_DIR, "telemetry_state_%s.json" % vin), encoding="utf-8") as f:
                td = json.load(f)
        except Exception:
            td = None
        if td:
            _lu = td.get("last_update") or 0
            if _lu and (time.time() - _lu) > 600:
                td = None   # 遥测帧陈旧>10分钟:可能是流断供(scope被拒/流故障),不当作"驻车"真相,落 REST 兜底
        if td:
            fs = normalize_measurements(td.get("fields") or {}, td.get("field_ts") or {})
            lu = td.get("last_update") or 0
            lat, lon = fs.get("latitude"), fs.get("longitude")
            ac_power = float(fs.get("ac_charging_power") or 0)
            dc_power = float(fs.get("dc_charging_power") or 0)
            cs = {"charging_state": fs.get("charging_state") or "",
                  "battery_level": fs.get("battery_level"),
                  "battery_range": fs.get("rated_range"),
                  "charging": fs.get("charging"),
                  "charge_energy_added": fs.get("charge_energy_added"),
                  "sample_ts": lu * 1000,
                  "charger_power": max(ac_power, dc_power),
                  "ac_charging_power": ac_power, "dc_charging_power": dc_power,
                  "fast_charger_present": fs.get("fast_charger_present"),
                  "latitude": lat, "longitude": lon}
            charge_watch(uid, cs)   # 充电完成/低电量 → 聊天流系统消息
            _track_charging(uid, cs)   # 充电会话记账（遥测口径：按功率×时间积分）
            if lat is None or lon is None:
                return
            pt = {"ts": int(lu * 1000),
                  "lat": round(float(lat), 6), "lon": round(float(lon), 6),
                  "spd": float(fs["speed"]) * 1.609344 if fs.get("speed") is not None else None,
                  "gear": fs.get("shift_state"),
                  "odo": round((fs.get("odometer") or 0) * 1.609344, 2)}
            times = td.get("field_ts") or {}
            voltage, current = fs.get("pack_voltage"), fs.get("pack_current")
            if voltage is not None and current is not None and all(
                    0 <= lu - float(times.get(k) or 0) <= 120 for k in ("pack_voltage", "pack_current")):
                pt["pwr"] = round(voltage * current / 1000, 2)
                pt["power_source"] = "battery"
            if fs.get("battery_level") is not None:
                pt["bat"] = round(float(fs.get("battery_level")), 2)
            rr = fs.get("rated_range")
            if rr is not None:
                pt["rng"] = round(float(rr) * 1.609344, 1)
            it, ot = fs.get("inside_temp"), fs.get("outside_temp")
            if it is not None:
                pt["tin"] = round(float(it), 1)
            if ot is not None:
                pt["tout"] = round(float(ot), 1)
            # VIN 档案刷新(定位/胎压):cn 固件的 tpms 值只在遥测这条通道
            try:
                ap = os.path.join(USERS, "vehicles", vin + ".json")
                try:
                    arch = json.load(open(ap, encoding="utf-8"))
                except Exception:
                    arch = {}
                arch["latitude"] = pt["lat"]
                arch["longitude"] = pt["lon"]
                arch["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                tpm = {k[5:].lower(): v for k, v in fs.items() if str(k).startswith("tpms_")}
                if tpm:
                    arch["tire_pressure"] = tpm
                tmp = ap + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(arch, f, ensure_ascii=False, indent=1)
                os.replace(tmp, ap)
            except Exception:
                pass
            if pt["ts"] > last_ts(uid):
                _track_trip(uid, pt)
            moving = (pt.get("gear") in ("D", "R", "N")) or ((pt.get("spd") or 0) > 1)
            if not moving and not _last_moving.get(uid):
                return   # 驻车:不写点(车睡着遥测也不来,自然静默)
            _last_moving[uid] = moving
            if pt["ts"] <= last_ts(uid):
                return
            with open(tracks_file(uid), "a", encoding="utf-8") as f:
                f.write(json.dumps(pt, ensure_ascii=False) + "\n")
            log(f"{uid} +点(遥测) gear={pt['gear']} odo={pt['odo']}km")
            return bool(moving)
    # REST 兜底节流(无遥测文件的车才走到这里):统一 60s 一采(v1 同款,2026-10-26 排查已证
    # vehicle_data 轮询不唤醒不钉车;10-25 的"一直在线"真凶是特斯拉原生每小时心跳)
    _now = time.time()
    idle_cadence = int(os.environ.get("TRACK_REST_IDLE_INTERVAL", "60"))
    cadence = 60 if _now - _rest_ok.get(uid, 0) < 1200 else idle_cadence
    if _now - _rest_try.get(uid, 0) < cadence:
        return
    _rest_try[uid] = _now
    vid = vid_of(uid)
    tok = tok_of(uid)
    if not vid or not tok:
        return
    # 端点选择:授权缺 vehicle_location scope 的 token,带 location_data 会整体 403
    # (特斯拉 2026-10-22 前后收紧执行);降级去掉 location_data——轨迹经纬度仍由
    # drive_state 提供,行程/充电/温度全功能不受影响。恢复完整权限:重新授权勾选位置。
    ep = _EP_FULL if _loc_ok.get(uid, True) else _EP_NOLOC
    try:
        # endpoints 串 = 主应用同款（分号必须 %3B 编码：2026-10-07 实测 cn 网关对裸分号
        # 只下发 location_data，drive_state/vehicle_state 段全被吞 → gear/odo/速度全空）
        d = http_json(f"{FLEET}/api/1/vehicles/{vid}/vehicle_data?endpoints={ep}", tok)
    except urllib.error.HTTPError as e:
        if e.code in (408, 504):
            return  # 车睡着：vehicle_data 不唤醒，静默跳过（零成本）
        if e.code in (401, 403):
            body = ''
            try:
                body = e.read().decode('utf-8', 'replace')
            except Exception:
                pass
            if 'vehicle_location' in body and _loc_ok.get(uid, True):
                _loc_ok[uid] = False   # 下一轮起用降级端点
                log(f"{uid} 授权缺 vehicle_location scope(403),降级端点去 location_data;轨迹仍由 drive_state 提供,完整恢复需重新授权勾选位置")
                return
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
    _rest_ok[uid] = time.time()   # 200:车醒着,接下来 20 分钟内保持 60s 快采
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
    charge_watch(uid, cs)   # 充电完成/低电量 → 聊天流系统消息（须在驻车早退之前）
    cs = dict(cs)
    cs["latitude"], cs["longitude"] = lat, lon
    cs["ac_charging_power"] = float(cs.get("charger_actual_current") or 0) * float(cs.get("charger_voltage") or 0) / 1000.0
    cs["dc_charging_power"] = float(cs.get("charger_power") or 0) if cs.get("fast_charger_present") else 0
    _track_charging(uid, cs)   # 充电会话记账（须在驻车早退之前：驻车充电正是要记的）
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
    _track_trip(uid, pt)
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
    # 重复结算防护（2026-10-24）：3 分钟内已有里程差<0.3km 且时长差<5min 的结算文件时跳过
    #（历史上 P 档判定失守曾把同一段每轮重结算一次，一天同段写 40+ 个近似 ID 文件）
    try:
        ddir_chk = os.path.join(USERS, uid, "trip_details")
        if os.path.isdir(ddir_chk):
            import glob as _g
            for fp in _g.glob(os.path.join(ddir_chk, detail["id"][:8] + "*.json")):
                try:
                    prev = json.load(open(fp, encoding="utf-8"))
                except Exception:
                    continue
                ptid = os.path.basename(fp)[:-5]
                import time as _t
                _a = _t.mktime(_t.strptime(detail["id"], "%Y%m%d-%H%M%S"))
                _b = _t.mktime(_t.strptime(ptid, "%Y%m%d-%H%M%S"))
                if abs(_a - _b) <= 180 and abs((detail.get("distance_km") or 0) - (prev.get("distance_km") or 0)) < 0.3 \
                        and abs((detail.get("duration_min") or 0) - (prev.get("duration_min") or 0)) <= 5:
                    log(f"{uid} 跳过重复结算 {detail['id']} ≈ {ptid}")
                    return
    except Exception:
        pass
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
    # 行程完成 → 聊天流系统消息（2026-10-23 通知中心）
    try:
        s_addr = (detail.get("start_addr") or "").strip()
        e_addr = (detail.get("end_addr") or "").strip()
        tail = f"，从{s_addr[:12]}到{e_addr[:12]}" if s_addr and e_addr else ""
        sys_msg(uid, f"🚗 行程完成：{_dist} km · {int(_dur_s // 60)} 分钟{tail}",
                {"type": "trip_detail", "trip_id": detail["id"]})
    except Exception:
        pass
    # 订阅消息（2026-10-24）：用户授权过则微信通知，失败静默
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8794/internal/wxmsg",
            data=json.dumps({"uid": uid, "title": "行程完成",
                             "value": "%.1f km · %d 分钟" % (_dist, _dur_s / 60)}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    log("%s 行程结算 %s %.1fkm %d分钟" % (uid, detail["id"], _dist, _dur_s / 60))


def cleanup_old():
    """轨迹点月文件清理：2026-10-24 起行程做长线存储（trip_details 永久保留、
    行程回顾支持周/月/年筛选），不再删轨迹点（旧点文件是历史行程即时补算的唯一来源）。"""
    return


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
    log(f"watcher start idle_interval={INTERVAL}s moving_interval={MOVING_INTERVAL}s pid={os.getpid()} base={BASE}")
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
        # 行驶中每1s读一次本地遥测快照：不调用 Tesla API、不唤醒车辆；
        # 驻车仍按60s基线。此前10s抽样会漏掉最高车速，也难以形成连续功率曲线。
        time.sleep(MOVING_INTERVAL if any_moving else INTERVAL)


if __name__ == "__main__":
    main()
