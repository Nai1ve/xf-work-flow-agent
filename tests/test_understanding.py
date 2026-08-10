"""理解层单元测试：IntentRouter / TemporalResolver / MeetingConstraintExtractor。

覆盖「会议室 SOP」理解层的三个组件：
- 意图识别：S1 预订 / S2 查询 / S3 取消 / S4 重订 / S5 延长 / S6 参会人 / M 多轮；
- 时间解析：相对日期（今天/明天/下周X/X月X日）、起止时刻（上午/下午/半）、
  逐天搜索区间、多日同会议室表达；
- 约束抽取：园区/楼栋/楼层 → office_address、容量、屏幕、主题、人数、工位偏好。

测试只依赖 submission/utils/understanding.py，不触达官方 env。
"""

from __future__ import annotations

from datetime import date

from utils.understanding import (
    INTENT_BOOK,
    INTENT_CANCEL,
    INTENT_EXTEND,
    INTENT_MULTI_TURN,
    INTENT_PARTICIPANT,
    INTENT_QUERY,
    INTENT_REBOOK,
    INTENT_UNKNOWN,
    QUERY_BOOKING_LIST,
    QUERY_SCHEDULE,
    QUERY_UNBOOKABLE,
    QUERY_WORKSPACE,
    IntentRouter,
    MeetingConstraintExtractor,
    MeetingConstraints,
    TemporalResolver,
    analyze_meeting_query,
)

# 固定 now：2026-04-21（周二），用于可复现的相对时间断言。
NOW = "2026-04-21T09:00:00+08:00"


# ---------------------------------------------------------------------- 意图 --
# 构造符合 Python 文档约定的测试类：每个组件一个类，方法级断言意图判定。


class TestIntentRouter:
    """IntentRouter.route 的各意图判定路径。"""

    def setup_method(self) -> None:
        self.router = IntentRouter()

    def test_book_simple(self) -> None:
        """含「订/预订/找」且无取消/延长/参会人语义 → S1 预订。"""
        assert self.router.route("帮我在A1园区找个10人的会议室订一下") == INTENT_BOOK

    def test_book_with_time(self) -> None:
        """时间 + 地点 + 主题的完整预订请求 → S1 预订。"""
        query = "下周二下午2点到4点在0552_A1订个会议室做项目复盘"
        assert self.router.route(query) == INTENT_BOOK

    def test_query_pure(self) -> None:
        """名词化查询（预订情况/有哪些会议预订/工位在哪里）→ S2 查询。"""
        assert self.router.route("帮我看看这周的会议预订情况") == INTENT_QUERY
        assert self.router.route("帮我查一下A1-3F-349这个会议室本周的预订情况") == INTENT_QUERY
        assert self.router.route("帮我查一下今天我有哪些会议预订") == INTENT_QUERY
        assert self.router.route("我的工位在哪里") == INTENT_QUERY

    def test_query_unbookable(self) -> None:
        """「有哪些不可预订的会议室」→ S2 查询（先于「订」字判定）。"""
        assert self.router.route("有哪些不可预订的会议室") == INTENT_QUERY

    def test_cancel(self) -> None:
        """「取消预订」→ S3 取消。"""
        assert self.router.route("帮我取消这周五10点的会议") == INTENT_CANCEL

    def test_rebook(self) -> None:
        """「取消…重新订/换个大的」→ S4 取消后重订（取消+换的组合优先于纯取消）。"""
        assert self.router.route("取消原来的，换个更大的会议室重新订") == INTENT_REBOOK

    def test_extend(self) -> None:
        """「延长」→ S5 延长。"""
        assert self.router.route("帮我延长一下下午的会议") == INTENT_EXTEND

    def test_participant(self) -> None:
        """「加参会人/移除」→ S6 参会人管理。"""
        assert self.router.route("把张三加到我的会议里") == INTENT_PARTICIPANT

    def test_multi_turn_mode(self) -> None:
        """mode=multi_turn 直接判为 M 多轮（优先级最高）。"""
        assert self.router.route("帮我订个会议室", mode="multi_turn") == INTENT_MULTI_TURN

    def test_unknown(self) -> None:
        """无语义特征 → unknown（安全默认：不执行写操作）。"""
        assert self.router.route("你好") == INTENT_UNKNOWN

    def test_book_beats_query_keyword(self) -> None:
        """「选个空闲的订」这类动词预订优先于「看看/找」的查询语义。"""
        assert self.router.route("看看哪个更空闲，选个空闲的订") == INTENT_BOOK


class TestTemporalResolver:
    """TemporalResolver 的相对时间解析（now 固定为 2026-04-21 周二）。"""

    def setup_method(self) -> None:
        self.resolver = TemporalResolver(NOW)

    def test_today(self) -> None:
        assert self.resolver.resolve_day("今天下午3点") == "2026-04-21"

    def test_tomorrow(self) -> None:
        assert self.resolver.resolve_day("明天") == "2026-04-22"

    def test_day_after_tomorrow(self) -> None:
        assert self.resolver.resolve_day("后天") == "2026-04-23"

    def test_literal_date(self) -> None:
        assert self.resolver.resolve_day("5月11日") == "2026-05-11"

    def test_next_weekday(self) -> None:
        """下周二 = 2026-04-28（本周二 04-21 + 7 天）。"""
        assert self.resolver.resolve_day("下周二") == "2026-04-28"

    def test_this_weekday_after_today(self) -> None:
        """本周五（04-21 之后的周五）→ 2026-04-24。"""
        assert self.resolver.resolve_day("本周五") == "2026-04-24"

    def test_weekday_past_rolls_forward(self) -> None:
        """周一（今天 04-21 是周二，本周一已过去）→ 顺延到下一周一 04-27。"""
        assert self.resolver.resolve_day("周一") == "2026-04-27"

    def test_resolve_time_range_am(self) -> None:
        assert self.resolver.resolve_time_range("上午9点到11点") == ("09:00", "11:00")

    def test_resolve_time_range_pm_inherit(self) -> None:
        """结束未带午别时继承起始午别：下午2点到4点 → 14:00-16:00。"""
        assert self.resolver.resolve_time_range("下午2点到4点") == ("14:00", "16:00")

    def test_resolve_time_range_half(self) -> None:
        assert self.resolver.resolve_time_range("下午2点半到4点") == ("14:30", "16:00")

    def test_resolve_time_range_invalid(self) -> None:
        """非法区间（开始 ≥ 结束）返回 (None, None)。"""
        assert self.resolver.resolve_time_range("下午4点到3点") == (None, None)

    def test_week_span_earliest_next_week(self) -> None:
        """下周最早能订上 → 下周一(04-27)~下周五(05-01)。"""
        assert self.resolver.resolve_week_span("下周最早能订上") == ("2026-04-27", "2026-05-01")

    def test_week_span_this_week(self) -> None:
        """本周最早能订上（今天周二）→ 04-21(今天)~04-24(周五)。"""
        assert self.resolver.resolve_week_span("本周最早能订上") == ("2026-04-21", "2026-04-24")

    def test_week_span_not_earliest(self) -> None:
        """非「最早」表达不触发逐天搜索区间。"""
        assert self.resolver.resolve_week_span("下周二的会议") == (None, None)

    def test_resolve_days_two(self) -> None:
        """周三和周四 → [2026-04-22, 2026-04-23]。"""
        assert self.resolver.resolve_days("周三和周四下午") == ["2026-04-22", "2026-04-23"]

    def test_resolve_days_three(self) -> None:
        """周二、周三、周四 → 三个日期。"""
        days = self.resolver.resolve_days("周二、周三、周四的会议")
        assert days == ["2026-04-21", "2026-04-22", "2026-04-23"]

    def test_resolve_days_single_not_multi(self) -> None:
        """单个星期表达不是多日。"""
        assert self.resolver.resolve_days("下周二") == []

    def test_offset_weekday_cross_month(self) -> None:
        """跨月边界：2026-04-30 为下周四（04-21 周二 → 本周四 04-23 + 7）。"""
        assert self.resolver._offset_weekday("四", weeks=1).isoformat() == "2026-04-30"


class TestMeetingConstraintExtractor:
    """MeetingConstraintExtractor.extract 的地点/容量/设备/主题抽取。"""

    def setup_method(self) -> None:
        self.resolver = TemporalResolver(NOW)

    def test_building_and_floor(self) -> None:
        """A1 园区 + 3楼 → office_address 0552_A1_3F（默认园区小镇）。"""
        c = self._extract("下周二下午2点到4点在A1园区3楼订个会议室")
        assert c.building == "A1"
        assert c.floor == "3F"
        assert c.addresses == ["0552_A1_3F"]

    def test_hefei_campus_explicit(self) -> None:
        """合肥园区显式指定 → campus=0551 且 campus_explicit=True。"""
        c = self._extract("合肥A3园区订个会议室")
        assert c.campus == "0551"
        assert c.campus_explicit is True
        assert c.addresses == ["0551_A3"]

    def test_floor_without_building(self) -> None:
        """小镇一楼（无楼栋）→ 枚举各楼栋的 1F 地址。"""
        c = self._extract("小镇一楼订个会议室")
        assert c.floor == "1F"
        assert c.addresses == ["0552_A1_1F", "0552_A2_1F", "0552_A3_1F", "0552_A4_1F", "0552_A5_1F"]

    def test_capacity_and_attendees(self) -> None:
        """10人以上 → capacity_gte=10 且 attendees=10。"""
        c = self._extract("找个10人以上的会议室")
        assert c.capacity_gte == 10
        assert c.attendees == 10

    def test_screen_true(self) -> None:
        c = self._extract("需要有屏幕的会议室")
        assert c.has_screen is True

    def test_screen_not_needed(self) -> None:
        c = self._extract("不需要屏幕")
        assert c.has_screen is False

    def test_title(self) -> None:
        c = self._extract("主题是项目复盘")
        assert c.title == "项目复盘"

    def test_workspace_hint(self) -> None:
        c = self._extract("帮我订离我工位最近的会议室")
        assert c.workspace_hint is True

    def test_workspace_hint_adjacent(self) -> None:
        """「在他工位附近订」→ workspace_hint（0245「工位附近」）。"""
        c = self._extract("查一下我的工位，然后在工位附近订下周二下午2点到3点的会议室")
        assert c.workspace_hint is True

    def test_book_one_of_multi_days(self) -> None:
        """0223「周三和周四都要空闲，找到后订周三的」→ days 双日 + book_only_day 周三。"""
        c = self._extract(
            "帮我找一个A1园区3楼10人以上带屏幕的会议室，周三和周四下午2点到4点都要空闲，"
            "找到后订周三的，主题是跨天评审"
        )
        # NOW=2026-04-21（周二）→ 周三=04-22、周四=04-23。
        assert c.days == ["2026-04-22", "2026-04-23"]
        assert c.book_only_day == "2026-04-22"
        assert c.start == "14:00"
        assert c.end == "16:00"
        assert c.title == "跨天评审"

    def test_time_flexible(self) -> None:
        c = self._extract("下午3点到5点，如果这个时间不行，前后半小时看看")
        assert c.time_flexible is True

    def test_fallback_building(self) -> None:
        """A1 优先、也可以 A2 → addresses 追加 0552_A2。"""
        c = self._extract("在A1园区订，也可以A2")
        assert c.fallback_building == "A2"
        assert c.addresses == ["0552_A1", "0552_A2"]

    def test_fallback_building_suffix(self) -> None:
        """「不行的话A2也可以」（备选在楼栋后）同样识别为备选楼栋。"""
        c = self._extract("优先A1四楼，不行的话A2也可以")
        assert c.fallback_building == "A2"
        # 备选地址为楼栋级，不带主楼栋的楼层（0023 金标直接查 A2 楼栋）。
        assert c.addresses == ["0552_A1_4F", "0552_A2"]

    def test_no_fallback_single_building(self) -> None:
        """只出现一个楼栋 → 无备选。"""
        c = self._extract("在A1园区订个会议室")
        assert c.fallback_building is None
        assert c.addresses == ["0552_A1"]

    def test_no_fallback_named_rooms_same_building(self) -> None:
        """命名房间 A1-349 / A1-305（同楼栋）不误判为备选。"""
        c = self._extract("看看A1-349和A1-305哪个空闲")
        assert c.fallback_building is None

    def test_named_room(self) -> None:
        """命名房间 A1-3F-349 → named_room（S1s 场景标记）。"""
        c = self._extract("看看A1-3F-349哪天有空")
        assert c.named_room == "A1-3F-349"

    def _extract(self, query: str) -> MeetingConstraints:
        return MeetingConstraintExtractor().extract(query, self.resolver)


class TestAnalyzeMeetingQuery:
    """一站式 analyze_meeting_query 的意图 + 约束集成。"""

    def test_book_with_full_constraints(self) -> None:
        intent, c = analyze_meeting_query(
            "下周二下午2点到4点在0552_A1订个10人的会议室，主题是项目复盘",
            NOW,
        )
        assert intent == INTENT_BOOK
        assert c.day == "2026-04-28"
        assert c.start == "14:00"
        assert c.end == "16:00"
        assert c.addresses == ["0552_A1"]
        assert c.capacity_gte == 10
        assert c.title == "项目复盘"

    def test_multi_turn_forced(self) -> None:
        intent, _ = analyze_meeting_query("帮我订个会议室", NOW, mode="multi_turn")
        assert intent == INTENT_MULTI_TURN

    def test_query_schedule(self) -> None:
        intent, c = analyze_meeting_query("A1-3F-349 这周的日程", NOW)
        assert intent == INTENT_QUERY
        assert c.query_type == QUERY_SCHEDULE
        assert c.schedule_room_id == "A1-3F-349"

    def test_query_booking_list(self) -> None:
        intent, c = analyze_meeting_query("这周的会议预订情况", NOW)
        assert intent == INTENT_QUERY
        assert c.query_type == QUERY_BOOKING_LIST

    def test_query_booking_list_keyword(self) -> None:
        """0242「关键词是项目启动」→ query_keyword 抽取 + 下周一日期解析。"""
        intent, c = analyze_meeting_query("帮我查一下下周一有哪些会议预订，关键词是项目启动", NOW)
        assert intent == INTENT_QUERY
        assert c.query_type == QUERY_BOOKING_LIST
        assert c.day == "2026-04-27"  # NOW=04-21（周二）→ 下周一
        assert c.query_keyword == "项目启动"

    def test_query_unbookable(self) -> None:
        intent, c = analyze_meeting_query("有哪些不可预订的会议室", NOW)
        assert intent == INTENT_QUERY
        assert c.query_type == QUERY_UNBOOKABLE
        assert c.bookable is False

    def test_query_workspace(self) -> None:
        intent, c = analyze_meeting_query("我的工位在哪里", NOW)
        assert intent == INTENT_QUERY
        assert c.query_type == QUERY_WORKSPACE

    def test_cancel_intent(self) -> None:
        intent, _ = analyze_meeting_query("帮我取消明天上午的会议预订", NOW)
        assert intent == INTENT_CANCEL

    def test_cancel_keyword_compound_title(self) -> None:
        """跨域 Fix（zh_0019/mr_0025/zh_0204）：「那个项目复盘会议室」→ 关键词必须是
        完整会议名「项目复盘」，不能被前瞻里的复合词（复盘会）截断成「项目」。"""
        _, c = analyze_meeting_query("帮我取消我下周二下午2点到3点那个项目复盘会议室", NOW)
        assert c.query_keyword == "项目复盘"

    def test_cancel_keyword_compound_title_bare_hui(self) -> None:
        """「那个项目复盘会」→ 完整标题「项目复盘」（裸「会」前瞻兜住 复盘会 型查询）。"""
        _, c = analyze_meeting_query("帮我把那个项目复盘会延长半小时", NOW)
        assert c.query_keyword == "项目复盘"
