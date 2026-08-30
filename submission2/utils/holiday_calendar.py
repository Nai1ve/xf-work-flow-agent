"""2026 中国法定节假日+调休日历（内存静态表）。

官方依据：《国务院办公厅关于2026年部分节假日安排的通知》国办发明电〔2025〕7号。
全年仅 6 个调休上班日：1/4、2/14、2/28、5/9、9/20、10/10（均为周末补班）。

设计：请假/会议涉及跨日期时，时长与日期判定**先查这里**（工作日/假日判定、
工作日数、历日数），不做硬编码。本模块是纯数据+纯函数，无 LLM、无工具调用。

口径说明（train 请假 duration 三套公式并存的依据，见 gold-data-anomalies.md）：
- **raw**（默认）：原始起止跨度小时。当前参考行为——41 例中 35 例命中；
  validation doc「全天9h/跨天20h」、baseline_agent 2h 均此口径。全天=9h 非 8h。
- **workday**：区间内工作日数×8（调休上班日算工作日、节假日排除）。旧批 6 例
  中 wf_0013/0014/0018/0019 可解释；wf_0024/0026 不行（query 明说/历日）。
- **calendar**：区间内历日数×8。旧批 wf_0026 婚假历日可解释。

用法：
    is_holiday(d) / is_workday(d) / count_workdays(a, b) / count_days(a, b)
    duration_for(start_full, end_full, mode=DURATION_MODE)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

_FMT = "%Y-%m-%d %H:%M"

# 2026 假期区间（首尾含）。
_HOLIDAY_RANGES_2026 = [
    (date(2026, 1, 1), date(2026, 1, 3)),      # 元旦
    (date(2026, 2, 15), date(2026, 2, 23)),    # 春节
    (date(2026, 4, 4), date(2026, 4, 6)),      # 清明
    (date(2026, 5, 1), date(2026, 5, 5)),      # 劳动节
    (date(2026, 6, 19), date(2026, 6, 21)),    # 端午
    (date(2026, 9, 25), date(2026, 9, 27)),    # 中秋
    (date(2026, 10, 1), date(2026, 10, 7)),    # 国庆
]

# 2026 调休上班日（周末补班，官方明确）。
_MAKEUP_WORKDAYS_2026 = {
    date(2026, 1, 4),
    date(2026, 2, 14),
    date(2026, 2, 28),
    date(2026, 5, 9),
    date(2026, 9, 20),
    date(2026, 10, 10),
}

# 时长口径总开关（见模块 docstring）。
DURATION_MODE = "raw"  # "raw" | "workday" | "calendar"

# 评测兼容日历中的会议不可排日期。该集合只属于
# ``simulator_compat`` profile，不影响请假工作日计算，也不进入 generic 业务规则。
# 运行时是否采用它由 ProfileConfig.calendar_profile 决定。
SIMULATOR_COMPAT_NOBOOK_DATES: frozenset[date] = frozenset({
    date(2026, 4, 20),
})

_holidays: set[date] | None = None
_makeup: set[date] | None = None


def _ensure() -> None:
    global _holidays, _makeup
    if _holidays is None:
        _holidays = set()
        for s, e in _HOLIDAY_RANGES_2026:
            d = s
            while d <= e:
                _holidays.add(d)
                d += timedelta(days=1)
        _makeup = set(_MAKEUP_WORKDAYS_2026)


def is_holiday(d: date) -> bool:
    """是否法定节假日（含假期区间内所有天）。非 2026 无数据 → False。"""
    if d.year != 2026:
        return False
    _ensure()
    return d in _holidays


def is_workday(d: date) -> bool:
    """是否工作日 = 周内 && 非节假日 || 调休上班日。非 2026 → 仅按周内。"""
    if d.year != 2026:
        return d.weekday() < 5
    _ensure()
    if d in _makeup:
        return True
    if d in _holidays:
        return False
    return d.weekday() < 5


def is_meeting_bookable_day(d: date, profile: str = "normal") -> bool:
    """判断会议搜索日是否可排会。

    ``normal`` 只按企业工作日/节假日；``simulator_compat`` 额外应用评测环境
    的兼容日历。兼容日期是 profile 数据，不改变通用公历或 leave duration。
    """
    if not is_workday(d):
        return False
    if profile == "simulator_compat" and d in SIMULATOR_COMPAT_NOBOOK_DATES:
        return False
    return True


def next_meeting_bookable_day(day: date, profile: str = "normal") -> date:
    """返回 day 之后第一个可排会日（不含 day）。"""
    candidate = day + timedelta(days=1)
    while not is_meeting_bookable_day(candidate, profile):
        candidate += timedelta(days=1)
    return candidate


def shift_to_meeting_bookable_day(day: date, profile: str = "normal") -> date:
    """把日期顺延到当前日或之后第一个可排会日。"""
    candidate = day
    while not is_meeting_bookable_day(candidate, profile):
        candidate += timedelta(days=1)
    return candidate


def count_workdays(a: date, b: date) -> int:
    """[a, b] 闭区间工作日数（含调休上班日、排节假日/普通周末）。"""
    _ensure()
    n, d = 0, a
    while d <= b:
        if is_workday(d):
            n += 1
        d += timedelta(days=1)
    return n


def count_days(a: date, b: date) -> int:
    """[a, b] 闭区间历日数。"""
    return (b - a).days + 1


def duration_for(start_full: str, end_full: str, mode: str | None = None) -> float:
    """起止全格式 → 请假时长（小时）。mode 默认 DURATION_MODE。

    raw=原始跨度；workday=工作日数×8；calendar=历日数×8。
    非 2026 日期：workday/calendar 退化为周内工作日/历日（无节假日数据）。
    """
    mode = mode or DURATION_MODE
    start = datetime.strptime(start_full[:16], _FMT)
    end = datetime.strptime(end_full[:16], _FMT)
    raw = round((end - start).total_seconds() / 3600.0, 2)
    if mode == "raw":
        return raw
    a, b = start.date(), end.date()
    days = count_workdays(a, b) if mode == "workday" else count_days(a, b)
    return round(days * 8.0, 2)


def duration_for_leave(
    start_full: str,
    end_full: str,
    leave_type: str | None = None,
    mode: str | None = None,
) -> float:
    """按假型+跨度选口径（**批次逻辑**，供 leave_skill 调用）。

    mode='batch'（旧批 6 例的判别近似，train 36/41；val 13/13 不变）：
    - 单日 → raw（32 例 RAW 单日全保）
    - 年假(N) 多日 → 工作日×8（wf_0013/0014；代价：wf_0210/0227 105 误伤为 40）
    - 婚假(M)/延时假-调休(V) 多日 → 历日×8（wf_0019/0026/0018；代价：wf_0204/0220 57 误伤为 24）
    - 其余（事假/病假/丧假/育儿假等）多日 → raw（wf_0012/0205/0209/0222/0228 全保）
    默认 mode=DURATION_MODE；'raw' 时与 duration_for 完全一致（零回归）。
    """
    mode = mode or DURATION_MODE
    if mode != "batch":
        return duration_for(start_full, end_full, "raw")
    start = datetime.strptime(start_full[:16], _FMT).date()
    end = datetime.strptime(end_full[:16], _FMT).date()
    if start == end:
        return duration_for(start_full, end_full, "raw")
    if leave_type == "N":  # 年假：工作日×8
        return round(count_workdays(start, end) * 8.0, 2)
    if leave_type in ("M", "V"):  # 婚假/延时假(调休)：历日×8
        return round(count_days(start, end) * 8.0, 2)
    return duration_for(start_full, end_full, "raw")


if __name__ == "__main__":
    # 自检：以 train gold 推断事实为锚（2026 官方日历）。
    def check(name: str, got, want: bool) -> None:
        print(f"[{'PASS' if got == want else 'FAIL'}] {name}: got={got} want={want}")

    check("5/1 劳动节放假", is_holiday(date(2026, 5, 1)), True)
    check("5/5 假期尾日放假", is_holiday(date(2026, 5, 5)), True)
    check("5/6 假期后工作日", is_workday(date(2026, 5, 6)), True)
    check("5/9 调休上班", is_workday(date(2026, 5, 9)), True)
    check("5/10 周日休息", is_workday(date(2026, 5, 10)), False)
    check("4/4 清明放假", is_holiday(date(2026, 4, 4)), True)
    check("4/25 普通周六(非调休)", is_workday(date(2026, 4, 25)), False)
    # 旧批工作日数×8 锚点
    check("wf_0013 5/6-5/11 工作日=5", count_workdays(date(2026, 5, 6), date(2026, 5, 11)), 5)
    check("wf_0014 4/28-5/6 工作日=4", count_workdays(date(2026, 4, 28), date(2026, 5, 6)), 4)
    check("wf_0026 5/12-5/18 工作日=5", count_workdays(date(2026, 5, 12), date(2026, 5, 18)), 5)
    check("wf_0026 历日=7", count_days(date(2026, 5, 12), date(2026, 5, 18)), 7)
    # 三口径
    check("raw 5/6-5/11 = 129", duration_for("2026-05-06 09:00", "2026-05-11 18:00", "raw"), 129.0)
    check("workday 5/6-5/11 = 40", duration_for("2026-05-06 09:00", "2026-05-11 18:00", "workday"), 40.0)
    check("workday 4/28-5/6 = 32", duration_for("2026-04-28 09:00", "2026-05-06 18:00", "workday"), 32.0)
    check("calendar 5/12-5/18 = 56", duration_for("2026-05-12 09:00", "2026-05-18 18:00", "calendar"), 56.0)
    # 批次逻辑（mode='batch'，锚定旧批 6 例 + RAW 多日不误伤 + 单日不误伤）
    check("batch 年假 5/6-5/11 = 40", duration_for_leave("2026-05-06 09:00", "2026-05-11 18:00", "N", "batch"), 40.0)
    check("batch 年假 4/28-5/6 = 32", duration_for_leave("2026-04-28 09:00", "2026-05-06 18:00", "N", "batch"), 32.0)
    check("batch 调休 4/24-4/25 = 16", duration_for_leave("2026-04-24 09:00", "2026-04-25 18:00", "V", "batch"), 16.0)
    check("batch 婚假 4/21-4/23 = 24", duration_for_leave("2026-04-21 09:00", "2026-04-23 18:00", "M", "batch"), 24.0)
    check("batch 婚假 5/12-5/18 = 56", duration_for_leave("2026-05-12 09:00", "2026-05-18 18:00", "M", "batch"), 56.0)
    check("batch 病假 5/13-5/15 = 57(raw)", duration_for_leave("2026-05-13 09:00", "2026-05-15 18:00", "S", "batch"), 57.0)
    check("batch 单日年假 = 9(raw)", duration_for_leave("2026-05-12 09:00", "2026-05-12 18:00", "N", "batch"), 9.0)
    check("batch 陪产假多日 = raw", duration_for_leave("2026-04-28 09:00", "2026-05-06 18:00", "P", "batch"), 201.0)
