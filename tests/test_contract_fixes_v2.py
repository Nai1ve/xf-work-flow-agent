"""V2 契约修复的纯函数回归。"""

from utils.speech_act import parse_speech_act
from utils.context import CaseContext, FactStore
from utils.dag_runtime import DagTask, NodeOutcome, NodeStatus, TaskDag
from utils.understanding import MeetingConstraintExtractor, TemporalResolver, analyze_meeting_query
from utils.meeting_skill import MeetingOpPlanner
from utils.profiles import ProfileConfig
from utils.budget_skill import (
    BudgetDraft,
    BudgetPlanner,
    BudgetRow,
    _ambiguous_total_breakdown,
    _has_explicit_line_amounts,
    _regex_unit_for_material,
)
from utils.llm_gateway import FakeBackend, LLMGateway
from utils.leave_skill import LeaveExecutor
from utils.redaction import redact_text, redact_value
from types import SimpleNamespace


def test_submit_synonyms_are_explicit_submit() -> None:
    for text in ("直接提掉", "提上去", "提品牌广告服务费用", "走审批", "送审"):
        decision = parse_speech_act(text)
        assert decision.selected is True
        assert decision.explicit_submit is True


def test_negative_submit_is_not_a_positive_submit() -> None:
    decision = parse_speech_act("暂不提交")
    assert decision.selected is False
    assert decision.forbid_submit is True
    assert decision.explicit_submit is False
    assert decision.conflict is False


def test_do_not_save_draft_means_submit() -> None:
    decision = parse_speech_act("不要保存草稿")
    assert decision.selected is True
    assert decision.forbid_draft is True


def test_conflicting_speech_is_blockable() -> None:
    decision = parse_speech_act("不提交，但直接提上去")
    assert decision.selected is None
    assert decision.conflict is True


def test_order_after_does_not_block_independent_task() -> None:
    context = CaseContext("case", "q", "now", "single_turn")

    def blocked(task, ctx):
        return NodeOutcome(NodeStatus.BLOCKED, error="no_fact")

    def success(task, ctx):
        return NodeOutcome(NodeStatus.SUCCEEDED, output={"ok": True})

    tasks = [
        DagTask("a", "meeting", "a", handler=blocked),
        DagTask("b", "budget", "b", order_after=["a"], handler=success),
    ]
    for task in tasks:
        context.add_task(task.task_id, task.unit_type, task.sub_query)
    result = TaskDag(tasks).run(context)
    assert result["a"].status is NodeStatus.BLOCKED
    assert result["b"].status is NodeStatus.SUCCEEDED


def test_fact_store_records_supersedes_and_write_sources() -> None:
    facts = FactStore()
    first = facts.upsert("expense.project_code", "A-1", source="RUNTIME_TOOL", task_id="a")
    second = facts.upsert("expense.project_code", "A-2", source="DIALOGUE_REPLY", task_id="a")
    assert second.supersedes == first.fact_id
    assert facts.unique_value("expense.project_code") == "A-2"  # 最新明确回复覆盖旧事实
    inferred = facts.upsert("expense.project_code", "A-3", source="INFERRED", task_id="c")
    assert inferred.can_drive_write is False


def test_next_week_question_is_weekday_range_not_today() -> None:
    intent, constraints = analyze_meeting_query(
        "下周哪天下午有空，帮我订一个会议室", "2026-04-18T10:00:00+08:00", "single_turn"
    )
    assert intent == "book"
    assert constraints.day is None
    assert constraints.week_start == "2026-04-20"
    assert constraints.week_end == "2026-04-24"
    plan = MeetingOpPlanner().plan(
        "下周哪天下午有空，帮我订一个会议室",
        "2026-04-18T10:00:00+08:00",
        "single_turn",
        None,
    )
    assert plan.ops[0].action == "earliest"
    assert plan.ops[0].target["week_start"] == "2026-04-20"


def test_forward_relative_meeting_date_skips_weekend_in_normal_calendar() -> None:
    resolver = TemporalResolver("2026-04-18T10:00:00+08:00")
    assert MeetingOpPlanner._normalize_day_value(
        "明天", resolver, compat_calendar=False
    ) == "2026-04-20"


def test_forward_relative_meeting_date_uses_simulator_compat_profile() -> None:
    resolver = TemporalResolver("2026-04-18T10:00:00+08:00")
    assert MeetingOpPlanner._normalize_day_value(
        "明天", resolver, compat_calendar=True
    ) == "2026-04-21"


def test_schedule_week_keyword_expands_to_weekday_range() -> None:
    plan = MeetingOpPlanner().plan(
        "帮我查 A3-3F-312 会议室下周的日程",
        "2026-05-11T09:00:00+08:00",
        "single_turn",
        None,
    )
    target = plan.ops[0].target
    assert plan.ops[0].action == "query"
    assert target["start_date"] == "2026-05-18"
    assert target["end_date"] == "2026-05-22"


def test_candidate_profile_disables_legacy_budget_templates(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_EXECUTION_PROFILE", "candidate_v2")
    config = ProfileConfig.from_env()
    assert config.profile_name == "candidate_v2"
    assert config.legacy_budget_templates is False
    assert config.strict_runtime_mode is True
    assert config.allow_oa_postcheck(explicit_request=False, multi_domain=True) is False
    assert config.allow_oa_postcheck(explicit_request=True, multi_domain=True) is True


def test_candidate_profile_does_not_apply_event_leave_default(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_EXECUTION_PROFILE", "candidate_v2")
    config = ProfileConfig.from_env()
    from utils.profiles import CompatibilityPolicy

    policy = CompatibilityPolicy(config).decide(
        "submit_or_draft", semantic_context={"event_leave_default": True}
    )
    assert policy.selected_value is False


def test_dependent_meeting_plan_binds_only_unique_case_facts() -> None:
    """跨会议 Task 的“刚订/那天”引用在执行后晚绑定，不猜全局订单。"""
    from my_agent import MyAgent

    context = CaseContext("case", "q", "now", "single_turn")
    context.upsert_fact("meeting.booking_id", "BK-1", source="TASK_OUTPUT", task_id="task-0")
    context.upsert_fact("meeting.day", "2026-04-21", source="TASK_OUTPUT", task_id="task-0")
    context.upsert_fact("meeting.room_id", "A1-3F-305", source="TASK_OUTPUT", task_id="task-0")
    context.upsert_fact("meeting.start", "14:00", source="TASK_OUTPUT", task_id="task-0")
    context.upsert_fact("meeting.end", "15:00", source="TASK_OUTPUT", task_id="task-0")
    plan = SimpleNamespace(ops=[SimpleNamespace(action="participant_add", target={"persons": [{"name": "李明"}]})])

    bound = MyAgent._bind_meeting_reference_facts(plan, "把李明加到刚订的会议", context)
    target = bound.ops[0].target
    assert target["order_id"] == "BK-1"
    assert target["day"] == "2026-04-21"
    assert target["room_id"] == "A1-3F-305"


def test_dependent_meeting_plan_does_not_fill_without_fact() -> None:
    from my_agent import MyAgent

    context = CaseContext("case", "q", "now", "single_turn")
    plan = SimpleNamespace(ops=[SimpleNamespace(action="cancel", target={})])
    bound = MyAgent._bind_meeting_reference_facts(plan, "取消刚订的会议", context)
    assert bound.ops[0].target == {}


def test_dependent_meeting_plan_prefers_required_task_fact() -> None:
    from my_agent import MyAgent

    context = CaseContext("case", "q", "now", "single_turn")
    context.upsert_fact("meeting.booking_id", "BK-A", source="TASK_OUTPUT", task_id="task-0")
    context.upsert_fact("meeting.booking_id", "BK-B", source="TASK_OUTPUT", task_id="task-1")
    plan = SimpleNamespace(ops=[SimpleNamespace(action="cancel", target={})])
    bound = MyAgent._bind_meeting_reference_facts(
        plan, "取消刚订的会议", context, dependencies=["task-1"]
    )
    assert bound.ops[0].target["order_id"] == "BK-B"


def test_explicit_multi_line_amounts_protect_legacy_memory() -> None:
    text = "视频 2 条每条 1.5 万，发布会 1 场 4 万"
    rows = [
        BudgetRow(material_name="视频", quantity="2", unit_price=""),
        BudgetRow(material_name="发布会", quantity="1", unit_price=""),
    ]
    assert _regex_unit_for_material(text, "视频") == 15000
    assert _regex_unit_for_material(text, "发布会") == 40000
    assert _has_explicit_line_amounts(text, rows) is True


def test_merged_service_row_with_total_is_blocked_but_compound_goods_are_not() -> None:
    """只有总额时，合并的服务概念不能用模型猜测单价落库。"""
    assert _ambiguous_total_breakdown(
        BudgetDraft(rows=[BudgetRow("短片与专题设计")]),
        "区域营销联合路演项目要做短片和专题设计，总预算3万元",
        explicit_line_amounts=False,
        total_explicit=30000.0,
    ) is True
    assert _ambiguous_total_breakdown(
        BudgetDraft(rows=[BudgetRow("易拉宝与展架")]),
        "渠道活动现场易拉宝和展架物料，总预算1.2万元",
        explicit_line_amounts=False,
        total_explicit=12000.0,
    ) is False


def test_budget_planner_never_accepts_model_invented_project_code() -> None:
    """code_hint 只能来自用户原文，模型返回的任意编码不能驱动写入。"""
    gateway = LLMGateway(
        config={
            "provider": "openai_compatible",
            "base_url": "https://test.example/v1",
            "model": "test-model",
            "api_key": "test-key",
            "llm_budget_s": 35,
        },
        backend=FakeBackend(
            [
                {"project": {"search_term": "品牌升级", "code_hint": "A-999999999"}, "confidence": 0.9},
                {"category_hint": "品牌广告服务", "detail_rows": [], "confidence": 0.9},
            ]
        ),
    )
    draft = BudgetPlanner().plan("项目是品牌升级，预算2万元", "2026-04-18T10:00:00+08:00", "single_turn", gateway)
    assert draft.code_hint == ""


def test_dependent_meeting_reference_gate_requires_runtime_fact() -> None:
    from my_agent import MyAgent

    context = CaseContext("case", "q", "now", "single_turn")
    assert MyAgent._missing_meeting_reference_fact(
        "取消刚订的会议", context, ["task-0"]
    ) == "meeting.booking_id"
    context.upsert_fact("meeting.booking_id", "BK-1", source="TASK_OUTPUT", task_id="task-0")
    assert MyAgent._missing_meeting_reference_fact(
        "取消刚订的会议", context, ["task-0"]
    ) == "meeting.day"
    context.upsert_fact("meeting.day", "2026-04-21", source="TASK_OUTPUT", task_id="task-0")
    assert MyAgent._missing_meeting_reference_fact(
        "取消刚订的会议", context, ["task-0"]
    ) is None


def test_meeting_reference_without_declared_predecessor_is_blocked() -> None:
    """模型漏报 requires 时，显式指代也不能回退到第一条会议。"""
    from my_agent import MyAgent

    context = CaseContext("case", "q", "now", "single_turn")
    assert MyAgent._missing_meeting_reference_fact(
        "取消刚订的会议", context, []
    ) == "meeting.booking_id"
    assert MyAgent._missing_meeting_reference_fact(
        "安排在那天同一时间", context, []
    ) == "meeting.day"
    assert MyAgent._missing_meeting_reference_fact(
        "查询这个会议室下周的日程", context, []
    ) is None


def test_diagnostic_redaction_keeps_last_four_only() -> None:
    text = redact_text("联系人 13812345678，下载 https://example.test/file?a=secret&token=x")
    assert "13812345678" not in text
    assert "*******5678" in text
    assert "token=" not in text
    assert redact_value({"phone": "18600001234"})["phone"] == "*******1234"


def test_leave_next_week_weekday_uses_unique_meeting_anchor() -> None:
    context = CaseContext("case", "q", "now", "single_turn")
    context.upsert_fact("meeting.day", "2026-04-21", source="TASK_OUTPUT", task_id="task-0")
    executor = LeaveExecutor.__new__(LeaveExecutor)
    executor._context = context
    result = executor._resolve_schedule(
        "下周二下午3点到6点的事假",
        "明天开会，再请下周二的事假",
        "2026-04-18T10:00:00+08:00",
    )
    assert result == [("2026-04-28 15:00", "2026-04-28 18:00")]
