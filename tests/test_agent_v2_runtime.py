"""纯 V2 运行时的无网络契约测试。

这些测试只覆盖 DAG、证据隔离、运行时 Schema 和会议恢复字段依赖，不读取
Train/Val 的答案，也不调用外部模型；官方 runner 的回放另由 scripts/run_agent.py
负责。
"""

from __future__ import annotations

from utils.context import CaseContext
from utils.dag_runtime import DagTask, NodeOutcome, NodeStatus, TaskDag
from utils.meeting_skill import MeetingOp, MeetingOpPlanner
from utils.workflow_registry import WorkflowSchemaRegistry


def test_recovery_book_inherits_explicit_or_configured_extension_minutes() -> None:
    planner = MeetingOpPlanner()

    explicit = [
        MeetingOp("extend", {"minutes": 45}),
        MeetingOp("cancel", {}),
        MeetingOp("book", {"inherit_title": True}),
    ]
    planner._propagate_recovery_fields(explicit, "延长45分钟，冲突取消后重订")
    assert explicit[-1].target["minutes"] == 45

    configured = [
        MeetingOp("cancel", {"conditional": True}),
        MeetingOp("book", {"inherit_title": True}),
    ]
    planner._propagate_recovery_fields(configured, "已经订了就先试着延长，不行就取消重订")
    assert configured[-1].target["minutes"] == 30


def test_recovery_does_not_overwrite_explicit_book_value() -> None:
    ops = [
        MeetingOp("extend", {"minutes": 30}),
        MeetingOp("cancel", {}),
        MeetingOp("book", {"inherit_title": True, "minutes": 60}),
    ]
    MeetingOpPlanner()._propagate_recovery_fields(ops, "延长30分钟，冲突取消重订")
    assert ops[-1].target["minutes"] == 60


def test_task_dag_blocks_only_dependents_and_keeps_independent_task() -> None:
    context = CaseContext("case", "q", "2026-04-18T10:00:00+08:00", "single_turn")

    def fail(task: DagTask, ctx: CaseContext):
        return NodeOutcome(NodeStatus.BLOCKED, error="blocked")

    def ok(task: DagTask, ctx: CaseContext):
        return NodeOutcome(NodeStatus.SUCCEEDED, output={"task": task.task_id})

    dag = TaskDag(
        [
            DagTask("a", "meeting", "a", handler=fail),
            DagTask("b", "leave", "b", depends_on=["a"], handler=ok),
            DagTask("c", "budget", "c", handler=ok),
        ]
    )
    for task in dag.tasks.values():
        context.add_task(task.task_id, task.unit_type, task.sub_query)
    result = dag.run(context)
    assert result["a"].status is NodeStatus.BLOCKED
    assert result["b"].status is NodeStatus.SKIPPED
    assert result["c"].status is NodeStatus.SUCCEEDED


def test_evidence_ledger_is_task_scoped() -> None:
    context = CaseContext("case", "q", "now", "single_turn")
    context.ledger.set_active_task("task-a")
    context.ledger.add("tool_result", "tool.a", {"ok": True})
    context.ledger.set_active_task("task-b")
    context.ledger.add("tool_result", "tool.b", {"ok": True})
    assert [r.source for r in context.ledger.for_task("task-a")] == ["tool.a"]
    assert [r.source for r in context.ledger.for_task("task-b")] == ["tool.b"]


def test_runtime_workflow_schema_compiles_required_fields_and_dependencies() -> None:
    registry = WorkflowSchemaRegistry()
    spec = registry.ingest(
        {
            "workflow_id": 34747,
            "required_fields": ["project_code", "total_amount"],
            "fields": [
                {"key": "material_category", "required": True, "field_id": 29023},
                {"key": "material_subclass", "depends_on": ["material_category"]},
            ],
            "detail_tables": {"detail_2": {"required_fields": ["unit_price"]}},
        }
    )
    assert spec is not None
    assert set(registry.required(34747)) == {"project_code", "total_amount", "material_category"}
    assert registry.field_id(34747, "material_category") == 29023
    assert set(registry.validate_data(34747, {"project_code": "A"})) == {
        "total_amount",
        "material_category",
    }
