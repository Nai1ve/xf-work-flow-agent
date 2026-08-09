"""意图识别（分解 + 路由）单元测试：IntentRecognizer / TaskGraphIR。

覆盖（用户纠正后的架构：识别层只做分解 + 路由，不做参数抽取）：
- 3 类粗粒度单元词表（meeting/leave/budget）与跨域多单元；
- LLM 正常路径（fake gateway）：单单元 / 多单元 depends_on / 单元级 confidence；
- 降级路径：空 / 低置信 / 非法 unit_type / 槽位键 / LLM 不可用 → 规则兜底；
- multi_turn mode 透传；规则兜底关键词边界（请三天假 / 「假如」不误伤）。
"""

from __future__ import annotations

from utils.llm_gateway import FakeBackend, LLMGateway
from utils.understanding import (
    CONFIDENCE_FLOOR,
    UNIT_BUDGET,
    UNIT_LEAVE,
    UNIT_MEETING,
    IntentRecognizer,
    TaskGraphIR,
    TaskUnit,
)

# 固定 now：2026-04-21（周二），用于可复现断言。
NOW = "2026-04-21T09:00:00+08:00"

# 显式测试配置（非真实密钥）。
CFG = {
    "provider": "openai_compatible",
    "base_url": "https://test.example/v1",
    "model": "test-model",
    "api_key": "test-key",
}


def _gateway(*responses: object) -> LLMGateway:
    return LLMGateway(config=CFG, backend=FakeBackend(list(responses)))


class TestIntentRecognizerLlm:
    """LLM 正常路径（fake gateway）。"""

    def test_single_meeting(self) -> None:
        r = IntentRecognizer().analyze(
            "帮我在A1园区订个10人的会议室",
            NOW,
            None,
            _gateway(
                {
                    "task_units": [
                        {"unit_type": "meeting", "sub_query": "帮我在A1园区订个10人的会议室"}
                    ],
                    "confidence": 0.95,
                }
            ),
        )
        assert r.source == "llm"
        assert [u.unit_type for u in r.task_units] == [UNIT_MEETING]
        assert r.confidence == 0.95

    def test_multi_domain_with_depends_on(self) -> None:
        """跨域 → [meeting, leave]，depends_on 排序后 meeting 在前。"""
        r = IntentRecognizer().analyze(
            "订个会议室，然后请三天假",
            NOW,
            None,
            _gateway(
                {
                    "task_units": [
                        {"unit_type": "meeting", "sub_query": "订个会议室"},
                        {"unit_type": "leave", "depends_on": [0], "sub_query": "然后请三天假"},
                    ],
                    "confidence": 0.9,
                }
            ),
        )
        assert [u.unit_type for u in r.ordered_units()] == [UNIT_MEETING, UNIT_LEAVE]
        assert r.task_units[0].sub_query == "订个会议室"

    def test_per_unit_confidence_derived(self) -> None:
        """模型把 confidence 放进单元而非顶层 → 取各单元最大值作为整体置信度。"""
        r = IntentRecognizer().analyze(
            "订个会议室，然后请三天假",
            NOW,
            None,
            _gateway(
                {
                    "task_units": [
                        {"unit_type": "meeting", "sub_query": "订个会议室", "confidence": 0.98},
                        {"unit_type": "leave", "sub_query": "请三天假", "confidence": 0.9},
                    ]
                }
            ),
        )
        assert r.source == "llm"
        assert r.confidence == 0.98

    def test_multi_turn_mode_transparent(self) -> None:
        """multi_turn 只是标记，识别仍按请求内容出单元。"""
        r = IntentRecognizer().analyze(
            "帮我在A2园区订个会议室",
            NOW,
            "multi_turn",
            _gateway(
                {
                    "task_units": [
                        {"unit_type": "meeting", "sub_query": "帮我在A2园区订个会议室"}
                    ],
                    "confidence": 0.99,
                }
            ),
        )
        assert r.mode == "multi_turn"
        assert r.source == "llm"
        assert [u.unit_type for u in r.task_units] == [UNIT_MEETING]


class TestIntentRecognizerFallback:
    """降级路径：一律规则兜底，source="fallback"。"""

    def test_empty_units(self) -> None:
        r = IntentRecognizer().analyze(
            "你好", NOW, None, _gateway({"task_units": [], "confidence": 0.9})
        )
        assert r.source == "fallback"

    def test_low_confidence(self) -> None:
        r = IntentRecognizer().analyze(
            "帮我订个会议室",
            NOW,
            None,
            _gateway({"task_units": [{"unit_type": "meeting"}], "confidence": 0.3}),
        )
        assert r.source == "fallback"

    def test_invalid_unit_type(self) -> None:
        """非法 unit_type 过不了 schema → gateway 兜底 → 规则兜底。"""
        r = IntentRecognizer().analyze(
            "帮我订个会议室",
            NOW,
            None,
            _gateway({"task_units": [{"unit_type": "room"}], "confidence": 0.9}),
        )
        assert r.source == "fallback"

    def test_slot_key_rejected(self) -> None:
        """task_unit 里带槽位键（location）→ additionalProperties 拒绝 → 兜底。"""
        r = IntentRecognizer().analyze(
            "帮我订个会议室",
            NOW,
            None,
            _gateway(
                {"task_units": [{"unit_type": "meeting", "location": "A1"}], "confidence": 0.9}
            ),
        )
        assert r.source == "fallback"

    def test_no_gateway(self) -> None:
        r = IntentRecognizer().analyze("我想请三天年假", NOW, None, None)
        assert r.source == "fallback"
        assert [u.unit_type for u in r.task_units] == [UNIT_LEAVE]

    def test_confidence_floor(self) -> None:
        assert CONFIDENCE_FLOOR == 0.55


class TestRuleFallback:
    """规则兜底关键词分类（降级路径专用）。"""

    def setup_method(self) -> None:
        self.r = IntentRecognizer()

    def test_leave_phrases(self) -> None:
        for query in ("请假", "请三天假", "请两天年假", "休年假", "调休"):
            got = [u.unit_type for u in self.r._fallback_units(query)]
            assert got == [UNIT_LEAVE], query

    def test_budget_phrases(self) -> None:
        for query in ("报销笔记本电脑费用", "申报项目预算", "采购办公设备"):
            got = [u.unit_type for u in self.r._fallback_units(query)]
            assert got == [UNIT_BUDGET], query

    def test_meeting_phrases(self) -> None:
        for query in (
            "帮我订个会议室",
            "查一下这周的会议预订情况",
            "延长半小时",
            "加参会人",
        ):
            got = [u.unit_type for u in self.r._fallback_units(query)]
            assert got == [UNIT_MEETING], query

    def test_cross_domain(self) -> None:
        got = [u.unit_type for u in self.r._fallback_units("订个会议室，然后请三天假")]
        assert got == [UNIT_MEETING, UNIT_LEAVE]
        got = [u.unit_type for u in self.r._fallback_units("订个会议室，另外报销物资")]
        assert got == [UNIT_MEETING, UNIT_BUDGET]

    def test_jia_ru_not_leave(self) -> None:
        """「假如这个时间不行」里的「假」不误判为请假（默认会议兜底）。"""
        got = [u.unit_type for u in self.r._fallback_units("假如这个时间不行，前后半小时看看")]
        assert got == [UNIT_MEETING]

    def test_default_meeting_nonempty(self) -> None:
        """兜底保证非空：默认会议域。"""
        got = [u.unit_type for u in self.r._fallback_units("你好")]
        assert got == [UNIT_MEETING]


class TestTaskGraphIR:
    """TaskGraphIR 依赖拓扑序。"""

    def test_ordered_units_topological(self) -> None:
        ir = TaskGraphIR(
            task_units=[
                TaskUnit(UNIT_MEETING),
                TaskUnit(UNIT_BUDGET, depends_on=[0]),
            ]
        )
        assert [u.unit_type for u in ir.ordered_units()] == [UNIT_MEETING, UNIT_BUDGET]

    def test_ordered_units_ignores_bad_index(self) -> None:
        """越界依赖下标不崩，按列表序输出。"""
        ir = TaskGraphIR(task_units=[TaskUnit(UNIT_MEETING, depends_on=[5])])
        assert [u.unit_type for u in ir.ordered_units()] == [UNIT_MEETING]
