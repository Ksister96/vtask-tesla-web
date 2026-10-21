# -*- coding: utf-8 -*-
"""Tesla 车控意图匹配器：自然语言 -> {action, params} JSON

用 GLM 大模型把用户自然语言映射到官方命令集。
输入: 自然语言（命令行参数或 stdin）
输出: {"action": "...", "params": {...}} 或 {"error": "..."}
"""
import json
import os
import sys
import urllib.request

API_URL = "https://api.teamorouter.cn/v1/chat/completions"
MODEL = "glm-5.3-flash-free"
MODEL_FALLBACK = "glm-5.3-flash"   # 福利版402限流时自动兜底（付费，单价极低）
API_KEY = os.environ.get("VECTIDE_API_KEY", "")

ACTIONS = """可用动作清单（JSON 的 action 字段必须是这些之一，全小写下划线）：
- honk_horn: 鸣笛/响喇叭
- flash_lights: 闪灯/双闪
- door_lock: 锁车/锁门
- door_unlock: 解锁/开门
- actuate_trunk: 开/关前备箱或后备箱（电动尾门可关闭）, params: {"which_trunk": "front"|"rear"}（用户说"关闭后备箱/关后备箱"也映射到它，which_trunk=rear）
- window_control: 车窗通风或关闭, params: {"command": "vent"|"close"}
- auto_conditioning_start: 开空调/开制冷/开暖气
- auto_conditioning_stop: 关空调
- set_temps: 设置温度, params: {"driver_temp": 数字, "passenger_temp": 数字}（温度可为小数，中国用户说摄氏度）
- set_preconditioning_max: 极速降温/极速加热, params: {"on": true|false}
- set_climate_keeper_mode: 保持模式, params: {"climate_keeper_mode": 1|2|3}（1=开启,2=宠物,3=露营）
- remote_steering_wheel_heater_request: 方向盘加热, params: {"on": true|false}
- remote_seat_heater_request: 座椅加热, params: {"seat_position": 0|1|2|3|4|5|6|7|8, "level": 0-3}（0=主驾,1=副驾,2=二排左,4=二排中,5=二排右）
- remote_seat_cooler_request: 座椅通风, params: {"seat_position": 0|1, "seat_cooler_level": 1-3}
- charge_start: 开始充电
- charge_stop: 停止充电
- set_charge_limit: 充电上限, params: {"percent": 数字}
- set_charging_amps: 充电电流安培, params: {"charging_amps": 数字}
- charge_port_door_open: 打开充电口
- charge_port_door_close: 关闭充电口
- set_scheduled_charging: 定时充电, params: {"enable": true|false, "time": 分钟数(午夜后)}
- set_sentry_mode: 哨兵模式, params: {"on": true|false}
- set_valet_mode: 代客模式, params: {"on": true|false, "password": 可选}
- set_vehicle_name: 改车名, params: {"vehicle_name": "新名字"}
- schedule_software_update: 预约更新, params: {"offset_sec": 秒数}
- cancel_software_update: 取消更新
- wake_up: 唤醒车辆
- status: 查车况（电量/续航/里程/温度/锁定）
- media_volume_up / media_volume_down / media_toggle_playback / media_next_track / media_prev_track: 媒体控制
- adjust_volume: 音量, params: {"volume": 数字}
- speed_limit_set_limit: 限速, params: {"limit_mph": 数字}
- trigger_homelink: 触发车库门, params: {"lat": 数字, "lon": 数字}

注意：
- "全部温度"、"主驾温度"等对话无明确数值时给默认合理值 22.0
- 危险动作（erase_user_data/reset_pin/clear_pin）不要匹配
- 用户说"查车况/电量/续航/状态/车在哪"都映射 status
- 用户在一句话中包含多个操作（如“开空调且闪灯”、“开后备箱顺便查电量”）时，必须返回 actions 数组，包含所有要执行的动作对象
- 即使只有单个动作，统一返回 {"actions": [{"action": "...", "params": {...}}]} 结构
- 不确定时返回 {"error": "无法识别意图"}"""

SYSTEM = """你是特斯拉车控命令解析器。把用户的中文自然语言指令转换为结构化命令。
只输出一个 JSON 对象，必须包含 "actions" 数组（即使只有单个动作），不要输出任何其他文字。

%s

示例：
输入: 帮我把车里空调调到25度
输出: {"actions":[{"action":"set_temps","params":{"driver_temp":25,"passenger_temp":25}}]}

输入: 按两下喇叭
输出: {"actions":[{"action":"honk_horn","params":{}}]}

输入: 帮我开空调21度，打开主驾座椅通风，顺便闪个灯
输出: {"actions":[{"action":"auto_conditioning_start","params":{}},{"action":"set_temps","params":{"driver_temp":21,"passenger_temp":21}},{"action":"remote_seat_cooler_request","params":{"seat_position":0,"seat_cooler_level":3}},{"action":"flash_lights","params":{}}]}

输入: 闪个灯顺便看看车还有多少电
输出: {"actions":[{"action":"flash_lights","params":{}},{"action":"status","params":{}}]}""" % ACTIONS


def call_llm(user_text: str) -> dict:
    last_err = ""
    for model in (MODEL, MODEL_FALLBACK):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP Error {e.code}"
            if e.code in (402, 429, 500, 502, 503):
                continue   # 福利版限流/过载 → 付费版兜底
            return {"error": f"API调用失败: {e}"}
        except Exception as e:
            return {"error": f"API调用失败: {e}"}
        text = data["choices"][0]["message"]["content"]
        # 模型可能把 JSON 放在 reasoning_content（content 为空）→ 回退
        if not text or not text.strip():
            text = data["choices"][0]["message"].get("reasoning_content") or ""
        # 提取 JSON（模型可能包在 markdown 代码块里）
        start_js = text.find("{")
        end_js = text.rfind("}") + 1
        if start_js == -1 or end_js == 0:
            last_err = f"模型输出无法解析: {text[:120]}"
            continue
        try:
            res = json.loads(text[start_js:end_js])
        except Exception as e:
            last_err = f"模型输出JSON错误: {e}"
            continue
        # 兼容旧格式：如果是单个 {"action": ..., "params": ...}，包装为 {"actions": [...]}
        if "action" in res and "actions" not in res:
            res = {"actions": [{"action": res["action"], "params": res.get("params") or {}}]}
        return res
    return {"error": f"API调用失败: {last_err}"}

if __name__ == "__main__":
    if not API_KEY:
        print(json.dumps({"error": "缺少 VECTIDE_API_KEY 环境变量"}, ensure_ascii=False))
        sys.exit(1)
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read().strip()
    if not text:
        print(json.dumps({"error": "请输入指令"}, ensure_ascii=False))
        sys.exit(1)
    result = call_llm(text)
    print(json.dumps(result, ensure_ascii=False))