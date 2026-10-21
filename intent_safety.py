# -*- coding: utf-8 -*-
"""Shared, side-effect-free intent gate. Run before normalization or splitting."""
import re

_NEGATION = re.compile(r"别|不要|不用|无需|不必|先不|暂不|不想|不能|不得|不准|禁止|取消|撤销|没(?:有)?(?:锁|开|关|解锁|充)|不(?:开|关|锁|解锁|充)")
_QUERY = re.compile(r"[?？]|[吗么呢]\s*[!！。.]?\s*$|(?:了)?(?:没|没有)\s*$|是否|有没有|是不是|能否|可否|怎么|如何|为什么|多少|几度|电量|续航|车况|状态|电池(?:健康)?|健康度|胎压|在哪|位置|查一下|查询")
_AMBIGUOUS = re.compile(r"如果|假如|要是|可能|或许|例如|比如|听说|他说|她说|我说|刚才|之前|昨天|已经|应该|是不是|\b(?:if|maybe|example)\b", re.I)
_PREFIX = re.compile(r"^(?:(?:请|帮我|麻烦你|麻烦|帮忙|把|将|给我)\s*)+")
_EXPLICIT = {
    "关方向盘加热": ("remote_steering_wheel_heater_request", {"on": False}),
    "关闭方向盘加热": ("remote_steering_wheel_heater_request", {"on": False}),
    "方向盘加热关掉": ("remote_steering_wheel_heater_request", {"on": False}),
    "开方向盘加热": ("remote_steering_wheel_heater_request", {"on": True}),
    "打开方向盘加热": ("remote_steering_wheel_heater_request", {"on": True}),
    "关充电口": ("charge_port_door_close", {}),
    "关闭充电口": ("charge_port_door_close", {}),
    "充电口关上": ("charge_port_door_close", {}),
    "开充电口": ("charge_port_door_open", {}),
    "打开充电口": ("charge_port_door_open", {}),
    "停止充电": ("charge_stop", {}),
    "结束充电": ("charge_stop", {}),
}

def intent_kind(text):
    t = str(text or "").strip()
    if not t:
        return "ambiguous"
    # Split only to distinguish a leading imperative from quoted/conditional clauses.
    clauses = [x.strip() for x in re.split(r"(?:然后|接着|并且|而且|同时|顺便|以及|但是|不过|但|之后|后|[,，;；。])", t) if x.strip()]
    command_head = bool(clauses and re.match(r"^(?:请|帮我|麻烦(?:你)?|把|将)?\s*(?:锁车|落锁|解锁|开门|打开车门|闪灯|鸣笛|按喇叭|开空调|打开空调|关空调|关闭空调|开暖风|开冷风|开窗|关窗|开车窗|关车窗|打开后备箱|开后备箱|关后备箱|打开前备箱|开前备箱|关闭前备箱|开哨兵|关哨兵|关闭哨兵|打开充电口|开充电口|关闭充电口|关充电口|停止充电|开启座椅|关闭座椅|打开座椅|关闭方向盘加热|关方向盘加热|开方向盘加热|设定充电|充电限制|充电上限|充电到|充到|预热|设为)", clauses[0]))
    for i, clause in enumerate(clauses):
        if _NEGATION.search(clause) or re.search(r"\b(?:do not|don't|dont|never|stop doing)\b", clause, re.I):
            # Explicit negation applies to a command only when it is the initial
            # intent. A later negative clause cannot negate an earlier imperative.
            return "negation" if i == 0 else "ambiguous"
    # A query clause means the utterance is not a command unless another explicit
    # command clause precedes it, in which case require clarification.
    query_indexes = [i for i,c in enumerate(clauses) if _QUERY.search(c)]
    if query_indexes:
        return "ambiguous" if command_head and any(i > 0 for i in query_indexes) else "query"
    if _AMBIGUOUS.search(t):
        return "ambiguous"
    if re.search(r"(?:我|你|他|她|车辆|车|空调).*(?:已经|刚刚|刚才)", t):
        return "ambiguous"
    return "command"

def safety_reply(kind):
    if kind == "negation":
        return "收到，这句话包含否定或取消意图，不会执行车辆动作。请将要执行的操作单独说清楚。"
    return "这句话的操作意图不明确，不会执行车辆动作。请直接说要执行的操作。"

def explicit_action(text):
    t = _PREFIX.sub("", str(text or "").strip()).strip(" 。.,，!！")
    hit = _EXPLICIT.get(t)
    return {"action": hit[0], "params": dict(hit[1])} if hit else None
