# -*- coding: utf-8 -*-
"""车辆状态链路的时间戳、新鲜度和脱敏诊断工具。"""
import hashlib
import time
from datetime import datetime, timezone


def epoch_seconds(value):
    """兼容秒/毫秒时间戳和 ISO-8601，失败返回 0。"""
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


def uid_tag(uid):
    """日志只保留不可逆短标识，不写邮箱、姓名、VIN 或原始 uid。"""
    return hashlib.sha256(str(uid or "").encode("utf-8")).hexdigest()[:12]


def build_state_meta(fields, source, fresh, source_ts=None, ingest_ts=None,
                     publish_ts=None, stale_after_seconds=30):
    """构造供 REST/SSE 共用的状态流转元信息，所有公开时间戳为毫秒。"""
    fields = fields or {}
    publish = epoch_seconds(publish_ts) or time.time()
    has_explicit_vehicle_ts = "_vehicle_ts" in fields
    vehicle = epoch_seconds(fields.get("_vehicle_ts"))
    if not has_explicit_vehicle_ts:
        vehicle = epoch_seconds(fields.get("_frame_ts")) or epoch_seconds(source_ts)
    ingest = (epoch_seconds(fields.get("_ingest_ts"))
              or epoch_seconds(ingest_ts)
              or vehicle)
    observed = vehicle or ingest
    age_ms = max(0, int(round((publish - observed) * 1000))) if observed else None
    stale = bool(fields.get("_stale")) or not bool(fresh)
    if age_ms is not None and age_ms > int(stale_after_seconds * 1000):
        stale = True
    return {
        "vehicle_ts": int(vehicle * 1000) if vehicle else None,
        "ingest_ts": int(ingest * 1000) if ingest else None,
        "publish_ts": int(publish * 1000),
        "age_ms": age_ms,
        "source": str(source or "unknown"),
        "fresh": bool(fresh),
        "stale": stale,
    }
