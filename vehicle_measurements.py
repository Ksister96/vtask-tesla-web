"""Shared telemetry measurement validation; missing values must never become zero."""
import math
import time


def number(value, low, high):
    try:
        value = float(value)
        return value if math.isfinite(value) and low <= value <= high else None
    except (TypeError, ValueError):
        return None


def normalize(fields, field_ts=None, now=None):
    f = dict(fields)
    hvac = f.get('hvac_power')
    if isinstance(hvac, bool):
        f['is_climate_on'] = hvac
    elif isinstance(hvac, (str, int, float)):
        state = str(hvac).strip().lower()
        if state in ('hvacpowerstateoff', 'off', 'false', '0', '0.0'):
            f['is_climate_on'] = False
        elif state in ('hvacpowerstateon', 'on', 'true', '1', '1.0'):
            f['is_climate_on'] = True
    # Missing power is not an OFF observation; retain an already normalized value.
    gear = str(f.get("shift_state") or "")
    if gear.startswith("ShiftState"):
        gear = gear[len("ShiftState"):]
    f["shift_state"] = gear if gear in ("D", "R", "N", "P") else None
    dc = number(f.get("dc_charging_power"), 0, 1500) or 0
    ac = number(f.get("ac_charging_power"), 0, 100) or 0
    f["dc_charging_power"], f["ac_charging_power"] = dc, ac
    f["charge_energy_added"] = number(f.get("charge_energy_added"), 0, 1000)
    state = str(f.get("charging_state") or "").lower()
    ended = state in ("complete", "disconnected", "stopped", "nopower")
    f["charging"] = not ended and (state in ("charging", "starting") or max(ac, dc) > 1)
    f["charger_power"] = max(ac, dc) if f["charging"] else 0
    f["charger_voltage"] = number(f.get("charger_voltage"), 80, 1000) if f["charging"] else None
    # ChargeAmps is AC input current, not DC battery current.
    f["charger_actual_current"] = number(f.get("charger_actual_current"), 0, 200) if f["charging"] and dc <= 1 else None
    f["pack_voltage"] = number(f.get("pack_voltage"), 100, 1000)
    f["pack_current"] = number(f.get("pack_current"), -2000, 2000)
    if field_ts is not None:
        now = time.time() if now is None else now
        for key in ("charger_voltage", "charger_actual_current", "pack_voltage", "pack_current"):
            if not 0 <= now - float(field_ts.get(key) or 0) <= 180:
                f[key] = None
    return f
