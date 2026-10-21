# -*- coding: utf-8 -*-
"""特斯拉车控总入口：自然语言 -> 意图 -> 执行 -> 结果（带自愈执行层）

用法:
  python run.py "帮我把空调调到25度"
  python run.py "看看车还有多少电"

自愈能力（目标：不受环境影响、成功率高）：
1. token 过期/401 → 自动跑 refresh-token.ps1 刷新后重试
2. 代理未启动/端口不通 → 自动跑 start-proxy.ps1 拉起后重试
3. 网络抖动/5xx → 指数退避重试（3次）
4. 429 限流 → 等待后重试（最多3次）
5. 车辆休眠 → 自动唤醒 + 轮询等 online
6. 每次执行都给明确中文结果，绝不静默失败
"""
import io as _io
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

# 强制 UTF-8 输出（Windows 控制台默认 GBK 会爆 emoji/特殊字符）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 本地代理是自签证书 → 不验证（仅 localhost 使用）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

BASE = r"E:\tesla-fleet"
io_open = _io.open
INTENT = os.path.join(BASE, "intent.py")
WATCH = os.path.join(BASE, "smart_climate_watch.py")
REFRESH = os.path.join(BASE, "refresh-token.ps1")
START_PROXY = os.path.join(BASE, "start-proxy.ps1")
FLEET = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"
VIN = "LRWYGCEK9MC024938"
VID = "367104879939990"
PROXY = "https://localhost:4443"

# ============ 反馈闭环：学习打点（2026-08-31） ============
# 每次动作被正确执行后，把「用户原话 + 动作」上报给语义路由 /learn，
# 让新说法自动纳入向量匹配（越用越准）。同步但极快（~15ms），失败静默，绝不影响主流程。
LEARN_URL = "http://127.0.0.1:8791/learn"


def learn_phrase(text, action, params=None):
    """把成功执行的原话上报学习（火并忘：不影响主流程）。"""
    try:
        body = json.dumps({"text": text, "intent": action, "params": params or {}}).encode("utf-8")
        req = urllib.request.Request(LEARN_URL, data=body,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
    except Exception:
        pass  # 语义路由不可用时静默跳过，不阻断车控

# 智能温控上下限（用户 2026-08-30 确认）
CLIMATE_MIN = 19.0   # 最低设定温度（猛降）
CLIMATE_MAX = 27.0   # 最高设定温度（猛热）
CALLBACK_MARGIN = 3.0  # 过冲回调阈值：进入目标±3°C 温和区间即回调

# ============ 幂等保护（敏感动作 30 秒内不重复执行） ============
IDEMPOTENT_ACTIONS = {
    "actuate_trunk", "honk_horn", "flash_lights", "window_control",
    "door_unlock", "door_lock",
    "remote_seat_heater_request", "remote_steering_wheel_heater_request",
    "media_toggle_playback", "media_next_track", "media_prev_track",
    "auto_conditioning_start", "charge_start", "charge_stop",
}
IDEMPOTENT_FILE = os.path.join(BASE, "idempotency.json")
IDEMPOTENT_WINDOW = 30  # 秒


def check_idempotent(action):
    """若同动作在窗口期内已执行过，返回剩余秒数；否则返回 None。"""
    try:
        if not os.path.exists(IDEMPOTENT_FILE):
            return None
        with open(IDEMPOTENT_FILE, "r", encoding="utf-8") as f:
            rec = json.load(f)
        last = rec.get(action)
        if not last:
            return None
        elapsed = time.time() - last.get("ts", 0)
        remain = IDEMPOTENT_WINDOW - elapsed
        if remain > 0:
            return int(remain)
    except Exception:
        pass
    return None


def mark_idempotent(action):
    """记录动作执行时间。"""
    try:
        rec = {}
        if os.path.exists(IDEMPOTENT_FILE):
            with open(IDEMPOTENT_FILE, "r", encoding="utf-8") as f:
                rec = json.load(f)
        rec[action] = {"ts": time.time()}
        with open(IDEMPOTENT_FILE, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception:
        pass




# 自愈参数
RETRY_NET = 3          # 网络抖动重试次数
RETRY_429 = 3          # 429 限流重试次数
PROXY_WAIT = 5         # 拉起代理后等待秒数
PROXY_PROBE = 3        # 探测代理端口重试次数

# 动作 -> proxy 命令路径
ACTIONS = {
    "auto_conditioning_start": "auto_conditioning_start",
    "honk_horn": "honk_horn",
    "flash_lights": "flash_lights",
    "door_lock": "door_lock",
    "door_unlock": "door_unlock",
    "actuate_trunk": "actuate_trunk",
    "window_control": "window_control",
    "auto_conditioning_stop": "auto_conditioning_stop",
    "set_temps": "set_temps",
    "set_preconditioning_max": "set_preconditioning_max",
    "set_climate_keeper_mode": "set_climate_keeper_mode",
    "remote_steering_wheel_heater_request": "remote_steering_wheel_heater_request",
    "remote_seat_heater_request": "remote_seat_heater_request",
    "remote_seat_cooler_request": "remote_seat_cooler_request",
    "charge_start": "charge_start",
    "charge_stop": "charge_stop",
    "set_charge_limit": "set_charge_limit",
    "set_charging_amps": "set_charging_amps",
    "charge_port_door_open": "charge_port_door_open",
    "charge_port_door_close": "charge_port_door_close",
    "set_scheduled_charging": "set_scheduled_charging",
    "set_sentry_mode": "set_sentry_mode",
    "set_valet_mode": "set_valet_mode",
    "set_vehicle_name": "set_vehicle_name",
    "schedule_software_update": "schedule_software_update",
    "cancel_software_update": "cancel_software_update",
    "wake_up": "wake_up",
    "media_volume_up": "media_volume_up",
    "media_volume_down": "media_volume_down",
    "media_toggle_playback": "media_toggle_playback",
    "media_next_track": "media_next_track",
    "media_prev_track": "media_prev_track",
    "adjust_volume": "adjust_volume",
    "speed_limit_set_limit": "speed_limit_set_limit",
    "trigger_homelink": "trigger_homelink",
    # 只读
    "status": "status",
}


# ============ 自愈基础层 ============

def run_ps(script, args=None):
    """跑 PowerShell 脚本，返回 (返回码, 输出文本)。"""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    if args:
        cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def get_token():
    with open(os.path.join(BASE, "tokens.json"), "r", encoding="utf-8-sig") as f:
        return json.load(f)["access_token"]


def refresh_token_auto():
    """token 失效时自动刷新。返回 (成功, 提示)。"""
    code, out = run_ps(REFRESH)
    if code == 0:
        return True, "token 已自动刷新"
    return False, f"token 刷新失败: {out[-200:]}"


def probe_proxy():
    """探测本地代理 4443 端口。返回 bool。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4443), timeout=3):
            return True
    except Exception:
        return False


def ensure_proxy():
    """确保代理在跑：探测不通就拉起，最多等 PROXY_WAIT*PROXY_PROBE 秒。返回 (ok, msg)。"""
    if probe_proxy():
        return True, "代理正常"
    run_ps(START_PROXY)
    for _ in range(PROXY_PROBE):
        time.sleep(PROXY_WAIT)
        if probe_proxy():
            return True, "代理已自动拉起"
    return False, "代理无法启动（检查 start-proxy.ps1 / exe 是否被删）"


def http(method, url, headers=None, body=None, timeout=40):
    """通用 HTTPS 请求，带网络抖动重试 + 429 限流等待。返回 (json或None, 错误文本)。"""
    data = body.encode("utf-8") if isinstance(body, str) else body
    last_err = ""
    for attempt in range(1, RETRY_NET + 1):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return (json.loads(raw) if raw else None), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 限流：等待后重试
                wait = min(30, 5 * attempt)
                time.sleep(wait)
                last_err = f"429 限流(第{attempt}次,已等{wait}s)"
                if attempt >= RETRY_429:
                    return None, "429 限流多次重试仍失败，请稍后再试"
                continue
            if e.code >= 500:
                # 5xx 不再静默重试：可能是车休眠导致，直接返回给上层决定要不要唤醒
                return None, f"服务端错误 {e.code}"
            # 401/403 等由上层处理
            return None, f"HTTP {e.code}"
        except Exception as e:
            time.sleep(2 * attempt)
            last_err = f"网络错误: {e}"
    return None, last_err or "请求失败"


# ============ 业务层 ============

def proxy_command(path, params, retry_auth=True):
    """调用本地代理发命令。带 token 刷新/代理拉起/休眠唤醒自愈。返回 (成功与否, 结果文本)。

    测试支持：TESLA_MOCK_COMMAND=asleep-wake 模拟「车眠→命令 500→唤醒→重试成功」全链路
    （不调真实 Tesla API，但完整走唤醒等待逻辑，用于插件/自愈链路调试）。
    """
    # 1. 先确保代理活着
    mock = os.environ.get("TESLA_MOCK_COMMAND", "").strip()
    _mock_first = mock == "asleep-wake"
    cnt_file = os.path.join("E:\\tesla-fleet", "_mock_cmd_cnt")
    ok, msg = ensure_proxy()
    if not ok:
        return False, f"代理不可用: {msg}"

    # 2. 发命令：401 刷新 token 重试一次；车休眠 500 → 唤醒后重试（最多 3 次）
    #    （2026-08-31 优化：改为失败才唤醒，避免在线时每次命令都付查询/唤醒开销）
    #    （2026-09-02 优化：第一次 500 后只发 wake_up 不等轮询，立刻重试命令；
    #      第二次仍失败才轮询等 online。目标：休眠场景从 18s 压到 3-8s）
    for attempt in range(3):
        if _mock_first:
            # 测试用：模拟「车眠→第一次命令 500→wake_vehicle 唤醒→第二次成功」
            # 每次进入循环先递增计数器：attempt1=cnt1 车眠错误→wake_vehicle→continue
            # attempt2=cnt2 成功。计数器文件在 wake_vehicle 的 mock 里会重置。
            time.sleep(0.1)
            _cnt = 0
            if os.path.exists(cnt_file):
                try: _cnt = int(open(cnt_file).read().strip() or "0")
                except: _cnt = 0
            _cnt += 1
            try: open(cnt_file, "w").write(str(_cnt))
            except: pass
            if _cnt == 1:
                # 第一次：车眠错误。上层会跳到“车休眠 500 → 唤醒后重试一次”分支
                data, err = None, '{"response":null,"error":"vehicle unavailable","error_description":"vehicle is offline or asleep"}'
                resp = {}
            else:
                data, err = {"response": {"result": True}}, None
                try: os.remove(cnt_file)
                except: pass
            if err:
                e_l = err.lower()
                if ("offline" in e_l or "asleep" in e_l or "vehicle unavailable" in e_l
                        or "服务端错误" in err or "500" in err):
                    # 第一次先只发送唤醒，立即重试；后续失败才等待在线
                    wake_vehicle(wait_online=(attempt > 0))
                    continue
                return False, err
            resp = (data or {}).get("response") or {}
            if resp.get("result") is True:
                return True, (data or {}).get("error_description") or ""
        tok = get_token()
    ok, msg = ensure_proxy()
    if not ok:
        return False, f"代理不可用: {msg}"

    # 2. 发命令：401 刷新 token 重试；车休眠 500 → 先快速重试，再等待在线重试
    #    （2026-08-31 优化：改为失败才唤醒，避免在线时每次命令都付查询/唤醒开销）
    #    （2026-09-02 优化：第一次 wake 不等待 online，立刻重发命令）
    for attempt in range(3):
        tok = get_token()
        url = f"{PROXY}/api/1/vehicles/{VIN}/command/{path}"
        body = json.dumps(params or {})
        data, err = http("POST", url, headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        }, body=body, timeout=60)
        if err:
            # 网络层错误：可能是车休眠导致，唤醒后重试
            # 注意：5xx（如服务端错误 500）也可能是车休眠直接发命令导致，同样唤醒后重试
            e_l = err.lower()
            if ("offline" in e_l or "asleep" in e_l or "vehicle unavailable" in e_l
                    or "服务端错误" in err or "500" in err):
                wake_vehicle(wait_online=(attempt > 0))
                continue
            return False, err
        resp = (data or {}).get("response") or {}
        if resp.get("result") is True:
            return True, (data or {}).get("error_description") or ""
        # 401/token 失效 → 刷新后重试一次
        err_str = str(data or "")
        if retry_auth and ("401" in err_str or "authentication" in err_str.lower() or "invalid token" in err_str.lower()):
            ok_refresh, rmsg = refresh_token_auto()
            if ok_refresh:
                continue
            return False, f"token 已失效且自动刷新失败: {rmsg}"
        # 车休眠 500（vehicle unavailable: vehicle is offline or asleep）→ 唤醒后重试一次
        e_l = err_str.lower()
        if "offline" in e_l or "asleep" in e_l or "vehicle unavailable" in e_l:
            wake_vehicle(wait_online=(attempt > 0))
            continue
        return False, f"失败: {(data or {}).get('error') or ''} {(data or {}).get('error_description') or ''}"


def wake_vehicle(wait_online=True):
    """唤醒车辆并（可选）轮询等在线。返回 (ok, 提示)。

    车休眠时直接发命令会 500（vehicle unavailable: vehicle is offline or asleep），
    所以命令前必须先唤醒（2026-08-31 修复）。

    测试支持：TESLA_MOCK_COMMAND=asleep-wake 联合使用，
    模拟「车已唤醒」快速返回，不调真实 Tesla API，不等真实 45s。
    """
    if os.environ.get("TESLA_MOCK_COMMAND", "").strip() == "asleep-wake":
        wf = os.path.join("E:\\tesla-fleet", "_mock_wake.flag")
        if os.path.exists(wf):
            try: os.remove(wf)
            except: pass
        ms = int(os.environ.get("TESLA_MOCK_WAKE_MS", "1000"))
        time.sleep(ms / 1000.0)
        return True, "车辆已唤醒在线 (mock)"
    tok = get_token()
    h = {"Authorization": f"Bearer {tok}"}
    # 先查状态，已在线直接返回（省一次唤醒）
    data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}", headers=h, timeout=15)
    if err and "401" in err:
        refresh_token_auto()
        tok = get_token()
        h = {"Authorization": f"Bearer {tok}"}
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}", headers=h, timeout=15)
    if not err and (data or {}).get("response", {}).get("state") == "online":
        return True, "车辆已在线"
    # 发唤醒
    http("POST", f"{FLEET}/api/1/vehicles/{VID}/wake_up",
         headers={**h, "Content-Type": "application/json"}, body="{}", timeout=30)
    if not wait_online:
        return True, "唤醒指令已发送"
    # 轮询在线（车休眠唤醒需 10~60 秒）
    # 2026-09-02 优化：先查再睡（原来先睡 5s 再查，唤醒后白等一个间隔），间隔 2s 更早确认在线
    for _ in range(20):
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}", headers=h, timeout=15)
        if not err and (data or {}).get("response", {}).get("state") == "online":
            return True, "车辆已唤醒在线"
        time.sleep(2)
    return False, "唤醒后 40 秒仍未在线，可能信号弱或车辆异常"


def read_vehicle_json():
    """唤醒并读取完整车况 JSON（带自愈）。失败返回 None。"""
    # token 可能失效 → 先探测，401 则刷新
    tok = get_token()
    h = {"Authorization": f"Bearer {tok}"}
    data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}", headers=h, timeout=20)
    if err and "401" in err:
        refresh_token_auto()
        tok = get_token()
        h = {"Authorization": f"Bearer {tok}"}
    # 唤醒
    http("POST", f"{FLEET}/api/1/vehicles/{VID}/wake_up",
         headers={**h, "Content-Type": "application/json"}, body="{}", timeout=30)
    # 轮询在线（车休眠唤醒需 10~60 秒）
    # 2026-09-02 优化：先查再睡（原来先睡 8s 再查），间隔 2s 更早确认在线
    for _ in range(20):
        data, _ = http("GET", f"{FLEET}/api/1/vehicles/{VID}", headers=h, timeout=20)
        if (data or {}).get("response", {}).get("state") == "online":
            break
        time.sleep(2)
    data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}/vehicle_data", headers=h, timeout=40)
    if err or not data:
        return None
    return (data or {}).get("response", {})



def decide_climate(inside_temp, last_set):
    """按温差（车内−上次设定）分档。返回 (目标温度, 模式名, 是否激进需回调)。

    温差 > +25: 极限暴晒 → 最低温 19°C 猛降
    温差 +10~+25: 偏热 → 上次设定 −5°C 快降
    温差 −15~+10: 适中 → 上次设定原样温和维持（省电）
    温差 < −15: 严寒 → 最高温 27°C 猛热 + 方向盘/座椅加热
    """
    delta = inside_temp - last_set
    if delta > 25:
        return CLIMATE_MIN, "猛降", True
    if delta >= 10:
        return round(last_set - 5, 1), "快降", True
    if delta >= -15:
        return round(last_set, 1), "温和维持", False
    return CLIMATE_MAX, "猛热", True


def smart_climate(monitor=True):
    """智能温控：读车况→按温差定档→开空调设温→（可选）后台过冲回调监测。

    返回 (成功与否, 中文结果)。
    """
    # 快速读车况（0.3s，不等待唤醒16s）；车眠读不到也不阻塞——命令本身会唤醒车
    v = read_vehicle_fast()
    cl = (v or {}).get("climate_state", {}) or {}
    inside = cl.get("inside_temp")
    last_set = cl.get("driver_temp_setting")
    if inside is None or last_set is None:
        # 车眠/缺温度：用上次设定（没有就用默认21.0）温和维持，不阻塞等待
        temp = last_set if last_set is not None else 21.0
        mode = "温和维持"
        aggressive = False
        inside = temp  # 温差显示为0（实际未知）
        last_set = temp
    else:
        temp, mode, aggressive = decide_climate(inside, last_set)

    # 两条命令并行发（实测各 ~2.3s：串行 4.5s → 并行 ~2.3s，2026-08-31 优化）
    import threading
    _clim = {}

    def _c1():
        _clim["ok1"], _clim["t1"] = proxy_command("auto_conditioning_start", {})

    def _c2():
        _clim["ok2"], _clim["t2"] = proxy_command("set_temps", {"driver_temp": temp, "passenger_temp": temp})

    th1 = threading.Thread(target=_c1)
    th2 = threading.Thread(target=_c2)
    th1.start(); th2.start()
    th1.join(); th2.join()
    ok1, t1 = _clim.get("ok1", False), _clim.get("t1", "")
    ok2, t2 = _clim.get("ok2", False), _clim.get("t2", "")
    extra = ""
    if aggressive and temp == CLIMATE_MAX:
        # 严寒：方向盘 + 主驾座椅加热（最高档）
        proxy_command("remote_steering_wheel_heater_request", {"on": True})
        proxy_command("remote_seat_heater_request", {"seat_position": 0, "level": 3})
        extra = " | 方向盘+主驾座椅加热"

    monitor_msg = ""
    if not (ok1 and ok2):
        return False, None, None, None, None, f"命令发送失败（开启={'成功' if ok1 else '失败'}, 设温={'成功' if ok2 else '失败'}）：{t1 or t2}"

    if monitor and aggressive:
        try:
            subprocess.Popen(
                [sys.executable, WATCH, str(temp), str(last_set)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            monitor_msg = "智能监测"
        except Exception as e:
            monitor_msg = ""
    return True, mode, temp, inside, last_set, monitor_msg



# ============ 反馈优化：天气聚合 ============
WEATHER_CACHE = os.path.join(BASE, "weather-cache.json")
WEATHER_CACHE_TTL = 600  # 10 分钟缓存

# ============ 中文映射（反馈全中文化） ============
ACTION_CN = {
    "honk_horn": "鸣笛", "flash_lights": "闪灯",
    "door_lock": "锁车", "door_unlock": "解锁",
    "actuate_trunk": "开关后备箱", "window_control": "车窗控制",
    "auto_conditioning_start": "开启空调", "auto_conditioning_stop": "关闭空调",
    "set_temps": "设定温度", "set_preconditioning_max": "极速预处理",
    "set_climate_keeper_mode": "气候保持模式",
    "remote_steering_wheel_heater_request": "方向盘加热",
    "remote_seat_heater_request": "座椅加热", "remote_seat_cooler_request": "座椅通风",
    "charge_start": "开始充电", "charge_stop": "停止充电",
    "set_charge_limit": "设置充电上限", "set_charging_amps": "设置充电电流",
    "charge_port_door_open": "打开充电口", "charge_port_door_close": "关闭充电口",
    "set_scheduled_charging": "定时充电", "set_sentry_mode": "哨兵模式",
    "set_valet_mode": "代客模式", "set_vehicle_name": "修改车名",
    "schedule_software_update": "预约软件更新", "cancel_software_update": "取消软件更新",
    "wake_up": "唤醒车辆", "media_volume_up": "音量加",
    "media_volume_down": "音量减", "media_toggle_playback": "播放/暂停",
    "media_next_track": "下一曲", "media_prev_track": "上一曲",
    "adjust_volume": "调节音量", "speed_limit_set_limit": "设置限速",
    "trigger_homelink": "触发车库门", "status": "查询车况",
}
WEATHER_CN = {
    "clear": "晴", "sunny": "晴", "partly cloudy": "多云",
    "cloudy": "阴", "overcast": "阴天", "mist": "薄雾",
    "fog": "雾", "haze": "霾", "smoke": "烟尘", "dust": "沙尘",
    "light rain": "小雨", "patchy rain nearby": "附近零星小雨",
    "thundery outbreaks in nearby": "附近有雷阵雨",
    "thundery outbreaks possible": "可能有雷阵雨",
    "moderate rain": "中雨", "heavy rain": "大雨",
    "light drizzle": "毛毛雨", "patchy light rain": "零星小雨",
    "light snow": "小雪", "patchy snow nearby": "附近零星小雪",
    "snow": "雪", "sleet": "雨夹雪", "ice pellets": "冰粒",
    "freezing fog": "冻雾", "blowing snow": "吹雪", "blizzard": "暴风雪",
    "showers": "阵雨", "light showers": "小阵雨",
    "torrential rain shower": "暴雨",
}
CHARGING_CN = {
    "Disconnected": "未连接电源", "Charging": "充电中", "Complete": "充电完成",
    "Stopped": "已停止", "Starting": "开始充电", "NoPower": "无电源",
    "CompleteNoPower": "完成(无电源)", "DoorOpen": "门未关", "Invalid": "异常",
}
STATE_CN = {"online": "在线", "offline": "离线", "asleep": "休眠"}


def translate_weather(desc):
    """wttr.in 英文天气描述 → 中文。"""
    if not desc:
        return ""
    d = desc.strip().lower()
    for k, v in WEATHER_CN.items():
        if k in d:
            return v
    return desc


def action_cn(action):
    return ACTION_CN.get(action, action)




def get_weather(city="Shanghai"):
    """获取天气（wttr.in，国内可达），带 10 分钟缓存。失败返回 None，不影响主流程。"""
    now = time.time()
    try:
        if os.path.exists(WEATHER_CACHE):
            c = json.load(io_open(WEATHER_CACHE, encoding="utf-8"))
            if now - c.get("ts", 0) < WEATHER_CACHE_TTL and c.get("data"):
                return c["data"]
    except Exception:
        pass
    try:
        url = f"https://wttr.in/{city}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
        cur = d["current_condition"][0]
        desc = cur.get("weatherDesc") or [{}]
        today = d.get("weather", [{}])[0]
        area = (d.get("nearest_area") or [{}])[0].get("areaName", [{}])[0].get("value", city)
        city_cn = "上海" if str(city).lower() in ("shanghai",) or "poon" in str(area).lower() else area
        data = {
            "city": city_cn,
            "temp": cur.get("temp_C"),
            "feels": cur.get("FeelsLikeC"),
            "weather": translate_weather(desc[0].get("value", "")) if desc else "",
            "humidity": cur.get("humidity"),
            "wind": cur.get("windspeedKmph"),
            "maxt": today.get("maxtempC"),
            "mint": today.get("mintempC"),
        }
        try:
            io_open(WEATHER_CACHE, "w", encoding="utf-8").write(json.dumps({"ts": now, "data": data}, ensure_ascii=False))
        except Exception:
            pass
        return data
    except Exception:
        return None


def weather_advice(w):
    """根据天气给一句短建议。"""
    if not w:
        return ""
    tips = []
    try:
        feels = float(w.get("feels") or w.get("temp") or 0)
    except Exception:
        feels = 0
    desc = (w.get("weather") or "").lower()
    is_rain = any(k in desc for k in ("rain", "thunder", "drizzle", "雨", "雪"))
    if is_rain:
        tips.append("有降水，出门带伞")
    if feels >= 33:
        tips.append("体感炎热，注意防暑")
    elif feels <= 5:
        tips.append("低温，注意保暖")
    elif feels <= 12:
        tips.append("偏凉，记得加衣")
    if not tips:
        tips.append("天气不错，出行愉快")
    return "；".join(tips)


def read_vehicle_fast():
    """快速读车况（不唤醒、不等在线），失败返回 None。用于执行后的车况快照。"""
    try:
        tok = get_token()
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}/vehicle_data",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        if err or not data:
            return None
        return (data or {}).get("response", {})
    except Exception:
        return None


def probe_state():
    """快速探测车状态，不唤醒、5s 超时。返回 state 字符串（online/asleep/offline）或 None。
    供插件判断是否需要先发\"正在唤醒\"提示用。
    测试支持：环境变量 TESLA_MOCK_STATE=asleep|online|offline 可强制返回（不调 Tesla API），用于插件调试。"""
    mock = os.environ.get("TESLA_MOCK_STATE", "").strip()
    if mock in ("online", "asleep", "offline"):
        return mock
    try:
        tok = get_token()
        data, err = http("GET", f"{FLEET}/api/1/vehicles/{VID}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=5)
        if err:
            return None
        return ((data or {}).get("response") or {}).get("state")
    except Exception:
        return None


def print_weather():
    """打印天气+建议到 stdout。"""
    w = get_weather()
    if not w:
        return
    t = w.get("temp") or "?"
    f = w.get("feels") or t
    print(f"⛅ {w.get('city')} {t}°C 体感{f}°C {w.get('weather')} 湿度{w.get('humidity')}%")
    adv = weather_advice(w)
    if adv:
        print(f"💡 {adv}")


def format_report(ok, action, detail="", v=None, w=None):
    """优化反馈：操作结果 + 车况快照 + 天气 + 建议。v/w 可预取。"""
    lines = []
    head = "✅ " if ok else "❌ "
    lines.append(head + action_cn(action) + (f" | {detail}" if detail else ""))
    if v is None:
        v = read_vehicle_fast()
    if v:
        cs, vs, cl = (v.get("charge_state") or {}), (v.get("vehicle_state") or {}), (v.get("climate_state") or {})
        bats = cs.get("battery_level")
        rng = cs.get("battery_range")
        inside = cl.get("inside_temp")
        rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
        in_s = f"{round(inside, 1)}C" if inside is not None else "?"
        lock_s = "已锁" if vs.get("locked") else "未锁"
        lines.append(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s.replace('C', '°C')}")
    if w is None:
        w = get_weather()
    if w:
        t = w.get("temp") or "?"
        f = w.get("feels") or t
        lines.append(f"⛅ {w.get('city')} {t}°C 体感{f}°C {w.get('weather')} 湿度{w.get('humidity')}%")
        adv = weather_advice(w)
        if adv:
            lines.append(f"💡 {adv}")
    return "\n".join(lines)


def read_status():
    """读车况（走 REST，带唤醒逻辑）。返回 (成功, 文本)。"""
    v = read_vehicle_json()
    if not v:
        return False, "读车况失败"
    cs, vs, cl = v.get("charge_state", {}), v.get("vehicle_state", {}), v.get("climate_state", {})
    state_cn = STATE_CN.get(str(v.get('state')).lower(), v.get('state') or '未知')
    charge_cn = CHARGING_CN.get(cs.get('charging_state'), cs.get('charging_state') or '未知')
    lock_cn = "已锁" if vs.get('locked') else "未锁"
    sentry_cn = "已开启" if vs.get('sentry_mode') else "未开启"
    bats = cs.get('battery_level')
    rng = cs.get('battery_range')
    in_t = cl.get('inside_temp')
    out_t = cl.get('outside_temp')
    last_set = cl.get('driver_temp_setting')
    rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
    in_s = f"{round(in_t, 1)}°C" if in_t is not None else "?"
    out_s = f"{round(out_t, 1)}°C" if out_t is not None else "?"
    set_s = f"{last_set}°C" if last_set is not None else "?"
    lines = [
        f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_cn}",
        f"🌡️ 车内 {in_s} / 车外 {out_s} | 上次设定 {set_s}",
    ]
    return True, "\n".join(lines)


# ============ 规则快匹配（高频指令秒级响应） ============
RULE_MAP = [
    # 解锁必须先于锁车判断（“解锁车辆”含“锁车”子串，2026-08-31 修复）
    (("解锁车", "解锁车辆", "开门锁", "把车解锁", "车门解锁", "车子解锁"), "door_unlock", {}),
    (("锁车", "锁门", "上锁", "把车锁上", "关上车门", "锁好车"), "door_lock", {}),
    (("查电量", "多少电", "看电量", "电量多少", "还有多少电", "电量", "剩余电量"), "status", {}),
    # [rule-expand 2026-09-14] 高频口语说法纳入本地直通（原先落 LLM，实测 2~13s）
    (("车况", "车锁了没", "车锁没", "锁没锁", "锁了没", "看下车", "查车", "状态如何", "车怎么样",
      "帮我看看车", "看看车", "看下车况", "查下车", "车辆状态", "车现在",
      "续航", "能跑多远", "跑多远", "还剩多少电", "剩下多少电", "电量还有", "电池还有"),
     "status", {}),
    (("鸣笛", "按喇叭", "响喇叭", "喇叭", "horn", "honk"), "honk_horn", {}),
    (("闪灯", "闪两下", "双闪", "flash", "闪一下"), "flash_lights", {}),
    (("开空调", "打开空调", "空调开", "开制冷", "开冷气", "启动空调"), "auto_conditioning_start", {}),
    (("关空调", "空调关", "关闭空调", "关制冷", "停空调", "关冷气"), "auto_conditioning_stop", {}),
    (("开前备箱", "前备箱", "开前箱", "打开前备箱"), "actuate_trunk", {"which_trunk": "front"}),
    (("开后尾箱", "后备箱", "开后备箱", "尾箱", "后尾箱"), "actuate_trunk", {"which_trunk": "rear"}),
    # 关哨兵必须先于开：开哨兵 rule 首词是裸“哨兵”，会吞掉“关闭哨兵/关掉哨兵”导致关永远失效
    (("关哨兵", "关闭哨兵", "关掉哨兵"), "set_sentry_mode", {"on": False}),
    (("哨兵", "打开哨兵", "开哨兵", "sentry", "守卫模式"), "set_sentry_mode", {"on": True}),
    (("开始充电", "开始充", "开始冲", "充电开始"), "charge_start", {}),
    (("停止充电", "停止充", "停充电", "停止冲"), "charge_stop", {}),
    (("开充电口", "打开充电口", "充电口开"), "charge_port_door_open", {}),
    (("关充电口", "关闭充电口", "充电口关"), "charge_port_door_close", {}),
    (("唤醒", "叫醒车", "wake"), "wake_up", {}),
    # 关方向盘必须先于开（“关方向盘加热”含“方向盘加热”子串），否则关永远命中开
    (("关方向盘加热", "方向盘加热关"), "remote_steering_wheel_heater_request", {"on": False}),
    (("方向盘加热", "开方向盘加热"), "remote_steering_wheel_heater_request", {"on": True}),
    (("主驾座椅加热", "座椅加热开", "开座椅加热", "座椅加热高档"), "remote_seat_heater_request", {"seat_position": 0, "level": 3}),
    (("座椅加热中档", "座椅加热二档", "座椅加热2档"), "remote_seat_heater_request", {"seat_position": 0, "level": 2}),
    (("座椅加热低档", "座椅加热一档", "座椅加热1档"), "remote_seat_heater_request", {"seat_position": 0, "level": 1}),
    (("关座椅加热", "座椅加热关"), "remote_seat_heater_request", {"seat_position": 0, "level": 0}),
    # 座椅通风：关通风必须先于开
    (("关座椅通风", "座椅通风关", "关闭座椅通风", "关通风"), "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 0}),
    (("主驾座椅通风", "座椅通风开", "开座椅通风", "座椅通风高档", "通风高档", "座椅通风3档", "通风3档"), "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 3}),
    (("座椅通风中档", "通风中档", "座椅通风二档", "座椅通风2档", "通风2档"), "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 2}),
    (("座椅通风低档", "通风低档", "座椅通风一档", "座椅通风1档", "通风1档"), "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 1}),
    (("座椅通风", "开通风"), "remote_seat_cooler_request", {"seat_position": 0, "seat_cooler_level": 1}),
    (("音量加", "声音大点", "音量大"), "media_volume_up", {}),
    (("音量减", "声音小点", "音量小"), "media_volume_down", {}),
    (("暂停", "播放", "下一曲", "切歌", "上一曲"), "media_toggle_playback", {}),
]


def rule_match(text):
    """规则快匹配：命中返回 intent dict，未命中返回 None（走 LLM）。"""
    t = text.strip()
    for kws, action, params in RULE_MAP:
        for kw in kws:
            if kw in t:
                return {"action": action, "params": params}
    # L0.5：本地语义路由（嵌入式模型，绕过云端大模型）
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8791/route",
            data=json.dumps({"text": t}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        if r.get("route") == "action":
            action = r.get("action")
            params = r.get("params") or {}
            # 有的 action 在 run.py 里映射为别的（如 rear/front trunk），交给执行层处理
            return {"action": action, "params": params, "_semantic": True}
    except Exception:
        pass  # 语义路由不可用时静默降级到 LLM
    return None


def main():
    # 快速探测车在线状态（不唤醒、5s 超时），插件用 — 用于判断是否要先发"正在唤醒"提示
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-state":
        s = probe_state()
        print(s if s else "unknown")
        return
    if len(sys.argv) < 2:
        print("用法: python run.py \"你的指令\"")
        return
    user_text = " ".join(sys.argv[1:])

    # 1. 意图识别：规则快匹配优先（0 LLM 延迟），模糊才调 LLM
    # 复合指令连接词检查：如果含“顺便/然后/再/并且/且/还有”或者内嵌逗号隔开多个动作，
    # 优先跳过单动作规则快匹配走 LLM 解析。
    # 注意：语音转写常带尾逗号（“解锁车辆，”），尾标点只是杂质，不算复合；先去尾标点再判。
    _st = user_text.strip().rstrip("，,。.、!！?？ ").strip()
    is_compound = (any(w in _st for w in ["顺便", "然后", "再", "并且", "而且", "还有", "同时", "加上", "以及"]) or
                   "，" in _st or "、" in _st)
    # [perf-fix 2026-09-14] 含'还有/再'的车况句误判复合 -> 整句规则直通
    # 「车还有多少电」因含'还有'被判复合，跳过规则直奔 LLM（实测 2~13s）。
    # 修正：拆开连接词两侧，两侧都能各自命中动作才算真·多动作复合；
    # 否则（如「车」+「多少电」）整句规则命中即采信，不再白等云端 LLM。
    _whole_rule = rule_match(user_text)
    _real_multi = False
    if is_compound:
        if not _whole_rule:
            _real_multi = True
        else:
            import re as _re2
            _parts = [p for p in _re2.split(
                r"[，,、;；\s]+|顺便|然后|接着|并且|而且|还有|同时|加上|以及|再", _st) if p.strip()]
            _hit_cnt = 0
            for _p in _parts:
                try:
                    if rule_match(_p.strip()):
                        _hit_cnt += 1
                except Exception:
                    pass
            _real_multi = _hit_cnt >= 2
    intent = None if (is_compound and _real_multi) else _whole_rule
    if intent is None:
        env = os.environ.copy()
        env["VECTIDE_API_KEY"] = os.environ.get("VECTIDE_API_KEY", "")
        try:
            out = subprocess.run(
                [sys.executable, INTENT, user_text],
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=90,
            ).stdout.strip()
            intent = json.loads(out)
        except Exception as e:
            print(f"意图识别失败: {e}")
            return

    if "error" in intent:
        print(intent["error"])
        return

    # 兼容格式：如果输出是 actions 列表
    actions_list = intent.get("actions")
    if not actions_list and "action" in intent:
        actions_list = [{"action": intent["action"], "params": intent.get("params", {})}]
    if not actions_list:
        print("未识别到可执行的车辆指令")
        return

    # 如果只有单个动作，走原有的快速单动作管道（100% 保持旧逻辑行为）
    if len(actions_list) == 1:
        act_item = actions_list[0]
        action = act_item.get("action", "")
        params = act_item.get("params", {})

        if action == "status":
            ok, text = read_status()
            print(text)
            print_weather()
            if ok:
                learn_phrase(user_text, "status", {})
            return

        # 智能温控：用户说"开空调"（未指定温度）→ 走温差分档 + 过冲回调
        if action == "auto_conditioning_start":
            remain = check_idempotent("auto_conditioning_start")
            if remain is not None:
                print(f"✅ 开启空调 | 已在{remain}秒内启动过，已跳过重复启动")
                print_weather()
                return
            ok, mode, temp, inside, last_set, mono = smart_climate(monitor=True)
            if ok:
                mark_idempotent("auto_conditioning_start")
                learn_phrase(user_text, "auto_conditioning_start", {})
                head = f"✅ 开启空调 | 设定{temp}°C（温差{round(inside - last_set, 1)}°C，{mode}）"
                if mono:
                    head += " | 智能监测"
                print(head)
            else:
                print(f"❌ 开启空调失败 | {mono or ''}")
            v = read_vehicle_fast()
            if v:
                cs, vs, cl = (v.get("charge_state") or {}), (v.get("vehicle_state") or {}), (v.get("climate_state") or {})
                bats = cs.get("battery_level")
                rng = cs.get("battery_range")
                in_t = cl.get("inside_temp")
                rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
                in_s = f"{round(in_t, 1)}C" if in_t is not None else "?"
                lock_s = "已锁" if vs.get("locked") else "未锁"
                print(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s.replace('C', '°C')}")
            w = get_weather()
            if w:
                t = w.get("temp") or "?"
                f_ = w.get("feels") or t
                print(f"⛅ {w.get('city')} {t}°C 体感{f_}°C {w.get('weather')} 湿度{w.get('humidity')}%")
                adv = weather_advice(w)
                if adv:
                    print(f"💡 {adv}")
            return

        if action not in ACTIONS:
            print(f"未知动作: {action}")
            return

        # 2. 幂等保护：敏感动作 30 秒内不重复执行
        if action in IDEMPOTENT_ACTIONS:
            remain = check_idempotent(action)
            if remain is not None:
                print(format_report(False, action, f"{remain}秒内已执行过，已跳过重复动作"))
                return

        # 3. 并行：命令执行 与 车况/天气 同时跑
        import threading
        _box = {}

        def _run_cmd():
            _box["ok"], _box["text"] = proxy_command(ACTIONS[action], params)

        t = threading.Thread(target=_run_cmd)
        t.start()
        v = read_vehicle_fast()   # 主线程并行读车况
        w = get_weather()          # 主线程并行读天气
        t.join()
        ok, text = _box["ok"], _box["text"]
        if ok and action in IDEMPOTENT_ACTIONS:
            mark_idempotent(action)
        if ok:
            learn_phrase(user_text, action, params)
        print(format_report(ok, action, text if not ok else "", v, w))
        return

    # ===== 多任务复合指令执行管道 =====
    import threading
    results = []
    # 依赖排序：空调启动(auto_conditioning_start)优先先跑，之后是设温/通风/加热
    # 查车况(status)最后跑或作为底盘数据聚合
    has_status_action = any(item.get("action") == "status" for item in actions_list)
    exec_actions = [item for item in actions_list if item.get("action") != "status"]

    # 1. 如果包含开空调，先执行空调启动
    has_ac_start = any(item.get("action") == "auto_conditioning_start" for item in exec_actions)
    if has_ac_start:
        remain = check_idempotent("auto_conditioning_start")
        if remain is not None:
            results.append(("auto_conditioning_start", True, f"已在{remain}秒内启动过"))
        else:
            ok, msg = proxy_command(ACTIONS["auto_conditioning_start"], {})
            if ok:
                mark_idempotent("auto_conditioning_start")
                results.append(("auto_conditioning_start", True, "已开启"))
            else:
                results.append(("auto_conditioning_start", False, msg or "开启失败"))

    # 2. 其余动作并行/快速执行
    def _exec_single(item):
        act = item.get("action")
        par = item.get("params", {})
        if act == "auto_conditioning_start":
            return  # 上面已经前置跑过了
        if act not in ACTIONS:
            results.append((act, False, f"未知动作 {act}"))
            return
        if act in IDEMPOTENT_ACTIONS:
            rem = check_idempotent(act)
            if rem is not None:
                results.append((act, True, f"{rem}秒内已执行过，跳过"))
                return
        ok, text = proxy_command(ACTIONS[act], par)
        if ok and act in IDEMPOTENT_ACTIONS:
            mark_idempotent(act)
        desc = "已完成" if ok else (text or "执行失败")
        # 详细参数描述
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
        t = threading.Thread(target=_exec_single, args=(item,))
        t.start()
        threads.append(t)

    # 主线程同时拉取车况与天气
    v = read_vehicle_fast()
    w = get_weather()

    for t in threads:
        t.join()

    # 3. 聚合输出汇报
    succ_cnt = sum(1 for _, ok, _ in results)
    total_cnt = len(results)
    if total_cnt > 0:
        head_mark = "✅" if succ_cnt == total_cnt else ("⚠️" if succ_cnt > 0 else "❌")
        print(f"{head_mark} 复合指令执行完成（{succ_cnt}/{total_cnt}）")
        for act, ok, desc in results:
            mark = "·" if ok else "×"
            print(f" {mark} {action_cn(act)}: {desc}")
    elif has_status_action:
        print("✅ 车况已更新")

    # 统一车况和天气行
    if v:
        cs, vs, cl = (v.get("charge_state") or {}), (v.get("vehicle_state") or {}), (v.get("climate_state") or {})
        bats = cs.get("battery_level")
        rng = cs.get("battery_range")
        in_t = cl.get("inside_temp")
        rng_s = f"{round(rng * 1.609)}km" if rng is not None else "?"
        in_s = f"{round(in_t, 1)}C" if in_t is not None else "?"
        lock_s = "已锁" if vs.get("locked") else "未锁"
        print(f"🚗 电量 {bats}% | 续航 {rng_s} | 锁定 {lock_s} | 车内 {in_s.replace('C', '°C')}")
    w = w or get_weather()
    if w:
        t = w.get("temp") or "?"
        f_ = w.get("feels") or t
        print(f"⛅ {w.get('city')} {t}°C 体感{f_}°C {w.get('weather')} 湿度{w.get('humidity')}%")
        adv = weather_advice(w)
        if adv:
            print(f"💡 {adv}")
    return


if __name__ == "__main__":
    main()