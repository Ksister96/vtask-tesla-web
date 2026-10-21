# -*- coding: utf-8 -*-
"""asr_postprocess.py — ASR 转写文本后处理（本地 whisper 与云端 SenseVoice 共用）

只依赖标准库，Windows(Python 3.14) 与 Linux(Python 3.11) 通用。

职责：
  1. WHISPER_INITIAL_PROMPT —— whisper 专用语境先验（SenseVoice 不支持 prompt，云端不用）
  2. postprocess(text)      —— 确定性错字/同音纠正 + 时间表达规范化

维护约定（重要）：车控语音的纠正表**只有这一份**。
  本地 C:\\Users\\z1316\\scripts\\asr_postprocess.py
  云端 /opt/tesla-asr/asr_postprocess.py
两个引擎各自服务里**禁止再抄一份**，否则必然漂移。改动后两边同步覆盖。
"""

# 语音识别语境先验（仅 whisper 使用）。
# 强化中文车控与日常出行语境：加入「去东方明珠、去虹桥、去人民广场」等出行先验，
# 抑制指向词漂移（如「去」误识为「水」「处」「出」）及常见地名错音。
# 2026-10-22 补组合指令先验：一句话连发多个动作（闪灯鸣笛+调温等）是高频说法，
# 温度同时给中文数字与阿拉伯数字两种写法（whisper 两者都可能输出）。
WHISPER_INITIAL_PROMPT = (
    "这是一段特斯拉车控与日常用车的中文语音指令。常用操作包括："
    "一点一刻、两点三刻、三点一刻、四点两刻、十点三刻、十二点一刻、"
    "七点半、八点一刻、明早七点一刻去上班、晚上八点两刻去外滩；"
    "几点几分去东方明珠、去虹桥机场、去虹桥机场、去人民广场、去外滩、去上班、去陆家嘴、前往某地、到某处用车；"
    "打开空调、关闭空调、调到24度、空调调到二十度、空调调到二十三度、充电充到八十、"
    "解锁车辆、锁车、闪灯、鸣笛、后备箱、前备箱、关后备箱再锁车、车窗、哨兵模式、电池电量、查车况；"
    "组合指令一句话连发：闪灯鸣笛、先闪灯再鸣笛、闪灯鸣笛空调调到二十度、打开空调再锁车。"
)


def _apply_once(text):
    """单次纠正（与历史实现逐行等价，勿随意调整顺序）。"""
    # 常见错别字（确定性）
    text = text.replace("工调", "空调").replace("车量", "车辆")

    # 车控动词同音纠正（组合指令实测高频：「鸣笛」被听成「名笛/明笛/鸣滴」，「闪灯」被听成「山灯」；
    # 候选错词均非常用词，替换无副作用）
    text = text.replace("名笛", "鸣笛").replace("明笛", "鸣笛").replace("鸣滴", "鸣笛")
    text = text.replace("山灯", "闪灯")

    # 时间表达同音错字纠正（实测：whisper 把"十点三刻"听成"试电三颗"）
    # 必须在数字/时间还原之前执行，否则"试电三颗"里的字无法定位。
    text = text.replace("试电三颗", "十点三刻").replace("十点三颗", "十点三刻")
    text = text.replace("试电一刻", "十点一刻").replace("十点一颗", "十点一刻")
    text = text.replace("试电两刻", "十点两刻").replace("试电半", "十点半")

    # 地名同音纠正（实测："虹桥"被听成"风桥"/"红桥"）
    text = text.replace("风桥", "虹桥").replace("红桥", "虹桥")
    text = text.replace("红巧", "虹桥").replace("鸿桥", "虹桥")

    # 出行场景指向词的同音/漂移字纠正：
    # 「7点15,水东方明珠」->「7点15去东方明珠」；「7.26处于东方民族」->「7点26去东方民族」
    # 形式：时间/标点后出现「水/处/处于/出」，后跟汉字地名（2 字以上）
    text = _fix_qu(text)

    # 时间规范化：whisper 常把「7点15分去」转成「7.15去 / 7.15分去」。
    # 用车预约语境里 X.Y（Y∈0-59）几乎都是几点几分解读，必须还原成「X点Y分」，
    # 否则下游 parse_time 会把 X.Y 误当月/日分隔符（如 7.15 -> 7月15日，甚至跨年）。
    text = _fix_clock(text)

    # 时间还原后可能又暴露出指向词问题，再修一遍
    text = _fix_qu(text)
    return text


# 「水X / 处X」常见词表：这些不是地点指代词，命中必须跳过，避免把正常词纠坏。
# 例：水箱温度 / 水果买了没 / 处理一下 —— 都不能变成「去…」。
_NOT_QU = (
    "水箱 水温 水果 水平 水池 水质 水分 水泥 水管 水泵 水龙头 水位 水电 水费 水杯 "
    "水面 水深 水流 水花 水汽 水雾 水枪 水壶 水瓶 水表 水域 水岸 水路 水货 水军 水帘 水表 "
    "处理 处于 处分 处罚 处境 处长 处方 处置 处事 处处 处所 处级"
).split()
# 地名里几乎不会出现的虚词/动词字；出现在候选地名中即放弃纠正。
_NOT_QU_CHARS = set("了的地得着呢吗吧啊哦呀是不没别很太会能想要")


def _fix_qu(text):
    import re

    # 语境一：紧跟在时间/数字/标点/空白后（原规则，最可靠）
    text = re.sub(
        r"(?<=[点分\d\s，,。；;！!？?])(?:处于|水|处)(?=[\u4e00-\u9fa5]{2,})",
        "去",
        text,
    )

    # 语境二：句首或标点后单独出现（2026-09-15 周哥反馈：脱离时间语境也得修，
    # 如「水东方明珠」以前修不到）。保守起⻅：候选地名限 2~8 字、须到句末/标点为止，
    # 且不含虚词、不以常见「水X/处X」词开头。
    pat = re.compile(
        r"(^|[，,。；;！!？?\s])(水|处)([\u4e00-\u9fa5]{2,8})(?=$|[，,。；;！!？?\s])"
    )

    def _repl(m):
        head, ch, tail = m.group(1), m.group(2), m.group(3)
        if (ch + tail).startswith(tuple(_NOT_QU)):
            return m.group(0)
        if _NOT_QU_CHARS & set(tail):
            return m.group(0)
        return head + "去" + tail

    return pat.sub(_repl, text)


def _fix_clock(text):
    """把 7.15 / 7.15分 这类误写的时刻还原成 7点15(分)。"""
    import re

    def ok(y):
        return 0 <= int(y) <= 59

    # 情形A：X.Y分 -> X点YY分（几乎无歧义）
    text = re.sub(
        r"(\d{1,2})\.(\d{1,2})分",
        lambda m: f"{int(m.group(1))}点{int(m.group(2)):02d}分" if ok(m.group(2)) else m.group(0),
        text,
    )
    # 情形B：X.Y 后紧跟出行词，按几点几分解读
    text = re.sub(
        r"(\d{1,2})\.(\d{1,2})(?=去|到|前往|要用|要|出发|出门|用车|打车|接|去往)",
        lambda m: f"{int(m.group(1))}点{int(m.group(2)):02d}" if ok(m.group(2)) else m.group(0),
        text,
    )
    # 情形C：X.Y 在句末/标点前
    text = re.sub(
        r"(\d{1,2})\.(\d{1,2})(?=[\s，,。]|$)",
        lambda m: f"{int(m.group(1))}点{int(m.group(2)):02d}" if ok(m.group(2)) else m.group(0),
        text,
    )
    return text


def postprocess(text):
    """对 ASR 原始输出做确定性纠正；空输入原样返回。"""
    if not text:
        return text
    return _apply_once(text).rstrip("，,。 ")


if __name__ == "__main__":
    import sys
    for line in sys.stdin.read().splitlines():
        print(postprocess(line))
