"""程序侧业务规则组件：模型拿不到的公司数据/惯例，由程序归一与翻译。

两类规则（用户定稿，均「包含业务规则，不能给模型处理」）：
1. 地址归一（显示名 → 内部码）：园区码表（0551=合肥，0552=小镇）是公司数据，
   模型从 sub_query 结构性无法推导，必须程序查表。LLM 输出「小镇A1四楼」「A1_4F」
   「A1园区」→ 归一为工具契约地址「0552_A1_4F」等，供执行层 room.list 使用。
2. 公司时间计算器：午别 + 时长（「周三下午…连续用3小时」）→ 规范起止时刻
   （下午 → 14:00 起点，+3h → 17:00）。含公司工作时段惯例，不给模型处理。

适用范围：#41「抽取=模型执行」保留的窄例外——模型主导字段抽取（时间/容量/
流程），但这两类**查表/惯例翻译**程序拥有，即使模型已给值也按业务规则归一
（与「绝不覆盖模型语义值」不冲突：这不是语义纠错，是格式/惯例翻译）。
"""

from __future__ import annotations

import re
from typing import Any

# 园区码表（与 executor / simulator _match_office_address 同源）。
_CAMPUS_CODE = {"小镇": "0552", "合肥": "0551"}
_DEFAULT_CAMPUS = "0552"

_BUILDING_RE = re.compile(r"[A-Z]\d")
_FLOOR_CN = {
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "零": "0",
}

# 「无楼栋有楼层」地址展开时的楼栋枚举（与 understanding._build_addresses 同源）：
# 地址只给园区+楼层（如 小镇一楼 → 0552_1F）时，room.list 无法直接匹配，需展开为
# 园区下各楼栋的同楼层（0552_A1_1F … 0552_A5_1F）。**唯一权威定义**，两处共用。
_FLOORLESS_BUILDINGS = ("A1", "A2", "A3", "A4", "A5")


def normalize_office_address(address: str) -> str:
    """把单个地址归一为工具契约内部码（``园区码[_楼栋[_楼层]]``）。

    已归一（以 0551/0552 开头）直接透传；显示名（小镇A1四楼 / A1_4F / A2 /
    A1园区）解析园区、楼栋、楼层后重组。解析不出楼栋时降级为园区码。

    Args:
        address: 单个候选地址（可能为显示名）。

    Returns:
        归一后的内部码；解析失败时原样返回（不引入错误）。
    """
    if not address:
        return ""
    s = str(address).strip()
    # 已是内部码：透传（0552 / 0552_A1 / 0552_A1_4F）。
    if s.startswith("0551") or s.startswith("0552"):
        return s

    campus = _DEFAULT_CAMPUS
    for name, code in _CAMPUS_CODE.items():
        if name in s:
            campus = code
            break
    building_m = _BUILDING_RE.search(s)
    building = building_m.group(0) if building_m else None
    floor = _extract_floor(s)

    parts = [campus]
    if building:
        parts.append(building)
    if floor:
        parts.append(floor)
    return "_".join(parts)


def _extract_floor(text: str) -> str | None:
    """从文本提取楼层并归一（4楼 / 四楼 / 4F → "4F"；无楼层返回 None）。"""
    m = re.search(r"([0-9]|" + "|".join(_FLOOR_CN) + r")\s*(?:楼|F|f)", text)
    if not m:
        return None
    token = m.group(1)
    digit = token if token.isdigit() else _FLOOR_CN.get(token, "")
    return f"{digit}F" if digit else None


def normalize_addresses(addresses: Any) -> list[str]:
    """对候选地址列表整体归一（保持顺序、去空、去重）。

    无楼栋有楼层（如「小镇一楼」→ ``0552_1F``）时，把该地址展开为园区下各楼栋的
    同楼层（``0552_A1_1F`` … ``0552_A5_1F``）：room.list 的 office_address 契约
    是 ``园区[_楼栋[_楼层]]``，无楼栋的 ``0552_1F`` 会被执行层当成「楼栋=1F」
    而匹配不到任何房间（mr_0038 归零根因）。展开后执行层按楼栋逐个搜索，
    与理解层 `_build_addresses` 的「无楼栋有楼层 → 各楼栋该楼层」规则一致。
    """
    if not isinstance(addresses, list):
        return []
    out: list[str] = []
    for a in addresses:
        code = normalize_office_address(str(a))
        for expanded in _expand_floorless_address(code):
            if expanded and expanded not in out:
                out.append(expanded)
    return out


def _expand_floorless_address(code: str) -> list[str]:
    """把无楼栋有楼层的内部码（``0552_1F``）展开为各楼栋同楼层；否则原样返回。

    判定：两段式（``园区_楼层``，第二段是楼层码如 ``1F``）。已含楼栋（``0552_A1_1F``）
    或仅园区（``0552``）均不展开——前者契约完整，后者是合法宽搜范围。
    """
    parts = code.split("_")
    if len(parts) == 2 and re.fullmatch(r"\d+F", parts[1]):
        campus, floor = parts
        return [f"{campus}_{building}_{floor}" for building in _FLOORLESS_BUILDINGS]
    return [code]


def normalize_building(building: Any) -> Any:
    """楼栋名归一（A1…A5 透传；显示名提取楼栋字母数字）。"""
    if not isinstance(building, str):
        return building
    m = _BUILDING_RE.search(building.strip())
    return m.group(0) if m else building


def normalize_campus(campus: Any) -> Any:
    """园区名归一（0551/0552 透传；小镇→0552、合肥→0551）。"""
    if not isinstance(campus, str):
        return campus
    s = campus.strip()
    if s.startswith("0551") or s.startswith("0552"):
        return s
    for name, code in _CAMPUS_CODE.items():
        if name in s:
            return code
    return s


def normalize_floor(floor: Any) -> Any:
    """楼层归一（4F / 四楼 / 4楼 → "4F"）。"""
    if not isinstance(floor, str):
        return floor
    extracted = _extract_floor(floor.strip())
    return extracted if extracted else floor


# ------------------------------------------------------------ 公司时间计算器 --

# 公司工作时段起点（业务惯例）：午别 → 当日起点时刻。
_PERIOD_START = {"上午": "09:00", "中午": "12:00", "下午": "14:00", "晚上": "19:00"}

# 裸午别默认起止（业务惯例）：查询只给午别、无时长无显式时刻 → 固定 1 小时。
# gold 全量实测（zh_0003/0004/0005/0006/0009/0015 6 case 一致）：上午→10:00-11:00、
# 下午→14:00-15:00。注意上午是 10:00 而非工作时段起点 09:00。
_PERIOD_DEFAULT = {"上午": ("10:00", "11:00"), "下午": ("14:00", "15:00")}

_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def resolve_company_time(query: str) -> tuple[str, str] | None:
    """公司时间翻译：``午别 + 时长``（无显式起止）→ (start, end)。

    触发条件（三者同时满足，缺一即 None）：
    - query 含午别词（上午/中午/下午/晚上）；
    - query 含时长词（N小时 / 半小时 / N个半小时）；
    - query **无**显式「X点到Y点」区间（有显式区间走 LLM/规则，不被覆盖）。

    翻译规则（业务惯例）：
    - 起点 = 该午别的公司工作时段起点（下午 → 14:00），终点 = 起点 + 时长。
      示例：`下午…连续用3小时` → (14:00, 17:00)。
    - 只有午别、无时长（也无显式区间）→ 固定 1 小时默认：上午→(10:00, 11:00)、
      下午→(14:00, 15:00)。示例：`订明天下午的会议室` → (14:00, 15:00)。

    Args:
        query: 会议 sub_query（编排层上下文）。

    Returns:
        (start, end) 命中业务规则；否则 None。
    """
    if not query:
        return None
    if _has_explicit_range(query):
        return None
    period = _match_period(query)
    if period is None:
        return None
    hours = _match_duration_hours(query)
    if hours is None:
        return _PERIOD_DEFAULT.get(period)

    start_min = _to_minutes(_PERIOD_START[period])
    end_min = start_min + round(hours * 60)
    return _fmt_minutes(start_min), _fmt_minutes(end_min)


def _has_explicit_range(query: str) -> bool:
    """query 是否含显式「X点到Y点」起止区间。"""
    return bool(
        re.search(r"\d+\s*点\s*(?:半)?\s*(?:到|至|~|—|-)\s*\d+\s*点", query)
    )


def _match_period(query: str) -> str | None:
    for p in _PERIOD_START:
        if p in query:
            return p
    return None


def _match_duration_hours(query: str) -> float | None:
    """抽取时长并换算小时（3小时→3，半小时→0.5，一个半小时→1.5）。"""
    # 半小时 / 一个半小时 / 1个半小时。
    m_half = re.search(r"(?:一个|1个)半\s*小时", query)
    if m_half:
        return 1.5
    if re.search(r"半个\s*小时", query) or re.search(r"半小时", query):
        return 0.5
    # N 小时 / N 个小时 / 连续用N小时。
    m = re.search(r"([0-9]|" + "|".join(_CN_NUM) + r")\s*个?\s*(?:小|钟头)?\s*小时", query)
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return float(token)
    return float(_CN_NUM.get(token, 0))


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"
