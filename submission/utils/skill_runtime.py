from __future__ import annotations

import json
from typing import Any


class WorkflowSkillRuntime:
    """Persistent execution state for one declarative business-skill DAG."""

    TERMINAL_STATUSES = {"completed", "blocked", "skipped"}

    def __init__(self, skill_id: str, definition: dict[str, Any]):
        self.skill_id = skill_id
        self.definition = json.loads(json.dumps(definition, ensure_ascii=False, default=str))
        self.nodes = [item for item in self.definition.get("nodes") or [] if isinstance(item, dict) and item.get("id")]
        self.statuses = {str(item["id"]): "pending" for item in self.nodes}
        self.blocked_reason = ""
        self.transitions: list[dict[str, Any]] = []
        self.validations: dict[str, dict[str, Any]] = {}
        self.failures: list[dict[str, Any]] = []
        self.retry_counts: dict[str, int] = {}
        self._failure_fingerprints: set[str] = set()
        self.terminal_result: dict[str, Any] = {}

    def sync_completed(self, completed_node_ids: set[str]) -> None:
        for node_id in completed_node_ids:
            self.mark_completed(node_id, source="legacy_sync")

    def mark_completed(self, node_id: str, *, source: str = "validator", evidence_refs: list[str] | None = None) -> None:
        if node_id not in self.statuses or self.statuses[node_id] == "completed":
            return
        previous = self.statuses[node_id]
        self.statuses[node_id] = "completed"
        self.transitions.append(
            {
                "node": node_id,
                "from": previous,
                "to": "completed",
                "source": source,
                "evidence_refs": list(evidence_refs or []),
            }
        )

    def apply_validation(self, node_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if node_id not in self.statuses:
            return {"status": "ignored", "node": node_id}
        status = str(result.get("status") or "pending")
        normalized = {
            "status": status,
            "reason": str(result.get("reason") or ""),
            "evidence_refs": [str(item) for item in result.get("evidence_refs") or [] if item],
            "fingerprint": str(result.get("fingerprint") or ""),
        }
        self.validations[node_id] = normalized
        if status in {"passed", "failed"} and not self._dependencies_completed(node_id):
            normalized["status"] = f"{status}_waiting_dependencies"
            return {"status": "pending", "node": node_id, "validation": normalized["status"]}
        if status == "passed":
            self.mark_completed(node_id, evidence_refs=normalized["evidence_refs"])
            return {"status": "completed", "node": node_id}
        if status == "failed":
            return self.resolve_failure(
                node_id,
                normalized["reason"] or "validation_failed",
                fingerprint=normalized["fingerprint"],
            )
        return {"status": "pending", "node": node_id}

    def _dependencies_completed(self, node_id: str) -> bool:
        node = self.node(node_id) or {}
        return all(self.statuses.get(str(dependency)) == "completed" for dependency in node.get("depends_on") or [])

    def mark_running(self, node_id: str) -> None:
        if self.statuses.get(node_id) == "pending":
            self.statuses[node_id] = "running"
            self.transitions.append({"node": node_id, "from": "pending", "to": "running"})

    def mark_blocked(self, reason: str, node_id: str = "") -> dict[str, Any]:
        if self.blocked_reason:
            return {
                "status": "blocked",
                "node": node_id or self.current_node_id(),
                "reason": self.blocked_reason,
                "action": "block",
                "result": dict(self.terminal_result),
            }
        target = node_id or self.current_node_id()
        if target:
            result = self.resolve_failure(target, reason, force_terminal=True)
            if result.get("status") == "blocked":
                return result
        self.blocked_reason = reason
        self.transitions.append({"node": target, "to": "blocked", "reason": reason})
        return {"status": "blocked", "node": target, "reason": reason, "action": "block"}

    def current_node_id(self) -> str:
        running = next((node_id for node_id, status in self.statuses.items() if status == "running"), "")
        return running or self.next_ready_id()

    def failure_edge(self, node_id: str, reason: str) -> dict[str, Any]:
        node = self.node(node_id) or {}
        edges = node.get("failure_edges") if isinstance(node.get("failure_edges"), dict) else {}
        edge = edges.get(reason) or edges.get("*")
        if isinstance(edge, str):
            return {"action": "block", "reason": edge}
        return dict(edge) if isinstance(edge, dict) else {"action": "block", "reason": reason}

    def resolve_failure(
        self,
        node_id: str,
        reason: str,
        *,
        fingerprint: str = "",
        force_terminal: bool = False,
    ) -> dict[str, Any]:
        fingerprint = fingerprint or f"{node_id}:{reason}:{len(self.failures)}"
        if fingerprint in self._failure_fingerprints:
            return {"status": "duplicate", "node": node_id, "reason": reason}
        self._failure_fingerprints.add(fingerprint)
        edge = self.failure_edge(node_id, reason)
        action = "block" if force_terminal else str(edge.get("action") or "block")
        attempt = int(self.retry_counts.get(node_id) or 0) + 1
        self.retry_counts[node_id] = attempt
        record = {
            "node": node_id,
            "reason": reason,
            "action": action,
            "attempt": attempt,
            "target": str(edge.get("target") or node_id),
            "fingerprint": fingerprint,
        }
        self.failures.append(record)
        if action == "retry":
            max_attempts = max(1, int(edge.get("max_attempts") or 1))
            if attempt <= max_attempts:
                target = str(edge.get("target") or node_id)
                self.reset_from(target)
                self.transitions.append(
                    {"node": node_id, "to": "retry", "reason": reason, "target": target, "attempt": attempt}
                )
                return {"status": "retry", **record}
            exhausted = edge.get("on_exhausted") if isinstance(edge.get("on_exhausted"), dict) else {}
            reason = str(exhausted.get("reason") or edge.get("exhausted_reason") or reason)
            action = str(exhausted.get("action") or "block")
            record["reason"] = reason
            record["action"] = action
        if action == "skip":
            previous = self.statuses.get(node_id, "pending")
            self.statuses[node_id] = "skipped"
            self.transitions.append({"node": node_id, "from": previous, "to": "skipped", "reason": reason})
            return {"status": "skipped", **record}
        self.blocked_reason = str(edge.get("reason") or reason)
        self.terminal_result = dict(edge.get("result")) if isinstance(edge.get("result"), dict) else {}
        if node_id in self.statuses:
            previous = self.statuses[node_id]
            self.statuses[node_id] = "blocked"
            self.transitions.append({"node": node_id, "from": previous, "to": "blocked", "reason": self.blocked_reason})
        return {
            "status": "blocked",
            "node": node_id,
            "reason": self.blocked_reason,
            "action": "block",
            "attempt": attempt,
            "result": dict(self.terminal_result),
        }

    def reset_from(self, node_id: str) -> None:
        affected = {node_id}
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                current = str(node.get("id") or "")
                if current in affected:
                    continue
                if any(str(dependency) in affected for dependency in node.get("depends_on") or []):
                    affected.add(current)
                    changed = True
        for current in affected:
            if current not in self.statuses:
                continue
            previous = self.statuses[current]
            self.statuses[current] = "pending"
            self.validations.pop(current, None)
            if previous != "pending":
                self.transitions.append({"node": current, "from": previous, "to": "pending", "source": "failure_edge"})

    def node(self, node_id: str) -> dict[str, Any] | None:
        return next((item for item in self.nodes if str(item.get("id")) == node_id), None)

    def is_ready(self, node_id: str) -> bool:
        node = self.node(node_id)
        return bool(node is not None and self.statuses.get(node_id) == "pending" and self._dependencies_completed(node_id))

    def ready_nodes(self, phases: set[str] | None = None) -> list[dict[str, Any]]:
        return [
            node
            for node in self.nodes
            if self.is_ready(str(node.get("id"))) and (not phases or str(node.get("phase")) in phases)
        ]

    def next_ready_id(self) -> str:
        ready = self.ready_nodes()
        return str(ready[0].get("id")) if ready else ""

    def remaining_cost(self, phases: set[str] | None = None) -> int:
        return sum(
            int(node.get("cost") or 0)
            for node in self.nodes
            if self.statuses.get(str(node.get("id"))) not in self.TERMINAL_STATUSES
            and (not phases or str(node.get("phase")) in phases)
        )

    def summary(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "statuses": dict(self.statuses),
            "next_ready": [str(item.get("id")) for item in self.ready_nodes()],
            "remaining_cost": self.remaining_cost(),
            "blocked_reason": self.blocked_reason,
            "transition_count": len(self.transitions),
            "validations": dict(self.validations),
            "retry_counts": dict(self.retry_counts),
            "failures": list(self.failures[-10:]),
            "terminal_result": dict(self.terminal_result),
        }
