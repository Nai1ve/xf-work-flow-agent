"""轻量纯 Python Task DAG 运行时。

Skill 仍由各域模块实现；本模块负责依赖校验、状态流转、跨任务隔离和异常转译，
避免入口层按域批处理而破坏用户请求顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from .context import CaseContext


@dataclass
class NodeSpec:
    """稳定 Skill 子图中的节点声明。

    ``TaskDag`` 只负责调度 ``DagTask``；NodeSpec 是供规划/审计层公开使用的
    无副作用契约，工具调用仍由各 Skill handler 实现。
    """

    node_id: str
    kind: str = "decide"
    depends_on: list[str] = field(default_factory=list)
    risk: str = "low"
    cost: int = 0


@dataclass
class TaskSpec:
    """跨域任务的公开规格（不含模型生成的工具名或业务 ID）。"""

    task_id: str
    intent: str
    sub_query: str = ""
    dependencies: list[str] = field(default_factory=list)
    order_after: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    skill: str | None = None
    budget: int | None = None


@dataclass
class SkillSpec:
    """稳定 Skill 子图规格。"""

    name: str
    intent: str
    nodes: list[NodeSpec] = field(default_factory=list)
    min_steps: int = 0
    max_steps: int | None = None


@dataclass
class DomainResult:
    """领域结果与证据分离，供 Projection 晚绑定。"""

    domain: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None


@dataclass
class AgentState:
    """公开的 case 运行状态；实例只在一个用例内创建。"""

    case_id: str
    user_query: str = ""
    now_iso: str = ""
    profile: str = ""
    tasks: dict[str, TaskSpec] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    final_answer: dict[str, Any] = field(default_factory=dict)
    budget_remaining: int | None = None


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class NodeOutcome:
    status: NodeStatus
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    retryable: bool = False


@dataclass
class DagTask:
    task_id: str
    unit_type: str
    sub_query: str
    depends_on: list[str] = field(default_factory=list)
    # 新接口：order_after 只控制顺序；depends_on 作为旧接口保留并视为硬依赖。
    order_after: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    handler: Callable[["DagTask", CaseContext], NodeOutcome] | None = None


class TaskDag:
    """稳定拓扑顺序的 DAG；依赖失败时只跳过其后继。"""

    def __init__(self, tasks: Iterable[DagTask] = ()) -> None:
        self.tasks: dict[str, DagTask] = {}
        for task in tasks:
            self.add(task)

    def add(self, task: DagTask) -> None:
        if task.task_id in self.tasks:
            raise ValueError(f"duplicate task id: {task.task_id}")
        self.tasks[task.task_id] = task

    def validate(self) -> None:
        for task in self.tasks.values():
            edges = list(dict.fromkeys([*task.depends_on, *task.order_after, *task.requires]))
            if task.task_id in edges:
                raise ValueError(f"self dependency: {task.task_id}")
            missing = [d for d in edges if d not in self.tasks]
            if missing:
                raise ValueError(f"missing dependency {task.task_id}: {missing}")
        self.topological_order()

    def topological_order(self) -> list[DagTask]:
        self.validate_without_recursion()
        state: dict[str, int] = {}
        result: list[DagTask] = []

        def visit(task_id: str) -> None:
            mark = state.get(task_id, 0)
            if mark == 1:
                raise ValueError(f"cycle detected at {task_id}")
            if mark == 2:
                return
            state[task_id] = 1
            task = self.tasks[task_id]
            for dep in list(dict.fromkeys([*task.depends_on, *task.order_after, *task.requires])):
                visit(dep)
            state[task_id] = 2
            result.append(task)

        for task_id in self.tasks:
            visit(task_id)
        return result

    def validate_without_recursion(self) -> None:
        for task in self.tasks.values():
            edges = list(dict.fromkeys([*task.depends_on, *task.order_after, *task.requires]))
            if task.task_id in edges:
                raise ValueError(f"self dependency: {task.task_id}")
            if any(dep not in self.tasks for dep in edges):
                raise ValueError(f"missing dependency for {task.task_id}")

    def run(self, context: CaseContext) -> dict[str, NodeOutcome]:
        """串行执行；异常转为 FAILED，不逃出入口。"""
        outcomes: dict[str, NodeOutcome] = {}
        try:
            order = self.topological_order()
        except Exception as exc:  # noqa: BLE001
            for task in self.tasks.values():
                outcomes[task.task_id] = NodeOutcome(NodeStatus.FAILED, error=str(exc))
            return outcomes

        for task in order:
            task_ctx = context.tasks.get(task.task_id)
            if task_ctx is not None:
                task_ctx.status = NodeStatus.READY.value
            # 新建任务使用 order_after；depends_on 仍是旧调用方的硬依赖。
            blocked_by = [
                dep for dep in list(dict.fromkeys([*task.depends_on, *task.requires]))
                if outcomes.get(dep, NodeOutcome(NodeStatus.FAILED)).status
                not in {NodeStatus.SUCCEEDED}
            ]
            if blocked_by:
                outcome = NodeOutcome(NodeStatus.SKIPPED, error=f"dependency_not_succeeded:{','.join(blocked_by)}")
                outcomes[task.task_id] = outcome
                if task_ctx is not None:
                    task_ctx.status = outcome.status.value
                    task_ctx.failure_code = outcome.error
                continue
            if task_ctx is not None:
                task_ctx.status = NodeStatus.RUNNING.value
            try:
                outcome = task.handler(task, context) if task.handler else NodeOutcome(NodeStatus.BLOCKED, error="handler_missing")
                if not isinstance(outcome, NodeOutcome):
                    outcome = NodeOutcome(NodeStatus.SUCCEEDED, output=outcome if isinstance(outcome, dict) else {})
            except Exception as exc:  # noqa: BLE001
                outcome = NodeOutcome(NodeStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            outcomes[task.task_id] = outcome
            if task_ctx is not None:
                task_ctx.status = outcome.status.value
                task_ctx.output = outcome.output
                task_ctx.failure_code = outcome.error
        return outcomes


__all__ = [
    "NodeStatus",
    "NodeOutcome",
    "NodeSpec",
    "TaskSpec",
    "SkillSpec",
    "DomainResult",
    "AgentState",
    "DagTask",
    "TaskDag",
]
