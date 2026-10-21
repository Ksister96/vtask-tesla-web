# -*- coding: utf-8 -*-
"""朋友版定时用车：时间理解、任务持久化和服务端调度。"""
import json, os, re, threading, time, uuid, subprocess, sys, traceback
import urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("MULTI_BASE", "/opt/tesla-fleet-multi")
USERS_DIR = os.path.join(BASE, "users")
AMAP_KEY_FILE = os.path.join(BASE, "amap_key.txt")
TZ = timezone(timedelta(hours=8))
LOCK = threading.RLock()
_PUSH_SENDER_MOD = None
TRAVEL_WORDS = re.compile(r"用车|出门|出发|出行|开车|启程|去往|前往|去[\u4e00-\u9fa5A-Za-z0-9]{1,12}|到[\u4e00-\u9fa5A-Za-z0-9]{1,12}")
WEEKDAY = {"一":0,"二":1,"三":2,"四":3,"五":4,"六":5,"日":6,"天":6}
ROUTE_TIMEOUT = 12


def _sched_log(msg):
    try:
        with open(os.path.join(BASE, "multi-sched.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(TZ).isoformat()} {msg}\n")
    except Exception:
        pass


def load_amap_key():
    try:
        with open(AMAP_KEY_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def _geocode_with_city(destination, key, city):
    """三级精准搜索：
    1. inputtips(关键词输入提示，带纠错)：如「东方民族」能纠错出「东方明珠广播电视塔」
    2. place/text(POI 文本搜索)：智能模糊匹配最相关的地标 POI
    3. geocode/geo(基础地址编码)：保底查标准门牌
    返回: (location, display_name) 或 ("", "")
    """
    dest = destination.strip()
    if not dest:
        return "", ""

    # 1. inputtips 关键词纠错（速度快且支持错别字联想）
    try:
        qt = urllib.parse.urlencode({"keywords": dest, "key": key, "city": city, "output": "JSON"})
        with urllib.request.urlopen("https://restapi.amap.com/v3/assistant/inputtips?" + qt, timeout=ROUTE_TIMEOUT) as r:
            tips = json.loads(r.read().decode("utf-8"))
        for t in tips.get("tips") or []:
            loc = (t.get("location") or "").strip()
            name = (t.get("name") or "").strip()
            if "," in loc and loc != "[]":
                return loc, name
    except Exception:
        pass

    # 2. place/text 智能 POI 搜索（全国/全城热点地标容错最高）
    try:
        qp = urllib.parse.urlencode({"keywords": dest, "key": key, "city": city, "output": "JSON", "offset": 5, "page": 1})
        with urllib.request.urlopen("https://restapi.amap.com/v3/place/text?" + qp, timeout=ROUTE_TIMEOUT) as r:
            pois = json.loads(r.read().decode("utf-8"))
        for p in pois.get("pois") or []:
            loc = (p.get("location") or "").strip()
            name = (p.get("name") or "").strip()
            if "," in loc:
                return loc, name
    except Exception:
        pass

    # 3. 基础地理编码保底
    try:
        q = urllib.parse.urlencode({"address": dest, "key": key, "output": "JSON", "city": city})
        with urllib.request.urlopen("https://restapi.amap.com/v3/geocode/geo?" + q, timeout=ROUTE_TIMEOUT) as r:
            geo = json.loads(r.read().decode("utf-8"))
        if str(geo.get("status")) == "1" and geo.get("geocodes"):
            loc = geo["geocodes"][0].get("location", "")
            formatted = geo["geocodes"][0].get("formatted_address", dest)
            if "," in loc:
                return loc, formatted
    except Exception:
        pass

    return "", ""


def route_info(origin_lon, origin_lat, destination, dest_location=None, city="上海"):

    """高德驾车路线（距离/用时）；dest_location 已知坐标时跳过地理编码。失败返回 None。"""
    key = load_amap_key()
    if not key or not destination:
        return None
    try:
        loc = dest_location
        resolved_name = destination
        if not loc:
            loc, resolved_name = _geocode_with_city(destination, key, city)
            if not loc:
                return None
        if "," not in loc:
            return None
        q = urllib.parse.urlencode({"origin": f"{float(origin_lon):.6f},{float(origin_lat):.6f}", "destination": loc, "key": key, "extensions": "base", "output": "JSON"})
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
        _sched_log(f"route_info 失败: {destination}: {e}")
        return None


def car_origin_wgs84(uid):
    """车辆当前位置（VIN 档案，WGS84）；无则 None。
    出发前 ETA 复查用：车在订计划后可能挪过窝，起点必须以车辆实时上报为准
    （2026-10-07 用户定稿：车辆定位替代手机定位）。本地直读档案，避免反向 import 主模块。"""
    try:
        with open(os.path.join(USERS_DIR, uid, "account.json"), encoding="utf-8-sig") as f:
            acct = json.load(f)
        vin = ((acct.get("vehicles") or [{}])[0].get("vin") or "").strip()
        if not vin:
            return None
        with open(os.path.join(USERS_DIR, "vehicles", vin + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
        lat, lon = meta.get("latitude"), meta.get("longitude")
        if lat is None or lon is None:
            return None
        return {"longitude": float(lon), "latitude": float(lat)}
    except Exception:
        return None


def update_route_eta(uid, task):
    """出发前重查路线（预热时调用）。起点=车辆当前位置（优先）→任务内旧起点兜底。
    返回 True 表示用时变化>=5分钟（调用方应发通知）。"""
    try:
        origin = car_origin_wgs84(uid) or task.get("origin")
        if not origin or not task.get("dest_location"):
            return False
        task["origin"] = {"longitude": origin["longitude"], "latitude": origin["latitude"]}
        r = route_info(float(origin["longitude"]), float(origin["latitude"]), task.get("dest_name") or task.get("dest", ""), dest_location=task["dest_location"])
        if not r:
            return False
        old_dur = task.get("duration_min", 0)
        task["distance_km"] = r["distance_km"]
        task["duration_min"] = r["duration_min"]
        task["dest_location"] = r["dest_location"]
        # 出发时间=用户说的时间；路程用时只作为「建议最晚出发」参考展示，
        # 绝不回写 preheat_at/remind_at（否则路程越长预热被推到过去）。
        leave_by = datetime.fromisoformat(task["departure_at"]) - timedelta(minutes=r["duration_min"] + 0)
        task["suggest_leave_by"] = leave_by.isoformat()
        return abs(r["duration_min"] - old_dur) >= 5
    except Exception:
        return False

def _path(uid, name): return os.path.join(USERS_DIR, uid, name)
def _load(uid, name, default):
    try:
        with open(_path(uid,name), "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default
def _save(uid, name, data):
    os.makedirs(_path(uid, ""), exist_ok=True)
    tmp = _path(uid, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    # Windows 下并发读写时 os.replace 可能撞 PermissionError，重试几次；最终失败必须留日志
    for i in range(4):
        try:
            os.replace(tmp, _path(uid, name))
            return
        except PermissionError:
            time.sleep(0.15 * (i + 1))
        except Exception as e:
            _sched_log(f"save error {uid}/{name}: {e}")
            return
    try:
        os.replace(tmp, _path(uid, name))
    except Exception as e:
        _sched_log(f"save error (retries exhausted) {uid}/{name}: {e}")


def _sched_update(uid, task_id, mutate):
    """线程安全地更新单个任务：每次都重新加载最新文件再改再存。
    预热线程执行 40~150s，若持有旧快照最后落盘，会把提醒线程已写入的状态覆盖回去
    （实测导致任务永远卡在 running）。"""
    with LOCK:
        items = _load(uid, "schedules.json", [])
        task = next((t for t in items if t.get("id") == task_id), None)
        if task is None:
            return None
        mutate(task)
        _save(uid, "schedules.json", items)
        return task

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
              "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
              "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}


def _cn_num(s):
    """中文数字字符串 -> int。支持 0-99 常用口语写法（十、十二、二十、二十三、廿三）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    # 廿 / 卅
    if s.startswith("廿"):
        rest = s[1:]
        return 20 + (_cn_num(rest) or 0)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _cn_num(a) if a else 1
        ones = _cn_num(b) if b else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    total, ok = 0, False
    for ch in s:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
        ok = True
    return total if ok else None


_CN_NUM_PAT = r"[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十廿]+"


def parse_clock(s):
    """解析时刻，返回 (h, mm) 或 None。
    支持：7点15分 / 7点半 / 一点一刻 / 两点三刻 / 晚上八点两刻 / 十二点 / 明早七点
    """
    s = (s or "").replace("：", ":")
    # 把"点"前的中文数字换成阿拉伯数字，便于统一处理（不影响已是数字的情况）
    s = re.sub(r"(" + _CN_NUM_PAT + r")\s*(?=点|:|：)", lambda m: str(_cn_num(m.group(1)) or m.group(1)), s)

    m = re.search(r"(\d{1,2})\s*点(半)", s)
    if m:
        return (int(m.group(1)), 30)
    # N刻：一刻=15 / 两刻=30 / 三刻=45；也支持"1刻"
    m = re.search(r"(\d{1,2})\s*点\s*(" + _CN_NUM_PAT + r"|\d{1,2})\s*刻", s)
    if m:
        k = _cn_num(m.group(2))
        if k is None or not (0 <= k <= 4):
            return None
        mm = (k * 15) % 60
        h = int(m.group(1)) + (int((k * 15) / 60))
        return (h, mm) if h <= 23 and mm <= 59 else None
    m = re.search(r"(\d{1,2})\s*点半", s)
    if m:
        return (int(m.group(1)), 30)
    # 通用：N点[MM分] / N:MM / N点整
    # 分钟同样允许中文数字：ASR 关掉 ITN 后输出「九点五十分」，
    # 旧实现只认阿拉伯数字，会把 9:50 误判成 9:00 整（2026-09-15 修）。
    m = re.search(r"(\d{1,2})\s*(?:[:.点时]\s*(" + _CN_NUM_PAT + r"|\d{1,2})?)", s)
    if not m:
        return None
    h = int(m.group(1))
    _mm_txt = (m.group(2) or "").strip()
    mm = _cn_num(_mm_txt) if _mm_txt else 0
    if mm is None:
        return None
    return (h, mm) if h <= 23 and mm <= 59 else None

def apply_tod(s,h,mm):
    if re.search(r"下午|午后",s) and h<12:h+=12
    elif re.search(r"晚上|今晚|今夜|夜晚|傍晚|夜里|夜间",s) and h<12:h+=12
    elif not re.search(r"凌晨|早上|早晨|清晨|早|上午|黎明",s) and h<12:
        now=datetime.now(TZ)
        if now.hour>h or (now.hour==h and now.minute>mm): h+=12
    return h,mm

def parse_time(text):
    now=datetime.now(TZ); t=text.strip(); clock=parse_clock(t)
    if not clock:return None
    h,mm=apply_tod(t,*clock)
    md=re.search(r"(?:^|[^\d])(\d{1,2})\s*[/月.\-]\s*(\d{1,2})\s*日?",t)
    if md:
        try: base=datetime(now.year,int(md.group(1)),int(md.group(2)),tzinfo=TZ)
        except ValueError:return None
        if base.date()<now.date(): base=base.replace(year=now.year+1)
    else:
        wd=re.search(r"(?:周|星期|礼拜)([一二三四五六日天])",t)
        if wd: days=(WEEKDAY[wd.group(1)]-now.weekday())%7 or 7
        elif re.search(r"大后天",t): days=3
        elif re.search(r"后天",t): days=2
        elif re.search(r"明天|明早|明晚|明日",t): days=1
        else: days=0
        base=datetime(now.year,now.month,now.day,tzinfo=TZ)+timedelta(days=days)
    dt=base.replace(hour=h,minute=mm,second=0,microsecond=0)
    if dt<=now and not md and not re.search(r"周|星期|礼拜|明|后天",t): dt+=timedelta(days=1)
    return dt

def extract_dest(text):
    """从「去/到/前往 XX」提取目的地；车控/无意义词返回 None。"""
    m = re.search(r"(?:去|到|前往|回)([^\s，。,.!?！？]{1,16})", text)
    if not m: return None
    d = m.group(1).strip().strip("的了")
    d = re.sub(r"(?:用车|出门|出发|出行|开车|启程)$", "", d).strip()
    if not d or len(d) < 2: return None
    if d in ("锁车","解锁","开门","开空调","关空调","充电","闪灯","鸣笛","唤醒","开哨兵","关哨兵","查电量","查车况","检查","上班","睡觉","吃饭","厕所"): return None
    if re.fullmatch(r"(?:吧|吗|啊|呢|呀)", d): return None
    return d

def book(text):
    if not TRAVEL_WORDS.search(text): return None, "未识别到明确的用车意图"
    dt=parse_time(text)
    if not dt:return None,"没解析到明确时间，例如：明早7点用车"
    if dt <= datetime.now(TZ)+timedelta(minutes=1):return None,"时间太近，至少提前1分钟"
    if dt > datetime.now(TZ)+timedelta(hours=24*7):return None,"时间过远（超过 7 天），请重新说一次时间"
    # 预热/提醒只跟出发时间绑固定提前量，不掺入路程时间（用户约定：说的时间=出发时间）
    task={"id":"sch_"+uuid.uuid4().hex[:12],"text":text,"dest":extract_dest(text),"departure_at":dt.isoformat(),"preheat_at":(dt-timedelta(minutes=15)).isoformat(),"remind_at":(dt-timedelta(minutes=3)).isoformat(),"preheat_status":"pending","remind_status":"pending","status":"pending","created_at":datetime.now(TZ).isoformat()}
    return task,None

def confirm(uid, task):
    with LOCK:
        items=_load(uid,"schedules.json",[]); items.append(task); _save(uid,"schedules.json",items)
    return task

def cancel_latest(uid, text=""):
    """取消定时任务：优先 pending，无 pending 时回退到最近 24h 内创建的任务；都没有则 None。"""
    with LOCK:
        items=_load(uid,"schedules.json",[])
        pending=[x for x in items if x.get("status")=="pending"]
        target=None
        if pending:
            target=pending[-1]
        else:
            recent=[]; now=datetime.now(TZ)
            for x in items:
                ca=x.get("created_at")
                if not ca: continue
                try:
                    if (now-datetime.fromisoformat(ca)) < timedelta(hours=24): recent.append(x)
                except Exception: pass
            if recent: target=recent[-1]
        if not target: return None
        target["status"]="cancelled"
        target["preheat_status"]="cancelled"
        target["remind_status"]="cancelled"
        _save(uid,"schedules.json",items)
        return target

def list_pending(uid):
    """返回该用户所有待执行（pending）的定时任务。"""
    with LOCK:
        items=_load(uid,"schedules.json",[])
        return [x for x in items if x.get("status")=="pending"]


def _load_push_sender():
    """按文件路径显式加载 push_sender，不依赖 sys.path / 当前工作目录。"""
    import importlib.util
    global _PUSH_SENDER_MOD
    if _PUSH_SENDER_MOD is not None:
        return _PUSH_SENDER_MOD
    try:
        mod_path = os.path.join(BASE, "push_sender.py")
        spec = importlib.util.spec_from_file_location("push_sender", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PUSH_SENDER_MOD = mod
        return mod
    except Exception as e:
        _sched_log(f"push_sender load error: {e}")
        return None


def notify(uid, title, body, kind, dedup_key=None):
    """写通知 + 发 Web Push。dedup_key 用于幂等去重（同 key 10 分钟内只发一次）。"""
    now = datetime.now(TZ)
    with LOCK:
        # 幂等去重：防止 tick 重入/重试导致同一条通知重复推送（用户明确要求"发一次就行"）
        if dedup_key:
            seen = _load(uid, "push_dedup.json", {})
            last = seen.get(dedup_key)
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < 600:
                        _sched_log(f"dedup skip: uid={uid} key={dedup_key}")
                        return
                except Exception:
                    pass
            seen[dedup_key] = now.isoformat()
            # 只保留最近 200 条，避免文件无限增长
            if len(seen) > 200:
                for k in sorted(seen, key=lambda x: seen[x])[:-200]:
                    seen.pop(k, None)
            _save(uid, "push_dedup.json", seen)
        ns=_load(uid,"notifications.json",[])
        item = {"id":"ntf_"+uuid.uuid4().hex[:12],"title":title,"body":body,"kind":kind,"ts":now.isoformat(),"read":False}
        ns.append(item)
        _save(uid,"notifications.json",ns[-100:])
    # 触发 Web Push 推送手机系统横幅（显式路径加载，杜绝 ImportError）
    try:
        push_sender = _load_push_sender()
        if push_sender is None:
            _sched_log(f"push skip (module unavailable) uid={uid} kind={kind}")
            return
        u_dir = _path(uid, "")
        push_sender.push_to_user(u_dir, {"title": title, "body": body, "kind": kind, "id": item["id"]})
    except Exception as e:
        _sched_log(f"push notify error uid={uid}: {e}")

def _run(uid, text):
    try:
        env = os.environ.copy()
        env["MULTI_BASE"] = BASE
        r=subprocess.run([sys.executable,"-X","utf8",os.path.join(BASE,"user_runner.py"),uid,text],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=150,env=env)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:return "执行异常："+str(e)

def refresh_state_cache(uid):
    """预热成功后刷新 last_state.json 缓存，让前端按钮同步真实车况。
    用 __json__ 拿新鲜结构化车况写入缓存；失败则静默（不阻断定时主流程）。"""
    try:
        out = _run(uid, "__json__")
        if not out:
            return False
        js = json.loads(out)
        if not js.get("fresh") or not js.get("fields"):
            return False
        json_file = os.path.join(USERS_DIR, uid, "last_state.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({"fields": js["fields"], "ts": time.time()}, f, ensure_ascii=False)
        return True
    except Exception:
        return False

def _execute_due_safe(uid, task_id, kind):
    """线程体兜底：任何异常必须落到调度日志，绝不允许线程静默死亡。"""
    try:
        _execute_due(uid, task_id, kind)
    except Exception:
        _sched_log("execute_due error uid=%s kind=%s:\n%s"
                   % (uid, kind, traceback.format_exc()))


def _execute_due(uid, task_id, kind):
    """独立线程执行到点任务。预热子进程最长 150s，绝不能内联在 5s 一轮的 tick 里，
    否则一个用户的预热会阻塞所有用户的提醒/预热。
    所有落盘都经 _sched_update 重新加载最新文件——线程不持有长命快照。"""
    _sched_log(f"execute_due start uid={uid} kind={kind}")
    if kind == "preheat":
        out = _run(uid, "开空调")
        ok = bool(out and not out.startswith(("意图识别失败", "❌", "执行异常")))

        def _mut(t):
            t["preheat_status"] = "done" if ok else "failed"
            t["preheat_result"] = (out or "")[:1000]
        _sched_update(uid, task_id, _mut)

        route_line_box = [""]

        def _route(t):
            try:
                if update_route_eta(uid, t):
                    route_line_box[0] = f"\n🔄 路线更新：预计 {t['duration_min']} 分钟，建议最晚 {datetime.fromisoformat(t['suggest_leave_by']).strftime('%H:%M')} 出发"
            except Exception:
                pass
        if ok:
            _sched_update(uid, task_id, _route)
            try:
                refresh_state_cache(uid)
            except Exception:
                pass
        route_line = route_line_box[0]

        with LOCK:
            ns = _load(uid, "notifications.json", [])
            ns.append({"id": "ntf_" + uuid.uuid4().hex[:12],
                       "title": "⏰ 预热已执行" if ok else "❌ 预热失败",
                       "body": (out or "没有返回结果") + route_line,
                       "kind": kind, "ts": datetime.now(TZ).isoformat(),
                       "read": False, "silent": True})
            _save(uid, "notifications.json", ns[-100:])
        _sched_log(f"preheat done (thread, silent, no push) uid={uid} ok={ok}")
    else:
        def _mut(t):
            t["remind_status"] = "done"
        _sched_update(uid, task_id, _mut)
        notify(uid, "🚗 出发提醒", f"3分钟后出发（{_sched_text(uid, task_id)}）", kind,
               dedup_key=f"{task_id}:remind")
    # 两个环节都落定（非 pending/running）→ 整个任务置 done
    def _fin(t):
        if t.get("preheat_status") not in ("pending", "running") \
                and t.get("remind_status") not in ("pending", "running"):
            t["status"] = "done"
    _sched_update(uid, task_id, _fin)


def _sched_text(uid, task_id):
    with LOCK:
        t = next((x for x in _load(uid, "schedules.json", []) if x.get("id") == task_id), None)
        return (t or {}).get("text", "")


def tick():
    now=datetime.now(TZ)
    try: uids=[x for x in os.listdir(USERS_DIR) if os.path.isdir(_path(x,""))]
    except Exception:return
    for uid in uids:
        with LOCK: items=_load(uid,"schedules.json",[])
        changed=False
        for task in items:
            if task.get("status")!="pending":continue
            for kind,key,label in (("preheat","preheat_at","预热"),("remind","remind_at","提醒")):
                if task.get(kind+"_status")!="pending":continue
                try: due=datetime.fromisoformat(task[key])
                except Exception: continue
                if due < now - timedelta(hours=2):
                    task[kind+"_status"] = "stale"
                    _sched_log(f"stale: task={task.get('id','?')[:16]} {kind} {due.isoformat()} < now")
                    changed = True
                    # 立即落盘，避免下一轮 tick 重复处理
                    with LOCK: _save(uid,"schedules.json",items)
                    continue
                if now < due: continue
                # 【防重入】执行前先把状态置为 running 并立即落盘：
                # 后续轮次的 tick 看到非 pending 就不会重复执行。
                # 实际执行放到独立线程：预热子进程最长 150s，不能阻塞 tick 主循环。
                task[kind+"_status"] = "running"
                with LOCK: _save(uid,"schedules.json",items)
                threading.Thread(target=_execute_due_safe, args=(uid, task.get("id"), kind),
                                 daemon=True, name=f"sched-{kind}-{str(task.get('id'))[:8]}").start()
                changed=True
            # 两个环节都落定（非 pending/running）才整单完结；running 表示执行线程在途
            if task.get("preheat_status") not in ("pending","running") and task.get("remind_status") not in ("pending","running"):
                task["status"]="done"
                with LOCK: _save(uid,"schedules.json",items)
        if changed:
            with LOCK:_save(uid,"schedules.json",items)

def _recover_stuck_running():
    """把卡在 running 超过 10 分钟的状态复位为 pending（进程崩溃/重启留下的残局）。
    复位后任务会被正常重试；notify 的 dedup_key(taskid:kind) 会拦住重复推送。"""
    try:
        uids=[x for x in os.listdir(USERS_DIR) if os.path.isdir(_path(x,""))]
    except Exception: return
    now=datetime.now(TZ)
    for uid in uids:
        with LOCK: items=_load(uid,"schedules.json",[])
        changed=False
        for task in items:
            if task.get("status")!="pending": continue
            for kind in ("preheat","remind"):
                if task.get(kind+"_status")!="running": continue
                try:
                    # 用创建时间兜底判断；running 没有时间戳，用 preheat_at/remind_at 估计
                    base=datetime.fromisoformat(task.get("preheat_at") if kind=="preheat" else task.get("remind_at"))
                except Exception:
                    base=None
                # 出发时间过了 30 分钟还没跑完，视为残局直接结束该环节
                if base and (now-base) > timedelta(minutes=30):
                    task[kind+"_status"]="failed"
                    task[kind+"_result"]="进程重启导致中断"
                    _sched_log(f"recover: stuck running -> failed task={task.get('id','?')[:16]} {kind}")
                    changed=True
                else:
                    # 还没超时，复位为 pending 让 tick 重试（dedup 会拦重复推送）
                    task[kind+"_status"]="pending"
                    _sched_log(f"recover: stuck running -> pending task={task.get('id','?')[:16]} {kind}")
                    changed=True
        if changed:
            with LOCK: _save(uid,"schedules.json",items)


def start_scheduler():
    def loop():
        while True:
            try: tick()
            except Exception: pass
            time.sleep(5)
    # 启动时先做一次残局恢复
    try: _recover_stuck_running()
    except Exception as e: _sched_log(f"recover error: {e}")
    t=threading.Thread(target=loop,name="multi-scheduler",daemon=True); t.start(); return t

def list_notifications(uid):
    with LOCK:
        ns=_load(uid,"notifications.json",[]); out=[x for x in ns if not x.get("read")]
        for x in ns:x["read"]=True
        _save(uid,"notifications.json",ns)
        return out
