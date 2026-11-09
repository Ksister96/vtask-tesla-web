# -*- coding: utf-8 -*-
"""行程一次性订阅：额度 +1/-1、43101 清零、未记账时仍按微信一次性语义发送。"""
import importlib.util
import json
import os
import tempfile

TMP = tempfile.mkdtemp(prefix="wxmsg_test_")
os.environ["MULTI_BASE"] = TMP
os.makedirs(os.path.join(TMP, "users", "t1"), exist_ok=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cwm_wxmsg", os.path.join(ROOT, "car-web-multi.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

assert m.WXMSG_QUOTA_FILE.startswith(TMP), m.WXMSG_QUOTA_FILE
assert m.WXMSG_TRIP_TMPL and "tesla" not in m.WXMSG_TRIP_TMPL.lower()

LOGS = []
m.log_line = lambda kind, msg: LOGS.append((kind, msg))

TMPL = m.WXMSG_TRIP_TMPL
PHRASE = m.WXMSG_FIELD_PHRASE
THING = m.WXMSG_FIELD_THING
PHRASE_MAX = m.WXMSG_PHRASE_MAX
THING_MAX = m.WXMSG_THING_MAX
PHRASE_DEF = m.WXMSG_PHRASE_DEFAULT
THING_DEF = m.WXMSG_THING_DEFAULT


def logs_text():
    return "\n".join(msg for _, msg in LOGS)


# 文案：字段名/长度/默认文案一律读 WXMSG_* 配置；空值/商标有兜底；不传 time
assert PHRASE == "phrase1" and THING == "thing12"
assert PHRASE_MAX == 5 and THING_MAX == 20
assert PHRASE_DEF == "行程结束" and THING_DEF == "已到达"
p = m.wxmsg_trip_payload(PHRASE_DEF, "12.3 km · 25 分钟")
assert p[PHRASE]["value"] == PHRASE_DEF
assert p[THING]["value"] == "12.3 km · 25 分钟"
assert set(p.keys()) == {PHRASE, THING}
assert "特斯拉" not in json.dumps(p, ensure_ascii=False)
p = m.wxmsg_trip_payload("特斯拉行程结束", "Tesla 12km")
assert p[PHRASE]["value"] == PHRASE_DEF
assert p[THING]["value"] == THING_DEF
long_phrase = "已到达啦啦"
p = m.wxmsg_trip_payload(long_phrase, "说明" * 30)
assert p[PHRASE]["value"] == long_phrase[:PHRASE_MAX]
assert len(p[PHRASE]["value"]) == PHRASE_MAX
assert len(p[THING]["value"]) == THING_MAX
p = m.wxmsg_trip_payload("", "")
assert p[PHRASE]["value"] == PHRASE_DEF
assert p[THING]["value"] == THING_DEF
# 运行时改配置应立刻生效（换模板只改一处）
m.WXMSG_FIELD_PHRASE = "thing1"
m.WXMSG_FIELD_THING = "thing2"
m.WXMSG_PHRASE_MAX = 4
m.WXMSG_THING_MAX = 8
m.WXMSG_PHRASE_DEFAULT = "已到达"
m.WXMSG_THING_DEFAULT = "终点附近"
alt = m.wxmsg_trip_payload("", "ABCDEFGHIJK")
assert set(alt.keys()) == {"thing1", "thing2"}
assert alt["thing1"]["value"] == "已到达"
assert alt["thing2"]["value"] == "ABCDEFGH"
m.WXMSG_FIELD_PHRASE = PHRASE
m.WXMSG_FIELD_THING = THING
m.WXMSG_PHRASE_MAX = PHRASE_MAX
m.WXMSG_THING_MAX = THING_MAX
m.WXMSG_PHRASE_DEFAULT = PHRASE_DEF
m.WXMSG_THING_DEFAULT = THING_DEF

# accept +1；同一 trip 不重复加；无 trip_id 的另一次再 +1
a = m.wxmsg_note_accept("u1", TMPL, "tripA", "accept")
assert a["added"] and a["remain"] == 1, a
a = m.wxmsg_note_accept("u1", TMPL, "tripA", "accept")
assert (not a["added"]) and a["remain"] == 1, a
a = m.wxmsg_note_accept("u1", TMPL, "", "accept")
assert a["added"] and a["remain"] == 2, a

# 拒绝不加次数
r = m.wxmsg_note_accept("u1", TMPL, "tripB", "reject")
assert (not r["added"]) and r["remain"] == 2, r
assert "授权 reject" in logs_text()

# 有额度才发；成功 -1；同一行程不再发
prep = m.wxmsg_prepare_send("u1", TMPL, "20261109-181000")
assert prep["action"] == "send" and prep["tracked"] and prep["remain"] == 2, prep
done = m.wxmsg_apply_send_result("u1", TMPL, "20261109-181000", ok=True, errcode=0)
assert done["decremented"] and done["remain"] == 1, done
assert "额度 -1" in logs_text()
again = m.wxmsg_prepare_send("u1", TMPL, "20261109-181000")
assert again["action"] == "skip" and again["reason"] == "already_sent", again
# 重放成功结果不重复扣
done2 = m.wxmsg_apply_send_result("u1", TMPL, "20261109-181000", ok=True, errcode=0)
assert (not done2["decremented"]) and done2["remain"] == 1, done2

# 其他错误不扣
kept = m.wxmsg_apply_send_result("u1", TMPL, "20261109-190000", ok=False, errcode=47003)
assert kept["remain"] == 1 and not kept["cleared"], kept

# 43101 清零，之后跳过
cleared = m.wxmsg_apply_send_result("u1", TMPL, "20261109-190000", ok=False, errcode=43101)
assert cleared["cleared"] and cleared["remain"] == 0, cleared
assert "errcode=43101 本地一次性额度清零" in logs_text()
skip = m.wxmsg_prepare_send("u1", TMPL, "20261109-200000")
assert skip["action"] == "skip" and skip["reason"] == "no_quota", skip

# 再接受一次，恢复可发
a = m.wxmsg_note_accept("u1", TMPL, "tripC", "accept")
assert a["remain"] == 1, a
assert m.wxmsg_prepare_send("u1", TMPL, "20261109-210000")["action"] == "send"

# 从未上报 accept：照样发，成功不写成 remain=0
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181100")["action"] == "send"
ghost = m.wxmsg_apply_send_result("u2", TMPL, "20261109-181100", ok=True, errcode=0)
assert ghost["tracked"] is False and ghost["remain"] is None and not ghost["decremented"], ghost
assert "按微信一次性语义" in logs_text()
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181100")["reason"] == "already_sent"
assert m.wxmsg_prepare_send("u2", TMPL, "20261109-181200")["action"] == "send"
# 未记账时 43101 不制造 remain=0 把门关上
z = m.wxmsg_apply_send_result("u9", TMPL, "t-9", ok=False, errcode=43101)
assert z["tracked"] is False and z["remain"] is None, z
assert m.wxmsg_prepare_send("u9", TMPL, "t-other")["action"] == "send"

# 上限
for i in range(m.WXMSG_QUOTA_CAP + 3):
    m.wxmsg_note_accept("u3", TMPL, "n%d" % i, "accept")
snap = m.wxmsg_prepare_send("u3", TMPL, "fresh")
assert snap["remain"] == m.WXMSG_QUOTA_CAP, snap

# 发送日志：成功 / 43101（含 HTTP 错误体）
class Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEEN = {}


def fake_open(req, timeout=8):
    SEEN["url"] = req.full_url
    SEEN["body"] = json.loads(req.data.decode("utf-8"))
    return Resp(SEEN["resp"])


class _SendStub:
    _wxmsg_access_token = m.Handler._wxmsg_access_token


m.urllib.request.urlopen = fake_open
m._WXMSG_TOKEN["tok"] = "tok"
m._WXMSG_TOKEN["ts"] = 10**12
os.environ["WXMSG_MINIPROGRAM_STATE"] = "developer"
SEEN["resp"] = {"errcode": 0, "errmsg": "ok"}
card = m.wxmsg_trip_payload(PHRASE_DEF, "12.3 km · 25 分钟")
ok = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card, "pages/home/home")
assert ok["ok"] and ok["errcode"] == 0, ok
assert "cgi-bin/message/subscribe/send" in SEEN["url"]
assert SEEN["body"]["miniprogram_state"] == "developer"
assert SEEN["body"]["data"][PHRASE]["value"] == PHRASE_DEF
assert SEEN["body"]["data"][THING]["value"] == "12.3 km · 25 分钟"
assert "time3" not in SEEN["body"]["data"]
assert set(SEEN["body"]["data"].keys()) == {PHRASE, THING}
assert "发送成功 errcode=0" in logs_text()

SEEN["resp"] = {"errcode": 43101, "errmsg": "user refuse to accept the msg"}
bad = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert bad["ok"] is False and bad["errcode"] == 43101, bad
assert "发送失败 errcode=43101" in logs_text()
assert "一次性额度已用完" in logs_text()

class Boom(Exception):
    def read(self):
        return b'{"errcode":43101,"errmsg":"user refuse to accept the msg"}'


def boom_open(req, timeout=8):
    raise Boom("http 412")


m.urllib.request.urlopen = boom_open
bad2 = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert bad2["errcode"] == 43101, bad2

m._WXMSG_TOKEN["tok"] = ""
m._WXMSG_TOKEN["ts"] = 0
m.MP_WX_APPID = ""
m.MP_WX_SECRET = ""
miss = m.Handler.send_subscribe_msg(_SendStub(), "openid-ABCDEFGH", TMPL, card)
assert miss["ok"] is False and miss["errmsg"] == "no access_token", miss
assert "无 access_token" in logs_text()

# settle_trip 把行程号和 phrase 默认文案交给本机接口，失败只记日志
import track_watcher_multi as tw

posted = {}


class TwResp:
    def read(self):
        return b'{"ok":true,"sent":false,"reason":"no_quota","errcode":null}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def tw_open(req, timeout=5):
    posted["url"] = req.full_url
    posted["body"] = json.loads(req.data.decode("utf-8"))
    return TwResp()


tw.urllib.request.urlopen = tw_open
t0 = 1700000000000
tw.settle_trip("t1", [
    {"ts": t0, "lat": 31.2, "lon": 121.5, "gear": "D", "spd": 30, "odo": 10.0},
    {"ts": t0 + 180000, "lat": 31.3, "lon": 121.6, "gear": "D", "spd": 40, "odo": 12.5},
    {"ts": t0 + 181000, "lat": 31.3, "lon": 121.6, "gear": "P", "spd": 0, "odo": 12.5},
])
assert posted["url"].endswith("/internal/wxmsg"), posted
assert posted["body"]["title"] == PHRASE_DEF
assert posted["body"]["uid"] == "t1"
assert posted["body"]["trip_id"]
assert "特斯拉" not in posted["body"]["title"]
assert "特斯拉" not in posted["body"]["value"]

print("WXMSG_QUOTA_TESTS_PASS")
