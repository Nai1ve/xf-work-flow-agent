"""第一批会议/DAG/Projection 修复的定向回归。"""

from __future__ import annotations

from types import SimpleNamespace

from my_agent import MyAgent
from utils.context import CaseContext
from utils.meeting_skill import MeetingOp, MeetingOpPlan


def _unit(text: str, depends_on: list[int] | None = None) -> SimpleNamespace:
    return SimpleNamespace(unit_type="meeting", sub_query=text, depends_on=depends_on or [])


def test_same_meeting_query_cancel_rebook_units_are_merged() -> None:
    units = [
        _unit("查询周四A3-3F-311的会议日程"),
        _unit("取消原会议订单号 SEED-0246-001"),
        _unit("在同一天重订产品发布会"),
    ]
    merged = MyAgent._normalize_meeting_units(units, enabled=True)
    assert len(merged) == 1
    assert "查询周四" in merged[0].sub_query
    assert "取消原会议" in merged[0].sub_query
    assert "重订产品发布会" in merged[0].sub_query


def test_independent_meeting_units_are_not_merged() -> None:
    units = [
        _unit("查第一个会议"),
        _unit("另外订第二个会议室"),
    ]
    assert len(MyAgent._normalize_meeting_units(units, enabled=True)) == 2
    # feature flag 关闭时必须保留识别层原始切分。
    assert MyAgent._normalize_meeting_units(units, enabled=False) is units


def test_explicit_order_room_and_day_bind_to_plan_and_user_facts() -> None:
    context = CaseContext("case", "q", "now", "single_turn")
    plan = MeetingOpPlan(ops=[MeetingOp("cancel", {}), MeetingOp("book", {})])
    MyAgent._bind_explicit_meeting_facts(
        plan,
        "取消订单号 SEED-0246-001，房间 A3-3F-311，周四重订",
        context,
        "task-0",
    )
    assert plan.ops[0].target["order_id"] == "SEED-0246-001"
    assert plan.ops[1].target["room_id"] == "A3-3F-311"
    assert context.facts.unique_value("meeting.order_id", task_id="task-0") == "SEED-0246-001"
    assert context.facts.unique_value("meeting.room_id", task_id="task-0") == "A3-3F-311"
    assert context.facts.unique_value("meeting.day", task_id="task-0") == "周四"


def test_ambiguous_cancel_has_not_cancelled_projection() -> None:
    plan = MeetingOpPlan(ops=[MeetingOp("cancel", {})])
    projected = MyAgent._project_meeting_result(
        {"booking_result": {"status": "blocked", "reason": "need_confirmation"}},
        plan,
    )
    assert projected["booking_result"] == {
        "status": "not_cancelled",
        "reason": "ambiguous_booking",
    }


def test_empty_conditional_cancel_has_no_result_projection() -> None:
    plan = MeetingOpPlan(ops=[MeetingOp("cancel", {"conditional": True})])
    projected = MyAgent._project_meeting_result({}, plan)
    assert projected["booking_result"] == {
        "status": "not_cancelled",
        "reason": "no_result",
    }
