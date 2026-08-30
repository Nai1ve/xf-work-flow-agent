"""单个训练用例内的上下文与证据账本。

所有对象由 ``MyAgent.run`` 每次新建，不使用进程级业务缓存。账本只记录当前
用例实际看到的工具、追问和规则证据，最终投影只能引用这些记录。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .workflow_registry import WorkflowSchemaRegistry


WRITE_FACT_SOURCES = frozenset({
    "USER_EXPLICIT",
    "DIALOGUE_REPLY",
    "RUNTIME_TOOL",
    "TASK_OUTPUT",
})


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
class ResolvedFact:
    """当前 case 内已经解析的事实；不跨 case 共享。"""

    key: str
    value: Any
    source: str
    task_id: str | None = None
    confidence: float = 1.0
    shareable: bool = True
    evidence_ids: list[str] = field(default_factory=list)
    supersedes: str | None = None
    fact_id: str = ""
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def can_drive_write(self) -> bool:
        return self.source in WRITE_FACT_SOURCES

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "task_id": self.task_id,
            "confidence": self.confidence,
            "shareable": self.shareable,
            "evidence_ids": list(self.evidence_ids),
            "supersedes": self.supersedes,
        }


class FactStore:
    """用例内事实账本。

    同一槽位的新明确事实不会删除旧记录，而是通过 ``supersedes`` 串起来；查询
    默认只返回最新有效值。只有显式来源可以驱动写入，INFERRED 仅供排序。
    """

    def __init__(self) -> None:
        self._facts: list[ResolvedFact] = []
        self._counter = 0

    def upsert(
        self,
        key: str,
        value: Any,
        *,
        source: str,
        task_id: str | None = None,
        confidence: float = 1.0,
        shareable: bool = True,
        evidence_ids: list[str] | None = None,
    ) -> ResolvedFact:
        normalized_source = str(source or "").upper()
        self._counter += 1
        previous = self.latest(key, task_id=task_id, shareable_only=False)
        fact = ResolvedFact(
            key=str(key),
            value=value,
            source=normalized_source,
            task_id=task_id,
            confidence=max(0.0, min(float(confidence), 1.0)),
            shareable=bool(shareable),
            evidence_ids=list(evidence_ids or []),
            supersedes=previous.fact_id if previous is not None else None,
            fact_id=f"fact-{self._counter}",
        )
        self._facts.append(fact)
        return fact

    def all(self, *, task_id: str | None = None) -> list[ResolvedFact]:
        return [f for f in self._facts if task_id is None or f.task_id == task_id]

    def latest(
        self,
        key: str,
        *,
        task_id: str | None = None,
        shareable_only: bool = False,
    ) -> ResolvedFact | None:
        for fact in reversed(self._facts):
            if fact.key != key:
                continue
            if task_id is not None and fact.task_id != task_id:
                continue
            if shareable_only and not fact.shareable:
                continue
            return fact
        return None

    def candidates(
        self,
        key: str,
        *,
        task_id: str | None = None,
        shareable_only: bool = True,
    ) -> list[ResolvedFact]:
        values: list[ResolvedFact] = []
        superseded_ids = {fact.supersedes for fact in self._facts if fact.supersedes}
        seen: set[str] = set()
        for fact in reversed(self._facts):
            if fact.key != key or (task_id is not None and fact.task_id != task_id):
                continue
            if fact.fact_id in superseded_ids:
                continue
            if shareable_only and not fact.shareable:
                continue
            marker = repr(fact.value)
            if marker in seen:
                continue
            seen.add(marker)
            values.append(fact)
        return list(reversed(values))

    def unique_value(self, key: str, *, task_id: str | None = None) -> Any:
        candidates = self.candidates(key, task_id=task_id, shareable_only=True)
        if len(candidates) == 1 and candidates[0].can_drive_write:
            return candidates[0].value
        return None

    def as_dict(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self._facts]


@dataclass
class TaskContext:
    task_id: str
    unit_type: str
    sub_query: str
    status: str = "PENDING"
    output: dict[str, Any] = field(default_factory=dict)
    failure_code: str | None = None
    order_after: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)


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
    facts: FactStore = field(default_factory=FactStore)
    run_id: str | None = None
    package_version: str = ""

    def add_task(
        self,
        task_id: str,
        unit_type: str,
        sub_query: str,
        *,
        order_after: list[str] | None = None,
        requires: list[str] | None = None,
    ) -> TaskContext:
        task = TaskContext(
            task_id,
            unit_type,
            sub_query,
            order_after=list(order_after or []),
            requires=list(requires or []),
        )
        self.tasks[task_id] = task
        return task

    def record_policy(self, decision: Any) -> None:
        payload = decision.as_dict() if hasattr(decision, "as_dict") else decision
        self.policy_decisions.append(payload)
        self.ledger.add("policy_decision", payload.get("policy_id", "policy") if isinstance(payload, dict) else "policy", payload, provenance="policy")

    def upsert_fact(self, key: str, value: Any, *, source: str, task_id: str | None = None,
                    confidence: float = 1.0, shareable: bool = True,
                    evidence_ids: list[str] | None = None) -> ResolvedFact:
        fact = self.facts.upsert(
            key,
            value,
            source=source,
            task_id=task_id,
            confidence=confidence,
            shareable=shareable,
            evidence_ids=evidence_ids,
        )
        self.ledger.add(
            "fact_upsert",
            key,
            fact.as_dict(),
            task_id=task_id,
            provenance="fact_store",
        )
        return fact


__all__ = [
    "WRITE_FACT_SOURCES",
    "EvidenceRecord",
    "EvidenceLedger",
    "ResolvedFact",
    "FactStore",
    "TaskContext",
    "CaseContext",
]
