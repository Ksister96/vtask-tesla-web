# -*- coding: utf-8 -*-
"""user_runner.py — 朋友专用车控执行入口（与主 run.py 隔离）

设计原则（用户 2026-09-02 确认）：
- 完全不影响主链路（微信车控 run.py）
- 每用户独立 token/VIN，从 users/<uid>/ 读取
- 命令走同一本地代理（127.0.0.1:4443，代理本身按请求 token 区分车）
- 只读复用：智能温控/天气/反馈模板从主 run.py 导入函数（不 import 整个 run 的 main）
- 危险动作白名单：默认禁止 erase_user_data / reset_pin / clear_pin 等
"""
import io as _io
import json
import os
import ssl
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# 强制 UTF-8（Windows 控制台 GBK 会炸 emoji/中文）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 特斯拉域名严格校验证书；本机 4443 代理为自签证书，单独用宽松上下文
SSL_CTX = ssl.create_default_context()
PROXY_CTX = ssl._create_unverified_context()

MULTI_BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
OWNER_BASE = os.environ.get("OWNER_BASE", "/opt/tesla-fleet")
# 车控命令→REST 路径的覆盖表（多用户版可独立修正，不必动生产 E:\tesla-fleet\run.py）。
# 键 = intent 动作名，值 = /api/1/vehicles/{vin}/command/<path> 里的 <path>；值为空串表示本地不支持。
DYNAMIC_ACTIONS = {"window_control": "window_control"}
USERS_DIR = os.path.join(MULTI_BASE, "users")
PROXY = "https://localhost:4443"
FLEET = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"

# 从主 run.py 导入只读逻辑（智能温控/天气/模板），避免重复维护
sys.path.insert(0, OWNER_BASE)
import run as _owner

# 动作→REST 路径表：本地覆盖优先，其余继承主链路 run.ACTIONS
ACTION_PATHS = dict(getattr(_owner, "ACTIONS", {}) or {})
ACTION_PATHS.update(DYNAMIC_ACTIONS)

# 白名单：朋友可用的动作（安全 60 命令全集里的可控子集）
ALLOWED_ACTIONS = {
    "honk_horn", "flash_lights", "door_lock", "door_unlock",
    "actuate_trunk", "window_control",
    "auto_conditioning_start", "auto_conditioning_stop", "set_temps",
    "set_preconditioning_max", "set_climate_keeper_mode",
    "remote_steering_wheel_heater_request", "remote_seat_heater_request",
    "remote_seat_cooler_request",
    "charge_start", "charge_stop", "set_charge_limit", "set_charging_amps",
    "charge_port_door_open", "charge_port_door_close",
    "set_scheduled_charging", "set_sentry_mode", "set_valet_mode",
    "wake_up", "media_volume_up", "media_volume_down",
    "media_toggle_playback", "media_next_track", "media_prev_track",
    "adjust_volume", "speed_limit_set_limit", "trigger_homelink",
    "status",
}

# 绝对禁止（官方最危险操作，任何用户都不能碰）
FORBIDDEN = {"erase_user_data", "reset_pin", "clear_pin", "set_vehicle_name", "schedule_software_update", "cancel_software_update"}


CHAT_GUIDE = """我是你的特斯拉用车助手，暂时只支持车辆控制和定时用车。

你可以直接说：
• 锁车 / 解锁
• 开空调 / 关闭空调
• 空调调到 22 度
• 查车况 / 查电量
• 闪灯 / 鸣笛
• 明早 7 点用车
• 取消定时用车"""


def learn_phrase(text, action, params=None):
    """成功控车后把用户原话交给本地语义路由学习；失败静默，不阻塞控车。"""
    try:
        body = json.dumps({"text": text, "intent": action, "params": params or {}}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8791/learn", data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
    except Exception:
        pass


def log_line(uid, kind, msg):
    try:
        logf = os.path.join(USERS_DIR, uid, "ops.log")
        os.makedirs(os.path.dirname(logf), exist_ok=True)
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {kind}: {msg}\n")
    except Exception:
        pass


def get_user_tokens_path(uid):
    return os.path.join(USERS_DIR, uid, "tokens.json")


def user_exists(uid):
    return os.path.exists(os.path.join(USERS_DIR, uid, "tokens.json"))


def get_token(uid):
    """读用户自己的 access_token；没有则返回 None（绝不回退到主人的 tokens.json）。"""
    p = get_user_tokens_path(uid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f).get("access_token")
    except Exception:
        return None


def get_vin(uid):
    """用户绑定信息里的 VIN；没有则返回 None。"""
    p = os.path.join(USERS_DIR, uid, "account.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return (json.load(f).get("vehicles") or [{}])[0].get("vin")
    except Exception:
        return None


USER_SETTING_DEFAULTS = {
    "smartClimate": True,      # 智能温控：开空调按温差自动分档
    "replyShowCar": True,      # 回复模板：显示 🚗 车辆状态行
    "replyShowWeather": True,  # 回复模板：显示 ⛅ 天气行
    "replyShowTip": True,      # 回复模板：显示 💡 建议行
}

def load_user_settings(uid):
    """读用户设置（users/<uid>/settings.json），只取本模块关心的字段，缺省用默认值。
    与 car-web-multi.py 的 DEFAULT_SETTINGS 对齐；这里白名单取子集，避免耦合全量。
    """
    out = dict(USER_SETTING_DEFAULTS)
    try:
        with open(os.path.join(USERS_DIR, uid, "settings.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in USER_SETTING_DEFAULTS:
                if k in d:
                    out[k] = bool(d[k])
    except Exception:
        pass
    return out


def http(method, url, headers=None, body=None, timeout=40):
    """通用 HTTPS 请求（带重试/429），与主 run.py 行为一致但独立实现，避免改主线。"""
    data = body.encode("utf-8") if isinstance(body, str) else body
    last_err = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        # 本机代理(4443,自签)用宽松上下文；特斯拉域名严格校验
        ctx = PROXY_CTX if ("//localhost" in url or "//127.0.0.1" in url) else SSL_CTX
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return (json.loads(raw) if raw else None), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 限流只重试一次即返回（主链路多为并发调用，重试风暴只会叠加等待）。
                last_err = f"429 限流(第{attempt}次)"
                if attempt >= 2:
                    break
                time.sleep(min(30, 5 * attempt))
                continue
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code >= 500:
                return None, f"服务端错误 {e.code} {body_txt}".strip()
            return None, f"HTTP {e.code} {body_txt}".strip()
        except Exception as e:
            last_err = f"网络错误: {e}"
            # 连接类错误快速重试一次（不睡）；第三次才短暂等待，避免并发命令各自累加十几秒。
            if attempt < 3:
                continue
            time.sleep(2 * attempt)
    return None, last_err or "请求失败"


def ensure_proxy():
    """确保本地代理在跑（复用主链路 start-proxy.ps1）。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4443), timeout=3):
            return True
    except Exception:
        pass
    # Linux: 直接拉起 tesla-http-proxy（systemd 常驻，这里是即时补救）
    try:
        subprocess.Popen(
            ["/opt/tesla/bin/tesla-http-proxy",
             "-key-file", os.path.join(OWNER_BASE, "private-key.pem"),
             "-cert", os.path.join(OWNER_BASE, "certs", "tls-cert.pem"),
             "-tls-key", os.path.join(OWNER_BASE, "certs", "tls-key.pem"),
             "-port", "4443"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        pass
    for _ in range(3):
        time.sleep(3)
        try:
            with socket.create_connection(("127.0.0.1", 4443), timeout=3):
                return True
        except Exception:
            continue
    return False


def refresh_token(uid):
    """按用户刷新 refresh_token（单次使用，用完即换新）。"""
    if os.environ.get("SANDBOX_NO_REFRESH"):
        return False, "沙箱已禁用本地刷新(防与云端互踢)；请重跑 tflow.py test --car 拉取云端新 token"
    p = get_user_tokens_path(uid)
    if not os.path.exists(p):
        return False, "用户 token 文件不存在"
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            t = json.load(f)
    except Exception as e:
        return False, f"读 token 失败: {e}"
    rt = t.get("refresh_token")
    if not rt:
        return False, "没有 refresh_token"
    cid = t.get("client_id") or "b0708cc4-c9b7-4a66-a061-feb3ef3c58a9"
    # 注意：刷新可能也需要 client_secret（auth.tesla.cn 要求），从用户文件拿
    secret = t.get("client_secret", "")
    body = (f"grant_type=refresh_token&client_id={urllib.parse.quote(cid)}"
            f"&refresh_token={urllib.parse.quote(rt)}")
    if secret:
        body += f"&client_secret={urllib.parse.quote(secret)}"
    try:
        req = urllib.request.Request(
            "https://auth.tesla.cn/oauth2/v3/token",
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            nt = json.loads(resp.read().decode("utf-8"))
        if nt.get("access_token"):
            nt["client_id"] = cid
            if secret:
                nt["client_secret"] = secret
            # 原子写：防止崩溃在写一半导致 access+refresh 双丢
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(nt, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            return True, "token 已刷新"
        return False, f"刷新返回异常: {nt}"
    except Exception as e:
        return False, f"刷新失败: {e}"


def get_vehicle_id(uid):
    """从 account.json 取用户绑定车的数字 vehicle ID（不是 VIN）。"""
    p = os.path.join(USERS_DIR, uid, "account.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            v = (json.load(f).get("vehicles") or [{}])[0]
        return v.get("id") or v.get("vin")
    except Exception:
        return None


_wake_lock = threading.Lock()
_wake_inflight = {}


def _wake_once(uid, vin, vid, wait_online=True):
    """唤醒去重：同一用户同一时刻只真正唤醒一次（wait_online 以首发者为准）。
    复合指令是并发下发命令的，命令里各自发现“车 asleep”会各起一轮唤醒+40s 轮询，
    等待时间按路数叠加（实测复合指令因此多等几十秒）。这里让后来者等首发者结果。
    首发者若是“只发唤醒指令不等在线”，后来者需要等在线时再补一次轮询。"""
    with _wake_lock:
        entry = _wake_inflight.get(uid)
        if entry is not None:
            ev, box = entry
            wait_for = True
        else:
            ev, box = threading.Event(), {}
            _wake_inflight[uid] = (ev, box)
            wait_for = False
    if wait_for:
        ev.wait(75)
        ok, msg = bool(box.get("ok")), (box.get("msg") or "唤醒结果未知")
        if not wait_online or box.get("waited"):
            return ok, msg
        # 首发者没等在线，这里自己补一次短轮询
        return _wait_online(uid, vin, vid)
    try:
        ok, msg = wake_vehicle(uid, vin, vid, wait_online=wait_online)
        box["waited"] = bool(wait_online)
    except Exception as e:
        ok, msg, box["waited"] = False, str(e), False
    box["ok"], box["msg"] = ok, msg
    ev.set()
    with _wake_lock:
        _wake_inflight.pop(uid, None)
    return ok, msg


def _wait_online(uid, vin, vid, tries=15):
    """只轮询等车辆在线（不再发 wake_up）。返回 (ok, msg)。"""
    tok = get_token(uid)
    if not (vid and tok):
        return False, "无法确认车辆在线"
    h = {"Authorization": f"Bearer {tok}"}
    for _ in range(tries):
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}", headers=h, timeout=15)
        if not err and (data or {}).get("response", {}).get("state") == "online":
            return True, "车辆已唤醒在线"
        time.sleep(2)
    return False, "唤醒后仍未在线"


def wake_vehicle(uid, vin, vid=None, wait_online=True):
    """唤醒用户车辆。返回 (ok, msg)。vid 为数字 vehicle id，缺省时从 account 取。"""
    if not vid:
        vid = get_vehicle_id(uid)
    if not vid:
        return False, "未找到车辆 ID"
    tok = get_token(uid)
    if not tok:
        return False, "用户未绑定 token"
    h = {"Authorization": f"Bearer {tok}"}
    data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}", headers=h, timeout=15)
    if err and "401" in err:
        ok, m = refresh_token(uid)
        if ok:
            tok = get_token(uid)
            h = {"Authorization": f"Bearer {tok}"}
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}", headers=h, timeout=15)
    if not err and (data or {}).get("response", {}).get("state") == "online":
        return True, "车辆已在线"
    http("POST", f"{FLEET}/api/1/vehicles/{vid}/wake_up",
         headers={**h, "Content-Type": "application/json"}, body="{}", timeout=30)
    if not wait_online:
        return True, "唤醒指令已发送"
    for _ in range(20):
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}", headers=h, timeout=15)
        if not err and (data or {}).get("response", {}).get("state") == "online":
            return True, "车辆已唤醒在线"
        time.sleep(2)
    return False, "唤醒后 40 秒仍未在线"


def proxy_command(uid, vin, path, params, retry_auth=True):
    """通过本地代理给用户的车发命令。带刷新/唤醒自愈。返回 (ok, msg)。"""
    if not ensure_proxy():
        return False, "代理不可用"
    for attempt in range(3):
        tok = get_token(uid)
        if not tok:
            return False, "用户未绑定 token，请先完成绑定"
        url = f"{PROXY}/api/1/vehicles/{vin}/command/{path}"
        body = json.dumps(params or {})
        data, err = http("POST", url, headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        }, body=body, timeout=60)
        if err:
            e_l = err.lower()
            if ("offline" in e_l or "asleep" in e_l or "vehicle unavailable" in e_l
                    or "服务端错误" in err or "500" in err):
                _wake_once(uid, vin, get_vehicle_id(uid), wait_online=(attempt > 0))
                continue
            return False, err
        resp = (data or {}).get("response") or {}
        if resp.get("result") is True:
            return True, (data or {}).get("error_description") or ""
        if "already_set" in str(resp.get("reason") or ""):
            return True, "already_set"   # 幂等：车端已是目标值，视为成功（重复设置不再报失败）
        err_str = str(data or "")
        if retry_auth and ("401" in err_str or "authentication" in err_str.lower() or "invalid token" in err_str.lower()):
            ok_r, m = refresh_token(uid)
            if ok_r:
                continue
            return False, f"token 失效且刷新失败: {m}"
        e_l = err_str.lower()
        if "offline" in e_l or "asleep" in e_l or "vehicle unavailable" in e_l:
            _wake_once(uid, vin, get_vehicle_id(uid), wait_online=(attempt > 0))
            continue
        return False, f"失败: {(data or {}).get('error') or ''} {(data or {}).get('error_description') or ''}"
    return False, "多次重试仍失败"


def probe_state(uid):
    """快速探测用户车辆状态（不唤醒、5s 超时）。返回 online/asleep/offline 或 None。
    供网页进程判断是否需要先发"正在唤醒"提示（两段式反馈）用。"""
    vid = get_vehicle_id(uid)
    tok = get_token(uid)
    if not vid or not tok:
        return None
    try:
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=5)
        if err:
            return None
        return ((data or {}).get("response") or {}).get("state")
    except Exception:
        return None


# == 控制授权（虚拟钥匙配对）探测 ==
# 判据：只发无损 door_lock 探针。命中“钥匙未配对”签名→needs_pairing；
# result:true（已锁=幂等成功）→ ready；offline/asleep/网络/超时→ unknown（不误判）。
_NOT_PAIRED_MARKERS = ("public key has not been paired", "not been paired",
                        "vehicle rejected request", "rejected request",
                        "key has not been paired", "public key rejected")


def pair_probe(uid):
    """探测该用户绑定车辆是否已完成虚拟钥匙（控制授权）配对。
    返回 dict: {c: ready|needs_pairing|unknown, vin, reason}
    - ready        门锁命令被车接受（已配钥匙）
    - needs_pairing  命中“public key has not been paired”（钥匙没装进车）
    - unknown        离线/休眠醒不过/网络错/超时/没绑（无法判定，不弹引导）
    """
    vin = get_vin(uid) if hasattr(get_vin, '__call__') else None
    try:
        vin = get_vin(uid)
    except Exception:
        vin = None
    tok = get_token(uid)
    if not vin or not tok:
        return {"c": "unknown", "reason": "unbound-or-notoken"}
    # 无损探针：door_lock（车已锁则幂等无副作用）
    data, err = None, None
    try:
        body = json.dumps({})
        url = f"{PROXY}/api/1/vehicles/{vin}/command/door_lock"
        data, err = http("POST", url, headers={
            "Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, body=body, timeout=60)
    except Exception as e:
        return {"c": "unknown", "vin": vin, "reason": f"exception:{e}"}
    raw = json.dumps({"err": err, "data": data}, ensure_ascii=False)
    low = raw.lower()
    # 未配钥匙签名
    for mk in _NOT_PAIRED_MARKERS:
        if mk.lower() in low:
            return {"c": "needs_pairing", "vin": vin, "reason": mk}
    # 车直接接受 / 已锁 / 命令返回 result:true
    if err is None and isinstance(data, dict):
        resp = (data or {}).get("response") or {}
        if resp.get("result") is True or resp.get("reason") in ("already_done", "done"):
            return {"c": "ready", "vin": vin, "reason": "lock-accepted"}
    # 可能休眠/离线/醒不过 → unknown（让上层可重试提示，不误弹未配对）
    if err or "offline" in low or "asleep" in low or "wake" in low or "timeout" in low or \
       "unavailable" in low or "5" in low[:100]:
        return {"c": "unknown", "vin": vin, "reason": (err or "offline-unavailable")[:120]}
    return {"c": "unknown", "vin": vin, "reason": "unclassifiable"}


def read_vehicle(uid, vin):
    """读用户车辆车况（快速版，不唤醒）。失败返回 None。用数字 id 而不是 VIN。"""
    tok = get_token(uid)
    if not tok:
        return None
    vid = get_vehicle_id(uid)
    if not vin or not vid:
        return None
    data, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}/vehicle_data",
                     headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    if err or not data:
        return None
    resp = (data or {}).get("response", {})
    try:
        ec = (resp.get("vehicle_config") or {}).get("exterior_color")
        ds = resp.get("drive_state") or {}
        meta = {"ts": int(time.time() * 1000)}
        if ec:
            meta["exterior_color"] = ec
        if ds.get("latitude") is not None and ds.get("longitude") is not None:
            meta["lat"] = ds.get("latitude")
            meta["lon"] = ds.get("longitude")
            meta["heading"] = ds.get("heading")
            meta["odometer_km"] = round((resp.get("vehicle_state") or {}).get("odometer") or 0, 2) * 1.609344
        json.dump(meta, open(os.path.join(USERS, uid, "vehicle_meta.json"), "w", encoding="utf-8"))
    except Exception:
        pass
    return resp


# ===== 智能温控 v2（2026-09-15 周哥定版）=====
# 设计要点：
# 1) 每次智能温控前，先唤醒车辆、读到"真实车内温度"再决定怎么控温（不再凭空猜温差）；
# 2) 偏好基线固定 21°C —— 温差 = 车内真实温度 − 21；基线不参与自我迭代（根治"越开越低/永久27"的棘轮）；
# 3) 夏季制冷激进档补主驾座椅通风；冬季制热激进档补方向盘加热 + 主驾座椅加热；
# 4) 过冲回调目标 = 基线 21°C（回调真正把温度拉回温和值，而不是把激进值再写一遍）；
# 5) 回调前先确认空调仍开着、设定值没被用户手改（改过就退出，不覆盖用户操作）。
CLIMATE_PREF = 21.0     # 偏好基线（用户偏好温度）
CLIMATE_MIN = 19.0      # 制冷最低设定
CLIMATE_MAX = 27.0      # 制热最高设定
CLIMATE_WATCH_SCRIPT = os.path.join(MULTI_BASE, "smart_climate_watch_multi.py")


def decide_climate_multi(inside, pref=None):
    """按"车内真实温度 − 偏好基线(21°C)"分档。返回 (目标温度, 模式, 是否激进, 加料类型)。

    温差 > +25  → 19°C 猛降（制冷加料：主驾座椅通风）
    温差 +10~+25 → 19°C 快降（制冷加料：主驾座椅通风）
    温差 −15~+10 → 21°C 温和维持（省电，无加料）
    温差 < −15  → 27°C 猛热（制热加料：方向盘加热 + 主驾座椅加热）
    """
    pref = CLIMATE_PREF if pref is None else float(pref)
    delta = float(inside) - pref
    if delta > 25:
        return CLIMATE_MIN, "猛降", True, "cool"
    if delta >= 10:
        # [fix 2026-09-28] 快降 19→20：19°C 是猛降（>25°C 温差）场景；
        # 快降用 20°C + 座椅通风已足够，且更贴近回调目标 21°C，体感不冰
        return max(round(pref - 1, 1), CLIMATE_MIN), "快降", True, "cool"
    if delta >= -15:
        return round(pref, 1), "温和维持", False, ""
    return CLIMATE_MAX, "猛热", True, "heat"


def read_vehicle_woken(uid, vin, tries=4, gap=3):
    """智能温控前置：先唤醒并等在线，再读真实车内温度。

    车已在线 → 快读（~0.3s）；休眠/离线 → 唤醒+轮询在线后重读（最多 tries 次，每次隔 gap 秒）。
    返回 (vehicle_data 或 None, 说明)。
    """
    def _inside(v):
        return ((v or {}).get("climate_state") or {}).get("inside_temp")

    v = read_vehicle(uid, vin)
    if _inside(v) is not None:
        return v, "已在线"
    ok, msg = _wake_once(uid, vin, get_vehicle_id(uid), wait_online=True)
    for i in range(max(1, int(tries))):
        v = read_vehicle(uid, vin)
        if _inside(v) is not None:
            return v, msg
        if i < int(tries) - 1:
            time.sleep(gap)
    return None, (msg or "唤醒失败")


def climate_addons(uid, vin, kind):
    """激进档"加料"：cool=主驾座椅通风3档；heat=方向盘加热+主驾座椅加热3档。返回中文说明。"""
    if kind == "cool":
        proxy_command(uid, vin, "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 3})
        return "主驾座椅通风3档"
    if kind == "heat":
        proxy_command(uid, vin, "remote_steering_wheel_heater_request", {"on": True})
        proxy_command(uid, vin, "remote_seat_heater_request", {"seat_position": 0, "level": 3})
        return "方向盘加热+主驾座椅加热3档"
    return ""


def set_temps_verified(uid, vin, temp, tries=3):
    """运行中直调 set_temps(双区) + 回读车机校验 + 重试（2026-10-06）。
    官方 App 的正路：空调运行中直接调即可生效。回读 driver_temp_setting 验证，
    未生效自动重试；全失败返回 False（调用方可落「停机态写入」保底）。"""
    for i in range(tries):
        ok, _ = proxy_command(uid, vin, "set_temps", {"driver_temp": temp, "passenger_temp": temp})
        if ok:
            time.sleep(2)
            v = read_vehicle(uid, vin)
            dts = ((v or {}).get("climate_state") or {}).get("driver_temp_setting")
            try:
                if dts is not None and abs(float(dts) - float(temp)) < 0.3:
                    return True
            except (TypeError, ValueError):
                pass
            log_line(uid, "set_temps", f"直调第{i+1}次回读不符({dts})，重试")
        else:
            time.sleep(1.5)
    return False


def set_temp_keep_passenger(uid, vin, temp):
    """只写主驾温度（副驾沿用自身设定）；车端不接受单参数时退回双参数。返回 (ok, msg)。
    v2 修复（2026-09-15）：设温一成功，若该温度是激进值（!=偏好基线），立即启动过冲回调
    监测——放在这里而不是调用方末尾，确保主流程中途被超时掐死时，激进温度也有人拉回。
    """
    # [2026-09-29] 单参数假成功（result:true 但车机不生效），双区直写
    ok, msg = proxy_command(uid, vin, "set_temps", {"driver_temp": temp, "passenger_temp": temp})
    # 设温成功且是激进值 → 立刻启动回调监测（原调用方的重复启动会被 claim_single 去重拦掉）
    if ok and temp is not None and abs(float(temp) - CLIMATE_PREF) > 0.6:
        try:
            # [2026-09-29] 手动设定温度：用户设的值就是新基准，无需回调到别的目标
            pass
        except Exception:
            pass
    return ok, msg


def start_climate_watch(uid, veh_id, cur_temp, target_temp):
    """启动过冲回调监测（传"当前设定"与"回调目标"）。返回 (ok, msg)。"""
    if not veh_id or cur_temp is None or target_temp is None:
        return False, "缺少参数"
    if float(cur_temp) == float(target_temp):
        return False, "无需回调"
    try:
        subprocess.Popen(
            [sys.executable, CLIMATE_WATCH_SCRIPT, str(uid), str(veh_id), str(cur_temp), str(target_temp)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True, "已启动"
    except Exception as e:
        try:
            log_line(uid, "climate", f"启动过冲监测失败: {e}")
        except Exception:
            pass
        return False, str(e)


def smart_climate_plan(uid, vin):
    """算一次智能温控方案（会先唤醒读真实车内温度）。返回 dict。
    [2026-09-29 v3] 分档基准 = 当前空调设定温度（用户此刻偏好），读不到/异常回退 21°C。"""
    st = load_user_settings(uid)
    smart = bool(st.get("smartClimate", True))
    v, why = read_vehicle_woken(uid, vin)
    cl_state = (v or {}).get("climate_state") or {}
    inside = cl_state.get("inside_temp")
    _dts = cl_state.get("driver_temp_setting")
    try:
        pref = round(float(_dts) * 2) / 2 if _dts is not None else CLIMATE_PREF
    except Exception:
        pref = CLIMATE_PREF
    if not (15 <= pref <= 28):
        pref = CLIMATE_PREF
    try:
        log_line(uid, "climate", f"plan: inside={inside} dts={_dts} pref={pref}")
    except Exception:
        pass
    if not smart:
        return {"smart": False, "temp": pref, "mode": "手动", "aggressive": False,
                "extra": "", "inside": inside, "delta": None, "why": why, "v": v, "pref": pref}
    if inside is None:
        # 唤醒后仍读不到真实温度 → 按基准温和执行，不瞎猜温差
        return {"smart": True, "temp": pref, "mode": "温和维持", "aggressive": False,
                "extra": "", "inside": None, "delta": None, "why": why, "v": v, "pref": pref}
    temp, mode, aggressive, kind = decide_climate_multi(inside, pref=pref)
    return {"smart": True, "temp": temp, "mode": mode, "aggressive": aggressive,
            "extra": kind, "inside": inside, "delta": round(float(inside) - pref, 1),
            "why": why, "v": v, "pref": pref}


def climate_head(plan, extra_cn="", prefix="✅ 开启空调"):
    """智能温控首行文案（统一模板）。"""
    t = plan.get("temp")
    if not plan.get("smart"):
        head = f"{prefix} | 设定{t}°C（智能温控已关闭，按偏好基线）"
    elif plan.get("inside") is None:
        head = f"{prefix} | 设定{t}°C（车内温度未知，温和维持）"
    else:
        head = (f"{prefix} | 设定{t}°C（车内{round(float(plan['inside']), 1)}°C，"
                f"温差{plan.get('delta')}°C，{plan.get('mode')}）")
    if extra_cn:
        head += f" | {extra_cn}"
    return head


def fmt_report(uid, vin, ok, action, detail="", v=None, w=None):
    """按用户车况生成反馈（🚗/⛅/💡）。可传入并行预取的 v/w，避免重复请求。
    车辆状态/天气/建议三行按用户设置开关过滤（回复模板设置）。
    """
    st = load_user_settings(uid)
    lines = []
    lines.append(("✅ " if ok else "❌ ") + _owner.action_cn(action) + (f" | {detail}" if detail else ""))
    if st.get("replyShowCar", True):
        if v is None:
            v = read_vehicle(uid, vin)
        if v:
            cs, vs, cl = (v.get("charge_state") or {}), (v.get("vehicle_state") or {}), (v.get("climate_state") or {})
            bats = cs.get("battery_level")
            rng = cs.get("battery_range")
            inside = cl.get("inside_temp")
            rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
            in_s = f"{round(inside, 1)}°C" if inside is not None else "?"
            lock_s = "已锁" if vs.get("locked") else "未锁"
            lines.append(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s}")
    if st.get("replyShowWeather", True):
        if w is None:
            w = _owner.get_weather()
        if w:
            t = w.get("temp") or "?"
            f = w.get("feels") or t
            lines.append(f"⛅ {w.get('city')} {t}°C 体感{f}°C {w.get('weather')} 湿度{w.get('humidity')}%")
            if st.get("replyShowTip", True):
                adv = _owner.weather_advice(w)
                if adv:
                    lines.append(f"💡 {adv}")
    return "\n".join(lines)


def run_text(uid, user_text):
    """给用户的自然语言指令做主链路（与 run.py main 对齐，但走用户 token/VIN）。"""
_SPLIT_RE_LOCAL = None


def _seg_split(text):
    """按分隔符/连接词切分复合指令。"""
    import re as _re
    global _SPLIT_RE_LOCAL
    if _SPLIT_RE_LOCAL is None:
        _SPLIT_RE_LOCAL = _re.compile(r"[，,、;；]|\s+|然后|接着|再|并且|而且|还有|同时|顺便|以及|加上|并把|和|跟|与|及|＋|\+")
    st = (text or "").strip().rstrip("，,。.、!！?？ ").strip()
    if not st:
        return []
    noise = {"帮我", "请", "麻烦", "一下", "把", "让", "给", "的", "个", "了", "车", "一次", "两下", "三下"}
    out = []
    for s in _SPLIT_RE_LOCAL.split(st):
        s = s.strip().strip("，,。.、!！?？ ").strip()
        if not s or s in noise:
            continue
        # 粘连动作二次切分（"闪灯鸣笛" → 闪灯 / 鸣笛）
        for piece in _split_glued(s):
            piece = _norm_seg(piece)
            if piece and piece not in noise:
                out.append(piece)
    return out


def _route_seg(seg):
    """单片段本地解析：语义路由服务(8791) + 主版 rule_match 兜底。命中返回 action dict。"""
    # 语义路由服务
    try:
        body = json.dumps({"text": seg}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:8791/route", data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("route") == "action" and r.get("action"):
            return {"action": str(r["action"]), "params": r.get("params") or {}}
    except Exception:
        pass
    # 主版规则兜底
    try:
        hit = _owner.rule_match(seg)
        if hit:
            return {"action": hit["action"], "params": hit.get("params", {})}
    except Exception:
        pass
    return None


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
_FILLER_HEAD = ("帮我把", "把车", "帮个忙把", "帮我", "麻烦你", "麻烦", "帮忙", "请", "给", "让", "把", "将", "先")
_PARTICLE_TAIL = ("一次", "一下下", "两下", "三下", "一下", "呗", "呀", "啦", "哦", "哈", "嘛")


def _norm_seg(seg):
    """片段规范化：循环剥前缀填充词/尾部量词，产出干净动作短语。"""
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


def _compound_local(text):
    """复合指令本地快解析：全部片段命中才返回动作清单，否则 None（退回 LLM）。"""
    segs = _seg_split(text)
    if len(segs) < 2:
        return None
    acts = []
    for seg in segs:
        # 全量规则优先于路由/粗规则：「空调开到22°C」片段会被粗规则「空调开」吞成
        # 不带温度的 auto_conditioning_start，温度丢失（2026-10-22）
        hit = _local_full_match(seg) or _route_seg(seg)
        if not hit:
            return None
        acts.append(hit)
    return acts




_LOCAL_FULL_RULES = [
    # (关键词元组, action, params 或 callable(text)->params)
    (("充电口", "开充电口", "打开充电口"), "charge_port_door_open", {}),
    (("关充电口", "关闭充电口", "充电口关上"), "charge_port_door_close", {}),
    (("开始充电", "开始充电吧", "充会电", "充下电"), "charge_start", {}),
    (("停止充电", "停止充电吧", "别充了", "结束充电"), "charge_stop", {}),
    (("开窗", "打开车窗", "开车窗", "车窗打开"), "window_control", {"command": "vent"}),
    (("关窗", "关闭车窗", "关车窗", "车窗关上", "车窗关好"), "window_control", {"command": "close"}),
    (("开哨兵", "开哨兵模式", "开启哨兵", "哨兵打开"), "set_sentry_mode", {"on": True}),
    (("关哨兵", "关哨兵模式", "关闭哨兵", "哨兵关掉"), "set_sentry_mode", {"on": False}),
    (("开后备箱", "打开后备箱", "后备箱打开", "开尾箱"), "actuate_trunk", {"which_trunk": "rear"}),
    (("关后备箱", "关闭后备箱", "后备箱关上", "关尾箱"), "close_trunk", {}),
    (("开前备箱", "打开前备箱", "前备箱打开", "开前盖", "打开前盖"), "actuate_trunk", {"which_trunk": "front"}),
    (("关前备箱", "关闭前备箱", "前备箱关上", "关前盖"), "actuate_trunk_front_close", {}),
    # 解锁必须先于锁车判断（“解锁车辆”含“锁车”子串，run.py 2026-08-31 同款修复；
    # 此表曾漏同步 → 「解锁车辆」被解析成 door_lock，锁/解锁颠倒属危险级）
    (("解锁", "解锁车辆", "开门", "打开车门", "车门打开"), "door_unlock", {}),
    (("锁车", "锁门", "把车锁", "车上锁", "锁好车", "落锁", "锁一下车"), "door_lock", {}),
    (("闪灯", "闪一下灯", "闪个灯", "灯闪一下"), "flash_lights", {}),
    (("鸣笛", "按喇叭", "响个喇叭", "喇叭响一下"), "honk_horn", {}),
    (("开空调", "打开空调", "空调打开", "开冷气", "开冷风", "开暖风", "开热风", "制冷", "制热"), "auto_conditioning_start", {}),
    (("关空调", "关闭空调", "空调关掉", "关冷气", "关暖风"), "auto_conditioning_stop", {}),
    (("开方向盘加热", "打开方向盘加热", "方向盘加热打开", "方向盘加热"), "remote_steering_wheel_heater_request", {"on": True}),
    (("充电"), "charge_start", {}),
    (("关方向盘加热", "关闭方向盘加热", "方向盘加热关掉"), "remote_steering_wheel_heater_request", {"on": False}),
    (("开车窗通风", "车窗通风"), "window_control", {"command": "vent"}),
]

_TEMP_RE = re.compile(r"(\d{2}(?:\.5)?|\d{1})(?:\s*度|\s*°C|\s*℃)")
_SEAT_LEVEL_RE = re.compile(r"([一二三1-3])\s*档")
_CN_DIG = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_TEMP_RE = re.compile(r"([零一二三四五六七八九十]{2,3}(?:点[零一二三四五六七八九])?)(?=\s*度|\s*°C|\s*℃)")


def _cn_temp_norm(t):
    """中文数字温度标准化：「空调二十一度」「二十三点五度」→ 21度/23.5度（1-99 含 .5）"""
    def _c2n(s):
        if "点" in s:
            a, _, b = s.partition("点")
            v = _c2n(a)
            dec = _CN_DIG.get(b[-1], 0) if b else 0
            return None if v is None else v + dec / 10
        if s == "十":
            return 10
        if "十" in s:
            a, _, b = s.partition("十")
            return (_CN_DIG.get(a, 1) if a else 1) * 10 + (_CN_DIG.get(b, 0) if b else 0)
        return _CN_DIG.get(s)
    def _repl(m):
        v = _c2n(m.group(1))
        sv = ("%g" % v) if v is not None else None
        return sv if sv is not None else m.group(1)
    return _CN_TEMP_RE.sub(_repl, t or "")


def _local_full_match(text):
    """全量本地匹配：返回动作 dict 或 None。支持「空调24度」数值与「座椅加热2档」档位。"""
    if not text:
        return None
    t = _cn_temp_norm(text.strip())   # 2026-10-06 修：「空调二十一度」中文数字温度无法识别
    # 数值温度：「空调24度」「温度调到22.5」
    if ("空调" in t or "温度" in t) and any(w in t for w in ("度", "°C", "℃")):
        m = _TEMP_RE.search(t)
        if m:
            try:
                v = float(m.group(1))
                if 15 <= v <= 28:
                    return {"action": "set_temps", "params": {"driver_temp": v}}
            except ValueError:
                pass
    # 充电限制（2026-10-07 第三屏±按钮/语音「充电到80」「充电上限85」）：50-100
    if any(w in t for w in ("充电限制", "充电上限", "充电到", "充到", "充至")):
        m = re.search(r"([0-9]{2,3})\s*%?", t)
        if not m:
            _cn = re.search(r"([一二三四五六七八九]+[百十][一二三四五六七八九]?|[一二三四五六七八九十])", t)
            if _cn:
                s = _cn.group(1)
                if "百" in s:
                    m = re.match(r"\d+", "100")
                elif "十" in s:
                    a, _, b = s.partition("十")
                    n10 = _CN_DIG.get(a, 1) * 10 + (_CN_DIG.get(b, 0) if b else 0)
                    m = re.match(r"\d+", str(n10))
                else:
                    m = re.match(r"\d+", str(_CN_DIG.get(s, 0) or 0))
        if m:
            try:
                v = int(m.group(0))
                if 50 <= v <= 100:
                    return {"action": "set_charge_limit", "params": {"percent": v}}
            except ValueError:
                pass
    # 座椅加热/通风 + 档位
    if "座椅" in t or "座位" in t:
        m = _SEAT_LEVEL_RE.search(t)
        level = 0
        if m:
            c = m.group(1)
            level = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}.get(c, 1)
        if "加热" in t or "热" in t:
            act = "remote_seat_heater_request"
            return {"action": act, "params": {"seat_position": 0, "level": level}}
        if "通风" in t or "凉" in t:
            act = "remote_seat_cooler_request"
            return {"action": act, "params": {"seat_position": 0, "seat_cooler_level": max(level, 1)}}
    for keys, act, params in _LOCAL_FULL_RULES:
        if any(k in t for k in keys):
            return {"action": act, "params": dict(params)}
    return None


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

def resolve_actions(user_text):
    """原样还塕 run_text 的解析逻辑：把自然语言转成动作清单。
    返回 (actions_list, err_or_None)。err 为退出文案（不再继续执行）。"""
    user_text = _canon_cmd(user_text)
    # 规则快匹配（复用主 run.py 的 RULE_MAP/rule_match）
    # 复合指令连接词检查：如果含“顺便/然后/再/并且/且/还有”或者内嵌逗号隔开多个动作，
    # 优先跳过单动作规则快匹配走 LLM 解析。
    # 注意：语音转写常带尾逗号（“解锁车辆，”），尾标点只是杂质不算复合；先去尾标点再判。
    _st = (user_text or "").strip().rstrip("，,。.、!！?？ ").strip()
    # [fix 2026-09-25] "帮我解锁/请锁车"这类礼貌前缀不在 run.py 规则表里，原样匹配会失配
    # 落入 LLM 解析（key 失效时直接报 401）。剥掉前缀后再匹配。
    _norm_text = user_text or ""
    for _p in ("帮我", "麻烦你", "麻烦", "帮忙", "请"):
        if _norm_text.startswith(_p) and len(_norm_text) > len(_p):
            _norm_text = _norm_text[len(_p):]
            break
    # 充电限制（2026-10-07 第三屏±按钮发送「充电限制NN」/语音「充电到80」）：去空格整句匹配，
    # 复合句（如「充到80然后锁车」）不劫持，照走原解析
    _t_cl = (_norm_text or user_text or "").replace(" ", "")
    _mcl = re.fullmatch(r"(?:充电限制|充电上限|充电到|充到|充至)([0-9]{2,3}|[一二三四五六七八九]+[百十][一二三四五六七八九]?|[一二三四五六七八九十])%?", _t_cl or "")
    if _mcl:
        _s = _mcl.group(1)
        if _s.isdigit():
            _n = int(_s)
        elif "百" in _s:
            _n = 100
        elif "十" in _s:
            _a, _, _b = _s.partition("十")
            _n = _CN_DIG.get(_a, 1) * 10 + (_CN_DIG.get(_b, 0) if _b else 0)
        else:
            _n = _CN_DIG.get(_s, 0)
        if _n:
            return [{"action": "set_charge_limit",
                     "params": {"percent": max(50, min(100, _n))}}], None
    # [fix 2026-10-22] ASR 常在中文词间插空格（「空调 开到 22 度」）：空格把整句撕成片段，
    # 被误判复合后温度匹配失败、落入 LLM（key 失效即引导文案）。单动作温度句先按去空格
    # 整句匹配（与上方充电限制同思路）；含连接词/分隔符的复合句不劫持，照走原解析。
    _t_tm = (_norm_text or user_text or "").replace(" ", "")
    if _t_tm and not _mcl and ("空调" in _t_tm or "温度" in _t_tm) \
            and any(w in _t_tm for w in ("度", "°C", "℃")) \
            and not any(w in _t_tm for w in ("然后", "接着", "再", "并且", "而且", "还有", "同时", "顺便", "以及", "加上")) \
            and not any(c in _t_tm for c in "，,、;；"):
        # 纯温度句才捷径：温度词之前还有其它动作关键词（「解锁车辆闪灯空调二十度」）
        # ＝粘连复合句，交回复合链路逐段解析，否则会吞掉前面的动作
        _ac_pos = min([_t_tm.find(w) for w in ("空调", "温度") if w in _t_tm])
        if not _GLUED_ACT_PAT.search(_t_tm[:_ac_pos]):
            _tt = _cn_temp_norm(_t_tm)
            _mtemp = _TEMP_RE.search(_tt)
            if _mtemp:
                try:
                    _tv = float(_mtemp.group(1))
                    if 15 <= _tv <= 28:
                        return [{"action": "set_temps", "params": {"driver_temp": _tv}}], None
                except ValueError:
                    pass
    is_compound = (any(w in _st for w in ["顺便", "然后", "再", "并且", "而且", "还有", "同时", "加上", "以及"]) or
                   "，" in _st or "、" in _st or "," in _st)
    # 多片段（含空格分隔，如"锁车 鸣笛 闪灯"）也算复合：本地能全拆就本地走
    _segs_pre = _seg_split(user_text)
    if not is_compound and len(_segs_pre) >= 2:
        is_compound = True
    # 复合指令先走本地快解析（毫秒级），全部命中即返回，不走云端 LLM
    # [fix 2026-09-25] 本地兜底：run.py 规则表只有「解锁车辆」长形，裸「解锁/帮我解锁」
    # 会失配落入 LLM（key 失效即 401）。这里直接本地判定。
    # [fix 2026-10-05] 复合句（粘连/多段）不得被此短路拦成单动作——曾致
    # 「解锁车辆打开空调闪灯」只执行解锁（短路排在复合解析之前）。补 not is_compound。
    if not is_compound and "解锁" in (_norm_text or user_text) and "锁定" not in (_norm_text or user_text):
        return [{"action": "door_unlock", "params": {}}], None
    _whole_rule = _owner.rule_match(_norm_text or user_text)
    # [fix 2026-10-22] 粗规则（run.py RULE_MAP 是子串匹配）会劫持更具体的说法：
    #   a) 「空调开到22°C」含关键词「空调开」→ 被劫持成不带温度的开空调；
    #   b) 「关后备箱」含关键词「后备箱」→ 被劫持成开后备箱（关变开，危险）。
    # 粗规则命中空调开关/后备箱时先试更具体的全量规则，命中即采纳
    # （set_temps 执行链隐含开空调；close_trunk / 前备箱关同在表内）。
    if not is_compound and _whole_rule and _whole_rule.get("action") in ("auto_conditioning_start", "auto_conditioning_stop", "actuate_trunk"):
        _mt = _local_full_match(_norm_text or user_text)
        if _mt and _mt.get("action") in ("set_temps", "close_trunk", "actuate_trunk_front_close"):
            return [_mt], None
    if is_compound:
        _local = _compound_local(user_text)
        if _local and len(_local) >= 2:
            return _local, None
        # [perf-fix 2026-09-14] 含'还有/再'的车况句误判复合 -> 整句规则直通
        # 「车还有多少电」因含'还有'被判复合，拆分（'车'/'多少电'）又凑不出多动作，
        # 于是白等云端 LLM 2~13s。修正：非真·多动作复合时，整句规则命中即采信。
        _hit_cnt = 0
        for _s in _segs_pre:
            try:
                if _route_seg(_s):
                    _hit_cnt += 1
            except Exception:
                pass
        if _hit_cnt < 2 and _whole_rule:
            return [{"action": _whole_rule["action"], "params": _whole_rule.get("params", {})}], None
        if _local:
            return _local, None
    # [2026-09-29] 本地全量规则：指令集合是封闭的（白名单动作+常见说法），
    # 能本地解析的绝不依赖 LLM（key 失效/超时都不影响常用指令）。
    _norm = _norm_text or user_text
    if not is_compound and not _whole_rule:
        _m = _local_full_match(_norm)
        if _m:
            return [_m], None
    intent = _whole_rule if not is_compound else None
    if intent is None:
        # 调用 intent.py 支持复合指令与 LLM 意图解析
        env = os.environ.copy()
        env["VECTIDE_API_KEY"] = os.environ.get("VECTIDE_API_KEY", "")
        try:
            out = subprocess.run(
                [sys.executable, os.path.join(OWNER_BASE, "intent.py"), user_text],
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=25,
            ).stdout.strip()
            intent = json.loads(out)
        except Exception:
            # LLM 解析超时/失败：退回本地单动作规则，避免用户干等（复合指令则给引导）
            _fb = _owner.rule_match(user_text)
            if _fb:
                return [_fb], None
            return None, CHAT_GUIDE
    if not intent or "error" in intent:
        # [2026-09-29] LLM 出错（key 失效/超时等）一律给引导，不透出原始错误（如 401）
        return None, CHAT_GUIDE

    actions_list = intent.get("actions")
    if not actions_list and "action" in intent:
        actions_list = [{"action": intent["action"], "params": intent.get("params", {})}]
    if not actions_list:
        return None, "❌ 未识别到可执行的车辆指令"
    return actions_list, None


def describe_action(item):
    """把单个 action 转成人类可读的中文描述，供“已收到N个任务”中间态展示。"""
    act = (item or {}).get("action", "")
    par = (item or {}).get("params", {}) or {}
    base = _owner.action_cn(act) if act in ALLOWED_ACTIONS else act
    if act == "set_temps":
        return f"{base} {par.get('driver_temp', 22)}°C"
    if act == "remote_seat_heater_request":
        pos = "主驾" if par.get("seat_position", 0) == 0 else "副驾"
        return f"{base}: {pos} {par.get('level', 1)}档"
    if act == "remote_seat_cooler_request":
        pos = "主驾" if par.get("seat_position", 0) == 0 else "副驾"
        return f"{base}: {pos} {par.get('seat_cooler_level', 1)}档"
    if act == "remote_steering_wheel_heater_request":
        return f"{base}: {'开' if par.get('on') else '关'}"
    if act in ("auto_conditioning_start", "auto_conditioning_stop", "door_lock",
               "door_unlock", "charge_start", "charge_stop", "set_sentry_mode"):
        if act == "set_sentry_mode":
            return f"{base}: {'开' if par.get('on') else '关'}"
    return base


def run_text(uid, user_text):
    """给用户的自然语言指令做主链路（与 run.py main 对齐，但走用户 token/VIN）。"""
    vid = get_vin(uid)
    if not vid:
        return "❌ 你还没绑定车辆，请先完成绑定"
    vin = vid
    actions_list, resolve_err = resolve_actions(user_text)
    if resolve_err:
        return resolve_err

    # 单动作走原逻辑
    if len(actions_list) == 1:
        act_item = actions_list[0]
        action = act_item.get("action", "")
        params = act_item.get("params", {})
        if action in FORBIDDEN or action not in ALLOWED_ACTIONS:
            return f"❌ 该操作({action})不在允许范围内"


        if action == "status":
            st = load_user_settings(uid)
            v = read_vehicle(uid, vin)
            if not v:
                wake_vehicle(uid, vin, get_vehicle_id(uid), wait_online=True)
                v = read_vehicle(uid, vin)
                if not v:
                    return "❌ 读车况失败"
            cs, vs, cl = (v.get("charge_state") or {}), (v.get("vehicle_state") or {}), (v.get("climate_state") or {})
            state_cn = _owner.STATE_CN.get(str(v.get('state')).lower(), v.get('state') or '未知')
            charge_cn = _owner.CHARGING_CN.get(cs.get('charging_state'), cs.get('charging_state') or '未知')
            lock_cn = "已锁" if vs.get('locked') else "未锁"
            bats = cs.get('battery_level')
            rng = cs.get('battery_range')
            in_t = cl.get('inside_temp')
            out_t = cl.get('outside_temp')
            rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
            in_s = f"{round(in_t, 1)}°C" if in_t is not None else "?"
            out_s = f"{round(out_t, 1)}°C" if out_t is not None else "?"
            lines = [f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_cn} | 状态 {state_cn}",
                     f"🌡️ 车内 {in_s} / 车外 {out_s} | 充电 {charge_cn}"]
            if st.get("replyShowWeather", True):
                w = _owner.get_weather()
                if w:
                    t = w.get("temp") or "?"
                    f = w.get("feels") or t
                    lines.append(f"⛅ {w.get('city')} {t}°C 体感{f}°C {w.get('weather')} 湿度{w.get('humidity')}%")
                    if st.get("replyShowTip", True):
                        adv = _owner.weather_advice(w)
                        if adv:
                            lines.append(f"💡 {adv}")
            return "\n".join(lines)
        # 智能温控 v2（2026-09-15）：先唤醒读真实车内温度 → 按基线21°C分档 → 执行 → 加料 → 过冲回调
        if action == "auto_conditioning_start":
            st = load_user_settings(uid)
            veh_id = get_vehicle_id(uid)
            plan = smart_climate_plan(uid, vin)
            temp = plan["temp"]
            # [fix 2026-09-29 终版实测] auto_conditioning_start 执行时会把车机温度改写为
            # 其内部上次值（实测把刚写入的 24 覆盖回 15）。顺序定为：start 先点火，
            # set_temps 收尾锁定目标温度——最后写入者永远是我们。
            # [2026-09-29 实测定稿] 唯一全绿组合：stop → set_temps(双区,停机态真实生效)
            # → 3s → start。运行中 set_temps 被该车固件拒绝（假成功），必须走停机态写入。
            proxy_command(uid, vin, "auto_conditioning_stop", {})
            time.sleep(2)
            ok2, t2 = set_temp_keep_passenger(uid, vin, temp)
            time.sleep(3)
            ok1, t1 = proxy_command(uid, vin, "auto_conditioning_start", {})
            if not (ok1 and ok2):
                return (f"❌ 开启空调失败（开启={'成功' if ok1 else '失败'}, 设温={'成功' if ok2 else '失败'}）："
                        f"{t1 or t2}")
            extra_cn = climate_addons(uid, vin, plan["extra"]) if plan["extra"] else ""
            if plan["aggressive"]:
                start_climate_watch(uid, veh_id, temp, plan.get("pref") or CLIMATE_PREF)
            lines = [climate_head(plan, extra_cn)]
            if st.get("replyShowCar", True):
                v2 = read_vehicle(uid, vin)
                if v2:
                    cs, vs2, cl2 = (v2.get("charge_state") or {}), (v2.get("vehicle_state") or {}), (v2.get("climate_state") or {})
                    bats = cs.get("battery_level")
                    rng = cs.get("battery_range")
                    in_t = cl2.get("inside_temp")
                    rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
                    in_s = f"{round(in_t, 1)}C" if in_t is not None else "?"
                    lock_s = "已锁" if vs2.get("locked") else "未锁"
                    lines.append(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s.replace('C', '°C')}")
            if st.get("replyShowWeather", True):
                w = _owner.get_weather()
                if w:
                    t = w.get("temp") or "?"
                    f = w.get("feels") or t
                    lines.append(f"⛅ {w.get('city')} {t}°C 体感{f}°C {w.get('weather')} 湿度{w.get('humidity')}%")
                    if st.get("replyShowTip", True):
                        adv = _owner.weather_advice(w)
                        if adv:
                            lines.append(f"💡 {adv}")
            return "\n".join(lines)
        path = ACTION_PATHS.get(action)
        if not path:
            return f"未知动作: {action}"

        if action == "remote_seat_heater_request" and params.get("level", 0) > 0:
            try:
                proxy_command(uid, vin, "auto_conditioning_start", {})
            except Exception:
                pass
        if action == "set_temps":
            # [2026-10-06] 温度指令隐含「开空调」：先点火（幂等，已运行无害），再直调设温——
            # start 会重置车机温度，set_temps 收尾锁定目标（最后写入者永远是我们）。
            # 直调+回读校验+重试；全失败落「停机态写入」保底（stop → set → 3s → start）。
            _target = params.get("driver_temp", 21)
            proxy_command(uid, vin, "auto_conditioning_start", {})
            if set_temps_verified(uid, vin, _target):
                learn_phrase(user_text, action, params)
                v_after = read_vehicle(uid, vin)
                w_after = _owner.get_weather()
                return fmt_report(uid, vin, True, action, f"空调已开启，设定 {_target}°C", v_after, w_after)
            # 保底：停机态写入（09-29 定稿组合）
            proxy_command(uid, vin, "auto_conditioning_stop", {})
            time.sleep(2)
            _ok2, _t2 = set_temp_keep_passenger(uid, vin, _target)
            time.sleep(3)
            _ok1, _t1 = proxy_command(uid, vin, "auto_conditioning_start", {})
            if not (_ok1 and _ok2):
                return (f"❌ 设定温度失败（开启={'成功' if _ok1 else '失败'}, 设温={'成功' if _ok2 else '失败'}）："
                        f"{_t1 or _t2}")
            v_after = read_vehicle(uid, vin)
            w_after = _owner.get_weather()
            return fmt_report(uid, vin, True, action, f"空调已开启，设定 {_target}°C", v_after, w_after)

        if action == "set_charge_limit":
            # 充电上限（2026-10-07 第三屏±/语音「充电到80」）：执行+回读确认
            _pct = max(50, min(100, int(params.get("percent", 0) or 0)))
            ok, msg = proxy_command(uid, vin, "set_charge_limit", {"percent": _pct})
            if ok:
                learn_phrase(user_text, action, params)
                time.sleep(2)
                v_after = read_vehicle(uid, vin)
                _real = ((v_after or {}).get("charge_state") or {}).get("charge_limit_soc")
                detail = f"充电上限已设为 {int(float(_real)) if _real is not None else _pct}%"
                return fmt_report(uid, vin, True, action, detail, v_after, _owner.get_weather())
            return f"❌ 设置充电上限失败：{msg or '车未响应'}"

        if action == "remote_seat_cooler_request" and params.get("seat_cooler_level", 0) > 0:
            try:
                proxy_command(uid, vin, "auto_conditioning_start", {})
            except Exception:
                pass

        import threading
        box = {}
        def _command():
            box["ok"], box["text"] = proxy_command(uid, vin, path, params)
        th = threading.Thread(target=_command)
        th.start()
        v_after = read_vehicle(uid, vin)
        w_after = _owner.get_weather()
        th.join()
        ok, text = box.get("ok", False), box.get("text", "")
        if ok:
            learn_phrase(user_text, action, params)
        return fmt_report(uid, vin, ok, action, text or "", v_after, w_after)

    # ===== 多动作复合执行管道 =====
    import threading
    results = []
    st = load_user_settings(uid)
    has_status_action = any(item.get("action") == "status" for item in actions_list)
    exec_actions = [item for item in actions_list if item.get("action") != "status"]

    # 1. 开空调前置（智能温控 v2：先唤醒读真实车内温度，再定温+加料+过冲监测）
    has_ac_start = any(item.get("action") == "auto_conditioning_start" for item in exec_actions)
    has_explicit_temp = any(item.get("action") == "set_temps" for item in exec_actions)
    if has_ac_start:
        ac_plan = smart_climate_plan(uid, vin)
        ok, msg = proxy_command(uid, vin, "auto_conditioning_start", {})
        if ok:
            detail = f"已开启"
            if not has_explicit_temp:
                detail += f"，设定{ac_plan['temp']}°C"
            results.append(("auto_conditioning_start", True, detail))
            if not has_explicit_temp:
                ok2, msg2 = set_temp_keep_passenger(uid, vin, ac_plan["temp"])
                if ok2:
                    results.append(("set_temps", True, f"设定 {ac_plan['temp']}°C"))
                    if ac_plan["extra"]:
                        extra_cn = climate_addons(uid, vin, ac_plan["extra"])
                        if extra_cn:
                            add_act = "remote_seat_cooler_request" if ac_plan["extra"] == "cool" else "remote_seat_heater_request"
                            results.append((add_act, True, extra_cn))
                    if ac_plan["aggressive"]:
                        start_climate_watch(uid, get_vehicle_id(uid), ac_plan["temp"], ac_plan.get("pref") or CLIMATE_PREF)
                else:
                    results.append(("set_temps", False, msg2 or "设温失败"))
        else:
            results.append(("auto_conditioning_start", False, msg or "开启失败"))

    # 2. 其余动作并行
    def _exec_item(item):
        act = item.get("action")
        par = item.get("params", {})
        if act == "auto_conditioning_start":
            return
        if act in FORBIDDEN or act not in ALLOWED_ACTIONS:
            results.append((act, False, "操作受限"))
            return
        path = ACTION_PATHS.get(act)
        if not path:
            results.append((act, False, "未知动作"))
            return
        if act == "remote_seat_heater_request" and par.get("level", 0) > 0 and not has_ac_start:
            try: proxy_command(uid, vin, "auto_conditioning_start", {})
            except Exception: pass
        if act == "remote_seat_cooler_request" and par.get("seat_cooler_level", 0) > 0 and not has_ac_start:
            try: proxy_command(uid, vin, "auto_conditioning_start", {})
            except Exception: pass

        if act == "set_temps":
            # [fix 2026-10-22] 复合管道此前直调 set_temps：该车固件在空调未运行时拒绝/假成功
            # （09-29 定稿），单动作链路早有「先点火→直调→回读校验→失败落停机态写入」专用
            # 分支，复合清单漏配同款 → 「解锁车辆，闪灯，空调开到22°C」只执行前两个动作。
            # 此处补齐同语义（复合并行线程内 sleep 只阻塞本动作，不影响其余动作）。
            _target = par.get("driver_temp", 21)
            proxy_command(uid, vin, "auto_conditioning_start", {})
            if set_temps_verified(uid, vin, _target):
                results.append((act, True, f"空调已开启，设定 {_target}°C"))
            else:
                # 保底：停机态写入（09-29 定稿组合）
                proxy_command(uid, vin, "auto_conditioning_stop", {})
                time.sleep(2)
                _ok2, _t2 = set_temp_keep_passenger(uid, vin, _target)
                time.sleep(3)
                _ok1, _t1 = proxy_command(uid, vin, "auto_conditioning_start", {})
                _ok = bool(_ok1 and _ok2)
                results.append((act, _ok, f"空调已开启，设定 {_target}°C" if _ok else
                                f"设温失败（开启={'成功' if _ok1 else '失败'}, 设温={'成功' if _ok2 else '失败'}）"))
            return

        ok, text = proxy_command(uid, vin, path, par)
        desc = "已完成" if ok else (text or "失败")
        if ok:
            if act == "set_temps":
                desc = f"设定 {par.get('driver_temp', 22)}°C"
            elif act == "remote_seat_cooler_request":
                pos = "主驾" if par.get("seat_position", 0) == 0 else "副驾"
                desc = f"{pos}通风 {par.get('seat_cooler_level', 1)}档"
            elif act == "remote_seat_heater_request":
                pos = "主驾" if par.get("seat_position", 0) == 0 else "副驾"
                desc = f"{pos}加热 {par.get('level', 1)}档"
        results.append((act, ok, desc))

    threads = []
    for item in exec_actions:
        if item.get("action") == "auto_conditioning_start":
            continue
        t = threading.Thread(target=_exec_item, args=(item,))
        t.start()
        threads.append(t)

    # 车况/天气与命令下发并行取，免掉执行完再串行追加的网络往返。
    # 但“等在线”必须等命令线程全部结束：否则车休眠场景下命令线程里的唤醒自愈还没完成，
    # 这里会对着 asleep 的干等 30 秒拿到陈旧车况。
    box = {}

    def _after():
        for t in threads:
            t.join()
        box["v"] = read_vehicle(uid, vin)
        box["w"] = _owner.get_weather()

    th_after = threading.Thread(target=_after)
    th_after.start()
    th_after.join(timeout=60)
    v_after, w_after = box.get("v"), box.get("w")

    # 3. 聚合输出
    lines = []
    succ_cnt = sum(1 for _, ok, _ in results)
    total_cnt = len(results)
    if total_cnt > 0:
        head_mark = "✅" if succ_cnt == total_cnt else ("⚠️" if succ_cnt > 0 else "❌")
        lines.append(f"{head_mark} 复合指令执行完成（{succ_cnt}/{total_cnt}）")
        for act, ok, desc in results:
            mark = "·" if ok else "×"
            lines.append(f" {mark} {_owner.action_cn(act)}: {desc}")
    elif has_status_action:
        lines.append("✅ 车况已更新")

    if v_after and st.get("replyShowCar", True):
        cs, vs2, cl2 = (v_after.get("charge_state") or {}), (v_after.get("vehicle_state") or {}), (v_after.get("climate_state") or {})
        bats = cs.get("battery_level")
        rng = cs.get("battery_range")
        in_t = cl2.get("inside_temp")
        rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
        in_s = f"{round(in_t, 1)}C" if in_t is not None else "?"
        lock_s = "已锁" if vs2.get("locked") else "未锁"
        lines.append(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s.replace('C', '°C')}")
    if w_after and st.get("replyShowWeather", True):
        t = w_after.get("temp") or "?"
        f = w_after.get("feels") or t
        lines.append(f"⛅ {w_after.get('city')} {t}°C 体感{f}°C {w_after.get('weather')} 湿度{w_after.get('humidity')}%")
        if st.get("replyShowTip", True):
            adv = _owner.weather_advice(w_after)
            if adv:
                lines.append(f"💡 {adv}")
    return "\n".join(lines)


def json_state(uid):
    """返回结构化车况（供 /api/state 下发 vehicle_data）。car 在线才返回新鲜字段。
    返回: {"fresh": bool, "fields": {...}}  fields 只在 fresh=True 时有效。
    字段对齐前端多态同步：仅用真实存在的回读字段，杜绝按文本瞎猜。
    """
    vin = get_vin(uid)
    if not vin:
        return {"fresh": False, "reason": "unbound"}
    v = read_vehicle(uid, vin)
    if not v:
        # 可能在休眠/离线；尝试唤醒重读同 status 逻辑
        wake_vehicle(uid, vin, get_vehicle_id(uid), wait_online=True)
        v = read_vehicle(uid, vin)
    if not v:
        return {"fresh": False, "reason": "unavailable"}
    cs = v.get("charge_state") or {}
    vs = v.get("vehicle_state") or {}
    cl = v.get("climate_state") or {}
    swh = cl.get("steering_wheel_heater")
    # 判定车辆是否配备/支持前排座椅通风：
    # 支持通风的车型在 climate_state 中会有 climate_seat_cooling_front_left/right 或 SeatVentEnabled
    cooler_left = cl.get("climate_seat_cooling_front_left")
    if cooler_left is None:
        cooler_left = cl.get("seat_cooler_left")
    
    has_cooling = False
    if cl.get("SeatVentEnabled") is True or cl.get("has_seat_cooling") is True:
        has_cooling = True
    elif cooler_left is not None:
        has_cooling = True

    cooler_level = int(cooler_left) if cooler_left is not None else 0

    fields = {
        "online": True,
        "locked": bool(vs.get("locked")),
        "sentry_mode": bool(vs.get("sentry_mode")),
        "is_climate_on": bool(cl.get("is_climate_on")),
        "steering_wheel_heater": (int(swh) > 0) if swh is not None else None,
        "has_seat_cooling": has_cooling,
        "seat_cooler_level": cooler_level,
        "charging": bool(cs.get("charging_state") in ("Charging", "Starting", "Complete")),
        "battery_level": cs.get("battery_level"),
        "charging_state": cs.get("charging_state"),
        "driver_temp_setting": cl.get("driver_temp_setting"),
        "inside_temp": cl.get("inside_temp"),
    }
    return {"fresh": True, "fields": fields}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python user_runner.py <uid> \"指令\"")
        sys.exit(1)
    uid = sys.argv[1]
    text = " ".join(sys.argv[2:])
    if not user_exists(uid):
        print("❌ 用户不存在或未绑定")
        sys.exit(1)
    if text.strip() == "__state__":
        print(probe_state(uid))
        sys.exit(0)
    if text.strip() == "__json__":
        # 结构化车况：浏览器状态同步用（fresh + fields）
        print(json.dumps(json_state(uid), ensure_ascii=False))
        sys.exit(0)
    if text.startswith("__selfcmd2__"):
        # 调试：__selfcmd2__ <path> <json_params>
        rest = text[len("__selfcmd2__"):].strip()
        pth, _, pj = rest.partition(" ")
        vid, tok = get_vin(uid), get_token(uid)
        data, err = http("POST", f"{PROXY}/api/1/vehicles/{vid}/command/{pth}", headers={
            "Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            body=(pj or "{}"), timeout=60)
        print(json.dumps({"err": err, "data": data}, ensure_ascii=False)[:500])
        sys.exit(0)
    if text.startswith("__selfcmd__"):
        # 调试：用本文件的 http() 直接打代理，验证发送路径与真实返回（不影响正常链路）
        path = text[len("__selfcmd__"):].strip()
        vid = get_vin(uid)
        tok = get_token(uid)
        data, err = http("POST", f"{PROXY}/api/1/vehicles/{vid}/command/{path}", headers={
            "Authorization": f"Bearer {tok}", "Content-Type": "application/json"}, body="{}", timeout=60)
        print(json.dumps({"err": err, "data": data}, ensure_ascii=False)[:400])
        sys.exit(0)
    if text.strip() == "__pairprobe__":
        # 探测控制授权（虚拟钥匙）状态：仅 door_lock 无损探针，供网页判断是否弹配对引导
        print(json.dumps(pair_probe(uid), ensure_ascii=False)); sys.exit(0)
    if text.startswith("__plan__"):
        # 只解析“这几步任务”不执行（供休眠唤醒时的“已收到N个任务”中间态）
        user_text = text[len("__plan__"):].strip()
        actions_list, resolve_err = resolve_actions(user_text)
        if resolve_err:
            print(json.dumps({"ok": False, "error": resolve_err}, ensure_ascii=False))
        else:
            n = len(actions_list)
            print(json.dumps({
                "ok": True, "n": n,
                "tasks": [describe_action(a) for a in actions_list],
                "compound": n > 1,
            }, ensure_ascii=False))
        sys.exit(0)
    print(run_text(uid, text))
    log_line(uid, "cmd", text)