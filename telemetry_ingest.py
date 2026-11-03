#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tesla Fleet Telemetry -> telemetry_state_<VIN>.json
按 VIN 分文件落盘：每台车一份快照，互不串数据。
兼容旧路径：仍维护 /opt/tesla-fleet/telemetry_state.json（指向最新有数据的 VIN 的旧文件），
供未迁移的读取方过渡；新读取方应使用 *_<VIN>.json。
"""
import json
import shutil
import os
import socket
import time
import sys
from datetime import datetime, timezone
from vehicle_measurements import normalize

LOG = "/var/log/fleet-telemetry/server.log"
STATE_DIR = "/opt/tesla-fleet/telemetry_states"
LEGACY_STATE = "/opt/tesla-fleet/telemetry_state.json"
TMP_SUFFIX = ".tmp"
TELEMETRY_NOTIFY_HOST = "127.0.0.1"
TELEMETRY_NOTIFY_PORT = int(os.environ.get("TELEMETRY_NOTIFY_PORT", "18796"))

# 遥测字段名 -> 内部字段名
FIELD_MAP = {
    # 电量/续航
    "BatteryLevel": "battery_level",
    "RatedRange": "rated_range",
    "EstBatteryRange": "est_battery_range",
    "ChargeState": "charging_state",
    "ACChargingPower": "ac_charging_power",
    "DCChargingPower": "dc_charging_power",
    "ChargeLimitSoc": "charge_limit_soc",
    "ChargerVoltage": "charger_voltage",
    "ChargeAmps": "charger_actual_current",
    "DCChargingEnergyIn": "charge_energy_added",
    "ACChargingEnergyIn": "ac_energy_added",
    "PackVoltage": "pack_voltage",
    "PackCurrent": "pack_current",
    # 门锁/车门
    "Locked": "locked",
    "DoorState": "door_state",
    # 档位/行驶
    "Gear": "shift_state",
    "VehicleSpeed": "speed",
    # 里程/胎压/版本
    "Odometer": "odometer",
    "Version": "car_version",
    "TpmsPressureFl": "tpms_fl",
    "TpmsPressureFr": "tpms_fr",
    "TpmsPressureRl": "tpms_rl",
    "TpmsPressureRr": "tpms_rr",
    # 温度
    "InsideTemp": "inside_temp",
    "OutsideTemp": "outside_temp",
    # 空调（HvacPower 才是总开关）
    "HvacPower": "hvac_power",
    "HvacACEnabled": "hvac_ac_enabled",
    "HvacAutoMode": "hvac_auto_mode",
    "HvacFanSpeed": "fan_status",
    "HvacFanStatus": "hvac_fan_status",
    "HvacLeftTemperatureRequest": "driver_temp_setting",
    "HvacRightTemperatureRequest": "passenger_temp_setting",
    # 方向盘加热 / 座椅通风
    "HvacSteeringWheelHeatLevel": "steering_wheel_heater",
    "HvacSteeringWheelHeatAuto": "steering_wheel_heat_auto",
    "ClimateSeatCoolingFrontLeft": "seat_cooler_level",
    "ClimateSeatCoolingFrontRight": "seat_cooler_level_right",
    # 哨兵
    "SentryMode": "sentry_mode",
    # 电池包遥测(BatteryData 整包,2026-10-24)
    "BatteryData": "battery_pack",
    "FastChargerPresent": "fast_charger_present",
    "Location": "Location",
}

# 充电状态枚举 -> 中文/标准值
CHARGE_STATE_MAP = {
    "ChargeStateDisconnected": "Disconnected",
    "ChargeStateNoPower": "NoPower",
    "ChargeStateStarting": "Starting",
    "ChargeStateCharging": "Charging",
    "ChargeStateComplete": "Complete",
    "ChargeStateStopped": "Stopped",
}


def state_path(vin):
    return os.path.join(STATE_DIR, "telemetry_state_%s.json" % vin)


def load_state(vin):
    try:
        with open(state_path(vin), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fields": {}, "last_update": 0, "vin": vin}


def save_state(st):
    vin = st.get("vin") or "UNKNOWN"
    now = time.time()
    st["ingest_ts"] = now
    st["last_update"] = now   # 兼容旧读取方；语义同 ingest_ts
    path = state_path(vin)
    tmp = path + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, path)
    # [2026-09-28] 历史留档：每小时一份快照，保留 7 天（诊断充电/唤醒问题用）
    try:
        hdir = "/opt/tesla-fleet/telemetry_history"
        os.makedirs(hdir, exist_ok=True)
        hp = os.path.join(hdir, "tele_%s.json" % time.strftime("%Y%m%d-%H"))
        if not os.path.exists(hp):
            os.replace(tmp + ".hist", hp) if os.path.exists(tmp + ".hist") else shutil.copyfile(path, hp)
        # 清理 7 天前的
        cutoff = time.time() - 7 * 86400
        for fn in os.listdir(hdir):
            fp = os.path.join(hdir, fn)
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except Exception:
        pass
    # 只在本机通知网页服务：数据已经原子落盘，可以立即唤醒所有 SSE 消费者。
    # UDP 不阻塞遥测摄取；即使 web 暂停或重启，状态文件仍是事实源。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as notify:
            notify.sendto(vin.encode("ascii", errors="ignore")[:64],
                          (TELEMETRY_NOTIFY_HOST, TELEMETRY_NOTIFY_PORT))
    except OSError:
        pass
    # 兼容旧读取方：把最新一份同时镜像到旧路径（单文件时代行为）
    try:
        ltmp = LEGACY_STATE + TMP_SUFFIX
        with open(ltmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(ltmp, LEGACY_STATE)
    except Exception:
        pass
    # 通知 web 端：该 VIN 有新数据（web 服务监听事件文件的 mtime 做 SSE 唤醒）
    # 事件文件也按 VIN 分：telemetry_event_<VIN>.json；另维护全局 telemetry_event.json
    try:
        base = "/opt/tesla-fleet/telemetry_event"
        for path_, ev in ((path, base + "_" + vin + ".json"), (LEGACY_STATE, base + ".json")):
            seq = 0
            try:
                with open(ev, encoding="utf-8") as f:
                    seq = int(json.load(f).get("seq") or 0)
            except Exception:
                seq = int(time.time() * 10)
            with open(ev + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"seq": seq + 1, "ts": st["last_update"], "vin": vin,
                           "vehicle_ts": st.get("vehicle_ts"), "ingest_ts": st.get("ingest_ts")}, f)
            os.replace(ev + ".tmp", ev)
    except Exception:
        pass


def norm(value):
    """把遥测值规范化成仪表盘能用的形态"""
    if isinstance(value, dict):
        if value.get("invalid") is True:
            return None
        # {"doubleValue": 12.3} / {"stringValue": "x"} / {"booleanValue": true} / {"intValue": 5}
        for k in ("doubleValue", "floatValue", "intValue", "longValue", "stringValue", "booleanValue",
                  "shiftStateValue", "chargingValue", "detailedChargeStateValue"):
            if k in value:
                return value[k]
        # 嵌套的 enum 等
        return value.get("value", value)
    if isinstance(value, list):
        return [norm(v) for v in value]
    return value


def epoch_seconds(value):
    """遥测记录时间兼容秒/毫秒/ISO-8601；无法识别时返回 0。"""
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
        if number > 100000000000000000:
            return number / 1000000000.0
        if number > 100000000000000:
            return number / 1000000.0
        return number / 1000.0 if number > 100000000000 else number
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def record_vehicle_ts(rec, items):
    """Tesla 不同 logger 版本的事件时间字段不一致，按常见名称防御式提取。"""
    candidates = [rec]
    candidates.extend(item for item in items if isinstance(item, dict))
    for obj in candidates:
        for key in ("vehicle_ts", "CreatedAt", "createdAt", "created_at", "timestamp", "time"):
            parsed = epoch_seconds(obj.get(key))
            if parsed:
                return parsed
    return 0.0


def handle_record(rec):
    """处理一条 record_payload"""
    data = rec.get("data")
    vin = rec.get("vin")
    if not data or not vin:
        return False

    items = data if isinstance(data, list) else [data]
    st = load_state(vin)
    st["vin"] = vin
    fields = st.setdefault("fields", {})
    changed = False
    observed = False
    vehicle_ts = record_vehicle_ts(rec, items)
    if vehicle_ts:
        st["vehicle_ts"] = vehicle_ts

    for item in items:
        if not isinstance(item, dict):
            continue
        # logger 输出形如 {"BatteryLevel": {"doubleValue": 80.0}, ...}
        # 也可能带 key/value 包装
        pairs = item.items()
        if "key" in item and "value" in item:
            pairs = [(item["key"], item["value"])]
        for k, v in pairs:
            key = k.split(".")[-1]  # 去掉前缀
            if key in FIELD_MAP:
                observed = True
                val = norm(v)
                if key == "BatteryData" and isinstance(val, dict):
                    # BatteryData 是嵌套包:{PackVoltage:{doubleValue:339},...};拍平并去前缀
                    val = {str(kk).split(".")[-1]: norm(vv) for kk, vv in val.items()}
                if key == "ChargeState" and isinstance(val, str):
                    val = CHARGE_STATE_MAP.get(val, val)
                internal = FIELD_MAP[key]
                st.setdefault("field_ts", {})[internal] = vehicle_ts or time.time()
                if fields.get(internal) != val:
                    fields[internal] = val
                    changed = True
    # --- 枚举→布尔/数值 规范化（遥测原始值多为字符串枚举）---
    def _enum_bool(v):
        """HvacPowerStateOff / SentryModeStateOff 等枚举转布尔。"""
        if isinstance(v, bool):
            return v
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in ("", "<invalid>", "unknown", "none", "null"):
            return None
        # 明确表示"关/停/无"的枚举
        if s.endswith("off") or s.endswith("disabled") or s.endswith("disconnected") \
           or "stateoff" in s or s in ("0", "false", "off", "closed", "idle", "stopped"):
            return False
        if s.endswith("on") or s.endswith("enabled") or "stateon" in s \
           or s in ("1", "true", "on", "open", "charging"):
            return True
        return bool(v)

    def _clean(v):
        """清理 <invalid> 之类的占位值。"""
        if isinstance(v, str) and v.strip().lower() in ("<invalid>", "invalid", "unknown"):
            return None
        return v

    # 派生布尔/数值字段（前端按钮状态依赖）
    if changed or True:
        f = st.get("fields") or {}
        # 清理占位值
        for k in list(f.keys()):
            f[k] = _clean(f[k])
        # is_climate_on: HvacPower 优先（枚举转布尔）；退回 HvacACEnabled
        hp = _enum_bool(f.get("hvac_power"))
        if hp is not None:
            f["is_climate_on"] = hp
        else:
            ha = _enum_bool(f.get("hvac_ac_enabled"))
            if ha is not None:
                f["is_climate_on"] = ha
        # sentry_mode: 枚举转布尔
        sm = _enum_bool(f.get("sentry_mode"))
        if sm is not None:
            f["sentry_mode"] = sm
        # charging: ChargeState 转布尔
        cs = f.get("charging_state")
        if cs is not None:
            f["charging"] = str(cs).lower() in ("charging", "starting")
        # Location: 对象 {"latitude":..,"longitude":..} → 展开，供位置条/行程用
        locobj = f.get("Location")
        if isinstance(locobj, dict):
            if locobj.get("latitude") is not None:
                f["latitude"] = locobj.get("latitude")
                f["longitude"] = locobj.get("longitude")
            del f["Location"]
        # cn 固件直流快充时 ChargeState 仍是 Idle（2026-10-07 实测 58.7kW 直流+Idle）：
        # 用实际功率推导充电中（>1kW），Complete 优先
        try:
            pwr = max(float(f.get("dc_charging_power") or 0), float(f.get("ac_charging_power") or 0))
            if cs is not None and str(cs).lower() == "complete":
                f["charging"] = False
            elif pwr > 1:
                f["charging"] = True
        except Exception:
            pass
        # 数值规范化
        for k in ("driver_temp_setting", "passenger_temp_setting", "inside_temp",
                  "outside_temp", "battery_level", "odometer"):
            v = f.get(k)
            if isinstance(v, str):
                try:
                    f[k] = float(v)
                except Exception:
                    pass
        # steering_wheel_heater: level>0 视为开
        sw = f.get("steering_wheel_heater")
        if isinstance(sw, bool):
            pass
        elif sw is not None:
            try:
                f["steering_wheel_heater"] = float(sw) > 0
            except Exception:
                b = _enum_bool(sw)
                if b is not None:
                    f["steering_wheel_heater"] = b

    st["fields"] = normalize(st["fields"])
    # 即使字段值没变化，也要更新时间链：车辆仍持续上报，不能被误判为遥测断供。
    if observed and (changed or time.time() - float(st.get("last_update") or 0) >= 2):
        save_state(st)
    return changed


def main():
    # 从文件末尾开始（避免重启刷历史）
    try:
        f = open(LOG, encoding="utf-8", errors="replace")
        f.seek(0, 2)
    except FileNotFoundError:
        time.sleep(2)
        f = open(LOG, encoding="utf-8", errors="replace")
        f.seek(0, 2)

    buf = ""
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.3)
            # 日志轮转双重处理(2026-10-28):copytruncate 截断(inode 不变,文件变小)时
            # 旧句柄偏移永远追不上新内容——必须回卷;rename 轮转(换新 inode)则重开新文件。
            # 此前只处理了 rename,首次 size 轮转(10-26)后摄取静默卡死 2.5 天。
            try:
                st_size = os.path.getsize(LOG)
                if st_size < f.tell():
                    f.seek(0)          # 被截断:从头读新日志
                    buf = ""
                elif os.stat(LOG).st_ino != os.fstat(f.fileno()).st_ino:
                    f.close()
                    f = open(LOG, encoding="utf-8", errors="replace")
                    f.seek(0, 2)
                    buf = ""
            except Exception:
                pass
            continue
        buf += line
        if not line.endswith("\n"):
            continue
        raw = buf.strip()
        buf = ""
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("msg") == "record_payload" or "data" in obj:
            try:
                handle_record(obj)
            except Exception as e:
                print("handle err:", e, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
