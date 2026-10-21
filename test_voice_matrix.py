# -*- coding: utf-8 -*-
"""语音控车解析层全量矩阵测试（离线，不碰车、不依赖 LLM/语义路由）。

用法：python -X utf8 test_voice_matrix.py
覆盖：单指令 / 温度指令（含中文数字、°C、ASR 空格）/ 复合指令（粘连、连接词、
空格分隔、指令含温度）/ 模糊口语。两前端（小程序/H5）共用此解析层，测过即两边生效。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# OWNER_BASE：本地 Windows 与服务器 /opt 环境都能跑（服务器默认即正确值）
os.environ.setdefault("OWNER_BASE", "/opt/tesla-fleet" if os.name == "posix" else r"E:\tesla\fleet")

import user_runner as ur


class _NoLLM:   # 模拟 LLM/key 不可用：任何漏进 LLM 的说法都会被测试抓出来
    def run(self, *a, **k):
        raise RuntimeError("llm-off")


ur.subprocess = _NoLLM()
ur._route_seg = lambda seg: None   # 语义路由服务离线：只考本地规则层

# (说法, 期望动作序列, 期望参数关键字 dict 列表——只比对给出的键)
CASES = [
    # ---- 单指令 ----
    ("锁车", ["door_lock"], [{}]),
    ("解锁车辆", ["door_unlock"], [{}]),
    ("闪灯", ["flash_lights"], [{}]),
    ("鸣笛", ["honk_horn"], [{}]),
    ("开空调", ["auto_conditioning_start"], [{}]),
    ("关空调", ["auto_conditioning_stop"], [{}]),
    ("打开后备箱", ["actuate_trunk"], [{"which_trunk": "rear"}]),
    ("关后备箱", ["close_trunk"], [{}]),
    ("开前备箱", ["actuate_trunk"], [{"which_trunk": "front"}]),
    ("开哨兵模式", ["set_sentry_mode"], [{"on": True}]),
    ("关闭哨兵", ["set_sentry_mode"], [{"on": False}]),
    ("充电", ["charge_start"], [{}]),
    ("停止充电", ["charge_stop"], [{}]),
    ("打开充电口", ["charge_port_door_open"], [{}]),
    ("开车窗", ["window_control"], [{"command": "vent"}]),
    ("关车窗", ["window_control"], [{"command": "close"}]),
    ("方向盘加热", ["remote_steering_wheel_heater_request"], [{"on": True}]),
    ("关方向盘加热", ["remote_steering_wheel_heater_request"], [{"on": False}]),
    ("座椅加热2档", ["remote_seat_heater_request"], [{"level": 2}]),
    ("座椅通风", ["remote_seat_cooler_request"], [{"seat_cooler_level": 1}]),
    ("查电量", ["status"], [{}]),
    # ---- 温度指令（数字 / °C / 中文数字 / ASR 空格 / 小数） ----
    ("空调调到22度", ["set_temps"], [{"driver_temp": 22}]),
    ("空调开到22°C", ["set_temps"], [{"driver_temp": 22}]),
    ("空调开到25度", ["set_temps"], [{"driver_temp": 25}]),
    ("空调开到二十三度", ["set_temps"], [{"driver_temp": 23}]),
    ("空调二十一度", ["set_temps"], [{"driver_temp": 21}]),
    ("温度调到23.5度", ["set_temps"], [{"driver_temp": 23.5}]),
    ("空调 开到 22 度", ["set_temps"], [{"driver_temp": 22}]),   # ASR 词间插空格
    # ---- 充电限制 ----
    ("充电到80", ["set_charge_limit"], [{"percent": 80}]),
    ("充电上限85", ["set_charge_limit"], [{"percent": 85}]),
    # ---- 复合指令 ----
    ("闪灯鸣笛", ["flash_lights", "honk_horn"], [{}, {}]),
    ("解锁车辆打开空调闪灯", ["door_unlock", "auto_conditioning_start", "flash_lights"], [{}, {}, {}]),
    ("解锁车辆，闪灯，空调开到22°C", ["door_unlock", "flash_lights", "set_temps"],
     [{}, {}, {"driver_temp": 22}]),   # 2026-10-22 用户原句：温度段被粗规则吞、解锁段被锁车劫持
    ("解锁车辆闪灯空调二十度", ["door_unlock", "flash_lights", "set_temps"],
     [{}, {}, {"driver_temp": 20}]),   # 纯粘连版：粘连切分需识别「空调/温度」分隔点（闪灯曾被吞）
    ("闪灯空调开到22°C", ["flash_lights", "set_temps"], [{}, {"driver_temp": 22}]),
    ("空调二十度", ["set_temps"], [{"driver_temp": 20}]),   # 单温度指令回归：切分增强不误伤
    ("空调开到22°C然后锁车", ["set_temps", "door_lock"], [{"driver_temp": 22}, {}]),
    ("空调开到22度然后锁车", ["set_temps", "door_lock"], [{"driver_temp": 22}, {}]),
    ("先闪灯再鸣笛然后锁车", ["flash_lights", "honk_horn", "door_lock"], [{}, {}, {}]),
    ("锁车 鸣笛 闪灯", ["door_lock", "honk_horn", "flash_lights"], [{}, {}, {}]),
    ("空调开到22度顺便锁车", ["set_temps", "door_lock"], [{"driver_temp": 22}, {}]),
    ("充电到80然后锁车", ["set_charge_limit", "door_lock"], [{"percent": 80}, {}]),
    # ---- 模糊口语 ----
    ("帮我解锁", ["door_unlock"], [{}]),
    ("把车锁了", ["door_lock"], [{}]),
    ("空调关了", ["auto_conditioning_stop"], [{}]),
    ("把空调打开", ["auto_conditioning_start"], [{}]),
    ("空调开一下", ["auto_conditioning_start"], [{}]),
    ("麻烦锁一下车", ["door_lock"], [{}]),
]


def params_match(got, want):
    return all(abs(got[k] - want[k]) < 0.01 if isinstance(want[k], float) else got[k] == want[k]
               for k in want)


def run_case(text, want_actions, want_params):
    acts, err = ur.resolve_actions(text)
    if err:
        return False, "解析失败: " + str(err)[:60]
    if not acts:
        return False, "无动作"
    got_names = [a.get("action") for a in acts]
    if got_names != want_actions:
        return False, f"动作不符: {got_names}"
    for a, wp in zip(acts, want_params):
        if wp and not params_match(a.get("params") or {}, wp):
            return False, f"参数不符: {a.get('params')}"
    return True, ""


def main():
    fails = []
    for text, wa, wp in CASES:
        ok, msg = run_case(text, wa, wp)
        mark = "PASS" if ok else "FAIL"
        print(f"{mark} {text} -> {msg if msg else ' / '.join(wa)}")
        if not ok:
            fails.append(text)
    print(f"\nRESULT: {len(CASES) - len(fails)}/{len(CASES)} pass")
    if fails:
        print("FAILED:", " | ".join(fails))
        sys.exit(1)


if __name__ == "__main__":
    main()
