"""会议 Task 引用/依赖修复的 v3 回归。"""

from __future__ import annotations

from types import SimpleNamespace

from my_agent import MyAgent
from utils.context import CaseContext
from utils.dag_runtime import DagTask, NodeOutcome, NodeStatus, TaskDag
from utils.profiles import ProfileConfig


def _meeting_units(*queries: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(unit_type="meeting", sub_query=q) for q in queries]


def test_explicit_order_and_weekday_do_not_require_previous_meeting() -> None:
    units = _meeting_units(
        "查询本周日程",
        "取消原会议，订单号 order-alpha，并在同一天（周四）重订",
    )
    assert MyAgent._infer_task_requires(
        1, units[1].sub_query, units, full_query="查询本周日程；原会议周四重订"
    ) == []
    context = CaseContext("case", "q", "now", "single_turn")
    assert MyAgent._missing_meeting_reference_fact(
        units[1].sub_query, context, ["task-0"]
    ) is None


def test_leave_clause_that_says_that_afternoon_requires_meeting_day() -> None:
    units = [
        SimpleNamespace(unit_type="meeting", sub_query="订会议"),
        SimpleNamespace(unit_type="leave", sub_query="那天下午请假"),
    ]
    assert MyAgent._infer_task_requires(1, units[1].sub_query, units) == ["task-0"]


def test_full_query_reference_does_not_pollute_independent_leave_task() -> None:
    units = [
        SimpleNamespace(unit_type="meeting", sub_query="查询原会议"),
        SimpleNamespace(unit_type="leave", sub_query="周五下午请假"),
    ]
    assert MyAgent._infer_task_requires(
        1, units[1].sub_query, units, full_query="查询原会议，然后周五下午请假"
    ) == []


def test_reference_v3_switch_preserves_legacy_inference_and_gate_when_off() -> None:
    units = [
        SimpleNamespace(unit_type="meeting", sub_query="查会议"),
        SimpleNamespace(unit_type="leave", sub_query="下午请假"),
    ]
    # 关闭时保留旧的 full_query 扫描；打开时只看当前子句。
    assert MyAgent._infer_task_requires(
        1,
        units[1].sub_query,
        units,
        full_query="查原会议，然后下午请假",
        meeting_reference_v3=False,
    ) == ["task-0"]
    assert MyAgent._infer_task_requires(
        1,
        units[1].sub_query,
        units,
        full_query="查原会议，然后下午请假",
        meeting_reference_v3=True,
    ) == []
    context = CaseContext("case", "q", "now", "single_turn")
    assert MyAgent._missing_meeting_reference_fact(
        "取消原会议，订单号=custom-42",
        context,
        ["task-0"],
        meeting_reference_v3=False,
    ) == "meeting.booking_id"
    assert MyAgent._missing_meeting_reference_fact(
        "取消原会议，订单号=custom-42",
        context,
        ["task-0"],
        meeting_reference_v3=True,
    ) is None


def test_explicit_slot_detectors_are_conservative() -> None:
    assert MyAgent._has_explicit_meeting_date("查本周日程") is False
    assert MyAgent._has_explicit_meeting_date("查下周的日程") is False
    assert MyAgent._has_explicit_meeting_date("周四下午") is True
    assert MyAgent._has_explicit_meeting_date("2026年4月23日") is True
    assert MyAgent._has_explicit_order_id("订单号=custom-42") is True
    assert MyAgent._has_explicit_order_id("订单号=123ABC") is True
    assert MyAgent._has_explicit_order_id("预订号 12345") is False
    assert MyAgent._has_explicit_order_id("550e8400-e29b-41d4-a716-446655440000") is True


def test_order_after_does_not_skip_independent_meeting_after_blocked_one() -> None:
    context = CaseContext("case", "q", "now", "single_turn")
    ran: list[str] = []

    def blocked(task: DagTask, _context: CaseContext) -> NodeOutcome:
        ran.append(task.task_id)
        return NodeOutcome(
            NodeStatus.BLOCKED,
            output={"booking_result": {"status": "blocked", "reason": "unresolved_meeting_reference"}},
            error="unresolved_meeting_reference",
        )

    def success(task: DagTask, _context: CaseContext) -> NodeOutcome:
        ran.append(task.task_id)
        return NodeOutcome(NodeStatus.SUCCEEDED, output={"booking_result": {"status": "success"}})

    tasks = [
        DagTask("task-0", "meeting", "查会议", handler=blocked),
        DagTask("task-1", "meeting", "显式订单重订", order_after=["task-0"], handler=success),
    ]
    for task in tasks:
        context.add_task(task.task_id, task.unit_type, task.sub_query)
    outcomes = TaskDag(tasks).run(context)
    assert outcomes["task-0"].status is NodeStatus.BLOCKED
    assert outcomes["task-1"].status is NodeStatus.SUCCEEDED
    assert ran == ["task-0", "task-1"]
    assert outcomes["task-0"].output["booking_result"]["status"] == "blocked"


def test_projected_safe_noop_remains_blocked_inside_dag() -> None:
    """对外是 not_cancelled，内部不能让 requires 任务误消费其事实。"""
    context = CaseContext("case", "q", "now", "single_turn")

    def safe_noop(_task: DagTask, _context: CaseContext) -> NodeOutcome:
        return MyAgent._skill_result_outcome({
            "booking_result": {
                "status": "not_cancelled",
                "reason": "ambiguous_booking",
            }
        })

    def should_not_run(_task: DagTask, _context: CaseContext) -> NodeOutcome:
        raise AssertionError("requires task must not consume a safe no-op")

    tasks = [
        DagTask("task-0", "meeting", "取消但不唯一", handler=safe_noop),
        DagTask(
            "task-1",
            "meeting",
            "使用刚才会议",
            requires=["task-0"],
            handler=should_not_run,
        ),
    ]
    for task in tasks:
        context.add_task(task.task_id, task.unit_type, task.sub_query)

    outcomes = TaskDag(tasks).run(context)
    assert outcomes["task-0"].status is NodeStatus.BLOCKED
    assert outcomes["task-1"].status is NodeStatus.SKIPPED

    # 对外仍保留 not_cancelled 投影，便于官方 evaluator 识别安全 no-op。
    result = MyAgent._project_meeting_result(
        {"booking_result": {"status": "blocked", "reason": "need_confirmation"}},
        SimpleNamespace(ops=[SimpleNamespace(action="cancel")]),
    )
    assert result["booking_result"]["status"] == "not_cancelled"


def test_schedule_query_records_only_unique_booking_facts() -> None:
    context = CaseContext("case", "q", "now", "single_turn")
    context.ledger.set_active_task("task-0")
    context.ledger.add(
        "tool_result",
        "meetingroom.room.schedule",
        {
            "args": {"room_id": "A1-3F-349", "start_date": "2026-04-23", "end_date": "2026-04-23"},
            "result": {"bookings": [{"booking_id": "order-alpha", "day": "2026-04-23"}]},
        },
    )
    MyAgent._record_task_facts(
        context,
        "task-0",
        {"booking_result": {"status": "queried", "room_id": "A1-3F-349", "start_date": "2026-04-23", "end_date": "2026-04-23"}},
    )
    assert context.facts.unique_value("meeting.day", task_id="task-0") == "2026-04-23"
    assert context.facts.unique_value("meeting.booking_id", task_id="task-0") == "order-alpha"

    multi = CaseContext("case2", "q", "now", "single_turn")
    multi.ledger.set_active_task("task-0")
    multi.ledger.add(
        "tool_result",
        "meetingroom.room.schedule",
        {
            "args": {"room_id": "A1-3F-349", "start_date": "2026-04-23", "end_date": "2026-04-23"},
            "result": {"bookings": [{"booking_id": "order-alpha"}, {"booking_id": "order-beta"}]},
        },
    )
    MyAgent._record_task_facts(
        multi,
        "task-0",
        {"booking_result": {"status": "queried", "room_id": "A1-3F-349", "start_date": "2026-04-23", "end_date": "2026-04-23"}},
    )
    assert multi.facts.unique_value("meeting.room_id", task_id="task-0") == "A1-3F-349"
    assert multi.facts.unique_value("meeting.day", task_id="task-0") is None
    assert multi.facts.unique_value("meeting.booking_id", task_id="task-0") is None


def test_dag_execution_keeps_blocked_task_visible_when_later_meeting_succeeds(monkeypatch) -> None:
    """合成执行链：前一独立会议 blocked，后一会议仍执行且成功事实不被覆盖。"""
    import my_agent as module

    class Env:
        def reset(self, _case_id: str) -> dict:
            return {
                "user_query": "先查一个会议室，再独立订另一个会议室",
                "now": "2026-05-12T08:00:00",
                "mode": "single_turn",
                "step_budget": 8,
            }

        def list_tools(self) -> list[dict]:
            return []

        def call_tool(self, _name: str, _args: dict) -> dict:
            raise AssertionError("synthetic executor should own outcomes")

    class IR:
        confidence = 0.9
        source = "synthetic"
        elapsed_s = 0.0
        mode = "single_turn"
        task_units = [
            SimpleNamespace(unit_type="meeting", sub_query="第一个独立会议" , depends_on=[]),
            SimpleNamespace(unit_type="meeting", sub_query="第二个独立会议", depends_on=[]),
        ]

    class Plan:
        source = "synthetic"
        confidence = 1.0
        elapsed_s = 0.0
        ops = [SimpleNamespace(action="query", target={})]

    class Skill:
        last_planner_gateway = None
        last_timings = {"recognize_s": 0.0, "orchestrate_s": 0.0}

        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return IR(), Plan()

        def plan_task(self, *args, **kwargs):
            return Plan()

    class Executor:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def execute_ops(self, _plan):
            Executor.calls += 1
            if Executor.calls == 1:
                return {
                    "booking_result": {
                        "status": "blocked",
                        "reason": "unresolved_meeting_reference",
                    }
                }
            return {
                "booking_result": {
                    "status": "success",
                    "room_id": "room-success",
                }
            }

    monkeypatch.setattr(module, "MeetingSkill", Skill)
    monkeypatch.setattr(module, "MeetingroomExecutor", Executor)
    agent = module.MyAgent(Env())
    agent.profile_config = ProfileConfig(meeting_reference_v3=True)
    result = agent.run("case")
    assert Executor.calls == 2
    assert result["booking_result"]["status"] == "success"
    assert result["booking_result"]["room_id"] == "room-success"
    assert result["task_outcomes"]["task-0"]["booking_result"]["status"] == "blocked"
