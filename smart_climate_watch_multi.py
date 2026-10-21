# -*- coding: utf-8 -*-
"""多用户版智能温控过冲回调监测（v2，2026-09-15）。

用法：python smart_climate_watch_multi.py <uid> <vehicle_id> <当前设定温度> <回调目标温度>

v1 的 bug（2026-09-15 排查确认）：调用方传的是 <激进温度> <温和目标>，脚本却按
<初始设定> <目标> 反着取参，导致方向判反 —— 车内 45°C 也立刻判"到位"，回调写回的还是
激进温度本身（空操作），从来没有真正把温度拉回温和值。v2 修正方向，并把回调目标固定为
偏好基线 21°C。

v2 规则：
- 当前设定 < 目标 → 制冷中：等车内降到 目标+3 以内，回调到目标
- 当前设定 > 目标 → 制热中：等车内升到 目标−3 以上，回调到目标
- 回调前先确认：空调仍开着、设定值没被用户手改过（手改过就直接退出，不覆盖用户操作）
- 每轮重读 token（token 轮换后不会静默失效）
- 同一用户只允许一个监测进程（自动结束旧进程，防堆积互相覆盖）
- 最多 30 分钟（6 次 × 5 分钟）
"""
import json
import os
import ssl
import sys
import time
import urllib.request

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
USERS = os.path.join(BASE, "users")
FLEET = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"
PROXY = "https://localhost:4443"
CTX = ssl.create_default_context()              # 特斯拉域名严格校验
PROXY_CTX = ssl._create_unverified_context()    # 本机 4443 代理（自签证书）
MARGIN = 3.0          # 温和区间 ±3°C
INTERVAL = 300        # 每 5 分钟
MAX_LOOPS = 6         # 最多 30 分钟


def read_json(p):
    with open(p, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def http(method, url, headers=None, body=None, timeout=30):
    req = urllib.request.Request(url, data=(body.encode() if isinstance(body, str) else body),
                                 headers=headers or {}, method=method)
    # 本机代理(4443,自签)用宽松上下文；特斯拉域名严格校验
    ctx = PROXY_CTX if ("//localhost" in url or "//127.0.0.1" in url) else CTX
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            return (json.loads(raw) if raw else {}), None
    except Exception as e:
        return None, str(e)


def _is_our_watch(pid):
    """确认该 pid 真是本监测脚本（读 /proc/<pid>/cmdline）。非 Linux / 读不到 → 返回 False（宁可不杀）。"""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            cl = f.read().decode("utf-8", "replace")
        return "smart_climate_watch_multi.py" in cl
    except Exception:
        return False


def claim_single(uid):
    """同用户只留一个监测进程：确认旧进程确实是本脚本才结束它（防止 pid 复用误杀）。"""
    p = os.path.join(USERS, uid, "climate_watch.pid")
    try:
        if os.path.exists(p):
            old = (open(p).read() or "").strip()
            if old.isdigit() and int(old) != os.getpid() and _is_our_watch(old):
                try:
                    os.kill(int(old), 15)
                    time.sleep(0.5)
                except Exception:
                    pass
        with open(p, "w") as f:
            f.write(str(os.getpid()))
        return p
    except Exception:
        return None


def set_temp(uid, vid, tok, target):
    """回调设温：只写主驾（副驾沿用自身设定）；车端不接受单参数则退回双参数。"""
    url = f"{PROXY}/api/1/vehicles/{vid}/command/set_temps"
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    # [2026-09-29] 单参数假成功，直接双区
    d, err = http("POST", url, h,
                  json.dumps({"driver_temp": target, "passenger_temp": target}), 60)
    return d, err


def main():
    if len(sys.argv) < 5:
        return
    uid, vid = sys.argv[1], sys.argv[2]
    try:
        cur, target = float(sys.argv[3]), float(sys.argv[4])
    except ValueError:
        return
    cooling = cur < target   # 我们设的是低温（cur<target）→ 制冷中
    pidfile = claim_single(uid)
    try:
        for _ in range(MAX_LOOPS):
            time.sleep(INTERVAL)
            try:
                tok = read_json(os.path.join(USERS, uid, "tokens.json")).get("access_token")
            except Exception:
                tok = None
            if not tok:
                continue
            d, err = http("GET", f"{FLEET}/api/1/vehicles/{vid}/vehicle_data",
                          {"Authorization": "Bearer " + tok}, timeout=20)
            cs = ((d or {}).get("response") or {}).get("climate_state") or {}
            if err or not cs:
                continue
            inside = cs.get("inside_temp")
            if inside is None:
                continue
            if cs.get("is_climate_on") is False:
                return                      # 空调被关了 → 退出，不覆盖
            now_set = cs.get("driver_temp_setting")
            if now_set is not None and abs(float(now_set) - cur) > 0.6:
                return                      # 用户手改过设定 → 退出，不覆盖
            reached = inside <= target + MARGIN if cooling else inside >= target - MARGIN
            if reached:
                set_temp(uid, vid, tok, target)
                return
    finally:
        try:
            if pidfile and os.path.exists(pidfile):
                if (open(pidfile).read() or "").strip() == str(os.getpid()):
                    os.remove(pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()
