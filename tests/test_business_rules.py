"""业务规则组件单元测试：地址归一 + 公司时间计算器。

用户定稿（#41 保留的两类窄例外，均「包含业务规则，不能给模型处理」）：
1. 地址归一：显示名（A1园区 / 小镇A1四楼 / A1_4F / A2）→ 工具契约内部码
   （0552_A1 / 0552_A1_4F）。园区码表是公司数据，模型结构性无法推导；
2. 公司时间计算器：午别+时长（周三下午…连续用3小时）→ 规范起止（14:00-17:00）。
   公司工作时段惯例，不给模型处理。显式「X点到Y点」不触发（模型/规则值保留）。

**不触网**：纯程序逻辑，无 LLM 依赖。
"""

from __future__ import annotations

from utils.business_rules import (
    normalize_addresses,
    normalize_building,
    normalize_campus,
    normalize_floor,
    normalize_office_address,
    resolve_company_time,
)


class TestNormalizeOfficeAddress:
    def test_internal_code_passthrough(self) -> None:
        for addr in ("0552", "0552_A1", "0552_A1_4F", "0551", "0551_A2_3F"):
            assert normalize_office_address(addr) == addr

    def test_display_name_variants(self) -> None:
        # 0021/0023 各显示名形态 → 内部码。
        assert normalize_office_address("A1园区") == "0552_A1"
        assert normalize_office_address("小镇A1四楼") == "0552_A1_4F"
        assert normalize_office_address("A1_4F") == "0552_A1_4F"
        assert normalize_office_address("A2") == "0552_A2"
        assert normalize_office_address("合肥A2三楼") == "0551_A2_3F"
        assert normalize_office_address("小镇A3") == "0552_A3"

    def test_no_building_falls_back_to_campus(self) -> None:
        assert normalize_office_address("小镇") == "0552"
        assert normalize_office_address("A1园区") == "0552_A1"  # building 仍优先

    def test_empty_or_non_string(self) -> None:
        assert normalize_office_address("") == ""
        assert normalize_office_address(None) == ""

    def test_normalize_addresses_ordered_dedup(self) -> None:
        assert normalize_addresses(["A1园区", "A1园区", "小镇A1四楼"]) == ["0552_A1", "0552_A1_4F"]
        assert normalize_addresses(["小镇A1四楼", "A1_4F"]) == ["0552_A1_4F"]  # 去重（同码）
        assert normalize_addresses([]) == []
        assert normalize_addresses(None) == []

    def test_normalize_building_floor_campus(self) -> None:
        assert normalize_building("A2") == "A2"
        assert normalize_building("A1园区") == "A1"
        assert normalize_building(None) is None
        assert normalize_floor("4F") == "4F"
        assert normalize_floor("四楼") == "4F"
        assert normalize_floor(None) is None
        assert normalize_campus("0552") == "0552"
        assert normalize_campus("小镇") == "0552"
        assert normalize_campus("合肥") == "0551"


class TestResolveCompanyTime:
    def test_afternoon_three_hours(self) -> None:
        # 0048 基准：下午 → 14:00 起点，+3h → 17:00。
        assert resolve_company_time("周三下午需要一间A1园区容量12人以上、带屏幕、能连续用3小时的会议室") == (
            "14:00",
            "17:00",
        )

    def test_morning_and_evening(self) -> None:
        assert resolve_company_time("上午开会2小时") == ("09:00", "11:00")
        assert resolve_company_time("晚上讨论1小时") == ("19:00", "20:00")
        assert resolve_company_time("中午休息半小时") == ("12:00", "12:30")

    def test_chinese_numerals_and_half_hours(self) -> None:
        assert resolve_company_time("下午三个小时") == ("14:00", "17:00")
        assert resolve_company_time("下午一个半小时") == ("14:00", "15:30")
        assert resolve_company_time("下午半小时") == ("14:00", "14:30")

    def test_explicit_range_not_translated(self) -> None:
        # 有显式「X点到Y点」→ 计算器不触发（模型/规则值保留）。
        assert resolve_company_time("周三下午2点到3点") is None
        assert resolve_company_time("下午2点至4点，时长3小时") is None

    def test_missing_period_or_duration(self) -> None:
        assert resolve_company_time("周三需要连续用3小时") is None  # 无午别
        assert resolve_company_time("") is None
        assert resolve_company_time(None) is None

    def test_bare_period_default(self) -> None:
        # 裸午别（无时长、无显式时刻）→ 固定 1 小时默认（gold 6 case 一致）。
        assert resolve_company_time("订明天下午的会议室") == ("14:00", "15:00")
        assert resolve_company_time("下午的会议室") == ("14:00", "15:00")
        assert resolve_company_time("订明天上午会议室") == ("10:00", "11:00")
        assert resolve_company_time("上午开会") == ("10:00", "11:00")

    def test_custom_case_from_query(self) -> None:
        # 0021：下午 + 3小时 + 无显式区间。
        assert resolve_company_time("帮我订周三下午在A1园区能连续用3小时的会议室") == ("14:00", "17:00")
