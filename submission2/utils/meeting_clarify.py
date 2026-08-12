"""meeting 域多轮澄清槽规格：共享澄清器（utils.clarifier）的会议实现。

用户定案（2026-08-12）：多轮澄清是三域共用的通用能力。leave/budget 在各自
skill 内声明槽规格；meeting 在此模块声明（解析较复杂：相对日期 + 中文数字
时刻 + 园区/人数/主题）。缺槽判定基于**确定性约束提取器**对原始 query 的
初始解析（``c.building`` / ``c.day`` / ``c.capacity_gte`` / ``c.title``），
与 gold 的 dialogue_state.missing_slots 对齐：
- mt_0001/0201「帮我订个会议室」→ 全缺（day/office_id/attendees/title）；
- mt_0003「帮我在A2园区订」/ mt_0009、0202「先在A1看看，不行A2也可以」
  → 楼栋已在 query → office_id 不缺，只问 day/attendees/title。

模拟器协议（env.reply，见 simulator/env.py `_simulate_user_reply`）：提问语
**必须含** SLOT_PATTERNS 触发词，否则 resolved_slot 不命中（返回 fallback，
白耗一步）。对应关系（env.py:40-43）：
- day → "时间"；office_id → "园区"；attendees → "多少人"；title → "主题"。
"""

from __future__ import annotations

import re
from typing import Any

from utils.clarifier import ClarifySlot
from utils.leave_skill import _cn_num, _hour_with_period
from utils.understanding import MeetingConstraintExtractor, MeetingConstraints, TemporalResolver

# 提问语必须含模拟器 SLOT_PATTERNS 触发词（env.py:40-43）。
_CLARIFY_QUESTIONS: dict[str, str] = {
    "day": "请问是在什么时间？",
    "office_id": "请问是在哪个园区？",
    "attendees": "请问大概多少人参加？",
    "title": "请问会议主题是什么？",
}

# 会议确认语：须命中 CONFIRM_PATTERNS（env.py:59-64 含「确认」）。gold 在
# mt_0009/0202 的确认句会带「改订A2」说明，但内容不影响协议——只要求命中
# 确认模式即解锁 confirmation_required_before 里的 booking.create。
CONFIRM_REPLY = "可以的话我现在直接帮你预订，确认吗？"

# 订单号澄清：取消/延长等非预订意图在 query 无 SEED-* 时先问订单号
# （mt_0004/0011/0204/0205 的 missing_slots 都是 ['order_id']）。提问语必须含
# SLOT_PATTERNS["order_id"] 触发词（env.py:44「订单号」）。
ORDER_ID_QUESTION = "请提供这条会议预订的订单号。"

# 订单号透传（与 understanding._ORDER_ID_RE 同源）：只识别原文出现的 SEED-*。
_ORDER_ID_RE = re.compile(r"SEED-[A-Za-z0-9_-]+")


def parse_order_id(msg: str) -> str | None:
    """「订单号是 SEED-CANCEL-SELF-001。」→「SEED-CANCEL-SELF-001」。"""
    m = _ORDER_ID_RE.search(msg or "")
    return m.group(0) if m else None


def build_order_id_spec(c: MeetingConstraints) -> ClarifySlot:
    """订单号澄清槽：query 无 order_id 时缺（取消/延长多轮 case）。"""

    def _missing(t: str, o: dict[str, Any]) -> bool:
        return not c.order_id_hint and not o.get("order_id")

    def _parse(msg: str, o: dict[str, Any]) -> dict[str, Any]:
        oid = parse_order_id(msg)
        return {"order_id": oid} if oid else {}

    return ClarifySlot(
        key="order_id",
        question=ORDER_ID_QUESTION,
        missing=_missing,
        parse=_parse,
    )

# 「下午两点到三点」→ 中文/阿拉伯数字时刻区间（gold 模拟器 slot_replies 用
# 中文数字：mt_0001 的「下周二下午两点到三点」。与 understanding._fill_time 的
# 差异是只接受阿拉伯数字，这里补上中文数字支持）。
_TIME_RANGE_RE = re.compile(
    r"(上午|下午|晚上|中午)?\s*"
    r"([一两二三四五六七八九十\d]+)\s*点\s*(半)?\s*"
    r"(?:到|至|~|—|-)\s*"
    r"(上午|下午|晚上|中午)?\s*"
    r"([一两二三四五六七八九十\d]+)\s*点\s*(半)?"
)


def _parse_reply_time_range(msg: str) -> tuple[str | None, str | None]:
    """「下午两点到三点」→ (14:00, 15:00)；结束段无午别时继承起始午别。

    Returns:
        (start, end) HH:MM；非法区间（start>=end）或无法解析返回 (None, None)。
    """
    m = _TIME_RANGE_RE.search(msg or "")
    if not m:
        return None, None
    start_period = m.group(1)
    start_hour = _hour_with_period(int(_cn_num(m.group(2))), start_period)
    start_minute = 30 if m.group(3) == "半" else 0
    end_period = m.group(4) or start_period
    end_hour = _hour_with_period(int(_cn_num(m.group(5))), end_period)
    end_minute = 30 if m.group(6) == "半" else 0
    if start_hour * 60 + start_minute >= end_hour * 60 + end_minute:
        return None, None
    return f"{start_hour:02d}:{start_minute:02d}", f"{end_hour:02d}:{end_minute:02d}"


def _strip_title(msg: str) -> str:
    """「主题写季度复盘。」→「季度复盘」（剥离 主题/是/为/写 等引导词 + 标点）。"""
    t = re.sub(r"^主题\s*(?:是|为|写|就说|说)?\s*[:：]?\s*", "", (msg or "").strip())
    return t.strip("。，,、;；\t\n ")


def build_meeting_specs(now_iso: str, c: MeetingConstraints) -> list[ClarifySlot]:
    """构造 meeting 澄清槽规格；缺槽判定基于约束提取器对原始 query 的解析 ``c``。

    Args:
        now_iso: env.reset 的 now（ISO 字符串），解析回复里的相对日期用。
        c: ``analyze_meeting_query`` 对原始 query 解析出的 MeetingConstraints
            （未做任何澄清前的初值；只读）。

    Returns:
        ClarifySlot 列表（顺序 = 提问顺序：day → office_id → attendees → title）。
    """
    resolver = TemporalResolver(now_iso)

    def _missing_day(t: str, o: dict[str, Any]) -> bool:
        return not (c.day or c.days or c.week_start) and not o.get("day")

    def _parse_day(msg: str, o: dict[str, Any]) -> dict[str, Any]:
        # day 回复同时携带起止时刻（「下周二下午两点到三点」）→ 一并采纳。
        out: dict[str, Any] = {}
        day = resolver.resolve_day(msg)
        if day:
            out["day"] = day
        start, end = _parse_reply_time_range(msg)
        if start:
            out["start"] = start
        if end:
            out["end"] = end
        return out

    def _missing_office(t: str, o: dict[str, Any]) -> bool:
        # 初始解析无显式楼栋（campus 级默认地址不算）且澄清未给 → 缺。
        # mt_0003/0009/0202 query 已带楼栋（A2/A1）→ 不缺；mt_0001/0201 无 → 缺。
        return not c.building and not o.get("office_id") and not o.get("addresses")

    def _parse_office(msg: str, o: dict[str, Any]) -> dict[str, Any]:
        # 复用提取器的地点填充：「A1园区。」→ addresses=['0552_A1'], building=A1。
        tmp = MeetingConstraints()
        MeetingConstraintExtractor()._fill_location(msg, tmp)
        if not tmp.addresses:
            return {}
        out: dict[str, Any] = {"addresses": tmp.addresses}
        if tmp.building:
            out["office_id"] = tmp.building
        return out

    def _missing_attendees(t: str, o: dict[str, Any]) -> bool:
        return c.capacity_gte is None and "capacity_gte" not in o

    def _parse_attendees(msg: str, o: dict[str, Any]) -> dict[str, Any]:
        m = re.search(r"(\d{1,3})\s*(?:个)?人", msg or "")
        if not m:
            return {}
        n = int(m.group(1))
        return {"capacity_gte": n, "attendees": n}

    def _missing_title(t: str, o: dict[str, Any]) -> bool:
        return not c.title and not o.get("title")

    def _parse_title(msg: str, o: dict[str, Any]) -> dict[str, Any]:
        title = _strip_title(msg)
        return {"title": title} if title else {}

    return [
        ClarifySlot(
            key="day",
            question=_CLARIFY_QUESTIONS["day"],
            missing=_missing_day,
            parse=_parse_day,
        ),
        ClarifySlot(
            key="office_id",
            question=_CLARIFY_QUESTIONS["office_id"],
            missing=_missing_office,
            parse=_parse_office,
        ),
        ClarifySlot(
            key="attendees",
            question=_CLARIFY_QUESTIONS["attendees"],
            missing=_missing_attendees,
            parse=_parse_attendees,
        ),
        ClarifySlot(
            key="title",
            question=_CLARIFY_QUESTIONS["title"],
            missing=_missing_title,
            parse=_parse_title,
        ),
    ]


def apply_clarified(c: MeetingConstraints, clarified: dict[str, Any]) -> None:
    """把澄清采纳值合并进约束（原地更新 c）。

    只覆盖澄清明确给出的键；未澄清的槽保持原值（query 已带/执行层兜底）。
    """
    for key, attr in (
        ("day", "day"),
        ("start", "start"),
        ("end", "end"),
        ("office_id", "building"),
        ("capacity_gte", "capacity_gte"),
        ("attendees", "attendees"),
        ("title", "title"),
    ):
        v = clarified.get(key)
        if v is not None:
            setattr(c, attr, v)
    if clarified.get("addresses"):
        c.addresses = clarified["addresses"]
