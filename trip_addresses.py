# -*- coding: utf-8 -*-
"""行程详情起终点地址修复（小程序 /api/trip_list 与网页详情共用口径）。

历史结算文件可能留下占位文案、坐标串或仅到市/区级的短地址；列表接口在返回前
按需调用 refresh_addresses，成功后由调用方原子回写 JSON。
"""
import re

# 与 car-web-multi._api_trip_detail 旧缓存升级规则对齐
_BAD_PLACEHOLDERS = ("", "未知位置", "未知", "起点", "终点")


def _is_bad_addr(addr):
    s = str(addr or "").strip()
    if s in _BAD_PLACEHOLDERS:
        return True
    if re.search(r"\d{1,3}\.\d{3,}", s):
        return True
    if re.fullmatch(r"[^市县区]{0,3}[市县区]", s):
        return True
    return False


def refresh_addresses(detail, regeo_fn):
    """若 start/end 地址为占位或短地址，用 regeo_fn(lat, lon) 回填。

    Args:
        detail: trip_details/*.json 结算 dict（就地修改）
        regeo_fn: 逆地理函数，签名 (lat, lon) -> str|None/空串

    Returns:
        True 表示至少改写了一处地址（调用方应原子回写）；否则 False。
    """
    if not isinstance(detail, dict) or not callable(regeo_fn):
        return False
    changed = False
    for tag in ("start", "end"):
        coord = detail.get(tag + "_coord")
        addr_key = tag + "_addr"
        if not (isinstance(coord, (list, tuple)) and len(coord) >= 2):
            continue
        if not _is_bad_addr(detail.get(addr_key)):
            continue
        try:
            name = regeo_fn(float(coord[0]), float(coord[1]))
        except Exception:
            name = None
        if name and not _is_bad_addr(name):
            detail[addr_key] = name
            changed = True
    return changed
