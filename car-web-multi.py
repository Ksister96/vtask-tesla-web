# -*- coding: utf-8 -*-
"""car-web-multi.py — 朋友专用多用户特斯拉车控网页服务（独立端口 8796（dev 测试））

与主链路隔离（用户 2026-09-02 确认）：
- 主链路 car-web-server.py(8793) + run.py + 微信插件 完全不动
- 本服务独立：端口 8796、用户目录 E:/tesla-fleet-multi-dev/users/、不读主人的 tokens.json
- 每个朋友：访问码登录（沿用 access-codes 思路，但独立文件）→ OAuth 绑定自己的特斯拉 → 控制自己的车

架构:
  浏览器 ──HTTP──> 本服务(8796)
        ├── 访问码登录（friends-codes.json）
        ├── /api/oauth/start → 跳特斯拉授权
        ├── /api/oauth/callback → 换 token + 列车辆
        ├── /api/vehicles → 选车绑定
        └── /api/chat → user_runner.py 执行
"""
import base64
import gzip
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import io
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import math

import car_schedule_multi as schedule

# 邮箱验证码登录（stdlib + SQLite；2026-10-17 起唯一登录方式，访问码/设备码下线）
from email_auth import EmailAuthStore, AuthError
from auth_http import EmailAuthEndpoints

BASE = os.environ.get("MULTI_BASE", r"E:\tesla-fleet-multi")
RUNNER = os.path.join(BASE, "user_runner.py")
STATIC_DIR = os.path.join(BASE, "static")
USERS_DIR = os.path.join(BASE, "users")
CODES_FILE = os.path.join(BASE, "friends-codes.json")
EMAIL_AUTH = EmailAuthStore(BASE)   # 邮箱身份库 identity.sqlite3（邮箱→uid + 验证码挑战 + 频控）
LOG_FILE = os.path.join(BASE, "multi-web.log")
AMAP_KEY_FILE = os.path.join(BASE, "amap_key.txt")
# 小程序（微信）凭据：环境变量注入；两者齐备走真 code2session，缺省为 mock 模式
MP_WX_APPID = os.environ.get("MP_WX_APPID", "")
MP_WX_SECRET = os.environ.get("MP_WX_SECRET", "")

# 监控大屏：手机可访问的实时用户操作面板
# 密码三种来源：环境变量 MONITOR_PASSWORD > 配置文件 monitor.json > 自动生成并写入 monitor.json
MONITOR_FILE = os.path.join(BASE, "monitor.json")
USER_NAMES_FILE = os.path.join(BASE, "user_names.json")

def _claim_assets_by_vin(uid, vin):
    """2026-10-06 方案A：绑定/加载时自动认领同 VIN 老账号的资产（皮肤档取各车身最新、
    settings 取最近活跃），并把老账号的登录码停用（身份收敛到新账号，换设备不掉资产）。
    幂等：account.claimed_vin 标记，认领过的不再重复。"""
    vin = (vin or "").strip().upper()
    if not vin:
        return
    my_dir = user_dir(uid)
    acct_p = os.path.join(USERS_DIR, uid, "account.json")
    try:
        acct = json.load(open(acct_p, encoding="utf-8"))
    except Exception:
        return
    if acct.get("claimed_vin") == vin:
        return
    import glob as _g
    old_uids = []
    for f in _g.glob(os.path.join(USERS_DIR, "*", "account.json")):
        ouid = os.path.basename(os.path.dirname(f))
        if ouid == uid:
            continue
        try:
            a = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if any(str(v.get("vin", "")).strip().upper() == vin for v in (a.get("vehicles") or [])):
            old_uids.append(ouid)
    # 1) 皮肤档：按 body 各取 ts 最新的迁入
    best = {}
    for ouid in old_uids:
        for f in _g.glob(os.path.join(USERS_DIR, ouid, "wrap_state*.json")):
            bn = os.path.basename(f)
            try:
                st = (json.load(open(f, encoding="utf-8")).get("state") or {})
            except Exception:
                continue
            key = st.get("body") or bn.replace("wrap_state", "").replace(".json", "") or bn
            ts = st.get("ts", 0)
            if ts >= best.get(key, ({}, -1))[1]:
                best[key] = (st, ts, bn)
    for key, (st, ts, bn) in best.items():
        m = re.fullmatch(r"[a-z0-9]+", str(key) or "")
        dst_name = ("wrap_state_%s.json" % key) if m else bn
        try:
            with open(os.path.join(my_dir, dst_name), "w", encoding="utf-8") as f:
                json.dump({"state": st, "ts": now_iso() if 'now_iso' in dir() else ts}, f, ensure_ascii=False)
        except Exception:
            pass
    # 2) settings：取最近活跃同 VIN 账号的（壁纸等偏好跟着走）
    try:
        _old_sorted = sorted(old_uids, key=lambda o: os.path.getmtime(os.path.join(USERS_DIR, o, "settings.json"))
                             if os.path.exists(os.path.join(USERS_DIR, o, "settings.json")) else 0, reverse=True)
        for ouid in _old_sorted:
            sp = os.path.join(USERS_DIR, ouid, "settings.json")
            if os.path.exists(sp):
                dst = os.path.join(my_dir, "settings.json")
                if not os.path.exists(dst):
                    import shutil as _sh
                    _sh.copy(sp, dst)
                break
    except Exception:
        pass
    # 2.5) 行程记录迁移（2026-10-13 用户要求像车漆一样跟车走）：
    #      老账号 tracks/*.jsonl 按 ts 去重合并迁入（同 VIN 多账号各存一份相同行程，不否认重），
      #      trip_details 目标缺失才复制；watcher 只写增量，迁移后新行程继续落新账号
    try:
        import shutil as _sh2
        tdst = os.path.join(my_dir, "tracks")
        os.makedirs(tdst, exist_ok=True)
        seen_ts = set()
        for _f in _g.glob(os.path.join(tdst, "*.jsonl")):
            try:
                with open(_f, encoding="utf-8") as fh:
                    for _line in fh:
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _ts = json.loads(_line).get("ts")
                        except Exception:
                            continue
                        if _ts:
                            seen_ts.add(_ts)
            except Exception:
                pass
        merged = 0
        for ouid in old_uids:
            for f in _g.glob(os.path.join(USERS_DIR, ouid, "tracks", "*.jsonl")):
                dst = os.path.join(tdst, os.path.basename(f))
                newlines = []
                try:
                    with open(f, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                _ts = json.loads(line).get("ts")
                            except Exception:
                                continue
                            if _ts and _ts in seen_ts:
                                continue
                            if _ts:
                                seen_ts.add(_ts)
                            newlines.append(line)
                except Exception:
                    continue
                if newlines:
                    with open(dst, "a", encoding="utf-8") as out:
                        out.write("\n".join(newlines) + "\n")
                    merged += len(newlines)
        ddst = os.path.join(my_dir, "trip_details")
        os.makedirs(ddst, exist_ok=True)
        copied = 0
        for ouid in old_uids:
            for f in _g.glob(os.path.join(USERS_DIR, ouid, "trip_details", "*.json")):
                dst = os.path.join(ddst, os.path.basename(f))
                if os.path.exists(dst):
                    continue
                try:
                    _sh2.copy(f, dst)
                    copied += 1
                except Exception:
                    pass
        if merged or copied:
            log_line("claim", f"{uid} 迁移行程记录：轨迹点 {merged} / 详情 {copied}")
    except Exception:
        pass
    # 3) 老账号登录码停用（身份收敛；已发 session 到期后自然失效）
    #    注意：设备码自动建档的用户 rec.uid 为 None、code==uid，两种都要匹配
    try:
        codes = load_codes()
        changed = False
        for code, rec in codes.items():
            if (rec.get("uid") in old_uids) or (code in old_uids):
                if rec.get("enabled", True):
                    rec["enabled"] = False
                    rec["note"] = (rec.get("note", "") + " ").strip() + "(已并入 " + uid + ")"
                    changed = True
        if changed:
            save_codes(codes)
    except Exception:
        pass
    # 4) 标记认领完成
    acct["claimed_vin"] = vin
    save_account(uid, acct)
    log_line("claim", f"{uid} 认领同VIN({vin})资产：老账号 {old_uids} 已停用")


def _load_user_names():
    try:
        if os.path.exists(USER_NAMES_FILE):
            with open(USER_NAMES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_user_names(names):
    try:
        with open(USER_NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

MONITOR_EVENTS_MAX = 80
# 默认仅本机回环；可用环境变量 CAR_HOST 临时放开（如 CAR_HOST=0.0.0.0 供局域网/手机联调）
import os as _os
HOST = _os.environ.get("CAR_HOST", "127.0.0.1")
PORT = 8796

# 特斯拉（中国区）OAuth
AUTH_URL = "https://auth.tesla.cn/oauth2/v3/authorize"
TOKEN_URL = "https://auth.tesla.cn/oauth2/v3/token"
FLEET_URL = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"
CLIENT_ID = "b0708cc4-c9b7-4a66-a061-feb3ef3c58a9"
# 密钥不入库：从环境变量读取（云端在 tesla-web.service 里配置，缺失则 OAuth 绑定不可用）
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
# 回调：本地先用本机地址（Tesla 官方要求 https，但本地验证阶段可用 localhost 测试或后续换公网）
# 注意：中国区回调需要 https，本机测试用 cpolar 内网穿透暴露 https 公网地址
REDIRECT_URI = "https://vtask-tesla.cpolar.cn/api/oauth/callback"
SCOPE = "openid offline_access user_data vehicle_device_data vehicle_cmds vehicle_charging_cmds vehicle_location"

SESSION_TTL = 30 * 24 * 3600  # 30 天免登录；会话持久化，服务重启后仍保持登录
RATE_LIMIT = 20
RATE_WINDOW = 60
RUN_TIMEOUT = 150
MAX_TEXT = 500
MAX_AUDIO = 10 * 1024 * 1024  # 10MB
ROUTE_TIMEOUT = 12
WHISPER_URL = "http://127.0.0.1:8790/transcribe"

_tz = timezone(timedelta(hours=8))

# ---- 会话持久化（重启不丢登录态） ----
SESSION_FILE = os.path.join(BASE, "sessions.json")
_session_lock = threading.Lock()

def _session_load():
    """启动时恢复会话，清理已过期的。"""
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        for k in list(d):
            if d[k].get("exp", 0) < now:
                d.pop(k, None)
        return d
    except Exception:
        return {}

def _session_save():
    try:
        with _session_lock:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(_sessions, f, ensure_ascii=False)
    except Exception:
        pass

_sessions = _session_load()  # web token -> {code, name, uid, exp}
_rate = {}
_chat_lock = threading.RLock()
CHAT_MAX = 100

# ??? Geolocation ?? WGS-84????? Web Service ?????? GCJ-02 ???
_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323

def _out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)

def _transform_lat(x, y):
    ret = -100.0 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*abs(x)**0.5
    ret += (20*math.sin(6*x*_PI) + 20*math.sin(2*x*_PI))*2/3
    ret += (20*math.sin(y*_PI) + 40*math.sin(y/3*_PI))*2/3
    ret += (160*math.sin(y/12*_PI) + 320*math.sin(y*_PI/30))*2/3
    return ret

def _transform_lon(x, y):
    ret = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*abs(x)**0.5
    ret += (20*math.sin(6*x*_PI) + 20*math.sin(2*x*_PI))*2/3
    ret += (20*math.sin(x*_PI) + 40*math.sin(x/3*_PI))*2/3
    ret += (150*math.sin(x/12*_PI) + 300*math.sin(x/30*_PI))*2/3
    return ret

def wgs84_to_gcj02(lon, lat):
    """? WGS-84 ?? GCJ-02????????????"""
    lon, lat = float(lon), float(lat)
    if _out_of_china(lon, lat): return lon, lat
    dlat = _transform_lat(lon-105, lat-35); dlon = _transform_lon(lon-105, lat-35)
    radlat = lat/180*_PI; magic = 1-_EE*math.sin(radlat)**2; root = math.sqrt(magic)
    dlat = dlat*180/((_A*(1-_EE))/(magic*root)*_PI)
    dlon = dlon*180/(_A/root*math.cos(radlat)*_PI)
    return lon+dlon, lat+dlat

def load_amap_key():
    try:
        with open(AMAP_KEY_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def car_origin_wgs84(uid):
    """车辆当前位置（VIN 档案，WGS84）；无则 None。
    定时用车路线起点用（2026-10-07 用户定稿：车辆定位优先于手机定位）。
    档案坐标由每次 vehicle_data 成功拉取时刷新（行驶中另有轨迹采集器每分钟刷新）。"""
    try:
        acct = get_account(uid) or {}
        vin = ((acct.get("vehicles") or [{}])[0].get("vin") or "").strip()
        if not vin:
            return None
        meta = vehicle_archive_read(vin) or {}
        lat, lon = meta.get("latitude"), meta.get("longitude")
        if lat is None or lon is None:
            return None
        return {"longitude": float(lon), "latitude": float(lat), "updated": meta.get("updated")}
    except Exception:
        return None


def route_info(origin_lon, origin_lat, destination, dest_location=None, city="上海"):
    """高德地理编码 + 驾车路线；失败返回 None，不阻断定时用车。"""
    key = load_amap_key()
    if not key or not destination:
        return None
    try:
        raw_lon, raw_lat = float(origin_lon), float(origin_lat)
        gcj_lon, gcj_lat = wgs84_to_gcj02(raw_lon, raw_lat)
        log_line("route", f"???? WGS84=({raw_lon:.6f},{raw_lat:.6f}) -> GCJ02=({gcj_lon:.6f},{gcj_lat:.6f})")
        loc = dest_location
        resolved_name = destination
        if not loc:
            # 目的地本身就是 "lon,lat" 坐标串时直接用，别拿去地理编码（AMap 搜不到）
            _m = re.match(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$", destination or "")
            if _m:
                _lon, _lat = float(_m.group(1)), float(_m.group(2))
                if -180 <= _lon <= 180 and -90 <= _lat <= 90:
                    loc = "%s,%s" % (_m.group(1), _m.group(2))
        if not loc:
            # 注意：_geocode_with_city 返回 (location, display_name) 元组，必须解包。
            # 早期副本漏了解包 -> loc 是 tuple -> `"," not in loc` 恒 False
            # -> 地理编码明明成功仍 return None（/api/route 报"路线查询失败"）。
            loc, resolved_name = schedule._geocode_with_city(destination, key, city)
            if not loc:
                return None
        if "," not in loc:
            return None
        q = urllib.parse.urlencode({"origin": f"{gcj_lon:.6f},{gcj_lat:.6f}", "destination": loc, "key": key, "extensions": "base", "output": "JSON"})
        with urllib.request.urlopen("https://restapi.amap.com/v3/direction/driving?" + q, timeout=ROUTE_TIMEOUT) as r:
            route = json.loads(r.read().decode("utf-8"))
        path = ((route.get("route") or {}).get("paths") or [None])[0]
        if str(route.get("status")) != "1" or not path:
            return None
        distance_m = float(path.get("distance", 0))
        duration_s = int(float(path.get("duration", 0)))
        if distance_m <= 0 or duration_s <= 0:
            return None
        return {"dest_name": resolved_name or destination, "dest_location": loc, "distance_km": round(distance_m / 1000, 1), "duration_min": max(1, round(duration_s / 60))}
    except Exception as e:
        log_line("route", f"路线查询失败: {destination}: {e}")
        return None


def save_place(uid, name, location):
    path = os.path.join(USERS_DIR, uid, "places.json")
    try:
        with open(path, "r", encoding="utf-8") as f: places = json.load(f)
    except Exception: places = {}
    places[name] = {"name": name, "location": location}
    with open(path, "w", encoding="utf-8") as f: json.dump(places, f, ensure_ascii=False, indent=2)


def load_place(uid, name):
    try:
        with open(os.path.join(USERS_DIR, uid, "places.json"), "r", encoding="utf-8") as f:
            return json.load(f).get(name)
    except Exception: return None


def now_iso():
    return datetime.now(_tz).strftime("%Y-%m-%d %H:%M:%S")

_REGEO_CACHE = {}
_REGEO_FAIL_UNTIL = 0   # 高德失败冷静期截止时间戳（2026-10-13）


def _regeo_full(lat, lon):
    """(地名, 区名) 二元组；进程内缓存同 200 米。regeo_name/regeo_district 共用一次高德调用。
    可靠性（2026-10-13）：失败/空响应一律返回空串且【不写缓存】——此前把 31.4834,121.x 坐标串
    当地名缓存，一次网络抖动就在列表/详情长期显示原始坐标；失败后 5 分钟冷静期不再打高德。"""
    global _REGEO_FAIL_UNTIL
    key = "%.3f,%.3f" % (lat, lon)
    if key in _REGEO_CACHE:
        return _REGEO_CACHE[key]
    if len(_REGEO_CACHE) > 500:
        _REGEO_CACHE.clear()
    if time.time() < _REGEO_FAIL_UNTIL:
        return ("", "")   # 高德不可用冷静期：宁可短暂空缺，不用坐标串充数
    name = "%.4f,%.4f" % (lat, lon)
    dist = ""
    ok = False
    try:
        k = load_amap_key()
        if k:
            q = urllib.parse.urlencode({"location": "%.6f,%.6f" % (lon, lat), "key": k, "extensions": "all"})
            with urllib.request.urlopen("https://restapi.amap.com/v3/geocode/regeo?" + q, timeout=6) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            rg = (d or {}).get("regeocode") or {}
            comp = rg.get("addressComponent") or {}
            pois = rg.get("pois") or []
            city = comp.get("city")
            if isinstance(city, list):
                city = city[0] if city else ""
            dist = comp.get("district") or ""
            town = comp.get("township") or ""
            street = (comp.get("street") or "") + (comp.get("number") or "")
            if pois and pois[0].get("name"):
                name = str(pois[0]["name"]); ok = True
            else:
                fa = str(rg.get("formatted_address") or "")
                if fa.startswith("中国"):
                    fa = fa[2:]
                for _m in (re.match(r"^[^省]{2,8}省", fa), re.match(r"^[^市]{2,8}市", fa),
                           re.match(r"^[^区]{2,8}区", fa), re.match(r"^[^县]{2,8}县", fa)):
                    if _m:
                        fa = fa[_m.end():]
                if fa:
                    name = fa; ok = True
                elif (dist or "") + town + street:
                    name = (dist or "") + town + street; ok = True
                elif town:
                    name = town; ok = True
                elif dist:
                    name = dist; ok = True
                elif city:
                    name = city; ok = True
    except Exception:
        ok = False
    if ok:
        _REGEO_CACHE[key] = (name, dist)
        return (name, dist)
    _REGEO_FAIL_UNTIL = time.time() + 300
    return ("", "")


def regeo_name(lat, lon):
    """起点/终点坐标 → 详细地名（POI 优先，退格式化地址逐级去前缀，再退区县+街道）。"""
    return _regeo_full(lat, lon)[0]


def regeo_district(lat, lon):
    """坐标 → 区/县名（路书海报地点胶囊用，区级粒度）。失败返回空串。"""
    return _regeo_full(lat, lon)[1]


def bind_profile_snapshot(uid, vid, tok):
    """绑定建档：全量 vehicle_data 存 vehicle_meta.json（车身色/车型/配置一次到位）。"""
    try:
        import ssl as _ssl
        req = urllib.request.Request(
            f"{FLEET_URL}/api/1/vehicles/{vid}/vehicle_data",
            headers={"Authorization": "Bearer " + tok})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        resp = (d or {}).get("response") or {}
        vc = resp.get("vehicle_config") or {}
        ec = vc.get("exterior_color")
        if ec:
            mp = os.path.join(USERS_DIR, uid, "vehicle_meta.json")
            meta = {"exterior_color": ec, "model": vc.get("car_type"),
                    "trim": vc.get("trim_badging"), "ts": int(time.time() * 1000)}
            json.dump(meta, open(mp, "w", encoding="utf-8"))
            meta["vin"] = resp.get("vin")
            meta["paint_color"] = (resp.get("vehicle_state") or {}).get("paint_color")
            vehicle_archive_update(resp.get("vin"), meta)   # 同步进 VIN 档案（跟车走）
            log_line("bind", f"{uid} 车辆档案已建立 色={ec} 型={vc.get('car_type')} VIN={resp.get('vin')}")
    except Exception:
        pass


def log_line(kind, msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {kind}: {msg}\n")
    except Exception:
        pass


# ---------- VIN 车辆档案（档案跟着车走，跨账号共享同一台车） ----------
def vehicle_archive_path(vin):
    return os.path.join(USERS_DIR, "vehicles", (vin or "").strip() + ".json")


def vehicle_archive_update(vin, patch):
    """浅合并写入 VIN 档案（不删已有键），带更新时间戳。"""
    vin = (vin or "").strip()
    if not vin:
        return
    p = vehicle_archive_path(vin)
    data = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.update(patch)
    data["vin"] = vin
    data["updated"] = now_iso()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log_line("archive", f"VIN 档案写入失败 {vin}: {e}")


def vehicle_archive_read(vin):
    """读 VIN 档案；档案不存在时懒迁移：合并各 uid 历史 vehicle_meta 里同 VIN 的记录。"""
    vin = (vin or "").strip()
    if not vin:
        return {}
    p = vehicle_archive_path(vin)
    if not os.path.exists(p):
        merged = {}
        try:
            for uid in os.listdir(USERS_DIR):
                up = os.path.join(USERS_DIR, uid, "vehicle_meta.json")
                if not os.path.isfile(up):
                    continue
                try:
                    with open(up, "r", encoding="utf-8") as f:
                        m = json.load(f)
                except Exception:
                    continue
                uvin = (m.get("vin") or "")
                try:   # 优先用账号绑定的 VIN（历史 vehicle_meta 可能没存 vin 字段）
                    with open(os.path.join(USERS_DIR, uid, "account.json"), "r", encoding="utf-8-sig") as f:
                        acct = json.load(f)
                    uvin = ((acct.get("vehicles") or [{}])[0].get("vin") or "") or uvin
                except Exception:
                    pass
                if uvin.strip() == vin:
                    merged.update(m)
        except Exception:
            pass
        if merged:
            vehicle_archive_update(vin, merged)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# ---------- 访问码（独立于主 access-codes.json） ----------
def load_codes():
    try:
        with open(CODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_codes(codes):
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)

def ensure_codes_file():
    if os.path.exists(CODES_FILE):
        return
    save_codes({
        "demo01": {"name": "演示朋友", "enabled": True, "note": "示例码，请改成你自己的"},
    })
    log_line("init", f"已生成初始 {CODES_FILE}")

# ---------- 监控大屏 ----------
def _load_or_create_monitor_key():
    """从 monitor.json 读或随机生成一个 12 位密码，写回文件。
    优先环境变量 MONITOR_PASSWORD，未设才走文件。"""
    try:
        if os.path.exists(MONITOR_FILE):
            d = json.load(open(MONITOR_FILE, encoding="utf-8"))
            k = d.get("key")
            if k and len(k) >= 8:
                return k
    except Exception:
        pass
    import secrets as _sec
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    k = "".join(_sec.choice(alpha) for _ in range(10))
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(MONITOR_FILE, "w", encoding="utf-8") as f:
            json.dump({"key": k, "created": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return k

MONITOR_KEY = os.environ.get("MONITOR_PASSWORD") or _load_or_create_monitor_key()

# ---------- 贴膜工坊：官方模板清单 + VIN 离线解码（2026-10-02 缩减为在售 8 套，只留有 3D 车模的车型） ----------
_skin_cache = [0, []]  # [ts, items]

WRAP_TEMPLATES = {
    "modely": {"name": "老款 Model Y", "file": "wraps/modely.png"},
    "modely-2025-performance": {"name": "新款 Model Y", "file": "wraps/modely-2025-performance.png"},
    "modely-l": {"name": "Model Y L", "file": "wraps/modely-l.png"},
    "model3": {"name": "老款 Model 3", "file": "wraps/model3.png"},
    "model3-2024-base": {"name": "新款 Model 3", "file": "wraps/model3-2024-base.png"},
    "model3-2024-performance": {"name": "新款 3P", "file": "wraps/model3-2024-performance.png"},
}

def wrap_decode_vin(vin):
    """VIN 前缀 → 模板 key（离线，不依赖车辆在线）。"""
    v = (vin or "").upper()
    if v.startswith("LRW3E7"):
        return "model3"
    if v.startswith("LRW3"):
        return "model3"
    if v.startswith("LRW"):
        return "modely"
    return "modely"

SKIN_MODEL_FAMILY = {
    "modely": {"model-y", "model-y-2025-premium", "model-y-2025-performance",
               "model-y-2025-base", "model-y-l"},
    "model3": {"model-3", "model-3-2024-base", "model-3-2024-performance"},
}

# 模板 key → tesla-skin 素材标签（精确匹配：素材只适配「对着这张模板画」的车）
# 依据：白区 IoU 实测——modely-2025 premium≈performance(0.93) 可互换；base 独立布局(0.44)不混；
# model3-2024 base≈performance(0.94) 可互换；老款与新款模板互不适配
WRAP_SKIN_FILTER = {
    "modely": ["model-y"],
    "modely-2025-performance": ["model-y-2025-premium", "model-y-2025-performance"],
    "modely-l": ["model-y-l"],
    "model3": ["model-3"],
    "model3-2024-base": ["model-3-2024-base", "model-3-2024-performance"],
    "model3-2024-performance": ["model-3-2024-performance", "model-3-2024-base"],
}

# ---------- 用户 ----------
def user_dir(uid):
    return os.path.join(USERS_DIR, uid)

def get_account(uid):
    p = os.path.join(user_dir(uid), "account.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_account(uid, acct):
    os.makedirs(user_dir(uid), exist_ok=True)
    with open(os.path.join(user_dir(uid), "account.json"), "w", encoding="utf-8") as f:
        json.dump(acct, f, ensure_ascii=False, indent=2)

def get_user_dir_tokens(uid):
    p = os.path.join(user_dir(uid), "tokens.json")
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None

def save_user_tokens(uid, tok):
    os.makedirs(user_dir(uid), exist_ok=True)
    tok["client_id"] = CLIENT_ID
    tok["client_secret"] = CLIENT_SECRET
    # 原子写：写一半崩溃不会同时丢 access+refresh token（refresh_token 轮换型，丢了就得重新绑车）
    dst = os.path.join(user_dir(uid), "tokens.json")
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)
    os.replace(tmp, dst)


# ---------- 控制授权（虚拟钥匙配对）状态 ----------
PAIR_URL = "https://www.tesla.cn/_ak/pk.vtask.top"   # 2026-10-07 定版：vtask.top 10-06 首配失败被特斯拉配对层记负面缓存(预抓取即拒,重注册不清)；pk.vtask.top 全新子域一次配对成功+遥测域名检查通过
_NOT_PAIRED_IN_REPLY = ("public key has not been paired", "not been paired",
                        "vehicle rejected request", "rejected request")


def _pair_state_path():
    return os.path.join(USERS_DIR, "pair_state.json")


def get_pair_state(uid):
    try:
        with open(_pair_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return (d or {}).get(uid, {})
    except Exception:
        return {}


PAIR_CACHE_TTL = 24 * 3600  # control_ready 缓存有效期（秒）：超时强制重探，避免用户在车机删钥匙后引导永不弹


def pair_cache_ready(uid):
    """带时效判定 control_ready：仅在缓存未过期时信任。
    返回 True 表示可直接走已配对快速路径（不醒车）。"""
    st = get_pair_state(uid) or {}
    if not st.get("control_ready"):
        return False
    ts = st.get("ts")
    if not ts:
        return False
    try:
        import datetime as _dt
        d = _dt.datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
        age = time.time() - d.timestamp()
        return age < PAIR_CACHE_TTL
    except Exception:
        return False


def set_pair_state(uid, ready):
    try:
        os.makedirs(USERS_DIR, exist_ok=True)
        d = {}
        p = _pair_state_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        (d.setdefault(uid, {}))["control_ready"] = bool(ready)
        d[uid]["ts"] = now_iso()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def probe_user_pairing(uid):
    """调用 user_runner 的 __pairprobe__（door_lock，同步主链路）。
    返回 (ready, needs_pairing)。unknown 双 false、不改状态。"""
    out, err = run_runner(uid, "__pairprobe__")
    c = None
    if not err and out:
        try:
            c = (json.loads(out) or {}).get("c")
        except Exception:
            c = None
    if c == "ready":
        set_pair_state(uid, True)
        return True, False
    if c == "needs_pairing":
        set_pair_state(uid, False)
        return False, True
    return False, False


def reply_needs_pairing(reply):
    """判断一条命令回复文本是否命中“钥匙未配对”签名（用于兜底重弹）。"""
    if not reply:
        return False
    low = str(reply).lower()
    return any(mk in low for mk in _NOT_PAIRED_IN_REPLY)


def _reply_is_failure(reply, err):
    """判断一次执行是否"失败"：有 err，或回复文本带失败标记。"""
    if err:
        return True
    if not reply:
        return True
    s = str(reply)
    if "❌" in s:
        return True
    for kw in ("多次重试仍失败", "重试仍失败", "执行失败", "无法执行", "请求被拒绝", "命令失败"):
        if kw in s:
            return True
    return False


def check_pair_after_failure(uid, reply=None, err=None, timeout=45):
    """周哥方案：控制命令执行失败时，主动检测是否"虚拟钥匙没绑定"。
    真没绑 → 重置 pair_state 并返回 True（前端据此弹绑定引导）。
    关键：不靠文案猜测，直接问车（无损 door_lock 探针）。
    已配对 / 车离线看不出来 → False，不误扰用户。
    """
    if not _reply_is_failure(reply, err):
        return False
    try:
        out, e2 = run_runner(uid, "__pairprobe__")
        c = None
        if not e2 and out:
            try:
                c = (json.loads(out) or {}).get("c")
            except Exception:
                c = None
        if c == "needs_pairing":
            set_pair_state(uid, False)   # 确认没绑 → 允许引导页弹出
            return True
        if c == "ready":
            set_pair_state(uid, True)    # 确认已绑 → 刷新缓存时间戳
        return False
    except Exception:
        return False


# ---------- 仪表盘中键 · 快捷动作池 ----------
# id -> 池内白名单动作；前端负责显示图标与发送自然语言指令，后端只做持久化
DASHBTN_POOL = [
    # 多态切换（点击=切到目标档并执行；前端按真实车况同步档位）
    {"id": "doorlock", "cmd": "帮我锁车"},
    {"id": "climateswitch", "cmd": "打开空调"},
    {"id": "seat3", "cmd": "座椅加热低档"},
    {"id": "coolerswitch", "cmd": "座椅通风低档"},
    {"id": "wheelswitch", "cmd": "打开方向盘加热"},
    {"id": "sentryswitch", "cmd": "打开哨兵模式"},
    {"id": "chargeswitch", "cmd": "开始充电"},
    # 瞬时动作
    {"id": "battery", "cmd": "车还有多少电"},
    {"id": "doc", "cmd": "看看车子车况"},
    {"id": "bulb", "cmd": "闪灯一下"},
    {"id": "horn", "cmd": "按一下喇叭"},
    {"id": "truck", "cmd": "打开后备箱"},
    {"id": "carfront", "cmd": "打开前备箱"},
    {"id": "temp22", "cmd": "空调设为22度"},
]
def dashbtns_path(uid):
    return os.path.join(user_dir(uid), "dashbtns.json")
def default_dashbtn_ids():
    return ["doorlock", "climateswitch", "battery"]
def load_dashbtns(uid, defaults=None):
    valid=[p["id"] for p in DASHBTN_POOL]
    try:
        with open(dashbtns_path(uid),"r",encoding="utf-8") as f:
            d=json.load(f)
        arr=d.get("buttons") if isinstance(d,dict) else d
    except Exception:
        arr=[]
    ids=[x.get("id") if isinstance(x,dict) else x for x in (arr if isinstance(arr,list) else [])]
    out=[];seen=set()
    for i in ids:
        if i in valid and i not in seen and len(out)<4:
            seen.add(i);out.append(i)
    for i in (defaults if isinstance(defaults, list) else default_dashbtn_ids()):
        if len(out)>=3: break
        if i not in seen:
            out.append(i);seen.add(i)
    return out
def save_dashbtns(uid, ids):
    os.makedirs(user_dir(uid), exist_ok=True)
    tmp=dashbtns_path(uid)+".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump({"buttons":ids}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, dashbtns_path(uid))

# ---------- 用户设置 ----------
DEFAULT_SETTINGS = {
    "topic": "自动",        # 主题：浅色 | 深色 | 自动
    "wallpaper": "",        # 自定义壁纸：""=无；"#RRGGBB"=纯色；"url(...)" 由前端直接填背景值
    "fontSize": "std",      # 字号：sm | std | lg
    "showTime": True,        # 是否显示时间戳分组
    "confirmCmd": False,     # 危险指令二次确认(待第二批)
    "carName": "",           # 车昵称：""=用特斯拉账号里的车辆名
    "smartClimate": True,    # 智能温控：开空调按温差自动分档(猛降/快降/温和/猛热)
    "replyShowCar": True,    # 回复模板：显示 🚗 车辆状态行
    "replyShowWeather": True,# 回复模板：显示 ⛅ 天气行
    "replyShowTip": True,    # 回复模板：显示 💡 建议行
}
def settings_path(uid):
    return os.path.join(user_dir(uid), "settings.json")
def load_settings(uid):
    try:
        with open(settings_path(uid), "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    out = dict(DEFAULT_SETTINGS)
    for k in DEFAULT_SETTINGS:
        if k in d:
            out[k] = d[k]
    return out
def save_settings(uid, patch):
    os.makedirs(user_dir(uid), exist_ok=True)
    cur = load_settings(uid)
    allowed = set(DEFAULT_SETTINGS.keys())
    for k, v in (patch or {}).items():
        if k in allowed:
            cur[k] = v
    tmp = settings_path(uid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, settings_path(uid))
    return cur

# ---------- 聊天记录 ----------
def chat_path(uid):
    return os.path.join(user_dir(uid), "chat.json")

def load_chat(uid):
    try:
        with open(chat_path(uid), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_chat(uid, messages):
    os.makedirs(user_dir(uid), exist_ok=True)
    messages = messages[-CHAT_MAX:]
    tmp = chat_path(uid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    os.replace(tmp, chat_path(uid))

def append_chat(uid, who, text):
    text = str(text or "").strip()
    if not text:
        return
    with _chat_lock:
        messages = load_chat(uid)
        messages.append({"who": who, "text": text, "ts": now_iso()})
        save_chat(uid, messages)

# ---------- 会话 ----------
def make_session(code, name, uid=None):
    token = secrets.token_hex(16)
    with _session_lock:
        _sessions[token] = {"code": code, "name": name, "uid": uid or code, "exp": time.time() + SESSION_TTL}
    _session_save()
    return token

def check_session(token):
    s = _sessions.get(token)
    if not s:
        return None
    if s["exp"] < time.time():
        with _session_lock:
            _sessions.pop(token, None)
        _session_save()
        return None
    return s

# ---------- OAuth ----------
def oauth_start_url(state, redirect_uri):
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "prompt": "login consent",   # 2026-10-07 强制显示授权确认页（含新增的 vehicle_location），不再静默沿用旧授权
    })
    return f"{AUTH_URL}?{q}"

def oauth_exchange(code, redirect_uri):
    """code → token（POST auth.tesla.cn，与 exchange-and-list.ps1 一致）。"""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "audience": FLEET_URL,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return None, str(e)

OAUTH_PENDING_TTL = 15 * 60  # state 有效期 15 分钟

def oauth_state_consume(uid, state):
    """校验并消费 pending state：存在、uid 匹配、未过期才放行；放行后立即删除（一次性）。
    同时清理全部过期项，防止 oauth-pending.json 无限增长。
    没有这一步，任何人可构造 victim_uid:任意值 的回调把车绑到别人账号（登录 CSRF）。"""
    pf = os.path.join(BASE, "oauth-pending.json")
    try:
        with open(pf, "r", encoding="utf-8") as f:
            pend = json.load(f)
    except Exception:
        return False
    if not isinstance(pend, dict):
        pend = {}
    rec = pend.get(state)
    ok = bool(rec) and rec.get("uid") == uid \
        and (time.time() - float(rec.get("ts", 0))) < OAUTH_PENDING_TTL
    now = time.time()
    pend = {k: v for k, v in pend.items()
            if now - float((v or {}).get("ts", 0)) < OAUTH_PENDING_TTL}
    pend.pop(state, None)
    try:
        tmp = pf + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pend, f, ensure_ascii=False)
        os.replace(tmp, pf)
    except Exception:
        pass
    return ok

def list_vehicles(tok):
    """用用户 token 拉车辆列表。返回 (列表, 错误)。"""
    req = urllib.request.Request(f"{FLEET_URL}/api/1/vehicles",
                                 headers={"Authorization": f"Bearer {tok}"})
    import ssl
    ctx = ssl.create_default_context()  # 严格校验特斯拉证书（本地 4443 自签代理不走此处）
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        resp_list = d.get("response") or []
        out = []
        for v in resp_list:
            out.append({
                "id": v.get("id"),
                "vin": v.get("vin"),
                "display_name": v.get("display_name") or "未命名",
                "state": v.get("state"),
            })
        return out, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return None, str(e)

# 高频单动作本地快解析：异步路径不必起子进程也能毫秒级先把任务清单回到用户手上。
_FAST_RULES = [
    # 解锁必须先于锁车（"解锁车辆"含"锁车"子串，run.py/user_runner 同款修复），
    # 否则复合指令「解锁车辆，…」第一段被解析成 door_lock，锁/解锁颠倒属危险级
    (("解锁", "开门", "打开车门"), "door_unlock", {}),
    (("锁车", "锁门", "把车锁", "车上锁", "锁好车"), "door_lock", {}),
    (("闪灯", "闪一下灯"), "flash_lights", {}),
    (("鸣笛", "按喇叭", "响个喇叭"), "honk_horn", {}),
    (("开空调", "打开空调", "开暖风", "开冷风"), "auto_conditioning_start", {}),
    (("关空调", "关闭空调", "空调关掉"), "auto_conditioning_stop", {}),
    (("开后备箱", "打开后备箱"), "actuate_trunk", {}),
    (("关后备箱", "后备箱关上"), "close_trunk", {}),
    (("开哨兵", "开启哨兵"), "set_sentry_mode", {"on": True}),
    (("关哨兵", "关闭哨兵"), "set_sentry_mode", {"on": False}),
]
_COMPOUND_WORDS = ("顺便", "然后", "再", "并且", "而且", "还有", "同时", "加上", "以及",
                   "先", "接着", "最后")
# 任务清单用的中文名（与 user_runner 的 describe_action 保持同样读法）
_LABELS = {
    "door_lock": "锁车", "door_unlock": "解锁", "flash_lights": "闪灯", "honk_horn": "鸣笛",
    "auto_conditioning_start": "开启空调", "auto_conditioning_stop": "关闭空调",
    "actuate_trunk": "后备箱", "close_trunk": "关后备箱", "set_sentry_mode": "哨兵模式",
    "status": "查车况", "window_control": "车窗控制", "charge_start": "开始充电",
    "charge_stop": "停止充电", "wake_vehicle": "唤醒车辆", "set_climate_keeper_mode": "空调养护模式",
    "set_preconditioning_max": "强力预设", "remote_steering_wheel_heater_request": "方向盘加热",
    "remote_seat_heater_request": "座椅加热", "remote_seat_cooler_request": "座椅通风",
    "set_temps": "设定温度", "vehicle_reset": "车辆重置", "enable_keyless_driving": "影子模式",
}


def _action_label(action, params=None):
    """动作→中文任务名（用于“已收到N个任务”中间态）。"""
    params = params or {}
    label = _LABELS.get(action, action)
    if action == "set_temps":
        return f"{label} {params.get('driver_temp', 22)}°C"
    if action == "set_sentry_mode":
        return f"{label}: {'开' if params.get('on') else '关'}"
    if action == "remote_seat_heater_request":
        pos = "主驾" if params.get("seat_position", 0) == 0 else "副驾"
        return f"{label}: {pos} {params.get('level', 1)}档"
    if action == "remote_seat_cooler_request":
        pos = "主驾" if params.get("seat_position", 0) == 0 else "副驾"
        return f"{label}: {pos} {params.get('seat_cooler_level', 1)}档"
    if action == "remote_steering_wheel_heater_request":
        return f"{label}: {'开' if params.get('on') else '关'}"
    return label


# （2026-10-05 去重：此处曾有一份早期 _plan_fast 定义，与 1175 行生效版重复且缺粘连守卫，
#  按 Python 后定义覆盖前定义的规则它是死代码，已删除；粘连守卫补在生效版上。）
# 复合指令连接词（与 user_runner.resolve_actions 的判断保持一致，加上空格分隔场景）
_SEG_SPLIT_RE = re.compile(r"[，,、;；]|\s+|然后|接着|再|并且|而且|还有|同时|顺便|以及|加上|和|跟|与|及|＋|\+")


# 粘连动作二次切分：用户常把多个动作连着说（"闪灯鸣笛""解锁闪灯鸣笛"），
# 中间没有分隔符，整段会被当成一个片段 → 只执行第一个命中的动作、其余被吞。
# 这里按动作关键词在段内的出现位置把段切成多个片段。
_GLUED_ACT_PAT = re.compile(
    r"锁车|锁门|把车锁|车上锁|锁好车|解锁|开门|打开车门|"
    r"闪灯|闪一下灯|闪一闪|鸣笛|按喇叭|响个喇叭|按个喇叭|"
    r"打开空调|开空调|关空调|关闭空调|开暖风|开冷风|空调关掉|"
    r"打开后备箱|开后备箱|关后备箱|后备箱关上|"
    r"开启哨兵|开哨兵|关闭哨兵|关哨兵|"
    r"空调|温度"
)


def _split_glued(seg):
    """把"闪灯鸣笛"这类粘连段按动作关键词切成 ['闪灯','鸣笛']。
    只在切出 >=2 段时返回，否则原样返回 [seg]，避免误伤单动作。"""
    seg = (seg or "").strip()
    if not seg:
        return []
    hits = list(_GLUED_ACT_PAT.finditer(seg))
    if len(hits) < 2:
        return [seg]
    parts, seen = [], set()
    for i, m in enumerate(hits):
        # 每个动作关键词起点到下一个关键词起点之间算一段
        end = hits[i + 1].start() if i + 1 < len(hits) else len(seg)
        piece = seg[m.start():end].strip().strip("，,。.、!！?？ ").strip()
        if piece and piece not in seen:
            seen.add(piece)
            parts.append(piece)
    return parts if len(parts) >= 2 else [seg]


# 片段清洗：剥前缀填充词与尾部量词/语气词（"把车解锁"→"解锁"、"鸣笛一次"→"鸣笛"）
# 与 user_runner._norm_seg 保持同逻辑（解析层与执行层双解析器必须行为一致）
_FILLER_HEAD = ("帮我把", "把车", "帮个忙把", "帮我", "麻烦你", "麻烦", "帮忙", "请", "给", "让", "把", "将", "先")
_PARTICLE_TAIL = ("一次", "一下下", "两下", "三下", "一下", "呗", "呀", "啦", "哦", "哈", "嘛")


def _norm_seg(seg):
    seg = (seg or "").strip().strip("，,。.、!！?？ ").strip()
    changed = True
    while changed and seg:
        changed = False
        for f in _FILLER_HEAD:
            if seg.startswith(f) and len(seg) > len(f):
                seg = seg[len(f):].strip()
                changed = True
                break
        for p in _PARTICLE_TAIL:
            if seg.endswith(p) and len(seg) > len(p):
                seg = seg[:-len(p)].strip()
                changed = True
                break
    return seg


def _segment_compound(text):
    """把复合指令按分隔符/连接词切成片段，丢掉空片段与纯语气词。"""
    st = (text or "").strip().rstrip("，,。.、!！?？ ").strip()
    if not st:
        return []
    segs = [s.strip().strip("，,。.、!！?？ ").strip() for s in _SEG_SPLIT_RE.split(st)]
    noise = {"帮我", "请", "麻烦", "一下", "把", "让", "给", "车", "的", "个", "了", "一次", "两下", "三下"}
    out = []
    for s in segs:
        if not s:
            continue
        if s in noise:
            continue
        # 粘连动作二次切分（"闪灯鸣笛" → 闪灯 / 鸣笛），并逐段清洗前后缀
        for piece in _split_glued(s):
            piece = _norm_seg(piece)
            if piece and piece not in noise:
                out.append(piece)
    return out


# 复合片段温度解析：粗规则「空调开」会把「空调开到22°C」吞成不带温度的 auto_conditioning_start
# （语义路由同样可能路由到开空调）。片段含空调/温度+度数时优先 set_temps——判据与
# user_runner._local_full_match 一致（2026-10-22）。
_SEG_TEMP_RE = re.compile(r"(\d{2}(?:\.5)?|\d{1})(?:\s*度|\s*°C|\s*℃)")
_SEG_CN_TEMP_RE = re.compile(r"([零一二三四五六七八九十]{2,3}(?:点[零一二三四五六七八九])?)(?=\s*度|\s*°C|\s*℃)")
_SEG_CN_DIG = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _seg_set_temp(seg):
    """片段含空调/温度+度数 → set_temps 动作；不构成温度指令返回 None。"""
    if ("空调" not in seg and "温度" not in seg) or not any(w in seg for w in ("度", "°C", "℃")):
        return None

    def _c2n(s):
        if "点" in s:
            a, _, b = s.partition("点")
            v = _c2n(a)
            return None if v is None else v + (_SEG_CN_DIG.get(b[-1], 0) if b else 0) / 10
        if s == "十":
            return 10
        if "十" in s:
            a, _, b = s.partition("十")
            return (_SEG_CN_DIG.get(a, 1) if a else 1) * 10 + _SEG_CN_DIG.get(b, 0)
        return _SEG_CN_DIG.get(s)

    def _repl(m):
        v = _c2n(m.group(1))
        return ("%g" % v) if v is not None else m.group(1)

    t = _SEG_CN_TEMP_RE.sub(_repl, seg)
    m = _SEG_TEMP_RE.search(t)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return {"action": "set_temps", "params": {"driver_temp": v}} if 15 <= v <= 28 else None


def _route_one(seg):
    """单个片段本地解析：温度指令 → 高频规则 → 语义路由服务。命中返回 (action, params)，否则 None。"""
    _st = _seg_set_temp(seg)
    if _st:
        return _st["action"], dict(_st["params"])
    for keys, act, params in _FAST_RULES:
        if any(k in seg for k in keys):
            return act, dict(params)
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8791/route",
            data=json.dumps({"text": seg}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("route") == "action" and r.get("action"):
            act = str(r.get("action"))
            # 基础合法性：动作名须为 snake_case 标识符；真正的安全校验（FORBIDDEN/
            # ALLOWED_ACTIONS）由执行层 user_runner 把关，此处不重复定义。
            if act and re.fullmatch(r"[a-z][a-z0-9_]*", act):
                return act, (r.get("params") or {})
    except Exception:
        pass
    return None


def _plan_compound_local(text):
    """复合指令本地快解析。全部片段命中才返回动作清单；有任一未命中返回 None（退回 LLM）。"""
    segs = _segment_compound(text)
    if len(segs) < 2:
        return None
    actions = []
    for seg in segs:
        hit = _route_one(seg)
        if not hit:
            return None  # 有片段吃不准，整句交回 LLM，保证不误判
        act, params = hit
        actions.append({"action": act, "params": params})
    return actions


# ---- 进程内车况直连 + 30s 缓存（/api/state 专用，替代两个子进程冷启动） ----
_ST_CACHE_TTL = 30
_st_cache = {}                 # uid -> {"fields": {...}, "ts": float}
_st_cache_lock = threading.Lock()


_ST_FPOLL = {}                 # uid -> until_ts：指令后30s直连窗口（后备箱等遥测没有的字段靠全量快照确认）

def _invalidate_state_cache(uid):
    """车控命令执行后调用：下次 /api/state 强制拉真实车况（否则按钮状态可能最长 30s 不同步）。
    2026-10-11 同时开30s直连窗口：后备箱状态遥测不推，需全量快照确认执行结果。"""
    with _st_cache_lock:
        _st_cache.pop(uid, None)
        _ST_FPOLL[uid] = time.time() + 30


def _vehicle_data_fields(uid):
    """进程内直连特斯拉拉结构化车况。字段映射与 user_runner.json_state 完全对齐，
    额外补 rated_range（英里，与遥测同口径）和 outside_temp 供文本行使用。
    失败返回 None（调用方回退旧子进程链路，其可自愈刷新 token）。不主动唤醒车辆。"""
    acct = get_account(uid)
    tok = get_user_dir_tokens(uid)
    vehs = (acct or {}).get("vehicles") or []
    veh = vehs[0] if vehs else None
    vid = (veh or {}).get("id")
    vin = (veh or {}).get("vin")
    at = (tok or {}).get("access_token") or (tok or {}).get("id_token")
    if not (vid and vin and at):
        return None
    # [2026-10-06] 中国区必须显式带 endpoints（含 location_data）才下发定位数据
    # （对照 798491-collab/tesla 开源实现：裸 vehicle_data 在中国区不稳/缺定位）
    _endpoints = "location_data%3Bcharge_state%3Bdrive_state%3Bvehicle_state%3Bclimate_state%3Bgui_settings%3Bvehicle_config"
    # [2026-10-06] 中国区必须显式带 endpoints（含 location_data）才下发定位数据
    # （对照 798491-collab/tesla 开源实现：裸 vehicle_data 在中国区不稳/缺定位）
    # ⚠️ location_data 需要 token 带 vehicle_location scope：旧授权 token 会整体 403，
    # 自动降级裸请求重拉（车况保全，定位等重新授权后自动解锁）
    _endpoints = "location_data%3Bcharge_state%3Bdrive_state%3Bvehicle_state%3Bclimate_state%3Bgui_settings%3Bvehicle_config"
    _vd_url = f"{FLEET_URL}/api/1/vehicles/{vid}/vehicle_data?endpoints={_endpoints}"
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()  # 严格校验特斯拉证书
        with urllib.request.urlopen(urllib.request.Request(_vd_url, headers={"Authorization": f"Bearer {at}"}), timeout=15, context=ctx) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as _he:
        if _he.code in (401, 403):
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        f"{FLEET_URL}/api/1/vehicles/{vid}/vehicle_data",
                        headers={"Authorization": f"Bearer {at}"}), timeout=15, context=ctx) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None
    v = (d or {}).get("response") or {}
    cs = v.get("charge_state") or {}
    vs = v.get("vehicle_state") or {}
    cl = v.get("climate_state") or {}
    swh = cl.get("steering_wheel_heater")
    cooler_left = cl.get("climate_seat_cooling_front_left")
    if cooler_left is None:
        cooler_left = cl.get("seat_cooler_left")
    has_cooling = (cl.get("SeatVentEnabled") is True or cl.get("has_seat_cooling") is True
                   or cooler_left is not None)
    # 胎压：在 vehicle_state 段（顶层从来没有）。旧字段 tire_pressure{fl,fr,rl,rr}，
    # 新字段 tpms_pressure_fl/fr/rl/rr——归一成前端期望的 {fl,fr,rl,rr}
    _tp = vs.get("tire_pressure") or {}
    if not _tp:
        _fl, _fr = vs.get("tpms_pressure_fl"), vs.get("tpms_pressure_fr")
        _rl, _rr = vs.get("tpms_pressure_rl"), vs.get("tpms_pressure_rr")
        if not (_fl is None and _fr is None and _rl is None and _rr is None):
            _tp = {"fl": _fl, "fr": _fr, "rl": _rl, "rr": _rr}
    fields = {
        "online": True,
        "locked": bool(vs.get("locked")),
        "sentry_mode": bool(vs.get("sentry_mode")),
        "is_climate_on": bool(cl.get("is_climate_on")),
        "steering_wheel_heater": (int(swh) > 0) if swh is not None else None,
        "has_seat_cooling": has_cooling,
        "seat_cooler_level": int(cooler_left) if cooler_left is not None else 0,
        "charging": bool(cs.get("charging_state") in ("Charging", "Starting", "Complete")),
        "battery_level": cs.get("battery_level"),
        "charging_state": cs.get("charging_state"),
        "charge_limit_soc": cs.get("charge_limit_soc"),   # 充电上限（2026-10-08 第三屏滑杆用）
        "driver_temp_setting": cl.get("driver_temp_setting"),
        "inside_temp": cl.get("inside_temp"),
        "outside_temp": cl.get("outside_temp"),
        "rated_range": cs.get("battery_range"),
        "paint_color": vs.get("paint_color"),   # 车漆名（仪表盘3D初始颜色同步用）
        # 3D车模前后备箱真实状态（遥测不推，仅全量快照有；前端按钮以此确认，2026-10-11）
        "trunk_open": (None if vs.get("rt") is None else bool(vs.get("rt"))),
        "frunk_open": (None if vs.get("ft") is None else bool(vs.get("ft"))),
        # 胎压（2026-10-07 修层级：在 vehicle_state 里，不在顶层；旧固件 tire_pressure{fl..}，
        # 新固件 tpms_pressure_fl 等，归一成 {fl,fr,rl,rr} bar）
        "tire_pressure": _tp,
        "odometer": vs.get("odometer"),
        "charge_rate": cs.get("charge_rate"),
        "time_to_full_charge": cs.get("time_to_full_charge"),
        "charger_power": cs.get("charger_power"),
        # [2026-10-06] 定位字段透传（endpoints 带 location_data 后可用）：行驶回顾/地图用
        "latitude": (v.get("drive_state") or {}).get("latitude"),
        "longitude": (v.get("drive_state") or {}).get("longitude"),
        "heading": (v.get("drive_state") or {}).get("heading"),
        "speed": (v.get("drive_state") or {}).get("speed"),
    }
    try:   # VIN 档案：每次直连拉到车况就归档（颜色/配置/里程/胎压…跟着车走）
        vc = v.get("vehicle_config") or {}
        vehicle_archive_update(vin, dict(fields,
            exterior_color=vc.get("exterior_color"),
            car_type=vc.get("car_type"),
            trim_badging=vc.get("trim_badging"),
            wheel_type=vc.get("wheel_type")))
    except Exception:
        pass
    return fields


def _fields_to_text(fields):
    """fields -> 前端 renderStateCard 可解析的多行文本（与遥测分支同一格式）。"""
    battery = fields.get("battery_level")
    battery_s = f"{round(battery)}%" if battery is not None else "--%"
    range_miles = fields.get("rated_range")
    range_km = round(range_miles * 1.609) if range_miles is not None else None
    range_s = f"{range_km}km" if range_km is not None else "--km"
    locked = fields.get("locked")
    locked_s = "已锁" if locked else ("未锁" if locked is not None else "--")
    inside = fields.get("inside_temp")
    inside_s = f"{round(inside, 1)}°C" if inside is not None else "--°C"
    outside = fields.get("outside_temp")
    outside_s = f"{round(outside, 1)}°C" if outside is not None else "--°C"
    charging_state = fields.get("charging_state")
    charging_s = ("充电中" if charging_state in ("Charging", "Starting")
                   else ("已充满" if charging_state == "Complete" else "已停止"))
    is_climate_on = fields.get("is_climate_on")
    climate_s = "空调开启" if is_climate_on else ("空调关" if is_climate_on is not None else "--")
    # 充电文案：优先用采集端功率推导的 charging（cn 固件直流快充 ChargeState 仍 Idle）
    chg = fields.get("charging")
    if chg is True:
        charging_s = "充电中"
    elif chg is False:
        charging_s = "已充满" if charging_state == "Complete" else "已停止"
    else:
        charging_s = ("充电中" if charging_state in ("Charging", "Starting")
                       else ("已充满" if charging_state == "Complete" else "已停止"))
    return (f"电量 {battery_s} | 续航 {range_s} | 锁定 {locked_s} | 状态 在线\n"
            f"车内 {inside_s} / 车外 {outside_s} | 充电 {charging_s}\n{climate_s}")


# ---- SSE 客户端注册表与唤醒阶段广播（唤醒等待可视化） ----
_sse_clients = {}            # uid -> [handler, ...]
_sse_lock = threading.Lock()


def _sse_emit(uid, obj):
    """向该用户的全部 SSE 连接广播一条事件；写失败的连接就地剔除。"""
    payload = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
    with _sse_lock:
        conns = list(_sse_clients.get(uid) or [])
    for h in conns:
        try:
            h.wfile.write(payload)
            h.wfile.flush()
        except Exception:
            with _sse_lock:
                lst = _sse_clients.get(uid) or []
                if h in lst:
                    lst.remove(h)


def _inject_meta_fields(vin_full, fields):
    """VIN 档案元数据/兜底注入——/api/state 与 SSE 遥测帧共用（2026-10-07）。
    SSE 帧曾经不走注入：行车中每几秒推一帧把胎压/定位卡片数据冲掉
    （用户实测：行车中胎压消失、定位冻结在原地）。"""
    try:
        meta = vehicle_archive_read(vin_full) or {}
        add = {k: meta.get(k) for k in ("car_type", "trim_badging") if meta.get(k)}
        if vin_full:
            add["vin"] = vin_full
        # 胎压/里程兜底：行车遥测帧无胎压（cn 固件不推 tpms 值）时，下发档案最后已知值
        if meta.get("tire_pressure") and not (fields or {}).get("tire_pressure"):
            add["tire_pressure"] = meta["tire_pressure"]
            add["tire_pressure_stale"] = True
        if meta.get("odometer") and not (fields or {}).get("odometer"):
            add["odometer"] = meta["odometer"]
        # 位置：直连位置=实时带 now；遥测无 Location（scope 未授权）时用档案最后已知坐标
        if (fields or {}).get("latitude") is not None:
            # 位置时间戳=遥测帧采集时间（车休眠时帧是最后已知值，不能盖成响应时间，否则前端永远显示"刚刚更新"）
            _fts = (fields or {}).get("_frame_ts")
            try:
                add["location_ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(_fts))) if _fts else ""
            except Exception:
                add["location_ts"] = ""
        elif meta.get("latitude") is not None:
            add["latitude"] = meta["latitude"]
            add["longitude"] = meta["longitude"]
            if meta.get("heading") is not None:
                add["heading"] = meta["heading"]
            add["location_stale"] = True
            add["location_ts"] = meta.get("updated") or ""   # 兜底也用稳定值：now_iso 会让SSE签名持续抖动
        if add:
            fields = dict(fields or {})
            fields.update(add)
            fields.pop("_frame_ts", None)
    except Exception:
        pass
    return fields


def _state_sig(fields):
    """SSE 去重签名：对传感器微抖不敏感（温0.5°/坐标~100m/航向5°），帧时间戳不参与。
    前端每帧都重建整卡=第四屏闪跳，推送只应发生在用户可见显示值真变化时（2026-10-11）。"""
    try:
        f = dict(fields or {})
        f.pop("location_ts", None)
        for _k in ("inside_temp", "outside_temp"):
            if isinstance(f.get(_k), (int, float)):
                f[_k] = round(float(f[_k]) * 2) / 2
        for _k in ("latitude", "longitude"):
            if isinstance(f.get(_k), (int, float)):
                f[_k] = round(float(f[_k]), 3)
        if isinstance(f.get("heading"), (int, float)):
            f["heading"] = round(float(f["heading"]) / 5) * 5
        if isinstance(f.get("speed"), (int, float)):
            f["speed"] = round(float(f["speed"]))
        return json.dumps(f, sort_keys=True, ensure_ascii=False)
    except Exception:
        return json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)


def _scan_trip_points(udir, _dtx, hours_before=2, hours_after=22):
    """行程即时补算/旧缓存修复共用：扫轨迹月文件，取时间窗内原始点。"""
    tdir = os.path.join(udir, "tracks")
    pts = []
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            if not fn.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(tdir, fn), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            q = json.loads(line)
                            if q.get("ts", 0) >= (_dtx - timedelta(hours=hours_before)).timestamp() * 1000 \
                               and q.get("ts", 0) <= (_dtx + timedelta(hours=hours_after)).timestamp() * 1000:
                                pts.append(q)
                        except Exception:
                            pass
            except Exception:
                pass
    return pts


def probe_state_of(uid):
    """不唤醒的快速探测（模块级，供 SSE 观察者使用）：只读车辆元数据 state。
    返回 online/asleep/offline 原始值；失败返回 None。"""
    acct = get_account(uid)
    tok = get_user_dir_tokens(uid)
    if not acct or not tok:
        return None
    vehs = acct.get("vehicles") or []
    if not vehs:
        return None
    vid = vehs[0].get("id")
    at = tok.get("access_token") or tok.get("id_token")
    if not vid or not at:
        return None
    req = urllib.request.Request(
        f"{FLEET_URL}/api/1/vehicles/{vid}",
        headers={"Authorization": f"Bearer {at}"})
    try:
        import ssl as _ssl
        ctx = _ssl.create_default_context()  # 严格校验特斯拉证书
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return ((d or {}).get("response") or {}).get("state")
    except Exception:
        return None


def _wake_observer(uid, stop_ev):
    """命令执行期间观察车辆状态变化并经 SSE 推送（只读元数据，绝不主动唤醒——
    真正的唤醒由 user_runner 做，这里只负责把"车在醒"讲给前端看）。"""
    last = None
    probes = 0
    while not stop_ev.wait(6):
        # [fix 2026-09-26] 探测降频(6s)+上限12次+上线即停：原 2.5s 连打 60 次
        # 会触发特斯拉 429 限流，拖慢命令自身的读温/回读链路至 150s 超时。
        if probes >= 12:
            break
        with _sse_lock:
            if not (_sse_clients.get(uid)):
                continue  # 无人在看就不探测，省 API 调用
        st = probe_state_of(uid)
        probes += 1
        if not st:
            continue
        sk = str(st).lower()
        ph = "online" if sk == "online" else ("waking" if sk == "asleep" else "offline")
        if ph != last:
            _sse_emit(uid, {"type": "wake", "phase": ph})
            last = ph
            if ph == "online":
                break  # 车已醒，观察者使命结束，把 API 配额让给命令链路


def run_runner(uid, text):
    """跑 user_runner.py（子进程，避免污染主进程 run 模块状态）。
    注入 MULTI_BASE 环境变量：dev 版自动指向 dev 目录，生产版指向生产目录，
    user_runner.py 据此读写对应用户数据。
    """
    env = os.environ.copy()
    env["MULTI_BASE"] = BASE
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8", RUNNER, uid, text],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT, env=env,
        )
        out = (r.stdout or "").strip()
        if not out and r.returncode != 0:
            out = (r.stderr or "").strip() or f"执行失败(exit {r.returncode})"
        return out, None
    except subprocess.TimeoutExpired:
        return "", "⌛ 车辆唤醒或执行耗时过长，命令可能已生效；请稍后下拉查看车况，请勿重复下发"
    except Exception as e:
        return "", str(e)


def plan_runner(uid, text):
    """跑 user_runner.py 的 __plan__：只解析任务清单，不执行。返回 (dict|None, err)。"""
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8", RUNNER, uid, "__plan__ " + text],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=100,
        )
        out = (r.stdout or "").strip()
        if not out:
            return None, (r.stderr or "").strip() or "解析失败"
        d = json.loads(out)
        return d, None
    except Exception as e:
        return None, str(e)

# ============================================================
# 异步车控任务（休眠唤醒场景的“已收到N个任务 → 完成后更新”）
# 在线车不受影响：仍然同步一次拿到模板。只有探测到休眠/离线、
# 唤醒耗时长时才走异步：先回任务清单，后台线程执行，前端轮询取完成稿。
# ============================================================
# 每个用户同时只允许一个车控请求在跑：并发下发既容易撞服务商/特斯拉限流，
# 也会让前一条的结果被后一条覆盖（用户体感“卡很久”常常是排队+重试叠出来的）。
_uid_busy = {}
_uid_busy_lock = threading.Lock()


def _uid_acquire(uid):
    with _uid_busy_lock:
        if uid in _uid_busy:
            return False
        _uid_busy[uid] = time.time()
        return True


def _uid_release(uid):
    with _uid_busy_lock:
        _uid_busy.pop(uid, None)


# 任务清单中文名（“已收到N个任务”中间态用，与 user_runner.describe_action 同读法）
_LABELS = {
    "door_lock": "锁车", "door_unlock": "解锁", "flash_lights": "闪灯", "honk_horn": "鸣笛",
    "auto_conditioning_start": "开启空调", "auto_conditioning_stop": "关闭空调",
    "actuate_trunk": "后备箱", "close_trunk": "关后备箱", "set_sentry_mode": "哨兵模式",
    "status": "查车况", "window_control": "车窗控制", "charge_start": "开始充电",
    "charge_stop": "停止充电", "wake_vehicle": "唤醒车辆",
    "set_climate_keeper_mode": "空调养护模式", "set_preconditioning_max": "强力预设",
    "remote_steering_wheel_heater_request": "方向盘加热",
    "remote_seat_heater_request": "座椅加热", "remote_seat_cooler_request": "座椅通风",
    "set_temps": "设定温度", "enable_keyless_driving": "影子模式",
    "media_update": "媒体更新", "trigger_homelink": "车库门",
}


def _action_label(action, params=None):
    params = params or {}
    label = _LABELS.get(action, action)
    if action == "set_temps":
        return "{} {}C".format(label, params.get("driver_temp", 22)).replace("C", "°C")
    if action == "set_sentry_mode":
        return "{}: {}".format(label, "开" if params.get("on") else "关")
    if action == "remote_seat_heater_request":
        pos = "主驾" if params.get("seat_position", 0) == 0 else "副驾"
        return "{}: {} {}档".format(label, pos, params.get("level", 1))
    if action == "remote_seat_cooler_request":
        pos = "主驾" if params.get("seat_position", 0) == 0 else "副驾"
        return "{}: {} {}档".format(label, pos, params.get("seat_cooler_level", 1))
    if action == "remote_steering_wheel_heater_request":
        return "{}: {}".format(label, "开" if params.get("on") else "关")
    return label


# 高频单动作本地快解析：命中就不起解析子进程，任务清单毫秒级先回给用户
_FAST_RULES = [
    (("锁车", "锁门", "把车锁", "车上锁", "锁好车"), "door_lock", {}),
    (("解锁", "开门", "打开车门"), "door_unlock", {}),
    (("闪灯", "闪一下灯"), "flash_lights", {}),
    (("鸣笛", "按喇叭", "响个喇叭"), "honk_horn", {}),
    (("开空调", "打开空调", "开暖风", "开冷风"), "auto_conditioning_start", {}),
    (("关空调", "关闭空调", "空调关掉"), "auto_conditioning_stop", {}),
    (("开后备箱", "打开后备箱"), "actuate_trunk", {}),
    (("关后备箱", "后备箱关上"), "close_trunk", {}),
    (("开哨兵", "开启哨兵"), "set_sentry_mode", {"on": True}),
    (("关哨兵", "关闭哨兵"), "set_sentry_mode", {"on": False}),
]
_COMPOUND_WORDS = ("顺便", "然后", "再", "并且", "而且", "还有", "同时", "加上", "以及",
                   "先", "接着", "最后")


# 口语归一（2026-10-08）：「把车锁了/空调关了」这类了尾/倒序说法规则表覆盖不到，
# 曾落入已失效的 LLM 解析（key 失效 401）致高频指令全挂。入口统一改写为规范说法。
# 问句（吗/么/呢结尾）与否定句不归一——不把疑问/拒绝当动作执行。
_CANON_RES = (
    (re.compile(r"把?车锁了|锁上了|把车锁好"), "锁车"),
    (re.compile(r"^锁了$|^落锁$"), "锁车"),
    (re.compile(r"把?空调关了|空调关掉了|关掉空调"), "关空调"),
    (re.compile(r"把?空调开了"), "开空调"),
    (re.compile(r"后备箱关了"), "关后备箱"),
)
_CANON_Q_RE = re.compile(r"[吗么呢]\s*[?？!！]?\s*$")
def _canon_cmd(text):
    t = (text or "").strip()
    if not t or _CANON_Q_RE.search(t):
        return text
    if re.search(r"别|不要|不用|先不", t):
        return text
    for rx, rep in _CANON_RES:
        t = rx.sub(rep, t)
    return t

def _plan_fast(text):
    """命中高频单动作直接给出动作清单；含连接词/逗号/长句/多动作粘连一律返回 None。
    （本函数曾与 786 行重复定义，2026-10-05 已去重：保留本份并补粘连守卫）"""
    st = (text or "").strip().rstrip("。，,!！?？.、 ").strip()
    if not st or len(st) > 12:
        return None
    if any(w in st for w in _COMPOUND_WORDS) or "，" in st or "、" in st or "," in st:
        return None
    if any(w in st for w in ("别", "不要", "不用", "先不")):
        return None   # 否定句不触发动作（"别锁车"不得匹配锁车）
    # 无连接词的粘连句（"解锁车辆打开空调"）含多个动作：粘连切分出 >=2 段则交复合链路，
    # 否则单动作快答（2026-10-05 修复：此前粘连句抢先单答吞掉后续动作）
    if len(_split_glued(st)) >= 2:
        return None
    for keys, act, params in _FAST_RULES:
        if any(k in st for k in keys):
            return [{"action": act, "params": dict(params)}]
    return None


_REQUIRED_SCHEDULE_KEYS = ("id", "departure_at", "preheat_at", "remind_at", "created_at")


def _iso_or_none(v):
    """ISO 字符串 -> datetime；无效返回 None（避免文案处 KeyError/ValueError 500）。"""
    try:
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


_async_lock = threading.Lock()
_async_jobs = {}          # job_id -> {uid,text,status,tasks[n], reply, ts, error}
_async_seq = 0


_user_queues = {}
_user_queues_lock = threading.Lock()

def _process_user_queue(uid):
    while True:
        with _user_queues_lock:
            q = _user_queues.get(uid)
            if not q:
                _user_queues.pop(uid, None)
                _uid_release(uid)
                break
            job_id = q[0]

        with _async_lock:
            rec = _async_jobs.get(job_id)
            if rec:
                rec["status"] = "running"
        text = rec["text"] if rec else ""
        try:
            # 唤醒等待可视化：广播 job 开始 + 观察车辆状态（仅在有 SSE 观众时才轮询探测）
            _sse_emit(uid, {"type": "job", "phase": "start", "text": text})
            stop_obs = threading.Event()
            threading.Thread(target=_wake_observer, args=(uid, stop_obs),
                             daemon=True, name=f"wake-obs-{uid}").start()
            try:
                out, err = run_runner(uid, text)
            finally:
                stop_obs.set()
            with _async_lock:
                rec = _async_jobs.get(job_id)
                if rec:
                    rec["reply"] = out
                    rec["error"] = err
                    # 执行失败 → 主动探测是否虚拟钥匙没绑（周哥方案）；命中则触发绑定引导
                    if check_pair_after_failure(uid, out, err):
                        rec["pairing"] = True
                    elif reply_needs_pairing(out):
                        rec["pairing"] = True
                    rec["status"] = "done"
            if out:
                append_chat(uid, "car", out)
            else:
                append_chat(uid, "car", ("❌ " + err) if err else "❌ 车辆没有响应，可能在信号较弱的区域；指令已结束，可稍后再试一次")
            _invalidate_state_cache(uid)
            _sse_emit(uid, {"type": "job", "phase": "done"})
        except Exception as e:
            with _async_lock:
                rec = _async_jobs.get(job_id)
                if rec:
                    rec["error"] = str(e)
                    rec["status"] = "done"
            append_chat(uid, "car", "❌ " + str(e))

        with _user_queues_lock:
            q = _user_queues.get(uid)
            if q and q[0] == job_id:
                q.pop(0)
            if not q:
                _user_queues.pop(uid, None)
                _uid_release(uid)
                break

def _enqueue_job(uid, text, tasks):
    global _async_seq
    with _async_lock:
        _async_seq += 1
        job_id = f"job_{_async_seq}_{int(time.time())}"
        _async_jobs[job_id] = {
            "uid": uid, "text": text, "status": "queued",
            "tasks": tasks, "reply": None, "error": None, "ts": time.time(),
        }
    with _user_queues_lock:
        if uid not in _user_queues:
            _user_queues[uid] = []
        q = _user_queues[uid]
        q.append(job_id)
        pos = len(q)
        should_start = (pos == 1)

    if should_start:
        # 能拿到锁才启动队列线程；拿不到说明有同步执行在跑，稍后由同步路径接管队列
        if _uid_acquire(uid):
            threading.Thread(target=_process_user_queue, args=(uid,), daemon=True).start()
    return job_id, pos


def _cleanup_jobs():
    """只清理已结束且超过 10 分钟的 job；running 的永不清（否则前端轮询 404 永远转圈）。"""
    now = time.time()
    with _async_lock:
        for k in [k for k, v in _async_jobs.items()
                  if v.get("status") != "running" and now - v.get("ts", 0) > 600]:
            _async_jobs.pop(k, None)


# ---------- HTTP ----------
# ---------- 静态传输压缩（2026-10-03）：文本类文件 gzip，压缩结果按 (路径,mtime,大小) 缓存 ----------
_GZ_CACHE = {}

def _gz_data(fp, data):
    """返回 gzip 压缩结果（带缓存）；数据过小/过大返回 None（不值得压）。
    fp 为真实文件路径时按 (路径,mtime,大小) 缓存；合成键（非路径）直接按内容哈希缓存。"""
    if len(data) < 512 or len(data) > 8 * 1024 * 1024:
        return None
    if fp.startswith("_"):
        key = ("synthetic", fp, hashlib.md5(data).hexdigest())
    else:
        try:
            st = os.stat(fp)
            key = (fp, st.st_mtime, st.st_size)
        except OSError:
            return None
    if key not in _GZ_CACHE:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as f:
            f.write(data)
        if len(_GZ_CACHE) > 96:
            _GZ_CACHE.clear()
        _GZ_CACHE[key] = buf.getvalue()
    return _GZ_CACHE[key]

def _accepts_gzip(handler):
    return "gzip" in (handler.headers.get("Accept-Encoding") or "")

def _downscale_img(data, max_px, quality):
    """图片降采样到 ≤max_px 的 WebP；无收益/失败返回 None（原样直出）。需 pillow。
    缩略图 256/q80；素材大图 1024/q85（贴膜工作分辨率即 1024²，视觉无差、传输省 3~5 倍）。"""
    if _PILImage is None:
        return None
    try:
        im = _PILImage.open(io.BytesIO(data))
        w, h = im.size
        r = min(1.0, max_px / max(w, h))   # ≤max_px 不再缩像素，但仍转 WebP（格式重编码常常省一半以上）
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        if r < 1.0:
            im = im.resize((max(1, int(w * r)), max(1, int(h * r))))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=4)
        out = buf.getvalue()
        return out if len(out) < len(data) else None
    except Exception:
        return None

# ---- 社区素材缩略图降采样（装了 pillow 才生效；未装则原样直出）----
try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None
_THUMB_PATHS = None

def _thumb_path_set():
    """缩略图站内路径集合（_api_wrap_gallery 拉取清单时落盘 gallery_thumb_paths.json）。"""
    global _THUMB_PATHS
    if _THUMB_PATHS is None:
        try:
            with open(os.path.join(BASE, "gallery_thumb_paths.json"), "r", encoding="utf-8") as f:
                _THUMB_PATHS = set(json.load(f))
        except Exception:
            _THUMB_PATHS = set()
    return _THUMB_PATHS

class Handler(EmailAuthEndpoints, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    email_auth = EMAIL_AUTH

    def log_message(self, fmt, *args):
        pass

    # auth_http.EmailAuthEndpoints 需要的宿主回调：旧访问码档案（仅供 legacy 关联，无 UI 入口）
    def _load_email_codes(self):
        return load_codes()

    def finish_email_login(self, identity, link_current):
        uid = identity["uid"]
        email = identity["email"]
        name = f"用户 {email.split('@')[0][:10]}"
        token = make_session(email, name, uid)
        acct = get_account(uid)
        log_line("login", f"邮箱登录: {email} uid={uid} bound={bool(acct)}")
        return {
            "ok": True, "token": token, "name": name,
            "bound": bool(acct and acct.get("vehicles")),
            "vehicles": (acct or {}).get("vehicles", []),
        }

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # 大 JSON 响应 gzip（素材清单 500 项约 250KB→约 40KB）
        if len(body) > 2048 and _accepts_gzip(self):
            gz = _gz_data("_json_" + ctype + str(len(body)), body)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                body = gz
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _redirect_uri(self):
        """动态取当前域名的回调地址：朋友从哪个域名进来就用哪个域名回调，换地址不怕。"""
        proto = (self.headers.get("X-Forwarded-Proto") or "https").strip().lower()
        host = (self.headers.get("Host") or "").strip()
        if host:
            return f"{proto}://{host}/api/oauth/callback"
        return REDIRECT_URI

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        if length > 1024 * 1024:
            return b""
        return self.rfile.read(length)

    def _parse_json(self):
        raw = self._read_body()
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    CT_3D = {"html": "text/html; charset=utf-8", "js": "application/javascript",
             "wasm": "application/wasm", "pck": "application/octet-stream",
             "png": "image/png"}

    def do_HEAD(self):
        path = self.path.split("?")[0]
        if path.startswith("/wraps3d/"):
            name = path[len("/wraps3d/"):]
            if re.fullmatch(r"[A-Za-z0-9_./-]+", name or "") and ".." not in (name or ""):
                fp = os.path.join(STATIC_DIR, "wraps3d", *name.split("/"))
                if os.path.exists(fp):
                    log_line("wraps3d", f"HEAD {name} <- {self.client_address[0]} OK")
                    self.send_response(200)
                    self.send_header("Content-Type", self.CT_3D.get(name.rsplit(".", 1)[-1], "application/octet-stream"))
                    self.send_header("Content-Length", str(os.path.getsize(fp)))
                    _short = name.endswith(".html") or (name.endswith(".js") and not name.startswith("lib/"))
                    self.send_header("Cache-Control", "no-cache" if _short else "public, max-age=604800")
                    self.end_headers()
                    return
        self.send_response(404)
        self.end_headers()

    def _serve_wraps3d(self, name):
        """3D 静态文件（static/wraps3d/，含车模 GLB 与官方贴图）。缺失时 404。"""
        m = re.fullmatch(r"[A-Za-z0-9_./-]+", name or "")
        if not m or ".." in name:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        fp = os.path.join(STATIC_DIR, "wraps3d", *name.split("/"))
        ctype = self.CT_3D.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        try:
            size = os.path.getsize(fp)
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # lib/ 下 three 引擎文件内容不变、由 importmap ?v= 锁版本 → 长缓存（省每次切换 1.3MB 重拉）
        short = name.endswith(".html") or (name.endswith(".js") and not name.startswith("lib/"))
        self.send_header("Cache-Control", "no-cache" if short else "public, max-age=604800")
        # 文本类（html/js/json）且客户端接受：gzip 整体发送（lib 的 three.module.js 1.27MB→约330KB）
        gz = None
        if name.endswith((".html", ".js", ".json")) and _accepts_gzip(self):
            try:
                with open(fp, "rb") as f:
                    gz = _gz_data(fp, f.read())
            except OSError:
                gz = None
        if gz is not None:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(gz)))
            self.end_headers()
            self.wfile.write(gz)
            log_line("wraps3d", f"{name} <- {self.client_address[0]} 完成 gzip {len(gz)}B/{size}B")
            return
        self.send_header("Content-Length", str(size))
        self.end_headers()
        log_line("wraps3d", f"{name} <- {self.client_address[0]} 开始 {size}B")
        sent = 0
        try:
            with open(fp, "rb") as f:
                last_log = 0
                while sent < size:
                    chunk = f.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if sent - last_log >= 32 * 1024 * 1024:
                        last_log = sent
                        log_line("wraps3d", f"{name} <- {self.client_address[0]} 进度 {sent // 1048576}/{size // 1048576}MB")
            log_line("wraps3d", f"{name} <- {self.client_address[0]} 完成 {sent}B")
        except Exception as e:
            log_line("wraps3d", f"{name} <- {self.client_address[0]} 中断 {sent}B {type(e).__name__}")

    def _api_trips(self, query):
        """近 N 天行程（由轨迹点实时切段；点文件仅保留近一周，见方案 §6）。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        uid = s["uid"]
        try:
            days = max(1, min(31, int(query.get("days", ["7"])[0])))
        except Exception:
            days = 7
        cutoff = (time.time() - days * 86400) * 1000
        tdir = os.path.join(user_dir(uid), "tracks")
        pts = []
        if os.path.isdir(tdir):
            for fn in sorted(os.listdir(tdir)):
                if not fn.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(tdir, fn), encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                q = json.loads(line)
                                if q.get("ts", 0) >= cutoff:
                                    pts.append(q)
                            except Exception:
                                pass
                except Exception:
                    pass
        import track_split
        out = []
        for i, t in enumerate(track_split.split_trips(pts)):
            out.append({
                "id": "t%d" % i,
                # 统一规则 detail_id（出发时间命名）：点开时结算文件有则秒开、无则即时计算
                "detail_id": datetime.fromtimestamp(t["t0"] / 1000).strftime("%Y%m%d-%H%M%S"),
                "date": datetime.fromtimestamp(t["t0"] / 1000).strftime("%Y-%m-%d"),
                "time": datetime.fromtimestamp(t["t0"] / 1000).strftime("%H:%M"),
                "km": t["km"], "min": t["min"], "n": t["n"],
                "from": (regeo_name(t["start"]["lat"], t["start"]["lon"]) or "未知位置"),
                "to": (regeo_name(t["end"]["lat"], t["end"]["lon"]) or "未知位置"),
                "from_dist": regeo_district(t["start"]["lat"], t["start"]["lon"]),
                "to_dist": regeo_district(t["end"]["lat"], t["end"]["lon"]),
                "pts": track_split.downsample(
                    [["%.6f" % q["lon"], "%.6f" % q["lat"]] for q in t["pts"]], 100),
            })
        self._json(200, {"ok": True, "days": days, "trips": out})

    def _api_trip_detail(self, query):
        """单个行程详情 JSON（P 档结算数据：里程/用时/车速/电量/续航/温度/趋势）。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tid = (q.get("id") or [""])[0]
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", tid):
            self._json(400, {"ok": False, "error": "参数错误"})
            return
        fp = os.path.join(user_dir(s["uid"]), "trip_details", tid + ".json")
        if not os.path.isfile(fp):
            # 即时计算：从轨迹点结算该行程（结果缓存，下次秒开）
            try:
                _dt = datetime.strptime(tid, "%Y%m%d-%H%M%S")
            except ValueError:
                self._json(400, {"ok": False, "error": "参数错误"})
                return
            pts = _scan_trip_points(user_dir(s["uid"]), _dt)
            import track_split
            detail = None
            for t in track_split.split_trips(pts):
                if abs(t["t0"] - _dt.timestamp() * 1000) < 60000:
                    mv = [p for p in t["pts"] if p.get("gear") in ("D", "R", "N") or (p.get("spd") or 0) > 1]
                    if len(mv) < 2:
                        break
                    detail = track_split.build_detail(mv, tid=tid)   # 全字段构建器（旧版只补基础字段，能耗/电量/温度缓存后永久缺失 2026-10-11）
                    break
            if not detail:
                self._json(404, {"ok": False, "error": "行程不存在"})
                return
            if detail.get("start_coord"):
                detail["start_addr"] = regeo_name(detail["start_coord"][0], detail["start_coord"][1]) or detail["start_addr"]
            if detail.get("end_coord"):
                detail["end_addr"] = regeo_name(detail["end_coord"][0], detail["end_coord"][1]) or detail["end_addr"]
            ddir = os.path.join(user_dir(s["uid"]), "trip_details")
            os.makedirs(ddir, exist_ok=True)
            with open(os.path.join(ddir, tid + ".json"), "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False)
        try:
            # 旧缓存地址升级：此前 regeo_name 只到市/区级（如「宝山区」），命中短地址就重算回写
            with open(fp, encoding="utf-8") as f:
                dcache = json.load(f)
            _short_addr = lambda a: (not str(a or '').strip()) or bool(re.search(r'\d{1,3}\.\d{3,}', str(a or ''))) or bool(re.fullmatch(r"[^市县区]{0,3}[市县区]", str(a or "")))   # 坐标串也算坏地址（2026-10-13）
            if dcache.get("start_coord") and (_short_addr(dcache.get("start_addr")) or _short_addr(dcache.get("end_addr"))):
                dcache["start_addr"] = regeo_name(dcache["start_coord"][0], dcache["start_coord"][1]) or dcache.get("start_addr")
                dcache["end_addr"] = regeo_name(dcache["end_coord"][0], dcache["end_coord"][1]) or dcache.get("end_addr")
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(dcache, f, ensure_ascii=False)
            # 旧缓存能量/电量字段补齐：早期即时补算只存了基础字段，有轨迹点就重算（2026-10-11）
            if ("consumed_kwh" not in dcache or "wh_per_km" not in dcache) and dcache.get("id"):   # 旧结算文件无净口径能耗也重算
                try:
                    import track_split as _ts2
                    _dt2 = datetime.strptime(dcache["id"], "%Y%m%d-%H%M%S")
                    _pts2 = _scan_trip_points(user_dir(s["uid"]), _dt2)
                    for _t2 in _ts2.split_trips(_pts2):
                        if abs(_t2["t0"] - _dt2.timestamp() * 1000) < 60000:
                            _mv2 = [p for p in _t2["pts"] if p.get("gear") in ("D", "R", "N") or (p.get("spd") or 0) > 1]
                            if len(_mv2) >= 2:
                                _d2 = _ts2.build_detail(_mv2, tid=dcache["id"])
                                for _k, _v in _d2.items():
                                    if _k not in ("start_addr", "end_addr"):
                                        dcache[_k] = _v
                                with open(fp, "w", encoding="utf-8") as f:
                                    json.dump(dcache, f, ensure_ascii=False)
                        break
                except Exception:
                    pass
        except Exception:
            pass
        with open(fp, encoding="utf-8") as f:
            self._send(200, f.read(), "application/json; charset=utf-8")

    def _api_wrap_save(self):
        """喷漆记忆：保存当前工坊状态（车型/颜色/填充图/贴纸+3D合成图），按用户隔离。
        ?beacon=1 路径：sendBeacon 无法带自定义头，token 经 query 传入（pagehide 抢救用）。"""
        s = self._auth()
        if not s:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _bt = (q.get("token") or [""])[0]
            s = check_session(_bt) if _bt else None
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            if ln <= 0 or ln > 8 * 1024 * 1024:
                raise ValueError("size")
            body = json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "请求无效或过大"})
            return
        st = body.get("state")
        if not isinstance(st, dict) or not st.get("tplKey"):
            self._json(400, {"ok": False, "error": "状态无效"})
            return
        # 未定制（无图案无贴纸且默认白）不入库——防旧页面自动存白膜盖掉真车色
        if not st.get("fill") and not st.get("stickers") and \
           str(st.get("color", "")).lower() in ("#f8fafc", "#ffffff", "none", ""):
            self._json(200, {"ok": True, "skipped": True})
            return
        # 2026-10-06 按车身拆存档（给 Y 喷的膜不能套到 3 上）：工坊 tplKey → 仪表 3D 车身
        TPL2BODY = {"modely": "modely", "model3": "model3",
                    "model3-2024-base": "model3h", "model3-2024-performance": "model3p",
                    "modely-2025-performance": "modely25b", "modely-l": "modelyl"}
        _body = str(st.get("body") or TPL2BODY.get(str(st.get("tplKey")), "")).strip()
        if not re.fullmatch(r"[a-z0-9]+", _body or ""):
            _body = ""
        fn = ("wrap_state_%s.json" % _body) if _body else "wrap_state.json"
        p = os.path.join(user_dir(s["uid"]), fn)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"state": st, "ts": now_iso()}, f, ensure_ascii=False)
        os.replace(tmp, p)
        self._json(200, {"ok": True, "body": _body or None})

    def _api_wrap_get(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        # 2026-10-06 ?body= 按车身取存档（仪表/工坊只恢复当前车的喷漆）
        # 无 body：取该用户全部存档里 ts 最新的——前端不再等车型就绪，打开即拉（省 10s 白等）
        _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _body = (_q.get("body") or [""])[0].strip()
        if not re.fullmatch(r"[a-z0-9]+", _body or ""):
            _body = ""
        d = {}
        if _body:
            try:
                with open(os.path.join(user_dir(s["uid"]), "wrap_state_%s.json" % _body), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
        else:
            import glob as _g2
            _cands = list(_g2.glob(os.path.join(user_dir(s["uid"]), "wrap_state_*.json")))
            _legacy = os.path.join(user_dir(s["uid"]), "wrap_state.json")
            if os.path.exists(_legacy):
                _cands.append(_legacy)
            _best, _bt = None, -1
            for _f in _cands:
                try:
                    _d2 = json.load(open(_f, encoding="utf-8"))
                    _t2 = (_d2.get("state") or {}).get("ts", 0)
                    if _t2 > _bt:
                        _best, _bt = _d2, _t2
                except Exception:
                    pass
            d = _best or {}
        try:
            st = d.get("state")
            # 未定制状态（无图案无贴纸且默认白）不下发——否则车模永远白膜
            if st and not st.get("fill") and not st.get("stickers") and                str(st.get("color", "")).lower() in ("#f8fafc", "#ffffff", "none", ""):
                st = None
            # 档案挂 VIN：本账号无有效喷漆时，借同 VIN 账号的最新档案（喷漆跟车走，换设备/换码不丢）
            if not st:
                try:
                    _vin = ((get_account(s["uid"]).get("vehicles") or [{}])[0].get("vin") or "").upper()
                    if _vin:
                        import glob as _glob
                        _bt = 0
                        _pat = ("wrap_state_%s.json" % _body) if _body else "wrap_state*.json"
                        for _f in _glob.glob(os.path.join(USERS_DIR, "*", _pat)):
                            _ouid = _f.split(os.sep)[-2]   # 2026-10-06 修：[-3] 取到的是 'users'，借档从未命中（上线即坏）
                            if _ouid == s["uid"]:
                                continue
                            try:
                                _a = get_account(_ouid)
                                if not _a or not any(str(v.get("vin", "")).upper() == _vin for v in (_a.get("vehicles") or [])):
                                    continue
                                _w = json.load(open(_f, encoding="utf-8")).get("state")
                                if _w and _w.get("ts", 0) > _bt:
                                    st = _w
                                    _bt = _w.get("ts", 0)
                            except Exception:
                                pass
                except Exception:
                    pass
            self._json(200, {"ok": True, "state": st, "body": _body or None})
        except Exception:
            self._json(200, {"ok": True, "state": None, "body": _body or None})

    def _api_wrap_clear(self):
        """恢复原车漆：删除该用户的喷漆存档。?body= 指定车身；无 body 清全部（含按车身拆分的档）。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _body = (_q.get("body") or [""])[0].strip()
        if not re.fullmatch(r"[a-z0-9]+", _body or ""):
            _body = ""
        removed = []
        import glob as _g3
        if _body:
            _p = os.path.join(user_dir(s["uid"]), "wrap_state_%s.json" % _body)
            if os.path.exists(_p):
                os.remove(_p)
                removed.append(os.path.basename(_p))
        else:
            for _f in _g3.glob(os.path.join(user_dir(s["uid"]), "wrap_state*.json")):
                try:
                    os.remove(_f)
                    removed.append(os.path.basename(_f))
                except Exception:
                    pass
        self._json(200, {"ok": True, "removed": removed})

    def _api_vehicle_color(self):
        """真车漆色：VIN 档案优先（跟着车走，含懒迁移的历史记录），回落 uid 历史档案。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录"})
            return
        vin = ""
        try:
            acct = get_account(s["uid"]) or {}
            vin = ((acct.get("vehicles") or [{}])[0].get("vin") or "")
        except Exception:
            vin = ""
        meta = vehicle_archive_read(vin) if vin else {}
        ec = meta.get("exterior_color")
        if not ec:
            p = os.path.join(user_dir(s["uid"]), "vehicle_meta.json")
            try:
                ec = json.load(open(p, encoding="utf-8")).get("exterior_color")
            except Exception:
                pass
        self._json(200, {"ok": True, "exterior_color": ec, "vin": vin or None,
                         "odometer": meta.get("odometer"),
                         "paint_color": meta.get("paint_color"),
                         "car_type": meta.get("car_type"),
                         "trim_badging": meta.get("trim_badging")})

    def _serve_file(self, rel, ctype):
        fp = os.path.join(STATIC_DIR, rel)
        try:
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if _accepts_gzip(self):
                gz = _gz_data(fp, data)
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Vary", "Accept-Encoding")
                    data = gz
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        if path == "/manifest.json":
            self._serve_file("manifest.json", "application/manifest+json; charset=utf-8")
            return
        if path == "/sw.js":
            self._serve_file("sw.js", "application/javascript; charset=utf-8")
            return
        if path == "/vendor/qrcode.js":
            self._serve_file("vendor/qrcode.js", "application/javascript; charset=utf-8")
            return
        if path == "/diag-geo.html":
            self._serve_file("diag-geo.html", "text/html; charset=utf-8")
            return
        if path == "/api/trip_detail":
            self._api_trip_detail(query)
            return
        if path == "/test-trip.html":
            self._serve_file("test-trip.html", "text/html; charset=utf-8")
            return
        if path == "/test/trip_detail_latest.json":
            self._serve_file("test/trip_detail_latest.json", "application/json; charset=utf-8")
            return
        if path == "/api/trips":
            self._api_trips(query)
            return
        if path == "/api/wrap/get":
            self._api_wrap_get()
            return
        if path == "/api/vehicle/color":
            self._api_vehicle_color()
            return
        if path.startswith("/icons/") and (path.endswith(".png") or path.endswith(".jpg")):
            ext = "png" if path.endswith(".png") else "jpeg"
            self._serve_file(path.lstrip("/"), f"image/{ext}")
            return
        if path == "/api/oauth/callback":
            self._oauth_callback(query)
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "ts": now_iso()})
            return
        if path == "/api/wrap/templates":
            self._api_wrap_templates()
            return
        if path == "/api/wrap/gallery":
            self._api_wrap_gallery()
            return
        if path == "/api/wrap/gallery_image":
            self._api_wrap_gallery_image(query)
            return
        if path.startswith("/wraps/") and path.endswith(".png"):
            fn = path[len("/wraps/"):]
            if re.fullmatch(r"[A-Za-z0-9_-]+\.png", fn) or re.fullmatch(r"presets/[A-Za-z0-9_-]+\.png", fn):
                self._serve_file("wraps/" + fn, "image/png")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if path.startswith("/wraps3d/"):
            self._serve_wraps3d(path[len("/wraps3d/"):])
            return
        if path == "/api/stream":
            self._api_stream(query)
            return
        if path == "/api/state":
            self._api_state()
            return
        if path == "/api/account":
            self._api_account()
            return
        if path == "/api/settings":
            self._api_settings_get()
            return
        if path == "/api/dashbtns":
            self._api_dashbtns_get()
            return
        if path == "/api/notifications":
            self._api_notifications()
            return
        if path == "/api/announcement":
            self._api_announcement()
            return
        if path == "/api/chat/history":
            self._api_chat_history()
            return
        if path == "/api/chat/job":
            self._api_chat_job()
            return
        if path == "/api/schedules":
            self._api_schedules()
            return
        if path == "/.well-known/appspecific/com.tesla.3p.public-key.pem":
            self._serve_tesla_public_key()
            return
        if path == "/.well-known/appspecific/com.tesla.3p.public-key":
            self._serve_tesla_public_key_b64()
            return
        if path == "/api/push/public_key":
            self._api_push_public_key()
            return
        if path == "/api/monitor/overview":
            self._api_monitor_overview()
            return
        if path == "/api/monitor/events":
            self._api_monitor_events()
            return
        if path == "/monitor":
            self._api_monitor_page()
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/login":
            self._api_login()
        elif path == "/api/email/send":
            self._api_email_send()
        elif path == "/api/email/verify":
            self._api_email_verify()
        elif path == "/api/charge_limit":
            self._api_charge_limit()
        elif path == "/api/wrap/rembg":
            self._api_wrap_rembg()
        elif path == "/api/wrap/save":
            self._api_wrap_save()
        elif path == "/api/wrap/clear":
            self._api_wrap_clear()
        elif path == "/api/logout":
            self._api_logout()
        elif path == "/api/unbind":
            self._api_unbind()
        elif path == "/api/feedback":
            self._api_feedback()
        elif path == "/api/oauth/start":
            self._api_oauth_start()
        elif path == "/api/bind":
            self._api_bind()
        elif path == "/api/chat":
            self._api_chat()
        elif path == "/api/control/probe":
            self._api_control_probe()
        elif path == "/api/route":
            self._api_route()
        elif path == "/api/dashbtns":
            self._api_dashbtns_save()
        elif path == "/api/settings":
            self._api_settings_save()
        elif path == "/api/transcribe":
            self._api_transcribe()
        elif path == "/api/schedule/confirm":
            self._api_schedule_confirm()
        elif path == "/api/schedule/cancel":
            self._api_schedule_cancel()
        elif path == "/api/wake":
            self._api_wake()
        elif path == "/api/push/subscribe":
            self._api_push_subscribe()
        elif path == "/api/push/unsubscribe":
            self._api_push_unsubscribe()
        elif path == "/api/push/test":
            self._api_push_test()
        elif path == "/api/monitor/set_name":
            self._api_monitor_set_name()
        elif path == "/api/mp/login":
            self._api_mp_login()
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        return check_session(h[7:].strip())

    def _rate_ok(self, code):
        now = time.time()
        q = _rate.setdefault(code, [])
        q = [t for t in q if now - t < RATE_WINDOW]
        _rate[code] = q
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True

    def _transcribe_whisper(self, audio_path):
        """调常驻 whisper 服务(8790) 转写，返回文本。"""
        try:
            req = urllib.request.Request(
                WHISPER_URL,
                data=json.dumps({"path": audio_path}).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                return d.get("text", "")
        except Exception:
            return ""

    # ---- 小程序登录（骨架，2026-10-04 预埋；凭据走环境变量 MP_WX_APPID/MP_WX_SECRET）----
    def _api_mp_login(self):
        """wx.login 的 code 换 openid 并落到 uid。未配置 AppID/Secret 时为 mock 模式：
        用 code 派生伪 openid，供小程序壳无凭据联调；配置后自动走 code2session 真换。
        uid 以 openid 哈希生成，档案落 friends-codes.json（与 H5 自助注册同构），
        openid 本身不回传客户端。"""
        ip = self.client_address[0] if self.client_address else "?"
        if not self._rate_ok("mplogin:" + ip):
            self._json(429, {"ok": False, "error": "尝试过于频繁，请稍后再试"})
            return
        data = self._parse_json()
        if not data or not str(data.get("code", "")).strip():
            self._json(400, {"ok": False, "error": "缺少 code"})
            return
        code = str(data["code"]).strip()
        is_mock = not (MP_WX_APPID and MP_WX_SECRET)
        openid = None
        if is_mock:
            openid = "mock_" + hashlib.sha1(("wtask:" + code).encode("utf-8")).hexdigest()[:16]
        else:
            try:
                u = ("https://api.weixin.qq.com/sns/jscode2session?appid=%s&secret=%s"
                     "&js_code=%s&grant_type=authorization_code"
                     % (MP_WX_APPID, MP_WX_SECRET, urllib.parse.quote(code)))
                with urllib.request.urlopen(u, timeout=10) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                openid = d.get("openid")
                if not openid:
                    log_line("mplogin", f"code2session 失败: {d}")
            except Exception as e:
                log_line("mplogin", f"code2session 异常: {e}")
        if not openid:
            self._json(402, {"ok": False, "error": "微信登录失败，请重试"})
            return
        uid = "MP" + hashlib.sha1(openid.encode("utf-8")).hexdigest()[:10].upper()
        codes = load_codes()
        if uid not in codes:
            codes[uid] = {"name": "小程序用户 " + uid[-4:], "enabled": True}
            save_codes(codes)
            log_line("mplogin", f"新建小程序用户档案: {uid}")
        log_line("mplogin", f"小程序登录: {uid} mock={is_mock}")
        self._json(200, {"ok": True, "uid": uid, "mock": is_mock})

    # ---- 登录 ----
    def _api_login(self):
        # 访问码/设备码登录已下线（2026-10-17）：登录统一走邮箱验证码 /api/email/send + /api/email/verify
        self._json(410, {"ok": False, "error": "访问码登录已下线，请使用邮箱验证码登录"})

    # ---- OAuth 开始 ----
    def _api_oauth_start(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        state = secrets.token_urlsafe(16)
        # 把 uid 绑进 state（后续 callback 用 uid 恢复会话，而不是 token——callback 是无鉴权回调）
        state_full = f"{s['uid']}:{state}"
        # 存 pending state（防伪造）
        try:
            pf = os.path.join(BASE, "oauth-pending.json")
            pend = {}
            if os.path.exists(pf):
                with open(pf, "r", encoding="utf-8") as f:
                    pend = json.load(f)
            pend[state] = {"uid": s["uid"], "ts": time.time()}
            with open(pf, "w", encoding="utf-8") as f:
                json.dump(pend, f, ensure_ascii=False)
        except Exception:
            pass
        url = oauth_start_url(state_full, self._redirect_uri())
        log_line("oauth", f"{s['name']}({s['uid']}) 开始绑定")
        self._json(200, {"ok": True, "url": url})

    # ---- 贴膜工坊 ----
    def _api_wrap_templates(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        acct = get_account(s["uid"]) or {}
        vin = ((acct.get("vehicles") or [{}])[0]).get("vin", "")
        rec = wrap_decode_vin(vin)
        lst = [{"key": k, "name": v["name"], "url": "/" + v["file"],
                "recommended": (k == rec)} for k, v in WRAP_TEMPLATES.items()]
        # 推荐的排最前
        lst.sort(key=lambda x: (not x["recommended"], x["key"]))
        presets = [
            {"key": "camo_forest", "name": "丛林迷彩", "url": "/wraps/presets/camo_forest.png"},
            {"key": "camo_desert", "name": "沙漠迷彩", "url": "/wraps/presets/camo_desert.png"},
            {"key": "carbon", "name": "碳纤维", "url": "/wraps/presets/carbon.png"},
            {"key": "cyberpunk", "name": "赛博朋克", "url": "/wraps/presets/cyberpunk.png"},
            {"key": "sunset", "name": "落日", "url": "/wraps/presets/sunset.png"},
            {"key": "starry", "name": "星空", "url": "/wraps/presets/starry.png"},
        ]
        self._json(200, {"ok": True, "vin": vin, "recommended": rec,
                         "templates": lst, "presets": presets,
                         "skinFamily": sorted(SKIN_MODEL_FAMILY.get(rec, set())),
                         # 按模板 key 下发素材过滤标签：每键只吃「对着这张官方模板画的」素材，
                         # 不按 family 混装（老款3 曾混入 64% 新款3 设计=不适配主诉）
                         "families": WRAP_SKIN_FILTER})

    def _api_wrap_gallery(self):
        """tesla-skin.com 社区素材：按下载量取前 500（服务端排序+缓存 10 分钟）。
        2026-10-03 起免登录开放（内容为公开社区代理，无用户数据）；上传/导出/绑定仍需登录。"""
        global _skin_cache
        now = time.time()
        if _skin_cache[0] and now - _skin_cache[0] < 600:
            self._json(200, {"ok": True, "items": _skin_cache[1]})
            return
        # 落盘缓存：进程重启后直接用，避免每次冷启动都拉 822KB 全量
        cache_file = os.path.join(BASE, "wrap_gallery_cache.json")
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                _disk = json.load(f)
            if _disk.get("items") and now - _disk.get("ts", 0) < 86400:
                _skin_cache = [_disk.get("ts", now), _disk["items"]]
                self._json(200, {"ok": True, "items": _skin_cache[1]})
                return
        except Exception:
            pass
        try:
            req = urllib.request.Request("https://www.tesla-skin.com/api/skins?limit=300", headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
                "Accept": "application/json", "Referer": "https://www.tesla-skin.com/"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            # 网络失败：有旧盘缓存就用旧的
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    _disk = json.load(f)
                if _disk.get("items"):
                    _skin_cache = [_disk.get("ts", now), _disk["items"]]
                    self._json(200, {"ok": True, "items": _skin_cache[1], "stale": True})
                    return
            except Exception:
                pass
            self._json(502, {"ok": False, "error": f"素材社区暂时不可用: {e}"})
            return
        items = []
        thumb_paths = []
        for x in d:
            if not x.get("image"):
                continue
            tp = x.get("thumbnail") or ""
            thumb_paths.append(tp)
            items.append({
                "name": (x.get("name") or "未命名")[:30],
                "model": x.get("model") or "",
                "downloads": x.get("downloads") or 0,
                "likes": x.get("likes") or 0,
                "author": (x.get("author") or "")[:20],
                "thumb": "/api/wrap/gallery_image?path=" + urllib.parse.quote(tp, safe=""),
                "full": "/api/wrap/gallery_image?path=" + urllib.parse.quote(x.get("image") or "", safe=""),
            })
        items.sort(key=lambda x: -x["downloads"])
        items = items[:500]
        try:
            with open(os.path.join(BASE, "gallery_thumb_paths.json"), "w", encoding="utf-8") as f:
                json.dump(thumb_paths, f)
            global _THUMB_PATHS
            _THUMB_PATHS = None  # 下次代理请求重载
        except Exception:
            pass
        _skin_cache[0] = now
        _skin_cache[1] = items
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"ts": now, "items": items}, f, ensure_ascii=False)
        except Exception:
            pass
        self._json(200, {"ok": True, "items": items})

    _img_cache_lock = __import__("threading").Lock()  # 串行化同图并发回源（防缓存目录重复拉取风暴）

    def _api_wrap_gallery_image(self, query):
        """图片代理（带磁盘缓存）：仅允许 /api/image/ 开头的站内路径（防 SSRF）。
        首拉回源落盘；命中后按用途出图——缩略图 256/q80、素材大图 1024/q85 的 WebP 变体
        （tesla-skin 的"缩略图"有些实际 200~466KB；贴膜工作分辨率 1024²，视觉无差）。
        免认证：<img> 标签带不了 Authorization；内容为公开社区图片。"""
        p = (query.get("path") or [""])[0]
        if not p.startswith("/api/image/"):
            self._send(400, "bad path", "text/plain; charset=utf-8")
            return
        import hashlib
        key = hashlib.md5(p.encode("utf-8")).hexdigest()
        cache_dir = os.path.join(BASE, "gallery_img_cache")
        in_thumbs = p in _thumb_path_set()
        use_pil = _PILImage is not None
        cpath = os.path.join(cache_dir, key + ".data")
        mpath = os.path.join(cache_dir, key + ".ctype")
        vpath = None
        vpx, vq = 0, 85
        if use_pil:
            if in_thumbs:
                vpath, vpx, vq = os.path.join(cache_dir, key + ".t256.data"), 256, 80
            else:
                vpath, vpx, vq = os.path.join(cache_dir, key + ".f1024.data"), 1024, 85
        data = None
        ctype = "image/webp"
        from_variant = False
        # ① 降采样变体命中 → 直接出
        if vpath:
            try:
                with open(vpath, "rb") as f:
                    data = f.read()
                ctype = "image/webp"
                from_variant = True
            except OSError:
                data = None
        # ② 原图缓存
        if data is None:
            try:
                with open(cpath, "rb") as f:
                    data = f.read()
                with open(mpath, "r", encoding="utf-8") as f:
                    ctype = f.read().strip() or "image/webp"
            except OSError:
                data = None
        # ③ 都没有 → 回源拉原图并落盘
        if data is None:
            req = urllib.request.Request("https://www.tesla-skin.com" + p, headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
                "Referer": "https://www.tesla-skin.com/"})
            try:
                with self._img_cache_lock:
                    try:
                        with open(cpath, "rb") as f:
                            data = f.read()
                        with open(mpath, "r", encoding="utf-8") as f:
                            ctype = f.read().strip() or "image/webp"
                    except OSError:
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            data = resp.read(2 * 1024 * 1024)
                            ctype = resp.headers.get("Content-Type") or "image/webp"
                        try:
                            os.makedirs(cache_dir, exist_ok=True)
                            tmp = cpath + "." + str(time.time())
                            with open(tmp, "wb") as f:
                                f.write(data)
                            os.replace(tmp, cpath)
                            with open(mpath, "w", encoding="utf-8") as f:
                                f.write(ctype)
                        except Exception:
                            pass
            except Exception as e:
                self._send(502, "upstream error", "text/plain; charset=utf-8")
                return
        # ④ 需要变体且手里还是原图 → 生成（并发写盘），改发变体
        if vpath and not from_variant and data:
            tdata = _downscale_img(data, vpx, vq)
            if tdata:
                try:
                    tmp = vpath + "." + str(time.time())
                    with open(tmp, "wb") as f:
                        f.write(tdata)
                    os.replace(tmp, vpath)
                except Exception:
                    pass
                data, ctype = tdata, "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=604800")   # 内容寻址（路径哈希）不可变 → 7 天
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api_wrap_rembg(self):
        """抠图代理：收 multipart/png 图片 → rembg 处理 → 返回透明底 PNG。
        rembg 未安装/失败时返回明确错误（前端给引导），绝不静默。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8 * 1024 * 1024:
            self._json(400, {"ok": False, "error": "图片为空或超过 8MB"})
            return
        data = self.rfile.read(length)
        try:
            from rembg import remove
        except ImportError:
            self._json(501, {"ok": False, "error": "服务端未安装抠图组件（rembg），请使用纯色/渐变模式，或联系管理员安装"})
            return
        try:
            out = remove(data)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"抠图失败: {e}"})

    # ---- OAuth 回调 ----
    def _oauth_callback(self, query):
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        err = (query.get("error") or [""])[0]
        if err or not code:
            msg = f"授权失败: {err or '缺少 code'}"
            log_line("oauth", msg)
            self._show_html(msg)
            return
        # 校验 state
        uid = None
        try:
            parts = state.split(":", 1)
            if len(parts) == 2:
                uid = parts[0]
        except Exception:
            pass
        if not uid:
            self._show_html("无效的 state，请重新绑定")
            return
        if not oauth_state_consume(uid, parts[1]):
            log_line("oauth", f"state 校验失败（伪造或过期）: uid={uid}")
            self._show_html("state 已过期或无效，请回到网页重新发起绑定")
            return
        tok, terr = oauth_exchange(code, self._redirect_uri())
        if terr or not tok or not tok.get("access_token"):
            log_line("oauth", f"{uid} 换 token 失败: {terr or tok}")
            self._show_html(f"换取 token 失败: {terr or '未知错误'}")
            return
        save_user_tokens(uid, tok)
        # 拉车辆
        vlist, verr = list_vehicles(tok.get("access_token"))
        log_line("oauth", f"{uid} 换 token 成功，车辆列表: {verr or len(vlist or [])} 辆")
        if verr:
            self._show_html(f"token 已保存，但拉取车辆失败: {verr}。请返回网页手动刷新")
            return
        # 未选车：存临时车辆列表，等用户选；若只有一辆自动绑定
        acct = get_account(uid) or {"vehicles": []}
        if len(vlist) == 1:
            acct["vehicles"] = vlist
            acct.pop("pending_vehicles", None)
            save_account(uid, acct)
            log_line("oauth", f"{uid} 自动绑定唯一车辆 {vlist[0].get('vin')}")
            self._show_html(f"绑定成功！你的「{vlist[0].get('display_name') or '特斯拉'}」已可控制。请回到网页刷新。", ok=True)
        else:
            acct["pending_vehicles"] = vlist
            if not acct.get("vehicles"):
                save_account(uid, acct)
            log_line("oauth", f"{uid} 多辆车待选: {len(vlist)}")
            self._show_html(f"授权成功！检测到 {len(vlist)} 辆车，请回到网页选择要控制的那辆。", ok=True)

    def _show_html(self, msg, ok=False):
        body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>绑定结果</title>
<style>body{{font-family:-apple-system,sans-serif;background:#ededed;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:12px;padding:28px;max-width:360px;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}}
.icon{{font-size:44px}} .msg{{font-size:15px;color:#333;margin:14px 0 20px;line-height:1.6;word-break:break-all}}
a{{display:inline-block;background:#07c160;color:#fff;text-decoration:none;padding:11px 26px;border-radius:8px;font-size:15px}}</style>
</head><body><div class="card">
<div class="icon">{'✅' if ok else '⚠️'}</div>
<div class="msg">{msg}</div>
<a href="/">返回网页</a></div></body></html>"""
        self._send(200, body, "text/html; charset=utf-8")

    # ---- 账号状态 ----
    # ---- 仪表盘中键(三个快捷键) ----
    def _api_dashbtns_get(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        self._json(200, {"ok": True, "buttons": load_dashbtns(s["uid"], default_dashbtn_ids())})

    def _api_dashbtns_save(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._parse_json() or {}
        arr = data.get("buttons")
        if not isinstance(arr, list) or not arr:
            self._json(400, {"ok": False, "error": "格式错误：需要 buttons 数组"})
            return
        valid = {p["id"] for p in DASHBTN_POOL}
        ids = [str(x).strip() for x in arr[:3] if isinstance(x, str) and x in valid]
        if len(ids) < 3:
            self._json(400, {"ok": False, "error": "需要三个有效动作"})
            return
        save_dashbtns(s["uid"], ids)
        log_line("dashbtns", f"{s['name']}({s['uid']}) 更新中键 {','.join(ids)}")
        self._json(200, {"ok": True, "buttons": ids})

    # ---- 用户设置 ----
    def _api_settings_get(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        self._json(200, {"ok": True, "settings": load_settings(s["uid"])})

    def _api_settings_save(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._parse_json() or {}
        patch = data.get("settings")
        if not isinstance(patch, dict):
            self._json(400, {"ok": False, "error": "格式错误：需要 settings 对象"})
            return
        cur = save_settings(s["uid"], patch)
        log_line("settings", f"{s['name']}({s['uid']}) 更新设置 {list(patch.keys())}")
        self._json(200, {"ok": True, "settings": cur})

    def _api_account(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        acct = get_account(s["uid"]) or {}
        veh = acct.get("vehicles") or []
        pending = acct.get("pending_vehicles") or []
        # 方案A：已绑定但未认领同 VIN 老账号资产时，自动认领（幂等，claimed_vin 标记）
        try:
            if veh and veh[0].get("vin") and acct.get("claimed_vin") != veh[0].get("vin"):
                _claim_assets_by_vin(s["uid"], veh[0].get("vin"))
                acct = get_account(s["uid"]) or {}
        except Exception:
            pass
        self._json(200, {
            "ok": True,
            "name": s["name"],
            "bound": bool(veh),
            "vehicles": veh,
            "pending_vehicles": pending,
        })

    def _api_control_probe(self):
        """探测控制授权（虚拟钥匙）状态。POST /api/control/probe。
        已 ready → 快速返回不用醒车；否则调 user_runner 无损 door_lock 探针归类。
        """
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        uid = s["uid"]
        acct = get_account(uid) or {}
        veh = acct.get("vehicles") or []
        if not veh:
            self._json(200, {"ok": True, "bound": False, "ready": False, "needs_pairing": False, "state": "unbound"}); return
        vin = veh[0].get("vin")
        # 已记 ready 且缓存未过期 → 快速返回（不醒车）
        if pair_cache_ready(uid):
            self._json(200, {"ok": True, "bound": True, "ready": True, "needs_pairing": False,
                             "state": "ready", "vin": vin, "pair_url": PAIR_URL}); return
        ready, needs = probe_user_pairing(uid)
        if ready:
            self._json(200, {"ok": True, "bound": True, "ready": True, "needs_pairing": False,
                             "state": "ready", "vin": vin, "pair_url": PAIR_URL}); return
        if needs:
            self._json(200, {"ok": True, "bound": True, "ready": False, "needs_pairing": True,
                             "state": "needs_pairing", "vin": vin, "pair_url": PAIR_URL}); return
        self._json(200, {"ok": True, "bound": True, "ready": False, "needs_pairing": False,
                         "state": "unknown", "vin": vin, "pair_url": PAIR_URL,
                         "hint": "车离线或正在唤醒，暂无法判定；稍后可重试"})

    # ---- 选车绑定 ----
    def _api_bind(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._parse_json() or {}
        vin = str(data.get("vin", "")).strip().upper()
        acct = get_account(s["uid"])
        if not acct:
            self._json(400, {"ok": False, "error": "尚未完成特斯拉授权，请先点「绑定我的特斯拉」"})
            return
        pending = acct.get("pending_vehicles") or []
        target = None
        for v in pending:
            if v.get("vin", "").upper() == vin:
                target = v
                break
        if not target:
            self._json(400, {"ok": False, "error": "该车辆不在授权列表里，请重新绑定"})
            return
        acct["vehicles"] = [target]
        acct.pop("pending_vehicles", None)
        save_account(s["uid"], acct)
        log_line("bind", f"{s['name']}({s['uid']}) 绑定车辆 {vin} {target.get('display_name')}")
        # 绑定建档：一次性拉全量 vehicle_data，车身色/车型配置立刻入库（不唤醒；
        # 车在睡则跳过，后续由 watcher / 刷新车况两条通道补齐）
        try:
            _tok = (get_user_dir_tokens(s["uid"]) or {}).get("access_token")
            if _tok and target.get("id"):
                bind_profile_snapshot(s["uid"], target.get("id"), _tok)
        except Exception:
            pass
        # 方案A：绑定完成即认领同 VIN 老账号资产（皮肤/设置迁移、老账号停用）
        try:
            _claim_assets_by_vin(s["uid"], vin)
        except Exception:
            pass
        self._json(200, {"ok": True, "vehicle": target})

    # ---- 语音转写 ----
    def _api_transcribe(self):
        """接收浏览器上传的音频（webm/ogg/wav），调 whisper 转写返回文本。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._read_body()
        if not data:
            self._json(400, {"ok": False, "error": "没有音频数据"})
            return
        if len(data) > MAX_AUDIO:
            self._json(413, {"ok": False, "error": "音频过大"})
            return
        ctype = self.headers.get("Content-Type", "")
        ext = "wav"
        if "webm" in ctype:
            ext = "webm"
        elif "ogg" in ctype:
            ext = "ogg"
        # 每次请求独立临时文件（uuid），防止并发上传互相覆盖；用完即删
        tmp = os.path.join(BASE, "tmp_web_audio_%s_%s.%s"
                           % (int(time.time() * 1000), secrets.token_hex(4), ext))
        try:
            with open(tmp, "wb") as f:
                f.write(data)
        except Exception:
            self._json(500, {"ok": False, "error": "保存音频失败"})
            return
        try:
            text = self._transcribe_whisper(tmp)
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        if not text:
            self._json(200, {"ok": False, "text": "", "error": "转写失败（whisper 服务不可用）"})
            return
        log_line("transcribe", f"{s['name']}: 语音={text}")
        self._json(200, {"ok": True, "text": text})

    # ---- 车控对话 ----
    def _api_chat(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        if not self._rate_ok(s["code"]):
            self._json(429, {"ok": False, "error": "操作太频繁，请稍后再试"})
            return
        data = self._parse_json()
        if not data:
            self._json(400, {"ok": False, "error": "参数错误"})
            return
        text = str(data.get("text", "")).strip()
        if not text:
            self._json(400, {"ok": False, "error": "消息不能为空"})
            return
        if len(text) > MAX_TEXT:
            self._json(400, {"ok": False, "error": "消息太长"})
            return
        # 口语归一：了尾/倒序说法改写为规范说法再进解析（问句/否定句原样）
        text = _canon_cmd(text)
        # 解绑咨询（关键词直达，先于绑定检查：未绑定时也给出正确答复）
        _no_kw = ("不用解绑", "别解绑", "不要解绑", "不解绑", "无需解绑", "先不解绑", "暂不解绑", "别取消", "不用取消绑定")
        _ub_kw = ("解绑", "解除绑", "取消绑", "撤销授权", "取消授权", "收回授权", "解除授权",
                  "移除绑", "删除绑", "删掉绑", "退出绑", "不绑了", "不想绑", "不要绑", "不想绑定",
                  "不想用了", "怎么解除绑定", "如何解除绑定")
        if any(x in text for x in _no_kw):
            reply = "好的，保持当前绑定，不解绑。"
            append_chat(s["uid"], "me", text); append_chat(s["uid"], "car", reply)
            self._json(200, {"ok": True, "reply": reply}); return
        if any(x in text for x in _ub_kw):
            acct0 = get_account(s["uid"])
            if acct0 and acct0.get("vehicles"):
                vn = acct0["vehicles"][0].get("display_name") or "你的特斯拉"
                reply = (f"解绑「{vn}」方法：点页面左上角的 V-TASK 图标 → 弹出的确认框里点「确认解绑」，即可完成解绑。"
                         "解绑后服务器会删除这台车的授权，无法再查看或控制；想再用需要重新走特斯拉授权绑定。")
            else:
                reply = "你当前没有绑定车辆，无需解绑。想用车的话：点页面顶部「开始绑定」→ 在特斯拉官方页面授权即可。"
            append_chat(s["uid"], "me", text); append_chat(s["uid"], "car", reply)
            self._json(200, {"ok": True, "reply": reply}); return

        acct = get_account(s["uid"])
        if not acct or not acct.get("vehicles"):
            self._json(200, {"ok": False, "reply": "你还没绑定车辆，请先点「绑定我的特斯拉」"})
            return

        # 常用地点：只有用户明确说“把这里设为X”且本次请求带定位才保存。
        pm = re.search(r"把这里设为(.{1,12})", text)
        if pm:
            origin = data.get("location") if isinstance(data.get("location"), dict) else None
            if not origin:
                reply = "请先点击📍获取当前位置，再说“把这里设为公司”"
            else:
                pname = pm.group(1).strip("的了 。，,.！!")
                raw_lon, raw_lat = float(origin.get("longitude")), float(origin.get("latitude"))
                gcj_lon, gcj_lat = wgs84_to_gcj02(raw_lon, raw_lat)
                save_place(s["uid"], pname, {"longitude": gcj_lon, "latitude": gcj_lat, "coord_type": "GCJ-02"})
                log_line("place", f"???? {pname}: WGS84=({raw_lon:.6f},{raw_lat:.6f}) -> GCJ02=({gcj_lon:.6f},{gcj_lat:.6f})")
                reply = f"已记住“{pname}”的位置，仅保存在你的账户里"
            append_chat(s["uid"], "me", text); append_chat(s["uid"], "car", reply)
            self._json(200, {"ok": True, "reply": reply}); return

        # 定时用语（用户 2026-09-03 确认：不要确认按钮，识别到即创建并直接输出定时模板）
        is_cancel = bool(re.search(r'(?:取消|不要|别|撤销|删(?:除|掉)?).*(?:定时|用车|预约|计划|行程|代办)|(?:定时|用车|预约|计划|行程|代办).*(?:取消|不要|别|撤销|删(?:除|掉)?)', text))
        if is_cancel:
            task = schedule.cancel_latest(s["uid"], text)
            if task:
                append_chat(s["uid"], "me", text)
                append_chat(s["uid"], "car", f"已取消定时用车：{task.get('text', '')}")
                self._json(200, {"ok": True, "reply": f"已取消定时用车：{task.get('text', '')}"})
            else:
                append_chat(s["uid"], "me", text)
                append_chat(s["uid"], "car", "目前没有待取消的定时用车")
                self._json(200, {"ok": True, "reply": "目前没有待取消的定时用车"})
            return

        if any(x in text for x in ("有哪些定时", "什么定时", "定时计划", "用车计划", "预约计划", "待执行计划", "查看计划", "查看定时")):
            tasks = schedule.list_pending(s["uid"])
            if tasks:
                rows = ["当前有 %d 个待执行计划：" % len(tasks)]
                for task in tasks:
                    dt = datetime.fromisoformat(task["departure_at"])
                    ph = datetime.fromisoformat(task["preheat_at"])
                    rm = datetime.fromisoformat(task["remind_at"])
                    rows.append(f"{dt.strftime('%m月%d日 %H:%M')} 用车（{ph.strftime('%H:%M')} 预热，{rm.strftime('%H:%M')} 提醒）")
                reply = "\n".join(rows)
            else:
                reply = "目前没有待执行的定时用车"
            append_chat(s["uid"], "me", text)
            append_chat(s["uid"], "car", reply)
            self._json(200, {"ok": True, "reply": reply})
            return
        task, serr = schedule.book(text)
        if task:
            # 路线起点：车辆当前位置优先（2026-10-07 用户定稿：车辆定位替代手机定位），
            # 车辆位置未知时回退手机定位。route_info 内部做 WGS84→GCJ02。
            car_o = car_origin_wgs84(s["uid"])
            origin = car_o or (data.get("location") if isinstance(data.get("location"), dict) else None)
            origin_from = "car" if car_o else "phone"
            try:
                olon, olat = float(origin.get("longitude")), float(origin.get("latitude"))
                if not (-180 <= olon <= 180 and -90 <= olat <= 90):
                    raise ValueError("坐标越界")
                destination = task.get("dest")
                place = load_place(s["uid"], destination) if destination else None
                if destination:
                    route = route_info(olon, olat, destination, (place or {}).get("location") if place else None, city="上海")
                    if route:
                        task.update(route)
                        task["origin"] = {"longitude": olon, "latitude": olat}
                        task["origin_from"] = origin_from
                        # 出发时间=用户说的时间。路线只用于展示距离/预计行驶时间，
                        # 不参与预热和提醒计算；预热/提醒保持出发前固定提前量（15/3分钟）。
            except (TypeError, ValueError):
                pass
            schedule.confirm(s["uid"], task)
            dt = datetime.fromisoformat(task["departure_at"])
            ph = datetime.fromisoformat(task["preheat_at"])
            rm = datetime.fromisoformat(task["remind_at"])
            lines = [
                f"📅 已创建定时用车：{task.get('text', '')}",
                f"出发：{dt.strftime('%m月%d日')} {dt.strftime('%H:%M')}",
            ]
            if task.get("distance_km") and task.get("duration_min"):
                lines.append(f"🚗 路程：约 {task['distance_km']} 公里｜预计 {task['duration_min']} 分钟"
                             + ("（起点=车辆当前位置）" if task.get("origin_from") == "car" else ""))
                if task.get("suggest_leave_by"):
                    lines.append(f"🕒 建议最晚 {datetime.fromisoformat(task['suggest_leave_by']).strftime('%H:%M')} 出发（含15分钟缓冲）")
            if not task.get("distance_km"):
                lines.append("📍 未获取车辆/手机定位，暂未计算路程；定时用车仍已正常创建")
            lines.extend([f"⏰ {ph.strftime('%H:%M')} 预热 ｜ {rm.strftime('%H:%M')} 提醒", "📍 到点自动执行，取消直接说「取消定时」"])
            reply = "\n".join(lines)
            append_chat(s["uid"], "me", text)
            append_chat(s["uid"], "car", reply)
            self._json(200, {"ok": True, "reply": reply})
            return
        log_line("chat", f"{s['name']}({s['uid']}): {text}")
        uid = s["uid"]
        # 若当前用户正忙，不拒绝、不让用户稍等，直接收下进入排队队列！
        with _user_queues_lock:
            in_flight = bool(_user_queues.get(uid))
        if in_flight:
            job_id, pos = _enqueue_job(uid, text, tasks=["待执行指令"])
            _cleanup_jobs()
            append_chat(s["uid"], "me", text)
            wait_msg = f"⏳ 已收下排队中（第 {pos-1} 位等待），上一条执行完自动开始…"
            self._json(200, {"ok": True, "mode": "queued", "job": job_id,
                             "pos": pos, "reply": wait_msg})
            return
        # 以下所有 return 路径必须释放该锁（异步路径由后台线程结束时释放）
        # 先解析（不执行）再决定同步/异步：
        # - 复合指令：无论车在不在线都走异步。多命令叠加、可能还要唤醒，同步干等体验最差。
        # - 单动作：车明确在线才同步一次拿终稿（快）；休眠/离线走异步，避免“正在处理”干等。
        # 高频单动作先走本地快解析，省掉解析子进程启动开销。
        fast = _plan_fast(text)
        plan = ({"ok": True, "n": 1,
                 "tasks": [_action_label(fast[0]["action"], fast[0].get("params"))],
                 "compound": False} if fast else None)
        _plan_src = "fast" if fast else None
        if plan is None:
            # 复合指令本地快解析（拆分+逐段语义路由，实测 3~23ms）
            _cl = _plan_compound_local(text)
            if _cl:
                plan = {"ok": True, "n": len(_cl),
                        "tasks": [_action_label(a["action"], a.get("params")) for a in _cl],
                        "compound": len(_cl) > 1, "_actions": _cl}
                _plan_src = "local"
        perr = None
        if plan is None:
            plan, perr = plan_runner(uid, text)
            _plan_src = "llm"
        if perr or not plan or not plan.get("ok"):
            # 解析失败：退回同步（可能给出提示文案或引导）
            out, err = run_runner(s["uid"], text)
            if err:
                reply = "❌ " + err
            else:
                reply = out or "❌ 未识别到可执行的车辆指令"
            append_chat(s["uid"], "me", text)
            append_chat(s["uid"], "car", reply)
            # 执行失败 → 主动探测是否虚拟钥匙没绑（周哥方案）
            _pairflag = check_pair_after_failure(uid, reply, err) or reply_needs_pairing(reply)
            _invalidate_state_cache(uid)
            _uid_release(uid)
            self._json(200, {"ok": True, "reply": reply, "mode": "sync", "pairing": _pairflag})
            return
        tasks = plan.get("tasks") or []
        n = plan.get("n") or len(tasks)
        compound = bool(plan.get("compound")) or n > 1
        log_line("chat", f"[{s['name']}] plan={_plan_src or 'llm'} n={n}")
        # 不再同步探测车辆状态（8s 超时太慢），直接走异步；后台执行时自动处理休眠唤醒
        _asleep = False
        if compound or _asleep:
            job_id, pos = _enqueue_job(uid, text, tasks)
            _cleanup_jobs()
            append_chat(s["uid"], "me", text)
            self._json(200, {"ok": True, "mode": "async", "job": job_id,
                             "n": n, "tasks": tasks, "sleep": _asleep, "pos": pos})
            return

        # 在线单动作：异步入队，前端立即显示"✅ 已发送"，后台执行完把车况补上（体感直出）。
        # 不再同步干等特斯拉 API 往返；用户点下瞬间就有反馈。
        job_id, pos = _enqueue_job(uid, text, tasks)
        _cleanup_jobs()
        append_chat(s["uid"], "me", text)
        self._json(200, {"ok": True, "mode": "async", "job": job_id,
                         "n": n, "tasks": tasks, "sleep": False, "pos": pos})
        return

    # ---- 定时任务/推送 ----
    def _api_schedule_confirm(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        data = self._parse_json() or {}
        task = data.get("schedule")
        if not isinstance(task, dict) or not all(task.get(k) for k in _REQUIRED_SCHEDULE_KEYS):
            self._json(400, {"ok": False, "error": "定时任务参数错误"}); return
        # 只接收服务器生成的字段，不能让客户端注入 uid/状态/执行时间字段。
        clean = {k: task[k] for k in ("id","text","departure_at","preheat_at","remind_at","created_at") if k in task}
        clean.update({"preheat_status":"pending", "remind_status":"pending", "status":"pending"})
        schedule.confirm(s["uid"], clean)
        _ph, _rm = _iso_or_none(clean.get("preheat_at")), _iso_or_none(clean.get("remind_at"))
        _tm = f"\n⏰ {_ph.strftime('%H:%M')} 预热 ｜ {_rm.strftime('%H:%M')} 提醒" if (_ph and _rm) else ""
        self._json(200, {"ok": True, "reply": f"📅 已创建定时用车：{clean.get('text','')}{_tm}"})

    def _api_charge_limit(self):
        """充电上限直调（2026-10-08 第三屏滑杆）：不进聊天历史。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        acct = get_account(s["uid"])
        if not acct or not acct.get("vehicles"):
            self._json(400, {"ok": False, "error": "未绑定车辆"})
            return
        data = self._parse_json() or {}
        try:
            pct = max(50, min(100, int(data.get("percent"))))
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "参数错误"})
            return
        vin = acct["vehicles"][0].get("vin")
        import user_runner   # 懒加载：签名命令代理在此模块（含 ensure_proxy/token 刷新）
        ok, msg = user_runner.proxy_command(s["uid"], vin, "set_charge_limit", {"percent": pct})
        if ok:
            self._json(200, {"ok": True, "limit": pct})
        else:
            self._json(200, {"ok": False, "error": msg or "车未响应"})

    def _api_route(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        data = self._parse_json() or {}
        origin = data.get("location") if isinstance(data.get("location"), dict) else {}
        destination = str(data.get("destination", "")).strip()
        try:
            lon, lat = float(origin.get("longitude")), float(origin.get("latitude"))
            if not (-180 <= lon <= 180 and -90 <= lat <= 90): raise ValueError()
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "定位坐标无效"}); return
        route = route_info(lon, lat, destination, city="上海")
        if not route:
            self._json(200, {"ok": False, "error": "路线查询失败"}); return
        self._json(200, {"ok": True, **route})

    def _api_logout(self):
        h = self.headers.get("Authorization", "")
        token = h[7:].strip() if h.startswith("Bearer ") else ""
        if token:
            with _session_lock:
                _sessions.pop(token, None)
            _session_save()
        self._json(200, {"ok": True})

    def _api_feedback(self):
        """接收测试用户问卷反馈并存储至 feedbacks.json（允许未登录/匿名提交）"""
        s = self._auth()
        uid = s.get("uid", "anonymous") if s else "anonymous"
        data = self._parse_json()
        if not data:
            self._json(400, {"ok": False, "error": "缺少数据"})
            return

        fb_file = os.path.join(BASE, "feedbacks.json")
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "time": now_str,
            "uid": uid,
            "speed": data.get("speed", ""),
            "features": data.get("features", []),
            "reliability": data.get("reliability", ""),
            "rating": data.get("rating", 5),
            "comments": str(data.get("comments", "")).strip()
        }

        feedbacks = []
        if os.path.exists(fb_file):
            try:
                feedbacks = json.load(io.open(fb_file, encoding="utf-8"))
                if not isinstance(feedbacks, list):
                    feedbacks = []
            except Exception:
                feedbacks = []

        feedbacks.append(entry)
        try:
            with io.open(fb_file, "w", encoding="utf-8") as f:
                json.dump(feedbacks, f, ensure_ascii=False, indent=2)
            self._json(200, {"ok": True, "msg": "反馈已收到，非常感谢！"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})

    def _api_unbind(self):
        """一键解绑：删除该账号持有的特斯拉授权与绑定/定时数据（收回钥匙）。"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        uid = s["uid"]
        removed = []
        for fn in ("tokens.json", "account.json", "schedules.json", "notifications.json"):
            fp = os.path.join(user_dir(uid), fn)
            try:
                if os.path.exists(fp):
                    os.remove(fp)
                    removed.append(fn)
            except Exception as e:
                log_line("unbind", f"删除 {fn} 失败: {e}")
        log_line("unbind", f"{s['name']}({uid}) 一键解绑，删除: {','.join(removed) or '无'}")
        self._json(200, {"ok": True, "removed": removed})

    def _api_schedules(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        self._json(200, {"ok": True, "items": schedule.list_pending(s["uid"])})

    def _api_schedule_cancel(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        task = schedule.cancel_latest(s["uid"])
        self._json(200, {"ok": True, "reply": f"已取消定时用车：{task.get('text','')}" if task else "目前没有待取消的定时用车"})

    def _api_notifications(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        self._json(200, {"ok": True, "items": schedule.list_notifications(s["uid"])})

    def _api_announcement(self):
        """返回服务端最新公告（新版本指引）。无公告时 ok=False，前端不打扰。"""
        try:
            p = os.path.join(BASE, "announcement.json")
            if not os.path.exists(p):
                self._json(200, {"ok": False})
                return
            with open(p, "r", encoding="utf-8") as f:
                ann = json.load(f)
            if not ann or not ann.get("version"):
                self._json(200, {"ok": False})
                return
            self._json(200, {
                "ok": True,
                "version": ann["version"],
                "title": ann.get("title", "新版本上线"),
                "action": ann.get("action", ""),
                "content": ann.get("content", []),
            })
        except Exception as e:
            log_line("announcement", f"读取公告失败: {e}")
            self._json(200, {"ok": False})

    def _api_chat_history(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        messages = load_chat(s["uid"])
        self._json(200, {"ok": True, "messages": messages})

    def _api_chat_job(self):
        """查询异步车控任务状态（休眠唤醒场景用）。GET /api/chat/job?job=ID"""
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"}); return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job_id = (q.get("job") or [""])[0]
        if not job_id:
            self._json(400, {"ok": False, "error": "缺少任务 ID"}); return
        with _async_lock:
            rec = _async_jobs.get(job_id)
        if not rec or rec.get("uid") != s["uid"]:
            self._json(404, {"ok": False, "error": "任务不存在或已过期"}); return
        if rec["status"] != "done":
            self._json(200, {"ok": True, "status": rec["status"], "n": rec.get("n", len(rec.get("tasks", [])))}) ; return
        reply = rec.get("reply")
        err = rec.get("error")
        pairing = bool(rec.get("pairing"))
        with _async_lock:
            _async_jobs.pop(job_id, None)
        if err:
            self._json(200, {"ok": True, "status": "done", "reply": "❌ " + err, "pairing": pairing})
        else:
            self._json(200, {"ok": True, "status": "done", "reply": reply or "❌ 无响应，请稍后再试", "pairing": pairing})

    # ---- 特斯拉公钥配对 well-known 路径（Fleet API 配对要求）----
    def _serve_tesla_public_key(self):
        # 特斯拉要求公钥在 /.well-known/appspecific/com.tesla.3p.public-key.pem 可访问
        # 并支持 Range 请求（bytes=0-200）
        pk_path = os.path.join(BASE, "tesla-public-key.pem")
        if not os.path.exists(pk_path):
            self._json(404, {"ok": False, "error": "公钥文件不存在"})
            return
        try:
            with io.open(pk_path, "rb") as f:
                data = f.read()
        except Exception:
            self._json(500, {"ok": False, "error": "读取公钥失败"})
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                start = int(a) if a else 0
                end = int(b) if b else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", "application/x-pem-file")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
            except Exception:
                self._json(500, {"ok": False, "error": "Range 处理失败"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-pem-file")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_tesla_public_key_b64(self):
        """无 .pem 后缀的标准路径：Tesla App 配对校验拉取的是 base64(DER) 单行（2026-10-06 补 cpolar 域名 404）。"""
        pk_path = os.path.join(BASE, "tesla-public-key.pem")
        if not os.path.exists(pk_path):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            import re as _re
            pem = io.open(pk_path, "r", encoding="utf-8").read()
            b64 = _re.sub(r"-----[^-]+-----|\s", "", pem)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b64)))
            self.end_headers()
            self.wfile.write(b64.encode())
        except Exception:
            self._send(500, "error", "text/plain; charset=utf-8")

    # ---- 推送相关 ----
    def _api_push_public_key(self):
        import push_sender
        pk = push_sender.get_public_key()
        if not pk:
            self._json(500, {"ok": False, "error": "推送服务未就绪"})
            return
        self._json(200, {"ok": True, "publicKey": pk})

    def _api_push_subscribe(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._parse_json()
        if not data or not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "无效的订阅信息"})
            return
        sub = data.get("subscription")
        if not sub or not sub.get("endpoint"):
            self._json(400, {"ok": False, "error": "无效的订阅信息"})
            return
        uid = s["uid"]
        u_dir = user_dir(uid)
        sub_file = os.path.join(u_dir, "push_subs.json")
        subs = []
        if os.path.exists(sub_file):
            try:
                with open(sub_file, "r", encoding="utf-8") as f:
                    subs = json.load(f)
            except Exception:
                subs = []
        # 去重更新：既要按 endpoint 去重，也要按 p256dh 密钥去重。
        # 原因：iOS/PWA 每次重装、清缓存都会生成**新的 endpoint**，
        # 只按 endpoint 去重会导致同一台设备堆积多个订阅 → 同一通知被推多次（实测 3 次）。
        new_ep = sub.get("endpoint")
        new_k = (sub.get("keys") or {}).get("p256dh")
        def _same(x):
            if x.get("endpoint") == new_ep:
                return True
            if new_k and (x.get("keys") or {}).get("p256dh") == new_k:
                return True
            return False
        subs = [x for x in subs if not _same(x)]
        subs.append(sub)
        # 每个用户最多保留 2 个订阅（0=不限制，按需调整）
        MAX_SUBS_PER_USER = 2
        if MAX_SUBS_PER_USER and len(subs) > MAX_SUBS_PER_USER:
            subs = subs[-MAX_SUBS_PER_USER:]
        try:
            with open(sub_file, "w", encoding="utf-8") as f:
                json.dump(subs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"保存订阅失败: {e}"})
            return
        self._json(200, {"ok": True, "msg": "订阅成功"})

    def _api_push_unsubscribe(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        data = self._parse_json()
        if not data or not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "无效的退订信息"})
            return
        sub = data.get("subscription")
        uid = s["uid"]
        u_dir = user_dir(uid)
        sub_file = os.path.join(u_dir, "push_subs.json")
        if not os.path.exists(sub_file):
            self._json(200, {"ok": True, "msg": "已退订"})
            return
        try:
            with open(sub_file, "r", encoding="utf-8") as f:
                subs = json.load(f)
        except Exception:
            subs = []
        if sub and sub.get("endpoint"):
            subs = [x for x in subs if x.get("endpoint") != sub.get("endpoint")]
        else:
            # 未传具体订阅：清空该用户全部推送订阅
            subs = []
        try:
            with open(sub_file, "w", encoding="utf-8") as f:
                json.dump(subs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"保存退订失败: {e}"})
            return
        self._json(200, {"ok": True, "msg": "已退订"})

    def _api_push_test(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        uid = s["uid"]
        import push_sender
        count = push_sender.push_to_user(user_dir(uid), {
            "title": "⚡ V-TASK 测试通知",
            "body": "手机消息推送已成功连通！锁屏状态下即可接收车况与定时提醒。",
            "kind": "test",
            "url": "/"
        })
        self._json(200, {"ok": True, "sent": count})
    def _api_wake(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        uid = s["uid"]
        acct = get_account(uid)
        tok = get_user_dir_tokens(uid)
        if not acct or not tok or not acct.get("vehicles"):
            self._json(400, {"ok": False, "error": "未找到绑定车辆"})
            return
        vid = acct["vehicles"][0].get("id")
        at = tok.get("access_token") or tok.get("id_token")
        if not vid or not at:
            self._json(400, {"ok": False, "error": "车辆授权缺失"})
            return
        import ssl as _ssl
        req = urllib.request.Request(
            f"{FLEET_URL}/api/1/vehicles/{vid}/wake_up",
            data=b"{}",
            headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
            method="POST"
        )
        try:
            ctx = _ssl.create_default_context()  # 严格校验特斯拉证书
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            st = ((d or {}).get("response") or {}).get("state")
            self._json(200, {"ok": True, "state": st or "waking", "msg": "唤醒指令已发送"})
        except Exception as e:
            self._json(500, {"ok": False, "error": f"唤醒失败: {e}"})

    # ================= 监控大屏 =================
    DANGER_KW = ("解锁", "开锁", "闪灯", "鸣笛", "喇叭", "开窗", "车窗", "前备箱", "后备箱", "wake", "喇叭")

    def _monitor_read_ops(self, uid, maxn=200):
        p = os.path.join(user_dir(uid), "ops.log")
        try:
            with open(p, encoding="utf-8") as f:
                lines = [x.strip() for x in f.read().splitlines() if x.strip()]
            return lines[-maxn:]
        except Exception:
            return []

    def _api_monitor_set_name(self):
        q = dict((k, v[0]) for k, v in urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").items())
        body = self._parse_json() or {}
        pw = q.get("pw") or body.get("pw")
        if pw != MONITOR_KEY:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        uid = (body.get("uid") or "").strip()
        name = (body.get("name") or "").strip()
        if not uid:
            self._json(400, {"ok": False, "error": "uid_required"})
            return
        names = _load_user_names()
        if name:
            names[uid] = name
        else:
            names.pop(uid, None)
        if _save_user_names(names):
            self._json(200, {"ok": True, "uid": uid, "name": name})
        else:
            self._json(500, {"ok": False, "error": "save_failed"})

    def _api_monitor_overview(self):
        q = dict((k, v[0]) for k, v in urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").items())
        if q.get("pw") != MONITOR_KEY:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        users = []
        user_names = _load_user_names()
        if os.path.isdir(USERS_DIR):
            for uid in os.listdir(USERS_DIR):
                ud = os.path.join(USERS_DIR, uid)
                if not os.path.isdir(ud) or not os.path.exists(os.path.join(ud, "account.json")):
                    continue
                acct = get_account(uid) or {}
                cars = acct.get("vehicles") or []
                car0 = cars[0] if cars else {}
                vin_full = car0.get("vin", "")
                # 优先使用遥测数据（更实时），否则用 last_state.json
                st = self._read_telemetry_state(vin_full)
                if st is None:
                    st = self._try_json_json(os.path.join(ud, "last_state.json"))
                fields = (st or {}).get("fields")
                ops = self._monitor_read_ops(uid, 3)
                last_op = ops[-1] if ops else ""
                ts_st = (st or {}).get("ts")
                pst = get_pair_state(uid) or {}
                pair_ready = pst.get("control_ready")
                pair_ts = pst.get("ts")
                users.append({
                    "uid": uid,
                    "name": user_names.get(uid, ""),
                    "car": car0.get("display_name") or (car0.get("vin", "")[-6:]) or "未绑车",
                    "vin": (car0.get("vin", "")[-6:]) if car0.get("vin") else "",
                    "online": fields.get("online") if fields else None,
                    "battery": fields.get("battery_level") if fields else None,
                    "locked": fields.get("locked") if fields else None,
                    "charging": fields.get("charging_state") if fields else None,
                    "pair_ready": pair_ready,
                    "pair_ts": pair_ts,
                    "last_op": last_op,
                    "last_ts": ts_st,
                })
        self._json(200, {"ok": True, "users": users, "user_names": user_names})

    def _api_monitor_events(self):
        q = dict((k, v[0]) for k, v in urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").items())
        if q.get("pw") != MONITOR_KEY:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        events = []
        user_names = _load_user_names()
        if os.path.isdir(USERS_DIR):
            for uid in os.listdir(USERS_DIR):
                ud = os.path.join(USERS_DIR, uid)
                if not os.path.isdir(ud):
                    continue
                for line in self._monitor_read_ops(uid, 500):
                    events.append({"uid": uid, "user_name": user_names.get(uid, ""), "line": line})
        def keyf(e):
            m = e["line"].split("]")[0] if "]" in e["line"] else ""
            return m
        events.sort(key=keyf, reverse=True)
        danger = any(any(kw in e["line"] for kw in self.DANGER_KW) for e in events[:5])
        self._json(200, {"ok": True, "events": events[:MONITOR_EVENTS_MAX], "now": time.time()})

    def _api_monitor_page(self):
        q = dict((k, v[0]) for k, v in urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "").items())
        if q.get("pw") != MONITOR_KEY:
            self._send(200, self._monitor_login_html(), "text/html; charset=utf-8")
            return
        try:
            with open(os.path.join(STATIC_DIR, "monitor.html"), encoding="utf-8") as f:
                body = f.read().replace("__PW__", MONITOR_KEY)
        except Exception:
            body = "<h1>monitor.html 缺失</h1>"
        self._send(200, body, "text/html; charset=utf-8")

    def _monitor_login_html(self):
        return """<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>车控监控大屏</title><style>body{font-family:system-ui;background:#0d1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}.c{background:#161b22;padding:30px;border-radius:14px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,.4)}h1{font-size:20px;margin-bottom:18px}input{padding:10px 12px;width:220px;border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#fff;font-size:16px;text-align:center}button{margin-top:14px;padding:10px 22px;border:none;border-radius:8px;background:#238636;color:#fff;font-size:15px;cursor:pointer}</style></head><body><div class=\"c\"><h1>🚗 车控监控大屏</h1><p style=\"color:#8b949e;font-size:13px\">请输入访问口令</p><input type=\"password\" id=\"p\" placeholder=\"口令\" autofocus><br><button onclick=\"location.href='/monitor?pw='+encodeURIComponent(document.getElementById('p').value)\">进入</button></div></body></html>"""

    def _try_json_json(self, p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _read_telemetry_state(self, vin):
        """读取遥测状态文件（按VIN），返回与 last_state.json 相同格式的数据。"""
        if not vin:
            return None
        telemetry_dir = os.environ.get("TELEMETRY_STATE_DIR", "/opt/tesla-fleet/telemetry_states")
        telemetry_file = os.path.join(telemetry_dir, f"telemetry_state_{vin}.json")
        try:
            with open(telemetry_file, encoding="utf-8") as f:
                data = json.load(f)
            # 转换为 last_state.json 格式
            fields = data.get("fields", {})
            # 遥测字段映射：hvac_power -> is_climate_on
            is_climate_on = fields.get("hvac_power") not in (None, "HvacPowerStateOff", "")
            fields["is_climate_on"] = is_climate_on
            # 充电状态映射：优先用采集端功率推导的 charging（cn 固件直流快充 ChargeState 仍 Idle）
            if fields.get("charging") is None:
                fields["charging"] = fields.get("charging_state") in ("Charging", "Starting")
            # 在线状态：有最近更新视为在线
            last_update = data.get("last_update", 0)
            fields["online"] = (time.time() - last_update) < 300  # 5分钟内视为在线
            return {"fields": fields, "ts": last_update}
        except Exception:
            return None

    def _telemetry_frame(self, vin):
        """读遥测并构造前端车况帧 (text_state, fields)；缺失或过旧（>30s）返回 None。"""
        telemetry = self._read_telemetry_state(vin)
        if not (telemetry and telemetry.get("ts") and (time.time() - telemetry["ts"]) < 30):
            return None
        fields = telemetry.get("fields", {})
        fields["_frame_ts"] = telemetry.get("ts")   # 帧采集时间：location_ts 用它（稳定），否则SSE签名每2s抖动重推=第四屏狂跳（2026-10-11）
        return _fields_to_text(fields), fields

    def _probe_state(self, uid):
        """不唤醒的快速探测（委托模块级实现，供 SSE 观察者复用）。"""
        return probe_state_of(uid)

    # ---- SSE 实时车况 ----
    def _api_stream(self, query):
        """GET /api/stream?token=xxx
        遥测数据变化即推送（不耗 API 配额）；无遥测时每 15s 发 ping 维持连接。
        客户端断开（关页/切后台）时线程自然退出，ThreadingHTTPServer 每连接一线程。"""
        s = check_session((query.get("token") or [""])[0])
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        acct = get_account(s["uid"])
        vin = ((acct or {}).get("vehicles") or [{}])[0].get("vin", "")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except Exception:
            return
        log_line("stream", f"{s['name']} SSE 连接建立")
        last_sig = None
        last_ping = time.time()
        # 注册到广播表：唤醒观察者/job 事件可主动推给这条连接
        with _sse_lock:
            _sse_clients.setdefault(s["uid"], []).append(self)
        try:
            while True:
                now = time.time()
                frame = self._telemetry_frame(vin)
                if frame:
                    fields = _inject_meta_fields(vin, frame[1])   # SSE 帧与 /api/state 同一套档案注入（胎压/定位兜底）
                    sig = _state_sig(fields)   # 显示敏感度去重：温度微抖/坐标漂移/帧时间戳不再触发重推
                    if sig != last_sig:
                        last_sig = sig
                        last_ping = now
                        payload = json.dumps(
                            {"type": "state", "state": frame[0], "vehicle_data": fields},
                            ensure_ascii=False)
                        self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                elif now - last_ping > 15:
                    last_ping = now
                    self.wfile.write(b'data: {"type":"ping"}\n\n')
                    self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log_line("stream", f"SSE 结束: {e}")
        finally:
            with _sse_lock:
                lst = _sse_clients.get(s["uid"]) or []
                if self in lst:
                    lst.remove(self)
                if not lst:
                    _sse_clients.pop(s["uid"], None)

    # ---- 状态 ----
    def _api_state(self):
        s = self._auth()
        if not s:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return
        acct = get_account(s["uid"])
        if not acct or not acct.get("vehicles"):
            self._json(200, {"ok": False, "state": ""})
            return
        uid = s["uid"]
        try:
            _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (_q.get("full") or ["0"])[0] == "1":
                _ST_FPOLL[uid] = time.time() + 15   # 前端激活车模屏/回前台：拉一次全量快照（前后备箱真实态，遥测不推 2026-10-11）
        except Exception:
            pass
        st_file = os.path.join(user_dir(uid), "last_state.txt")
        json_file = os.path.join(user_dir(uid), "last_state.json")

        def _read_last_text():
            try:
                with open(st_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                return ""

        def _read_last_json():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None

        def _reply(text_state, fields, fresh):
            fields = _inject_meta_fields(vin_full, fields)
            self._json(200, {"ok": True, "state": text_state, "vehicle_data": fields, "fresh": fresh})

        # 0) 优先使用遥测数据（最实时，不消耗API配额）；指令后直连窗口内跳过——
        # 后备箱等字段遥测不推，窗口内必须直连全量快照才能确认执行结果（2026-10-11）
        vin_full = (acct.get("vehicles") or [{}])[0].get("vin", "")
        frame = None if time.time() < _ST_FPOLL.get(uid, 0) else self._telemetry_frame(vin_full)
        if frame:
            _reply(frame[0], frame[1], True)
            return

        # 1) 先不唤醒探测：只读车辆元数据（state 字段）
        st = self._probe_state(uid)
        if st:
            sk = str(st).lower()
            if sk in ("asleep", "offline"):
                cn = "休眠" if sk == "asleep" else "离线"
                # [fix 2026-09-26] 优先用遥测"最后上报值"：车端主动推送、零配额零唤醒，
                # 字段比 last_state 文本快照新鲜齐全（电量/温度/锁闭/空调态）
                _tele = self._read_telemetry_state(vin_full)
                if _tele and _tele.get("fields"):
                    tf = dict(_tele.get("fields") or {})
                    tf["_frame_ts"] = _tele.get("ts")   # 帧采集时间：位置时间戳用它，而不是响应时间
                    # 2026-10-07 用户定版：离线/休眠时显示最后已知车控状态（同特斯拉 App），
                    # 不再剥离置 --；_stale 仅作标记不改变显示
                    try:
                        if time.time() - float(_tele.get("ts") or 0) > 1800:
                            tf["_stale"] = True
                    except Exception:
                        pass
                    txt2 = re.sub(r"状态\s*在线", f"状态 {cn}", _fields_to_text(tf))
                    _reply(txt2, tf, False)
                    return
                last_text = _read_last_text()
                if last_text:
                    new_state = re.sub(r"状态\s*\S+", f"状态 {cn}", last_text)
                else:
                    new_state = f"状态 {cn}\n锁定 --\n车内 --°C"
                last_json = _read_last_json()
                fields = (last_json or {}).get("fields") if last_json else None
                # 快照过旧（>30 分钟）时，不把陈旧的车控状态当作真相喂给前端按钮，
                # 否则"空调开启/已锁"这类状态会长期卡住按钮，导致总发同一条反向命令。
                try:
                    _ts = (last_json or {}).get("ts")
                    if not _ts:
                        _ts = os.path.getmtime(st_file)
                    _stale_txt = (time.time() - float(_ts)) > 1800
                    if fields and _ts and _stale_txt:
                        fields = dict(fields)
                        for _k in ("is_climate_on", "locked", "sentry_mode", "charging",
                                   "steering_wheel_heater", "seat_cooler_level"):
                            fields.pop(_k, None)
                        fields["_stale"] = True
                except Exception:
                    pass
                _reply(new_state, fields, False)
                return

        # 1.5) 进程内 30s 缓存：车控命令执行后会失效，命中则零开销返回
        # （直连窗口内用5s短缓存：前端pending轮询要尽快看到后备箱真实态翻转）
        ent = _st_cache.get(uid)
        _ttl = 5 if time.time() < _ST_FPOLL.get(uid, 0) else _ST_CACHE_TTL
        if ent and (time.time() - ent["ts"]) < _ttl:
            _reply(_fields_to_text(ent["fields"]), ent["fields"], True)
            return

        # 2) 在线/未知：进程内直连拉结构化车况（无子进程冷启动），持久化快照
        fields = _vehicle_data_fields(uid)
        if fields:
            with _st_cache_lock:
                _st_cache[uid] = {"fields": fields, "ts": time.time()}
            try:
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump({"fields": fields, "ts": time.time()}, f, ensure_ascii=False)
                # [fix 2026-09-26] 同步刷新文本快照：休眠时前端展示的"最后已知读数"来自
                # last_state.txt，若长期不更新会停留好几天前
                with open(st_file, "w", encoding="utf-8") as f:
                    f.write(_fields_to_text(fields))
            except Exception:
                pass
            _reply(_fields_to_text(fields), fields, True)
            return

        # 2b) 直连失败（典型：token 过期，子进程链路可自愈刷新）→ 旧子进程链路兜底
        jout, jerr = run_runner(uid, "__json__")
        js = None
        try:
            if jout:
                js = json.loads(jout)
        except Exception:
            js = None
        fields = None
        if js and js.get("fresh"):
            fields = js.get("fields")
            try:
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump({"fields": fields, "ts": time.time()}, f, ensure_ascii=False)
            except Exception:
                pass

        # 3) 文本（电量/温度/天气）走原查车况
        out, err = run_runner(uid, "查车况")
        if err or not out:
            if fields:
                bl = fields.get("battery_level")
                lock_cn = "已锁" if fields.get("locked") else "未锁"
                txt = f"🚗 电量 {bl}% | 锁定 {lock_cn} | 状态 驻车"
                cs = fields.get("charging_state")
                if cs:
                    txt += f"\n充电 {cs}"
                _reply(txt, fields, True)
                return
            self._json(200, {"ok": False, "state": ""})
            return
        try:
            with open(st_file, "w", encoding="utf-8") as f:
                f.write(out)
        except Exception:
            pass
        _reply(out, fields, bool(fields))

def main():
    ensure_codes_file()
    os.makedirs(USERS_DIR, exist_ok=True)
    # 启动时检查高德 Key，失败只告警，不阻断车控和定时服务。
    key = load_amap_key()
    if not key:
        print("[startup] 高德 Key 未配置，路线功能不可用")
    else:
        try:
            q = urllib.parse.urlencode({"address": "上海市", "key": key, "output": "JSON"})
            with urllib.request.urlopen("https://restapi.amap.com/v3/geocode/geo?" + q, timeout=8) as r:
                check = json.loads(r.read().decode("utf-8"))
            if str(check.get("status")) == "1":
                print("[startup] 高德 Key 校验通过")
            else:
                print(f"[startup] 高德 Key 无效：{check.get('info', '未知错误')} ({check.get('infocode', '')})")
        except Exception as e:
            print(f"[startup] 高德 Key 校验失败：{e}")
    schedule.start_scheduler()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log_line("start", f"multi car-web 启动: http://{HOST}:{PORT}")
    print(f"multi car-web listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()

if __name__ == "__main__":
    main()