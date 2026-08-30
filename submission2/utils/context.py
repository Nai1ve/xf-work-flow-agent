"""单个训练用例内的上下文与证据账本。

所有对象由 ``MyAgent.run`` 每次新建，不使用进程级业务缓存。账本只记录当前
用例实际看到的工具、追问和规则证据，最终投影只能引用这些记录。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .workflow_registry import WorkflowSchemaRegistry


@dataclass
class EvidenceRecord:
    kind: str
    source: str
    value: Any
    task_id: str | None = None
    valid: bool = True
    timestamp: float = field(default_factory=time.monotonic)
    provenance: str = "runtime"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "value": self.value,
            "task_id": self.task_id,
            "valid": self.valid,
            "provenance": self.provenance,
        }


class EvidenceLedger:
    """当前 case 的读证据、写证据、模型建议和规则裁决。"""

    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []
        self.active_task_id: str | None = None

    def set_active_task(self, task_id: str | None) -> None:
        """设置当前执行节点；未显式传 task_id 的证据自动归属该节点。"""
        self.active_task_id = task_id

    def add(
        self,
        kind: str,
        source: str,
        value: Any,
        *,
        task_id: str | None = None,
        valid: bool = True,
        provenance: str = "runtime",
    ) -> EvidenceRecord:
        if task_id is None:
            task_id = self.active_task_id
        record = EvidenceRecord(kind, source, value, task_id, valid, provenance=provenance)
        self.records.append(record)
        return record

    def for_task(self, task_id: str | None) -> list[EvidenceRecord]:
        return [r for r in self.records if r.task_id in {None, task_id}]

    def latest(self, kind: str, task_id: str | None = None) -> EvidenceRecord | None:
        for record in reversed(self.records):
            if record.kind == kind and (task_id is None or record.task_id == task_id):
                return record
        return None

    def values(self, kind: str, task_id: str | None = None) -> list[Any]:
        return [r.value for r in self.for_task(task_id) if r.kind == kind]


@dataclass
class TaskContext:
    task_id: str
    unit_type: str
    sub_query: str
    status: str = "PENDING"
    output: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None


@dataclass
class CaseContext:
    case_id: str
    user_query: str
    now_iso: str
    mode: str | None
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    tasks: dict[str, TaskContext] = field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    schema_registry: WorkflowSchemaRegistry = field(default_factory=WorkflowSchemaRegistry)

    def add_task(self, task_id: str, unit_type: str, sub_query: str) -> TaskContext:
        task = TaskContext(task_id, unit_type, sub_query)
        self.tasks[task_id] = task
        return task

    def record_policy(self, decision: Any) -> None:
        payload = decision.as_dict() if hasattr(decision, "as_dict") else decision
        self.policy_decisions.append(payload)
        self.ledger.add("policy_decision", payload.get("policy_id", "policy") if isinstance(payload, dict) else "policy", payload, provenance="policy")


__all__ = ["EvidenceRecord", "EvidenceLedger", "TaskContext", "CaseContext"]
