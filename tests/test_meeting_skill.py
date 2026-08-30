"""MeetingSkill / MeetingOpPlanner 单元测试。

覆盖：
- MeetingOpPlanner 规则兜底路径（gateway=None → _rule_plan 映射 12 种 op）；
- MeetingOpPlanner LLM 路径（FakeBackend 注入 → 解析 ops + _fill_gaps 补全）；
- MeetingSkill 顺序流水线（LLM#1 识别单元+sub_query → LLM#2 接收 sub_query
  上下文编排，卡感知后端保证确定性，测试断言 sub_query 未送入 LLM#2）。

**不触网**：所有 LLM 响应经 FakeBackend 注入，config 用显式测试配置。
"""

from __future__ import annotations

import json

import pytest

from utils.llm_gateway import FakeBackend, LLMGateway
from utils.meeting_skill import (
    MeetingOpPlanner,
    MeetingSkill,
)

# 显式测试配置（非真实密钥）。
CFG = {
    "provider": "openai_compatible",
    "base_url": "https://test.example/v1",
    "model": "test-model",
    "api_key": "test-key",
    "llm_budget_s": 35.0,
}

NOW = "2026-04-15T10:00:00"  # 周三 → 「下周二」= 2026-04-21


def _gateway(*responses: object) -> LLMGateway:
    """FakeBackend 注入的 gateway（响应按调用顺序弹出）。"""
    return LLMGateway(config=CFG, backend=FakeBackend(list(responses)))


# ------------------------------------------------------------------ 规则兜底 --

class TestRuleFallback:
    """gateway=None → _rule_plan 规则映射（12 种 op 家族）。"""

    def _plan(self, query: str) -> list[str]:
        planner = MeetingOpPlanner()
        plan = planner.plan(query, NOW, "single_turn", None)
        assert plan.source == "fallback"
        assert plan.confidence == 0.0
        return [op.action for op in plan.ops]

    def test_book(self) -> None:
        assert self._plan("帮我订下周二下午2点到3点10人带屏幕的会议室，主题项目复盘") == ["book"]

    def test_multi_day_slots(self) -> None:
        # 0043 同日多场
        query = "帮我订周三A1园区的会议室，上午9点到11点开需求评审，下午2点到4点开技术方案讨论，需要同一个房间"
        assert self._plan(query) == ["multi_day"]

    def test_earliest(self) -> None:
        # 0226 逐天最早：给出可订日期范围
        query = "下周一到下周四找最早一天下午2点能订上会议室，主题评审"
        assert self._plan(query) == ["earliest"]

    def test_compare_book(self) -> None:
        query = "看看A3-3F-311和A3-3F-312哪个更空闲，订下周四下午2点到4点，主题年终总结"
        assert self._plan(query) == ["compare_book"]

    def test_cancel(self) -> None:
        assert self._plan("我下周二下午那个项目复盘会议室不用了，帮我取消掉") == ["cancel"]

    def test_extend_conditional(self) -> None:
        query = "我下周二下午2点到3点在A1园区有个需求评审会，能多开半小时就延长，后面冲突就别动原会议"
        plan = MeetingOpPlanner().plan(query, NOW, "single_turn", None)
        assert [op.action for op in plan.ops] == ["extend"]
        assert plan.ops[0].target.get("conditional") is True

    def test_extend_plain(self) -> None:
        query = "帮我把我下周二2点到3点的会议延长半小时"
        plan = MeetingOpPlanner().plan(query, NOW, "single_turn", None)
        assert [op.action for op in plan.ops] == ["extend"]
        assert "conditional" not in plan.ops[0].target

    def test_rebook_larger(self) -> None:
        query = "我之前订的会议室太小了，帮我换个大一点的，20人以上"
        plan = MeetingOpPlanner().plan(query, NOW, "single_turn", None)
        assert [op.action for op in plan.ops] == ["rebook"]
        assert plan.ops[0].target.get("larger") is True

    def test_participant_add_dedup(self) -> None:
        query = "把李明加到下周三的项目评审会，如果他已经在我就不用加了"
        plan = MeetingOpPlanner().plan(query, NOW, "single_turn", None)
        assert [op.action for op in plan.ops] == ["participant_add"]
        assert plan.ops[0].target.get("dedup") is True

    def test_participant_remove(self) -> None:
        query = "把李明从下周三的项目评审会里移出去"
        assert self._plan(query) == ["participant_remove"]

    def test_participant_list(self) -> None:
        query = "看看下周三项目评审会有哪些人参加"
        assert self._plan(query) == ["participant_list"]

    def test_query(self) -> None:
        assert self._plan("帮我查一下下周一有哪些会议预订，关键词是项目启动") == ["query"]

    def test_decide(self) -> None:
        query = "下周二下午2点到3点，没订就帮我订，订了就延长半小时，冲突就取消重订"
        assert self._plan(query) == ["decide"]

    def test_unknown_garbage(self) -> None:
        assert self._plan("今天天气怎么样") == []


# --------------------------------------------------------------------- LLM --

class TestPlannerLLM:
    """LLM#2 正常/兜底路径：解析 ops + gap-fill 补全。"""

    def test_llm_parses_ops(self) -> None:
        gateway = _gateway(
            {
                "ops": [
                    {"action": "book", "target": {"day": "2026-04-21", "start": "14:00", "end": "15:00"}},
                ],
                "confidence": 0.95,
            }
        )
        plan = MeetingOpPlanner().plan("帮我订下周二下午2点到3点会议室", NOW, "single_turn", gateway)
        assert plan.source == "llm"
        assert [op.action for op in plan.ops] == ["book"]
        assert plan.confidence == 0.95

    def test_llm_earliest_plus_book_deduped(self) -> None:
        """0040：earliest 后紧跟 book → 确定性丢弃 book（同一会议不二订）。"""
        gateway = _gateway(
            {
                "ops": [
                    {"action": "earliest", "target": {"start": "09:00", "end": "11:00"}},
                    {"action": "book", "target": {"start": "09:00", "end": "11:00"}},
                ],
                "confidence": 0.95,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我在A1园区找一个这周最早能订上的会议室，上午9点到11点，主题是项目启动会",
            NOW, "single_turn", gateway,
        )
        assert plan.source == "llm"
        assert [op.action for op in plan.ops] == ["earliest"]

    def test_llm_multi_day_plus_book_deduped(self) -> None:
        """0223 同类：multi_day（终态订房）后紧跟 book → 丢弃。"""
        gateway = _gateway(
            {
                "ops": [
                    {"action": "multi_day", "target": {"days": ["2026-04-21", "2026-04-22"]}},
                    {"action": "book", "target": {"day": "2026-04-21"}},
                ],
                "confidence": 0.95,
            }
        )
        plan = MeetingOpPlanner().plan("多日校验只订一天", NOW, "single_turn", gateway)
        assert [op.action for op in plan.ops] == ["multi_day"]

    def test_multi_slot_title_prefix_is_repaired_from_user_text(self) -> None:
        """模型把多场主题压成前缀时，恢复用户原文的完整可观测标题。"""
        gateway = _gateway(
            {
                "ops": [
                    {
                        "action": "multi_day",
                        "target": {
                            "slots": [
                                {"start": "09:00", "end": "11:00", "title": "需求"},
                                {"start": "14:00", "end": "16:00", "title": "技术"},
                            ]
                        },
                    }
                ],
                "confidence": 0.95,
            }
        )
        query = (
            "帮我订周三A1园区的会议室，上午9点到11点开需求评审，"
            "下午2点到4点开技术方案讨论，需要同一个房间"
        )
        plan = MeetingOpPlanner().plan(query, NOW, "single_turn", gateway)
        slots = plan.ops[0].target["slots"]
        assert [slot["title"] for slot in slots] == ["需求评审", "技术方案讨论"]

    def test_dedup_terminal_booking_direct(self) -> None:
        """类方法直接测试：book 在终态 op 之前不丢，之后才丢。"""
        from utils.meeting_skill import MeetingOp

        cases = [
            ([MeetingOp("book", {}), MeetingOp("earliest", {})], ["book", "earliest"]),
            ([MeetingOp("earliest", {}), MeetingOp("book", {})], ["earliest"]),
            ([MeetingOp("compare_book", {}), MeetingOp("book", {}), MeetingOp("query", {})],
             ["compare_book", "query"]),
            ([MeetingOp("book", {}), MeetingOp("book", {})], ["book", "book"]),
            ([], []),
        ]
        for raw, expect in cases:
            out = MeetingOpPlanner._dedup_terminal_booking(raw)
            assert [o.action for o in out] == expect

    def test_llm_invalid_action_dropped(self) -> None:
        gateway = _gateway(
            {
                "ops": [
                    {"action": "not_an_op", "target": {}},
                    {"action": "cancel", "target": {"day": "2026-04-21"}},
                ],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan("取消我下周二那个会议", NOW, "single_turn", gateway)
        assert [op.action for op in plan.ops] == ["cancel"]

    def test_llm_low_confidence_falls_back(self) -> None:
        gateway = _gateway(
            {"ops": [{"action": "book", "target": {}}], "confidence": 0.4}
        )
        plan = MeetingOpPlanner().plan("帮我订个会议室", NOW, "single_turn", gateway)
        # 低置信 → 规则兜底（book 规则仍能命中）。
        assert plan.source == "fallback"
        assert plan.ops  # 兜底不为空

    def test_llm_empty_ops_falls_back(self) -> None:
        gateway = _gateway({"ops": [], "confidence": 0.9})
        plan = MeetingOpPlanner().plan("帮我订下周二会议室", NOW, "single_turn", gateway)
        assert plan.source == "fallback"

    def test_llm_forward_rel_shifted_to_bookable_day(self) -> None:
        """类A「明天→04-21」：now=04-18 周六时 明天=04-19 周日 → 顺延到 04-21。

        顺延只对前瞻相对词（明天/后天/大后天）生效；04-20 在 _MEETING_NOBOOK_DATES
        特例内跳过。金标准 04-21 由此可达（room.list A1+A2 本已搜索，缺的是
        gold 的 capacity_gte=10 trace 参数——那是另一个问题）。
        """
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"start": "14:00", "end": "15:00"}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我订明天下午2点到3点的会议室，先A1不行就A2，带屏幕，主题复盘",
            "2026-04-18T10:00:00", "single_turn", gateway,
        )
        t = plan.ops[0].target
        assert t["day"] == "2026-04-21", t
        assert t["addresses"] == ["0552_A1", "0552_A2"], t
        assert t["fallback_building"] == "A2", t

    def test_llm_forward_rel_weekday_not_shifted(self) -> None:
        """明天解析到工作日 → 不顺延（zh_0014 明天=05-13 周三保持原值）。"""
        gateway = _gateway(
            {"ops": [{"action": "book", "target": {}}], "confidence": 0.9}
        )
        plan = MeetingOpPlanner().plan(
            "帮我订明天下午2点到3点的会议室",
            "2026-05-12T10:00:00", "single_turn", gateway,
        )
        assert plan.ops[0].target["day"] == "2026-05-13"

    def test_llm_missing_fields_gap_filled(self) -> None:
        """LLM 漏 day/start/end → _fill_gaps 用规则抽取补全（日期换算）。"""
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"title": "项目复盘", "capacity": 10}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我订下周二下午2点到3点10人会议室，主题项目复盘", NOW, "single_turn", gateway
        )
        t = plan.ops[0].target
        assert t["day"] == "2026-04-21"
        assert t["start"] == "14:00"
        assert t["end"] == "15:00"

    def test_llm_invalid_address_replaced(self) -> None:
        """LLM 给显示名地址（A1园区）→ 程序查表归一为内部码（0552_A1）。"""
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"addresses": ["A1园区"], "day": "2026-04-21"}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我订下周二下午2点到3点A1园区的会议室", NOW, "single_turn", gateway
        )
        assert plan.ops[0].target["addresses"] == ["0552_A1"]

    def test_llm_display_address_normalized(self) -> None:
        """LLM 给显示名地址（小镇A1四楼）→ 查表归一为 0552_A1_4F。"""
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"addresses": ["小镇A1四楼"], "day": "2026-04-21"}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我订下周二下午2点到3点小镇A1四楼的会议室", NOW, "single_turn", gateway
        )
        assert plan.ops[0].target["addresses"] == ["0552_A1_4F"]

    def test_llm_time_translated_by_company_calculator(self) -> None:
        """0048：公司时间计算器翻译「下午…连续用3小时」→ 14:00-17:00。

        用户定稿：午别+时长属公司工作时段惯例，由程序侧计算器翻译，不给模型
        处理——即使 LLM 已给值（12:00-15:00）也按业务规则归一（惯例翻译）。
        """
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"day": "2026-05-06", "start": "12:00", "end": "15:00"}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "周三下午需要一间A1园区容量12人以上、带屏幕、能连续用3小时的会议室，帮我直接订上",
            NOW, "single_turn", gateway,
        )
        t = plan.ops[0].target
        assert t["start"] == "14:00"  # 下午 → 14:00 起点（公司工作时段惯例）
        assert t["end"] == "17:00"  # +3h

    def test_llm_explicit_range_time_preserved(self) -> None:
        """显式「X点到Y点」→ 计算器不触发，模型/规则值保留（#41 不覆盖）。"""
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"day": "2026-04-21", "start": "09:30", "end": "10:30"}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我订下周二上午9点半到10点半的会议室", NOW, "single_turn", gateway
        )
        t = plan.ops[0].target
        assert t["start"] == "09:30"
        assert t["end"] == "10:30"

    def test_llm_provided_address_not_overridden(self) -> None:
        """wf_0006：LLM 给地址 → 规则不再覆盖（#41），保留 LLM 值（可重验）。"""
        gateway = _gateway(
            {
                "ops": [{"action": "book", "target": {"day": "2026-04-21", "start": "14:00", "end": "15:00", "addresses": ["0552_A1"]}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我约下周二下午2点到3点在A2园区8人会议室，主题季度复盘",
            NOW, "single_turn", gateway,
        )
        assert plan.ops[0].target["addresses"] == ["0552_A1"]  # 模型已给，规则不覆盖

    def test_llm_hallucinated_order_id_stripped(self) -> None:
        """跨域 Fix A（zh_0019）：LLM 在 cancel target 编造 order_id（sub_query 无
        SEED-*）→ 剥除，强制执行层走 booking.list 定位（gold 的 list-before-cancel）。"""
        gateway = _gateway(
            {
                "ops": [
                    {
                        "action": "cancel",
                        "target": {"order_id": "SEED-CANCEL-FUZZY-001",
                                   "day": "2026-04-21", "keyword": "项目复盘"},
                    },
                ],
                "confidence": 0.95,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我取消我下周二下午2点到3点那个项目复盘会议室。",
            NOW, "single_turn", gateway,
        )
        assert plan.source == "llm"
        assert "order_id" not in plan.ops[0].target

    def test_llm_order_id_literal_in_context_kept(self) -> None:
        """原文字面出现 order_id（mr_0024/0222/0235）→ 保留直给，不误剥。"""
        gateway = _gateway(
            {
                "ops": [
                    {"action": "cancel",
                     "target": {"order_id": "SEED-CANCEL-SELF-001"}},
                ],
                "confidence": 0.95,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我取消我下周二下午2点到3点的项目复盘会议室，订单号是 SEED-CANCEL-SELF-001。",
            NOW, "single_turn", gateway,
        )
        assert plan.ops[0].target.get("order_id") == "SEED-CANCEL-SELF-001"

    def test_llm_unusable_action_falls_back(self) -> None:
        """0223：LLM 判 compare_book 却无 compare_rooms/named_room → 规则兜底 multi_day。"""
        gateway = _gateway(
            {
                "ops": [{"action": "compare_book", "target": {"day": "2026-05-13", "start": "14:00", "end": "16:00"}}],
                "confidence": 0.93,
            }
        )
        plan = MeetingOpPlanner().plan(
            "帮我找一个A1园区3楼10人以上带屏幕的会议室，周三和周四下午2点到4点都要空闲，找到后订周三的，主题是跨天评审",
            NOW, "single_turn", gateway,
        )
        assert plan.source == "fallback"
        assert [op.action for op in plan.ops] == ["multi_day"]

    def test_llm_extend_conditional_flag(self) -> None:
        """模型漏 conditional → 规则从 query 文本可靠判定并落进 target。"""
        gateway = _gateway(
            {
                "ops": [{"action": "extend", "target": {"day": "2026-04-21", "minutes": 30}}],
                "confidence": 0.9,
            }
        )
        plan = MeetingOpPlanner().plan(
            "我下周二2点有个会，能多开半小时就延长，冲突就别动原会议", NOW, "single_turn", gateway
        )
        assert plan.ops[0].target.get("conditional") is True


# ------------------------------------------------------------ MeetingSkill --

class _CardAwareBackend:
    """按 system 卡片区分 LLM#1 / LLM#2 的测试后端（顺序流水线下卡片确定）。"""

    def __init__(self, intent: dict, plan: dict) -> None:
        self.intent = intent
        self.plan = plan
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], **_: object) -> str:
        self.calls.append(list(messages))
        system = messages[0]["content"]
        if "意图识别器" in system and "会议编排器" not in system:
            return json.dumps(self.intent, ensure_ascii=False)
        if "会议编排器" in system:
            return json.dumps(self.plan, ensure_ascii=False)
        raise AssertionError(f"未知卡片: {system[:40]}")


class TestMeetingSkill:
    def test_sequential_two_calls_with_sub_query_handoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """顺序流水线：LLM#1 识别（带 sub_query）→ LLM#2 只收 sub_query 编排。"""
        intent_resp = {
            "task_units": [
                {
                    "unit_type": "meeting",
                    "sub_query": "帮我订下周二下午2点到3点10人带屏幕的会议室，主题项目复盘",
                    "depends_on": [],
                }
            ],
            "confidence": 0.9,
        }
        plan_resp = {
            "ops": [{"action": "book", "target": {"title": "项目复盘"}}],
            "confidence": 0.9,
        }
        backend = _CardAwareBackend(intent_resp, plan_resp)
        gateway = LLMGateway(config=CFG, backend=backend)
        # run() 内部 `from utils.llm_gateway import LLMGateway` 从源模块取类 → 打源模块。
        monkeypatch.setattr("utils.llm_gateway.LLMGateway", lambda logger=None: gateway)

        skill = MeetingSkill()
        ir, plan = skill.run(
            "帮我订下周二下午2点到3点10人带屏幕的会议室，主题项目复盘",
            NOW,
            "single_turn",
            gateway,
        )
        assert [u.unit_type for u in ir.task_units] == ["meeting"]
        assert ir.task_units[0].sub_query == "帮我订下周二下午2点到3点10人带屏幕的会议室，主题项目复盘"
        assert [op.action for op in plan.ops] == ["book"]
        assert plan.ops[0].target["title"] == "项目复盘"
        assert len(backend.calls) == 2  # 识别 + 编排两次请求都发了

        # LLM#2 的 user payload 只含 sub_query，不含完整 user_query（原文作校验证据）。
        plan_messages = [m for m in backend.calls if "会议编排器" in m[0]["content"]][0]
        payload = json.loads(plan_messages[1]["content"])
        assert "sub_query" in payload
        assert "user_query" not in payload

    def test_sequential_no_gateway_falls_back(self) -> None:
        """顺序流水线但 gateway 不可用 → 识别与编排各自规则兜底，不崩。"""
        skill = MeetingSkill()
        ir, plan = skill.run("帮我订下周二会议室", NOW, "single_turn", None)
        assert ir.source == "fallback"
        assert ir.task_units[0].sub_query == "帮我订下周二会议室"  # 兜底 sub_query=整句
        assert [op.action for op in plan.ops] == ["book"]

    def test_sequential_no_meeting_unit_skips_planner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """纯 leave 请求：识别出 leave 单元 → 编排层空 plan（不发 LLM#2）。"""
        intent_resp = {
            "task_units": [
                {"unit_type": "leave", "sub_query": "我要请2天年假", "depends_on": []}
            ],
            "confidence": 0.9,
        }
        backend = _CardAwareBackend(intent_resp, {"ops": [], "confidence": 0.9})
        gateway = LLMGateway(config=CFG, backend=backend)
        monkeypatch.setattr("utils.llm_gateway.LLMGateway", lambda logger=None: gateway)

        skill = MeetingSkill()
        ir, plan = skill.run("我要请2天年假", NOW, "single_turn", gateway)
        assert [u.unit_type for u in ir.task_units] == ["leave"]
        assert plan.ops == []
        assert len(backend.calls) == 1  # 只有识别，未发编排请求
