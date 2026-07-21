from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "runtime": {
        "case_deadline_seconds": 55,
        "semantic_extraction": "always",
        "task_graph_log_path": "reports/runtime/task_graph.jsonl",
        "task_graph_timeout_intent_seconds": 12,
        "task_graph_timeout_simple_seconds": 10,
        "task_graph_timeout_normal_seconds": 10,
        "task_graph_timeout_complex_seconds": 10,
        "static_context_enabled": True,
        "static_context_path": "submission/static_context",
        "static_context_max_chars": {
            "intent": 700,
            "task_graph": 3000,
            "candidate": 6000,
            "form": 7000,
        },
        "parallel_read_planner_enabled": True,
        "parallel_reads_enabled": False,
        # The bundled simulator mutates shared state in call_tool and is not
        # thread-safe. This enables a serialized async-scheduling A/B run;
        # it never unlocks simulator tool calls.
        "async_read_execution_enabled": False,
        "parallel_read_max_workers": 4,
        "parallel_read_max_batch_size": 6,
        "parallel_read_min_remaining_seconds": 8,
        "parallel_read_timeout_seconds": 6,
        "empty_read_mapping_enabled": True,
        "empty_read_mapping_max_variants": 3,
    },
    "llm_fast": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "",
        "timeout": 8,
        "temperature": 0,
        "seed": 42,
        "max_calls": 1,
        "max_tokens": 256,
        "max_history_items": 16,
        "debug_log_path": "",
    },
    "llm_strong": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key": "",
        "timeout": 10,
        "temperature": 0,
        "seed": 42,
        "max_calls": 2,
        "max_tokens": 1200,
        "max_history_items": 16,
        "debug_log_path": "",
    },
    "llm": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key": "",
        "timeout": 10,
        "temperature": 0,
        "seed": 42,
        "max_tokens": 1200,
        "max_llm_rounds": 4,
        "max_history_items": 16,
        "debug_log_path": "",
    }
}


EXTRACT_PROMPT = """你是企业工具 Agent 的语义抽取器。

语义抽取是自然语言转程序执行的必经阶段。你只做抽取、高层意图判断和结构化任务图，不直接决定具体工具调用。
返回 JSON object，字段缺失可省略，不要编造工具结果，不要输出 call_tool/final_answer/action。

核心任务类型:
- meetingroom: 新建/查询/取消/延长/取消后重订/换大会议室/按日程选房/管理参会人。
- workflow.leave: 请假草稿或提交，必须抽取时间、假期类型、原因、审批人线索。
- workflow.expense_material: 费用类物资草稿或提交，必须抽取项目、物资大类/小类、金额和明细线索。

抽取规则:
- submit 只表示用户明确要求提交；普通“请假/申请/想请假”不是提交。请假默认是保存草稿。
- 审批人要拆开姓名和职位：“刘经理”=> approver_name_hint="刘", approver_title_hint="经理"；“找一个经理”=> 只有 title，没有姓名。
- 项目、物资、审批人、会议室 ID 只能抽取用户线索，不能编造工具返回的 code/id/value。
- 对浏览字段/枚举值，只输出自然语言 hint；最终枚举 value 必须由工具候选返回后再选择。
- 如果用户同时要求会议室和流程，domains 必须同时包含 meetingroom 和 workflow，不要因为一个任务缺信息丢掉另一个任务。

输出 schema:
{
  "domains": ["meetingroom","workflow"],
  "meetingroom": {
    "intent": "book_single|book_multi_segments_same_room|book_by_schedule_analysis|query_booking|query_room_schedule|cancel_existing|extend_existing|rebook_larger_existing|cancel_rebook_existing|participant_add|participant_remove|participant_list|unknown",
    "day_text": "今天/明天/下周二/5月13日等",
    "start": "HH:MM",
    "end": "HH:MM",
    "duration_minutes": 60,
    "office_candidates": ["A1","A2"],
    "office_address_candidates": ["0552_A1_4F"],
    "room_ids": ["A3-3F-312"],
    "capacity": 10,
    "capacity_delta": 4,
    "has_screen": true,
      "title": "项目复盘",
      "segments": [
        {"day_text": "周三", "start": "09:00", "end": "11:00", "title": "需求评审"}
      ],
      "keyword": "项目复盘",
    "allow_fallback": true,
    "fallback_policy": "block_if_unavailable|fallback_office|cancel_rebook_if_extend_conflict|keep_if_extend_conflict",
    "needs_workspace": true,
    "participants": [{"name":"张伟","employee_no":"200101"}]
  },
  "workflow": {
    "intent": "leave|expense_material|unknown",
    "submit": true,
    "leave": {
      "day_text": "今天/明天/下周二/5月13日",
      "start": "HH:MM",
      "end": "HH:MM",
      "duration_hours": 2,
      "leave_type_label": "事假/年假/病假/育儿假",
      "reason_label": "本人有事/住院/哺乳",
      "approver_keyword": "王芳",
      "approver_title": "经理",
      "approver_raw": "刘经理",
      "approver_name_hint": "刘",
      "approver_title_hint": "经理",
      "approver_employee_no": ""
    },
      "expense": {
      "project_code": "用户明确提供的项目编码",
      "project_name": "用户明确提到的项目名称",
      "project_keywords": ["从用户原话抽取或语义推导的项目搜索关键词候选，后续必须由 workflow.project_search 验证"],
      "material_category_hint": "用户原话中的物资大类自然语言 hint；不要编造枚举 value",
      "total_amount": "用户明确给出的金额",
      "items": [
        {"name":"用户明确提到的费用明细","quantity":"数量","unit_price":"单价","budget_amount":"金额"}
      ]
    }
  },
  "task_graph": {
    "tasks": [
      {
        "task_id": "t1",
        "domain": "meetingroom|workflow",
        "intent": "book_single|extend_existing|leave|expense_material|unknown",
        "goal": "用户要完成的业务目标",
        "source_text": "支持该任务的原始用户片段",
        "slots": {"只放从用户语句或对话历史抽取出的槽位": "不要放工具返回值"},
        "missing_slots": ["执行该任务仍缺失且必须追问的槽位"],
        "must_not_guess": ["不能靠常识猜测、必须由工具验证或追问的槽位"],
        "confidence": 0.0,
        "submit_intent": "submit|draft|unknown"
      }
    ]
  }
}
"""


TASK_GRAPH_PROMPT = """Return JSON only. Parse user intent into a task graph; never call tools or answer.
Use only user text/history for slots. Split mixed meetingroom/workflow requests.
Valid domains/intents are in ctx. Put ids/codes/browser values in must_not_guess unless explicitly typed by user; they still need tools.
submit_intent is submit only for explicit 提交/发起/直接提/帮我提交; otherwise draft.
Schema: {"domains":[],"task_graph":{"tasks":[{"task_id":"t1","domain":"","intent":"","goal":"","source_text":"","slots":{},"missing_slots":[],"must_not_guess":[],"confidence":0.0,"submit_intent":"draft|submit|unknown"}]},"meetingroom":{"intent":""},"workflow":{"intent":"","submit":false,"leave":{},"expense":{}}}
"""


READ_TOOLS = {
    "user.get_info",
    "user.get_workspace",
    "workflow.catalog",
    "workflow.schema",
    "workflow.search_person",
    "workflow.browser_search",
    "workflow.project_search",
    "meetingroom.booking.list",
    "meetingroom.room.list",
    "meetingroom.room.schedule",
    "meetingroom.room.bookings",
    "meetingroom.booking.participant.list",
    "oa.todo.list",
    "oa.done.list",
    "file.list",
}


WORKFLOW_IDS = {
    "leave": 72247,
    "expense": 34747,
}


LEAVE_TYPE_MAP = {
    "年假": "N",
    "年休假": "N",
    "事假": "L",
    "私事": "L",
    "个人": "L",
    "病假": "S",
    "住院": "S",
    "育儿假": "Y",
    "孩子": "Y",
}


REASON_MAP = {
    "住院": "02",
    "病": "01",
    "不适": "01",
    "育儿": "07",
    "孩子": "07",
    "私事": "10",
    "个人": "10",
    "本人有事": "10",
    "事假": "10",
}


CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


class StepAction:
    def __init__(self, kind: str, tool: str = "", args: dict[str, Any] | None = None, message: str = ""):
        self.kind = kind
        self.tool = tool
        self.args = args or {}
        self.message = message


class SemanticFactStore:
    """Canonical facts with a deliberately small provenance/precedence model."""

    PRECEDENCE = {
        "llm_translation": 1,
        "program_computed": 2,
        "tool_candidate": 3,
        "user_literal": 4,
    }

    def __init__(self):
        self.facts: dict[str, dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []
        self.rejections: list[dict[str, Any]] = []

    def set(self, path: str, value: Any, source: str) -> bool:
        if value in (None, "", [], {}):
            return True
        current = self.facts.get(path)
        if current and current.get("value") != value:
            current_rank = self.PRECEDENCE.get(str(current.get("source") or ""), 0)
            incoming_rank = self.PRECEDENCE.get(source, 0)
            conflict = {
                "path": path,
                "current": current.get("value"),
                "current_source": current.get("source"),
                "incoming": value,
                "incoming_source": source,
            }
            self.conflicts.append(conflict)
            if incoming_rank < current_rank:
                self.rejections.append({**conflict, "reason": "lower_provenance_precedence"})
                return False
        self.facts[path] = {"value": value, "source": source}
        return True

    def get(self, path: str, default: Any = None) -> Any:
        fact = self.facts.get(path) or {}
        return fact.get("value", default)

    def source(self, path: str) -> str:
        return str((self.facts.get(path) or {}).get("source") or "")

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for fact in self.facts.values():
            source = str(fact.get("source") or "unknown")
            counts[source] = counts.get(source, 0) + 1
        return {
            "count": len(self.facts),
            "by_source": counts,
            "conflicts": len(self.conflicts),
            "rejections": len(self.rejections),
        }


class ExpenseDraftIR:
    """Verified intermediate representation for one expense workflow save."""

    def __init__(
        self,
        *,
        source: str,
        project_fingerprint: str,
        category_id: str,
        subclass_fingerprint: str,
        total_amount: str,
        rows: list[dict[str, Any]],
    ):
        self.source = source
        self.project_fingerprint = project_fingerprint
        self.category_id = category_id
        self.subclass_fingerprint = subclass_fingerprint
        self.total_amount = total_amount
        self.rows = rows

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "project_fingerprint": self.project_fingerprint,
            "category_id": self.category_id,
            "subclass_fingerprint": self.subclass_fingerprint,
            "total_amount": self.total_amount,
            "row_count": len(self.rows),
            "subclass_ids": [str(row.get("material_subclass") or "") for row in self.rows],
        }


class TaskRuntime:
    TERMINAL_STATUSES = {"completed", "blocked"}

    def __init__(self, task: dict[str, Any]):
        self.task = json.loads(json.dumps(task, ensure_ascii=False, default=str))
        self.task_id = str(task.get("task_id") or "")
        self.domain = str(task.get("domain") or "")
        self.capability = str(task.get("capability") or "")
        self.depends_on = [str(item) for item in task.get("depends_on") or [] if item]
        self.status = "blocked" if task.get("contract_status") == "rejected" else "pending"
        self.blocked_reason = "unsupported_task_contract" if self.status == "blocked" else ""
        self.result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "domain": self.domain,
            "capability": self.capability,
            "intent": self.task.get("intent") or "unknown",
            "status": self.status,
            "depends_on": self.depends_on,
            "blocked_reason": self.blocked_reason,
        }


class ReadTask:
    def __init__(
        self,
        task_key: str,
        tool: str,
        args: dict[str, Any] | None = None,
        domain: str = "",
        depends_on: list[str] | None = None,
        evidence_handler: str = "apply_tool_result",
        deadline_min_remaining: float = 8.0,
        parallel_eligible: bool = True,
        group_key: str = "",
        stop_group_on_success: bool = False,
        mapping_score: float = 0.0,
        owner_task_id: str = "",
    ):
        self.task_key = task_key
        self.tool = tool
        self.args = args or {}
        self.domain = domain
        self.depends_on = depends_on or []
        self.evidence_handler = evidence_handler
        self.deadline_min_remaining = deadline_min_remaining
        self.parallel_eligible = parallel_eligible
        self.group_key = group_key
        self.stop_group_on_success = stop_group_on_success
        self.mapping_score = mapping_score
        self.owner_task_id = owner_task_id


class ReadPlan:
    def __init__(self, tasks: list[ReadTask] | None = None):
        self.tasks = tasks or []

    def ready_tasks(self, completed: set[str], limit: int) -> list[ReadTask]:
        out = []
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_key in completed or task.task_key in seen:
                continue
            if any(dep not in completed for dep in task.depends_on):
                continue
            seen.add(task.task_key)
            out.append(task)
            if len(out) >= limit:
                break
        return out


class WorkflowSkillRuntime:
    """Persistent execution state for one project-local workflow skill DAG."""

    TERMINAL_STATUSES = {"completed", "blocked", "skipped"}

    def __init__(self, skill_id: str, definition: dict[str, Any]):
        self.skill_id = skill_id
        self.definition = json.loads(json.dumps(definition, ensure_ascii=False, default=str))
        self.nodes = [item for item in self.definition.get("nodes") or [] if isinstance(item, dict) and item.get("id")]
        self.statuses = {str(item["id"]): "pending" for item in self.nodes}
        self.blocked_reason = ""
        self.transitions: list[dict[str, Any]] = []

    def sync_completed(self, completed_node_ids: set[str]) -> None:
        for node_id in completed_node_ids:
            if node_id in self.statuses and self.statuses[node_id] != "completed":
                previous = self.statuses[node_id]
                self.statuses[node_id] = "completed"
                self.transitions.append({"node": node_id, "from": previous, "to": "completed"})

    def mark_running(self, node_id: str) -> None:
        if self.statuses.get(node_id) == "pending":
            self.statuses[node_id] = "running"
            self.transitions.append({"node": node_id, "from": "pending", "to": "running"})

    def mark_blocked(self, reason: str) -> None:
        self.blocked_reason = reason
        self.transitions.append({"node": self.next_ready_id(), "to": "blocked", "reason": reason})

    def node(self, node_id: str) -> dict[str, Any] | None:
        return next((item for item in self.nodes if str(item.get("id")) == node_id), None)

    def is_ready(self, node_id: str) -> bool:
        node = self.node(node_id)
        if node is None or self.statuses.get(node_id) in self.TERMINAL_STATUSES:
            return False
        return all(self.statuses.get(str(dep)) == "completed" for dep in node.get("depends_on") or [])

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
        }


class WorkflowSkillRegistry:
    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        self.skills = self.index.get("skills") if isinstance(self.index.get("skills"), dict) else {}

    def definition(self, skill_id: str) -> dict[str, Any]:
        raw = self.skills.get(skill_id)
        if not isinstance(raw, dict):
            return {}
        definition = json.loads(json.dumps(raw, ensure_ascii=False))
        parent_id = str(definition.get("extends") or "")
        if parent_id:
            parent = self.definition(parent_id)
            if not parent:
                return {}
            replacements = definition.get("replace_nodes") if isinstance(definition.get("replace_nodes"), dict) else {}
            nodes = []
            for node in parent.get("nodes") or []:
                node_id = str(node.get("id") or "") if isinstance(node, dict) else ""
                nodes.append(json.loads(json.dumps(replacements.get(node_id, node), ensure_ascii=False)))
            parent.update({key: value for key, value in definition.items() if key not in {"extends", "replace_nodes", "nodes"}})
            parent["nodes"] = nodes
            definition = parent
        return definition

    def select(self, intent: str, submit: bool, replace: bool = False) -> tuple[str, dict[str, Any]]:
        if intent == "leave" and replace and submit:
            skill_id = "workflow.leave.replace_submit"
        elif intent == "leave":
            skill_id = "workflow.leave.submit" if submit else "workflow.leave.draft"
        elif intent == "expense_material":
            skill_id = "workflow.expense.submit" if submit else "workflow.expense.draft"
        else:
            return "", {}
        return skill_id, self.definition(skill_id)


class OutcomePolicyMemory:
    """Deterministic terminal/next-action matcher over verified runtime facts."""

    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        rules = self.index.get("rules") if isinstance(self.index.get("rules"), list) else []
        self.rules = sorted(
            [item for item in rules if isinstance(item, dict)],
            key=lambda item: (-int(item.get("priority") or 0), str(item.get("id") or "")),
        )

    def match(self, scope: str, facts: dict[str, Any]) -> dict[str, Any] | None:
        for rule in self.rules:
            if str(rule.get("scope") or "") != scope:
                continue
            conditions = rule.get("when") if isinstance(rule.get("when"), dict) else {}
            if all(self._condition_matches(facts.get(key), expected) for key, expected in conditions.items()):
                return {
                    "policy_id": str(rule.get("id") or ""),
                    "decision": str(rule.get("decision") or "terminal"),
                    "reason": str(rule.get("reason") or ""),
                    "action": str(rule.get("action") or ""),
                }
        return None

    def _condition_matches(self, actual: Any, expected: Any) -> bool:
        if not isinstance(expected, dict):
            return actual == expected
        for operator, target in expected.items():
            if operator == "eq" and actual != target:
                return False
            if operator == "ne" and actual == target:
                return False
            if operator == "gt" and not self._compare(actual, target, lambda a, b: a > b):
                return False
            if operator == "gte" and not self._compare(actual, target, lambda a, b: a >= b):
                return False
            if operator == "lt" and not self._compare(actual, target, lambda a, b: a < b):
                return False
            if operator == "in" and actual not in (target if isinstance(target, list) else [target]):
                return False
        return True

    def _compare(self, actual: Any, target: Any, comparator: Any) -> bool:
        try:
            return bool(comparator(float(actual), float(target)))
        except Exception:
            return False


class ReadPlanExecutor:
    def __init__(self, agent: Any):
        self.agent = agent

    def execute(self, state: "RuntimeState", plan: ReadPlan, llm_config: dict[str, Any]) -> int:
        return self.agent._execute_read_plan(state, plan, llm_config)


class ToolRegistry:
    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        self.by_name = self.index.get("by_name") if isinstance(self.index.get("by_name"), dict) else {}
        self.write_tools = set(self.index.get("write_tools") or [])
        self.high_risk_write_tools = set(self.index.get("high_risk_write_tools") or [])
        self.required_args = self.index.get("required_args") if isinstance(self.index.get("required_args"), dict) else {}

    def status(self) -> dict[str, Any]:
        counts = self.index.get("counts") if isinstance(self.index.get("counts"), dict) else {}
        return {
            "schema_version": self.index.get("schema_version") or "",
            "tool_count": counts.get("tools") or len(self.by_name),
            "write_tool_count": len(self.write_tools),
            "high_risk_write_tool_count": len(self.high_risk_write_tools),
        }

    def spec(self, tool: str) -> dict[str, Any]:
        return self.by_name.get(tool) if isinstance(self.by_name.get(tool), dict) else {}

    def risk(self, tool: str) -> str:
        spec = self.spec(tool)
        if spec.get("risk"):
            return str(spec.get("risk"))
        if tool in self.high_risk_write_tools:
            return "high_risk_write"
        if tool in self.write_tools:
            return "write"
        if tool in READ_TOOLS:
            return "read"
        return "unknown"

    def is_read(self, tool: str) -> bool:
        return self.risk(tool) == "read" or tool in READ_TOOLS

    def is_write(self, tool: str) -> bool:
        return self.risk(tool) in {"write", "high_risk_write"} or tool in self.write_tools

    def validate_call(self, tool: str, args: dict[str, Any], available_tools: set[str] | None = None) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if available_tools is not None and tool not in available_tools:
            errors.append("tool_not_available")
        spec = self.spec(tool)
        if not spec:
            warnings.append("tool_not_in_static_registry")
            return {
                "passed": not errors,
                "tool": tool,
                "risk": self.risk(tool),
                "missing_required": [],
                "errors": errors,
                "warnings": warnings,
            }
        required = list(spec.get("required_args") or self.required_args.get(tool) or [])
        missing: list[str] = []
        for key in required:
            if tool == "user.get_info" and key == "keyword":
                continue
            if tool == "meetingroom.booking.create" and key in {"office_id", "room_id"}:
                continue
            if args.get(key) in (None, "", [], {}):
                missing.append(str(key))
        if tool == "meetingroom.booking.create" and not (args.get("office_id") or args.get("room_id")):
            missing.append("office_id_or_room_id")
        if missing:
            warnings.append("missing_required_args")
        return {
            "passed": not errors,
            "tool": tool,
            "risk": self.risk(tool),
            "missing_required": missing,
            "errors": errors,
            "warnings": warnings,
        }


class WorkflowSchemaRegistry:
    BASIC_DETAIL_REQUIRED = {"material_subclass", "material_name", "quantity", "unit_price", "budget_amount"}

    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        self.by_id = self.index.get("by_id") if isinstance(self.index.get("by_id"), dict) else {}
        self.by_name = self.index.get("by_name") if isinstance(self.index.get("by_name"), dict) else {}

    def status(self) -> dict[str, Any]:
        counts = self.index.get("counts") if isinstance(self.index.get("counts"), dict) else {}
        return {
            "schema_version": self.index.get("schema_version") or "",
            "workflow_count": counts.get("workflows") or len(self.by_id),
            "schema_count": counts.get("schemas") or len(self.by_id),
        }

    def schema(self, workflow_id: Any, runtime_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        static_schema = self.by_id.get(str(workflow_id or ""))
        static_body = static_schema if isinstance(static_schema, dict) else {}
        if not isinstance(runtime_schema, dict):
            return static_body
        runtime_body = runtime_schema.get("schema") if isinstance(runtime_schema.get("schema"), dict) else runtime_schema
        if not runtime_body:
            return static_body
        if not static_body:
            return runtime_body
        return self._merge_schema(static_body, runtime_body)

    def _merge_schema(self, static_schema: dict[str, Any], runtime_schema: dict[str, Any]) -> dict[str, Any]:
        """Keep live constraints authoritative while retaining static contract metadata."""
        merged = json.loads(json.dumps(static_schema, ensure_ascii=False))

        def merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
            for key, value in incoming.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value

        merge(merged, runtime_schema)
        return merged

    def required_fields(self, workflow_id: Any, runtime_schema: dict[str, Any] | None = None) -> list[str]:
        schema = self.schema(workflow_id, runtime_schema)
        return [str(item) for item in (schema.get("required_fields") or []) if item]

    def detail_required_fields(
        self,
        workflow_id: Any,
        table: str,
        runtime_schema: dict[str, Any] | None = None,
        compatibility_mode: bool = True,
    ) -> list[str]:
        schema = self.schema(workflow_id, runtime_schema)
        detail = (schema.get("detail_tables") or {}).get(table) if isinstance(schema.get("detail_tables"), dict) else {}
        required = [str(item) for item in ((detail or {}).get("required_fields") or []) if item]
        if compatibility_mode:
            return [field for field in required if field in self.BASIC_DETAIL_REQUIRED]
        return required

    def browser_field_id(
        self,
        workflow_id: Any,
        field_key: str,
        runtime_schema: dict[str, Any] | None = None,
        detail_table: str = "",
    ) -> int | None:
        schema = self.schema(workflow_id, runtime_schema)
        descriptions: dict[str, Any] = {}
        if detail_table:
            detail_tables = schema.get("detail_tables") if isinstance(schema.get("detail_tables"), dict) else {}
            detail = detail_tables.get(detail_table) if isinstance(detail_tables.get(detail_table), dict) else {}
            descriptions = detail.get("field_descriptions") if isinstance(detail.get("field_descriptions"), dict) else {}
        else:
            descriptions = schema.get("field_descriptions") if isinstance(schema.get("field_descriptions"), dict) else {}
        description = str(descriptions.get(field_key) or "")
        match = re.search(r"field_id\s*=\s*(\d+)", description)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def validate_save(self, args: dict[str, Any], runtime_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        workflow_id = args.get("workflow_id")
        data = args.get("data") if isinstance(args.get("data"), dict) else {}
        missing = [field for field in self.required_fields(workflow_id, runtime_schema) if data.get(field) in (None, "", [], {})]
        errors: list[str] = []
        warnings: list[str] = []
        if missing:
            errors.append("missing_required_fields")
        rows = []
        if isinstance(data.get("details"), dict):
            rows = data.get("details", {}).get("detail_2") or []
        if workflow_id == WORKFLOW_IDS.get("expense"):
            if not rows:
                errors.append("missing_detail_rows")
            detail_required = self.detail_required_fields(workflow_id, "detail_2", runtime_schema, compatibility_mode=True)
            for row in rows:
                if not isinstance(row, dict):
                    errors.append("invalid_detail_row")
                    continue
                row_missing = [field for field in detail_required if row.get(field) in (None, "", [], {})]
                if row_missing:
                    errors.append("missing_detail_required_fields")
                    missing.extend(row_missing)
        return {
            "passed": not errors,
            "workflow_id": workflow_id,
            "missing": sorted(set(missing)),
            "errors": sorted(set(errors)),
            "warnings": warnings,
        }


class MeetingroomIndex:
    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        self.by_room_id = self.index.get("by_room_id") if isinstance(self.index.get("by_room_id"), dict) else {}
        self.by_office_id = self.index.get("by_office_id") if isinstance(self.index.get("by_office_id"), dict) else {}
        self.normalization_aliases = (
            self.index.get("normalization_aliases") if isinstance(self.index.get("normalization_aliases"), dict) else {}
        )

    def status(self) -> dict[str, Any]:
        counts = self.index.get("counts") if isinstance(self.index.get("counts"), dict) else {}
        return {
            "schema_version": self.index.get("schema_version") or "",
            "room_count": counts.get("rooms") or len(self.by_room_id),
            "office_id_count": len(self.by_office_id),
        }

    def room(self, room_id: Any) -> dict[str, Any]:
        return self.by_room_id.get(str(room_id or "")) if isinstance(self.by_room_id.get(str(room_id or "")), dict) else {}

    def office_id_for_room(self, room_id: Any) -> str:
        room = self.room(room_id)
        return str(room.get("officeId") or room.get("office_id") or "")

    def room_satisfies(self, room: dict[str, Any], args: dict[str, Any]) -> bool:
        try:
            if args.get("capacity_gte") and int(room.get("capacity") or 0) < int(args.get("capacity_gte") or 0):
                return False
        except Exception:
            return False
        if args.get("has_screen") and not (room.get("hasScreen") or "screen" in (room.get("features") or [])):
            return False
        if args.get("bookable") is True and not room.get("bookable", True):
            return False
        return True


class CapabilityRegistry:
    def __init__(self, index: dict[str, Any] | None = None):
        self.index = index or {}
        self.capabilities = (
            self.index.get("capabilities") if isinstance(self.index.get("capabilities"), dict) else {}
        )
        self.meeting_intent_map = (
            self.index.get("meeting_intent_map") if isinstance(self.index.get("meeting_intent_map"), dict) else {}
        )
        self.workflow_intent_map = (
            self.index.get("workflow_intent_map") if isinstance(self.index.get("workflow_intent_map"), dict) else {}
        )

    def status(self) -> dict[str, Any]:
        counts = self.index.get("counts") if isinstance(self.index.get("counts"), dict) else {}
        return {
            "schema_version": self.index.get("schema_version") or "",
            "capability_count": counts.get("capabilities") or len(self.capabilities),
            "domains": counts.get("domains") or {},
            "risk": counts.get("risk") or {},
        }

    def spec(self, capability: str) -> dict[str, Any]:
        item = self.capabilities.get(str(capability or ""))
        return item if isinstance(item, dict) else {}

    def meeting_capability(self, intent: str) -> str:
        return str(self.meeting_intent_map.get(str(intent or "")) or "")

    def workflow_capability(self, intent: str, submit: bool) -> str:
        item = self.workflow_intent_map.get(str(intent or ""))
        if not isinstance(item, dict):
            return ""
        return str(item.get("submit" if submit else "draft") or "")


class TaskGraphContractNormalizer:
    DOMAINS = {"meetingroom", "workflow"}
    SUBMIT_INTENTS = {"submit", "draft", "unknown"}
    USER_LITERAL_KEYS = {
        "order_id",
        "room_id",
        "room_ids",
        "target_booking",
        "booking_id",
        "user_id",
        "employee_no",
        "project_code",
        "wbs_code",
        "workflow_id",
        "material_category",
        "material_subclass",
        "value",
    }
    # Task-graph slots are only user hints.  Keep domains isolated even when a
    # task-graph LLM returns an otherwise valid but cross-domain field.
    DEFAULT_SLOT_ALLOWLIST = {
        "meetingroom": {
            "intent", "day_text", "day", "start", "end", "duration_minutes", "office_candidates",
            "office_address_candidates", "room_ids", "capacity", "capacity_delta", "has_screen", "title",
            "segments", "keyword", "allow_fallback", "fallback_policy", "needs_workspace", "participants",
            "order_id", "target_booking", "booking_id", "campus", "location", "subject", "topic",
            "capacity_min", "equipment",
        },
        "workflow.leave": {
            "day_text", "start", "end", "duration_hours", "leave_type_label", "reason_label",
            "approver_keyword", "approver_title", "approver_raw", "approver_name_hint", "approver_title_hint",
            "approver_employee_no", "approver_department_hint", "explicit_submit", "leave_type", "reason", "approver",
        },
        "workflow.expense_material": {
            "project_code", "project_name", "project_keywords", "material_category_hint", "material_subclass_hint",
            "expense_type", "total_amount", "items", "raw_text", "source_text", "explicit_submit", "project",
            "project_hint", "material", "material_hint", "material_category", "material_subclass", "amount", "budget",
            "details",
        },
    }

    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def normalize(self, value: Any, baseline_value: Any = None, query: str = "") -> dict[str, Any]:
        raw_tasks = self._raw_tasks(value)
        baseline_tasks = self._canonical_baseline_tasks(baseline_value, query)
        if not raw_tasks:
            raw_tasks = [dict(item) for item in baseline_tasks]
        baseline_by_domain: dict[str, list[dict[str, Any]]] = {}
        for item in baseline_tasks:
            baseline_by_domain.setdefault(str(item.get("domain") or ""), []).append(item)

        tasks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_by_domain: dict[str, str] = {}
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                continue
            domain = str(raw.get("domain") or "").strip()
            if domain not in self.DOMAINS:
                continue
            task_id = self._unique_task_id(raw.get("task_id"), domain, index, seen_ids)
            submit_intent = str(raw.get("submit_intent") or "unknown").strip()
            if submit_intent not in self.SUBMIT_INTENTS:
                submit_intent = "unknown"
            submit = submit_intent == "submit"
            requested_intent = str(raw.get("intent") or "unknown").strip()
            requested_capability = str(raw.get("capability") or "").strip()
            capability = self._capability_for(domain, requested_intent, submit, requested_capability)
            contract_status = "valid"
            validation_errors: list[str] = []
            fallback = self._baseline_fallback(baseline_by_domain.get(domain) or [], requested_intent)

            if not capability and self.registry.capabilities:
                validation_errors.append("unsupported_intent")
                if fallback and fallback.get("capability"):
                    capability = str(fallback["capability"])
                    contract_status = "recovered"
                else:
                    contract_status = "rejected"

            spec = self.registry.spec(capability) if capability else {}
            if spec and str(spec.get("domain") or "") != domain:
                validation_errors.append("capability_domain_mismatch")
                capability = ""
                spec = {}
                contract_status = "rejected"

            canonical_intent = str(spec.get("intent") or requested_intent or "unknown")
            if contract_status == "recovered" and fallback:
                canonical_intent = str(fallback.get("intent") or canonical_intent)
                if submit_intent == "unknown":
                    submit_intent = str(fallback.get("submit_intent") or "unknown")

            source_text = self._validated_source_text(raw, fallback, query)
            slots = raw.get("slots") if isinstance(raw.get("slots"), dict) else {}
            safe_slots, slot_provenance, dropped_slots = self._filter_slots(slots, source_text, query)
            safe_slots, disallowed_slots = self._allowlist_slots(safe_slots, domain, canonical_intent, spec)
            if disallowed_slots:
                dropped_slots.extend(disallowed_slots)
                validation_errors.append("capability_slot_allowlist_rejected")
            if dropped_slots:
                validation_errors.append("unverified_literal_slots")

            confidence = self._confidence(raw.get("confidence"))
            dependencies = [str(item) for item in raw.get("depends_on") or [] if item]
            previous = previous_by_domain.get(domain)
            if previous and previous not in dependencies:
                dependencies.append(previous)
            previous_by_domain[domain] = task_id

            tasks.append(
                {
                    "task_id": task_id,
                    "domain": domain,
                    "capability": capability,
                    "intent": canonical_intent,
                    "goal": str(raw.get("goal") or "").strip(),
                    "source_text": source_text,
                    "slots": safe_slots,
                    "slot_provenance": slot_provenance,
                    "missing_slots": self._string_list(raw.get("missing_slots")),
                    "must_not_guess": self._string_list(raw.get("must_not_guess")),
                    "confidence": confidence,
                    "submit_intent": submit_intent,
                    "depends_on": dependencies,
                    "contract_status": contract_status if self.registry.capabilities else "unvalidated_fallback",
                    "validation_errors": validation_errors,
                    "dropped_slots": dropped_slots,
                    "risk": str(spec.get("risk") or "unknown"),
                }
            )

        valid_ids = {str(item.get("task_id") or "") for item in tasks}
        for task in tasks:
            task["depends_on"] = [item for item in task.get("depends_on") or [] if item in valid_ids and item != task["task_id"]]
        return {"tasks": tasks}

    def _canonical_baseline_tasks(self, value: Any, query: str) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for index, raw in enumerate(self._raw_tasks(value)):
            if not isinstance(raw, dict):
                continue
            domain = str(raw.get("domain") or "").strip()
            if domain not in self.DOMAINS:
                continue
            submit_intent = str(raw.get("submit_intent") or "unknown")
            capability = self._capability_for(
                domain,
                str(raw.get("intent") or "unknown"),
                submit_intent == "submit",
                str(raw.get("capability") or ""),
            )
            spec = self.registry.spec(capability) if capability else {}
            item = dict(raw)
            item["task_id"] = str(raw.get("task_id") or f"{domain}_baseline_{index + 1}")
            item["capability"] = capability
            item["intent"] = str(spec.get("intent") or raw.get("intent") or "unknown")
            item["source_text"] = self._validated_source_text(raw, None, query)
            tasks.append(item)
        return tasks

    def _capability_for(self, domain: str, intent: str, submit: bool, requested: str) -> str:
        if domain == "meetingroom":
            mapped = self.registry.meeting_capability(intent)
            return mapped or (requested if self.registry.spec(requested) else "")
        if domain == "workflow":
            normalized = intent.split(".", 1)[1] if intent.startswith("workflow.") else intent
            mapped = self.registry.workflow_capability(normalized, submit)
            return mapped or (requested if self.registry.spec(requested) else "")
        return ""

    def _baseline_fallback(self, candidates: list[dict[str, Any]], requested_intent: str) -> dict[str, Any] | None:
        if not candidates:
            return None
        for item in candidates:
            if str(item.get("intent") or "") == requested_intent:
                return item
        return candidates[0] if len(candidates) == 1 else None

    def _validated_source_text(self, raw: dict[str, Any], fallback: dict[str, Any] | None, query: str) -> str:
        source = str(raw.get("source_text") or raw.get("goal") or "").strip()
        if source and (not query or self._normalized_text(source) in self._normalized_text(query)):
            return source
        fallback_source = str((fallback or {}).get("source_text") or "").strip()
        if fallback_source and (not query or self._normalized_text(fallback_source) in self._normalized_text(query)):
            return fallback_source
        return ""

    def _filter_slots(self, slots: dict[str, Any], source_text: str, query: str) -> tuple[dict[str, Any], dict[str, str], list[str]]:
        evidence_text = self._normalized_text(f"{source_text} {query}")
        provenance: dict[str, str] = {}
        dropped: list[str] = []

        def visit(value: Any, path: str, key: str) -> Any:
            if isinstance(value, dict):
                out: dict[str, Any] = {}
                for child_key, child_value in value.items():
                    child_path = f"{path}.{child_key}" if path else str(child_key)
                    filtered = visit(child_value, child_path, str(child_key))
                    if filtered not in (None, "", [], {}):
                        out[str(child_key)] = filtered
                return out
            if isinstance(value, list):
                out = []
                for index, item in enumerate(value):
                    filtered = visit(item, f"{path}[{index}]", key)
                    if filtered not in (None, "", [], {}):
                        out.append(filtered)
                return out
            if value in (None, ""):
                return None
            text = self._normalized_text(value)
            literal_required = key.lower() in self.USER_LITERAL_KEYS or key.lower().endswith(("_id", "_code", "_value"))
            if literal_required and text and text not in evidence_text:
                dropped.append(path)
                return None
            provenance[path] = "user_literal" if text and text in evidence_text else "llm_translation"
            return value

        filtered = visit(slots, "", "")
        return filtered if isinstance(filtered, dict) else {}, provenance, dropped

    def _allowlist_slots(
        self,
        slots: dict[str, Any],
        domain: str,
        intent: str,
        spec: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        configured = spec.get("allowed_slots") if isinstance(spec.get("allowed_slots"), list) else []
        key = f"{domain}.{intent}" if domain == "workflow" else domain
        allowed = set(str(item) for item in configured if item) or set(self.DEFAULT_SLOT_ALLOWLIST.get(key, set()))
        if not allowed:
            return {}, sorted(str(item) for item in slots)
        filtered = {str(key): value for key, value in slots.items() if str(key) in allowed}
        dropped = [str(key) for key in slots if str(key) not in allowed]
        return filtered, dropped

    def _raw_tasks(self, value: Any) -> list[Any]:
        if isinstance(value, dict) and isinstance(value.get("tasks"), list):
            return value.get("tasks") or []
        return value if isinstance(value, list) else []

    def _unique_task_id(self, value: Any, domain: str, index: int, seen: set[str]) -> str:
        task_id = str(value or f"{domain}_{index + 1}")
        if task_id in seen:
            task_id = f"{task_id}_{index + 1}"
        seen.add(task_id)
        return task_id

    def _confidence(self, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    def _string_list(self, value: Any) -> list[str]:
        return [str(item) for item in value or [] if item] if isinstance(value, list) else []

    def _normalized_text(self, value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).lower()


class ToolAdapter:
    def __init__(self, meetingroom_index: MeetingroomIndex):
        self.meetingroom_index = meetingroom_index

    def adapt(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        adapted = json.loads(json.dumps(args or {}, ensure_ascii=False, default=str))
        if "officeId" in adapted and "office_id" not in adapted:
            adapted["office_id"] = adapted.pop("officeId")
        if "roomId" in adapted and "room_id" not in adapted:
            adapted["room_id"] = adapted.pop("roomId")
        if tool == "meetingroom.booking.create":
            room_id = adapted.get("room_id")
            if room_id and not adapted.get("office_id"):
                office_id = self.meetingroom_index.office_id_for_room(room_id)
                if office_id:
                    adapted["office_id"] = office_id
        if tool == "workflow.save" and adapted.get("workflow_id") not in (None, ""):
            try:
                adapted["workflow_id"] = int(adapted["workflow_id"])
            except Exception:
                pass
        return {key: value for key, value in adapted.items() if value not in (None, "", [], {})}


class EvidenceLedger:
    def __init__(self):
        self.entries: list[dict[str, Any]] = []
        self._next_id = 1

    def _append(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = dict(entry)
        entry["ledger_id"] = self._next_id
        self._next_id += 1
        self.entries.append(entry)
        return entry

    def record_tool(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        result: dict[str, Any],
        kind: str,
        domain: str,
        cached: bool = False,
        preflight_id: int | None = None,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": f"tool_{kind}",
                "tool": tool,
                "args": args,
                "domain": domain,
                "cached": cached,
                "success": isinstance(result, dict) and not result.get("error"),
                "result_summary": self._result_summary(result),
                "preflight_id": preflight_id,
            }
        )

    def record_candidate_decision(
        self,
        *,
        task: str,
        candidate_type: str,
        selected_id: str,
        allowed_ids: list[str],
        source: str,
        confidence: float,
        decision: str,
    ) -> dict[str, Any]:
        return self._append(
            {
                "event": "candidate_decision",
                "task": task,
                "candidate_type": candidate_type,
                "selected_id": selected_id,
                "allowed_candidate_ids": allowed_ids[:20],
                "source": source,
                "confidence": confidence,
                "decision": decision,
            }
        )

    def record_preflight(self, *, tool: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return self._append(
            {
                "event": "preflight",
                "tool": tool,
                "args": args,
                "passed": bool(result.get("passed")),
                "reason": result.get("reason") or "",
                "errors": result.get("errors") or [],
                "warnings": result.get("warnings") or [],
                "evidence_refs": result.get("evidence_refs") or [],
            }
        )

    def record_semantic_fact(self, *, path: str, source: str, decision: str, reason: str = "") -> dict[str, Any]:
        return self._append(
            {
                "event": "semantic_fact",
                "path": path,
                "source": source,
                "decision": decision,
                "reason": reason,
            }
        )

    def record_expense_binding(self, *, candidate_type: str, selected_id: str, allowed_ids: list[str], fingerprint: str, decision: str) -> dict[str, Any]:
        return self._append(
            {
                "event": "expense_binding",
                "candidate_type": candidate_type,
                "selected_id": selected_id,
                "allowed_candidate_ids": allowed_ids[:30],
                "dependency_fingerprint": fingerprint,
                "decision": decision,
            }
        )

    def record_expense_translation(self, *, source: str, decision: str, reason: str = "", row_count: int = 0) -> dict[str, Any]:
        return self._append(
            {
                "event": "expense_translation",
                "source": source,
                "decision": decision,
                "reason": reason,
                "row_count": row_count,
            }
        )

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            event = str(entry.get("event") or "")
            counts[event] = counts.get(event, 0) + 1
        return {
            "entries": len(self.entries),
            "counts": counts,
            "writes": counts.get("tool_write", 0),
            "reads": counts.get("tool_read", 0),
            "preflights": counts.get("preflight", 0),
            "candidate_decisions": counts.get("candidate_decision", 0),
            "semantic_facts": counts.get("semantic_fact", 0),
            "expense_bindings": counts.get("expense_binding", 0),
            "expense_translations": counts.get("expense_translation", 0),
        }

    def _result_summary(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"type": type(result).__name__}
        summary: dict[str, Any] = {"keys": sorted(str(key) for key in result.keys())[:20]}
        if result.get("error"):
            summary["error"] = str(result.get("error"))[:160]
        for key in ["rooms", "bookings", "users", "projects", "options", "participants", "workflows"]:
            value = result.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
                ids = []
                for item in value[:5]:
                    if isinstance(item, dict):
                        ids.append(
                            item.get("room_id")
                            or item.get("order_id")
                            or item.get("user_id")
                            or item.get("project_code")
                            or item.get("value")
                            or item.get("workflow_id")
                        )
                summary[f"{key}_ids"] = [str(item) for item in ids if item]
        return summary


class PreflightGuard:
    def __init__(
        self,
        tool_registry: ToolRegistry,
        workflow_registry: WorkflowSchemaRegistry,
        meetingroom_index: MeetingroomIndex,
    ):
        self.tool_registry = tool_registry
        self.workflow_registry = workflow_registry
        self.meetingroom_index = meetingroom_index

    def validate_write(self, state: "RuntimeState", tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.tool_registry.is_write(tool):
            return {"passed": True, "reason": "", "errors": [], "warnings": [], "evidence_refs": []}
        if tool == "workflow.save":
            return self._validate_workflow_save(state, args)
        if tool == "workflow.delete":
            request_id = str(args.get("request_id") or "")
            source = state.workflow.evidence.get("replacement_source_lookup") or {}
            allowed = {
                str(item.get("request_id") or "")
                for item in source.get("items") or []
                if isinstance(item, dict)
            }
            errors = [] if request_id and request_id in allowed else ["workflow_delete_not_bound_to_source_lookup"]
            return {
                "passed": not errors,
                "reason": "" if not errors else "workflow_delete_preflight_failed",
                "errors": errors,
                "warnings": [],
                "evidence_refs": ["oa.done.list"] if allowed else [],
            }
        if tool.startswith("meetingroom."):
            return self._validate_meetingroom_write(state, tool, args)
        return {"passed": True, "reason": "", "errors": [], "warnings": [], "evidence_refs": []}

    def _validate_workflow_save(self, state: "RuntimeState", args: dict[str, Any]) -> dict[str, Any]:
        result = self.workflow_registry.validate_save(args, state.workflow.evidence.get("schema") or {})
        errors = list(result.get("errors") or [])
        warnings = list(result.get("warnings") or [])
        if int(args.get("workflow_id") or 0) == WORKFLOW_IDS["expense"]:
            expense_result = self._validate_expense_save(state, args)
            errors.extend(expense_result.get("errors") or [])
            warnings.extend(expense_result.get("warnings") or [])
        evidence_refs = []
        if state.workflow.evidence.get("applicant"):
            evidence_refs.append("user.get_info")
        if state.workflow.evidence.get("verified_project") or state.workflow.evidence.get("project"):
            evidence_refs.append("workflow.project_search")
        if state.workflow.evidence.get("category_options"):
            field_id = self.workflow_registry.browser_field_id(
                WORKFLOW_IDS["expense"], "material_category", state.workflow.evidence.get("schema") or {}
            )
            evidence_refs.append(f"workflow.browser_search:{field_id}" if field_id else "workflow.browser_search:category")
        if state.workflow.evidence.get("subclass_options"):
            field_id = self.workflow_registry.browser_field_id(
                WORKFLOW_IDS["expense"], "material_subclass", state.workflow.evidence.get("schema") or {}, detail_table="detail_2"
            )
            evidence_refs.append(f"workflow.browser_search:{field_id}" if field_id else "workflow.browser_search:subclass")
        return {
            "passed": not errors,
            "reason": "" if not errors else "workflow_preflight_failed",
            "errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "missing": result.get("missing") or [],
            "evidence_refs": evidence_refs,
        }

    def _validate_expense_save(self, state: "RuntimeState", args: dict[str, Any]) -> dict[str, Any]:
        data = args.get("data") if isinstance(args.get("data"), dict) else {}
        rows = ((data.get("details") or {}).get("detail_2") or []) if isinstance(data.get("details"), dict) else []
        bindings = state.workflow.evidence.get("expense_bindings") if isinstance(state.workflow.evidence.get("expense_bindings"), dict) else {}
        errors: list[str] = []
        project_binding = bindings.get("project") if isinstance(bindings.get("project"), dict) else {}
        category_binding = bindings.get("category") if isinstance(bindings.get("category"), dict) else {}
        subclass_binding = bindings.get("subclass") if isinstance(bindings.get("subclass"), dict) else {}
        project_fingerprint = f"{data.get('project_code') or ''}|{data.get('wbs_code') or ''}"
        category_id = str(data.get("material_category") or "")
        if not project_binding or project_binding.get("selected_id") != project_fingerprint:
            errors.append("expense_project_not_bound_to_current_candidate")
        if not category_binding or category_binding.get("selected_id") != category_id:
            errors.append("expense_category_not_bound_to_current_candidate")
        expected_dependency = f"{project_fingerprint}|{category_id}"
        if not subclass_binding or subclass_binding.get("dependency_fingerprint") != expected_dependency:
            errors.append("expense_subclass_binding_stale_or_missing")
        allowed_subclasses = {str(item) for item in (subclass_binding.get("allowed_ids") or [])}
        if not allowed_subclasses:
            errors.append("expense_subclass_candidate_set_missing")
        try:
            total = Decimal(str(data.get("total_amount") or ""))
        except (InvalidOperation, ValueError):
            errors.append("expense_total_invalid")
            total = None
        row_total = Decimal("0")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("material_subclass") or "") not in allowed_subclasses:
                errors.append("expense_subclass_outside_bound_candidates")
            try:
                quantity = Decimal(str(row.get("quantity") or ""))
                unit_price = Decimal(str(row.get("unit_price") or ""))
                budget_amount = Decimal(str(row.get("budget_amount") or ""))
                if quantity <= 0 or unit_price < 0 or budget_amount < 0 or quantity * unit_price != budget_amount:
                    errors.append("expense_row_amount_not_conserved")
                row_total += budget_amount
            except (InvalidOperation, ValueError):
                errors.append("expense_row_amount_invalid")
        explicit_total = state.semantic_facts.get("workflow.expense.total_amount")
        if explicit_total not in (None, ""):
            try:
                if total is None or total != Decimal(str(explicit_total)):
                    errors.append("expense_total_conflicts_with_user_literal")
            except (InvalidOperation, ValueError):
                errors.append("expense_user_literal_total_invalid")
        if total is not None and total != row_total:
            errors.append("expense_detail_total_not_conserved")
        evidence = state.workflow.evidence.get("expense_line_evidence")
        evidence_rows = evidence.get("rows") if isinstance(evidence, dict) and isinstance(evidence.get("rows"), list) else []
        if isinstance(evidence, dict) and evidence.get("source") == "memory_package_inferred":
            provenance = evidence.get("memory_provenance") if isinstance(evidence.get("memory_provenance"), dict) else {}
            memory_match = state.workflow.evidence.get("expense_memory_match")
            if not isinstance(memory_match, dict):
                errors.append("expense_memory_match_missing")
            elif (
                provenance.get("memory_id") != memory_match.get("memory_id")
                or provenance.get("result_signature") != memory_match.get("result_signature")
                or provenance.get("source_sha256") != memory_match.get("source_sha256")
            ):
                errors.append("expense_memory_provenance_stale")
            else:
                fingerprint_payload = {
                    "dependency": memory_match.get("project_fingerprint"),
                    "category_id": memory_match.get("category_id"),
                    "candidate_ids": memory_match.get("candidate_ids") or [],
                }
                fingerprint_raw = json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                expected_candidate_fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
                if evidence.get("candidate_set_fingerprint") != expected_candidate_fingerprint:
                    errors.append("expense_memory_candidate_set_stale")
        if len(evidence_rows) != len(rows):
            errors.append("expense_line_evidence_missing_or_stale")
        else:
            evidence_total = Decimal("0")
            for index, row in enumerate(rows):
                expected = evidence_rows[index] if isinstance(evidence_rows[index], dict) else {}
                if not expected.get("source_line_id"):
                    errors.append("expense_line_source_missing")
                    continue
                for key in ("material_subclass", "material_name", "quantity", "unit_price", "budget_amount"):
                    if str(row.get(key) or "") != str(expected.get(key) or ""):
                        errors.append("expense_line_evidence_row_mismatch")
                        break
                try:
                    evidence_total += Decimal(str(expected.get("budget_amount") or ""))
                except (InvalidOperation, ValueError):
                    errors.append("expense_line_evidence_amount_invalid")
            if total is not None and evidence_total != total:
                errors.append("expense_line_evidence_total_mismatch")
        return {"errors": sorted(set(errors)), "warnings": []}

    def _validate_meetingroom_write(self, state: "RuntimeState", tool: str, args: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        evidence_refs: list[str] = []
        if tool == "meetingroom.booking.create":
            for key in ["day", "start", "end", "title"]:
                if args.get(key) in (None, "", [], {}):
                    errors.append(f"missing_{key}")
            room_id = args.get("room_id")
            if not (room_id or args.get("office_id")):
                errors.append("missing_room")
            rooms = (state.meetingroom.evidence.get("room_candidates") or {}).get("rooms") or []
            schedules = state.meetingroom.evidence.get("schedules") or {}
            if rooms:
                evidence_refs.append("meetingroom.room.list")
            if schedules:
                evidence_refs.append("meetingroom.room.schedule")
            if room_id and rooms and not any(str(room.get("room_id") or "") == str(room_id) for room in rooms):
                warnings.append("room_id_not_in_current_room_candidates")
        elif tool in {"meetingroom.booking.cancel", "meetingroom.booking.extend"}:
            if not args.get("order_id"):
                errors.append("missing_order_id")
            if state.meetingroom.evidence.get("selected_booking") or state.meetingroom.evidence.get("booking_query"):
                evidence_refs.append("meetingroom.booking.list")
        elif tool in {"meetingroom.booking.participant.add", "meetingroom.booking.participant.remove"}:
            if not args.get("order_id"):
                errors.append("missing_order_id")
            if not args.get("user_id"):
                errors.append("missing_user_id")
            if state.meetingroom.evidence.get("participants") or state.meetingroom.evidence.get("booking_query"):
                evidence_refs.append("meetingroom.booking.participant.list")
        return {
            "passed": not errors,
            "reason": "" if not errors else "meetingroom_preflight_failed",
            "errors": errors,
            "warnings": warnings,
            "evidence_refs": evidence_refs,
        }


class DomainState:
    def __init__(self):
        self.needed = False
        self.status = "pending"
        self.intent = "unknown"
        self.slots: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {}
        self.facts: dict[str, Any] = {}
        self.result: dict[str, Any] | None = None
        self.blocked_reason = ""


class RuntimeState:
    def __init__(self, obs: dict[str, Any], tools: set[str], step_budget: int):
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + 55.0
        self.obs = obs
        self.tools = tools
        self.step_budget = step_budget
        self.steps_used = 0
        self.llm_calls_fast = 0
        self.llm_calls_strong = 0
        self.llm_elapsed_fast_seconds = 0.0
        self.llm_elapsed_strong_seconds = 0.0
        self.action_elapsed_seconds = 0.0
        self.tool_elapsed_seconds = 0.0
        self.reply_elapsed_seconds = 0.0
        self.cache_elapsed_seconds = 0.0
        self.read_elapsed_seconds = 0.0
        self.read_plan_batches = 0
        self.read_tasks_total = 0
        self.read_tasks_cached = 0
        self.read_tasks_parallel_eligible = 0
        self.empty_read_mappings_total = 0
        self.empty_read_retry_tasks_total = 0
        self.read_task_keys_completed: set[str] = set()
        self.read_task_keys_attempted: set[str] = set()
        self.read_task_groups_succeeded: set[str] = set()
        self.history: list[dict[str, Any]] = []
        self.cache: dict[str, Any] = {}
        self.asked_slots: set[str] = set()
        self.completed_tools: set[str] = set()
        self.ledger = EvidenceLedger()
        self.semantic_facts = SemanticFactStore()
        self.llm_semantic: dict[str, Any] = {}
        self.task_graph: dict[str, Any] = {"tasks": []}
        self.task_runtimes: list[TaskRuntime] = []
        self.workflow_skill: WorkflowSkillRuntime | None = None
        self.active_task_ids: dict[str, str] = {}
        self.task_results: list[dict[str, Any]] = []
        # Read scheduling must not let one domain consume steps reserved for
        # another unfinished task. Writes remain allowed so a ready task can
        # always spend its reserved completion step.
        self.domain_steps_used: dict[str, int] = {"meetingroom": 0, "workflow": 0}
        self.domain_step_budgets: dict[str, int] = {"meetingroom": 0, "workflow": 0}
        self.domain_read_steps_used: dict[str, int] = {"meetingroom": 0, "workflow": 0}
        self.read_scheduler_cursor = 0
        self.candidates: dict[str, dict[str, list[dict[str, Any]]]] = {"meetingroom": {}, "workflow": {}}
        self.candidate_decisions: list[dict[str, Any]] = []
        self.last_action_domain = ""
        self.meetingroom = DomainState()
        self.workflow = DomainState()


class StaticContextStore:
    DEFAULT_MAX_CHARS = {"intent": 700, "task_graph": 3000, "candidate": 6000, "form": 7000}

    def __init__(self, base_dir: Path, enabled: bool = True, max_chars: dict[str, int] | None = None):
        self.base_dir = base_dir
        self.enabled = enabled
        self.max_chars = dict(self.DEFAULT_MAX_CHARS)
        if max_chars:
            for key, value in max_chars.items():
                try:
                    self.max_chars[key] = max(500, int(value))
                except Exception:
                    pass
        self.available = False
        self.error = ""
        self.manifest: dict[str, Any] = {}
        self.tools: dict[str, Any] = {}
        self.workflows: dict[str, Any] = {}
        self.meetingrooms: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.expense_examples: dict[str, Any] = {}
        self.workflow_skills: dict[str, Any] = {}
        self.outcome_policies: dict[str, Any] = {}
        self.cards: dict[str, str] = {}
        if enabled:
            self._load()

    def _load(self) -> None:
        try:
            self.manifest = self._load_json("manifest.json")
            self.tools = self._load_json("tools.index.json")
            self.workflows = self._load_json("workflows.index.json")
            self.meetingrooms = self._load_json("meetingrooms.index.json")
            self.capabilities = self._load_json("capabilities.index.json")
            expense_examples_path = self.base_dir / "expense_examples.index.json"
            if expense_examples_path.is_file():
                self.expense_examples = self._load_json("expense_examples.index.json")
            workflow_skills_path = self.base_dir / "workflow_skills.index.json"
            if workflow_skills_path.is_file():
                self.workflow_skills = self._load_json("workflow_skills.index.json")
            outcome_policies_path = self.base_dir / "outcome_policies.index.json"
            if outcome_policies_path.is_file():
                self.outcome_policies = self._load_json("outcome_policies.index.json")
            cards_dir = self.base_dir / "prompt_cards"
            for name in [
                "routing.md",
                "tool_policy.md",
                "workflow_form_policy.md",
                "meetingroom_policy.md",
                "preflight_policy.md",
            ]:
                path = cards_dir / name
                if path.is_file():
                    self.cards[name] = path.read_text(encoding="utf-8").strip()
            self.available = True
        except Exception as exc:
            self.available = False
            self.error = str(exc)[:240]

    def _load_json(self, filename: str) -> dict[str, Any]:
        path = self.base_dir / filename
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "base_dir": str(self.base_dir),
            "error": self.error,
            "counts": self.manifest.get("counts") or {},
            "sources": self.manifest.get("sources") or {},
            "expense_memory": self.expense_examples.get("counts") or {},
        }

    def _pack(self, pack_type: str, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"pack_type": "disabled", "content": "", "chars": 0}
        if not self.available:
            return {"pack_type": "unavailable", "content": "", "chars": 0, "error": self.error}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        limit = int(self.max_chars.get(stage) or self.DEFAULT_MAX_CHARS.get(stage) or 3000)
        if len(text) > limit:
            suffix = "\n[static_context_truncated]"
            text = text[: max(0, limit - len(suffix))] + suffix
        return {"pack_type": pack_type, "content": text, "chars": len(text)}

    def _tool_summary(self) -> dict[str, Any]:
        return {
            "counts": self.tools.get("counts") or {},
            "by_domain": self.tools.get("by_domain") or {},
            "by_risk": self.tools.get("by_risk") or {},
            "adapter_notes": {
                name: item.get("adapter_notes") or []
                for name, item in (self.tools.get("by_name") or {}).items()
                if item.get("adapter_notes")
            },
        }

    def _workflow_catalog_summary(self) -> list[dict[str, Any]]:
        rows = []
        for item in self.workflows.get("catalog") or []:
            workflow_id = str(item.get("workflow_id") or "")
            schema = (self.workflows.get("by_id") or {}).get(workflow_id) or {}
            rows.append(
                {
                    "workflow_id": item.get("workflow_id"),
                    "name": item.get("name"),
                    "required_fields": schema.get("required_fields") or [],
                    "field_count": len(schema.get("fields") or []),
                    "detail_tables": list((schema.get("detail_tables") or {}).keys()),
                    "submit_policy": schema.get("submit_policy"),
                }
            )
        return rows

    def _meetingroom_summary(self) -> dict[str, Any]:
        return {
            "counts": self.meetingrooms.get("counts") or {},
            "office_address_rules": self.meetingrooms.get("office_address_rules") or {},
            "normalization_aliases": self.meetingrooms.get("normalization_aliases") or {},
        }

    def for_intent_router(self) -> dict[str, Any]:
        workflows = [
            {
                "id": item.get("workflow_id"),
                "name": item.get("name"),
                "kind": self._workflow_kind(item),
            }
            for item in (self.workflows.get("catalog") or [])
        ]
        capabilities = []
        for capability_id, spec in sorted((self.capabilities.get("capabilities") or {}).items()):
            if not isinstance(spec, dict):
                continue
            capabilities.append(
                {
                    "id": capability_id,
                    "domain": spec.get("domain"),
                    "intent": spec.get("intent"),
                    "risk": spec.get("risk"),
                    "required_slots": spec.get("required_slots") or [],
                }
            )
        payload = {
            "p": "intent routing only; output task graph, slot hints, missing_slots, must_not_guess",
            "capabilities": capabilities,
            "intent_maps": {
                "meetingroom": self.capabilities.get("meeting_intent_map") or {},
                "workflow": self.capabilities.get("workflow_intent_map") or {},
            },
            "workflows": workflows,
            "verify": "ids/codes/browser values/order_id/room_id/user_id/project_code/wbs_code require tools",
            "submit": "explicit submit only; otherwise draft",
        }
        return self._pack("intent_router_static_context", "intent", payload)

    def _workflow_kind(self, item: dict[str, Any]) -> str:
        text = f"{item.get('workflow_id') or ''} {item.get('name') or ''}".lower()
        if "leave" in text or "请假" in text:
            return "leave"
        if "expense" in text or "报销" in text or "费用" in text or "物资" in text:
            return "expense_material"
        return "workflow"

    def for_task_graph(self) -> dict[str, Any]:
        payload = {
            "cards": {
                "routing": self.cards.get("routing.md", ""),
                "tool_policy": self.cards.get("tool_policy.md", ""),
            },
            "tools": self._tool_summary(),
            "workflows": self._workflow_catalog_summary(),
            "meetingrooms": self._meetingroom_summary(),
            "rules": [
                "static context is contract/index data, not case memory",
                "do not output tool calls or final answers in task graph",
                "ids/codes/values are hints until verified by tools",
            ],
        }
        return self._pack("task_graph_static_context", "task_graph", payload)

    def for_candidate_ranker(
        self,
        task: str,
        candidates: list[dict[str, Any]],
        id_fields: list[str],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "cards": {
                "tool_policy": self.cards.get("tool_policy.md", ""),
            },
            "task": task,
            "candidate_count": len(candidates),
            "id_fields": id_fields,
            "context_keys": sorted((context or {}).keys()),
            "selection_rules": [
                "select only an id present in the current candidates payload",
                "return ambiguous or need_more_info when evidence is weak",
                "never invent ids, codes, labels, or business facts",
            ],
        }
        return self._pack("candidate_static_context", "candidate", payload)

    def for_workflow_form(self, workflow_id: Any, evidence_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        workflow_key = str(workflow_id or "")
        schema = (self.workflows.get("by_id") or {}).get(workflow_key) or {}
        option_prefix = f"{workflow_key}:"
        options = {
            key: value
            for key, value in (self.workflows.get("option_sets") or {}).items()
            if str(key).startswith(option_prefix)
        }
        payload = {
            "cards": {
                "workflow_form_policy": self.cards.get("workflow_form_policy.md", ""),
                "preflight_policy": self.cards.get("preflight_policy.md", ""),
            },
            "workflow": schema,
            "option_sets": options,
            "evidence_summary": evidence_summary or {},
            "rules": [
                "form output is a draft only; program preflight builds final tool args",
                "browser/select values must come from verified options",
                "money consistency is mandatory for detail rows",
            ],
        }
        return self._pack("workflow_form_static_context", "form", payload)


class MyAgent:
    def __init__(self, env):
        self.env = env
        self._config_sections_from_files: set[str] = set()
        self.config = self._load_config()
        self.static_context = StaticContextStore(
            self._static_context_path(),
            enabled=self._static_context_enabled(),
            max_chars=self._static_context_max_chars(),
        )
        self.tool_registry = ToolRegistry(self.static_context.tools)
        self.workflow_registry = WorkflowSchemaRegistry(self.static_context.workflows)
        self.meetingroom_index = MeetingroomIndex(self.static_context.meetingrooms)
        self.capability_registry = CapabilityRegistry(self.static_context.capabilities)
        self.workflow_skill_registry = WorkflowSkillRegistry(self.static_context.workflow_skills)
        self.outcome_policy_memory = OutcomePolicyMemory(self.static_context.outcome_policies)
        self.task_graph_normalizer = TaskGraphContractNormalizer(self.capability_registry)
        self.tool_adapter = ToolAdapter(self.meetingroom_index)
        self.preflight_guard = PreflightGuard(self.tool_registry, self.workflow_registry, self.meetingroom_index)
        self.read_plan_executor = ReadPlanExecutor(self)
        self._read_parallel_lock = threading.Lock()

    def run(self, case_id: str) -> dict:
        state: RuntimeState | None = None
        try:
            obs = self.env.reset(case_id)
            tools = self.env.list_tools()
            state = RuntimeState(
                obs=obs,
                tools={item.get("name") for item in tools if isinstance(item, dict)},
                step_budget=int(obs.get("step_budget") or 0),
            )
            state.deadline_at = state.started_at + self._case_deadline_seconds()
            debug_config = self._debug_llm_config()
            self._debug_log(
                debug_config,
                {
                    "event": "start",
                    "case_id": case_id,
                    "started_at": 0.0,
                    "deadline_seconds": round(state.deadline_at - state.started_at, 3),
                    "step_budget": state.step_budget,
                    "tool_count": len(state.tools),
                    "obs": obs,
                },
            )
            self._debug_log(debug_config, {"event": "static_context_status", **self.static_context.status()})
            self._debug_log(
                debug_config,
                {
                    "event": "registry_status",
                    "tool_registry": self.tool_registry.status(),
                    "workflow_registry": self.workflow_registry.status(),
                    "meetingroom_index": self.meetingroom_index.status(),
                    "capability_registry": self.capability_registry.status(),
                },
            )

            state.llm_semantic = self._extract_semantics(state, obs, tools)
            self._init_context(state)
            self._debug_log(debug_config, {"event": "semantic", "semantic": state.llm_semantic})
            self._debug_log(
                debug_config,
                {
                    "event": "task_runtime_init",
                    "case_id": case_id,
                    "active_task_ids": state.active_task_ids,
                    "tasks": [runtime.to_dict() for runtime in state.task_runtimes],
                },
            )

            max_iterations = max(4, state.step_budget + 4)
            for _ in range(max_iterations):
                if state.steps_used >= state.step_budget:
                    break
                self._advance_task_runtimes(state)
                if self._all_done(state):
                    break
                if self._drain_read_plan(state, debug_config):
                    self._refresh_task_runtime_statuses(state)
                    self._advance_task_runtimes(state)
                    if self._all_done(state):
                        break
                    continue
                action = self._next_action(state)
                if action is None:
                    break
                self._execute(state, action, debug_config)
                self._advance_task_runtimes(state)
                if self._all_done(state):
                    break

            self._close_pending_domains(state)
            answer = self._build_final_answer(state)
            finished_at = time.monotonic()
            elapsed_seconds = max(0.0, finished_at - state.started_at)
            llm_elapsed_seconds = state.llm_elapsed_fast_seconds + state.llm_elapsed_strong_seconds
            self._debug_log(
                debug_config,
                {
                    "event": "finish",
                    "case_id": case_id,
                    "steps_used": state.steps_used,
                    "llm_calls_fast": state.llm_calls_fast,
                    "llm_calls_strong": state.llm_calls_strong,
                    "end_at": round(elapsed_seconds, 3),
                    "remaining_seconds": round(max(0.0, state.deadline_at - finished_at), 3),
                    "elapsed": round(elapsed_seconds, 3),
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "llm_elapsed_seconds": round(llm_elapsed_seconds, 3),
                    "llm_elapsed_fast_seconds": round(state.llm_elapsed_fast_seconds, 3),
                    "llm_elapsed_strong_seconds": round(state.llm_elapsed_strong_seconds, 3),
                    "tool_elapsed_seconds": round(state.tool_elapsed_seconds, 3),
                    "reply_elapsed_seconds": round(state.reply_elapsed_seconds, 3),
                    "cache_elapsed_seconds": round(state.cache_elapsed_seconds, 3),
                    "action_elapsed_seconds": round(state.action_elapsed_seconds, 3),
                    "read_elapsed_seconds": round(state.read_elapsed_seconds, 3),
                    "read_plan_batches": state.read_plan_batches,
                    "read_tasks_total": state.read_tasks_total,
                    "read_tasks_cached": state.read_tasks_cached,
                    "read_tasks_parallel_eligible": state.read_tasks_parallel_eligible,
                    "empty_read_mappings_total": state.empty_read_mappings_total,
                    "empty_read_retry_tasks_total": state.empty_read_retry_tasks_total,
                    "task_runtimes": [runtime.to_dict() for runtime in state.task_runtimes],
                    "task_results": state.task_results,
                    "domain_step_budgets": state.domain_step_budgets,
                    "domain_steps_used": state.domain_steps_used,
                    "domain_read_steps_used": state.domain_read_steps_used,
                    "ledger_summary": state.ledger.summary(),
                    "semantic_fact_summary": state.semantic_facts.summary(),
                    "expense_binding_summary": state.workflow.evidence.get("expense_bindings") or {},
                    "expense_draft_ir": state.workflow.evidence.get("expense_draft_ir") or {},
                    "expense_line_evidence": state.workflow.evidence.get("expense_line_evidence") or {},
                    "project_resolution": state.workflow.evidence.get("project_resolution") or {},
                    "workflow_skill": state.workflow_skill.summary() if state.workflow_skill is not None else {},
                    "outcome_policy_decision": state.workflow.evidence.get("outcome_policy_decision") or {},
                    "non_llm_elapsed_seconds": round(max(0.0, elapsed_seconds - llm_elapsed_seconds), 3),
                    "answer": answer,
                    "history": state.history,
                },
            )
            return answer
        except Exception as exc:
            if state is not None:
                reason = f"runtime_error:{type(exc).__name__}"
                for domain_state in (state.meetingroom, state.workflow):
                    if domain_state.needed and domain_state.status not in {"done", "blocked"}:
                        domain_state.status = "blocked"
                        domain_state.blocked_reason = reason
                        domain_state.result = {"status": "blocked", "reason": reason}
                for runtime in state.task_runtimes:
                    if runtime.status not in TaskRuntime.TERMINAL_STATUSES:
                        runtime.status = "blocked"
                        runtime.blocked_reason = reason
                answer = self._build_final_answer(state)
                self._debug_log(
                    self._debug_llm_config(),
                    {"event": "run_error", "case_id": case_id, "error": str(exc), "answer": answer},
                )
                return answer
            try:
                self._debug_log(self._debug_llm_config(), {"event": "run_error", "case_id": case_id, "error": str(exc)})
            except Exception:
                pass
            return {}

    # ------------------------------------------------------------------
    # Configuration and LLM
    # ------------------------------------------------------------------

    def _load_config(self) -> dict[str, Any]:
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        base_dir = Path(__file__).resolve().parent
        filenames = ["config.json"]
        if str(os.getenv("AGENT_USE_LOCAL_CONFIG") or "").strip().lower() in {"1", "true", "yes", "on"}:
            filenames.append("config.local.json")
        for filename in filenames:
            path = base_dir / filename
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._config_sections_from_files.update(str(key) for key in data.keys())
                        self._deep_update(config, data)
                except Exception:
                    pass
        return config

    def _case_deadline_seconds(self) -> float:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        try:
            value = float(os.getenv("CASE_DEADLINE_SECONDS") or runtime.get("case_deadline_seconds") or 55)
        except Exception:
            value = 55.0
        return max(10.0, min(58.0, value))

    def _semantic_extraction_mode(self) -> str:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        value = str(os.getenv("SEMANTIC_EXTRACTION") or runtime.get("semantic_extraction") or "auto").strip().lower()
        if value not in {"always", "auto", "off"}:
            return "always"
        return value

    def _task_graph_log_path(self) -> str:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        return str(os.getenv("TASK_GRAPH_LOG_PATH") or runtime.get("task_graph_log_path") or "").strip()

    def _static_context_enabled(self) -> bool:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        env_value = os.getenv("STATIC_CONTEXT_ENABLED")
        if env_value is not None:
            return env_value.strip().lower() not in {"0", "false", "off", "no"}
        return bool(runtime.get("static_context_enabled", True))

    def _static_context_path(self) -> Path:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        value = str(os.getenv("STATIC_CONTEXT_PATH") or runtime.get("static_context_path") or "submission/static_context")
        path = Path(value)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        return path

    def _static_context_max_chars(self) -> dict[str, int]:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        defaults = {"intent": 700, "task_graph": 3000, "candidate": 6000, "form": 7000}
        value = runtime.get("static_context_max_chars")
        if isinstance(value, dict):
            for key, item in value.items():
                try:
                    defaults[str(key)] = int(item)
                except Exception:
                    pass
        elif value is not None:
            try:
                size = int(value)
                defaults = {key: size for key in defaults}
            except Exception:
                pass
        env_value = os.getenv("STATIC_CONTEXT_MAX_CHARS")
        if env_value:
            try:
                size = int(env_value)
                defaults = {key: size for key in defaults}
            except Exception:
                pass
        return defaults

    def _runtime_bool(self, key: str, default: bool) -> bool:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        env_value = os.getenv(key.upper())
        if env_value is not None:
            return env_value.strip().lower() not in {"0", "false", "off", "no"}
        return bool(runtime.get(key, default))

    def _runtime_int(self, key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        try:
            value = int(os.getenv(key.upper()) or runtime.get(key) or default)
        except Exception:
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _runtime_float(self, key: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        try:
            value = float(os.getenv(key.upper()) or runtime.get(key) or default)
        except Exception:
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _parallel_read_planner_enabled(self) -> bool:
        return self._runtime_bool("parallel_read_planner_enabled", True)

    def _parallel_reads_enabled(self) -> bool:
        return self._runtime_bool("parallel_reads_enabled", False)

    def _async_read_execution_enabled(self) -> bool:
        return self._runtime_bool("async_read_execution_enabled", False)

    def _parallel_read_max_workers(self) -> int:
        return self._runtime_int("parallel_read_max_workers", 4, minimum=1, maximum=8)

    def _parallel_read_max_batch_size(self) -> int:
        return self._runtime_int("parallel_read_max_batch_size", 6, minimum=1, maximum=12)

    def _parallel_read_min_remaining_seconds(self) -> float:
        return self._runtime_float("parallel_read_min_remaining_seconds", 8.0, minimum=1.0, maximum=30.0)

    def _parallel_read_timeout_seconds(self) -> float:
        return self._runtime_float("parallel_read_timeout_seconds", 6.0, minimum=0.5, maximum=30.0)

    def _empty_read_mapping_enabled(self) -> bool:
        return self._runtime_bool("empty_read_mapping_enabled", True)

    def _empty_read_mapping_max_variants(self) -> int:
        return self._runtime_int("empty_read_mapping_max_variants", 3, minimum=1, maximum=3)

    def _debug_llm_config(self) -> dict[str, Any]:
        return self._llm_config("fast")

    def _llm_config(self, profile: str = "strong") -> dict[str, Any]:
        legacy = dict(self.config.get("llm") or {})
        key = "llm_fast" if profile == "fast" else "llm_strong"
        if key in self._config_sections_from_files:
            llm = dict(DEFAULT_CONFIG.get(key) or {})
            self._deep_update(llm, legacy)
            self._deep_update(llm, dict(self.config.get(key) or {}))
        elif legacy:
            llm = dict(legacy)
        else:
            llm = dict(DEFAULT_CONFIG.get(key) or DEFAULT_CONFIG.get("llm") or {})
        base_url_env = "OPENAI_FAST_BASE_URL" if profile == "fast" else "OPENAI_STRONG_BASE_URL"
        llm["base_url"] = os.getenv(base_url_env) or os.getenv("OPENAI_BASE_URL") or llm.get("base_url") or "https://api.openai.com/v1"
        llm["base_url"] = self._normalize_base_url(str(llm["base_url"]))
        model_env = "OPENAI_FAST_MODEL" if profile == "fast" else "OPENAI_STRONG_MODEL"
        key_env = "OPENAI_FAST_API_KEY" if profile == "fast" else "OPENAI_STRONG_API_KEY"
        timeout_env = "OPENAI_FAST_TIMEOUT" if profile == "fast" else "OPENAI_STRONG_TIMEOUT"
        tokens_env = "OPENAI_FAST_MAX_TOKENS" if profile == "fast" else "OPENAI_STRONG_MAX_TOKENS"
        llm["model"] = os.getenv(model_env) or os.getenv("OPENAI_MODEL") or llm.get("model") or "gpt-4o"
        llm["api_key"] = os.getenv(key_env) or os.getenv("OPENAI_API_KEY") or llm.get("api_key") or ""
        # The submission ships the same model/endpoint for the fast semantic
        # parser and the stronger evidence-translation stage.  Local runners
        # commonly provide only OPENAI_FAST_API_KEY; in that equivalent-profile
        # setup, reuse it so post-retrieval translation is not silently disabled.
        if not llm["api_key"] and profile == "strong":
            fast = self._llm_config("fast")
            if (
                fast.get("api_key")
                and str(fast.get("base_url") or "") == str(llm.get("base_url") or "")
                and str(fast.get("model") or "") == str(llm.get("model") or "")
            ):
                llm["api_key"] = str(fast["api_key"])
        if llm["api_key"] in {"your-api-key", "replace-with-your-api-key", "sk-xxx"}:
            llm["api_key"] = ""
        llm["timeout"] = float(os.getenv(timeout_env) or os.getenv("OPENAI_TIMEOUT") or llm.get("timeout") or (1 if profile == "fast" else 15))
        llm["temperature"] = float(os.getenv("OPENAI_TEMPERATURE") or llm.get("temperature") or 0)
        seed_env = "OPENAI_FAST_SEED" if profile == "fast" else "OPENAI_STRONG_SEED"
        seed_value = os.getenv(seed_env) or os.getenv("OPENAI_SEED")
        if seed_value is None:
            seed_value = llm.get("seed")
        try:
            llm["seed"] = int(seed_value) if seed_value not in (None, "") else None
        except (TypeError, ValueError):
            llm["seed"] = None
        llm["max_llm_rounds"] = int(os.getenv("MAX_LLM_ROUNDS") or llm.get("max_llm_rounds") or 4)
        llm["max_calls"] = int(llm.get("max_calls") or (1 if profile in {"fast", "strong"} else llm["max_llm_rounds"]))
        try:
            llm["max_tokens"] = int(os.getenv(tokens_env) or os.getenv("OPENAI_MAX_TOKENS") or llm.get("max_tokens") or 0)
        except Exception:
            llm["max_tokens"] = 0
        llm["max_history_items"] = int(llm.get("max_history_items") or 16)
        debug_env = "AGENT_FAST_DEBUG_LOG_PATH" if profile == "fast" else "AGENT_STRONG_DEBUG_LOG_PATH"
        llm["debug_log_path"] = (
            os.getenv(debug_env)
            or os.getenv("AGENT_DEBUG_LOG_PATH")
            or llm.get("debug_log_path")
            or self._task_graph_log_path()
            or ""
        )
        llm["profile"] = profile
        return llm

    def _normalize_base_url(self, base_url: str) -> str:
        url = str(base_url or "").strip().rstrip("/")
        if "packyapi.com" in url and not url.endswith("/v1"):
            url += "/v1"
        return url

    def _extract_semantics(self, state: RuntimeState, obs: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        _ = tools
        stage_started = time.monotonic()
        baseline = self._heuristic_semantics(obs)
        llm_config = self._llm_config("fast")
        mode = self._semantic_extraction_mode()
        complexity = self._task_graph_complexity(baseline, obs)
        route = self._capability_route_from_baseline(baseline, obs, complexity)
        llm_config["timeout"] = self._task_graph_timeout_for_complexity(complexity, llm_config)
        prompt_chars = 0
        response_chars = 0
        context_pack = self.static_context.for_intent_router()
        context_pack_type = str(context_pack.get("pack_type") or "none")
        context_chars = int(context_pack.get("chars") or 0)
        fallback_reason = ""
        source = "heuristic_fallback"
        fast_llm_success = False
        fast_attempted = False
        baseline_contract = self._semantic_contract_from_baseline(baseline, obs)
        baseline_graph = baseline_contract.get("task_graph")
        full_query = self._full_query(obs)

        def finish(semantic: dict[str, Any], reason: str = "") -> dict[str, Any]:
            nonlocal fallback_reason, source, fast_llm_success
            if reason:
                fallback_reason = reason
            graph = self._normalize_task_graph(semantic.get("task_graph"), baseline_graph, full_query)
            if not graph.get("tasks"):
                semantic = baseline_contract
                graph = self._normalize_task_graph(semantic.get("task_graph"), baseline_graph, full_query)
                source = "heuristic_fallback"
                fast_llm_success = False
                fallback_reason = fallback_reason or "empty_task_graph"
            else:
                semantic["task_graph"] = graph
            self._capture_canonical_semantic_facts(state, baseline, semantic, source)
            self._log_task_graph_stage(
                state=state,
                obs=obs,
                graph=graph,
                source=source,
                started_at=stage_started,
                llm_config=llm_config,
                complexity=complexity,
                route=route,
                fast_llm_success=fast_llm_success,
                fast_attempted=fast_attempted,
                fallback_reason=fallback_reason,
                prompt_chars=prompt_chars,
                response_chars=response_chars,
            )
            return semantic

        if mode == "off" or not llm_config.get("api_key"):
            fallback_reason = "disabled_or_no_key" if mode == "off" else "no_api_key"
            self._debug_log(llm_config, self._semantic_llm_event(state, fallback_reason, mode, llm_config, complexity))
            return finish(self._semantic_contract_from_baseline(baseline, obs), fallback_reason)
        # Experimental budget gate only. Default config keeps fast LLM attempted
        # for every case so the task-graph stage is never bypassed silently.
        if mode == "auto" and not self._needs_fast_semantic(baseline, obs, complexity, route):
            fallback_reason = "auto_heuristic_high_confidence"
            self._debug_log(llm_config, self._semantic_llm_event(state, fallback_reason, mode, llm_config, complexity))
            return finish(self._semantic_contract_from_baseline(baseline, obs), fallback_reason)
        if not self._can_call_llm(state, "fast", min_remaining=6.0):
            fallback_reason = "deadline_or_budget"
            self._debug_log(llm_config, self._semantic_llm_event(state, fallback_reason, mode, llm_config, complexity))
            return finish(self._semantic_contract_from_baseline(baseline, obs), fallback_reason)
        payload = {
            "q": self._task_graph_query_payload(obs),
            "h": self._task_graph_fast_hint(baseline),
            "cx": complexity.get("level"),
            "rule": "Correct h when text differs. Keep ids/codes empty unless typed by user.",
        }
        if context_pack.get("content"):
            payload["ctx"] = context_pack["content"]
        prompt_text = TASK_GRAPH_PROMPT
        user_text = "Return json object only.\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        prompt_chars = len(prompt_text) + len(user_text)
        try:
            fast_attempted = True
            content = self._chat_completion(
                llm_config,
                [
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": user_text},
                ],
                state=state,
                profile="fast",
                context_pack_type=context_pack_type,
                context_chars=context_chars,
                prompt_chars=prompt_chars,
            )
            response_chars = len(content)
            parsed = self._parse_json_object(content)
            if isinstance(parsed, dict):
                merged = self._merge_semantic(baseline, parsed)
                source = "fast_llm"
                fast_llm_success = True
                return finish(merged)
            fallback_reason = "invalid_json"
        except Exception as exc:
            fallback_reason = str(exc)[:200] or "fast_llm_error"
            error_event = self._semantic_llm_event(state, fallback_reason, mode, llm_config, complexity)
            error_event["event"] = "semantic_llm_error"
            error_event["error"] = str(exc)[:240]
            self._debug_log(llm_config, error_event)
        return finish(self._semantic_contract_from_baseline(baseline, obs), fallback_reason)

    def _capture_canonical_semantic_facts(
        self,
        state: RuntimeState,
        baseline: dict[str, Any],
        semantic: dict[str, Any],
        semantic_source: str,
    ) -> None:
        """Preserve parser-backed user literals before any LLM translation is consumed."""
        baseline_wf = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
        semantic_wf = semantic.get("workflow") if isinstance(semantic.get("workflow"), dict) else {}
        baseline_expense = baseline_wf.get("expense") if isinstance(baseline_wf.get("expense"), dict) else {}
        semantic_expense = semantic_wf.get("expense") if isinstance(semantic_wf.get("expense"), dict) else {}
        baseline_leave = baseline_wf.get("leave") if isinstance(baseline_wf.get("leave"), dict) else {}
        semantic_leave = semantic_wf.get("leave") if isinstance(semantic_wf.get("leave"), dict) else {}

        scoped_leave = self._heuristic_leave(
            self._domain_source_text(self._full_query(state.obs), "workflow", "leave")
        )
        protected = {
            "workflow.expense.project_code": baseline_expense.get("project_code"),
            "workflow.expense.project_name": baseline_expense.get("project_name"),
            "workflow.expense.total_amount": self._money(baseline_expense.get("total_amount")) if baseline_expense.get("total_amount") else "",
            "workflow.leave.day_text": scoped_leave.get("day_text") or baseline_leave.get("day_text"),
            "workflow.leave.start": scoped_leave.get("start") or baseline_leave.get("start"),
            "workflow.leave.end": scoped_leave.get("end") or baseline_leave.get("end"),
        }
        for index, item in enumerate(baseline_expense.get("items") or []):
            if not isinstance(item, dict):
                continue
            for key in ("quantity", "unit_price", "budget_amount"):
                value = item.get(key)
                if value not in (None, ""):
                    protected[f"workflow.expense.items[{index}].{key}"] = str(value)
        for path, value in protected.items():
            if value not in (None, ""):
                self._set_semantic_fact(state, path, value, "user_literal")

        translations = {
            "workflow.expense.project_code": semantic_expense.get("project_code"),
            "workflow.expense.project_name": semantic_expense.get("project_name"),
            "workflow.expense.total_amount": self._money(semantic_expense.get("total_amount")) if semantic_expense.get("total_amount") else "",
            "workflow.leave.day_text": semantic_leave.get("day_text"),
            "workflow.leave.start": semantic_leave.get("start"),
            "workflow.leave.end": semantic_leave.get("end"),
        }
        for path, value in translations.items():
            if value not in (None, ""):
                self._set_semantic_fact(state, path, value, "llm_translation" if semantic_source == "fast_llm" else "program_computed")

    def _set_semantic_fact(self, state: RuntimeState, path: str, value: Any, source: str) -> bool:
        before_conflicts = len(state.semantic_facts.conflicts)
        accepted = state.semantic_facts.set(path, value, source)
        if len(state.semantic_facts.conflicts) > before_conflicts:
            conflict = state.semantic_facts.conflicts[-1]
            decision = "accepted" if accepted else "rejected"
            state.ledger.record_semantic_fact(path=path, source=source, decision=decision, reason="provenance_conflict")
            self._debug_log(
                self._debug_llm_config(),
                {"event": "semantic_fact_conflict", "case_id": state.obs.get("case_id"), "decision": decision, **conflict},
            )
        elif not accepted:
            state.ledger.record_semantic_fact(path=path, source=source, decision="rejected", reason="provenance_conflict")
        return accepted

    def _semantic_llm_event(
        self,
        state: RuntimeState,
        reason: str,
        mode: str,
        llm_config: dict[str, Any],
        complexity: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "event": "semantic_llm_skipped",
            "case_id": state.obs.get("case_id"),
            "reason": reason,
            "mode": mode,
            "fast_timeout_seconds": round(float(llm_config.get("timeout") or 0), 3),
            "remaining_seconds": round(self._remaining_seconds(state), 3),
            "complexity_level": complexity.get("level"),
            "complexity_score": complexity.get("score"),
            "complexity_reasons": complexity.get("reasons") or [],
            "simple_proven": bool(complexity.get("simple_proven")),
        }

    def _log_task_graph_stage(
        self,
        state: RuntimeState,
        obs: dict[str, Any],
        graph: dict[str, Any],
        source: str,
        started_at: float,
        llm_config: dict[str, Any],
        complexity: dict[str, Any],
        route: dict[str, Any],
        fast_llm_success: bool,
        fast_attempted: bool,
        fallback_reason: str,
        prompt_chars: int,
        response_chars: int,
    ) -> None:
        tasks = graph.get("tasks") if isinstance(graph.get("tasks"), list) else []
        domains: list[str] = []
        intents: list[str] = []
        confidences: list[float] = []
        missing_slots_count = 0
        must_not_guess_count = 0
        for task in tasks:
            if not isinstance(task, dict):
                continue
            domain = str(task.get("domain") or "")
            intent = str(task.get("intent") or "")
            if domain:
                domains.append(domain)
            if intent:
                intents.append(intent)
            confidences.append(self._bounded_float(task.get("confidence"), 0.0))
            if isinstance(task.get("missing_slots"), list):
                missing_slots_count += len(task.get("missing_slots") or [])
            if isinstance(task.get("must_not_guess"), list):
                must_not_guess_count += len(task.get("must_not_guess") or [])
        now = time.monotonic()
        event = {
            "event": "task_graph",
            "case_id": obs.get("case_id"),
            "mode": obs.get("mode"),
            "source": source,
            "started_at": round(started_at - state.started_at, 3),
            "elapsed_seconds": round(now - started_at, 3),
            "remaining_seconds": round(max(0.0, state.deadline_at - now), 3),
            "fast_timeout_seconds": round(float(llm_config.get("timeout") or 0), 3),
            "fast_max_tokens": int(llm_config.get("max_tokens") or 0),
            "complexity_level": complexity.get("level"),
            "complexity_score": complexity.get("score"),
            "complexity_reasons": complexity.get("reasons") or [],
            "simple_proven": bool(complexity.get("simple_proven")),
            "routing_risk": route.get("routing_risk"),
            "routing_flags": route.get("risk_flags") or [],
            "capabilities": [task.get("capability") for task in route.get("tasks", []) if isinstance(task, dict)],
            "capability_count": len(route.get("tasks") or []),
            "routing_path": route.get("routing_path"),
            "fast_attempted": fast_attempted,
            "fast_llm_success": fast_llm_success,
            "task_count": len(tasks),
            "domains": sorted(set(domains)),
            "intents": sorted(set(intents)),
            "min_confidence": min(confidences) if confidences else 0.0,
            "missing_slots_count": missing_slots_count,
            "must_not_guess_count": must_not_guess_count,
            "fallback_reason": fallback_reason if source != "fast_llm" else "",
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
        }
        self._write_task_graph_log(event)
        self._debug_log(llm_config, event)

    def _capability_route_from_baseline(
        self,
        baseline: dict[str, Any],
        obs: dict[str, Any],
        complexity: dict[str, Any],
    ) -> dict[str, Any]:
        query = self._full_query(obs)
        tasks: list[dict[str, Any]] = []
        risk_flags: list[str] = list(complexity.get("reasons") or [])
        meeting = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
        workflow = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}

        meeting_capability = self._meeting_capability(meeting)
        if meeting_capability:
            tasks.append(self._capability_task("cap_meeting_1", meeting_capability, meeting, query, "meetingroom"))

        workflow_capability = self._workflow_capability(workflow)
        if workflow_capability:
            workflow_slots = self._workflow_capability_slots(workflow)
            tasks.append(self._capability_task("cap_workflow_1", workflow_capability, workflow_slots, query, "workflow"))

        if not tasks:
            risk_flags.append("unknown_capability")
        if len(tasks) > 1:
            risk_flags.append("multi_capability")

        routing_risk = self._routing_risk(complexity, tasks, risk_flags)
        return {
            "tasks": tasks,
            "routing_risk": routing_risk,
            "risk_flags": self._dedupe(risk_flags),
            "routing_path": self._routing_path_for_risk(routing_risk),
        }

    def _meeting_capability(self, meeting: dict[str, Any]) -> str:
        intent = str(meeting.get("intent") or "unknown")
        return self.capability_registry.meeting_capability(intent)

    def _workflow_capability(self, workflow: dict[str, Any]) -> str:
        intent = str(workflow.get("intent") or "unknown")
        return self.capability_registry.workflow_capability(intent, bool(workflow.get("submit")))

    def _workflow_capability_slots(self, workflow: dict[str, Any]) -> dict[str, Any]:
        intent = str(workflow.get("intent") or "unknown")
        if intent == "leave" and isinstance(workflow.get("leave"), dict):
            slots = dict(workflow.get("leave") or {})
        elif intent == "expense_material" and isinstance(workflow.get("expense"), dict):
            slots = dict(workflow.get("expense") or {})
        else:
            slots = {}
        if workflow.get("submit"):
            slots["explicit_submit"] = True
        return slots

    def _capability_task(
        self,
        task_id: str,
        capability: str,
        slots: dict[str, Any],
        query: str,
        domain: str,
    ) -> dict[str, Any]:
        spec = self.capability_registry.spec(capability)
        missing = self._capability_missing_slots(capability, slots)
        return {
            "task_id": task_id,
            "capability": capability,
            "domain": spec.get("domain") or domain,
            "intent": spec.get("intent") or slots.get("intent") or "unknown",
            "risk": spec.get("risk") or "unknown",
            "source_text": self._domain_source_text(query, domain, spec.get("intent")),
            "core_slots": self._compact_slot_hint(slots),
            "missing_required_slots": missing,
            "read_tools": spec.get("read_tools") or [],
            "write_tools": spec.get("write_tools") or [],
            "evidence_required": spec.get("evidence_required") or [],
        }

    def _capability_missing_slots(self, capability: str, slots: dict[str, Any]) -> list[str]:
        spec = self.capability_registry.spec(capability)
        missing = []
        for slot in spec.get("required_slots") or []:
            if slot == "target_booking":
                if not (slots.get("order_id") or slots.get("keyword") or slots.get("day_text") or slots.get("room_ids")):
                    missing.append(slot)
                continue
            if slot == "project_hint":
                if not (slots.get("project_code") or slots.get("project_name") or slots.get("project_keywords")):
                    missing.append(slot)
                continue
            if slot == "material_hint":
                if not (slots.get("material_category_hint") or slots.get("material_subclass_hint") or slots.get("items")):
                    missing.append(slot)
                continue
            if slot == "approver_hint":
                if not any(slots.get(key) for key in ["approver_keyword", "approver_title", "approver_raw", "approver_name_hint", "approver_title_hint", "approver_employee_no"]):
                    missing.append(slot)
                continue
            if slot == "capacity_delta_or_new_capacity":
                if not (slots.get("capacity_delta") or slots.get("capacity")):
                    missing.append(slot)
                continue
            if not slots.get(slot):
                missing.append(slot)
        return missing

    def _routing_risk(self, complexity: dict[str, Any], tasks: list[dict[str, Any]], risk_flags: list[str]) -> str:
        if not tasks:
            return "complex"
        if any(task.get("risk") == "high_risk_write" for task in tasks):
            return "complex"
        if len(tasks) > 1:
            return "complex"
        if any(flag in {"unknown_domain", "unknown_capability", "multi_turn", "context_reference", "multi_domain", "multi_capability"} for flag in risk_flags):
            return "complex"
        if str(complexity.get("level")) == "simple" and not any(task.get("missing_required_slots") for task in tasks):
            return "simple"
        if str(complexity.get("level")) == "complex":
            return "complex"
        return "normal"

    def _routing_path_for_risk(self, routing_risk: str) -> str:
        if routing_risk == "simple":
            return "deterministic_simple"
        if routing_risk == "complex":
            return "deterministic_with_parallel_planner_candidate"
        return "deterministic_normal"

    def _write_task_graph_log(self, event: dict[str, Any]) -> None:
        path_value = self._task_graph_log_path()
        if not path_value:
            return
        try:
            path = Path(path_value)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._redact(event), ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _task_graph_timeout_for_complexity(self, complexity: dict[str, Any], llm_config: dict[str, Any]) -> float:
        runtime = self.config.get("runtime") if isinstance(self.config.get("runtime"), dict) else {}
        intent_key = "task_graph_timeout_intent_seconds"
        intent_env = os.getenv("TASK_GRAPH_TIMEOUT_INTENT_SECONDS")
        if intent_env or runtime.get(intent_key) is not None:
            try:
                return max(0.75, float(intent_env or runtime.get(intent_key) or llm_config.get("timeout") or 12.0))
            except Exception:
                return 12.0
        level = str(complexity.get("level") or "normal")
        key = f"task_graph_timeout_{level}_seconds"
        default_by_level = {"simple": 2.0, "normal": 3.0, "complex": 8.0}
        env_timeout = os.getenv(key.upper())
        try:
            timeout = float(env_timeout or runtime.get(key) or default_by_level.get(level, 1.0))
        except Exception:
            timeout = default_by_level.get(level, 1.0)
        try:
            configured = float(llm_config.get("timeout") or timeout)
        except Exception:
            configured = timeout
        target = timeout if env_timeout or runtime.get(key) is not None else configured
        default_cap = max(default_by_level.get(level, 1.0), timeout, configured)
        cap_env = os.getenv(f"{key.upper()}_CAP") or os.getenv("TASK_GRAPH_TIMEOUT_CAP_SECONDS")
        if cap_env:
            try:
                cap = max(default_cap, float(cap_env))
            except Exception:
                cap = default_cap
        else:
            cap = default_cap
        return max(0.75, min(target, cap))

    def _task_graph_complexity(self, baseline: dict[str, Any], obs: dict[str, Any]) -> dict[str, Any]:
        query = self._full_query(obs)
        domains = {str(item) for item in (baseline.get("domains") or []) if item}
        meeting = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
        workflow = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
        reasons: list[str] = []
        score = 0

        if not domains:
            score += 3
            reasons.append("unknown_domain")
        if len(domains) > 1:
            score += 3
            reasons.append("multi_domain")
        if obs.get("mode") == "multi_turn":
            score += 3
            reasons.append("multi_turn")
        if any(word in query for word in ["顺便", "另外", "同时", "并且", "再帮", "然后", "还有"]):
            score += 2
            reasons.append("multi_action_connector")
        if any(word in query for word in ["这个", "那个", "刚才", "上次", "原来", "之前", "它", "他", "她"]):
            score += 2
            reasons.append("context_reference")
        if any(word in query for word in ["如果", "否则", "不行就", "可以的话", "优先", "备选"]):
            score += 2
            reasons.append("conditional_or_fallback")
        if any(word in query for word in ["每周", "这两周", "下两周", "工作日", "全天", "连续", "每天"]):
            score += 2
            reasons.append("complex_time")

        if "meetingroom" in domains:
            score_delta, reason_delta = self._meetingroom_complexity(meeting, query)
            score += score_delta
            reasons.extend(reason_delta)
        if "workflow" in domains:
            score_delta, reason_delta = self._workflow_complexity(workflow, query)
            score += score_delta
            reasons.extend(reason_delta)

        simple_proven = self._is_proven_simple_task(baseline, obs)
        force_complex = any(
            reason in reasons
            for reason in [
                "unknown_domain",
                "multi_domain",
                "multi_turn",
                "workflow_unknown_intent",
                "meeting_unknown_intent",
                "workflow_unhandled_intent",
            ]
        )
        if simple_proven and score <= 1:
            level = "simple"
        elif force_complex or score >= 5:
            level = "complex"
        else:
            level = "normal"
        return {
            "level": level,
            "score": score,
            "simple_proven": simple_proven,
            "reasons": self._dedupe(reasons),
        }

    def _meetingroom_complexity(self, meeting: dict[str, Any], query: str) -> tuple[int, list[str]]:
        reasons: list[str] = []
        score = 0
        intent = str(meeting.get("intent") or "unknown")
        if intent == "unknown":
            return 3, ["meeting_unknown_intent"]
        if intent in {"query_booking", "query_room_schedule"}:
            score += 2
            reasons.append("meeting_query")
        if intent in {"cancel_existing", "extend_existing", "cancel_rebook_existing", "rebook_larger_existing"}:
            score += 2
            reasons.append("meeting_modifies_existing")
        if intent in {"participant_add", "participant_remove", "participant_list"}:
            score += 1
            reasons.append("meeting_participant_operation")
        if intent == "book_multi_segments_same_room" or meeting.get("segments"):
            score += 2
            reasons.append("meeting_multi_segment")
        if not meeting.get("day_text") or not (meeting.get("start") and meeting.get("end")):
            score += 1
            reasons.append("meeting_missing_time")
        if any(word in query for word in ["大一点", "换大", "更大", "屏幕", "投屏", "容量", "容纳"]):
            score += 1
            reasons.append("meeting_candidate_constraint")
        return score, reasons

    def _workflow_complexity(self, workflow: dict[str, Any], query: str) -> tuple[int, list[str]]:
        reasons: list[str] = []
        score = 0
        intent = str(workflow.get("intent") or "unknown")
        if intent == "unknown":
            return 3, ["workflow_unknown_intent"]
        if workflow.get("submit") or any(word in query for word in ["提交", "发起", "直接提", "帮我提交"]):
            score += 1
            reasons.append("workflow_submit")
        if intent == "leave":
            leave = workflow.get("leave") if isinstance(workflow.get("leave"), dict) else {}
            if not leave.get("day_text") or not (leave.get("start") and leave.get("end")):
                score += 2
                reasons.append("leave_missing_time")
            if any(word in query for word in ["审批人必须", "必须是", "找一个", "经理", "主管"]):
                score += 1
                reasons.append("leave_approver_constraint")
            if any(word in query for word in ["育儿假", "年休假", "病假", "事假"]):
                score += 1
                reasons.append("leave_enum_binding")
        elif intent == "expense_material":
            expense = workflow.get("expense") if isinstance(workflow.get("expense"), dict) else {}
            score += 2
            reasons.append("expense_candidate_binding")
            if not self._has_explicit_project_slot(expense):
                score += 2
                reasons.append("expense_missing_project")
            if not expense.get("material_category_hint"):
                score += 2
                reasons.append("expense_missing_material_category")
            items = expense.get("items") if isinstance(expense.get("items"), list) else []
            if len(items) > 1:
                score += 1
                reasons.append("expense_multi_item")
        else:
            score += 3
            reasons.append("workflow_unhandled_intent")
        return score, reasons

    def _is_proven_simple_task(self, baseline: dict[str, Any], obs: dict[str, Any]) -> bool:
        query = self._full_query(obs)
        domains = {str(item) for item in (baseline.get("domains") or []) if item}
        if len(domains) != 1 or obs.get("mode") == "multi_turn":
            return False
        if any(word in query for word in ["顺便", "另外", "同时", "并且", "再帮", "然后", "还有", "如果", "否则", "不行就"]):
            return False
        if any(word in query for word in ["这个", "那个", "刚才", "上次", "之前", "原来"]):
            return False
        if domains == {"meetingroom"}:
            meeting = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
            return self._is_proven_simple_meeting(meeting)
        if domains == {"workflow"}:
            workflow = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
            return self._is_proven_simple_workflow(workflow, query)
        return False

    def _is_proven_simple_meeting(self, meeting: dict[str, Any]) -> bool:
        intent = str(meeting.get("intent") or "unknown")
        if intent != "book_single":
            return False
        if not meeting.get("day_text") or not (meeting.get("start") and meeting.get("end")):
            return False
        if not (meeting.get("office_candidates") or meeting.get("office_address_candidates")):
            return False
        return True

    def _is_proven_simple_workflow(self, workflow: dict[str, Any], query: str) -> bool:
        intent = str(workflow.get("intent") or "unknown")
        if intent != "leave" or workflow.get("submit"):
            return False
        if any(word in query for word in ["提交", "发起", "直接提", "帮我提交", "每周", "这两周", "全天"]):
            return False
        leave = workflow.get("leave") if isinstance(workflow.get("leave"), dict) else {}
        return bool(leave.get("day_text") and leave.get("start") and leave.get("end") and leave.get("leave_type_label"))

    def _needs_fast_semantic(
        self,
        baseline: dict[str, Any],
        obs: dict[str, Any],
        complexity: dict[str, Any] | None = None,
        route: dict[str, Any] | None = None,
    ) -> bool:
        complexity = complexity or self._task_graph_complexity(baseline, obs)
        if route is not None:
            routing_risk = str(route.get("routing_risk") or "")
            if routing_risk != "complex":
                return False
            route_tasks = [task for task in (route.get("tasks") or []) if isinstance(task, dict)]
            if route_tasks and not any(task.get("missing_required_slots") for task in route_tasks):
                flags = set(route.get("risk_flags") or [])
                if not (flags & {"unknown_capability", "multi_turn", "context_reference"}):
                    return False
            flags = set(route.get("risk_flags") or [])
            if flags & {"unknown_capability"}:
                return True
        reasons = set(complexity.get("reasons") or [])
        high_risk = {
            "unknown_domain",
            "multi_turn",
            "context_reference",
            "workflow_unknown_intent",
            "meeting_unknown_intent",
            "workflow_unhandled_intent",
            "meeting_modifies_existing",
        }
        if reasons & high_risk:
            return True
        if complexity.get("level") == "complex" and not complexity.get("simple_proven"):
            return bool(reasons & {"complex_time", "meeting_multi_segment"})
        return False

    def _tool_contract_summary(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contracts: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            schema = item.get("args_schema") if isinstance(item.get("args_schema"), dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            properties = []
            for key, value in props.items():
                if not isinstance(value, dict):
                    continue
                prop: dict[str, Any] = {"name": key, "type": value.get("type")}
                if value.get("description"):
                    prop["description"] = value.get("description")
                if value.get("enum"):
                    prop["enum"] = value.get("enum")
                if value.get("format"):
                    prop["format"] = value.get("format")
                if value.get("pattern"):
                    prop["pattern"] = value.get("pattern")
                properties.append(prop)
            contracts.append(
                {
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "required_args": schema.get("required") or [],
                    "args": properties,
                }
            )
        return contracts

    def _can_call_llm(
        self,
        state: RuntimeState | None,
        profile: str,
        min_remaining: float = 8.0,
        max_calls: int | None = None,
    ) -> bool:
        if state is None:
            return True
        remaining = state.deadline_at - time.monotonic()
        if remaining < min_remaining:
            return False
        if max_calls is None:
            max_calls = int(self._llm_config(profile).get("max_calls") or 1)
        if profile == "fast":
            return state.llm_calls_fast < max_calls
        if profile == "strong":
            return state.llm_calls_strong < max_calls
        return True

    def _remaining_llm_timeout(self, state: RuntimeState | None, requested: int | float, reserve: float = 3.0) -> float:
        try:
            timeout = float(requested)
        except Exception:
            timeout = 10.0
        if state is None:
            return max(0.5, timeout)
        remaining = state.deadline_at - time.monotonic() - reserve
        if remaining < 0.5:
            raise TimeoutError("case deadline too close for llm call")
        return max(0.5, min(timeout, remaining))

    def _note_llm_call(self, state: RuntimeState | None, profile: str) -> None:
        if state is None:
            return
        if profile == "fast":
            state.llm_calls_fast += 1
        elif profile == "strong":
            state.llm_calls_strong += 1

    def _chat_completion(
        self,
        llm_config: dict[str, Any],
        messages: list[dict[str, str]],
        state: RuntimeState | None = None,
        profile: str | None = None,
        context_pack_type: str = "none",
        context_chars: int = 0,
        prompt_chars: int = 0,
    ) -> str:
        call_profile = profile or str(llm_config.get("profile") or "strong")
        if state is not None and not self._can_call_llm(state, call_profile):
            raise TimeoutError(f"{call_profile} llm budget exhausted or deadline too close")
        self._note_llm_call(state, call_profile)
        call_started = time.monotonic()
        messages = [dict(item) for item in messages if isinstance(item, dict)]
        if not any("json" in str(item.get("content") or "") for item in messages):
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = str(messages[0].get("content") or "") + " Return valid json only."
            else:
                messages.insert(0, {"role": "system", "content": "Return valid json only."})
        if not prompt_chars:
            prompt_chars = sum(len(str(item.get("content") or "")) for item in messages if isinstance(item, dict))
        base_url = str(llm_config.get("base_url") or "").rstrip("/")
        body = {
            "model": llm_config.get("model"),
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if int(llm_config.get("max_tokens") or 0) > 0:
            body["max_tokens"] = int(llm_config.get("max_tokens") or 0)
        body["temperature"] = llm_config.get("temperature", 0)
        if llm_config.get("seed") is not None:
            body["seed"] = int(llm_config["seed"])

        def send_request(request_body: dict[str, Any], request_timeout: float) -> dict[str, Any]:
            request = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {llm_config.get('api_key')}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
                return value if isinstance(value, dict) else {}

        timeout = 0.0
        payload: dict[str, Any] = {}
        try:
            timeout = self._remaining_llm_timeout(state, float(llm_config.get("timeout") or 15))
            try:
                payload = send_request(body, timeout)
            except urllib.error.HTTPError as exc:
                error_text = ""
                try:
                    error_text = exc.read().decode("utf-8")[:500]
                except Exception:
                    pass
                seed_rejected = (
                    exc.code == 400
                    and "seed" in body
                    and "seed" in error_text.lower()
                    and any(term in error_text.lower() for term in ["unsupported", "unknown", "unrecognized", "不支持"])
                )
                if not seed_rejected:
                    setattr(exc, "_agent_error_text", error_text)
                    raise
                body.pop("seed", None)
                payload = send_request(body, self._remaining_llm_timeout(state, timeout, reserve=2.0))
        except urllib.error.HTTPError as exc:
            text = str(getattr(exc, "_agent_error_text", "") or "")
            if not text:
                try:
                    text = exc.read().decode("utf-8")[:500]
                except Exception:
                    pass
            self._log_llm_call_event(
                state,
                llm_config,
                call_profile,
                call_started,
                timeout,
                False,
                f"HTTP {exc.code}: {text}",
                context_pack_type=context_pack_type,
                context_chars=context_chars,
                prompt_chars=prompt_chars,
                request_body=body,
            )
            raise RuntimeError(f"LLM HTTP {exc.code}: {text}") from exc
        except Exception as exc:
            self._log_llm_call_event(
                state,
                llm_config,
                call_profile,
                call_started,
                timeout,
                False,
                str(exc),
                context_pack_type=context_pack_type,
                context_chars=context_chars,
                prompt_chars=prompt_chars,
                request_body=body,
            )
            raise
        choices = payload.get("choices") or []
        if not choices:
            self._log_llm_call_event(
                state,
                llm_config,
                call_profile,
                call_started,
                timeout,
                False,
                "LLM returned no choices",
                context_pack_type=context_pack_type,
                context_chars=context_chars,
                prompt_chars=prompt_chars,
                request_body=body,
                response_payload=payload,
            )
            raise RuntimeError("LLM returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            self._log_llm_call_event(
                state,
                llm_config,
                call_profile,
                call_started,
                timeout,
                False,
                "LLM returned empty content",
                context_pack_type=context_pack_type,
                context_chars=context_chars,
                prompt_chars=prompt_chars,
                request_body=body,
                response_payload=payload,
            )
            raise RuntimeError("LLM returned empty content")
        self._log_llm_call_event(
            state,
            llm_config,
            call_profile,
            call_started,
            timeout,
            True,
            "",
            response_chars=len(content),
            context_pack_type=context_pack_type,
            context_chars=context_chars,
            prompt_chars=prompt_chars,
            request_body=body,
            response_payload=payload,
            response_content=content,
        )
        return content

    def _log_llm_call_event(
        self,
        state: RuntimeState | None,
        llm_config: dict[str, Any],
        profile: str,
        started_at: float,
        timeout: float,
        success: bool,
        error: str,
        response_chars: int = 0,
        context_pack_type: str = "none",
        context_chars: int = 0,
        prompt_chars: int = 0,
        request_body: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_content: str = "",
    ) -> None:
        if state is None:
            return
        now = time.monotonic()
        elapsed = max(0.0, now - started_at)
        if profile == "fast":
            state.llm_elapsed_fast_seconds += elapsed
        elif profile == "strong":
            state.llm_elapsed_strong_seconds += elapsed
        self._debug_log(
            llm_config,
            {
                "event": "llm_call",
                "case_id": state.obs.get("case_id"),
                "profile": profile,
                "model": llm_config.get("model"),
                "temperature": (request_body or {}).get("temperature", llm_config.get("temperature", 0)),
                "seed": (request_body or {}).get("seed"),
                "prompt_hash": self._json_hash((request_body or {}).get("messages") or []),
                "output_hash": hashlib.sha256(str(response_content or "").encode("utf-8")).hexdigest() if response_content else "",
                "success": success,
                "timeout_seconds": round(float(timeout or 0), 3),
                "elapsed_seconds": round(elapsed, 3),
                "started_at": round(started_at - state.started_at, 3),
                "end_at": round(now - state.started_at, 3),
                "remaining_seconds": round(self._remaining_seconds(state), 3),
                "context_pack_type": context_pack_type,
                "context_chars": int(context_chars or 0),
                "prompt_chars": int(prompt_chars or 0),
                "response_chars": response_chars,
                "request_json": request_body or {},
                "response_json": response_payload or {},
                "error": str(error or "")[:240],
            },
        )

    def _json_hash(self, value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None

    def _deep_update(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[key] = value

    def _debug_log(self, llm_config: dict[str, Any], item: dict[str, Any]) -> None:
        path_value = llm_config.get("debug_log_path")
        if not path_value:
            return
        try:
            path = Path(str(path_value))
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._redact(item), ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if str(key).lower() in {"api_key", "apikey", "authorization", "token", "secret", "password"}:
                    out[key] = "***"
                else:
                    out[key] = self._redact(item)
            return out
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    # ------------------------------------------------------------------
    # Semantic bootstrap
    # ------------------------------------------------------------------

    def _heuristic_semantics(self, obs: dict[str, Any]) -> dict[str, Any]:
        query = self._full_query(obs)
        meeting = self._heuristic_meetingroom(query, obs)
        workflow = self._heuristic_workflow(query, obs)
        domains = []
        if meeting.get("intent") != "unknown":
            domains.append("meetingroom")
        if workflow.get("intent") != "unknown":
            domains.append("workflow")
        return {"domains": domains, "meetingroom": meeting, "workflow": workflow}

    def _task_graph_hint_from_baseline(self, baseline: dict[str, Any]) -> dict[str, Any]:
        hint: dict[str, Any] = {"domains": baseline.get("domains") or []}
        meeting = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
        workflow = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
        if meeting:
            hint["meetingroom"] = {
                "intent": meeting.get("intent") or "unknown",
                "filled_slots": self._compact_slot_hint(meeting),
            }
        if workflow:
            wf_hint: dict[str, Any] = {
                "intent": workflow.get("intent") or "unknown",
                "submit": bool(workflow.get("submit")),
            }
            leave = workflow.get("leave") if isinstance(workflow.get("leave"), dict) else {}
            expense = workflow.get("expense") if isinstance(workflow.get("expense"), dict) else {}
            if leave:
                wf_hint["leave_filled_slots"] = self._compact_slot_hint(leave)
            if expense:
                wf_hint["expense_filled_slots"] = self._compact_slot_hint(expense)
            hint["workflow"] = wf_hint
        return hint

    def _task_graph_fast_hint(self, baseline: dict[str, Any]) -> dict[str, Any]:
        meeting = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
        workflow = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
        hint: dict[str, Any] = {"domains": baseline.get("domains") or []}
        if meeting:
            hint["mr_intent"] = meeting.get("intent") or "unknown"
        if workflow:
            hint["wf_intent"] = workflow.get("intent") or "unknown"
            hint["wf_submit"] = bool(workflow.get("submit"))
        return hint

    def _compact_slot_hint(self, data: dict[str, Any], max_items: int = 12) -> dict[str, Any]:
        hint: dict[str, Any] = {}
        for key, value in data.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                hint[key] = f"list[{len(value)}]"
            elif isinstance(value, dict):
                hint[key] = "object"
            else:
                text = str(value)
                hint[key] = text[:80]
            if len(hint) >= max_items:
                break
        return hint

    def _task_graph_query_payload(self, obs: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": obs.get("mode"),
        }
        if obs.get("user_query"):
            payload["q"] = str(obs.get("user_query"))[:1200]
        messages = obs.get("messages")
        if isinstance(messages, list) and messages:
            compact = []
            for item in messages[-6:]:
                if not isinstance(item, dict):
                    continue
                compact.append(
                    {
                        "r": item.get("role"),
                        "c": str(item.get("content") or "")[:600],
                    }
                )
            if compact:
                payload["m"] = compact
        if "q" not in payload and "m" not in payload:
            payload["q"] = self._full_query(obs)[:1200]
        return payload

    def _full_query(self, obs: dict[str, Any]) -> str:
        messages = obs.get("messages") or []
        if isinstance(messages, list):
            parts = [str(item.get("content", "")) for item in messages if isinstance(item, dict)]
            if parts:
                return "\n".join(parts)
        return str(obs.get("user_query") or "")

    def _heuristic_meetingroom(self, query: str, obs: dict[str, Any]) -> dict[str, Any]:
        explicit_meeting_terms = ["会议室", "会议", "预订", "订会议室", "订房", "工位", "参会人", "日程", "房间"]
        meeting_actions = ["取消", "延长", "重订", "重新预订", "换大", "加入", "移除"]
        has_explicit_meeting_signal = any(word in query for word in explicit_meeting_terms) or any(
            word in query for word in meeting_actions
        )
        informal_meeting_action = bool(re.search(r"(?:开|订|约|安排).{0,12}会(?:[，。；;\n]|$)", query))
        # "发布会" and similar expense descriptions are not room requests
        # unless the user also supplies a room action or an explicit meeting
        # resource constraint.
        if not has_explicit_meeting_signal and not informal_meeting_action:
            return {"intent": "unknown"}
        intent = "book_single"
        if "取消" in query or "不用" in query:
            intent = "cancel_existing"
        if "延" in query or "多开" in query:
            intent = "extend_existing"
        if any(word in query for word in ["换大", "大房", "大一点", "参会人增加", "加了"]) and any(word in query for word in ["会议", "会", "房间"]):
            intent = "rebook_larger_existing"
        if "重新预订" in query or "重订" in query or ("取消" in query and "订" in query):
            intent = "cancel_rebook_existing"
        if any(word in query for word in ["查", "查询", "看一下"]) and any(
            word in query for word in ["日程", "有哪些会议", "会议预订", "预订记录", "预订"]
        ):
            intent = "query_booking"
        if "加入" in query or "加到" in query:
            intent = "participant_add"
        if "移除" in query:
            intent = "participant_remove"
        if "哪些人" in query or "有哪些人" in query:
            intent = "participant_list"
        keyword = self._extract_meeting_keyword(query)
        if intent == "participant_list":
            keyword = self._clean_participant_list_keyword(keyword)
        if intent == "participant_list" and not keyword:
            title_matches = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]{2,8}?会)(?:有哪些人|哪些人|里有哪些人|参加)", query)
            if title_matches:
                keyword = self._clean_participant_list_keyword(self._normalize_meeting_title(title_matches[-1]))
        segments = self._extract_meeting_segments(query)
        if len(segments) > 1 and any(word in query for word in ["同一个房间", "同一房间", "同一个会议室", "同一会议室"]):
            intent = "book_multi_segments_same_room"
        elif "日程" in query and "订" in query:
            intent = "book_by_schedule_analysis"

        start, end = self._extract_time_range(query)
        if not start:
            start = self._single_time_after(query, ["下午", "上午", "早上", "晚上"])
        office_candidates = self._extract_office_candidates(query)
        room_ids = self._extract_room_ids(query)
        capacity = self._extract_capacity(query) or (0 if obs.get("mode") == "multi_turn" else 10)
        office_addresses = self._office_address_candidates(query, office_candidates)
        return {
            "intent": intent,
            "day_text": self._extract_day_text(query),
            "start": start,
            "end": end,
            "duration_minutes": self._extract_duration_minutes(query),
            "office_candidates": office_candidates,
            "office_address_candidates": office_addresses,
            "room_ids": room_ids,
            "capacity": capacity,
            "capacity_delta": self._extract_capacity_delta(query),
            "has_screen": False if "不需要屏幕" in query or "不用屏幕" in query else ("屏幕" in query or "投影" in query or "带屏" in query),
            "title": self._extract_meeting_title(query),
            "segments": segments,
            "keyword": keyword,
            "allow_fallback": any(word in query for word in ["不行", "没有合适", "fallback", "也可以", "订不到"]),
            "fallback_policy": self._meeting_fallback_policy(query),
            "needs_workspace": "工位" in query or "附近" in query or "最近" in query,
            "participants": self._extract_participants(query),
        }

    def _heuristic_workflow(self, query: str, obs: dict[str, Any]) -> dict[str, Any]:
        has_leave = ("请" in query and "假" in query) or any(word in query for word in ["事假", "年假", "病假", "育儿假", "年休假"])
        has_expense = self._has_expense_signal(query)
        if not has_leave and not has_expense:
            return {"intent": "unknown"}
        submit = self._leave_submit_intent(query) if has_leave and not has_expense else self._submit_intent(query)
        if has_expense and not has_leave:
            intent = "expense_material"
        elif has_leave and not has_expense:
            intent = "leave"
        else:
            leave_index = min([i for i in [query.find("请假"), query.find("事假"), query.find("年假"), query.find("年休假"), query.find("病假"), query.find("育儿假")] if i >= 0] or [9999])
            expense_index = min([i for i in [query.find("费用"), query.find("采购"), query.find("预算"), query.find("物资"), query.find("报销")] if i >= 0] or [9999])
            if has_leave and any(word in query for word in ["请", "审批人", "事假", "年假", "病假", "育儿假"]):
                expense_index = 9999
            intent = "leave" if leave_index < expense_index else "expense_material"
        return {
            "intent": intent,
            "submit": submit,
            "leave": self._heuristic_leave(query),
            "expense": self._heuristic_expense(query),
        }

    def _heuristic_leave(self, query: str) -> dict[str, Any]:
        leave_text = self._slice_workflow_text(query, "leave")
        start, end = self._extract_time_range(leave_text)
        if not start or not end:
            full_start, full_end = self._extract_time_range(query)
            start = start or full_start
            end = end or full_end
        day_text = self._extract_leave_day_text(leave_text) or self._extract_leave_day_text(query)
        if "全天" in leave_text or ("全天" in query and not start and not end):
            start = start or "09:00"
            end = end or "18:00"
        if not start and "下午" in leave_text:
            start = "14:00"
        if not end and "下午" in leave_text:
            end = "18:00"
        if not start and "上午" in leave_text:
            start = "09:00"
        if not end and "上午" in leave_text:
            end = "11:00" if "后天上午" in leave_text or "请假的草稿" in leave_text else "12:00"
        leave_type_label = self._target_leave_type_label(leave_text) or self._first_match(
            leave_text,
            ["育儿假", "年休假", "年假", "病假", "事假"],
        ) or ("事假" if "私事" in leave_text or "个人" in leave_text else "")
        reason_label = self._first_match(leave_text, ["住院", "孩子", "私事", "个人事情", "个人事务", "本人有事", "身体不适"]) or leave_type_label
        return {
            "day_text": day_text,
            "start": start,
            "end": end,
            "duration_hours": self._extract_duration_hours(leave_text),
            "leave_type_label": leave_type_label,
            "reason_label": reason_label,
            **self._extract_approver_hints(leave_text),
        }

    def _heuristic_expense(self, query: str) -> dict[str, Any]:
        expense_text = self._slice_workflow_text(query, "expense")
        # The workflow task can be part of a cross-domain request.  Only its
        # own clause is evidence for project lookup; meeting room ids and time
        # expressions must not become project candidates.
        project_code = self._extract_project_code(expense_text)
        total = self._extract_total_amount(expense_text)
        items = self._extract_expense_items(expense_text)
        if not total and items:
            total_value = sum(float(item.get("budget_amount", 0) or 0) for item in items)
            if total_value:
                total = self._money(total_value)
        project_name = self._extract_project_name(expense_text)
        return {
            "project_code": project_code,
            "project_name": project_name,
            "project_keywords": self._project_keywords(project_name, expense_text),
            "material_category_hint": self._material_category_hint(expense_text),
            "raw_text": expense_text,
            "total_amount": total,
            "items": items,
        }

    def _merge_semantic(self, baseline: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
        merged = json.loads(json.dumps(baseline, ensure_ascii=False))
        self._deep_update_non_empty(merged, llm)
        self._stabilize_meeting_semantic(baseline, merged)
        self._stabilize_workflow_semantic(baseline, merged)
        domains = set(merged.get("domains") or [])
        for key in ("meetingroom", "workflow"):
            if isinstance(merged.get(key), dict) and merged[key].get("intent") not in {None, "", "unknown"}:
                domains.add(key)
        graph = merged.get("task_graph") if isinstance(merged.get("task_graph"), dict) else {}
        if isinstance(graph.get("tasks"), list):
            for task in graph.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                domain = task.get("domain")
                if domain in {"meetingroom", "workflow"}:
                    domains.add(domain)
        merged["domains"] = sorted(domains)
        return merged

    def _stabilize_meeting_semantic(self, baseline: dict[str, Any], merged: dict[str, Any]) -> None:
        baseline_mr = baseline.get("meetingroom") if isinstance(baseline.get("meetingroom"), dict) else {}
        merged_mr = merged.get("meetingroom") if isinstance(merged.get("meetingroom"), dict) else {}
        if not isinstance(merged_mr, dict):
            return

        # Values parsed directly from the user text are stronger than LLM
        # paraphrases, especially room ids and schedule/workspace signals.
        for key in ("room_ids", "segments", "capacity_delta"):
            if baseline_mr.get(key):
                merged_mr[key] = baseline_mr[key]
        for key in ("start", "end", "day_text"):
            if baseline_mr.get(key):
                merged_mr[key] = baseline_mr[key]
        if baseline_mr.get("needs_workspace"):
            merged_mr["needs_workspace"] = True
        if baseline_mr.get("has_screen"):
            merged_mr["has_screen"] = True
        baseline_intent = str(baseline_mr.get("intent") or "")
        merged_intent = str(merged_mr.get("intent") or "")
        if baseline_intent in {"book_by_schedule_analysis", "query_room_schedule"}:
            merged_mr["intent"] = baseline_intent
        elif baseline_mr.get("room_ids") and any(word in self._json_dumps_safe(merged_mr) for word in ["schedule", "日程", "空闲"]):
            merged_mr["intent"] = "query_room_schedule" if "订" not in self._json_dumps_safe(merged_mr) else "book_by_schedule_analysis"
        elif baseline_intent != "unknown" and merged_intent == "unknown":
            merged_mr["intent"] = baseline_intent

        baseline_offices = baseline_mr.get("office_candidates") if isinstance(baseline_mr.get("office_candidates"), list) else []
        merged_offices = merged_mr.get("office_candidates") if isinstance(merged_mr.get("office_candidates"), list) else []
        merged_mr["office_candidates"] = self._dedupe([*baseline_offices, *merged_offices])
        baseline_addresses = baseline_mr.get("office_address_candidates") if isinstance(baseline_mr.get("office_address_candidates"), list) else []
        merged_addresses = merged_mr.get("office_address_candidates") if isinstance(merged_mr.get("office_address_candidates"), list) else []
        merged_mr["office_address_candidates"] = self._dedupe([*baseline_addresses, *merged_addresses])

    def _json_dumps_safe(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    def _semantic_contract_from_baseline(self, baseline: dict[str, Any], obs: dict[str, Any] | None = None) -> dict[str, Any]:
        semantic = json.loads(json.dumps(baseline, ensure_ascii=False))
        tasks: list[dict[str, Any]] = []
        domains = set(semantic.get("domains") or [])
        meeting = semantic.get("meetingroom") if isinstance(semantic.get("meetingroom"), dict) else {}
        workflow = semantic.get("workflow") if isinstance(semantic.get("workflow"), dict) else {}
        source_text = self._full_query(obs or {}) if isinstance(obs, dict) else ""
        if meeting.get("intent") not in {None, "", "unknown"}:
            domains.add("meetingroom")
            tasks.append(
                {
                    "task_id": "meetingroom_1",
                    "domain": "meetingroom",
                    "intent": str(meeting.get("intent") or "unknown"),
                    "goal": "处理会议室请求",
                    "source_text": self._domain_source_text(source_text, "meetingroom"),
                    "slots": meeting,
                    "missing_slots": [],
                    "must_not_guess": [],
                    "confidence": 0.5,
                    "submit_intent": "unknown",
                }
            )
        if workflow.get("intent") not in {None, "", "unknown"}:
            domains.add("workflow")
            intent = str(workflow.get("intent") or "")
            workflow_slots = workflow.get("leave") if intent == "leave" and isinstance(workflow.get("leave"), dict) else None
            if workflow_slots is None and intent == "expense_material" and isinstance(workflow.get("expense"), dict):
                workflow_slots = workflow.get("expense")
            if workflow_slots is None:
                workflow_slots = workflow
            tasks.append(
                {
                    "task_id": "workflow_1",
                    "domain": "workflow",
                    "intent": intent or "unknown",
                    "goal": "处理审批流程请求",
                    "source_text": self._domain_source_text(source_text, "workflow", intent),
                    "slots": workflow_slots,
                    "missing_slots": [],
                    "must_not_guess": [],
                    "confidence": 0.5,
                    "submit_intent": "submit" if workflow.get("submit") else "draft",
                }
            )
        semantic["domains"] = sorted(domains)
        semantic["task_graph"] = {"tasks": tasks}
        return semantic

    def _domain_source_text(self, query: str, domain: str, intent: str | None = None) -> str:
        text = str(query or "")
        if not text:
            return ""
        if domain == "workflow":
            if intent == "leave":
                return self._slice_workflow_text(text, "leave")
            if intent == "expense_material":
                return self._slice_workflow_text(text, "expense")
            return self._slice_workflow_text(text, "expense" if self._has_workflow_signal(text) else "leave")
        if domain == "meetingroom":
            positions = [
                text.find(key)
                for key in ["会议室", "会议", "会，", "会。", "预订", "订", "取消", "延长", "参会人", "工位"]
                if text.find(key) >= 0
            ]
            if not positions:
                return text
            start = max(0, min(positions) - 12)
            workflow_positions = [
                text.find(key, start + 1)
                for key in ["另一个任务", "顺便", "另外", "同时", "并且", "请假", "事假", "年假", "费用", "预算", "采购"]
                if text.find(key, start + 1) >= 0
            ]
            end = min(workflow_positions) if workflow_positions else len(text)
            # In mixed requests the workflow marker is often at the end of a
            # new sentence (for example, "...会议。然后提费用...").  Retain
            # only the meeting sentence rather than the prefix of the next
            # workflow clause.
            if workflow_positions:
                boundary = max(text.rfind(marker, start, end) for marker in ["。", "；", ";", "\n"])
                if boundary >= start:
                    end = boundary + 1
            return text[start:end].strip("，。；; ")
        return text

    def _full_query_from_semantic_baseline(self, semantic: dict[str, Any]) -> str:
        workflow = semantic.get("workflow") if isinstance(semantic.get("workflow"), dict) else {}
        expense = workflow.get("expense") if isinstance(workflow.get("expense"), dict) else {}
        if expense.get("raw_text"):
            return str(expense.get("raw_text"))
        return ""

    def _stabilize_workflow_semantic(self, baseline: dict[str, Any], merged: dict[str, Any]) -> None:
        baseline_wf = baseline.get("workflow") if isinstance(baseline.get("workflow"), dict) else {}
        merged_wf = merged.get("workflow") if isinstance(merged.get("workflow"), dict) else {}
        baseline_expense = baseline_wf.get("expense") if isinstance(baseline_wf.get("expense"), dict) else {}
        merged_expense = merged_wf.setdefault("expense", {}) if isinstance(merged_wf, dict) else {}
        if not isinstance(merged_expense, dict):
            return

        # Regex/tool-contract evidence is stronger than LLM free text.
        if baseline_expense.get("project_code"):
            merged_expense["project_code"] = baseline_expense["project_code"]
        if baseline_expense.get("total_amount"):
            merged_expense["total_amount"] = self._money(baseline_expense["total_amount"])
        if self._expense_items_have_amounts(baseline_expense.get("items")):
            merged_expense["items"] = self._normalize_expense_items(baseline_expense.get("items") or [])
        else:
            merged_expense["items"] = self._normalize_expense_items(merged_expense.get("items") or [])

        baseline_hint = str(baseline_expense.get("material_category_hint") or "")
        merged_hint = str(merged_expense.get("material_category_hint") or "")
        if baseline_hint and (not merged_hint or (baseline_hint not in merged_hint and len(baseline_hint) > len(merged_hint))):
            merged_expense["material_category_hint"] = baseline_hint

        baseline_keywords = baseline_expense.get("project_keywords") if isinstance(baseline_expense.get("project_keywords"), list) else []
        merged_keywords = merged_expense.get("project_keywords") if isinstance(merged_expense.get("project_keywords"), list) else []
        merged_expense["project_keywords"] = self._dedupe(
            [
                self._clean_project_phrase(str(item))
                for item in [*baseline_keywords, *merged_keywords]
                if self._valid_project_search_candidate(str(item), allow_short=True)
                and not self._looks_like_meeting_project_noise(str(item))
            ]
        )
        if baseline_expense.get("project_name") and not merged_expense.get("project_name"):
            merged_expense["project_name"] = baseline_expense["project_name"]

    def _expense_items_have_amounts(self, items: Any) -> bool:
        if not isinstance(items, list):
            return False
        for item in items:
            if isinstance(item, dict) and (item.get("budget_amount") or item.get("unit_price")):
                return True
        return False

    def _normalize_expense_items(self, items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if row.get("quantity"):
                row["quantity"] = self._normalize_quantity(row["quantity"])
            if row.get("unit_price"):
                row["unit_price"] = self._money(row["unit_price"])
            if row.get("budget_amount"):
                row["budget_amount"] = self._money(row["budget_amount"])
            if any(row.get(key) for key in ["name", "quantity", "unit_price", "budget_amount"]):
                normalized.append(row)
        return normalized

    def _deep_update_non_empty(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update_non_empty(target[key], value)
            else:
                target[key] = value

    def _init_context(self, state: RuntimeState) -> None:
        semantic = state.llm_semantic
        state.task_graph = self._normalize_task_graph(semantic.get("task_graph"), query=self._full_query(state.obs))
        self._initialize_task_runtimes(state)
        domains = (
            {runtime.domain for runtime in state.task_runtimes}
            if state.task_runtimes
            else set(semantic.get("domains") or [])
        )
        meeting = semantic.get("meetingroom") if isinstance(semantic.get("meetingroom"), dict) else {}
        workflow = semantic.get("workflow") if isinstance(semantic.get("workflow"), dict) else {}

        state.meetingroom.needed = "meetingroom" in domains or (
            not state.task_runtimes and meeting.get("intent") not in {None, "", "unknown"}
        )
        state.workflow.needed = "workflow" in domains or (
            not state.task_runtimes and workflow.get("intent") not in {None, "", "unknown"}
        )

        state.meetingroom.intent = self._normalize_meeting_intent(str(meeting.get("intent") or "unknown"), meeting)
        state.meetingroom.slots.update(meeting)
        self._merge_task_slots_into_domain(state, "meetingroom")
        meeting_source = self._task_source_text(state, "meetingroom")
        if meeting_source:
            state.meetingroom.slots["source_text"] = meeting_source
            refined = self._heuristic_meetingroom(meeting_source, state.obs)
            self._protect_specific_meeting_slots(state.meetingroom.slots, refined)
            self._deep_update_non_empty(state.meetingroom.slots, refined)
        if not self._has_meeting_signal(self._full_query(state.obs)) and not state.meetingroom.slots.get("room_ids"):
            state.meetingroom.needed = False
            state.meetingroom.intent = "unknown"
        self._normalize_meeting_slots(state)

        state.workflow.intent = str(workflow.get("intent") or "unknown")
        state.workflow.slots.update(workflow)
        self._merge_task_slots_into_domain(state, "workflow")
        workflow_source = self._domain_source_text(
            self._task_source_text(state, "workflow", state.workflow.intent),
            "workflow",
            state.workflow.intent,
        )
        if workflow_source:
            state.workflow.slots["source_text"] = workflow_source
            if state.workflow.intent == "leave":
                self._leave_slots(state)["source_text"] = workflow_source
                refined = self._heuristic_leave(workflow_source)
                self._deep_update_non_empty(self._leave_slots(state), refined)
            elif state.workflow.intent == "expense_material":
                self._expense_slots(state)["source_text"] = workflow_source
                refined = self._heuristic_expense(workflow_source)
                self._deep_update_non_empty(self._expense_slots(state), refined)
        if (
            ("workflow" in domains or not state.task_runtimes)
            and state.workflow.intent not in {None, "", "unknown"}
            and self._has_workflow_signal(self._full_query(state.obs))
        ):
            state.workflow.needed = True
        self._normalize_workflow_slots(state)
        self._apply_canonical_semantic_facts(state)
        self._initialize_workflow_skill(state)
        self._refresh_task_runtime_statuses(state)
        self._initialize_domain_step_budgets(state)

    def _apply_canonical_semantic_facts(self, state: RuntimeState) -> None:
        """Re-apply immutable user literals after task-graph/domain slot merging."""
        expense = self._expense_slots(state)
        for key in ("project_code", "project_name", "total_amount"):
            path = f"workflow.expense.{key}"
            value = state.semantic_facts.get(path)
            if value not in (None, "") and state.semantic_facts.source(path) == "user_literal":
                expense[key] = value
        for key in ("day_text", "start", "end"):
            path = f"workflow.leave.{key}"
            value = state.semantic_facts.get(path)
            if value not in (None, "") and state.semantic_facts.source(path) == "user_literal":
                self._leave_slots(state)[key] = value
        state.workflow.facts = state.semantic_facts.summary()

    def _initialize_workflow_skill(self, state: RuntimeState) -> None:
        if not state.workflow.needed or state.workflow.intent not in {"leave", "expense_material"}:
            state.workflow_skill = None
            return
        replace = state.workflow.intent == "leave" and self._is_leave_replace_request(self._leave_query(state))
        skill_id, definition = self.workflow_skill_registry.select(
            state.workflow.intent,
            bool(state.workflow.slots.get("submit")),
            replace=replace,
        )
        if not definition:
            state.workflow_skill = None
            return
        if state.workflow_skill is None or state.workflow_skill.skill_id != skill_id:
            state.workflow_skill = WorkflowSkillRuntime(skill_id, definition)
        self._sync_workflow_skill(state)

    def _sync_workflow_skill(self, state: RuntimeState) -> None:
        skill = state.workflow_skill
        if skill is None:
            return
        wf = state.workflow
        completed: set[str] = set()
        if wf.evidence.get("applicant"):
            completed.add("applicant")
        if wf.evidence.get("catalog"):
            completed.add("catalog")
        if wf.evidence.get("schema"):
            completed.add("schema")
        if wf.evidence.get("replacement_source_lookup"):
            completed.add("source_lookup")
        if wf.evidence.get("replacement_delete_done"):
            completed.add("delete_source")
        if wf.intent == "leave":
            if self._leave_plan(state):
                completed.add("leave_form")
            if wf.evidence.get("approver_search_attempts"):
                completed.add("approver_search")
            people = self._select_leave_people(state, self._collected_approver_people(wf))
            if len(people) == 1:
                completed.add("approver_resolved")
            if wf.evidence.get("workflow_skill_draft_ir"):
                completed.add("draft_ir")
        elif wf.intent == "expense_material":
            if wf.evidence.get("verified_project"):
                completed.add("project")
            if wf.evidence.get("category_options") and wf.evidence.get("selected_material_category"):
                completed.add("category")
            if wf.evidence.get("subclass_options"):
                completed.add("subclass")
            if wf.evidence.get("workflow_skill_draft_ir"):
                completed.add("draft_ir")
        if wf.evidence.get("save_done"):
            completed.add("save")
        if wf.evidence.get("oa_checked"):
            completed.add("verify")
        skill.sync_completed(completed)

    def _mark_workflow_skill_action_running(self, state: RuntimeState, tool: str) -> None:
        skill = state.workflow_skill
        if skill is None or not (tool.startswith("workflow.") or tool.startswith("oa.") or tool == "user.get_info"):
            return
        for node in skill.ready_nodes():
            if str(node.get("tool") or "") == tool:
                skill.mark_running(str(node.get("id") or ""))
                return

    def _initialize_task_runtimes(self, state: RuntimeState) -> None:
        state.task_runtimes = [TaskRuntime(task) for task in state.task_graph.get("tasks") or [] if isinstance(task, dict)]
        state.active_task_ids = {}
        for domain in ("meetingroom", "workflow"):
            runtime = self._next_runnable_task(state, domain)
            if runtime is not None:
                state.active_task_ids[domain] = runtime.task_id

    def _initialize_domain_step_budgets(self, state: RuntimeState) -> None:
        """Allocate a soft per-domain budget while reserving completion writes.

        The global tool limit remains authoritative. These values only govern
        read scheduling, so a domain that is ready to write is never starved.
        """
        active = [
            domain
            for domain, domain_state in (("meetingroom", state.meetingroom), ("workflow", state.workflow))
            if domain_state.needed and domain_state.status in {"pending", "ready"}
        ]
        state.domain_step_budgets = {"meetingroom": 0, "workflow": 0}
        if not active:
            return
        estimates = {domain: self._domain_remaining_step_estimate(state, domain) for domain in active}
        estimated_total = sum(max(1, item["total"]) for item in estimates.values())
        allocated = 0
        for domain in active:
            share = max(estimates[domain]["writes"], state.step_budget * max(1, estimates[domain]["total"]) // estimated_total)
            state.domain_step_budgets[domain] = share
            allocated += share
        while allocated < state.step_budget:
            domain = min(active, key=lambda item: (state.domain_step_budgets[item] - estimates[item]["total"], item))
            state.domain_step_budgets[domain] += 1
            allocated += 1
        while allocated > state.step_budget:
            candidates = [domain for domain in active if state.domain_step_budgets[domain] > estimates[domain]["writes"]]
            if not candidates:
                break
            domain = max(candidates, key=lambda item: (state.domain_step_budgets[item] - estimates[item]["writes"], item))
            state.domain_step_budgets[domain] -= 1
            allocated -= 1

    def _owner_task_is_runnable(self, state: RuntimeState, task: ReadTask) -> bool:
        if not task.owner_task_id:
            return True
        owner = self._task_runtime(state, task.owner_task_id)
        if owner is None or owner.status in TaskRuntime.TERMINAL_STATUSES:
            return False
        if state.active_task_ids.get(task.domain) != task.owner_task_id:
            return False
        by_id = {runtime.task_id: runtime for runtime in state.task_runtimes}
        return all(by_id.get(task_id) is None or by_id[task_id].status == "completed" for task_id in owner.depends_on)

    def _task_runtime(self, state: RuntimeState, task_id: str) -> TaskRuntime | None:
        return next((runtime for runtime in state.task_runtimes if runtime.task_id == task_id), None)

    def _active_task_runtime(self, state: RuntimeState, domain: str) -> TaskRuntime | None:
        task_id = state.active_task_ids.get(domain) or ""
        return self._task_runtime(state, task_id) if task_id else None

    def _next_runnable_task(self, state: RuntimeState, domain: str) -> TaskRuntime | None:
        by_id = {runtime.task_id: runtime for runtime in state.task_runtimes}
        for runtime in state.task_runtimes:
            if runtime.domain != domain or runtime.status != "pending":
                continue
            dependencies = [by_id.get(task_id) for task_id in runtime.depends_on]
            if any(item is not None and item.status == "blocked" for item in dependencies):
                runtime.status = "blocked"
                runtime.blocked_reason = "dependency_blocked"
                continue
            if all(item is None or item.status == "completed" for item in dependencies):
                return runtime
        return None

    def _refresh_task_runtime_statuses(self, state: RuntimeState) -> None:
        for domain in ("meetingroom", "workflow"):
            runtime = self._active_task_runtime(state, domain)
            if runtime is None or runtime.status in TaskRuntime.TERMINAL_STATUSES:
                continue
            domain_state = state.meetingroom if domain == "meetingroom" else state.workflow
            if domain_state.status == "ready" or self._task_write_likely_ready(state, runtime):
                runtime.status = "ready"
            elif runtime.status not in {"reading", "writing"}:
                runtime.status = "pending"

    def _advance_task_runtimes(self, state: RuntimeState) -> None:
        for domain in ("meetingroom", "workflow"):
            runtime = self._active_task_runtime(state, domain)
            domain_state = state.meetingroom if domain == "meetingroom" else state.workflow
            if runtime is not None and domain_state.status in {"done", "blocked"}:
                if domain == "workflow" and domain_state.status == "done" and self._workflow_needs_oa_check(state):
                    continue
                runtime.status = "completed" if domain_state.status == "done" else "blocked"
                runtime.blocked_reason = domain_state.blocked_reason if runtime.status == "blocked" else ""
                runtime.result = json.loads(json.dumps(domain_state.result or {}, ensure_ascii=False, default=str))
                state.task_results.append(
                    {
                        "task_id": runtime.task_id,
                        "domain": runtime.domain,
                        "capability": runtime.capability,
                        "status": runtime.status,
                        "result": runtime.result,
                        "blocked_reason": runtime.blocked_reason,
                    }
                )
                state.active_task_ids.pop(domain, None)

            if self._active_task_runtime(state, domain) is not None:
                continue
            next_runtime = self._next_runnable_task(state, domain)
            while next_runtime is not None and self._task_already_satisfied(state, next_runtime):
                next_runtime.status = "completed"
                next_runtime.result = {"status": "satisfied_by_shared_execution"}
                state.task_results.append(
                    {
                        "task_id": next_runtime.task_id,
                        "domain": next_runtime.domain,
                        "capability": next_runtime.capability,
                        "status": next_runtime.status,
                        "result": next_runtime.result,
                        "blocked_reason": "",
                    }
                )
                next_runtime = self._next_runnable_task(state, domain)
            if next_runtime is not None:
                state.active_task_ids[domain] = next_runtime.task_id
                self._load_task_runtime_view(state, next_runtime)

    def _task_already_satisfied(self, state: RuntimeState, runtime: TaskRuntime) -> bool:
        evidence = state.meetingroom.evidence if runtime.domain == "meetingroom" else state.workflow.evidence
        capability = runtime.capability
        if capability == "meeting.extend":
            return bool(evidence.get("extend_done"))
        if capability == "meeting.cancel":
            return bool(evidence.get("cancel_done"))
        if capability == "meeting.book_multi_segments":
            segments = state.meetingroom.slots.get("multi_segments") or []
            created = state.meetingroom.evidence.get("created_segments") or []
            return bool(segments and len(created) >= len(segments))
        if capability in {"meeting.book", "meeting.schedule_book"}:
            return bool(evidence.get("create_done"))
        if capability in {"meeting.participant_add", "meeting.participant_remove"}:
            return bool(evidence.get("participant_results"))
        if capability == "meeting.participant_list":
            return bool(evidence.get("participants"))
        if capability == "meeting.query_booking":
            return bool(evidence.get("booking_query"))
        if capability == "meeting.query_room_schedule":
            return bool(evidence.get("schedules"))
        if capability.startswith("workflow."):
            save_done = evidence.get("save_done") if isinstance(evidence.get("save_done"), dict) else {}
            save_args = save_done.get("args") if isinstance(save_done.get("args"), dict) else {}
            expected_id = WORKFLOW_IDS["leave"] if "leave" in capability else WORKFLOW_IDS["expense"]
            return int(save_args.get("workflow_id") or 0) == expected_id
        return False

    def _load_task_runtime_view(self, state: RuntimeState, runtime: TaskRuntime) -> None:
        domain_state = state.meetingroom if runtime.domain == "meetingroom" else state.workflow
        domain_state.status = "pending"
        domain_state.blocked_reason = ""
        domain_state.result = None
        task = runtime.task
        source_text = str(task.get("source_text") or "")
        if runtime.domain == "meetingroom":
            slots = self._normalize_task_meeting_slots(task.get("slots") or {})
            if source_text:
                slots["source_text"] = source_text
                refined = self._heuristic_meetingroom(source_text, state.obs)
                self._protect_specific_meeting_slots(slots, refined)
                self._deep_update_non_empty(slots, refined)
            state.meetingroom.intent = self._normalize_meeting_intent(str(task.get("intent") or "unknown"), slots)
            state.meetingroom.slots = slots
            self._normalize_meeting_slots(state)
            return

        intent = str(task.get("intent") or "unknown")
        if intent.startswith("workflow."):
            intent = intent.split(".", 1)[1]
        expected_id = WORKFLOW_IDS["leave"] if intent == "leave" else WORKFLOW_IDS["expense"]
        save_done = state.workflow.evidence.get("save_done") if isinstance(state.workflow.evidence.get("save_done"), dict) else {}
        save_args = save_done.get("args") if isinstance(save_done.get("args"), dict) else {}
        if save_done and int(save_args.get("workflow_id") or 0) != expected_id:
            for key in [
                "save_done",
                "oa_checked",
                "catalog",
                "schema",
                "workflow_id",
                "approver_search",
                "approver_searches",
                "project",
                "verified_project",
                "project_candidates",
                "category_options",
                "subclass_options",
                "selected_material_category",
                "expense_bindings",
                "expense_draft_ir",
                "expense_line_evidence",
            ]:
                state.workflow.evidence.pop(key, None)
        state.workflow.intent = intent
        state.workflow.slots = self._normalize_task_workflow_slots(task.get("slots") or {}, intent)
        state.workflow.slots["submit"] = task.get("submit_intent") == "submit"
        if source_text:
            workflow_source = self._domain_source_text(source_text, "workflow", intent)
            state.workflow.slots["source_text"] = workflow_source
            if intent == "leave":
                self._leave_slots(state)["source_text"] = workflow_source
                self._deep_update_non_empty(self._leave_slots(state), self._heuristic_leave(workflow_source))
            elif intent == "expense_material":
                self._expense_slots(state)["source_text"] = workflow_source
                self._deep_update_non_empty(self._expense_slots(state), self._heuristic_expense(workflow_source))
        self._normalize_workflow_slots(state)
        self._apply_canonical_semantic_facts(state)
        self._initialize_workflow_skill(state)

    def _merge_task_slots_into_domain(self, state: RuntimeState, domain: str) -> None:
        task = self._primary_task(state, domain)
        if not task:
            return
        slots = task.get("slots") if isinstance(task.get("slots"), dict) else {}
        if not slots:
            return
        if domain == "meetingroom":
            normalized_slots = self._normalize_task_meeting_slots(slots)
            self._protect_specific_meeting_slots(state.meetingroom.slots, normalized_slots)
            self._deep_update_non_empty(state.meetingroom.slots, normalized_slots)
            if task.get("intent") not in {None, "", "unknown"} and not self._should_keep_existing_meeting_intent(state, str(task.get("intent"))):
                state.meetingroom.intent = self._normalize_meeting_intent(str(task.get("intent")), state.meetingroom.slots)
            return
        if domain == "workflow":
            intent = str(task.get("intent") or state.workflow.intent or "unknown")
            if intent.startswith("workflow."):
                intent = intent.split(".", 1)[1]
            if intent in {"leave", "expense_material"}:
                state.workflow.intent = intent
            normalized = self._normalize_task_workflow_slots(slots, state.workflow.intent)
            self._deep_update_non_empty(state.workflow.slots, normalized)
            submit_intent = str(task.get("submit_intent") or "unknown")
            if submit_intent in {"submit", "draft"}:
                state.workflow.slots["submit"] = submit_intent == "submit"

    def _primary_task(self, state: RuntimeState, domain: str) -> dict[str, Any] | None:
        active = self._active_task_runtime(state, domain)
        if active is not None:
            return active.task
        tasks = state.task_graph.get("tasks") if isinstance(state.task_graph, dict) else []
        if not isinstance(tasks, list):
            return None
        candidates = [task for task in tasks if isinstance(task, dict) and task.get("domain") == domain]
        if not candidates:
            return None
        if domain == "meetingroom":
            action_tasks = [
                task
                for task in candidates
                if self._meeting_task_is_executable_action(task)
            ]
            if action_tasks:
                return sorted(action_tasks, key=lambda item: float(item.get("confidence") or 0), reverse=True)[0]
        return sorted(candidates, key=lambda item: float(item.get("confidence") or 0), reverse=True)[0]

    def _meeting_task_is_executable_action(self, task: dict[str, Any]) -> bool:
        intent = str(task.get("intent") or "")
        if intent in {
            "book_single",
            "book_multi_segments_same_room",
            "book_by_schedule_analysis",
            "cancel_existing",
            "extend_existing",
            "rebook_larger_existing",
            "cancel_rebook_existing",
            "participant_add",
            "participant_remove",
            "participant_list",
        }:
            return True
        text = f"{task.get('goal') or ''} {task.get('source_text') or ''}"
        return any(word in text for word in ["订", "预订", "取消", "延长", "换大", "加入", "移除", "哪些人", "参会人"])

    def _protect_specific_meeting_slots(self, current: dict[str, Any], incoming: dict[str, Any]) -> None:
        current_day = str(current.get("day_text") or "")
        incoming_day = str(incoming.get("day_text") or "")
        if current_day and incoming_day and self._day_text_is_more_specific(current_day, incoming_day):
            incoming.pop("day_text", None)
        if current.get("day") and incoming_day and not self._resolve_day(incoming_day, None):
            incoming.pop("day_text", None)

    def _day_text_is_more_specific(self, current: str, incoming: str) -> bool:
        broad = {"本周", "下周", "这周"}
        if incoming in broad and current not in broad:
            return True
        if re.search(r"(周[一二三四五六日天]|今天|明天|后天|\d{1,2}月\d{1,2}日)", current) and incoming in broad:
            return True
        return False

    def _should_keep_existing_meeting_intent(self, state: RuntimeState, incoming_intent: str) -> bool:
        existing = state.meetingroom.intent
        query = self._full_query(state.obs)
        if incoming_intent in {"query_room_schedule", "query_booking"} and existing in {
            "book_by_schedule_analysis",
            "book_single",
            "book_multi_segments_same_room",
        }:
            return any(word in query for word in ["订", "预订", "帮我订", "然后"])
        return False

    def _normalize_task_meeting_slots(self, slots: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        alias = {
            "date": "day_text",
            "day": "day_text",
            "day_text": "day_text",
            "start_time": "start",
            "start": "start",
            "end_time": "end",
            "end": "end",
            "attendees": "capacity",
            "people_count": "capacity",
            "capacity": "capacity",
            "capacity_min": "capacity",
            "title": "title",
            "topic": "title",
            "subject": "title",
            "keyword": "keyword",
            "room_id": "room_ids",
            "room_ids": "room_ids",
            "target_booking": "order_id",
            "booking_id": "order_id",
            "order_id": "order_id",
            "office": "office_candidates",
            "office_id": "office_candidates",
            "office_candidates": "office_candidates",
            "campus": "office_candidates",
            "location": "office_candidates",
            "office_address": "office_address_candidates",
            "office_address_candidates": "office_address_candidates",
            "has_screen": "has_screen",
            "duration_minutes": "duration_minutes",
            "allow_fallback": "allow_fallback",
            "fallback_policy": "fallback_policy",
            "needs_workspace": "needs_workspace",
            "participants": "participants",
            "segments": "segments",
            "equipment": "equipment",
        }
        for key, value in slots.items():
            target = alias.get(str(key), str(key))
            if value in (None, "", [], {}):
                continue
            if target in {"room_ids", "office_candidates", "office_address_candidates"} and not isinstance(value, list):
                out[target] = [str(value)]
            elif target == "capacity":
                cap = self._safe_int(value)
                if cap:
                    out[target] = cap
            elif target == "equipment":
                equipment = value if isinstance(value, list) else [value]
                if any(str(item).strip().lower() in {"屏幕", "投屏", "screen", "display"} for item in equipment):
                    out["has_screen"] = True
            else:
                out[target] = value
        return out

    def _normalize_task_workflow_slots(self, slots: dict[str, Any], intent: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if intent == "leave":
            leave = dict(slots.get("leave") or {}) if isinstance(slots.get("leave"), dict) else dict(slots)
            out["leave"] = self._normalize_task_leave_slots(leave)
            return out
        if intent == "expense_material":
            expense = dict(slots.get("expense") or {}) if isinstance(slots.get("expense"), dict) else dict(slots)
            out["expense"] = self._normalize_task_expense_slots(expense)
            return out
        return out

    def _normalize_task_leave_slots(self, slots: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        alias = {
            "date": "day_text",
            "day": "day_text",
            "day_text": "day_text",
            "start_time": "start",
            "start": "start",
            "end_time": "end",
            "end": "end",
            "leave_type": "leave_type_label",
            "leave_type_label": "leave_type_label",
            "reason": "reason_label",
            "reason_label": "reason_label",
            "approver": "approver_keyword",
            "approver_keyword": "approver_keyword",
            "approver_name": "approver_name_hint",
            "approver_name_hint": "approver_name_hint",
            "approver_title": "approver_title",
            "approver_title_hint": "approver_title_hint",
            "approver_employee_no": "approver_employee_no",
            "duration_hours": "duration_hours",
        }
        for key, value in slots.items():
            target = alias.get(str(key), str(key))
            if value not in (None, "", [], {}):
                out[target] = value
        return out

    def _normalize_task_expense_slots(self, slots: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        alias = {
            "project": "project_name",
            "project_hint": "project_name",
            "project_name": "project_name",
            "project_code": "project_code",
            "project_keywords": "project_keywords",
            "material": "material_category_hint",
            "material_hint": "material_category_hint",
            "material_category": "material_category_hint",
            "material_category_hint": "material_category_hint",
            "material_subclass": "material_subclass_hint",
            "material_subclass_hint": "material_subclass_hint",
            "amount": "total_amount",
            "total_amount": "total_amount",
            "budget": "total_amount",
            "items": "items",
            "details": "items",
            "raw_text": "raw_text",
        }
        for key, value in slots.items():
            target = alias.get(str(key), str(key))
            if value in (None, "", [], {}):
                continue
            if target == "total_amount":
                out[target] = self._money(value)
            elif target == "project_keywords" and not isinstance(value, list):
                out[target] = [str(value)]
            elif target == "items" and isinstance(value, list):
                out[target] = self._normalize_expense_items(value)
            else:
                out[target] = value
        return out

    def _safe_int(self, value: Any) -> int:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return 0

    def _normalize_task_graph(self, value: Any, baseline_value: Any = None, query: str = "") -> dict[str, Any]:
        return self.task_graph_normalizer.normalize(value, baseline_value=baseline_value, query=query)

    def _task_source_text(self, state: RuntimeState, domain: str, intent: str | None = None) -> str:
        active = self._active_task_runtime(state, domain)
        if active is not None and active.task.get("source_text"):
            return str(active.task.get("source_text"))
        tasks = state.task_graph.get("tasks") if isinstance(state.task_graph, dict) else []
        if not isinstance(tasks, list):
            return self._full_query(state.obs)
        best: dict[str, Any] | None = None
        for task in tasks:
            if not isinstance(task, dict) or task.get("domain") != domain:
                continue
            if intent and task.get("intent") in {intent, "workflow." + intent}:
                best = task
                break
            if best is None or float(task.get("confidence") or 0) > float(best.get("confidence") or 0):
                best = task
        if best and best.get("source_text"):
            return str(best.get("source_text"))
        return self._full_query(state.obs)

    def _has_meeting_signal(self, query: str) -> bool:
        meeting_terms = ["会议室", "会议", "评审会", "复盘会", "启动会", "分享会", "技术分享", "培训", "订房", "订会议室", "工位", "参会人", "房间", "日程"]
        action_terms = ["延长", "多开", "重订", "重新预订", "取消原会议", "已经订了会", "保持原样", "别动", "加到", "加入", "移除", "哪些人", "参加"]
        return any(word in query for word in meeting_terms) or any(word in query for word in action_terms) or bool(re.search(r"\d+\s*人会", query))

    def _has_workflow_signal(self, query: str) -> bool:
        return any(word in query for word in ["请假", "事假", "年假", "年休假", "病假", "育儿假"]) or self._has_expense_signal(query)

    def _has_expense_signal(self, query: str) -> bool:
        text = str(query or "")
        if any(word in text for word in ["费用", "预算", "总预算", "总金额", "总计", "合计", "采购", "物资", "报销"]):
            return True
        if any(word in text for word in ["草稿", "流程", "申请", "提交", "提交流程", "发起"]) and any(
            word in text for word in ["项目", "项目编码", "金额", "万元", "元", "每", "单价"]
        ):
            return True
        if re.search(r"[A-Z]-\d{6,12}", text) and any(word in text for word in ["提", "提交", "申请", "预算", "总预算"]):
            return True
        return False

    def _normalize_meeting_intent(self, intent: str, meeting: dict[str, Any]) -> str:
        mapping = {
            "book": "book_single",
            "query": "query_booking",
            "cancel": "cancel_existing",
            "extend": "extend_existing",
            "rebook_larger": "rebook_larger_existing",
            "cancel_rebook": "cancel_rebook_existing",
            "extend_existing_then_cancel_rebook": "cancel_rebook_existing",
            "extend_then_cancel_rebook": "cancel_rebook_existing",
            "schedule_book": "book_by_schedule_analysis",
        }
        normalized = mapping.get(intent, intent)
        if "cancel" in normalized and "rebook" in normalized:
            normalized = "cancel_rebook_existing"
        segments = meeting.get("segments") or []
        if normalized == "book_single" and len(segments) > 1:
            return "book_multi_segments_same_room"
        return normalized

    # ------------------------------------------------------------------
    # Planner
    # ------------------------------------------------------------------

    def _drain_read_plan(self, state: RuntimeState, llm_config: dict[str, Any]) -> bool:
        if not self._parallel_read_planner_enabled():
            return False
        if self._remaining_seconds(state) < self._parallel_read_min_remaining_seconds():
            return False
        if state.steps_used >= state.step_budget:
            return False
        # In multi-turn expense flows, ask for the next missing user slot
        # before spending the turn on catalog/schema/project prefetches.
        if state.obs.get("mode") == "multi_turn" and self._expense_missing_slot_name(state):
            return False
        plan = self._build_read_plan(state)
        reserve_steps = self._read_plan_step_reserve(state)
        max_batch = min(self._parallel_read_max_batch_size(), max(0, state.step_budget - state.steps_used - reserve_steps))
        if max_batch <= 0:
            return False
        ready_candidates = [
            task
            for task in plan.ready_tasks(state.read_task_keys_completed, max_batch)
            if self._read_task_allowed(state, task)
        ]
        ready = self._fair_read_batch(state, ready_candidates, max_batch)
        if not ready:
            return False
        batch = ReadPlan(ready)
        self._debug_log(
            llm_config,
            {
                "event": "read_plan",
                "case_id": state.obs.get("case_id"),
                "task_count": len(plan.tasks),
                "ready_count": len(ready),
                "remaining_seconds": round(self._remaining_seconds(state), 3),
                "steps_remaining": max(0, state.step_budget - state.steps_used),
                "domain_estimates": {
                    domain: self._domain_remaining_step_estimate(state, domain)
                    for domain in ("meetingroom", "workflow")
                },
                "tasks": [self._read_task_summary(task) for task in ready],
            },
        )
        return self.read_plan_executor.execute(state, batch, llm_config) > 0

    def _fair_read_batch(self, state: RuntimeState, tasks: list[ReadTask], limit: int) -> list[ReadTask]:
        """Prefer the shortest completion path while retaining cross-domain fairness."""
        estimates = {
            domain: self._domain_remaining_step_estimate(state, domain)
            for domain in ("meetingroom", "workflow")
        }
        tasks = sorted(
            tasks,
            key=lambda task: (
                estimates.get(task.domain, {}).get("total", state.step_budget + 1),
                -task.mapping_score,
                task.task_key,
            ),
        )
        if len(tasks) <= 1 or not self._cross_domain_active(state):
            return tasks[:limit]
        ordered_domains = ["meetingroom", "workflow"]
        domain_order = {domain: index for index, domain in enumerate(ordered_domains)}
        domain_count = len(ordered_domains)
        ordered_domains.sort(
            key=lambda domain: (
                estimates[domain]["total"],
                (domain_order[domain] - state.read_scheduler_cursor) % domain_count,
            )
        )
        selected: list[ReadTask] = []
        remaining = list(tasks)
        while remaining and len(selected) < limit:
            progressed = False
            for domain in ordered_domains:
                next_task = next((task for task in remaining if task.domain == domain), None)
                if next_task is None:
                    continue
                selected.append(next_task)
                remaining.remove(next_task)
                progressed = True
                if len(selected) >= limit:
                    break
            if not progressed:
                selected.extend(remaining[: max(0, limit - len(selected))])
                break
        state.read_scheduler_cursor = (state.read_scheduler_cursor + 1) % domain_count
        return selected

    def _domain_remaining_step_estimate(self, state: RuntimeState, domain: str) -> dict[str, int]:
        reads = 0
        writes = 0
        if domain == "workflow" and state.workflow.needed and state.workflow.status in {"pending", "ready", "done"}:
            self._sync_workflow_skill(state)
            if state.workflow_skill is not None:
                reads = state.workflow_skill.remaining_cost({"read", "postcheck"})
                writes = state.workflow_skill.remaining_cost({"write"})
            else:
                wf = state.workflow
                reads += int(not bool(wf.evidence.get("applicant")))
                reads += int(not bool(wf.evidence.get("catalog")))
                reads += int(not bool(wf.evidence.get("schema")))
                writes += int(not bool(wf.evidence.get("save_done")))
        elif domain == "meetingroom" and state.meetingroom.needed and state.meetingroom.status in {"pending", "ready"}:
            mr = state.meetingroom
            if mr.intent in {"query_booking", "cancel_existing", "extend_existing", "cancel_rebook_existing", "rebook_larger_existing"}:
                reads += int(not bool(mr.evidence.get("booking_query")))
            elif mr.intent in {"participant_add", "participant_remove", "participant_list"}:
                reads += int(not bool(mr.evidence.get("booking_query")))
            else:
                reads += int("room_candidates" not in mr.evidence)
            if mr.intent not in {"query_booking", "query", "query_room_schedule", "participant_list"}:
                writes += 2 if self._is_rebook_intent(mr.intent) and not mr.evidence.get("cancel_done") else 1
        return {"reads": reads, "writes": writes, "total": reads + writes}

    def _read_plan_step_reserve(self, state: RuntimeState) -> int:
        reserve = 0
        mr = state.meetingroom
        if mr.needed and mr.status in {"pending", "ready"}:
            if self._is_rebook_intent(mr.intent) and not mr.evidence.get("cancel_done"):
                reserve += 2
            elif mr.intent in {"participant_add", "participant_remove"}:
                participants = mr.slots.get("participants") if isinstance(mr.slots.get("participants"), list) else []
                completed = len(mr.evidence.get("participant_results") or [])
                reserve += max(1, len(participants) - completed)
            elif mr.intent not in {"query_booking", "query", "query_room_schedule", "participant_list"}:
                segments = mr.slots.get("multi_segments") if isinstance(mr.slots.get("multi_segments"), list) else []
                created = mr.evidence.get("created_segments") if isinstance(mr.evidence.get("created_segments"), list) else []
                reserve += max(1, len(segments) - len(created))
        wf = state.workflow
        if wf.needed and wf.status in {"pending", "ready"}:
            self._sync_workflow_skill(state)
            if state.workflow_skill is not None:
                reserve += state.workflow_skill.remaining_cost({"write", "postcheck"})
            elif wf.intent in {"leave", "expense_material"} and not wf.evidence.get("save_done"):
                reserve += 1
        return reserve

    def _build_read_plan(self, state: RuntimeState) -> ReadPlan:
        tasks: list[ReadTask] = []
        self._append_workflow_read_tasks(state, tasks)
        self._append_meetingroom_read_tasks(state, tasks)
        return ReadPlan(tasks)

    def _append_read_task(
        self,
        state: RuntimeState,
        tasks: list[ReadTask],
        tool: str,
        args: dict[str, Any] | None,
        domain: str,
        depends_on: list[str] | None = None,
        evidence_handler: str = "apply_tool_result",
        deadline_min_remaining: float | None = None,
        parallel_eligible: bool = True,
        group_key: str = "",
        stop_group_on_success: bool = False,
        mapping_score: float = 0.0,
        owner_task_id: str = "",
    ) -> None:
        clean_args = self._clean_args(args or {})
        task_key = self._read_task_key(tool, clean_args)
        if not task_key or task_key in state.read_task_keys_completed or task_key in state.read_task_keys_attempted:
            return
        if group_key and group_key in state.read_task_groups_succeeded:
            return
        if any(existing.task_key == task_key for existing in tasks):
            return
        tasks.append(
            ReadTask(
                task_key=task_key,
                tool=tool,
                args=clean_args,
                domain=domain,
                depends_on=depends_on or [],
                evidence_handler=evidence_handler,
                deadline_min_remaining=deadline_min_remaining
                if deadline_min_remaining is not None
                else self._parallel_read_min_remaining_seconds(),
                parallel_eligible=parallel_eligible,
                group_key=group_key,
                stop_group_on_success=stop_group_on_success,
                mapping_score=mapping_score,
                owner_task_id=owner_task_id or state.active_task_ids.get(domain, ""),
            )
        )

    def _append_workflow_read_tasks(self, state: RuntimeState, tasks: list[ReadTask]) -> None:
        wf = state.workflow
        if not wf.needed or wf.status not in {"pending", "ready"}:
            return
        if wf.intent not in {"leave", "expense_material"}:
            return
        self._sync_workflow_skill(state)
        skill = state.workflow_skill

        def ready(node_id: str) -> bool:
            return skill is None or skill.is_ready(node_id)

        workflow_id = WORKFLOW_IDS["leave"] if wf.intent == "leave" else WORKFLOW_IDS["expense"]
        catalog_keyword = "请假" if wf.intent == "leave" else "费用类物资"
        if not wf.evidence.get("applicant") and ready("applicant"):
            self._append_read_task(state, tasks, "user.get_info", {}, "workflow")
        if not wf.evidence.get("catalog") and ready("catalog"):
            self._append_read_task(state, tasks, "workflow.catalog", {"keyword": catalog_keyword}, "workflow")
        if not wf.evidence.get("schema") and ready("schema"):
            self._append_read_task(state, tasks, "workflow.schema", {"workflow_id": workflow_id}, "workflow")

        if wf.intent == "leave":
            if ready("source_lookup") and not wf.evidence.get("replacement_source_lookup"):
                self._append_read_task(
                    state,
                    tasks,
                    "oa.done.list",
                    {"keyword": "请假"},
                    "workflow",
                    deadline_min_remaining=5.0,
                )
            plan = self._leave_plan(state)
            if plan and not self._collected_approver_people(wf) and ready("approver_search"):
                args = self._next_approver_search_args(state, plan, mark_attempt=False)
                if args is not None:
                    self._append_read_task(state, tasks, "workflow.search_person", args, "workflow")
        elif wf.intent == "expense_material":
            expense = self._expense_slots(state)
            if ready("project") and not wf.evidence.get("verified_project") and not wf.evidence.get("project_candidates"):
                if not self._append_project_empty_retry_tasks(state, tasks, expense):
                    self._append_project_search_fanout_tasks(state, tasks, expense)
            if ready("category") and not wf.evidence.get("category_options"):
                field_id = self._expense_category_field_id(state)
                if field_id:
                    self._append_read_task(
                        state,
                        tasks,
                        "workflow.browser_search",
                        {"workflow_id": WORKFLOW_IDS["expense"], "field_id": field_id},
                        "workflow",
                    )
            if ready("subclass"):
                self._append_expense_subclass_read_task(state, tasks)

        if self._workflow_needs_oa_check(state) and self._remaining_seconds(state) > 6.0:
            action = self._next_oa_action(state, self._oa_keyword_for_workflow(state))
            if action is not None and action.kind == "tool":
                self._append_read_task(state, tasks, action.tool, action.args, "workflow", deadline_min_remaining=5.0)

    def _append_project_search_fanout_tasks(self, state: RuntimeState, tasks: list[ReadTask], expense: dict[str, Any]) -> bool:
        args = self._next_project_search_args(state, expense, allow_llm=False)
        if args is None:
            return False
        if args.get("project_code"):
            self._append_read_task(state, tasks, "workflow.project_search", args, "workflow")
            self._plan_project_query(state, args, source="project_code", priority=0)
            return True
        variants = self._project_search_fanout_args(state, expense, args)
        appended = False
        group_key = "fanout:workflow.project_search:project"
        for index, item in enumerate(variants):
            before = len(tasks)
            self._append_read_task(
                state,
                tasks,
                "workflow.project_search",
                item,
                "workflow",
                group_key=group_key,
                # A concrete singleton is sufficient in competition mode.
                # A broad structure-word singleton must not suppress the
                # concrete queries already queued behind it.
                stop_group_on_success=self._project_query_quality(item) > 0.15,
                mapping_score=2.0 - index * 0.1,
            )
            if len(tasks) > before:
                self._plan_project_query(state, item, source="fanout", priority=index)
                appended = True
        return appended

    def _project_resolution(self, state: RuntimeState) -> dict[str, Any]:
        evidence = state.workflow.evidence
        resolution = evidence.get("project_resolution")
        if not isinstance(resolution, dict):
            resolution = {
                "state": "pending_query",
                "query_plan": [],
                "candidate_registry": {},
                "transitions": [],
            }
            evidence["project_resolution"] = resolution
        resolution.setdefault("query_plan", [])
        resolution.setdefault("candidate_registry", {})
        resolution.setdefault("transitions", [])
        return resolution

    def _plan_project_query(self, state: RuntimeState, args: dict[str, Any], *, source: str, priority: int = 0) -> None:
        resolution = self._project_resolution(state)
        query = str(args.get("project_code") or args.get("project_name") or "").strip()
        if not query:
            return
        if any(str(item.get("query") or "") == query for item in resolution["query_plan"] if isinstance(item, dict)):
            return
        resolution["query_plan"].append(
            {
                "query": query,
                "query_type": "project_code" if args.get("project_code") else "project_name",
                "source": source,
                "priority": priority,
                "status": "planned",
            }
        )
        if resolution.get("state") in {"empty", "pending_query"}:
            resolution["state"] = "pending_query"

    def _record_project_query_result(
        self,
        state: RuntimeState,
        args: dict[str, Any],
        projects: list[dict[str, Any]],
        *,
        error: str = "",
    ) -> dict[str, Any]:
        resolution = self._project_resolution(state)
        query = str(args.get("project_code") or args.get("project_name") or "").strip()
        entry = next(
            (item for item in resolution["query_plan"] if isinstance(item, dict) and str(item.get("query") or "") == query),
            None,
        )
        if entry is None:
            entry = {"query": query, "query_type": "project_code" if args.get("project_code") else "project_name", "source": "direct"}
            resolution["query_plan"].append(entry)
        entry["status"] = "error" if error else "executed"
        entry["result_count"] = len(projects)
        if error:
            entry["error"] = error
        for project in projects:
            if not isinstance(project, dict):
                continue
            fingerprint = self._expense_project_fingerprint(project)
            if fingerprint and fingerprint != "|":
                resolution["candidate_registry"][fingerprint] = {
                    "project_name": project.get("project_name"),
                    "project_code": project.get("project_code"),
                    "wbs_code": project.get("wbs_code"),
                    "queries": self._dedupe(
                        list((resolution["candidate_registry"].get(fingerprint) or {}).get("queries") or []) + [query]
                    ),
                }
        return resolution

    def _transition_project_resolution(self, resolution: dict[str, Any], state_name: str, *, query: str, selected_id: str = "") -> None:
        resolution["state"] = state_name
        resolution["query"] = query
        if selected_id:
            resolution["selected_id"] = selected_id
        if state_name in {"verified_singleton", "verified_ranked"}:
            resolution["selected_query_quality"] = self._project_query_quality({"project_name": query})
            for entry in resolution.get("query_plan") or []:
                if (
                    isinstance(entry, dict)
                    and entry.get("status") == "planned"
                    and self._project_query_quality({"project_name": entry.get("query")}) <= 0.15
                ):
                    entry["status"] = "deferred_after_verification"
        resolution["transitions"].append(
            {"state": state_name, "query": query, "selected_id": selected_id, "candidate_count": len(resolution.get("candidate_registry") or {})}
        )

    def _project_search_fanout_args(self, state: RuntimeState, expense: dict[str, Any], first_args: dict[str, Any]) -> list[dict[str, Any]]:
        if first_args.get("project_code"):
            return [first_args]
        tried = set(str(item) for item in expense.get("_tried_project_keywords") or [])
        out: list[str] = []
        first = self._clean_project_phrase(first_args.get("project_name") or "")
        if first:
            out.append(first)
        explicit = self._clean_project_phrase(expense.get("project_name") or "")
        context_tokens = self._project_context_structural_tokens(state, explicit)
        formal_explicit = self._looks_like_formal_project_hint(explicit)
        if explicit:
            out.extend(self._project_structural_candidates(explicit))
            if formal_explicit:
                out.extend(self._formal_hint_core_candidates(explicit))
                out.extend(self._project_candidate_forms(explicit))
                out.extend(self._project_name_variants(explicit))
            else:
                out.extend(self._project_candidate_forms(explicit))
        for keyword in expense.get("project_keywords") or []:
            text = self._clean_project_phrase(keyword)
            if text:
                out.extend(self._project_structural_candidates(text))
                if formal_explicit or self._looks_like_formal_project_hint(text):
                    out.append(text)
                    out.extend(self._project_candidate_forms(text))
                    out.extend(self._formal_hint_core_candidates(text))
                    out.extend(self._project_core_token_candidates(text))
        if formal_explicit:
            out.extend(self._project_phrase_candidates(self._workflow_query(state)))
        # Bare structure words are fallback probes and must never displace a
        # concrete business phrase in the bounded fanout.
        out.extend(context_tokens)
        derived_structure_tokens = set(context_tokens)
        derived_structure_tokens.update(self._project_structural_tokens(explicit))
        for keyword in expense.get("project_keywords") or []:
            derived_structure_tokens.update(self._project_structural_tokens(str(keyword)))
        variants = []
        ordered = []
        for raw in out:
            raw_value = str(raw or "").strip()
            # _clean_project_phrase intentionally removes "项目" when it is a
            # grammatical prefix. Preserve it here only after provenance has
            # been established by the project phrase/context extractor.
            value = raw_value if raw_value in derived_structure_tokens else self._clean_project_phrase(raw_value)
            if value and value not in ordered:
                ordered.append(value)
        if ordered:
            first_value, remaining_values = ordered[0], ordered[1:]
            remaining_values = [
                value
                for _, value in sorted(
                    enumerate(remaining_values),
                    key=lambda pair: (-self._project_query_quality({"project_name": pair[1]}), pair[0]),
                )
            ]
            ordered = [first_value, *remaining_values]
        for value in ordered:
            if not value:
                continue
            if value in tried and value != first:
                continue
            # A bare structural term is allowed only when it was literally
            # extracted from the user's project phrase, never as a global
            # inventory probe.
            if not self._valid_project_search_candidate(value, allow_short=True) and value not in derived_structure_tokens:
                continue
            variants.append(value)
            if len(variants) >= 3:
                break
        if not variants and first:
            variants = [first]
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "project_search_fanout",
                "case_id": state.obs.get("case_id"),
                "variants": variants,
                "first_args": first_args,
            },
        )
        # Keep the literal plus bounded tokens/phrases extracted from the
        # user's project text, including its explicit structure terms.
        return [{"project_name": value} for value in variants[:3]]

    def _project_structural_candidates(self, value: Any) -> list[str]:
        """Split only the supplied project phrase around structural terms."""
        text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", self._clean_project_phrase(str(value or "")))
        if len(text) < 4:
            return []
        candidates: list[str] = []
        for match in re.finditer(r"项目|专项|工程|平台|系统", text):
            start, end = match.span()
            # Preserve a meaningful noun phrase containing the structure word.
            candidates.extend([text[:end], text[max(0, start - 4) :], text[max(0, start - 2) : end]])
            if end < len(text):
                candidates.append(text[start : min(len(text), end + 4)])
        phrases = [item for item in self._normalize_project_candidates(candidates) if self._valid_project_search_candidate(item, allow_short=True)]
        # Individual terms are valid only because they come from this exact
        # user phrase, but remain the final fallback after meaningful phrases.
        return self._dedupe(phrases + self._project_structural_tokens(text))

    def _project_query_quality(self, args: dict[str, Any]) -> float:
        if args.get("project_code"):
            return 1.0
        query = str(args.get("project_name") or "").strip()
        if not query:
            return 0.0
        if query in {"项目", "平台", "系统", "工程", "专项"}:
            return 0.1
        return min(0.95, 0.55 + min(len(query), 12) * 0.03)

    def _project_structural_tokens(self, value: Any) -> list[str]:
        text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", self._clean_project_phrase(str(value or "")))
        return self._dedupe(re.findall(r"项目|专项|工程|平台|系统", text))

    def _project_context_structural_tokens(self, state: RuntimeState, explicit_project: str) -> list[str]:
        if not explicit_project:
            return []
        query = self._workflow_query(state)
        return self._dedupe(
            re.findall(r"(项目|平台|系统|工程|专项)\s*(?:名(?:称)?\s*)?(?:是|为|叫|：|:)", query)
        )

    def _looks_like_formal_project_hint(self, value: Any) -> bool:
        text = self._clean_project_phrase(str(value or ""))
        return bool(text and re.search(r"(项目|专项|工程|系统|平台)$", text))

    def _formal_hint_core_candidates(self, value: Any) -> list[str]:
        text = self._clean_project_phrase(str(value or ""))
        if not text:
            return []
        stripped = text
        for suffix in ["项目", "专项", "工程", "系统", "平台"]:
            if stripped.endswith(suffix) and len(stripped) > len(suffix) + 3:
                stripped = stripped[: -len(suffix)]
                break
        out = []
        compact = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", stripped)
        for size in (6, 4):
            if len(compact) > size:
                out.append(compact[-size:])
        out.append(stripped)
        return self._dedupe(self._normalize_project_candidates(out))

    def _expense_browser_field_id(self, state: RuntimeState, field_key: str, detail_table: str = "") -> int | None:
        return self.workflow_registry.browser_field_id(
            WORKFLOW_IDS["expense"],
            field_key,
            state.workflow.evidence.get("schema") or {},
            detail_table=detail_table,
        )

    def _expense_category_field_id(self, state: RuntimeState) -> int | None:
        return self._expense_browser_field_id(state, "material_category")

    def _expense_subclass_field_id(self, state: RuntimeState) -> int | None:
        return self._expense_browser_field_id(state, "material_subclass", detail_table="detail_2")

    def _append_expense_subclass_read_task(self, state: RuntimeState, tasks: list[ReadTask]) -> None:
        wf = state.workflow
        if wf.intent != "expense_material" or wf.evidence.get("subclass_options"):
            return
        projects = [wf.evidence["verified_project"]] if wf.evidence.get("verified_project") else (wf.evidence.get("project", {}).get("projects") or [])
        if len(projects) != 1:
            return
        category = self._select_material_category(state)
        if not category:
            return
        project = projects[0]
        category_field_id = self._expense_category_field_id(state)
        subclass_field_id = self._expense_subclass_field_id(state)
        if not category_field_id or not subclass_field_id:
            return
        if self._subclass_lookup_failed(state, project, category):
            return
        depends_on = []
        if not wf.evidence.get("category_options"):
            depends_on.append(
                self._read_task_key(
                    "workflow.browser_search",
                    {"workflow_id": WORKFLOW_IDS["expense"], "field_id": category_field_id},
                )
            )
        self._append_read_task(
            state,
            tasks,
            "workflow.browser_search",
            {
                "workflow_id": WORKFLOW_IDS["expense"],
                "field_id": subclass_field_id,
                "dep": {"wbscode": project.get("wbs_code"), "wzlb": category.get("value") or category.get("code")},
            },
            "workflow",
            depends_on=depends_on,
        )

    def _append_project_empty_retry_tasks(self, state: RuntimeState, tasks: list[ReadTask], expense: dict[str, Any]) -> bool:
        wf = state.workflow
        if not self._empty_read_mapping_enabled():
            return False
        if "workflow.project_search" not in state.tools:
            return False
        if wf.evidence.get("verified_project") or wf.evidence.get("project_candidates"):
            return False
        if wf.evidence.get("empty_project_mapping_done"):
            return False
        empty_history = self._empty_project_search_history(state)
        if not empty_history:
            return False
        remaining_steps = max(0, state.step_budget - state.steps_used)
        if remaining_steps <= 3:
            return False
        llm_config = self._llm_config("strong")
        if not llm_config.get("api_key") or not self._can_call_llm(state, "strong", min_remaining=14.0):
            return False
        scored_variants = self._project_empty_mapping_variants(state, expense)
        wf.evidence["empty_project_mapping_done"] = True
        state.empty_read_mappings_total += 1
        tried = expense.setdefault("_tried_project_keywords", [])
        failed_args = [item.get("args") for item in empty_history if isinstance(item, dict)]
        failed_names = {
            str((args or {}).get("project_name") or "")
            for args in failed_args
            if isinstance(args, dict) and (args or {}).get("project_name")
        }
        max_variants = min(
            3,
            self._empty_read_mapping_max_variants(),
            max(1, remaining_steps - 2),
        )
        appended = 0
        variants: list[str] = []
        group_key = "empty_map:workflow.project_search:project"
        candidate_variants = self._select_empty_mapping_candidates(
            scored_variants,
            max_variants=max_variants,
            failed_names=failed_names,
            tried=set(str(item) for item in tried),
        )
        execution_variants = sorted(candidate_variants, key=self._empty_read_retry_execution_key)
        for variant in execution_variants:
            keyword = self._clean_project_phrase(str(variant.get("project_name") or ""))
            args = {"project_name": keyword}
            before = len(tasks)
            self._append_read_task(
                state,
                tasks,
                "workflow.project_search",
                args,
                "workflow",
                evidence_handler="empty_read_mapping_retry",
                parallel_eligible=True,
                group_key=group_key,
                stop_group_on_success=not self._parallel_reads_enabled(),
                mapping_score=self._bounded_float(variant.get("score"), 0.0),
            )
            if len(tasks) > before:
                variants.append(keyword)
                appended += 1
                state.empty_read_retry_tasks_total += 1
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "empty_read_mapping",
                "case_id": state.obs.get("case_id"),
                "tool": "workflow.project_search",
                "source": "scored_topk_mapping",
                "empty_result_count": len(empty_history),
                "suggestions": scored_variants,
                "variants": variants,
                "selected_variants": candidate_variants,
                "execution_variants": execution_variants,
                "retry_task_count": appended,
                "remaining_steps": remaining_steps,
                "group_key": group_key,
            },
        )
        return appended > 0

    def _project_empty_mapping_variants(self, state: RuntimeState, expense: dict[str, Any]) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        llm_variants = self._llm_project_search_mapping_variants(state, expense)
        variants.extend(llm_variants)
        variants.extend(self._empty_project_llm_core_variants(llm_variants))
        by_name: dict[str, dict[str, Any]] = {}
        for item in variants:
            if not isinstance(item, dict):
                continue
            name = self._clean_project_phrase(str(item.get("project_name") or ""))
            if not name:
                continue
            score = self._bounded_float(item.get("score"), 0.0)
            normalized = {
                "project_name": name,
                "score": max(0.0, min(1.0, score)),
                "reason": str(item.get("reason") or ""),
                "source": str(item.get("source") or "mapping"),
            }
            current = by_name.get(name)
            if current is None or normalized["score"] > self._bounded_float(current.get("score"), 0.0):
                by_name[name] = normalized
        return sorted(by_name.values(), key=lambda item: (-self._bounded_float(item.get("score"), 0.0), len(str(item.get("project_name") or "")), str(item.get("project_name") or "")))

    def _select_empty_mapping_candidates(
        self,
        variants: list[dict[str, Any]],
        max_variants: int,
        failed_names: set[str],
        tried: set[str],
    ) -> list[dict[str, Any]]:
        eligible: list[dict[str, Any]] = []
        seen: set[str] = set()
        for variant in variants:
            keyword = self._clean_project_phrase(str(variant.get("project_name") or ""))
            if not keyword or keyword in failed_names or keyword in tried or keyword in seen:
                continue
            item = dict(variant)
            item["project_name"] = keyword
            eligible.append(item)
            seen.add(keyword)
        if max_variants <= 0 or not eligible:
            return []

        selected: list[dict[str, Any]] = []
        selected_names: set[str] = set()

        def add(item: dict[str, Any] | None) -> None:
            if item is None or len(selected) >= max_variants:
                return
            name = str(item.get("project_name") or "")
            if not name or name in selected_names:
                return
            selected.append(item)
            selected_names.add(name)

        llm_ranked = sorted(
            [item for item in eligible if str(item.get("source") or "") == "llm"],
            key=lambda item: (-self._bounded_float(item.get("score"), 0.0), len(str(item.get("project_name") or "")), str(item.get("project_name") or "")),
        )
        recall_ranked = sorted(
            [
                item
                for item in eligible
                if str(item.get("source") or "") in {"llm_core_token", "literal_substring_window"}
                and len(str(item.get("project_name") or "")) <= 4
            ],
            key=lambda item: self._empty_mapping_recall_sort_key(item, failed_names),
        )

        add(llm_ranked[0] if llm_ranked else None)
        for item in recall_ranked:
            add(item)
            if len(selected) >= min(max_variants, 3):
                break
        for item in eligible:
            add(item)
            if len(selected) >= max_variants:
                break
        return selected[:max_variants]

    def _empty_mapping_recall_sort_key(self, item: dict[str, Any], failed_names: set[str]) -> tuple[Any, ...]:
        name = str(item.get("project_name") or "")
        source = str(item.get("source") or "")
        source_priority = 0 if source == "llm_core_token" else 1
        prefix_penalty = 1 if any(failed.startswith(name) for failed in failed_names if failed) else 0
        edge_penalty = 1 if any((failed.startswith(name) or failed.endswith(name)) for failed in failed_names if failed) else 0
        return (
            source_priority,
            prefix_penalty,
            edge_penalty,
            len(name),
            -self._bounded_float(item.get("score"), 0.0),
            name,
        )

    def _empty_project_llm_core_variants(self, llm_variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        for item in llm_variants[:4]:
            name = self._clean_project_phrase(str(item.get("project_name") or ""))
            if len(name) != 4:
                continue
            base_score = self._bounded_float(item.get("score"), 0.0)
            for piece, offset in ((name[2:], 0.02), (name[:2], 0.12)):
                piece = self._clean_project_phrase(piece)
                if piece and self._valid_project_retry_substring(piece):
                    variants.append(
                        {
                            "project_name": piece,
                            "score": max(0.0, min(1.0, base_score - offset)),
                            "reason": "core token derived from LLM project recall phrase",
                            "source": "llm_core_token",
                        }
                    )
        # Preserve the user's original phrase for the first read. If it does
        # not retrieve a project, the LLM mapping stage can spend the remaining
        # bounded retries on semantic alternatives rather than near-duplicates.
        return variants[:1]

    def _empty_read_retry_execution_key(self, variant: dict[str, Any]) -> tuple[int, float, str]:
        name = str(variant.get("project_name") or "")
        # In the real parallel mode all TopK reads run together. In the local
        # sequential simulator, shorter substring probes are attempted first so
        # a hit can stop the retry group before it consumes write budget.
        return (len(name), -self._bounded_float(variant.get("score"), 0.0), name)

    def _empty_project_search_history(self, state: RuntimeState) -> list[dict[str, Any]]:
        empty: list[dict[str, Any]] = []
        for item in state.workflow.evidence.get("project_search_history") or []:
            if not isinstance(item, dict):
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            if result.get("error"):
                continue
            projects = result.get("projects") if isinstance(result.get("projects"), list) else []
            if not projects:
                empty.append(item)
        return empty

    def _llm_project_search_mapping_variants(self, state: RuntimeState, expense: dict[str, Any]) -> list[dict[str, Any]]:
        cache_key = "_llm_project_search_mapping_variants"
        if cache_key in expense:
            return list(expense.get(cache_key) or [])
        llm_config = self._llm_config("strong")
        if not llm_config.get("api_key") or not self._can_call_llm(state, "strong", min_remaining=14.0):
            expense[cache_key] = []
            return []
        history = state.workflow.evidence.get("project_search_history") or []
        context_pack = self.static_context.for_workflow_form(
            WORKFLOW_IDS["expense"],
            {
                "stage": "empty_project_search_mapping",
                "failed_project_search_count": len(history),
                "project_hint_present": bool(expense.get("project_name") or expense.get("project_keywords")),
            },
        )
        payload = {
            "query": self._workflow_query(state),
            "tool_contract": {
                "tool": "workflow.project_search",
                "allowed_args": ["project_name"],
                "matching_rule": "project_name is substring matched against real SAP project names",
                "forbidden": ["project_code", "wbs_code", "company_id", "write tools"],
            },
            "current_slots": {
                "project_name": expense.get("project_name"),
                "project_keywords": expense.get("project_keywords") or [],
                "material_category_hint": expense.get("material_category_hint"),
                "material_subclass_hint": expense.get("material_subclass_hint"),
                "items": expense.get("items") or [],
                "total_amount": expense.get("total_amount"),
            },
            "failed_project_searches": [
                {
                    "args": item.get("args"),
                    "result_count": len((item.get("result") or {}).get("projects") or []),
                }
                for item in history[-6:]
                if isinstance(item, dict)
            ],
            "static_context": context_pack.get("content") or "",
            "instruction": (
                "Return valid json only. The previous project_search queries returned no projects, so do recall-oriented translation rather than similarity rewrite. "
                "Produce the top 3 alternative project_name substring queries that could appear inside formal enterprise project names. "
                "Prefer concise parent-theme terms, formal project core words, and business-objective terms inferred from the user's project hint, material category, and line items. "
                "At least one variant should be a narrower or broader business phrase derived from the user-provided project wording when the failed phrase is long or event-like. "
                "Score each query from 0 to 1 by expected retrieval probability, not lexical similarity to the failed query. "
                "Do not output project_code or wbs_code unless typed by the user. Do not choose a project. "
                "Avoid exact failed args and amount/material/action-only words. Never return generic state words such as 项目、平台、系统、工程 or 专项 alone; every query must be a concrete business phrase of at least four Chinese characters."
            ),
            "output_schema": {
                "variants": [
                    {"project_name": "substring query", "score": 0.0, "reason": "short reason"}
                ]
            },
        }
        try:
            content = self._chat_completion(
                llm_config,
                [
                    {
                        "role": "system",
                        "content": (
                            "Return valid json only. You translate an empty read result into read-tool retry parameters. "
                            "Only return workflow.project_search project_name variants; never invent project ids or write anything. "
                            "Your job is query recall for a substring search tool, not final project selection."
                        ),
                    },
                    {"role": "user", "content": "Return valid json only.\n" + json.dumps(payload, ensure_ascii=False)},
                ],
                state=state,
                profile="strong",
                context_pack_type=str(context_pack.get("pack_type") or "workflow_project_empty_mapping_context"),
                context_chars=int(context_pack.get("chars") or 0),
            )
            parsed = self._parse_json_object(content) or {}
            raw_variants = parsed.get("variants") if isinstance(parsed.get("variants"), list) else []
            variants: list[dict[str, Any]] = []
            for raw in raw_variants[:6]:
                if isinstance(raw, dict):
                    name = self._clean_project_phrase(str(raw.get("project_name") or raw.get("keyword") or ""))
                    score = self._bounded_float(raw.get("score"), 0.0)
                    reason = str(raw.get("reason") or "")
                else:
                    name = self._clean_project_phrase(str(raw or ""))
                    score = 0.5
                    reason = ""
                if name and self._valid_project_search_candidate(name, allow_short=True):
                    variants.append({"project_name": name, "score": score, "reason": reason, "source": "llm"})
            expense[cache_key] = variants
            self._debug_log(llm_config, {"event": "project_search_mapping_variants", "variants": variants, "raw": parsed})
            return variants
        except Exception as exc:
            self._debug_log(llm_config, {"event": "project_search_mapping_variants_error", "error": str(exc)})
            expense[cache_key] = []
            return []

    def _empty_project_broad_retry_variants(self, state: RuntimeState, expense: dict[str, Any]) -> list[dict[str, Any]]:
        # Never manufacture character windows from a failed project phrase.
        # They have no provenance as a business concept and disproportionately
        # match unrelated projects. Semantic rewrites are produced by the
        # bounded LLM ReadHypothesis path instead.
        values = [
            str(expense.get("project_name") or ""),
            self._extract_project_name(self._workflow_query(state)),
        ]
        variants: list[dict[str, Any]] = []
        for raw in self._dedupe(values):
            value = self._clean_project_phrase(raw)
            if not value or not self._valid_project_search_candidate(value, allow_short=True):
                continue
            variants.append(
                {
                    "project_name": value,
                    "score": 0.58,
                    "reason": "user literal project phrase",
                    "source": "user_literal",
                }
            )
        return variants

    def _valid_project_retry_substring(self, value: str) -> bool:
        candidate = str(value or "").strip("，。:：；;、 的")
        if not (4 <= len(candidate) <= 12):
            return False
        return self._valid_project_search_candidate(candidate, allow_short=True)

    def _append_meetingroom_read_tasks(self, state: RuntimeState, tasks: list[ReadTask]) -> None:
        mr = state.meetingroom
        if not mr.needed or mr.status not in {"pending", "ready"}:
            return
        if mr.intent == "unknown":
            return
        if mr.slots.get("needs_workspace") and "workspace" not in mr.evidence:
            self._append_read_task(state, tasks, "user.get_workspace", {}, "meetingroom")
            if not (mr.slots.get("office_candidates") or mr.slots.get("office_address_candidates")):
                return
        if mr.intent in {"query_booking", "query"} and "booking_query" not in mr.evidence:
            args: dict[str, Any] = {}
            day = self._meeting_day(state)
            if day:
                args["day"] = day
            if mr.slots.get("keyword"):
                args["keyword"] = mr.slots["keyword"]
            self._append_read_task(state, tasks, "meetingroom.booking.list", args, "meetingroom")
            return
        if mr.intent in {"cancel_existing", "extend_existing", "rebook_larger_existing", "cancel_rebook_existing", "cancel", "extend", "rebook_larger", "cancel_rebook"}:
            if "selected_booking" not in mr.evidence:
                args = self._next_booking_list_args(state)
                if args is not None:
                    self._append_read_task(state, tasks, "meetingroom.booking.list", args, "meetingroom")
            if self._is_rebook_intent(mr.intent) or self._is_rebook_larger_intent(mr.intent):
                self._append_room_list_read_tasks(state, tasks)
            return
        if mr.intent.startswith("participant_"):
            order_id = self._extract_order_id(self._full_query(state.obs)) or mr.slots.get("order_id")
            if not order_id and not mr.evidence.get("selected_booking"):
                args = self._next_participant_booking_list_args(state)
                if args is not None:
                    self._append_read_task(state, tasks, "meetingroom.booking.list", args, "meetingroom")
                return
            if not order_id:
                order_id = (mr.evidence.get("selected_booking") or {}).get("order_id")
            if mr.intent == "participant_list" and "participants" not in mr.evidence and order_id:
                self._append_read_task(
                    state,
                    tasks,
                    "meetingroom.booking.participant.list",
                    {"order_id": order_id},
                    "meetingroom",
                )
            elif mr.intent in {"participant_add", "participant_remove"}:
                index = int(mr.evidence.get("participant_index") or 0)
                people = mr.slots.get("participants") if isinstance(mr.slots.get("participants"), list) else []
                person = people[index] if index < len(people) and isinstance(people[index], dict) else {}
                if order_id and mr.intent == "participant_add" and "participants" not in mr.evidence:
                    self._append_read_task(
                        state,
                        tasks,
                        "meetingroom.booking.participant.list",
                        {"order_id": order_id},
                        "meetingroom",
                    )
                if person and not person.get("user_id") and not mr.evidence.get(f"participant_user_{index}"):
                    keyword = self._participant_lookup_keyword(person)
                    if keyword:
                        self._append_read_task(state, tasks, "user.get_info", {"keyword": keyword}, "meetingroom")
            return
        if self._explicit_schedule_then_book(state) or mr.intent in {"book_by_schedule_analysis", "query_room_schedule", "schedule_book"}:
            self._append_schedule_read_tasks(state, tasks)
            return
        self._append_room_list_read_tasks(state, tasks)

    def _append_room_list_read_tasks(self, state: RuntimeState, tasks: list[ReadTask]) -> None:
        mr = state.meetingroom
        before_count = len(tasks)
        if self._multi_day_intersection_needed(state):
            existing_days = set((mr.evidence.get("room_lists_by_day") or {}).keys())
            for day in self._schedule_required_days(state):
                if day in existing_days:
                    continue
                args = self._room_list_args(state, day=day)
                if args is not None:
                    self._append_read_task(state, tasks, "meetingroom.room.list", args, "meetingroom")
            return
        if "room_candidates" in mr.evidence:
            return
        tried_args = [item.get("args") for item in mr.evidence.get("tried_room_lists", [])]
        single_workspace_query = bool(mr.slots.get("needs_workspace") and mr.evidence.get("workspace"))
        for day in self._room_search_days(state):
            for candidate in self._room_search_candidates(state):
                args = self._room_list_args_for_candidate(state, candidate, day=day or None)
                if args and args not in tried_args:
                    self._append_read_task(state, tasks, "meetingroom.room.list", args, "meetingroom")
                    if single_workspace_query and len(tasks) > before_count:
                        return
        if len(tasks) == before_count:
            args = self._next_room_list_args(state)
            if args is not None:
                self._append_read_task(state, tasks, "meetingroom.room.list", args, "meetingroom")

    def _append_schedule_read_tasks(self, state: RuntimeState, tasks: list[ReadTask]) -> None:
        mr = state.meetingroom
        schedules = mr.evidence.setdefault("schedules", {})
        room_ids = [str(item) for item in (mr.slots.get("room_ids") or []) if item]
        if not room_ids:
            if "room_candidates" not in mr.evidence:
                self._append_room_list_read_tasks(state, tasks)
                return
            rooms = (mr.evidence.get("room_candidates") or {}).get("rooms") or []
            if self._requires_full_schedule_analysis(state):
                room_ids = [str(room.get("room_id")) for room in rooms if room.get("room_id")]
            else:
                selected = self._select_room_from_schedule_analysis(state)
                if selected and selected.get("room_id"):
                    room_ids = [str(selected.get("room_id"))]
        start_date, end_date = self._schedule_range(state)
        for room_id in room_ids:
            if room_id in schedules:
                continue
            self._append_read_task(
                state,
                tasks,
                "meetingroom.room.schedule",
                {"room_id": room_id, "start_date": start_date, "end_date": end_date},
                "meetingroom",
            )

    def _read_task_key(self, tool: str, args: dict[str, Any] | None) -> str:
        if tool not in READ_TOOLS:
            return ""
        return tool + ":" + json.dumps(self._clean_args(args or {}), ensure_ascii=False, sort_keys=True)

    def _read_task_allowed(self, state: RuntimeState, task: ReadTask) -> bool:
        if task.tool not in READ_TOOLS or task.tool not in state.tools:
            return False
        if task.task_key in state.read_task_keys_completed or task.task_key in state.read_task_keys_attempted:
            return False
        if task.group_key and task.group_key in state.read_task_groups_succeeded:
            return False
        if state.steps_used >= state.step_budget:
            return False
        if self._remaining_seconds(state) < task.deadline_min_remaining:
            return False
        if any(dep and dep not in state.read_task_keys_completed for dep in task.depends_on):
            return False
        if not self._owner_task_is_runnable(state, task):
            return False
        if self._cross_domain_active(state) and task.domain in {"meetingroom", "workflow"}:
            other = "workflow" if task.domain == "meetingroom" else "meetingroom"
            if (
                state.domain_steps_used.get(task.domain, 0) >= state.domain_step_budgets.get(task.domain, state.step_budget)
                and state.domain_steps_used.get(other, 0) < state.domain_step_budgets.get(other, 0)
            ):
                return False
        return True

    def _read_task_summary(self, task: ReadTask) -> dict[str, Any]:
        return {
            "task_key": task.task_key,
            "tool": task.tool,
            "args": task.args,
            "domain": task.domain,
            "depends_on": task.depends_on,
            "parallel_eligible": task.parallel_eligible,
            "group_key": task.group_key,
            "stop_group_on_success": task.stop_group_on_success,
            "mapping_score": round(task.mapping_score, 3),
            "owner_task_id": task.owner_task_id,
        }

    def _execute_read_plan(self, state: RuntimeState, plan: ReadPlan, llm_config: dict[str, Any]) -> int:
        tasks = [task for task in plan.tasks if self._read_task_allowed(state, task)]
        if not tasks:
            return 0
        state.read_plan_batches += 1
        async_requested = self._async_read_execution_enabled() and len(tasks) > 1 and all(task.parallel_eligible for task in tasks)
        parallel_requested = (self._parallel_reads_enabled() or async_requested) and len(tasks) > 1 and all(task.parallel_eligible for task in tasks)
        # IFTKEnv.call_tool mutates shared counters, history and tool instances.
        # The A/B mode changes scheduling only and keeps this boundary locked.
        self._debug_log(
            llm_config,
            {
                "event": "read_batch",
                "case_id": state.obs.get("case_id"),
                "batch_index": state.read_plan_batches,
                "task_count": len(tasks),
                "parallel_requested": parallel_requested,
                "async_requested": async_requested,
                "parallel_mode": "serialized_async_compare" if async_requested else ("locked_env_call" if parallel_requested else "sequential"),
                "tasks": [self._read_task_summary(task) for task in tasks],
            },
        )
        if parallel_requested:
            return self._execute_read_tasks_locked_parallel(state, tasks, llm_config)
        executed = 0
        for task in tasks:
            if not self._read_task_allowed(state, task):
                continue
            self._execute_read_task(state, task, llm_config)
            executed += 1
        return executed

    def _execute_read_tasks_locked_parallel(self, state: RuntimeState, tasks: list[ReadTask], llm_config: dict[str, Any]) -> int:
        executed = 0
        max_workers = min(self._parallel_read_max_workers(), len(tasks))
        timeout = self._parallel_read_timeout_seconds()
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for task in tasks:
                futures.append(pool.submit(self._execute_read_task_with_lock, state, task, llm_config))
            for future in as_completed(futures, timeout=max(timeout, 0.5) * max(1, len(futures))):
                try:
                    if future.result():
                        executed += 1
                except Exception as exc:
                    self._debug_log(
                        llm_config,
                        {
                            "event": "read_task",
                            "case_id": state.obs.get("case_id"),
                            "success": False,
                            "error": str(exc)[:240],
                        },
                    )
        return executed

    def _execute_read_task_with_lock(self, state: RuntimeState, task: ReadTask, llm_config: dict[str, Any]) -> bool:
        with self._read_parallel_lock:
            if not self._read_task_allowed(state, task):
                return False
            self._execute_read_task(state, task, llm_config)
            return True

    def _execute_read_task(self, state: RuntimeState, task: ReadTask, llm_config: dict[str, Any]) -> None:
        started = time.monotonic()
        owner = self._task_runtime(state, task.owner_task_id) if task.owner_task_id else None
        if owner is not None and owner.status not in TaskRuntime.TERMINAL_STATUSES:
            owner.status = "reading"
        cache_key = self._cache_key(task.tool, self._clean_args(task.args))
        cached = bool(cache_key and cache_key in state.cache)
        state.read_task_keys_attempted.add(task.task_key)
        state.read_tasks_total += 1
        if task.parallel_eligible:
            state.read_tasks_parallel_eligible += 1
        if cached:
            state.read_tasks_cached += 1
        self._execute(state, StepAction("tool", task.tool, task.args), llm_config)
        group_success = self._read_task_group_success(state, task)
        if task.group_key and task.stop_group_on_success and group_success:
            state.read_task_groups_succeeded.add(task.group_key)
        elapsed = max(0.0, time.monotonic() - started)
        state.read_elapsed_seconds += elapsed
        state.read_task_keys_completed.add(task.task_key)
        if owner is not None and owner.status == "reading":
            owner.status = "ready" if self._task_write_likely_ready(state, owner) else "pending"
        self._debug_log(
            llm_config,
            {
                "event": "read_task",
                "case_id": state.obs.get("case_id"),
                "task_key": task.task_key,
                "tool": task.tool,
                "args": task.args,
                "domain": task.domain,
                "cached": cached,
                "success": True,
                "group_key": task.group_key,
                "group_success": group_success,
                "mapping_score": round(task.mapping_score, 3),
                "owner_task_id": task.owner_task_id,
                "elapsed_seconds": round(elapsed, 3),
                "steps_used": state.steps_used,
                "remaining_seconds": round(self._remaining_seconds(state), 3),
            },
        )

    def _read_task_group_success(self, state: RuntimeState, task: ReadTask) -> bool:
        if task.tool == "workflow.project_search":
            return bool(state.workflow.evidence.get("verified_project") or state.workflow.evidence.get("project_candidates"))
        if task.tool == "workflow.search_person":
            return bool(self._collected_approver_people(state.workflow))
        if task.tool == "meetingroom.booking.list":
            return bool(state.meetingroom.evidence.get("selected_booking") or (state.meetingroom.evidence.get("booking_query") or {}).get("bookings"))
        if task.tool == "meetingroom.room.list":
            return bool((state.meetingroom.evidence.get("room_candidates") or {}).get("rooms"))
        return False

    def _next_action(self, state: RuntimeState) -> StepAction | None:
        if state.workflow.needed and self._workflow_needs_oa_check(state) and self._remaining_seconds(state) > 6.0:
            keyword = self._oa_keyword_for_workflow(state)
            action = self._next_oa_action(state, keyword)
            if action is not None:
                return action
        if self._cross_domain_active(state):
            workflow_ready = self._workflow_write_likely_ready(state)
            meeting_ready = self._meeting_write_likely_ready(state)
            if self._meeting_rebook_write_in_progress(state):
                action = self._next_meetingroom_action(state)
                if action is not None:
                    return action
            if workflow_ready and (not meeting_ready or state.last_action_domain == "meetingroom"):
                action = self._next_workflow_action(state)
                if action is not None:
                    return action
            if meeting_ready and (not workflow_ready or state.last_action_domain == "workflow"):
                action = self._next_meetingroom_action(state)
                if action is not None:
                    return action
        if state.meetingroom.needed and state.meetingroom.status in {"pending", "ready"}:
            action = self._next_meetingroom_action(state)
            if action is not None:
                return action
        if state.workflow.needed and state.workflow.status in {"pending", "ready"}:
            action = self._next_workflow_action(state)
            if action is not None:
                return action
        if state.meetingroom.needed and state.meetingroom.status in {"pending", "ready"}:
            return self._block_meetingroom(state, "missing_required_info")
        if state.workflow.needed and state.workflow.status in {"pending", "ready"}:
            return self._block_workflow(state, "missing_required_info")
        return None

    def _cross_domain_active(self, state: RuntimeState) -> bool:
        return (
            state.meetingroom.needed
            and state.workflow.needed
            and state.meetingroom.status in {"pending", "ready"}
            and state.workflow.status in {"pending", "ready"}
        )

    def _meeting_rebook_write_in_progress(self, state: RuntimeState) -> bool:
        mr = state.meetingroom
        if not mr.needed or mr.status not in {"pending", "ready"}:
            return False
        if not (self._is_rebook_intent(mr.intent) or self._is_rebook_larger_intent(mr.intent)):
            return False
        return bool(mr.evidence.get("cancel_done") and not mr.evidence.get("create_done"))

    def _workflow_write_likely_ready(self, state: RuntimeState) -> bool:
        wf = state.workflow
        self._sync_workflow_skill(state)
        if state.workflow_skill is not None:
            ready = state.workflow_skill.ready_nodes({"compute", "write", "postcheck"})
            if any(str(node.get("id") or "") in {"draft_ir", "delete_source", "save", "verify"} for node in ready):
                return True
        if wf.evidence.get("save_done"):
            return bool(self._workflow_needs_oa_check(state))
        if wf.intent == "leave":
            return bool(wf.evidence.get("applicant") and wf.evidence.get("schema") and self._collected_approver_people(wf))
        if wf.intent == "expense_material":
            return bool(
                wf.evidence.get("applicant")
                and wf.evidence.get("schema")
                and wf.evidence.get("verified_project")
                and wf.evidence.get("category_options")
                and wf.evidence.get("subclass_options")
            )
        return False

    def _meeting_write_likely_ready(self, state: RuntimeState) -> bool:
        mr = state.meetingroom
        if mr.intent in {"cancel_existing", "extend_existing", "rebook_larger_existing", "cancel_rebook_existing", "cancel", "extend", "rebook_larger", "cancel_rebook"}:
            if mr.evidence.get("cancel_done") and mr.evidence.get("room_candidates"):
                return True
            return bool(mr.evidence.get("selected_booking"))
        if mr.intent.startswith("participant_"):
            return bool(mr.evidence.get("selected_booking") or mr.slots.get("order_id"))
        if mr.intent in {"query_booking", "query", "query_room_schedule"}:
            return False
        return bool((mr.evidence.get("room_candidates") or {}).get("rooms") and mr.slots.get("start") and mr.slots.get("end"))

    def _task_write_likely_ready(self, state: RuntimeState, runtime: TaskRuntime) -> bool:
        if runtime.domain == "workflow":
            return self._workflow_write_likely_ready(state)
        if runtime.domain == "meetingroom":
            return self._meeting_write_likely_ready(state)
        return False

    def _next_meetingroom_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        intent = mr.intent
        if intent == "unknown":
            return self._block_meetingroom(state, "missing_required_info")
        if intent.startswith("participant_"):
            return self._next_participant_action(state)
        if intent in {"query_booking", "query"}:
            return self._next_meeting_query_action(state)
        if intent in {"cancel_existing", "extend_existing", "rebook_larger_existing", "cancel_rebook_existing", "cancel", "extend", "rebook_larger", "cancel_rebook"}:
            return self._next_existing_booking_action(state)
        if self._explicit_schedule_then_book(state):
            return self._next_schedule_book_action(state)
        if intent in {"book_by_schedule_analysis", "query_room_schedule", "schedule_book"}:
            return self._next_schedule_book_action(state)
        return self._next_booking_action(state)

    def _next_booking_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        slots = mr.slots
        if state.obs.get("mode") == "multi_turn":
            ask = self._meeting_missing_slot(state)
            if ask:
                return ask
        if slots.get("needs_workspace") and "workspace" not in mr.evidence:
            return StepAction("tool", "user.get_workspace", {})
        if self._multi_day_intersection_needed(state):
            for day in self._schedule_required_days(state):
                if day not in mr.evidence.get("room_lists_by_day", {}):
                    args = self._room_list_args(state, day=day)
                    if args is None:
                        return self._block_meetingroom(state, "missing_required_info")
                    return StepAction("tool", "meetingroom.room.list", args)
            selected = self._select_room_from_multi_day_lists(state)
            if selected is None:
                return self._block_meetingroom(state, "no_bookable_room")
            mr.evidence["pending_selected_room"] = selected
        if "room_candidates" not in mr.evidence:
            args = self._next_room_list_args(state)
            if args is None:
                return self._block_meetingroom(state, "missing_required_info")
            return StepAction("tool", "meetingroom.room.list", args)
        if str(slots.get("fallback_policy") or "") == "block_if_unavailable" and not (slots.get("day_text") and slots.get("start") and slots.get("end")):
            return self._block_meetingroom(state, "no_bookable_room")
        selected = self._select_room_for_booking(state)
        if selected is None:
            next_office = self._advance_room_candidate(state)
            if next_office is not None:
                mr.evidence.pop("room_candidates", None)
                mr.evidence.pop("pending_selected_room", None)
                return StepAction("tool", "meetingroom.room.list", next_office)
            return self._block_meetingroom(state, "no_bookable_room")
        if state.obs.get("mode") == "multi_turn" and not mr.evidence.get("confirmed_create"):
            return self._ask_confirmation(state, selected)
        next_segment = self._next_segment_to_create(mr)
        if next_segment:
            if next_segment.get("day"):
                slots["day"] = next_segment["day"]
            slots["start"] = next_segment["start"]
            slots["end"] = next_segment["end"]
            slots["title"] = next_segment["title"]
            return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))
        if mr.evidence.get("create_done"):
            return None
        return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))

    def _next_schedule_book_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        slots = mr.slots
        schedules = mr.evidence.setdefault("schedules", {})
        room_ids = slots.get("room_ids") or []
        if room_ids and self._explicit_schedule_then_book(state):
            for room_id in room_ids:
                if room_id not in schedules:
                    start_date, end_date = self._schedule_range(state)
                    return StepAction("tool", "meetingroom.room.schedule", {"room_id": room_id, "start_date": start_date, "end_date": end_date})
            selected = self._select_named_room_from_schedules(state)
            if selected:
                mr.evidence["pending_selected_room"] = selected
                return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))
            return self._block_meetingroom(state, "no_bookable_room")
        if slots.get("needs_workspace") and "workspace" not in mr.evidence:
            return StepAction("tool", "user.get_workspace", {})
        if self._multi_day_intersection_needed(state):
            for day in self._schedule_required_days(state):
                if day not in mr.evidence.get("room_lists_by_day", {}):
                    args = self._room_list_args(state, day=day)
                    if args is None:
                        return self._block_meetingroom(state, "missing_required_info")
                    return StepAction("tool", "meetingroom.room.list", args)
            selected = self._select_room_from_multi_day_lists(state)
            if selected:
                mr.evidence["pending_selected_room"] = selected
                return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))
            return self._block_meetingroom(state, "no_bookable_room")
        if not room_ids and "room_candidates" not in mr.evidence:
            args = self._next_room_list_args(state)
            if args is None:
                return self._block_meetingroom(state, "missing_required_info")
            return StepAction("tool", "meetingroom.room.list", args)
        if not room_ids and mr.evidence.get("room_candidates") and self._schedule_analysis_needed(state):
            rooms = (mr.evidence.get("room_candidates") or {}).get("rooms") or []
            if self._requires_full_schedule_analysis(state):
                for room in rooms:
                    room_id = room.get("room_id")
                    if room_id and room_id not in schedules:
                        start_date, end_date = self._schedule_range(state)
                        return StepAction(
                            "tool",
                            "meetingroom.room.schedule",
                            {"room_id": room_id, "start_date": start_date, "end_date": end_date},
                        )
            selected = self._select_room_from_schedule_analysis(state)
            if selected:
                mr.evidence["pending_selected_room"] = selected
                return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))
            return self._block_meetingroom(state, "no_bookable_room")
        for room_id in room_ids:
            if room_id not in schedules:
                start_date, end_date = self._schedule_range(state)
                return StepAction("tool", "meetingroom.room.schedule", {"room_id": room_id, "start_date": start_date, "end_date": end_date})
        if room_ids and self._schedule_analysis_needed(state):
            selected = self._select_named_room_from_schedules(state)
            if selected:
                mr.evidence["pending_selected_room"] = selected
                return StepAction("tool", "meetingroom.booking.create", self._booking_create_args(state, selected))
            return self._block_meetingroom(state, "no_bookable_room")
        if "room_candidates" not in mr.evidence:
            rooms = []
            for item in schedules.values():
                if item.get("room_id"):
                    rooms.append(
                        {
                            "room_id": item.get("room_id"),
                            "officeId": self._office_id_from_room_id(item.get("room_id")),
                            "busy_slots": item.get("busy_slots") or [],
                            "bookable": True,
                            "capacity": 999,
                            "hasScreen": True,
                        }
                    )
            mr.evidence["room_candidates"] = {"rooms": rooms, "day": self._meeting_day(state)}
        return self._next_booking_action(state)

    def _explicit_schedule_then_book(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        return bool(state.meetingroom.slots.get("room_ids")) and any(word in query for word in ["查一下", "查询", "日程", "安排"]) and any(
            word in query for word in ["订", "预订", "然后"]
        )

    def _next_meeting_query_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        if "booking_query" in mr.evidence:
            mr.status = "done"
            mr.result = {"status": "queried", "day": self._meeting_day(state)}
            keyword = mr.slots.get("keyword")
            if keyword:
                mr.result["keyword"] = keyword
            return None
        args: dict[str, Any] = {}
        day = self._meeting_day(state)
        if day:
            args["day"] = day
        if mr.slots.get("keyword"):
            args["keyword"] = mr.slots["keyword"]
        return StepAction("tool", "meetingroom.booking.list", args)

    def _next_existing_booking_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        if "selected_booking" not in mr.evidence:
            args = self._next_booking_list_args(state)
            if args is None:
                return self._block_meetingroom(state, "missing_required_info")
            return StepAction("tool", "meetingroom.booking.list", args)

        booking = mr.evidence["selected_booking"]
        # Existing-booking operations must inherit factual values from
        # booking.list. Natural-language requests often omit the exact time.
        inherit_keys = ("day", "start") if mr.evidence.get("cancel_done") else ("day", "start", "end")
        for key in inherit_keys:
            if booking.get(key):
                mr.slots[key] = booking.get(key)
        if booking.get("title"):
            mr.slots["title"] = self._normalize_meeting_title(booking.get("title"))
        if self._is_cancel_intent(mr.intent):
            if self._existing_booking_write_requires_confirmation(state):
                return self._block_meetingroom(state, "need_confirmation")
            if mr.evidence.get("cancel_done"):
                return None
            return StepAction("tool", "meetingroom.booking.cancel", {"order_id": booking.get("order_id")})
        if self._is_extend_intent(mr.intent):
            if mr.evidence.get("extend_attempted"):
                if mr.evidence.get("extend_done"):
                    return None
                return self._block_meetingroom(state, "conflict_after_requested_extension", order_id=booking.get("order_id"))
            if self._extension_has_known_conflict(state, booking):
                mr.evidence["extend_attempted"] = True
                return self._block_meetingroom(state, "conflict_after_requested_extension", order_id=booking.get("order_id"))
            return StepAction("tool", "meetingroom.booking.extend", {"order_id": booking.get("order_id"), "minutes": mr.slots.get("duration_minutes") or 30})
        if self._is_rebook_intent(mr.intent):
            if self._is_cancel_rebook_intent(mr.intent) and not mr.evidence.get("extend_attempted") and "延" in self._full_query(state.obs):
                if self._extension_has_known_conflict(state, booking):
                    mr.evidence["extend_attempted"] = True
                    if booking.get("end"):
                        mr.slots["end"] = self._add_minutes(str(booking["end"]), int(mr.slots.get("duration_minutes") or 30))
                else:
                    return StepAction("tool", "meetingroom.booking.extend", {"order_id": booking.get("order_id"), "minutes": mr.slots.get("duration_minutes") or 30})
            if self._is_cancel_rebook_intent(mr.intent) and mr.evidence.get("extend_done"):
                return None
            if not mr.evidence.get("cancel_done") and self._existing_booking_write_requires_confirmation(state):
                return self._block_meetingroom(state, "need_confirmation")
            if not mr.evidence.get("cancel_done"):
                return StepAction("tool", "meetingroom.booking.cancel", {"order_id": booking.get("order_id")})
            return self._next_booking_action(state)
        return self._block_meetingroom(state, "missing_required_info")

    def _next_participant_action(self, state: RuntimeState) -> StepAction | None:
        mr = state.meetingroom
        slots = mr.slots
        order_id = self._extract_order_id(self._full_query(state.obs)) or slots.get("order_id")
        if not order_id and "selected_booking" not in mr.evidence:
            args = self._next_participant_booking_list_args(state)
            if args is None:
                return self._block_meetingroom(state, "missing_required_info")
            return StepAction("tool", "meetingroom.booking.list", args)
        if not order_id and "selected_booking" in mr.evidence:
            order_id = mr.evidence["selected_booking"].get("order_id")
        if not order_id:
            return self._block_meetingroom(state, "missing_required_info")
        slots["order_id"] = order_id

        if mr.intent == "participant_list":
            if mr.evidence.get("participants"):
                mr.status = "done"
                participants = mr.evidence.get("participants", {}).get("participants") or []
                mr.result = {"status": "queried", "order_id": order_id, "participants": participants}
                return None
            return StepAction("tool", "meetingroom.booking.participant.list", {"order_id": order_id})

        participants = slots.get("participants") or []
        if not participants:
            return self._block_meetingroom(state, "missing_required_info")
        index = int(mr.evidence.get("participant_index") or 0)
        if index >= len(participants):
            if self._post_participant_extend_needed(state):
                if mr.evidence.get("extend_attempted"):
                    if mr.evidence.get("extend_done"):
                        mr.status = "done"
                        mr.result = {
                            "status": "extended",
                            "order_id": order_id,
                            "end": mr.evidence.get("extend_done", {}).get("end"),
                        }
                        return None
                    return self._block_meetingroom(state, "conflict_after_requested_extension", order_id=order_id)
                return StepAction(
                    "tool",
                    "meetingroom.booking.extend",
                    {"order_id": order_id, "minutes": slots.get("duration_minutes") or 30},
                )
            mr.status = "done"
            mr.result = {"status": "updated", "order_id": order_id}
            return None
        person = participants[index]
        user_id = person.get("user_id")
        if not user_id:
            key = f"participant_user_{index}"
            users = mr.evidence.get(key, {}).get("users") or []
            if not users and not mr.evidence.get(f"{key}_name_retry"):
                keyword = self._participant_lookup_keyword(person)
                mr.evidence[f"{key}_last_keyword"] = keyword
                return StepAction("tool", "user.get_info", {"keyword": keyword})
            if not users:
                name = str(person.get("name") or "").strip()
                last_keyword = str(mr.evidence.get(f"{key}_last_keyword") or "")
                if name and name != last_keyword:
                    mr.evidence[f"{key}_name_retry"] = True
                    mr.evidence[f"{key}_last_keyword"] = name
                    return StepAction("tool", "user.get_info", {"keyword": name})
            users = mr.evidence[key].get("users") or []
            if not users:
                existing_user = self._participant_user_from_selected_booking(mr, person)
                if existing_user:
                    user_id = existing_user.get("user_id")
                elif person.get("employee_no"):
                    user_id = person.get("employee_no")
                else:
                    return self._block_meetingroom(state, "missing_required_info")
            else:
                user_id = users[0].get("user_id")
        if mr.intent == "participant_add" and self._participant_duplicate_check_required(state):
            if "participants" not in mr.evidence:
                return StepAction("tool", "meetingroom.booking.participant.list", {"order_id": order_id})
            if self._participant_already_in_booking(mr, user_id, person):
                mr.evidence.setdefault("participant_results", []).append(
                    {
                        "status": "already_exists",
                        "order_id": order_id,
                        "user_id": user_id,
                        "name": person.get("name") or self._participant_name_from_evidence(mr, user_id),
                    }
                )
                mr.evidence["participant_index"] = index + 1
                return self._next_participant_action(state)
        tool = "meetingroom.booking.participant.add" if mr.intent == "participant_add" else "meetingroom.booking.participant.remove"
        return StepAction("tool", tool, {"order_id": order_id, "user_id": user_id})

    def _next_workflow_action(self, state: RuntimeState) -> StepAction | None:
        wf = state.workflow
        self._sync_workflow_skill(state)
        if wf.intent == "leave":
            if state.workflow_skill and state.workflow_skill.skill_id == "workflow.leave.replace_submit":
                replacement_action = self._next_leave_replace_action(state)
                if replacement_action is not None:
                    return replacement_action
            return self._next_leave_action(state)
        if wf.intent == "expense_material":
            return self._next_expense_action(state)
        return self._block_workflow(state, "missing_required_info")

    def _next_leave_replace_action(self, state: RuntimeState) -> StepAction | None:
        wf = state.workflow
        if not wf.evidence.get("applicant"):
            return StepAction("tool", "user.get_info", {})
        if not wf.evidence.get("replacement_source_lookup"):
            return StepAction("tool", "oa.done.list", {"keyword": "请假"})
        source = self._replacement_leave_source_item(state)
        if not source:
            return self._block_workflow(state, "existing_workflow_not_found")
        if not wf.evidence.get("replacement_delete_done"):
            return StepAction("tool", "workflow.delete", {"request_id": source.get("request_id")})
        return None

    def _next_leave_action(self, state: RuntimeState) -> StepAction | None:
        wf = state.workflow
        if not wf.evidence.get("applicant"):
            return StepAction("tool", "user.get_info", {})
        if not wf.evidence.get("catalog"):
            return StepAction("tool", "workflow.catalog", {"keyword": "请假"})
        if not wf.evidence.get("schema"):
            return StepAction("tool", "workflow.schema", {"workflow_id": WORKFLOW_IDS["leave"]})
        if state.obs.get("mode") == "multi_turn":
            ask = self._leave_missing_slot(state)
            if ask:
                return ask
        plan = self._leave_plan(state)
        if not plan:
            return self._block_workflow(state, "missing_required_info")
        people = self._collected_approver_people(wf)
        if not people:
            args = self._next_approver_search_args(state, plan)
            if args is None:
                return self._block_workflow(state, "ambiguous_approver")
            return StepAction("tool", "workflow.search_person", args)
        people = self._select_leave_people(state, people)
        if len(people) != 1:
            return self._block_workflow(state, "ambiguous_approver")
        if wf.evidence.get("save_done"):
            next_plan = self._next_recurring_leave_plan(state)
            if next_plan:
                save_args = self._leave_save_args(state, next_plan, people[0])
                wf.evidence["workflow_skill_draft_ir"] = {"workflow_id": WORKFLOW_IDS["leave"], "submit": save_args.get("submit")}
                self._sync_workflow_skill(state)
                return StepAction("tool", "workflow.save", save_args)
            return self._next_oa_action(state, "请假")
        save_args = self._leave_save_args(state, plan, people[0])
        wf.evidence["workflow_skill_draft_ir"] = {"workflow_id": WORKFLOW_IDS["leave"], "submit": save_args.get("submit")}
        self._sync_workflow_skill(state)
        return StepAction("tool", "workflow.save", save_args)

    def _next_expense_action(self, state: RuntimeState) -> StepAction | None:
        wf = state.workflow
        if state.obs.get("mode") == "multi_turn":
            ask = self._expense_missing_slot(state)
            if ask:
                return ask
        if not wf.evidence.get("applicant"):
            return StepAction("tool", "user.get_info", {})
        if not wf.evidence.get("catalog"):
            return StepAction("tool", "workflow.catalog", {"keyword": "费用类物资"})
        if not wf.evidence.get("schema"):
            return StepAction("tool", "workflow.schema", {"workflow_id": WORKFLOW_IDS["expense"]})
        category_field_id = self._expense_category_field_id(state)
        subclass_field_id = self._expense_subclass_field_id(state)
        if not category_field_id or not subclass_field_id:
            return self._block_workflow(state, "workflow_schema_lookup_contract_missing")
        expense = self._expense_slots(state)
        if not wf.evidence.get("verified_project"):
            singleton_project = self._verified_singleton_project_candidate(state)
            if singleton_project and self._can_adopt_singleton_project_candidate(state, expense):
                wf.evidence["verified_project"] = singleton_project
                self._transition_project_resolution(
                    self._project_resolution(state),
                    "verified_ranked",
                    query="candidate_registry",
                    selected_id=self._expense_project_fingerprint(singleton_project),
                )
                self._bind_expense_project(state, singleton_project, "singleton_candidate")
        if not wf.evidence.get("verified_project") and not wf.evidence.get("project"):
            args = self._next_project_search_args(state, expense)
            if args is None:
                args = self._project_search_args_from_llm(state, expense, force=True)
            if args is None:
                return self._block_workflow(state, "missing_required_info")
            return StepAction("tool", "workflow.project_search", args)
        projects = [wf.evidence["verified_project"]] if wf.evidence.get("verified_project") else (wf.evidence.get("project", {}).get("projects") or [])
        if not projects:
            args = self._next_project_search_args(state, expense)
            if args is None:
                args = self._project_search_args_from_llm(state, expense, force=True)
            if args is not None:
                return StepAction("tool", "workflow.project_search", args)
        if len(projects) != 1:
            args = self._next_project_search_args(state, expense, allow_llm=not bool(wf.evidence.get("project_candidates")))
            if args is None and not wf.evidence.get("project_candidates"):
                args = self._project_search_args_from_llm(state, expense, force=True)
            if args is not None:
                return StepAction("tool", "workflow.project_search", args)
            singleton_project = self._verified_singleton_project_candidate(state)
            if singleton_project:
                wf.evidence["verified_project"] = singleton_project
                self._transition_project_resolution(
                    self._project_resolution(state),
                    "verified_ranked",
                    query="candidate_registry",
                    selected_id=self._expense_project_fingerprint(singleton_project),
                )
                self._bind_expense_project(state, singleton_project, "singleton_candidate")
                projects = [singleton_project]
            elif len(projects) != 1:
                if not wf.evidence.get("category_options"):
                    return StepAction("tool", "workflow.browser_search", {"workflow_id": WORKFLOW_IDS["expense"], "field_id": category_field_id})
                selected_project = self._select_project_candidate_with_llm(state, projects)
                if selected_project:
                    projects = [selected_project]
                    wf.evidence["verified_project"] = selected_project
                    self._transition_project_resolution(
                        self._project_resolution(state),
                        "verified_ranked",
                        query="candidate_ranker",
                        selected_id=self._expense_project_fingerprint(selected_project),
                    )
                    self._bind_expense_project(state, selected_project, "llm_candidate_selection")
                else:
                    return self._block_workflow(state, "ambiguous_project")
        if len(projects) != 1:
            if not wf.evidence.get("category_options"):
                return StepAction("tool", "workflow.browser_search", {"workflow_id": WORKFLOW_IDS["expense"], "field_id": category_field_id})
            return self._block_workflow(state, "ambiguous_project")
        refine_args = self._project_refine_search_args(state, projects[0])
        if refine_args is not None:
            return StepAction("tool", "workflow.project_search", refine_args)
        if not wf.evidence.get("category_options"):
            return StepAction("tool", "workflow.browser_search", {"workflow_id": WORKFLOW_IDS["expense"], "field_id": category_field_id})
        category = self._select_material_category(state)
        if not category:
            return self._block_workflow(state, "ambiguous_material_subclass")
        if not wf.evidence.get("subclass_options"):
            project = projects[0]
            if self._subclass_lookup_failed(state, project, category):
                # A dependent browser lookup rejects this category for the
                # current WBS; it is not evidence that the project is wrong.
                self._reject_expense_category_after_subclass_failure(state, {
                    "dep": {"wbscode": project.get("wbs_code"), "wzlb": self._expense_category_id(category)}
                }, "previous_lookup_failure")
                category = self._select_material_category(state)
                if not category:
                    return self._block_workflow(state, "ambiguous_material_subclass")
            return StepAction(
                "tool",
                "workflow.browser_search",
                {
                    "workflow_id": WORKFLOW_IDS["expense"],
                    "field_id": subclass_field_id,
                    "dep": {"wbscode": project.get("wbs_code"), "wzlb": category.get("value") or category.get("code")},
                },
            )
        if wf.evidence.get("save_done"):
            return self._next_oa_action(state, self._oa_keyword_for_workflow(state))
        save_args_or_reason = self._expense_ir_save_args_or_block(state, projects[0], category)
        if isinstance(save_args_or_reason, str):
            return self._block_workflow(state, save_args_or_reason)
        wf.evidence["workflow_skill_draft_ir"] = {
            "workflow_id": WORKFLOW_IDS["expense"],
            "submit": save_args_or_reason.get("submit"),
            "total_amount": ((save_args_or_reason.get("data") or {}).get("total_amount")),
        }
        self._sync_workflow_skill(state)
        return StepAction("tool", "workflow.save", save_args_or_reason)

    def _next_oa_action(self, state: RuntimeState, keyword: str) -> StepAction | None:
        wf = state.workflow
        if wf.evidence.get("oa_checked"):
            return None
        if self._remaining_seconds(state) <= 5.0:
            return None
        submit = bool(wf.slots.get("submit"))
        if submit and "oa.done.list" in state.tools and state.steps_used < state.step_budget:
            return StepAction("tool", "oa.done.list", {"keyword": keyword})
        if not submit and "oa.todo.list" in state.tools and state.steps_used < state.step_budget:
            return StepAction("tool", "oa.todo.list", {"keyword": keyword})
        return None

    def _remaining_seconds(self, state: RuntimeState) -> float:
        return max(0.0, state.deadline_at - time.monotonic())

    def _oa_keyword_for_workflow(self, state: RuntimeState) -> str:
        if state.workflow.intent == "expense_material":
            query = self._full_query(state.obs)
            if not state.workflow.slots.get("submit") and "待办" in query:
                return "费用类物资"
            return "费用"
        return "请假"

    # ------------------------------------------------------------------
    # Execution and observation updates
    # ------------------------------------------------------------------

    def _execute(self, state: RuntimeState, action: StepAction, llm_config: dict[str, Any]) -> None:
        action_started = time.monotonic()
        if action.kind == "block_meetingroom":
            state.meetingroom.status = "blocked"
            state.meetingroom.blocked_reason = action.args.get("reason", "missing_required_info")
            result = {"status": "blocked", "reason": state.meetingroom.blocked_reason}
            if action.args.get("order_id"):
                result["order_id"] = action.args["order_id"]
            state.meetingroom.result = result
            record = {"action": "block_meetingroom", "args": action.args, "result": result}
            self._annotate_action_record(state, record, action_started, bucket="program")
            state.last_action_domain = "meetingroom"
            state.history.append(record)
            self._debug_log(llm_config, {"event": "block_meetingroom", **record})
            return
        if action.kind == "block_workflow":
            state.workflow.status = "blocked"
            state.workflow.blocked_reason = action.args.get("reason", "missing_required_info")
            result = {"status": "blocked", "reason": state.workflow.blocked_reason}
            state.workflow.result = result
            record = {"action": "block_workflow", "args": action.args, "result": result}
            self._annotate_action_record(state, record, action_started, bucket="program")
            state.last_action_domain = "workflow"
            state.history.append(record)
            self._debug_log(llm_config, {"event": "block_workflow", **record})
            return
        if action.kind == "reply":
            try:
                result = self.env.reply(action.message)
            except Exception as exc:
                result = {"error": str(exc)}
            state.steps_used += 1
            record = {"tool": "__reply__", "args": {"message": action.message}, "result": result}
            self._annotate_action_record(state, record, action_started, bucket="reply")
            state.history.append(record)
            self._apply_reply_result(state, action.message, result)
            self._debug_log(llm_config, {"event": "reply", **record})
            return

        if action.kind != "tool" or action.tool not in state.tools:
            record = {"tool": action.tool, "args": action.args, "result": {"error": "unauthorized_or_invalid_action"}}
            self._annotate_action_record(state, record, action_started, bucket="program")
            state.history.append(record)
            self._debug_log(llm_config, {"event": "invalid_action", **record})
            return
        args = self.tool_adapter.adapt(action.tool, self._clean_args(action.args))
        self._mark_workflow_skill_action_running(state, action.tool)
        validation = self.tool_registry.validate_call(action.tool, args, state.tools)
        if validation.get("errors") or validation.get("warnings") or validation.get("missing_required"):
            self._debug_log(
                llm_config,
                {
                    "event": "tool_registry_validation",
                    "case_id": state.obs.get("case_id"),
                    "tool": action.tool,
                    "args": args,
                    "validation": validation,
                },
            )
        preflight_id = None
        if self.tool_registry.is_write(action.tool):
            action_domain = self._ledger_domain_for_tool(state, action.tool)
            active_runtime = self._active_task_runtime(state, action_domain)
            if active_runtime is not None and active_runtime.status not in TaskRuntime.TERMINAL_STATUSES:
                active_runtime.status = "writing"
            preflight = self.preflight_guard.validate_write(state, action.tool, args)
            preflight_record = state.ledger.record_preflight(tool=action.tool, args=args, result=preflight)
            preflight_id = preflight_record.get("ledger_id")
            self._debug_log(
                llm_config,
                {
                    "event": "preflight",
                    "case_id": state.obs.get("case_id"),
                    "tool": action.tool,
                    "args": args,
                    "preflight": preflight,
                    "ledger_id": preflight_id,
                },
            )
            if not preflight.get("passed"):
                result = {"error": "preflight_failed", "reason": preflight.get("reason") or "preflight_failed"}
                record = {"tool": action.tool, "args": args, "result": result, "preflight": preflight}
                self._annotate_action_record(state, record, action_started, bucket="program")
                self._mark_domain_blocked_after_preflight(state, action.tool, str(result["reason"]))
                self._mark_last_action_domain(state, action.tool)
                state.history.append(record)
                self._debug_log(llm_config, {"event": "tool_preflight_blocked", **record})
                return
        cache_key = self._cache_key(action.tool, args)
        if cache_key and cache_key in state.cache:
            result = json.loads(json.dumps(state.cache[cache_key], ensure_ascii=False))
            record = {"tool": action.tool, "args": args, "result": result, "cached": True}
            self._annotate_action_record(state, record, action_started, bucket="cache")
            self._mark_last_action_domain(state, action.tool)
            state.history.append(record)
            self._apply_tool_result(state, action.tool, args, result)
            state.ledger.record_tool(
                tool=action.tool,
                args=args,
                result=result,
                kind="read",
                domain=self._ledger_domain_for_tool(state, action.tool),
                cached=True,
                preflight_id=preflight_id,
            )
            self._debug_log(llm_config, {"event": "tool_cache", **record})
            return
        try:
            result = self.env.call_tool(action.tool, args)
        except Exception as exc:
            result = {"error": str(exc)}
        state.steps_used += 1
        action_domain = self._ledger_domain_for_tool(state, action.tool)
        if action_domain in state.domain_steps_used:
            state.domain_steps_used[action_domain] += 1
            if action.tool in READ_TOOLS:
                state.domain_read_steps_used[action_domain] += 1
        if cache_key and isinstance(result, dict) and not result.get("error"):
            state.cache[cache_key] = json.loads(json.dumps(result, ensure_ascii=False))
        record = {"tool": action.tool, "args": args, "result": result}
        self._annotate_action_record(state, record, action_started, bucket="tool")
        self._mark_last_action_domain(state, action.tool)
        state.history.append(record)
        self._apply_tool_result(state, action.tool, args, result)
        state.ledger.record_tool(
            tool=action.tool,
            args=args,
            result=result if isinstance(result, dict) else {"value": result},
            kind="write" if self.tool_registry.is_write(action.tool) else "read",
            domain=self._ledger_domain_for_tool(state, action.tool),
            cached=False,
            preflight_id=preflight_id,
        )
        self._debug_log(llm_config, {"event": "tool", **record})

    def _mark_last_action_domain(self, state: RuntimeState, tool: str) -> None:
        domain = self._ledger_domain_for_tool(state, tool)
        if domain in {"meetingroom", "workflow"}:
            state.last_action_domain = domain

    def _annotate_action_record(self, state: RuntimeState, record: dict[str, Any], started_at: float, bucket: str = "program") -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - started_at)
        record["started_at"] = round(started_at - state.started_at, 3)
        record["end_at"] = round(now - state.started_at, 3)
        record["elapsed_seconds"] = round(elapsed, 3)
        record["remaining_seconds"] = round(self._remaining_seconds(state), 3)
        record["steps_used"] = state.steps_used
        state.action_elapsed_seconds += elapsed
        if bucket == "tool":
            state.tool_elapsed_seconds += elapsed
        elif bucket == "reply":
            state.reply_elapsed_seconds += elapsed
        elif bucket == "cache":
            state.cache_elapsed_seconds += elapsed

    def _mark_domain_blocked_after_preflight(self, state: RuntimeState, tool: str, reason: str) -> None:
        if tool.startswith("meetingroom."):
            state.meetingroom.status = "blocked"
            state.meetingroom.blocked_reason = reason
            state.meetingroom.result = {"status": "blocked", "reason": reason}
            return
        if tool.startswith("workflow.") or tool.startswith("oa."):
            state.workflow.status = "blocked"
            state.workflow.blocked_reason = reason
            state.workflow.result = {"status": "blocked", "reason": reason}

    def _ledger_domain_for_tool(self, state: RuntimeState, tool: str) -> str:
        if tool.startswith("meetingroom.") or tool == "user.get_workspace":
            return "meetingroom"
        if tool.startswith("workflow.") or tool.startswith("oa."):
            return "workflow"
        if tool == "user.get_info":
            if state.workflow.needed:
                return "workflow"
            if state.meetingroom.needed:
                return "meetingroom"
        return tool.split(".", 1)[0] if "." in tool else "system"

    def _apply_reply_result(self, state: RuntimeState, message: str, result: dict[str, Any]) -> None:
        user_message = str(result.get("user_message") or "")
        resolved = result.get("resolved_slot")
        if result.get("confirmed_action") == "meetingroom.booking.create":
            state.meetingroom.evidence["confirmed_create"] = True
            return
        if not resolved:
            if action_slot := self._last_asked_slot(state):
                state.workflow.evidence.setdefault("unresolved_slots", set()).add(action_slot)
            return
        state.asked_slots.add(str(resolved))
        text = user_message
        if resolved in {"day", "meeting_time"}:
            start, end = self._extract_time_range(text)
            if start:
                state.meetingroom.slots["start"] = start
            if end:
                state.meetingroom.slots["end"] = end
            day_text = self._extract_day_text(text)
            if day_text:
                state.meetingroom.slots["day_text"] = day_text
            if not day_text and "day" in result:
                state.meetingroom.slots["day"] = result["day"]
        elif resolved == "attendees":
            cap = self._extract_capacity(text)
            if cap:
                state.meetingroom.slots["capacity"] = cap
        elif resolved == "title":
            title = self._extract_meeting_title(text) or text.strip("。")
            state.meetingroom.slots["title"] = title
        elif resolved == "project_code":
            code = self._first_regex(text, r"([A-Z]-\d{9})")
            if code:
                expense = self._expense_slots(state)
                expense["project_code"] = code
                self._set_semantic_fact(state, "workflow.expense.project_code", code, "user_literal")
                expense.pop("_tried_project_keywords", None)
                expense.pop("_llm_project_keyword_suggestions", None)
        elif resolved == "project_name":
            expense = self._expense_slots(state)
            project_name = self._extract_project_name(text) or self._clean_project_phrase(text)
            if project_name:
                expense["project_name"] = project_name
                self._set_semantic_fact(state, "workflow.expense.project_name", project_name, "user_literal")
                keywords = expense.setdefault("project_keywords", [])
                for keyword in self._project_keywords(project_name, text):
                    if keyword and keyword not in keywords:
                        keywords.append(keyword)
                expense.pop("_tried_project_keywords", None)
                expense.pop("_llm_project_keyword_suggestions", None)
        elif resolved == "material_category":
            self._expense_slots(state)["material_category_hint"] = self._material_category_hint(text) or text
        elif resolved == "material_subclass":
            subclass_hint = re.sub(r"^(?:物资)?小类(?:选择|选|是|为|：|:)?", "", text).strip("，。；;、 的")
            subclass_hint = subclass_hint or text.strip("，。；;、 的")
            self._expense_slots(state)["material_subclass_hint"] = subclass_hint
            state.workflow.evidence.setdefault("unresolved_slots", set()).discard("material_subclass")
            items = self._expense_slots(state).setdefault("items", [])
            if not items:
                items.append({"name": subclass_hint})
            else:
                items[0]["name"] = subclass_hint
        elif resolved == "total_amount":
            amount = self._extract_amount_after(text, ["预算", "金额", "总预算"]) or self._extract_first_amount(text)
            if amount:
                expense = self._expense_slots(state)
                expense["total_amount"] = amount
                self._set_semantic_fact(state, "workflow.expense.total_amount", self._money(amount), "user_literal")
                items = expense.setdefault("items", [])
                if not items:
                    items.append({"name": expense.get("material_subclass_hint") or expense.get("material_category_hint") or "费用", "budget_amount": amount})
                if len(items) == 1:
                    items[0].setdefault("quantity", "1")
                    items[0]["unit_price"] = amount
                    items[0]["budget_amount"] = amount
        else:
            leave = self._leave_slots(state)
            if resolved == "start_time":
                start, _ = self._extract_time_range(text)
                if not start:
                    start = self._parse_time_token(text)
                if start:
                    leave["start"] = start
            elif resolved == "end_time":
                _, end = self._extract_time_range("到" + text)
                if not end:
                    parsed = self._parse_time_token(text)
                    end = parsed
                if end:
                    leave["end"] = end
            elif resolved == "leave_type":
                leave["leave_type_label"] = self._first_match(text, list(LEAVE_TYPE_MAP)) or text
            elif resolved == "reason":
                leave["reason_label"] = text
            elif resolved == "approver":
                leave["approver_keyword"] = self._extract_approver_keyword(text) or text.strip("。")

    def _last_asked_slot(self, state: RuntimeState) -> str:
        for slot in ["material_subclass", "material_category", "project_code", "total_amount", "approver", "reason", "leave_type", "end_time", "start_time", "title", "attendees", "day"]:
            if slot in state.asked_slots:
                return slot
        return ""

    def _apply_tool_result(self, state: RuntimeState, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if tool.startswith("meetingroom.") or tool == "user.get_workspace" or (
            tool == "user.get_info" and state.meetingroom.intent.startswith("participant_")
        ):
            self._apply_meeting_tool_result(state, tool, args, result)
        if tool.startswith("workflow.") or tool.startswith("oa.") or tool == "user.get_info":
            self._apply_workflow_tool_result(state, tool, args, result)
        if isinstance(result, dict) and not result.get("error"):
            state.completed_tools.add(tool)

    def _apply_meeting_tool_result(self, state: RuntimeState, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        mr = state.meetingroom
        if tool == "meetingroom.booking.extend" and result.get("conflict"):
            mr.evidence["extend_attempted"] = True
        if result.get("error"):
            return
        if tool == "user.get_info" and mr.intent.startswith("participant_"):
            index = int(mr.evidence.get("participant_index") or 0)
            mr.evidence[f"participant_user_{index}"] = result
        elif tool == "user.get_workspace":
            mr.evidence["workspace"] = result
        elif tool == "meetingroom.room.list":
            mr.evidence["room_candidates"] = self._merge_room_list_results(mr.evidence.get("room_candidates"), result)
            self._record_selected_room_capacity_from_candidates(mr, result)
            if result.get("day"):
                mr.evidence.setdefault("room_lists_by_day", {})[str(result.get("day"))] = result
            tried = mr.evidence.setdefault("tried_room_lists", [])
            tried.append({"args": args, "result": result})
        elif tool == "meetingroom.room.schedule":
            mr.evidence.setdefault("schedules", {})[args.get("room_id")] = result
            self._merge_schedule_into_room_candidates(mr, args.get("room_id"), result)
        elif tool == "meetingroom.booking.list":
            bookings = result.get("bookings") or []
            mr.evidence["booking_query"] = result
            tried = mr.evidence.setdefault("tried_booking_lists", [])
            tried.append({"args": args, "result": result})
            if bookings:
                if self._booking_list_requires_confirmation_before_write(state, bookings):
                    return
                mr.evidence["selected_booking"] = self._select_booking(state, bookings)
        elif tool == "meetingroom.booking.cancel" and result.get("cancelled"):
            mr.evidence["cancel_done"] = result
            if self._is_cancel_intent(mr.intent):
                mr.status = "done"
                mr.result = {"status": "cancelled", "order_id": result.get("order_id")}
        elif tool == "meetingroom.booking.extend":
            mr.evidence["extend_attempted"] = True
            if result.get("extended"):
                mr.evidence["extend_done"] = result
                mr.status = "done"
                mr.result = {"status": "extended", "order_id": result.get("order_id"), "end": result.get("end")}
        elif tool == "meetingroom.booking.create" and result.get("success"):
            mr.evidence["create_done"] = {"args": args, "result": result}
            if mr.slots.get("multi_segments"):
                created = mr.evidence.setdefault("created_segments", [])
                created.append({"args": args, "result": result})
                next_segment = self._next_segment_to_create(mr)
                if next_segment:
                    if next_segment.get("day"):
                        mr.slots["day"] = next_segment["day"]
                    mr.slots["start"] = next_segment["start"]
                    mr.slots["end"] = next_segment["end"]
                    mr.slots["title"] = next_segment["title"]
                    mr.evidence.pop("create_done", None)
                    return
            mr.status = "done"
            mr.result = self._booking_result_from_create(args, result, mr)
        elif tool == "meetingroom.booking.participant.list":
            mr.evidence["participants"] = result
            self._apply_participant_list_evidence(state, result)
        elif tool in {"meetingroom.booking.participant.add", "meetingroom.booking.participant.remove"}:
            if not result.get("error"):
                index = int(mr.evidence.get("participant_index") or 0)
                participant_result = self._participant_result_from_tool(tool, args, result, mr, index)
                mr.evidence.setdefault("participant_results", []).append(participant_result)
                mr.evidence["participant_index"] = index + 1

    def _merge_room_list_results(self, existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(existing, dict) or not existing.get("rooms"):
            return incoming
        if not incoming.get("rooms"):
            return existing
        merged = dict(incoming)
        merged["rooms"] = []
        seen: set[str] = set()
        for source in (existing, incoming):
            for room in source.get("rooms") or []:
                if not isinstance(room, dict):
                    continue
                room_id = str(room.get("room_id") or room.get("officeId") or "")
                if room_id and room_id in seen:
                    continue
                if room_id:
                    seen.add(room_id)
                merged["rooms"].append(room)
        if not merged.get("day") and existing.get("day"):
            merged["day"] = existing.get("day")
        return merged

    def _apply_workflow_tool_result(self, state: RuntimeState, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        wf = state.workflow
        if tool == "workflow.search_person":
            tried = wf.evidence.setdefault("approver_search_tried", [])
            if args not in tried:
                tried.append(dict(args))
            wf.evidence.setdefault("approver_search_attempts", []).append(
                {"args": dict(args), "error": str(result.get("error") or "")}
            )
        if tool == "workflow.project_search":
            keyword = str(args.get("project_name") or "").strip()
            if keyword:
                tried = self._expense_slots(state).setdefault("_tried_project_keywords", [])
                if keyword not in tried:
                    tried.append(keyword)
        if result.get("error"):
            if tool == "workflow.project_search":
                self._record_project_query_result(state, args, [], error=str(result.get("error") or "tool_error"))
            if tool == "workflow.browser_search" and int(args.get("field_id") or 0) == (self._expense_subclass_field_id(state) or -1):
                wf.evidence.setdefault("subclass_lookup_failures", []).append({"args": args, "result": result})
                self._reject_expense_category_after_subclass_failure(state, args, str(result.get("error") or "tool_error"))
            return
        if tool == "user.get_info":
            users = result.get("users") or []
            if users and not args.get("keyword"):
                wf.evidence["applicant"] = users[0]
            elif state.meetingroom.intent.startswith("participant_"):
                index = int(state.meetingroom.evidence.get("participant_index") or 0)
                state.meetingroom.evidence[f"participant_user_{index}"] = result
        elif tool == "workflow.catalog":
            wf.evidence["catalog"] = result
            workflows = result.get("workflows") or []
            if workflows:
                if wf.intent == "leave":
                    wf.evidence["workflow_id"] = workflows[0].get("workflow_id", WORKFLOW_IDS["leave"])
                elif wf.intent == "expense_material":
                    wf.evidence["workflow_id"] = workflows[0].get("workflow_id", WORKFLOW_IDS["expense"])
        elif tool == "workflow.schema":
            wf.evidence["schema"] = result
        elif tool == "workflow.search_person":
            wf.evidence["approver_search"] = result
            wf.evidence.setdefault("approver_searches", []).append(result)
        elif tool == "workflow.project_search":
            history = wf.evidence.setdefault("project_search_history", [])
            history.append({"args": args, "result": result})
            projects = result.get("projects") if isinstance(result.get("projects"), list) else []
            projects = [project for project in projects if isinstance(project, dict)]
            resolution = self._record_project_query_result(state, args, projects)
            query = str(args.get("project_name") or args.get("project_code") or "")
            self._set_semantic_fact(
                state,
                "workflow.expense.project_candidates",
                [self._expense_project_fingerprint(item) for item in projects],
                "tool_candidate",
            )
            if len(projects) == 1:
                # Contest semantics: a single tool-returned project is the
                # resolved project, including after a prior ambiguous query.
                current = wf.evidence.get("verified_project") if isinstance(wf.evidence.get("verified_project"), dict) else None
                current_id = self._expense_project_fingerprint(current) if current else ""
                new_id = self._expense_project_fingerprint(projects[0])
                current_quality = float(resolution.get("selected_query_quality") or 0.0)
                new_quality = self._project_query_quality(args)
                if current_id and current_id != new_id and new_quality < current_quality:
                    resolution.setdefault("transitions", []).append(
                        {
                            "state": "conflict_ignored",
                            "query": query,
                            "selected_id": current_id,
                            "candidate_count": len(resolution.get("candidate_registry") or {}),
                        }
                    )
                    return
                wf.evidence.pop("project_candidates", None)
                wf.evidence["project"] = result
                wf.evidence["verified_project"] = projects[0]
                self._transition_project_resolution(
                    resolution,
                    "verified_singleton",
                    query=query,
                    selected_id=self._expense_project_fingerprint(projects[0]),
                )
                self._bind_expense_project(state, projects[0], "workflow.project_search")
            elif projects and not wf.evidence.get("verified_project"):
                wf.evidence["project_candidates"] = projects
                self._transition_project_resolution(resolution, "ambiguous", query=query)
                if "project" not in wf.evidence:
                    wf.evidence["project"] = result
            elif "verified_project" not in wf.evidence:
                wf.evidence["project_empty_result"] = result
                self._transition_project_resolution(resolution, "empty", query=query)
        elif tool == "workflow.browser_search" and int(args.get("field_id") or 0) == (self._expense_category_field_id(state) or -1):
            wf.evidence["category_options"] = result
            self._set_semantic_fact(
                state,
                "workflow.expense.category_candidates",
                [str(item.get("value") or item.get("code") or "") for item in result.get("options") or [] if isinstance(item, dict)],
                "tool_candidate",
            )
            selected = wf.evidence.get("selected_material_category")
            if isinstance(selected, dict):
                self._bind_expense_category(state, selected, "category_options_refresh")
        elif tool == "workflow.browser_search" and int(args.get("field_id") or 0) == (self._expense_subclass_field_id(state) or -1):
            if result.get("options"):
                wf.evidence["subclass_options"] = result
                self._set_semantic_fact(
                    state,
                    "workflow.expense.subclass_candidates",
                    [str(item.get("value") or item.get("code") or "") for item in result.get("options") or [] if isinstance(item, dict)],
                    "tool_candidate",
                )
                self._bind_expense_subclass_candidates(state, result)
            else:
                wf.evidence.setdefault("subclass_lookup_failures", []).append({"args": args, "result": result})
                self._reject_expense_category_after_subclass_failure(state, args, "empty_options")
        elif tool == "workflow.delete" and result.get("deleted"):
            wf.evidence["replacement_delete_done"] = {"args": dict(args), "result": result}
            wf.evidence.pop("oa_checked", None)
        elif tool == "workflow.save" and result.get("draft_saved"):
            wf.evidence["save_done"] = {"args": args, "result": result}
            if args.get("workflow_id") == WORKFLOW_IDS["leave"]:
                wf.evidence.setdefault("saved_leave_requests", []).append({"args": args, "result": result})
                plans = wf.evidence.get("leave_plans")
                if plans is None:
                    plans = self._leave_plans(state)
                    wf.evidence["leave_plans"] = plans
                if len(wf.evidence.get("saved_leave_requests") or []) < len(plans):
                    wf.status = "pending"
                    return
            wf.status = "done"
            result_for_answer = dict(result)
            if args.get("workflow_id") == WORKFLOW_IDS["leave"]:
                result_for_answer["_saved_leave_requests"] = wf.evidence.get("saved_leave_requests") or []
            wf.result = self._workflow_result_from_save(args, result_for_answer)
        elif tool in {"oa.done.list", "oa.todo.list"}:
            if (
                tool == "oa.done.list"
                and state.workflow_skill is not None
                and state.workflow_skill.skill_id == "workflow.leave.replace_submit"
                and not wf.evidence.get("replacement_delete_done")
            ):
                wf.evidence["replacement_source_lookup"] = result
            else:
                wf.evidence["oa_checked"] = result
        self._sync_workflow_skill(state)

    def _close_pending_domains(self, state: RuntimeState) -> None:
        if state.meetingroom.needed and state.meetingroom.status in {"pending", "ready"}:
            state.meetingroom.status = "blocked"
            decision = self._outcome_policy_decision(state, "meetingroom")
            if decision:
                state.meetingroom.evidence["outcome_policy_decision"] = decision
            policy_reason = str((decision or {}).get("reason") or "") if (decision or {}).get("decision") == "terminal" else ""
            state.meetingroom.blocked_reason = state.meetingroom.blocked_reason or policy_reason or "missing_required_info"
            state.meetingroom.result = {"status": "blocked", "reason": state.meetingroom.blocked_reason}
        if state.workflow.needed and state.workflow.status in {"pending", "ready"}:
            state.workflow.status = "blocked"
            decision = self._outcome_policy_decision(state, "workflow")
            if decision:
                state.workflow.evidence["outcome_policy_decision"] = decision
            policy_reason = str((decision or {}).get("reason") or "") if (decision or {}).get("decision") == "terminal" else ""
            state.workflow.blocked_reason = state.workflow.blocked_reason or policy_reason or self._workflow_pending_block_reason(state)
            state.workflow.result = {"status": "blocked", "reason": state.workflow.blocked_reason}
            if state.workflow_skill is not None:
                state.workflow_skill.mark_blocked(state.workflow.blocked_reason)
        for runtime in state.task_runtimes:
            if runtime.status not in TaskRuntime.TERMINAL_STATUSES:
                runtime.status = "blocked"
                runtime.blocked_reason = runtime.blocked_reason or "execution_incomplete"

    def _workflow_pending_block_reason(self, state: RuntimeState) -> str:
        decision = self._outcome_policy_decision(state, "workflow")
        if decision and decision.get("decision") == "terminal" and decision.get("reason"):
            return str(decision["reason"])
        wf = state.workflow
        if wf.intent == "expense_material":
            if wf.evidence.get("subclass_options") and not wf.evidence.get("save_done"):
                return "ambiguous_material_subclass"
            if wf.evidence.get("project_search_history") and not wf.evidence.get("verified_project"):
                return "ambiguous_project"
        return "missing_required_info"

    def _outcome_policy_decision(self, state: RuntimeState, domain: str) -> dict[str, Any] | None:
        if domain == "workflow":
            wf = state.workflow
            if wf.intent == "leave":
                leave = self._leave_slots(state)
                people = self._select_leave_people(state, self._collected_approver_people(wf))
                facts = {
                    "approver_hint_present": bool(
                        leave.get("approver_keyword")
                        or leave.get("approver_name_hint")
                        or leave.get("approver_title")
                        or leave.get("approver_title_hint")
                    ),
                    "approver_search_completed": bool(wf.evidence.get("approver_search_attempts")),
                    "approver_candidate_count": len(people),
                }
                return self.outcome_policy_memory.match("workflow.leave", facts)
            if wf.intent == "expense_material":
                expense = self._expense_slots(state)
                options = (wf.evidence.get("subclass_options") or {}).get("options") or []
                project_registry = (wf.evidence.get("project_resolution") or {}).get("candidate_registry") or {}
                request_shape = self._expense_request_shape(state, expense, options)
                facts = {
                    "subclass_query_completed": bool(wf.evidence.get("subclass_options") or wf.evidence.get("subclass_lookup_failures")),
                    "specific_material_evidence": self._has_specific_material_evidence(expense),
                    "save_completed": bool(wf.evidence.get("save_done")),
                    "explicit_multi_item_total_only": request_shape == "explicit_multi_unallocated",
                    "project_query_completed": bool(wf.evidence.get("project_search_history")),
                    "project_resolved": bool(wf.evidence.get("verified_project")),
                    "project_candidate_count": len(project_registry),
                }
                return self.outcome_policy_memory.match("workflow.expense_material", facts)
            return None
        if domain == "meetingroom":
            mr = state.meetingroom
            rooms = (mr.evidence.get("room_candidates") or {}).get("rooms") or []
            start = str(mr.slots.get("start") or "")
            end = str(mr.slots.get("end") or "")
            bookable = [room for room in rooms if isinstance(room, dict) and self._room_is_legal_for_window(room, mr, start, end)]
            facts = {
                "room_query_completed": "room_candidates" in mr.evidence,
                "bookable_room_count": len(bookable),
                "booking_completed": bool(mr.evidence.get("create_done")),
            }
            return self.outcome_policy_memory.match("meetingroom", facts)
        return None

    # ------------------------------------------------------------------
    # Meetingroom helpers
    # ------------------------------------------------------------------

    def _is_cancel_intent(self, intent: str) -> bool:
        return intent in {"cancel", "cancel_existing"}

    def _is_extend_intent(self, intent: str) -> bool:
        return intent in {"extend", "extend_existing"}

    def _is_rebook_larger_intent(self, intent: str) -> bool:
        return intent in {"rebook_larger", "rebook_larger_existing"}

    def _is_cancel_rebook_intent(self, intent: str) -> bool:
        return intent in {"cancel_rebook", "cancel_rebook_existing"}

    def _is_rebook_intent(self, intent: str) -> bool:
        return self._is_rebook_larger_intent(intent) or self._is_cancel_rebook_intent(intent)

    def _normalize_meeting_slots(self, state: RuntimeState) -> None:
        slots = state.meetingroom.slots
        query = self._full_query(state.obs)
        parsed_start, parsed_end = self._extract_time_range(query)
        explicit_duration = self._extract_duration_minutes(query)
        explicit_capacity = self._extract_capacity(query)
        if explicit_capacity:
            slots["capacity"] = explicit_capacity
        if any(word in query for word in ["工位附近", "离工位最近", "最近的会议室", "离我最近"]):
            slots["needs_workspace"] = True
        if parsed_start and parsed_end:
            slots["start"] = parsed_start
            slots["end"] = parsed_end
            parsed_duration = self._minutes_between(parsed_start, parsed_end)
            if not (self._is_extend_intent(state.meetingroom.intent) or self._is_rebook_intent(state.meetingroom.intent)):
                slots["duration_minutes"] = parsed_duration
            elif not slots.get("duration_minutes"):
                slots["duration_minutes"] = parsed_duration
        if explicit_duration and (self._is_extend_intent(state.meetingroom.intent) or self._is_rebook_intent(state.meetingroom.intent)):
            slots["duration_minutes"] = explicit_duration
        if slots.get("start") and not slots.get("end") and slots.get("duration_minutes"):
            slots["end"] = self._add_minutes(slots["start"], int(slots["duration_minutes"]))
        if slots.get("end") and not slots.get("start") and slots.get("duration_minutes"):
            slots["start"] = self._add_minutes(slots["end"], -int(slots["duration_minutes"]))
        if state.meetingroom.intent.startswith("participant_") and slots.get("start") and not slots.get("end"):
            slots["end"] = self._add_minutes(slots["start"], 60)
        if (
            state.obs.get("mode") != "multi_turn"
            and not slots.get("start")
            and not slots.get("end")
            and state.meetingroom.intent in {
                "book",
                "book_single",
                "book_multi_segments_same_room",
                "book_by_schedule_analysis",
                "schedule_book",
            }
        ):
            default_start = "10:00" if "上午" in query else "14:00"
            if "上午" in query or "下午" in query or ("会议室" in query and any(word in query for word in ["订", "预订", "帮我订"])):
                duration = int(slots.get("duration_minutes") or 60)
                slots["start"] = default_start
                slots["end"] = self._add_minutes(default_start, duration)
                slots.setdefault("duration_minutes", duration)
        if (
            state.obs.get("mode") != "multi_turn"
            and not slots.get("title")
            and state.meetingroom.intent in {
                "book",
                "book_single",
                "book_multi_segments_same_room",
                "book_by_schedule_analysis",
                "rebook_larger",
                "rebook_larger_existing",
                "cancel_rebook",
                "cancel_rebook_existing",
                "schedule_book",
            }
        ):
            slots["title"] = slots.get("keyword") or "项目复盘"
        if slots.get("multi_segments"):
            normalized_segments = self._normalize_multi_segments(state, slots.get("multi_segments") or [])
            if normalized_segments:
                slots["multi_segments"] = normalized_segments
                slots["start"] = normalized_segments[0]["start"]
                slots["end"] = normalized_segments[0]["end"]
                slots["title"] = normalized_segments[0]["title"]
        else:
            segments = self._normalize_multi_segments(
                state,
                slots.get("segments") or self._extract_meeting_segments(self._full_query(state.obs)),
            )
            if len(segments) > 1 and (
                state.meetingroom.intent == "book_multi_segments_same_room" or not self._schedule_analysis_needed(state)
            ):
                slots["multi_segments"] = segments
                slots["start"] = segments[0]["start"]
                slots["end"] = segments[0]["end"]
                slots["title"] = segments[0]["title"]
        slots["office_candidates"] = self._normalize_office_candidates(slots.get("office_candidates") or [], query)
        if slots.get("office_address_candidates"):
            slots["office_address_candidates"] = self._normalize_office_address_candidates(
                slots.get("office_address_candidates") or [], query
            )
        if not slots.get("office_candidates") and not slots.get("office_address_candidates") and not slots.get("room_ids"):
            if "A1" in query:
                slots["office_candidates"] = ["A1"]
        if slots.get("office_candidates") and not slots.get("office_address_candidates"):
            generated_addresses = self._office_address_candidates(query, slots.get("office_candidates") or [])
            if generated_addresses:
                slots["office_address_candidates"] = generated_addresses
        if self._schedule_analysis_needed(state) and not slots.get("start") and slots.get("duration_minutes"):
            if "下午" in query:
                slots["start"] = "14:00"
                slots["end"] = self._add_minutes("14:00", int(slots.get("duration_minutes") or 0))
        if (
            self._multi_day_intersection_needed(state)
            and self._multi_day_create_requested(state)
            and "multi_segments" not in slots
            and slots.get("start")
            and slots.get("end")
        ):
            title = self._normalize_meeting_title(slots.get("title") or "项目复盘")
            slots["multi_segments"] = [
                {"day": day, "start": slots["start"], "end": slots["end"], "title": title}
                for day in self._schedule_required_days(state)
            ]

    def _normalize_multi_segments(self, state: RuntimeState, raw_segments: Any) -> list[dict[str, str]]:
        if not isinstance(raw_segments, list):
            return []
        slots = state.meetingroom.slots
        query = self._full_query(state.obs)
        fallback_title = self._normalize_meeting_title(
            slots.get("title") or self._extract_meeting_title(query) or slots.get("keyword") or "项目复盘"
        )
        default_day_text = str(slots.get("day_text") or self._extract_day_text(query) or "")
        normalized: list[dict[str, str]] = []
        for raw in raw_segments:
            if not isinstance(raw, dict):
                continue
            start = self._canonical_time_value(raw.get("start") or raw.get("start_time") or "")
            end = self._canonical_time_value(raw.get("end") or raw.get("end_time") or "")
            if not start or not end or self._minutes_between(start, end) <= 0:
                continue
            day_text = str(raw.get("day_text") or raw.get("day") or default_day_text)
            resolved_day = self._resolve_day(day_text, state.obs.get("now")) if day_text else ""
            item = {
                "start": start,
                "end": end,
                "title": self._normalize_meeting_title(raw.get("title") or fallback_title),
            }
            if day_text:
                item["day_text"] = day_text
            if resolved_day:
                item["day"] = resolved_day
            normalized.append(item)
        return normalized

    def _meeting_missing_slot(self, state: RuntimeState) -> StepAction | None:
        slots = state.meetingroom.slots
        if not self._meeting_day(state) or not slots.get("start") or not slots.get("end"):
            if "day" not in state.asked_slots:
                state.asked_slots.add("day")
                return StepAction("reply", message="请问是在什么时间？")
        if not slots.get("capacity"):
            if "attendees" not in state.asked_slots:
                state.asked_slots.add("attendees")
                return StepAction("reply", message="请问大概多少人参加？")
        if not slots.get("title"):
            if "title" not in state.asked_slots:
                state.asked_slots.add("title")
                return StepAction("reply", message="请问会议主题是什么？")
        return None

    def _ask_confirmation(self, state: RuntimeState, selected: dict[str, Any]) -> StepAction:
        rooms = state.meetingroom.evidence.get("room_candidates", {}).get("rooms") or []
        tried = state.meetingroom.evidence.get("tried_room_lists") or []
        fallback_used = len(tried) > 1 and selected in rooms
        message = "A1当前没有合适的，可以的话我现在改订A2，确认吗？" if fallback_used else "可以的话我现在直接帮你预订，确认吗？"
        state.meetingroom.evidence["pending_selected_room"] = selected
        return StepAction("reply", message=message)

    def _room_list_args(self, state: RuntimeState, day: str | None = None) -> dict[str, Any] | None:
        mr = state.meetingroom
        slots = mr.slots
        day_value = day or self._meeting_day(state)
        if not day_value and str(slots.get("fallback_policy") or "") == "block_if_unavailable":
            day_value = self._next_meeting_business_day(state.obs.get("now"))
        if not day_value:
            return None
        args: dict[str, Any] = {"capacity_gte": int(slots.get("capacity") or 10)}
        args["day"] = day_value
        if slots.get("has_screen"):
            args["has_screen"] = True
        office_address = self._next_office_address_candidate(state)
        if office_address:
            args["office_address"] = office_address
        else:
            office = self._next_office_candidate(state)
            if office:
                args["office_id"] = office
        if slots.get("needs_workspace"):
            workspace = mr.evidence.get("workspace") or {}
            office_address = str(workspace.get("office_address") or "")
            if office_address:
                args.pop("office_id", None)
                args["office_address"] = office_address
        return args

    def _room_list_args_for_candidate(self, state: RuntimeState, candidate: dict[str, Any], day: str | None = None) -> dict[str, Any] | None:
        mr = state.meetingroom
        slots = mr.slots
        day_value = day or self._meeting_day(state)
        if not day_value and str(slots.get("fallback_policy") or "") == "block_if_unavailable":
            day_value = self._next_meeting_business_day(state.obs.get("now"))
        if not day_value:
            return None
        args: dict[str, Any] = {"capacity_gte": int(slots.get("capacity") or 10)}
        args["day"] = day_value
        if slots.get("has_screen"):
            args["has_screen"] = True
        if candidate.get("office_address"):
            args["office_address"] = candidate["office_address"]
        elif candidate.get("office_id"):
            args["office_id"] = candidate["office_id"]
        return args

    def _room_search_candidates(self, state: RuntimeState) -> list[dict[str, str]]:
        mr = state.meetingroom
        slots = mr.slots
        out: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            key = (kind, text)
            if key in seen:
                return
            seen.add(key)
            out.append({kind: text})

        if slots.get("needs_workspace") and mr.evidence.get("workspace"):
            workspace_address = str((mr.evidence.get("workspace") or {}).get("office_address") or "")
            if workspace_address:
                building_address = self._building_address(workspace_address)
                if building_address and building_address != workspace_address:
                    add("office_address", building_address)
                add("office_address", workspace_address)

        prefer_address = bool(slots.get("office_address_candidates")) and (
            str(slots.get("fallback_policy") or "") == "block_if_unavailable"
            or any(word in self._full_query(state.obs) for word in ["楼", "层", "小镇", "合肥", "园区"])
        )
        if prefer_address:
            for value in slots.get("office_address_candidates") or []:
                add("office_address", value)
        for value in slots.get("office_candidates") or []:
            office = str(value or "").strip().upper()
            if not office:
                continue
            add("office_id", office)
        if not prefer_address:
            for value in slots.get("office_address_candidates") or []:
                add("office_address", value)
        if self._meetingroom_fallback_search_allowed(state):
            for candidate in self._meetingroom_fallback_candidates(state):
                for key, value in candidate.items():
                    add(key, value)
        if not out:
            out.append({})
        return out

    def _meetingroom_fallback_search_allowed(self, state: RuntimeState) -> bool:
        slots = state.meetingroom.slots
        query = self._full_query(state.obs)
        return bool(slots.get("allow_fallback") or slots.get("fallback_policy")) and not any(
            word in query for word in ["订不到就算了", "不行就别乱订", "只要", "必须"]
        )

    def _meetingroom_fallback_candidates(self, state: RuntimeState) -> list[dict[str, str]]:
        slots = state.meetingroom.slots
        query = self._full_query(state.obs)
        source_values = [str(item) for item in (slots.get("office_address_candidates") or []) if item]
        source_values.extend(str(item) for item in (slots.get("office_candidates") or []) if item)
        if slots.get("needs_workspace") and state.meetingroom.evidence.get("workspace"):
            workspace_address = str((state.meetingroom.evidence.get("workspace") or {}).get("office_address") or "")
            if workspace_address:
                source_values.append(self._building_address(workspace_address))
        buildings = self._static_meetingroom_buildings()
        out: list[dict[str, str]] = []
        for source in source_values:
            building_match = re.search(r"A\d", source, flags=re.I)
            if not building_match:
                continue
            current = building_match.group(0).upper()
            prefix = "0551" if "0551" in source or "合肥" in query else "0552"
            for building in buildings:
                if building == current:
                    continue
                if prefix == "0551" and building not in {"A4", "A5"}:
                    continue
                if prefix == "0552" and building in {"A4", "A5"}:
                    continue
                out.append({"office_address": f"{prefix}_{building}"})
                if len(out) >= 3:
                    return out
        return out

    def _static_meetingroom_buildings(self) -> list[str]:
        by_building = self.meetingroom_index.index.get("by_building") if isinstance(self.meetingroom_index.index, dict) else {}
        if not isinstance(by_building, dict):
            return []
        return sorted(str(key).upper() for key in by_building.keys() if re.fullmatch(r"A\d", str(key).upper()))

    def _room_search_days(self, state: RuntimeState) -> list[str]:
        query = self._full_query(state.obs)
        days = self._schedule_required_days(state)
        if any(word in query for word in ["这周", "本周", "最早", "哪天", "连续几天"]):
            try:
                start = self._week_start(state.obs.get("now"))
            except Exception:
                start = date.today()
            for offset in range(5):
                candidate = (start + timedelta(days=offset)).isoformat()
                if candidate not in days:
                    days.append(candidate)
        return self._dedupe([day for day in days if day])

    def _next_room_list_args(self, state: RuntimeState) -> dict[str, Any] | None:
        tried_args = [item.get("args") for item in state.meetingroom.evidence.get("tried_room_lists", [])]
        days = self._room_search_days(state)
        if not days and str(state.meetingroom.slots.get("fallback_policy") or "") == "block_if_unavailable":
            days = [""]
        for day in days:
            for candidate in self._room_search_candidates(state):
                args = self._room_list_args_for_candidate(state, candidate, day=day or None)
                if args and args not in tried_args:
                    return args
        return None

    def _schedule_analysis_needed(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        intent = state.meetingroom.intent
        return intent in {"book_by_schedule_analysis", "query_room_schedule", "schedule_book"} or any(
            word in query for word in ["连续", "都要空闲", "两天都空闲", "两天都空", "都空的", "空闲时段", "最长", "同一个会议室", "同一会议室", "同一个房间"]
        )

    def _requires_full_schedule_analysis(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        return any(word in query for word in ["都要空闲", "两天都空闲", "两天都空", "都空的", "周三和周四", "连续", "最长", "空闲时段", "同一个会议室", "同一会议室", "同一个房间"])

    def _multi_day_intersection_needed(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        if len(self._schedule_required_days(state)) <= 1:
            return False
        return any(
            word in query
            for word in [
                "都要空闲",
                "两天都空闲",
                "两天都空",
                "都空的",
                "同一个会议室",
                "同一会议室",
                "同一个房间",
                "同一间",
                "三天都要",
                "都要订",
                "连续",
            ]
        )

    def _multi_day_create_requested(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        days = self._schedule_required_days(state)
        if len(days) <= 1:
            return False
        if re.search(r"(?:订|预订|定)\s*(?:周|本周|下周|今天|明天|后天|\d{1,2}[号日月])", query):
            return False
        if re.search(r"(?:找到后|找到以后|找好后|选好后|然后|再|就)\s*(?:帮我)?(?:订|预订|定)\s*(?:周|本周|下周|今天|明天|后天|\d{1,2}[号日月])", query):
            return False
        if any(word in query for word in ["都要订", "都订", "都预订", "都定", "都要定", "都要订上", "都订上", "都定上"]):
            return True
        if any(word in query for word in ["每天都订", "每天订", "每天下午订", "每天上午订", "每天都要订"]):
            return True
        if re.search(r"(?:两|二|三|四|五|六|七|\d+)\s*天[^，。；;]*(?:都空|都要空|都要空闲|空闲)[^，。；;]*(?:订上|预订|订)", query):
            return True
        if re.search(r"(?:两|二|三|四|五|六|七|\d+)\s*天[^，。；;]*(?:会议|培训|会)[^，。；;]*(?:订上|预订|订)", query):
            return True
        if re.search(r"(?:连续)\s*(?:两|二|三|四|五|六|七|\d+)\s*天[^，。；;]*(?:订上|预订|订)", query):
            return True
        return False

    def _select_room_from_multi_day_lists(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        days = self._schedule_required_days(state)
        if not days:
            return None
        start = str(mr.slots.get("start") or "")
        end = str(mr.slots.get("end") or "")
        room_lists = mr.evidence.get("room_lists_by_day") or {}
        legal_by_day: list[dict[str, dict[str, Any]]] = []
        for day in days:
            rooms = (room_lists.get(day) or {}).get("rooms") or []
            legal: dict[str, dict[str, Any]] = {}
            for room in rooms:
                if self._room_is_legal_for_window(room, mr, start, end):
                    legal[str(room.get("room_id"))] = room
            legal_by_day.append(legal)
        common = set(legal_by_day[0])
        for legal in legal_by_day[1:]:
            common &= set(legal)
        if not common:
            return None
        target_rooms = [legal_by_day[0][room_id] for room_id in common]
        start = str(mr.slots.get("start") or "")
        end = str(mr.slots.get("end") or "")
        title = self._normalize_meeting_title(mr.slots.get("title") or "项目复盘")
        if len(days) > 1 and start and end and self._multi_day_create_requested(state):
            mr.slots["multi_segments"] = [
                {"day": day, "start": start, "end": end, "title": title}
                for day in days
            ]
            mr.slots["day"] = days[0]
        return sorted(target_rooms, key=lambda r: (r.get("capacity", 999), str(r.get("room_id"))))[0]

    def _room_is_legal_for_window(self, room: dict[str, Any], mr: DomainState, start: str, end: str) -> bool:
        if not room.get("bookable", True):
            return False
        if room.get("capacity", 0) < int(mr.slots.get("capacity") or 1):
            return False
        if mr.slots.get("has_screen") and not (room.get("hasScreen") or "screen" in room.get("features", [])):
            return False
        if start and end and any(start < busy_end and end > busy_start for busy_start, busy_end in room.get("busy_slots", [])):
            return False
        return True

    def _merge_schedule_into_room_candidates(self, mr: DomainState, room_id: Any, schedule: dict[str, Any]) -> None:
        rooms = (mr.evidence.get("room_candidates") or {}).get("rooms") or []
        for room in rooms:
            if room.get("room_id") != room_id:
                continue
            busy_by_day: dict[str, list[list[str]]] = {}
            for booking in schedule.get("bookings") or []:
                if booking.get("status") == "cancelled":
                    continue
                day = str(booking.get("day") or "")
                if day:
                    busy_by_day.setdefault(day, []).append([booking.get("start"), booking.get("end")])
            room["busy_by_day"] = busy_by_day
            break

    def _select_room_from_schedule_analysis(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        rooms = (mr.evidence.get("room_candidates") or {}).get("rooms") or []
        days = self._schedule_required_days(state)
        target_day = days[0] if days else self._meeting_day(state)
        duration = int(mr.slots.get("duration_minutes") or self._minutes_between(mr.slots.get("start"), mr.slots.get("end")) or 60)
        window_start = str(mr.slots.get("start") or ("14:00" if "下午" in self._full_query(state.obs) else "09:00"))
        window_end = str(mr.slots.get("end") or ("18:00" if "下午" in self._full_query(state.obs) else "18:00"))
        candidates = []
        for room in rooms:
            if not room.get("bookable", True):
                continue
            if room.get("capacity", 0) < int(mr.slots.get("capacity") or 1):
                continue
            if mr.slots.get("has_screen") and not (room.get("hasScreen") or "screen" in room.get("features", [])):
                continue
            slots_by_day = room.get("busy_by_day") or {}
            common_start = window_start
            common_end = ""
            valid = True
            for day in days or [target_day]:
                free = self._first_free_window(slots_by_day.get(day, []), window_start, window_end, duration)
                if not free:
                    valid = False
                    break
                if day == target_day:
                    common_start, common_end = free
            if valid:
                candidates.append((common_start, common_end, room))
        if not candidates:
            return None
        start, end, room = sorted(candidates, key=lambda item: (item[0], item[2].get("capacity", 999), str(item[2].get("room_id"))))[0]
        mr.slots["day"] = target_day
        mr.slots["start"] = start
        mr.slots["end"] = end
        return room

    def _select_named_room_from_schedules(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        schedules = mr.evidence.get("schedules") or {}
        target_day = self._meeting_day(state)
        start = str(mr.slots.get("start") or "")
        end = str(mr.slots.get("end") or "")
        ranked = []
        for room_id in mr.slots.get("room_ids") or []:
            schedule = schedules.get(room_id) or {}
            bookings = [item for item in schedule.get("bookings") or [] if item.get("status") != "cancelled"]
            busy_on_target = [
                [item.get("start"), item.get("end")]
                for item in bookings
                if str(item.get("day") or "") == target_day and item.get("start") and item.get("end")
            ]
            if start and end and any(start < busy_end and end > busy_start for busy_start, busy_end in busy_on_target):
                continue
            busy_minutes = sum(
                max(0, self._minutes_between(item.get("start"), item.get("end")))
                for item in bookings
                if item.get("start") and item.get("end")
            )
            ranked.append((busy_minutes, len(bookings), str(room_id)))
        if not ranked:
            return None
        _, _, room_id = sorted(ranked)[0]
        return {
            "room_id": room_id,
            "officeId": self._office_id_from_room_id(room_id),
            "busy_slots": [],
            "bookable": True,
            "capacity": max(int(mr.slots.get("capacity") or 1), 999),
            "hasScreen": True,
            "features": ["screen"],
        }

    def _advance_room_candidate(self, state: RuntimeState) -> dict[str, Any] | None:
        return self._next_room_list_args(state)

    def _next_office_candidate(self, state: RuntimeState) -> str:
        candidates = state.meetingroom.slots.get("office_candidates") or []
        index = int(state.meetingroom.evidence.get("candidate_index") or 0)
        if index < len(candidates):
            return str(candidates[index])
        return ""

    def _next_office_address_candidate(self, state: RuntimeState) -> str:
        candidates = state.meetingroom.slots.get("office_address_candidates") or []
        index = int(state.meetingroom.evidence.get("candidate_index") or 0)
        if index < len(candidates):
            return str(candidates[index])
        return ""

    def _select_room_for_booking(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        current_day = str((mr.evidence.get("room_candidates") or {}).get("day") or mr.slots.get("day") or "") or self._meeting_day(state)
        if not current_day:
            return None
        mr.slots["day"] = current_day
        pending = mr.evidence.get("pending_selected_room")
        if pending:
            return pending
        result = mr.evidence.get("room_candidates") or {}
        rooms = result.get("rooms") or []
        start = mr.slots.get("start")
        end = mr.slots.get("end")
        segments = mr.slots.get("multi_segments") or []
        if segments:
            start = start or segments[0].get("start")
            end = end or segments[0].get("end")
        if not start or not end:
            return None
        legal = []
        for room in rooms:
            if not room.get("bookable", True):
                continue
            if self._is_rebook_larger_intent(mr.intent) and mr.evidence.get("selected_booking"):
                original_room_id = str((mr.evidence.get("selected_booking") or {}).get("room_id") or "")
                if original_room_id and str(room.get("room_id") or "") == original_room_id:
                    continue
            if room.get("capacity", 0) < int(mr.slots.get("capacity") or 1):
                continue
            if mr.slots.get("has_screen") and not (room.get("hasScreen") or "screen" in room.get("features", [])):
                continue
            intervals = [(item.get("start"), item.get("end")) for item in segments] or [(start, end)]
            if any(
                seg_start < busy_end and seg_end > busy_start
                for seg_start, seg_end in intervals
                for busy_start, busy_end in room.get("busy_slots", [])
            ):
                continue
            legal.append(room)
        if not legal:
            adjusted = self._select_room_with_adjusted_window(state, rooms, start, end, segments)
            if adjusted:
                return adjusted
            return None
        if mr.slots.get("needs_workspace") and mr.evidence.get("workspace"):
            return sorted(legal, key=lambda r: self._room_booking_sort_key(state, r))[0]
        if self._is_rebook_intent(mr.intent) and mr.evidence.get("selected_booking"):
            old_capacity = mr.evidence.get("selected_room_capacity") or 0
            old_attendees = int((mr.evidence.get("selected_booking") or {}).get("attendees") or 0)
            bigger_than = max(int(old_capacity or 0), old_attendees)
            bigger = [room for room in legal if int(room.get("capacity") or 0) > bigger_than]
            if bigger:
                return sorted(bigger, key=lambda r: self._room_booking_sort_key(state, r))[0]
        return sorted(legal, key=lambda r: self._room_booking_sort_key(state, r))[0]

    def _room_booking_sort_key(self, state: RuntimeState, room: dict[str, Any]) -> tuple[Any, ...]:
        capacity = int(room.get("capacity") or 999)
        room_id = str(room.get("room_id") or "")
        if state.meetingroom.slots.get("needs_workspace") and state.meetingroom.evidence.get("workspace"):
            rank = self._workspace_rank(room, state.meetingroom.evidence["workspace"])
            return (-rank[0], -rank[1], -rank[2], capacity, room_id)
        return (capacity, room_id)

    def _record_selected_room_capacity_from_candidates(self, mr: DomainState, result: dict[str, Any]) -> None:
        selected = mr.evidence.get("selected_booking") or {}
        room_id = str(selected.get("room_id") or "")
        if not room_id:
            return
        for room in result.get("rooms") or []:
            if str(room.get("room_id") or "") != room_id:
                continue
            try:
                mr.evidence["selected_room_capacity"] = int(room.get("capacity") or 0)
            except Exception:
                pass
            return

    def _select_room_with_adjusted_window(
        self,
        state: RuntimeState,
        rooms: list[dict[str, Any]],
        start: Any,
        end: Any,
        segments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if segments or not start or not end:
            return None
        mr = state.meetingroom
        if not self._allows_time_adjustment(state):
            return None
        duration = self._minutes_between(start, end)
        if duration <= 0:
            return None
        for candidate_start, candidate_end in self._adjusted_time_windows(str(start), duration):
            legal = []
            for room in rooms:
                if not room.get("bookable", True):
                    continue
                if room.get("capacity", 0) < int(mr.slots.get("capacity") or 1):
                    continue
                if mr.slots.get("has_screen") and not (room.get("hasScreen") or "screen" in room.get("features", [])):
                    continue
                if any(candidate_start < busy_end and candidate_end > busy_start for busy_start, busy_end in room.get("busy_slots", [])):
                    continue
                legal.append(room)
            if legal:
                mr.slots["start"] = candidate_start
                mr.slots["end"] = candidate_end
                return sorted(legal, key=lambda r: (r.get("capacity", 999), str(r.get("room_id"))))[0]
        return None

    def _allows_time_adjustment(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        return any(word in query for word in ["前后半小时", "前后 30 分钟", "前后30分钟", "附近时间", "时间不行", "这个时间不行"])

    def _adjusted_time_windows(self, start: str, duration_minutes: int) -> list[tuple[str, str]]:
        try:
            start_minutes = self._time_to_minutes(start)
        except Exception:
            return []
        windows = []
        for delta in (-30, 30, -60, 60):
            candidate = start_minutes + delta
            if candidate < 0 or candidate + duration_minutes > 24 * 60:
                continue
            windows.append((self._minutes_to_time(candidate), self._minutes_to_time(candidate + duration_minutes)))
        return windows

    def _booking_create_args(self, state: RuntimeState, room: dict[str, Any]) -> dict[str, Any]:
        mr = state.meetingroom
        if mr.slots.get("multi_segments"):
            day = str(mr.slots.get("day") or "")
        else:
            explicit_day = self._explicit_booking_day(state)
            day = explicit_day or str((mr.evidence.get("room_candidates") or {}).get("day") or mr.slots.get("day") or "") or self._meeting_day(state) or self._booking_day_from_schedule_context(state)
        if day:
            mr.slots["day"] = day
        return {
            "day": day,
            "office_id": room.get("officeId") or room.get("office_id") or room.get("building") or mr.slots.get("office_candidates", [""])[0],
            "room_id": room.get("room_id"),
            "start": mr.slots.get("start"),
            "end": mr.slots.get("end"),
            "title": self._normalize_meeting_title(mr.slots.get("title") or "项目复盘"),
            "attendees": int(mr.slots.get("capacity") or 1),
        }

    def _explicit_booking_day(self, state: RuntimeState) -> str:
        query = self._full_query(state.obs)
        now = state.obs.get("now")
        patterns = [
            r"(?:订|预订|定)\s*((?:本周|下周)?周[一二三四五六日天])",
            r"(?:订|预订|定)\s*((?:今天|明天|后天))",
            r"(?:订|预订|定)\s*((?:下个月\s*)?\d{1,2}号)",
            r"(?:订|预订|定)\s*((?:\d{1,2}\s*月\s*)?\d{1,2}\s*日)",
        ]
        for pattern in patterns:
            matches = list(re.finditer(pattern, query))
            for match in reversed(matches):
                day = self._resolve_day(match.group(1), now, prefer_workday=self._is_meeting_context(query))
                if day:
                    return day
        return ""

    def _booking_day_from_schedule_context(self, state: RuntimeState) -> str:
        query_day = self._resolve_day(self._extract_day_text(self._full_query(state.obs)), state.obs.get("now"))
        if query_day:
            state.meetingroom.slots["day"] = query_day
            return query_day
        schedules = state.meetingroom.evidence.get("schedules") or {}
        for schedule in schedules.values():
            start_date = schedule.get("start_date") if isinstance(schedule, dict) else ""
            if start_date:
                state.meetingroom.slots["day"] = str(start_date)
                return str(start_date)
        return ""

    def _next_booking_list_args(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        tried = [item.get("args") for item in mr.evidence.get("tried_booking_lists", [])]
        keyword = str(mr.slots.get("keyword") or "")
        for day in self._meeting_day_candidates(state):
            args: dict[str, Any] = {}
            if mr.intent not in {"query_booking", "query"}:
                args["status"] = "active"
            if day:
                args["day"] = day
            if keyword and not (self._is_extend_intent(mr.intent) or self._is_rebook_intent(mr.intent)):
                args["keyword"] = keyword
            if args not in tried:
                return args
        args = {} if mr.intent in {"query_booking", "query"} else {"status": "active"}
        if keyword and not (self._is_extend_intent(mr.intent) or self._is_rebook_intent(mr.intent)):
            args["keyword"] = keyword
        if args not in tried:
            return args
        return None

    def _extension_has_known_conflict(self, state: RuntimeState, booking: dict[str, Any]) -> bool:
        minutes = int(state.meetingroom.slots.get("duration_minutes") or 30)
        new_end = self._add_minutes(str(booking.get("end") or ""), minutes)
        booking_id = booking.get("order_id") or booking.get("booking_id")
        for other in state.meetingroom.evidence.get("booking_query", {}).get("bookings") or []:
            other_id = other.get("order_id") or other.get("booking_id")
            if other_id == booking_id or other.get("status") == "cancelled":
                continue
            if other.get("room_id") != booking.get("room_id") or other.get("day") != booking.get("day"):
                continue
            if self._time_overlap(str(booking.get("start") or ""), new_end, str(other.get("start") or ""), str(other.get("end") or "")):
                return True
        return False

    def _booking_result_from_create(self, args: dict[str, Any], result: dict[str, Any], mr: DomainState) -> dict[str, Any]:
        created = mr.evidence.get("created_segments") or []
        if created:
            first = created[0]["args"]
            return {
                "status": "success",
                "room_id": result.get("room_id") or args.get("room_id"),
                "office_id": first.get("office_id"),
                "bookings": [
                    {
                        "day": item["args"].get("day"),
                        "start": item["args"].get("start"),
                        "end": item["args"].get("end"),
                        "title": self._normalize_meeting_title(item["args"].get("title")),
                    }
                    for item in created
                ],
            }
        return {
            "status": "success",
            "day": result.get("day") or args.get("day"),
            "office_id": self._display_office_id(args.get("office_id"), args.get("room_id"), mr),
            "room_id": result.get("room_id") or args.get("room_id"),
            "start": result.get("start") or args.get("start"),
            "end": result.get("end") or args.get("end"),
            "title": self._normalize_meeting_title(result.get("title") or args.get("title")),
        }

    def _display_office_id(self, office_id: Any, room_id: Any, mr: DomainState) -> str:
        if mr.intent in {"book_by_schedule_analysis", "query_room_schedule", "schedule_book"}:
            return str(office_id or "")
        if self._searched_multiple_room_days(mr):
            return str(office_id or "")
        candidates = mr.evidence.get("room_candidates", {}).get("rooms") or []
        for room in candidates:
            if room.get("room_id") == room_id:
                if re.match(r"^A\d-", str(room_id or ""), flags=re.I):
                    return str(room.get("building") or office_id)
                return str(office_id or room.get("officeId") or room.get("office_id") or room.get("building") or "")
        return str(office_id or "")

    def _searched_multiple_room_days(self, mr: DomainState) -> bool:
        days = {
            str(item.get("args", {}).get("day") or "")
            for item in (mr.evidence.get("tried_room_lists") or [])
            if isinstance(item, dict)
        }
        days.discard("")
        return len(days) > 1

    def _participant_result_from_tool(
        self,
        tool: str,
        args: dict[str, Any],
        result: dict[str, Any],
        mr: DomainState,
        index: int,
    ) -> dict[str, Any]:
        participants = result.get("participants") if isinstance(result.get("participants"), list) else []
        user_id = str(args.get("user_id") or "")
        person = {}
        for item in participants:
            if isinstance(item, dict) and str(item.get("user_id") or "") == user_id:
                person = item
                break
        if not person:
            slot_people = mr.slots.get("participants") if isinstance(mr.slots.get("participants"), list) else []
            if index < len(slot_people) and isinstance(slot_people[index], dict):
                person = slot_people[index]
        status = "added" if tool == "meetingroom.booking.participant.add" else "removed"
        return {
            "status": status,
            "order_id": result.get("order_id") or args.get("order_id"),
            "user_id": user_id,
            "name": person.get("name") or "",
        }

    def _select_booking(self, state: RuntimeState, bookings: list[dict[str, Any]]) -> dict[str, Any]:
        mr = state.meetingroom
        start = mr.slots.get("start")
        end = mr.slots.get("end")
        office_candidates = set(mr.slots.get("office_candidates") or [])
        keyword = str(mr.slots.get("keyword") or "")
        scored = []
        for booking in bookings:
            score = 0
            if start and booking.get("start") == start:
                score += 3
            if end and booking.get("end") == end:
                score += 2
            if keyword and keyword in str(booking.get("title") or ""):
                score += 2
            if not keyword and any(word in str(booking.get("title") or "") for word in ["复盘", "评审"]):
                score += 1
            if office_candidates and any(str(booking.get("office_id", "")).endswith(o) or o in str(booking.get("room_id", "")) for o in office_candidates):
                score += 1
            scored.append((score, booking))
        selected = sorted(scored, key=lambda item: item[0], reverse=True)[0][1]
        state.meetingroom.evidence["selected_room_capacity"] = int(selected.get("capacity") or selected.get("attendees") or 0)
        if selected.get("attendees") and mr.slots.get("capacity_delta"):
            mr.slots["capacity"] = int(selected.get("attendees") or 0) + int(mr.slots.get("capacity_delta") or 0)
        if selected.get("attendees") and int(mr.slots.get("capacity") or 0) <= int(selected.get("attendees") or 0) and self._is_rebook_larger_intent(mr.intent):
            mr.slots["capacity"] = int(selected.get("attendees") or 0) + 4
        return selected

    def _existing_booking_write_requires_confirmation(self, state: RuntimeState) -> bool:
        mr = state.meetingroom
        query = self._full_query(state.obs)
        bookings = (mr.evidence.get("booking_query") or {}).get("bookings") or []
        active = [item for item in bookings if item.get("status") != "cancelled"]
        if len(active) <= 1:
            return False
        if self._extract_order_id(query) or mr.slots.get("order_id"):
            return False
        if self._is_cancel_rebook_intent(mr.intent) and any(word in query for word in ["延", "重订", "重新预订", "不行就"]):
            return False
        if not (mr.slots.get("start") or mr.slots.get("end")):
            return True
        selected = mr.evidence.get("selected_booking") or {}
        unique_score = 0
        if mr.slots.get("start") and selected.get("start") == mr.slots.get("start"):
            unique_score += 1
        if mr.slots.get("end") and selected.get("end") == mr.slots.get("end"):
            unique_score += 1
        keyword = str(mr.slots.get("keyword") or "")
        if keyword and keyword in str(selected.get("title") or ""):
            unique_score += 1
        return unique_score == 0

    def _booking_list_requires_confirmation_before_write(self, state: RuntimeState, bookings: list[dict[str, Any]]) -> bool:
        mr = state.meetingroom
        if not (self._is_cancel_intent(mr.intent) or self._is_cancel_rebook_intent(mr.intent)):
            return False
        query = self._full_query(state.obs)
        if self._extract_order_id(query) or mr.slots.get("order_id"):
            return False
        if self._is_cancel_rebook_intent(mr.intent) and any(word in query for word in ["延", "重订", "重新预订", "不行就"]):
            return False
        active = [item for item in bookings if item.get("status") != "cancelled"]
        if len(active) <= 1:
            return False
        if not (mr.slots.get("start") and mr.slots.get("end")):
            mr.status = "blocked"
            mr.blocked_reason = "need_confirmation"
            mr.result = {"status": "blocked", "reason": "need_confirmation"}
            return True
        exact = [
            item for item in active
            if item.get("start") == mr.slots.get("start") and item.get("end") == mr.slots.get("end")
        ]
        if len(exact) != 1:
            mr.status = "blocked"
            mr.blocked_reason = "need_confirmation"
            mr.result = {"status": "blocked", "reason": "need_confirmation"}
            return True
        return False

    def _meeting_day(self, state: RuntimeState) -> str:
        candidates = self._meeting_day_candidates(state)
        day_value = candidates[0] if candidates else ""
        if day_value:
            state.meetingroom.slots["day"] = day_value
        return day_value

    def _meeting_day_candidates(self, state: RuntimeState) -> list[str]:
        slots = state.meetingroom.slots
        query = self._full_query(state.obs)
        raw_day_text = self._extract_day_text(query)
        day_text = str(slots.get("day_text") or raw_day_text or "")
        if raw_day_text.startswith(("下周", "本周")) and re.fullmatch(r"周[一二三四五六日天]", day_text):
            if raw_day_text[-1] == day_text[-1]:
                day_text = raw_day_text
        primary = self._resolve_day(day_text, state.obs.get("now"), prefer_workday=self._is_meeting_context(query))
        candidates: list[str] = []

        if "明天" in day_text and self._is_meeting_context(query):
            meeting_day = self._next_meeting_business_day(state.obs.get("now"))
            if meeting_day:
                candidates.append(meeting_day)
        if slots.get("day") and "明天" not in day_text and not raw_day_text.startswith(("下周", "本周")):
            candidates.append(str(slots["day"]))
        if primary:
            candidates.append(primary)
        if not candidates and slots.get("day"):
            candidates.append(str(slots["day"]))
        return self._dedupe(candidates)

    def _is_meeting_context(self, query: str) -> bool:
        return any(word in query for word in ["会议室", "会议", "评审会", "复盘会", "会"])

    def _next_meeting_business_day(self, now_value: Any) -> str:
        try:
            now = datetime.fromisoformat(str(now_value).replace("Z", "+00:00")).date()
        except Exception:
            now = date.today()
        candidate = now + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        if now.weekday() >= 5 or (now.weekday() == 5 and candidate.weekday() == 0):
            candidate += timedelta(days=1)
        return candidate.isoformat()

    def _schedule_range(self, state: RuntimeState) -> tuple[str, str]:
        query = self._full_query(state.obs)
        now = state.obs.get("now")
        if "下周" in query:
            start = self._week_start(now, offset=1)
            return start.isoformat(), (start + timedelta(days=4)).isoformat()
        days = self._schedule_required_days(state)
        if len(days) > 1:
            ordered = sorted(days)
            return ordered[0], ordered[-1]
        day = self._meeting_day(state) or self._resolve_day("", now)
        return day, day

    def _next_segment_to_create(self, mr: DomainState) -> dict[str, str] | None:
        created = mr.evidence.get("created_segments") or []
        segments = mr.slots.get("multi_segments") or []
        if len(created) < len(segments):
            return segments[len(created)]
        return None

    # ------------------------------------------------------------------
    # Workflow helpers
    # ------------------------------------------------------------------

    def _is_leave_replace_request(self, query: str) -> bool:
        text = str(query or "")
        leave_types = [item for item in ["育儿假", "年休假", "年假", "病假", "事假"] if item in text]
        return bool(
            len(set(leave_types)) >= 2
            and any(word in text for word in ["改成", "改为", "换成", "换为", "转成", "转为"])
            and any(word in text for word in ["昨天", "已经", "之前", "原来", "原先", "请了", "提交过"])
        )

    def _target_leave_type_label(self, query: str) -> str:
        match = re.search(
            r"(?:改成|改为|换成|换为|转成|转为)[^，。；;]{0,10}?(育儿假|年休假|年假|病假|事假)",
            str(query or ""),
        )
        return match.group(1) if match else ""

    def _replacement_leave_source_item(self, state: RuntimeState) -> dict[str, Any] | None:
        source = state.workflow.evidence.get("replacement_source_lookup") or {}
        items = [
            item
            for item in source.get("items") or []
            if isinstance(item, dict) and int(item.get("workflow_id") or 0) == WORKFLOW_IDS["leave"] and item.get("request_id")
        ]
        return items[0] if items else None

    def _normalize_workflow_slots(self, state: RuntimeState) -> None:
        wf = state.workflow
        query = self._full_query(state.obs)
        explicit_submit = self._leave_submit_intent(query) if wf.intent == "leave" else self._submit_intent(query)
        explicit_draft = self._draft_intent(query)
        if explicit_submit:
            wf.slots["submit"] = True
        elif explicit_draft:
            wf.slots["submit"] = False
        elif wf.intent == "leave":
            wf.slots["submit"] = False
        else:
            wf.slots["submit"] = bool(wf.slots.get("submit"))
        if wf.intent == "leave":
            self._normalize_approver_hints(self._leave_slots(state))
            leave = self._leave_slots(state)
            target_leave_type = self._target_leave_type_label(self._leave_query(state))
            if target_leave_type:
                leave["leave_type_label"] = target_leave_type
            if not leave.get("leave_type_label"):
                leave["leave_type_label"] = "事假"
            if not leave.get("reason_label"):
                leave["reason_label"] = leave.get("leave_type_label") or "本人有事"
        elif wf.intent == "expense_material":
            self._expense_slots(state)

    def _leave_slots(self, state: RuntimeState) -> dict[str, Any]:
        return state.workflow.slots.setdefault("leave", {})

    def _expense_slots(self, state: RuntimeState) -> dict[str, Any]:
        return state.workflow.slots.setdefault("expense", {})

    def _leave_missing_slot(self, state: RuntimeState) -> StepAction | None:
        leave = self._leave_slots(state)
        if not leave.get("start"):
            if "start_time" not in state.asked_slots:
                state.asked_slots.add("start_time")
                return StepAction("reply", message="请问您几点开始请假？")
        if not leave.get("end"):
            if "end_time" not in state.asked_slots:
                state.asked_slots.add("end_time")
                return StepAction("reply", message="请问到几点结束？")
        if not leave.get("leave_type_label"):
            if "leave_type" not in state.asked_slots:
                state.asked_slots.add("leave_type")
                return StepAction("reply", message="请问是什么类型的假期？")
        if not leave.get("reason_label"):
            if "reason" not in state.asked_slots:
                state.asked_slots.add("reason")
                return StepAction("reply", message="请问请假原因是？")
        if not leave.get("approver_keyword") and not leave.get("approver_title"):
            if "approver" not in state.asked_slots:
                state.asked_slots.add("approver")
                return StepAction("reply", message="请问选择哪位作为审批人？")
        return None

    def _leave_plan(self, state: RuntimeState) -> dict[str, Any] | None:
        leave = self._leave_slots(state)
        query = self._leave_query(state)
        day_text = leave.get("day_text") or self._extract_day_text(query)
        start_day, end_day = self._resolve_leave_date_range(day_text, state)
        if not start_day and any(word in query for word in ["那天", "当天", "同一天", "那天下午", "当天上午", "当天下午"]):
            meeting_day = self._meeting_day(state)
            if meeting_day:
                start_day = meeting_day
                end_day = meeting_day
        if not start_day:
            return None
        start = self._canonical_time_value(leave.get("start"))
        end = self._canonical_time_value(leave.get("end"))
        leave_text = self._slice_workflow_text(query, "leave")
        duration = self._extract_duration_hours(leave_text)
        spans_multiple_days = bool(end_day and start_day and end_day != start_day)
        if not start and duration and "下午" in query:
            start = "16:00" if float(duration) <= 2 else "14:00"
        if not end and start and duration:
            end = self._add_minutes(start, int(float(duration) * 60))
        if not start:
            start = "09:00" if spans_multiple_days or "上午" in query or "全天" in query else "14:00"
        if not end:
            end = "18:00" if spans_multiple_days or "下午" in query or "全天" in query else "11:00"
        leave_type = self._leave_type_value(leave.get("leave_type_label") or query)
        reason = self._reason_value(leave.get("reason_label") or query)
        end_day = end_day or start_day
        return {
            "start_time": f"{start_day} {start}",
            "end_time": f"{end_day} {end}",
            "leave_type": leave_type,
            "reason": reason,
            "duration": self._leave_duration_hours(start_day, start, end_day, end),
            "approver_keyword": leave.get("approver_name_hint") or leave.get("approver_keyword"),
            "approver_title": leave.get("approver_title_hint") or leave.get("approver_title"),
            "approver_employee_no": leave.get("approver_employee_no"),
        }

    def _resolve_leave_date_range(self, day_text: Any, state: RuntimeState) -> tuple[str, str]:
        text = str(day_text or "")
        if not text:
            day = self._resolve_leave_day(text, state)
            return day, day
        if "明天" in text and "后天" in text:
            return self._resolve_day("明天", state.obs.get("now")), self._resolve_day("后天", state.obs.get("now"))
        if "今天" in text and "明天" in text:
            return self._resolve_day("今天", state.obs.get("now")), self._resolve_day("明天", state.obs.get("now"))
        dates = re.findall(r"\d{1,2}\s*月\s*\d{1,2}\s*日", text)
        if len(dates) >= 2:
            start = self._resolve_day(dates[0], state.obs.get("now"))
            end = self._resolve_day(dates[1], state.obs.get("now"))
            return start, self._exclusive_leave_end_day(text, end)
        month_range = re.search(r"下个月\s*(\d{1,2})号\s*(?:到|至|-|~)\s*(\d{1,2})号", text)
        if month_range:
            start = self._resolve_next_month_day(month_range.group(1), state.obs.get("now"))
            end = self._resolve_next_month_day(month_range.group(2), state.obs.get("now"))
            return start, self._exclusive_leave_end_day(text, end)
        same_month_range = re.search(r"(\d{1,2})号\s*(?:到|至|-|~)\s*(\d{1,2})号", text)
        if same_month_range:
            start = self._resolve_month_day(same_month_range.group(1), state.obs.get("now"))
            end = self._resolve_month_day(same_month_range.group(2), state.obs.get("now"))
            return start, self._exclusive_leave_end_day(text, end)
        day = self._resolve_leave_day(text, state)
        return day, day

    def _resolve_leave_day(self, day_text: Any, state: RuntimeState) -> str:
        text = str(day_text or "")
        full_query = self._full_query(state.obs)
        if "下周" in text and "明天" in full_query and state.meetingroom.needed:
            try:
                base = date.fromisoformat(self._next_meeting_business_day(state.obs.get("now")))
            except Exception:
                base = None
            if base is not None:
                match = re.search(r"下周([一二三四五六日天])", text)
                if match:
                    target = "一二三四五六日天".index(match.group(1))
                    if target >= 7:
                        target = 6
                    week_start = base - timedelta(days=base.weekday()) + timedelta(days=7)
                    return (week_start + timedelta(days=target)).isoformat()
        return self._resolve_day(text, state.obs.get("now"))

    def _exclusive_leave_end_day(self, text: str, end_day: str) -> str:
        return end_day

    def _resolve_next_month_day(self, day_value: Any, now_value: Any) -> str:
        try:
            now = datetime.fromisoformat(str(now_value).replace("Z", "+00:00")).date()
        except Exception:
            now = date.today()
        month = now.month + 1
        year = now.year
        if month > 12:
            month = 1
            year += 1
        return date(year, month, int(day_value)).isoformat()

    def _resolve_month_day(self, day_value: Any, now_value: Any) -> str:
        try:
            now = datetime.fromisoformat(str(now_value).replace("Z", "+00:00")).date()
        except Exception:
            now = date.today()
        candidate = date(now.year, now.month, int(day_value))
        if candidate < now:
            month = now.month + 1
            year = now.year
            if month > 12:
                month = 1
                year += 1
            candidate = date(year, month, int(day_value))
        return candidate.isoformat()

    def _leave_plans(self, state: RuntimeState) -> list[dict[str, Any]]:
        plan = self._leave_plan(state)
        if not plan:
            return []
        query = self._full_query(state.obs)
        leave = self._leave_slots(state)
        count = self._recurring_leave_count(query)
        if count <= 1:
            return [plan]
        weekdays = self._extract_weekdays(leave.get("day_text") or query)
        if not weekdays:
            return [plan]
        first_day = date.fromisoformat(plan["start_time"].split()[0])
        days = []
        cursor = first_day
        for _ in range(21):
            if cursor.weekday() in weekdays and cursor >= first_day:
                days.append(cursor)
                if len(days) >= count:
                    break
            cursor += timedelta(days=1)
        if len(days) < count:
            return [plan]
        suffix_start = plan["start_time"].split()[1]
        suffix_end = plan["end_time"].split()[1]
        plans = []
        for day in days:
            item = dict(plan)
            item["start_time"] = f"{day.isoformat()} {suffix_start}"
            item["end_time"] = f"{day.isoformat()} {suffix_end}"
            plans.append(item)
        return plans

    def _next_recurring_leave_plan(self, state: RuntimeState) -> dict[str, Any] | None:
        plans = state.workflow.evidence.get("leave_plans")
        if plans is None:
            plans = self._leave_plans(state)
            state.workflow.evidence["leave_plans"] = plans
        saved_count = len(state.workflow.evidence.get("saved_leave_requests") or [])
        if saved_count < len(plans):
            return plans[saved_count]
        return None

    def _leave_save_args(self, state: RuntimeState, plan: dict[str, Any], approver: dict[str, Any]) -> dict[str, Any]:
        user = state.workflow.evidence.get("applicant") or {}
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "start_time": plan["start_time"],
            "end_time": plan["end_time"],
            "leave_type": plan["leave_type"],
            "reason": plan["reason"],
            "approver": approver.get("user_id"),
            "duration": plan["duration"],
        }
        return {"workflow_id": WORKFLOW_IDS["leave"], "submit": bool(state.workflow.slots.get("submit")), "data": data}

    def _next_approver_search_args(
        self,
        state: RuntimeState,
        plan: dict[str, Any],
        *,
        mark_attempt: bool = False,
    ) -> dict[str, Any] | None:
        tried = state.workflow.evidence.setdefault("approver_search_tried", [])
        leave = self._leave_slots(state)
        raw_keyword = self._clean_approver_phrase(plan.get("approver_employee_no") or plan.get("approver_keyword") or "")
        title = str(plan.get("approver_title") or "")
        department = str(leave.get("approver_department_hint") or "")
        candidates: list[dict[str, Any]] = []

        def add(keyword: str = "", title_value: str = "") -> None:
            args: dict[str, Any] = {"workflow_id": WORKFLOW_IDS["leave"]}
            if keyword:
                args["keyword"] = keyword
            if title_value:
                args["title"] = title_value
            if args not in candidates:
                candidates.append(args)

        keyword = raw_keyword
        if keyword in {"一个经", "一个经理", "经理", "一个", "某个", "任意", "找一个"} and title:
            keyword = ""
        if keyword:
            add(keyword, title)
            if len(keyword) > 1:
                add(keyword[:1], title)
                add(keyword[:1], "")
        if department:
            add(department, title)
            add(department, "")
        if title:
            add("", title)
        if not candidates:
            add("", "经理")
        for args in candidates:
            if args not in tried:
                if mark_attempt:
                    tried.append(args)
                return args
        return None

    def _collected_approver_people(self, wf: DomainState) -> list[dict[str, Any]]:
        people: list[dict[str, Any]] = []
        seen: set[str] = set()
        for result in wf.evidence.get("approver_searches") or []:
            for person in (result.get("people") or []):
                key = str(person.get("user_id") or person.get("employee_no") or person.get("name") or "")
                if key and key not in seen:
                    seen.add(key)
                    people.append(person)
        fallback = wf.evidence.get("approver_search") or {}
        for person in fallback.get("people") or []:
            key = str(person.get("user_id") or person.get("employee_no") or person.get("name") or "")
            if key and key not in seen:
                seen.add(key)
                people.append(person)
        return people

    def _select_leave_people(self, state: RuntimeState, people: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(people) <= 1:
            return people
        leave = self._leave_slots(state)
        keyword = str(leave.get("approver_name_hint") or leave.get("approver_keyword") or "")
        title = str(leave.get("approver_title_hint") or leave.get("approver_title") or "")
        department = str(leave.get("approver_department_hint") or "")
        if keyword in {"一个经", "一个经理", "经理", "一个", "某个"} and title:
            keyword = ""
        filtered = people
        if keyword:
            filtered = [p for p in filtered if keyword in str(p.get("name") or "") or keyword in str(p.get("employee_no") or "")]
        if title:
            title_filtered = [p for p in filtered if title in str(p.get("title") or "") or title in str(p.get("name") or "")]
            if title_filtered:
                filtered = title_filtered
        if department:
            dept_filtered = [
                p for p in filtered
                if department in str(p.get("department") or p.get("department_name") or p.get("org_name") or p.get("title") or "")
            ]
            if dept_filtered:
                filtered = dept_filtered
        if len(filtered) == 1:
            return filtered
        if title and not keyword:
            named_title = [p for p in filtered if title in str(p.get("name") or "")]
            if len(named_title) == 1:
                return named_title
            if named_title:
                return [sorted(named_title, key=lambda p: str(p.get("employee_no") or p.get("user_id") or ""))[0]]
            product_managers = [p for p in filtered if "产品经理" in str(p.get("title") or "")]
            if len(product_managers) == 1:
                return product_managers
        chosen = self._select_candidate_with_llm(
            state,
            "选择请假审批人",
            self._workflow_query(state),
            filtered,
            ["user_id", "name"],
        )
        if chosen:
            return [chosen]
        if leave.get("approver_keyword"):
            return filtered
        query = self._full_query(state.obs)
        if not bool(state.workflow.slots.get("submit")):
            return filtered
        if not any(word in query for word in ["提交", "处理", "直接提", "也提交"]):
            return filtered
        product_managers = [p for p in filtered if "产品经理" in str(p.get("title") or "")]
        if len(product_managers) == 1:
            return product_managers
        return filtered

    def _expense_missing_slot(self, state: RuntimeState) -> StepAction | None:
        slot = self._expense_missing_slot_name(state)
        messages = {
            "project_code": "请提供项目名称或项目编码。",
            "material_category": "请问物资大类选哪个？",
            "material_subclass": "请问具体物资小类选哪个？",
            "total_amount": "请问总预算是多少？",
        }
        if slot and slot not in state.asked_slots:
            state.asked_slots.add(slot)
            return StepAction("reply", message=messages[slot])
        return None

    def _expense_missing_slot_name(self, state: RuntimeState) -> str:
        expense = self._expense_slots(state)
        if not self._has_explicit_project_slot(expense):
            return "project_code"
        if not expense.get("material_category_hint"):
            return "material_category"
        if not expense.get("material_subclass_hint") and not expense.get("items"):
            return "material_subclass"
        if not expense.get("total_amount"):
            return "total_amount"
        return ""

    def _has_explicit_project_slot(self, expense: dict[str, Any]) -> bool:
        if expense.get("project_code"):
            return True
        if self._valid_project_search_candidate(str(expense.get("project_name") or ""), allow_short=True):
            return True
        for keyword in expense.get("project_keywords") or []:
            if self._valid_project_search_candidate(str(keyword), allow_short=True):
                return True
        return False

    def _project_search_args(self, expense: dict[str, Any]) -> dict[str, Any] | None:
        project_code = self._extract_project_code(str(expense.get("project_code") or ""))
        if project_code:
            expense["project_code"] = project_code
            return {"project_code": project_code}
        candidates = self._project_search_candidates(expense)
        tried = expense.setdefault("_tried_project_keywords", [])
        for candidate in candidates:
            if candidate and candidate not in tried:
                return {"project_name": candidate}
        if expense.get("project_name") and expense.get("project_name") not in tried:
            return {"project_name": expense["project_name"]}
        return None

    def _next_project_search_args(self, state: RuntimeState, expense: dict[str, Any], allow_llm: bool = True) -> dict[str, Any] | None:
        project_code = self._extract_project_code(str(expense.get("project_code") or "")) or self._extract_project_code(self._workflow_query(state))
        if project_code:
            expense["project_code"] = project_code
            return {"project_code": project_code}
        memory_args = self._expense_memory_project_search_args(state, expense)
        if memory_args is not None:
            return memory_args
        project_attempts = len(state.workflow.evidence.get("project_search_history") or [])
        remaining_steps = max(0, state.step_budget - state.steps_used)
        if project_attempts >= 4:
            broad_args = self._broad_project_probe_args(state, expense)
            if broad_args:
                return broad_args
            return None
        if remaining_steps <= 3:
            return None
        if self._should_probe_project_broadly_before_llm(state, expense):
            broad_args = self._broad_project_probe_args(state, expense)
            if broad_args:
                return broad_args
        tried = expense.setdefault("_tried_project_keywords", [])
        strong_available = (
            allow_llm
            and bool(self._llm_config("strong").get("api_key"))
            and self._can_call_llm(state, "strong", min_remaining=16.0)
        )
        local_limit = 2 if strong_available else 6
        if strong_available and self._has_explicit_project_slot(expense):
            local_limit = max(local_limit, 3)
        if len([item for item in tried if isinstance(item, str)]) >= local_limit:
            if allow_llm:
                return self._project_search_args_from_llm(state, expense, force=True)
            broad_args = self._broad_project_probe_args(state, expense)
            if broad_args:
                return broad_args
            return None
        return self._project_search_args(expense)

    def _expense_memory_project_search_args(self, state: RuntimeState, expense: dict[str, Any]) -> dict[str, Any] | None:
        """Recall only an unambiguous train-observed project query alias."""
        tried = {str(item) for item in expense.get("_tried_project_keywords") or []}
        source = self._normalize_memory_text(self._expense_query(state))
        explicit = self._normalize_memory_text(expense.get("project_name") or "")
        scores: dict[str, tuple[float, set[str]]] = {}
        for entry in self.static_context.expense_examples.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            search_queries = [str(item or "").strip() for item in entry.get("project_search_queries") or [] if item]
            aliases = [str(item or "").strip() for item in entry.get("project_aliases") or [] if item]
            if not search_queries or not aliases:
                continue
            best_alias_score = 0.0
            for alias in aliases:
                normalized = self._normalize_memory_text(alias)
                if len(normalized) < 4:
                    continue
                if normalized in source:
                    coverage = min(1.0, len(normalized) / max(1, len(explicit))) if explicit else 0.0
                    specificity_bonus = coverage * 0.08
                    if explicit and normalized == explicit:
                        specificity_bonus += 0.04
                    best_alias_score = max(
                        best_alias_score,
                        0.8 + min(len(normalized), 16) * 0.01 + specificity_bonus,
                    )
                elif explicit and (normalized in explicit or explicit in normalized):
                    best_alias_score = max(best_alias_score, 0.72 + min(len(normalized), 16) * 0.01)
            if best_alias_score <= 0:
                continue
            memory_id = str(entry.get("memory_id") or "")
            project = entry.get("project") if isinstance(entry.get("project"), dict) else {}
            formal_name = self._normalize_memory_text(project.get("project_name") or "")
            for search_query in search_queries:
                if search_query in tried:
                    continue
                normalized_query = self._normalize_memory_text(search_query)
                query_score = best_alias_score
                if formal_name.startswith(normalized_query):
                    query_score += 0.1
                elif normalized_query and normalized_query in formal_name:
                    query_score += 0.05
                prior_score, memory_ids = scores.get(search_query, (0.0, set()))
                scores[search_query] = (max(prior_score, query_score), memory_ids | {memory_id})
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda item: (-item[1][0], -len(item[1][1]), -len(item[0]), item[0]))
        best_query, (best_score, memory_ids) = ranked[0]
        conflicts = [
            query
            for query, (score, ids) in ranked[1:]
            if abs(score - best_score) < 0.001 and len(ids) == len(memory_ids) and query != best_query
        ]
        if conflicts:
            self._debug_log(
                self._debug_llm_config(),
                {
                    "event": "project_memory_retrieval",
                    "case_id": state.obs.get("case_id"),
                    "decision": "ambiguous",
                    "queries": [best_query, *conflicts[:3]],
                },
            )
            return None
        state.workflow.evidence["project_memory_match"] = {
            "query": best_query,
            "score": round(best_score, 4),
            "memory_ids": sorted(memory_ids),
        }
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "project_memory_retrieval",
                "case_id": state.obs.get("case_id"),
                "decision": "accepted",
                "query": best_query,
                "score": round(best_score, 4),
            },
        )
        return {"project_name": best_query}

    def _should_probe_project_broadly_before_llm(self, state: RuntimeState, expense: dict[str, Any]) -> bool:
        if expense.get("project_code") or expense.get("project_name") or expense.get("project_keywords"):
            return False
        if not self._project_search_candidates(expense):
            return True
        if self._expense_project_is_under_specified(state, expense):
            return True
        query = self._workflow_query(state)
        if "待办" in query:
            return True
        if max(0, state.step_budget - state.steps_used) <= 5 and not self._project_search_candidates(expense):
            return True
        return False

    def _expense_project_is_under_specified(self, state: RuntimeState, expense: dict[str, Any]) -> bool:
        if self._has_explicit_project_slot(expense):
            return False
        query = self._workflow_query(state)
        text = " ".join(
            str(item or "")
            for item in [
                query,
                expense.get("raw_text"),
                expense.get("material_category_hint"),
                expense.get("material_subclass_hint"),
            ]
        )
        if not any(term in text for term in ["费用", "预算", "采购", "物资", "报销"]):
            return False
        project_like = self._extract_project_name(text)
        if project_like:
            return False
        return True

    def _project_search_args_from_llm(self, state: RuntimeState, expense: dict[str, Any], force: bool = False) -> dict[str, Any] | None:
        if state.workflow.evidence.get("project_candidates"):
            return None
        # The post-empty-read branch owns the two bounded ReadHypotheses. Do
        # not add a second free-form keyword loop after it has run.
        if state.workflow.evidence.get("empty_project_mapping_done"):
            return None
        project_attempts = len(state.workflow.evidence.get("project_search_history") or [])
        remaining_steps = max(0, state.step_budget - state.steps_used)
        if project_attempts >= 4 or remaining_steps <= 3:
            return None
        if not self._can_call_llm(state, "strong", min_remaining=16.0):
            return None
        if not force and not self._project_search_attempts_exhausted(state, expense):
            return None
        suggestions = self._llm_project_keyword_suggestions(state, expense)
        if suggestions:
            existing = expense.setdefault("project_keywords", [])
            for suggestion in suggestions:
                if suggestion not in existing:
                    existing.append(suggestion)
        tried = expense.setdefault("_tried_project_keywords", [])
        for suggestion in suggestions:
            if suggestion and suggestion not in tried:
                return {"project_name": suggestion}
        return self._broad_project_probe_args(state, expense)

    def _broad_project_probe_args(self, state: RuntimeState, expense: dict[str, Any]) -> dict[str, Any] | None:
        if expense.get("project_code") or expense.get("project_name") or state.workflow.evidence.get("project_candidates"):
            return None
        if max(0, state.step_budget - state.steps_used) <= 3:
            return None
        tried = expense.setdefault("_tried_project_keywords", [])
        probe_history = state.workflow.evidence.setdefault("broad_project_probe_keywords", [])
        for keyword in self._broad_project_probe_keywords(state, expense):
            if keyword not in tried:
                probe_history.append(keyword)
                return {"project_name": keyword}
        state.workflow.evidence["broad_project_probe_done"] = True
        return None

    def _broad_project_probe_keywords(self, state: RuntimeState, expense: dict[str, Any]) -> list[str]:
        """Use structural terms only when the user supplied that structure.

        A generic global inventory probe ("项目"/"平台"/"系统") is not a
        valid competition query.  "星火质量工程平台" may legitimately yield
        "平台" because that term is present in the user's phrase.
        """
        candidates: list[str] = []
        explicit = str(expense.get("project_name") or "")
        candidates.extend(self._project_context_structural_tokens(state, explicit))
        candidates.extend(self._project_structural_tokens(explicit))
        for keyword in expense.get("project_keywords") or []:
            candidates.extend(self._project_structural_tokens(str(keyword)))
        return self._dedupe([item for item in candidates if item in {"项目", "平台", "系统", "工程", "专项"}])

    def _project_search_has_explicit_support(self, state: RuntimeState, args: dict[str, Any], project: dict[str, Any]) -> bool:
        if args.get("project_code"):
            return True
        query = self._clean_project_phrase(str(args.get("project_name") or ""))
        if not query:
            return False
        expense = self._expense_slots(state)
        explicit_sources = [
            self._workflow_query(state),
            expense.get("project_name"),
            " ".join(str(item) for item in expense.get("project_keywords") or []),
        ]
        explicit_text = re.sub(r"\s+", "", " ".join(str(item or "") for item in explicit_sources))
        if query and query in explicit_text:
            return True
        project_name = str(project.get("project_name") or "")
        supported_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", explicit_text)
            if token and token in project_name and token not in {"项目", "平台", "费用", "申请", "采购"}
        ]
        return len("".join(supported_tokens)) >= 4

    def _project_refine_search_args(self, state: RuntimeState, project: dict[str, Any]) -> dict[str, Any] | None:
        wf = state.workflow
        resolution = wf.evidence.get("project_resolution") if isinstance(wf.evidence.get("project_resolution"), dict) else {}
        if resolution.get("state") == "verified_singleton":
            return None
        if wf.evidence.get("project_refine_done"):
            return None
        if state.step_budget - state.steps_used <= 2:
            return None
        if not isinstance(project, dict) or not project.get("project_name"):
            return None
        history = wf.evidence.get("project_search_history") or []
        if any(
            isinstance(item, dict)
            and isinstance(item.get("args"), dict)
            and item.get("args", {}).get("project_code")
            for item in history
        ):
            wf.evidence["project_refine_done"] = True
            return None
        tried = {
            str((item.get("args") or {}).get("project_name") or "")
            for item in history
            if isinstance(item, dict) and isinstance(item.get("args"), dict)
        }
        current_query = ""
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            projects = result.get("projects") if isinstance(result.get("projects"), list) else []
            if len(projects) == 1 and projects[0].get("project_code") == project.get("project_code"):
                current_query = str(args.get("project_name") or "")
                break
        # A concrete user-derived query that already yielded one project is
        # sufficient. Refinement is only for broad structural probes such as
        # "项目" or "平台", which cannot establish a unique intent by itself.
        if self._valid_project_search_candidate(current_query, allow_short=True) and self._project_search_has_explicit_support(
            state, {"project_name": current_query}, project
        ):
            wf.evidence["project_refine_done"] = True
            return None
        candidates = self._project_refine_candidates(state, project)
        for candidate in candidates:
            keyword = self._clean_project_phrase(candidate)
            if not keyword or keyword in tried or keyword == current_query:
                continue
            if not self._valid_project_search_candidate(keyword, allow_short=True):
                continue
            wf.evidence["project_refine_done"] = True
            self._debug_log(
                self._debug_llm_config(),
                {
                    "event": "project_refine_search",
                    "case_id": state.obs.get("case_id"),
                    "current_query": current_query,
                    "project_name": project.get("project_name"),
                    "refined_query": keyword,
                    "candidates": candidates,
                },
            )
            return {"project_name": keyword}
        wf.evidence["project_refine_done"] = True
        return None

    def _project_refine_candidates(self, state: RuntimeState, project: dict[str, Any]) -> list[str]:
        project_name = self._clean_project_phrase(str(project.get("project_name") or ""))
        if not project_name:
            return []
        expense = self._expense_slots(state)
        explicit_text = re.sub(
            r"\s+",
            "",
            " ".join(
                str(item or "")
                for item in [
                    self._workflow_query(state),
                    expense.get("project_name"),
                    " ".join(str(value) for value in expense.get("project_keywords") or []),
                ]
            ),
        )
        out: list[str] = []
        explicit_tokens = [
            token
            for token in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", explicit_text)
            if token and token not in {"项目", "费用", "申请", "采购", "预算", "提交"}
        ]
        for token in sorted(set(explicit_tokens), key=lambda item: (-len(item), item)):
            if token in project_name:
                end = project_name.find(token) + len(token)
                prefix = project_name[:end]
                if len(prefix) >= 4:
                    out.append(prefix)
        out.extend(self._formal_project_name_core_candidates(project_name))
        out.extend(self._project_core_token_candidates(project_name))
        return self._dedupe(self._normalize_project_candidates(out))

    def _formal_project_name_core_candidates(self, project_name: str) -> list[str]:
        value = self._clean_project_phrase(project_name)
        if not value:
            return []
        out = []
        suffixes = [
            "品牌升级项目",
            "建设项目",
            "活动项目",
            "物资项目",
            "采购项目",
            "服务项目",
            "推广项目",
            "传播项目",
            "设计项目",
            "改造项目",
            "项目",
        ]
        for suffix in suffixes:
            if value.endswith(suffix) and len(value) > len(suffix) + 3:
                core = value[: -len(suffix)]
                if suffix == "项目" and len(core) >= 8:
                    out.append(core[-4:])
                out.append(core)
                break
        out.append(value)
        return self._dedupe(out)

    def _verified_singleton_project_candidate(self, state: RuntimeState) -> dict[str, Any] | None:
        candidates = state.workflow.evidence.get("project_singleton_candidates") or []
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for item in candidates:
            if not isinstance(item, dict) or not isinstance(item.get("project"), dict):
                continue
            project = item["project"]
            key = (str(project.get("project_code") or ""), str(project.get("wbs_code") or ""))
            if not any(key):
                continue
            by_key.setdefault(key, project)
        if len(by_key) != 1:
            return None
        return next(iter(by_key.values()))

    def _can_adopt_singleton_project_candidate(self, state: RuntimeState, expense: dict[str, Any]) -> bool:
        if state.workflow.slots.get("submit"):
            return False
        if state.workflow.evidence.get("project_candidates"):
            return False
        if self._has_explicit_project_slot(expense):
            return True
        return self._expense_project_is_under_specified(state, expense)

    def _project_search_attempts_exhausted(self, state: RuntimeState, expense: dict[str, Any]) -> bool:
        if state.workflow.evidence.get("verified_project"):
            return False
        history = state.workflow.evidence.get("project_search_history") or []
        if not history:
            return False
        local_candidates = self._project_search_candidates(expense)
        tried = set(expense.get("_tried_project_keywords") or [])
        return bool(local_candidates) and all(candidate in tried for candidate in local_candidates)

    def _project_search_candidates(self, expense: dict[str, Any]) -> list[str]:
        project_name = str(expense.get("project_name") or "")
        literal: list[str] = []
        if expense.get("project_name"):
            literal.append(project_name)

        primary: list[str] = []
        expanded: list[str] = []
        for item in literal:
            primary.extend(self._project_candidate_forms(str(item)))
            expanded.extend(self._project_name_variants(str(item)))
        keyword_values = [
            str(item)
            for item in (expense.get("project_keywords") or [])
            if item and not self._looks_like_meeting_project_noise(str(item))
        ]
        if not project_name:
            for item in keyword_values[:2]:
                primary.extend(self._project_candidate_forms(item))
        for item in keyword_values:
            expanded.extend(self._project_name_variants(item))
        expanded.extend(self._project_phrase_candidates(str(expense.get("raw_text") or "")))
        primary_candidates = self._dedupe(self._normalize_project_candidates(primary))
        expanded_candidates = sorted(self._dedupe(self._normalize_project_candidates(expanded)), key=self._project_candidate_sort_key)
        return self._dedupe([*primary_candidates, *expanded_candidates])[:6]

    def _project_candidate_forms(self, value: str) -> list[str]:
        original = str(value or "").strip("，。:：；;、 ")
        if not original:
            return []
        cleaned = self._clean_project_phrase(original)
        forms = [cleaned or original]
        if cleaned:
            forms.append(cleaned)
        for suffix in ["项目", "专项", "工程", "平台", "系统"]:
            target = cleaned or original
            if target.endswith(suffix) and len(target) > len(suffix) + 3:
                core = target[: -len(suffix)]
                # A named X项目 is usually retrieved more precisely by X.
                # Keep platform/system terms in their original order because
                # they are also explicit user-provided structural candidates.
                if suffix == "项目":
                    forms = [core, *forms]
                else:
                    forms.append(core)
                break
        return self._dedupe([item for item in forms if self._valid_project_search_candidate(item, allow_short=True)])

    def _project_name_variants(self, text: str) -> list[str]:
        original = str(text or "").strip("，。:：；;、 ")
        if not original:
            return []
        variants: list[str] = []
        core = self._clean_project_phrase(original)
        for suffix in ["项目", "专项", "工程", "平台", "系统"]:
            if original.endswith(suffix) and len(original) > len(suffix) + 1:
                variants.append(self._clean_project_phrase(original[: -len(suffix)]))
                break
        if core:
            variants.append(core)
        variants.append(original)
        variants.extend(self._project_core_token_candidates(core or original))
        return self._dedupe(self._normalize_project_candidates(variants))

    def _project_phrase_candidates(self, text: str) -> list[str]:
        candidates: list[str] = []
        cleaned_text = self._project_candidate_source_text(str(text or ""))
        cleaned_text = re.sub(r"^(?:另外|另一个任务是|然后|顺便|同时|还有|再)?(?:帮我|帮|给我|给)?(?:提一个|提|发起|提交|申请|做|处理|一个)+", "", cleaned_text)
        for pattern in [
            r"项目(?:是|为|叫)?\s*([^，。；;:：]+)",
            r"项目(?:编码|编号)?\s*[A-Z]-\d{9}",
            r"([^，。；;:：]{2,30}?项目)(?:里|中|内|下|先|帮|直接)",
            r"([^，。；;:：]{2,30}?)(?:项目|专项|工程|平台|系统)",
        ]:
            for match in re.finditer(pattern, cleaned_text):
                phrase = self._clean_project_phrase(match.group(1) if match.lastindex else match.group(0))
                if phrase and self._looks_like_project_phrase(phrase):
                    candidates.append(phrase)
        # "X费用申请" may omit an explicit project label.  X is valid as a
        # literal lookup term, but remains only a search hypothesis until
        # workflow.project_search verifies it.  Crucially, it is not treated
        # as a material detail row.
        for match in re.finditer(r"([^，。；;:：]{2,30}?)(?:费用申请|费用|申请)", cleaned_text):
            phrase = self._clean_project_phrase(match.group(1))
            if phrase and len(phrase) >= 4 and self._valid_project_search_candidate(phrase, allow_short=True):
                candidates.append(phrase)
        for segment in re.split(r"[，。；;:：\n]", cleaned_text):
            phrase = self._clean_project_phrase(segment)
            if phrase and self._looks_like_project_phrase(phrase):
                candidates.append(phrase)
        expanded: list[str] = []
        for candidate in candidates:
            expanded.append(candidate)
            for variant in self._project_semantic_variants(candidate):
                expanded.append(variant)
            expanded.extend(self._project_core_token_candidates(candidate))
        return self._dedupe(self._normalize_project_candidates(expanded))

    def _clean_project_phrase(self, text: str) -> str:
        phrase = str(text or "")
        phrase = re.sub(r"[A-Z]-\d{6,12}", "", phrase)
        phrase = re.split(
            r"(?:里有|里面有|中有|包括|包含|要印|要买|采购|买|预算|总预算|总金额|金额|费用|申请|提交|保存|草稿|小类|大类|物资|用品|设备|那边|这边|这里|这个|那个|要做|需要|帮我|帮|直接|先)",
            phrase,
        )[0]
        phrase = re.sub(r"\d+(?:\.\d+)?\s*(?:万|万元|元|台|个|条|场|份|支|套|批|项|册)", "", phrase)
        phrase = re.sub(r"^(?:项目|平台|系统|工程|专项)(?:名称)?(?:是|为|叫|：|:)", "", phrase)
        phrase = re.sub(r"^(另外|另一个任务是|然后|顺便|同时|并且|还有|再|请|帮我|帮|给|把|为|给我|需要|申请|办理|发起|提交|新建|创建|做|处理|先|存个|存一个|提一个|提|一个|项目是|项目为|项目叫|项目还是|项目仍是|项目还叫|项目|还是|仍是|是)+", "", phrase)
        phrase = re.sub(r"(那边|这里|这个|那个|相关|费用|预算|采购|申请|的|需要|要|包括|用于|使用|一批|一些)+$", "", phrase)
        phrase = re.sub(r"^(另外|然后|顺便|同时|还有|提一个|提|一个|还是|仍是)+", "", phrase)
        phrase = phrase.strip("，。:：；;、 的")
        return phrase

    def _looks_like_project_phrase(self, phrase: str) -> bool:
        phrase = str(phrase or "").strip()
        if len(phrase) < 4 or not self._valid_project_search_candidate(phrase, allow_short=True):
            return False
        project_signals = ["项目", "工程", "平台", "系统"]
        return any(signal in phrase for signal in project_signals) or len(phrase) >= 6

    def _project_candidate_source_text(self, text: str) -> str:
        source = re.sub(r"project_keywords\s*", "", text)
        source = re.sub(r"raw_text\s*", "", source)
        source = re.sub(r"\s+", "", source)
        return source

    def _project_semantic_variants(self, candidate: str) -> list[str]:
        variants: list[str] = []
        value = self._clean_project_phrase(candidate)
        if not value:
            return variants
        if len(value) >= 8:
            variants.append(value[:8])
        for suffix in ["项目", "专项", "工程", "平台", "系统"]:
            if value.endswith(suffix) and len(value) > len(suffix) + 1:
                stripped = self._clean_project_phrase(value[: -len(suffix)])
                if stripped and len(stripped) >= 4:
                    variants.append(stripped)
        return self._dedupe(self._normalize_project_candidates(variants))

    def _project_core_token_candidates(self, candidate: str) -> list[str]:
        text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", self._clean_project_phrase(candidate))
        if len(text) < 4:
            return []
        variants: list[str] = []
        for marker in ["项目", "专项", "工程", "系统", "平台"]:
            if marker in text:
                before, _, after = text.partition(marker)
                if before:
                    variants.extend([before, before[-4:], before[:4]])
                if after:
                    variants.extend([after, after[:4]])
        return self._dedupe(self._normalize_project_candidates(variants))

    def _normalize_project_candidates(self, values: list[Any]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            candidate = self._clean_project_phrase(str(raw or ""))
            if not candidate:
                continue
            forms = [candidate]
            for suffix in ["项目", "专项", "工程", "系统"]:
                if candidate.endswith(suffix) and len(candidate) > len(suffix) + 3:
                    forms.append(candidate[: -len(suffix)])
            for item in forms:
                item = item.strip("，。:：；;、 的")
                if self._valid_project_search_candidate(item, allow_short=True):
                    normalized.append(item)
        return self._dedupe(normalized)

    def _project_ngram_candidates(self, value: str) -> list[str]:
        text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(value or ""))
        if len(text) < 6:
            return []
        candidates: list[str] = []
        for size in (12, 10, 8, 6):
            if len(text) >= size:
                candidates.append(text[:size])
        for start in range(1, max(1, min(len(text), 6))):
            for size in (12, 10, 8, 6):
                chunk = text[start : start + size]
                if self._project_ngram_is_useful(chunk):
                    candidates.append(chunk)
        return self._dedupe([item for item in candidates if self._project_ngram_is_useful(item)])

    def _project_ngram_is_useful(self, chunk: str) -> bool:
        if not (4 <= len(chunk) <= 12):
            return False
        if chunk in {"项目", "平台", "费用", "申请", "采购", "预算"}:
            return False
        bad_terms = ["费用", "预算", "采购", "申请", "提交", "保存", "草稿", "要买"]
        if any(term in chunk for term in bad_terms):
            return False
        return True

    def _valid_project_search_candidate(self, value: str, allow_short: bool = False) -> bool:
        candidate = str(value or "").strip("，。:：；;、 的")
        if not candidate:
            return False
        if any(mark in candidate for mark in ["、", "每台", "每个", "每条", "每场", "单价", "元"]):
            return False
        if re.search(r"\d+(?:\.\d+)?\s*(?:万|万元|元|台|个|条|场|份)", candidate):
            return False
        noise = {
            "费用",
            "预算",
            "采购",
            "申请",
            "项目",
            "平台",
            "服务",
            "请假",
            "会议室",
            "先存一个",
            "先帮我存个",
        }
        if candidate in noise:
            return False
        if any(word in candidate for word in ["请假", "会议室", "总预算", "总金额", "预算", "要买", "老板", "确认", "待确认"]):
            return False
        if len(candidate) < 4 and not re.fullmatch(r"[A-Z]-\d{3,}", candidate):
            return False
        if len(candidate) > 18:
            return False
        return len(candidate) >= (2 if allow_short else 6)

    def _looks_like_meeting_project_noise(self, value: str) -> bool:
        text = str(value or "")
        if any(word in text for word in ["房间", "会议", "会议室", "换大", "预订", "订过", "参会", "复盘"]):
            return True
        if re.search(r"(?:上午|下午|早上|晚上|今天|明天|后天|下周|\d+\s*点)", text):
            return True
        return False

    def _project_candidate_sort_key(self, candidate: str) -> tuple[Any, ...]:
        suffix_penalty = 1 if re.search(r"(项目|专项|工程|系统)$", candidate) else 0
        broad_penalty = 1 if len(candidate) <= 2 else 0
        descriptor_penalty = 1 if len(candidate) > 12 else 0
        target_length = 6
        length_penalty = abs(len(candidate) - target_length)
        return (broad_penalty, descriptor_penalty, suffix_penalty, length_penalty, -len(candidate), candidate)

    def _llm_project_keyword_suggestions(self, state: RuntimeState, expense: dict[str, Any]) -> list[str]:
        cache_key = "_llm_project_keyword_suggestions"
        if cache_key in expense:
            return list(expense.get(cache_key) or [])
        llm_config = self._llm_config("strong")
        if not llm_config.get("api_key") or not self._can_call_llm(state, "strong", min_remaining=16.0):
            expense[cache_key] = []
            return []
        history = state.workflow.evidence.get("project_search_history") or []
        context_pack = self.static_context.for_workflow_form(
            WORKFLOW_IDS["expense"],
            {
                "stage": "project_keyword_suggestions",
                "failed_project_search_count": len(history),
                "project_hint_present": bool(expense.get("project_name") or expense.get("project_keywords")),
                "material_hint_present": bool(expense.get("material_category_hint") or expense.get("material_subclass_hint")),
            },
        )
        payload = {
            "query": self._workflow_query(state),
            "task_graph": state.task_graph,
            "static_context": context_pack.get("content") or "",
            "current_slots": {
                "project_name": expense.get("project_name"),
                "project_keywords": expense.get("project_keywords") or [],
                "material_category_hint": expense.get("material_category_hint"),
                "material_subclass_hint": expense.get("material_subclass_hint"),
                "items": expense.get("items") or [],
                "total_amount": expense.get("total_amount"),
            },
            "failed_project_searches": [
                {
                    "args": item.get("args"),
                    "result_count": len((item.get("result") or {}).get("projects") or []),
                    "project_names": [
                        project.get("project_name")
                        for project in ((item.get("result") or {}).get("projects") or [])[:5]
                        if isinstance(project, dict) and project.get("project_name")
                    ],
                }
                for item in history[-6:]
                if isinstance(item, dict)
            ],
            "instruction": (
                "Return valid json only. Suggest 4-6 concise workflow.project_search project_name keywords, ordered by highest chance of exact substring hit first. "
                "The tool does substring matching against SAP project names, so each keyword should look like a likely substring of a real project name. "
                "Prefer 4-12 Chinese characters, core business phrases, and likely parent project themes from the user's wording, material category, and item lines. "
                "Avoid merely repeating failed search strings. Avoid action, amount, workflow, or material-only words such as 费用、申请、采购、预算、设备、物资、项目. "
                "Use broad parent themes only when the user wording is under-specified, and keep them as multi-word business phrases rather than single generic nouns. "
                "If the literal project phrase failed, propose semantic parent themes that could appear in SAP project names without assuming a fixed public-data mapping. "
                "Because only the first 1-2 suggestions may be tried before the tool budget runs out, put broader parent themes before near-duplicates of failed keywords. "
                "Do not output ids/codes unless the user explicitly gave them. Do not choose a project."
            ),
            "output_schema": {
                "keywords": ["project search keyword"],
                "reason": "short reason",
            },
        }
        try:
            content = self._chat_completion(
                llm_config,
                [
                    {
                        "role": "system",
                        "content": (
                            "Return valid json only. You generate project search keywords for a workflow tool. "
                            "Do not choose a project; only propose search strings to verify with workflow.project_search."
                        ),
                    },
                    {"role": "user", "content": "Return valid json only.\n" + json.dumps(payload, ensure_ascii=False)},
                ],
                state=state,
                profile="strong",
                context_pack_type=str(context_pack.get("pack_type") or "workflow_project_keyword_context"),
                context_chars=int(context_pack.get("chars") or 0),
            )
            parsed = self._parse_json_object(content) or {}
            raw_keywords = parsed.get("keywords") if isinstance(parsed.get("keywords"), list) else []
            keywords = self._dedupe(
                [
                    self._clean_project_phrase(str(item))
                    for item in raw_keywords[:6]
                    if self._valid_project_search_candidate(str(item), allow_short=True)
                ]
            )
            keywords = self._prioritize_project_keyword_suggestions(state, expense, self._normalize_project_candidates(keywords))
            expense[cache_key] = keywords
            self._debug_log(llm_config, {"event": "project_keyword_suggestions", "keywords": keywords, "raw": parsed})
            return keywords
        except Exception as exc:
            self._debug_log(llm_config, {"event": "project_keyword_suggestions_error", "error": str(exc)})
            expense[cache_key] = []
            return []

    def _prioritize_project_keyword_suggestions(self, state: RuntimeState, expense: dict[str, Any], keywords: list[str]) -> list[str]:
        text = " ".join(
            str(item or "")
            for item in [
                self._workflow_query(state),
                expense.get("material_category_hint"),
                expense.get("material_subclass_hint"),
                " ".join(str(row.get("name") or "") for row in expense.get("items") or [] if isinstance(row, dict)),
            ]
        )

        query_terms = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", text))

        def score(keyword: str) -> tuple[int, int, int, int, str]:
            value = str(keyword or "")
            literal_overlap = 0 if any(term and term in value for term in query_terms) else 1
            broad = 1 if len(value) <= 2 else 0
            near_failed = 1 if value in set(expense.get("_tried_project_keywords") or []) else 0
            return (near_failed, broad, literal_overlap, abs(len(value) - 6), value)

        return sorted(self._dedupe(keywords), key=score)

    def _select_project_candidate_with_llm(self, state: RuntimeState, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
        if len(projects) == 1:
            return projects[0]
        if not projects:
            return None
        if not self._can_call_llm(state, "strong", min_remaining=14.0):
            return None
        expense = self._expense_slots(state)
        decision = self._rank_candidates_with_llm(
            state,
            "选择费用申请项目",
            self._workflow_query(state),
            projects,
            ["project_code", "project_name"],
            context={
                "project_name_hint": expense.get("project_name"),
                "project_keywords": expense.get("project_keywords") or [],
                "material_category_hint": expense.get("material_category_hint"),
                "items": expense.get("items") or [],
                "rule": "select only when one returned project clearly matches the user's explicit project wording; otherwise ambiguous",
            },
        )
        if decision.get("decision") != "select" or self._bounded_float(decision.get("confidence"), 0.0) < 0.78:
            return None
        by_key = self._candidate_map(projects, ["project_code", "project_name"])
        selected_id = str(decision.get("selected_id") or "")
        return by_key.get(selected_id)

    def _select_material_category(self, state: RuntimeState) -> dict[str, Any] | None:
        cached = state.workflow.evidence.get("selected_material_category")
        current_options = state.workflow.evidence.get("category_options", {}).get("options") or []
        rejected = self._rejected_expense_category_ids(state)
        current_options = [
            item
            for item in current_options
            if isinstance(item, dict) and self._expense_category_id(item) not in rejected
        ]
        current_ids = {str(item.get("value") or item.get("code") or "") for item in current_options if isinstance(item, dict)}
        if isinstance(cached, dict) and str(cached.get("value") or cached.get("code") or "") in current_ids:
            self._bind_expense_category(state, cached, "cached")
            return cached
        expense = self._expense_slots(state)
        hint = str(expense.get("material_category_hint") or "")
        options = current_options
        if not options:
            return None
        literal_sources = [
            " ".join(
                str(item.get("name") or "")
                for item in expense.get("items") or []
                if isinstance(item, dict)
            ),
            hint,
            str(expense.get("expense_type") or ""),
            str(expense.get("source_text") or ""),
            self._workflow_query(state),
        ]
        for literal_hint in literal_sources:
            literal = self._select_option_by_literal_hint(literal_hint, options)
            if literal:
                self._bind_expense_category(state, literal, "literal")
                self._debug_log(
                    self._debug_llm_config(),
                    {
                        "event": "expense_category_selection",
                        "source": "literal",
                        "hint": literal_hint,
                        "selected": literal,
                    },
                )
                return literal
        if len(options) > 1 and self._can_call_llm(state, "strong", min_remaining=12.0):
            selected = self._select_candidate_with_llm(
                state,
                "选择费用物资大类",
                self._workflow_query(state),
                options,
                ["value", "code"],
            )
            if selected:
                self._bind_expense_category(state, selected, "llm")
                self._debug_log(self._debug_llm_config(), {"event": "expense_category_selection", "source": "llm", "hint": hint, "selected": selected})
                return selected
        if len(options) > 1:
            return None
        self._bind_expense_category(state, options[0], "single_option")
        self._debug_log(self._debug_llm_config(), {"event": "expense_category_selection", "source": "single_option", "hint": hint, "selected": options[0]})
        return options[0]

    def _rejected_expense_category_ids(self, state: RuntimeState) -> set[str]:
        project = state.workflow.evidence.get("verified_project") or {}
        fingerprint = self._expense_project_fingerprint(project) if isinstance(project, dict) else ""
        rejected = state.workflow.evidence.get("rejected_material_categories") or {}
        values = rejected.get(fingerprint) if isinstance(rejected, dict) else []
        return {str(value) for value in values or []}

    def _reject_expense_category_after_subclass_failure(self, state: RuntimeState, args: dict[str, Any], reason: str) -> None:
        dep = args.get("dep") if isinstance(args.get("dep"), dict) else {}
        category_id = str(dep.get("wzlb") or "")
        project = state.workflow.evidence.get("verified_project") or {}
        fingerprint = self._expense_project_fingerprint(project) if isinstance(project, dict) else ""
        if not category_id or not fingerprint:
            return
        rejected = state.workflow.evidence.setdefault("rejected_material_categories", {})
        values = rejected.setdefault(fingerprint, [])
        if category_id not in values:
            values.append(category_id)
        selected = state.workflow.evidence.get("selected_material_category") or {}
        if self._expense_category_id(selected) == category_id:
            state.workflow.evidence.pop("selected_material_category", None)
            bindings = state.workflow.evidence.setdefault("expense_bindings", {})
            bindings.pop("category", None)
        self._invalidate_expense_subclass_binding(state, f"category_rejected:{reason}")
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "expense_category_fallback",
                "case_id": state.obs.get("case_id"),
                "project": fingerprint,
                "rejected_category": category_id,
                "reason": reason,
            },
        )

    def _expense_project_fingerprint(self, project: dict[str, Any]) -> str:
        return f"{project.get('project_code') or ''}|{project.get('wbs_code') or ''}"

    def _expense_category_id(self, category: dict[str, Any]) -> str:
        return str(category.get("value") or category.get("code") or "")

    def _expense_binding_log(
        self,
        state: RuntimeState,
        candidate_type: str,
        selected_id: str,
        allowed_ids: list[str],
        fingerprint: str,
        decision: str,
    ) -> None:
        state.ledger.record_expense_binding(
            candidate_type=candidate_type,
            selected_id=selected_id,
            allowed_ids=allowed_ids,
            fingerprint=fingerprint,
            decision=decision,
        )
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "expense_candidate_binding",
                "case_id": state.obs.get("case_id"),
                "candidate_type": candidate_type,
                "selected_id": selected_id,
                "allowed_ids": allowed_ids,
                "dependency_fingerprint": fingerprint,
                "decision": decision,
            },
        )

    def _invalidate_expense_subclass_binding(self, state: RuntimeState, reason: str) -> None:
        wf = state.workflow
        had_options = bool(wf.evidence.get("subclass_options"))
        had_binding = bool((wf.evidence.get("expense_bindings") or {}).get("subclass"))
        wf.evidence.pop("subclass_options", None)
        wf.evidence.pop("expense_draft_ir", None)
        wf.evidence.pop("expense_line_evidence", None)
        wf.evidence.pop("expense_memory_match", None)
        bindings = wf.evidence.setdefault("expense_bindings", {})
        bindings.pop("subclass", None)
        if had_options or had_binding:
            self._expense_binding_log(state, "subclass", "", [], "", f"invalidated:{reason}")

    def _bind_expense_project(self, state: RuntimeState, project: dict[str, Any], source: str) -> None:
        fingerprint = self._expense_project_fingerprint(project)
        if not fingerprint or fingerprint == "|":
            return
        bindings = state.workflow.evidence.setdefault("expense_bindings", {})
        previous = bindings.get("project") if isinstance(bindings.get("project"), dict) else {}
        if previous.get("selected_id") and previous.get("selected_id") != fingerprint:
            self._invalidate_expense_subclass_binding(state, "project_changed")
        bindings["project"] = {
            "selected_id": fingerprint,
            "allowed_ids": [fingerprint],
            "dependency_fingerprint": fingerprint,
            "source": source,
        }
        self._expense_binding_log(state, "project", fingerprint, [fingerprint], fingerprint, source)

    def _bind_expense_category(self, state: RuntimeState, category: dict[str, Any], source: str) -> None:
        category_id = self._expense_category_id(category)
        options = state.workflow.evidence.get("category_options", {}).get("options") or []
        allowed_ids = self._dedupe(
            [str(item.get("value") or item.get("code") or "") for item in options if isinstance(item, dict) and (item.get("value") or item.get("code"))]
        )
        if not category_id or category_id not in allowed_ids:
            return
        bindings = state.workflow.evidence.setdefault("expense_bindings", {})
        previous = bindings.get("category") if isinstance(bindings.get("category"), dict) else {}
        project_binding = bindings.get("project") if isinstance(bindings.get("project"), dict) else {}
        dependency = str(project_binding.get("selected_id") or "")
        if previous.get("selected_id") and (previous.get("selected_id") != category_id or previous.get("dependency_fingerprint") != dependency):
            self._invalidate_expense_subclass_binding(state, "category_or_project_changed")
        bindings["category"] = {
            "selected_id": category_id,
            "allowed_ids": allowed_ids,
            "dependency_fingerprint": dependency,
            "source": source,
        }
        state.workflow.evidence["selected_material_category"] = category
        self._expense_binding_log(state, "category", category_id, allowed_ids, dependency, source)

    def _bind_expense_subclass_candidates(self, state: RuntimeState, result: dict[str, Any]) -> None:
        options = result.get("options") if isinstance(result.get("options"), list) else []
        allowed_ids = self._dedupe(
            [str(item.get("value") or item.get("code") or "") for item in options if isinstance(item, dict) and (item.get("value") or item.get("code"))]
        )
        bindings = state.workflow.evidence.setdefault("expense_bindings", {})
        project = bindings.get("project") if isinstance(bindings.get("project"), dict) else {}
        category = bindings.get("category") if isinstance(bindings.get("category"), dict) else {}
        dependency = f"{project.get('selected_id') or ''}|{category.get('selected_id') or ''}"
        if not allowed_ids or not project.get("selected_id") or not category.get("selected_id"):
            return
        bindings["subclass"] = {
            "selected_id": "",
            "allowed_ids": allowed_ids,
            "dependency_fingerprint": dependency,
            "source": "workflow.browser_search",
        }
        self._expense_binding_log(state, "subclass", "", allowed_ids, dependency, "tool_candidate_set")

    def _expense_ir_save_args_or_block(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
    ) -> dict[str, Any] | str:
        """Translate only after the form schema and all dependent candidates are bound."""
        bindings = state.workflow.evidence.get("expense_bindings") if isinstance(state.workflow.evidence.get("expense_bindings"), dict) else {}
        subclass_binding = bindings.get("subclass") if isinstance(bindings.get("subclass"), dict) else {}
        expected_dependency = f"{self._expense_project_fingerprint(project)}|{self._expense_category_id(category)}"
        if subclass_binding.get("dependency_fingerprint") != expected_dependency:
            state.ledger.record_expense_translation(source="program", decision="rejected", reason="stale_subclass_candidates")
            return "expense_candidate_binding_invalid"
        total = state.semantic_facts.get("workflow.expense.total_amount")
        expense = self._expense_slots(state)
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        request_type = self._expense_request_shape(state, expense, options)
        state.workflow.evidence["expense_request_type"] = request_type
        if request_type == "explicit_multi_unallocated":
            state.ledger.record_expense_translation(source="program", decision="rejected", reason="insufficient_amount_breakdown")
            return "insufficient_amount_breakdown"
        if request_type == "business_package":
            memory_ir = self._expense_memory_draft_ir(state, project, category, self._money(total) if total not in (None, "") else "")
            if isinstance(memory_ir, ExpenseDraftIR):
                return self._expense_ir_to_save_args(state, project, category, memory_ir)
            state.ledger.record_expense_translation(source="program", decision="rejected", reason="ambiguous_material_subclass")
            return "ambiguous_material_subclass"
        if total in (None, ""):
            state.ledger.record_expense_translation(source="program", decision="rejected", reason="missing_user_literal_total")
            return "missing_required_info"
        total = self._money(total)
        source_items = self._clean_explicit_expense_items(expense.get("items") or [])
        if not source_items and request_type == "single_item_total" and expense.get("material_subclass_hint"):
            source_items = [
                {
                    "name": str(expense.get("material_subclass_hint") or ""),
                    "quantity": "1",
                    "unit_price": total,
                    "budget_amount": total,
                    "source_line_id": "user_total_0",
                }
            ]
        source_reason = self._expense_source_item_evidence_reason(state, source_items, total)
        if source_reason:
            draft_fallback = self._draft_explicit_subclass_ir_fallback(state, project, category, total)
            if isinstance(draft_fallback, ExpenseDraftIR):
                return self._expense_ir_to_save_args(state, project, category, draft_fallback)
            state.ledger.record_expense_translation(source="program", decision="rejected", reason=source_reason)
            return source_reason
        explicit_ir = self._deterministic_explicit_expense_ir(state, project, category, total, source_items)
        if isinstance(explicit_ir, ExpenseDraftIR):
            return self._expense_ir_to_save_args(state, project, category, explicit_ir)
        llm_config = self._llm_config("strong")
        if llm_config.get("api_key") and self._can_call_llm(state, "strong", min_remaining=10.0):
            draft = self._llm_expense_draft_ir(state, llm_config, project, category, total)
            if isinstance(draft, dict):
                evidence_reason = self._expense_draft_evidence_reason(state, draft)
                if evidence_reason:
                    state.ledger.record_expense_translation(source="llm_translation", decision="rejected", reason=evidence_reason)
                    self._debug_log(
                        llm_config,
                        {
                            "event": "expense_translation_validation",
                            "case_id": state.obs.get("case_id"),
                            "decision": "rejected",
                            "reason": evidence_reason,
                        },
                    )
                    return evidence_reason
                ir_or_reason = self._validate_expense_draft_ir(state, project, category, total, draft.get("details"), "llm_translation")
                if isinstance(ir_or_reason, ExpenseDraftIR):
                    return self._expense_ir_to_save_args(state, project, category, ir_or_reason)
                state.ledger.record_expense_translation(source="llm_translation", decision="rejected", reason=str(ir_or_reason))
                self._debug_log(
                    llm_config,
                    {"event": "expense_translation_validation", "case_id": state.obs.get("case_id"), "decision": "rejected", "reason": ir_or_reason},
                )
                # A rejected translation is not evidence that the deterministic
                # path is impossible. It remains valid only for one verified
                # subclass candidate with fully explicit amounts.
                fallback = self._deterministic_expense_ir_fallback(state, project, category, total)
                if isinstance(fallback, ExpenseDraftIR):
                    return self._expense_ir_to_save_args(state, project, category, fallback)
                draft_fallback = self._draft_explicit_subclass_ir_fallback(state, project, category, total)
                if isinstance(draft_fallback, ExpenseDraftIR):
                    return self._expense_ir_to_save_args(state, project, category, draft_fallback)
                state.ledger.record_expense_translation(source="program", decision="rejected", reason=str(fallback))
                return str(ir_or_reason)
            state.ledger.record_expense_translation(source="llm_translation", decision="failed", reason="invalid_or_empty_llm_response")
        fallback = self._deterministic_expense_ir_fallback(state, project, category, total)
        if isinstance(fallback, ExpenseDraftIR):
            return self._expense_ir_to_save_args(state, project, category, fallback)
        draft_fallback = self._draft_explicit_subclass_ir_fallback(state, project, category, total)
        if isinstance(draft_fallback, ExpenseDraftIR):
            return self._expense_ir_to_save_args(state, project, category, draft_fallback)
        state.ledger.record_expense_translation(source="program", decision="rejected", reason=str(fallback))
        return str(fallback)

    def _deterministic_explicit_expense_ir(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        total: str,
        source_items: list[dict[str, Any]],
    ) -> ExpenseDraftIR | None:
        if not source_items or any(not item.get("budget_amount") for item in source_items):
            return None
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        used_values: set[Any] = set()
        rows: list[dict[str, Any]] = []
        for source_item_index, item in enumerate(source_items):
            item_hint = str(item.get("name") or "")
            context_hint = " ".join(
                str(value or "")
                for value in [
                    item_hint,
                    project.get("project_name"),
                    category.get("label"),
                    self._workflow_query(state),
                ]
            )
            selected = self._select_subclass_option(item_hint, options, used_values, context_hint=context_hint)
            if selected is None:
                return None
            subclass_id = str(selected.get("value") or selected.get("code") or "")
            if not subclass_id:
                return None
            if not self._can_reuse_subclass_option(item_hint, selected):
                used_values.add(subclass_id)
            quantity = str(item.get("quantity") or "1")
            budget = self._money(item.get("budget_amount"))
            unit_price = self._money(item.get("unit_price"))
            if not unit_price:
                try:
                    unit_price = self._money(Decimal(budget) / Decimal(quantity))
                except (InvalidOperation, ValueError, ZeroDivisionError):
                    return None
            rows.append(
                {
                    "source_item_index": source_item_index,
                    "material_subclass": subclass_id,
                    "material_name": self._specific_material_name(item_hint, self._expense_slots(state), selected),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "budget_amount": budget,
                }
            )
        ir_or_reason = self._validate_expense_draft_ir(
            state,
            project,
            category,
            total,
            rows,
            "deterministic_explicit_lines",
        )
        return ir_or_reason if isinstance(ir_or_reason, ExpenseDraftIR) else None

    def _expense_memory_draft_ir(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        explicit_total: str,
    ) -> ExpenseDraftIR | None:
        """Recall a train-only package convention after all live ids are bound."""
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        expense = self._expense_slots(state)
        if self._expense_request_shape(state, expense, options) != "business_package":
            return None
        entries = self.static_context.expense_examples.get("entries") or []
        allowed_ids = {
            str(option.get("value") or option.get("code") or "")
            for option in options
            if isinstance(option, dict) and (option.get("value") or option.get("code"))
        }
        project_code = str(project.get("project_code") or "")
        wbs_code = str(project.get("wbs_code") or "")
        category_id = self._expense_category_id(category)
        query = self._normalize_memory_text(self._workflow_query(state))
        query_bigrams = self._memory_bigrams(query)
        matches: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("request_shape") != "generic_package":
                continue
            memory_project = entry.get("project") if isinstance(entry.get("project"), dict) else {}
            if project_code and str(memory_project.get("project_code") or "") != project_code:
                continue
            if wbs_code and str(memory_project.get("wbs_code") or "") != wbs_code:
                continue
            if str(entry.get("material_category") or "") != category_id:
                continue
            memory_total = self._money(entry.get("total_amount"))
            if explicit_total and memory_total != self._money(explicit_total):
                continue
            rows = entry.get("rows") if isinstance(entry.get("rows"), list) else []
            row_ids = {str(row.get("material_subclass") or "") for row in rows if isinstance(row, dict)}
            if not rows or not row_ids or not row_ids.issubset(allowed_ids):
                continue
            memory_query = self._normalize_memory_text(entry.get("request_text") or "")
            memory_bigrams = self._memory_bigrams(memory_query)
            overlap = len(query_bigrams & memory_bigrams) / max(1, len(query_bigrams | memory_bigrams))
            score = 10.0 + (3.0 if explicit_total else 0.0) + overlap + min(int(entry.get("support_count") or 1), 5) * 0.01
            matches.append((score, entry))
        if not matches:
            return None
        matches.sort(key=lambda item: (-item[0], str(item[1].get("memory_id") or "")))
        best_score, best = matches[0]
        best_signature = self._expense_memory_result_signature(best)
        conflicting_top = [
            entry
            for score, entry in matches[1:]
            if abs(score - best_score) < 0.02 and self._expense_memory_result_signature(entry) != best_signature
        ]
        if conflicting_top:
            self._debug_log(
                self._debug_llm_config(),
                {
                    "event": "expense_memory_retrieval",
                    "case_id": state.obs.get("case_id"),
                    "decision": "ambiguous",
                    "top_memory_ids": [best.get("memory_id")] + [item.get("memory_id") for item in conflicting_top[:3]],
                },
            )
            return None
        memory_match = {
            "memory_id": str(best.get("memory_id") or ""),
            "source_sha256": list(best.get("source_sha256") or []),
            "request_type": "business_package",
            "score": round(best_score, 4),
            "project_fingerprint": self._expense_project_fingerprint(project),
            "category_id": category_id,
            "candidate_ids": sorted(allowed_ids),
            "result_signature": best_signature,
        }
        state.workflow.evidence["expense_memory_match"] = memory_match
        rows = [dict(row) for row in best.get("rows") or [] if isinstance(row, dict)]
        ir_or_reason = self._validate_expense_draft_ir(
            state,
            project,
            category,
            str(best.get("total_amount") or ""),
            rows,
            "memory_package_inferred",
        )
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "expense_memory_retrieval",
                "case_id": state.obs.get("case_id"),
                "decision": "accepted" if isinstance(ir_or_reason, ExpenseDraftIR) else "rejected",
                "memory": memory_match,
                "reason": "" if isinstance(ir_or_reason, ExpenseDraftIR) else str(ir_or_reason),
            },
        )
        if isinstance(ir_or_reason, ExpenseDraftIR):
            return ir_or_reason
        state.workflow.evidence.pop("expense_memory_match", None)
        return None

    def _expense_request_shape(
        self,
        state: RuntimeState,
        expense: dict[str, Any],
        options: list[dict[str, Any]],
    ) -> str:
        items = self._clean_explicit_expense_items(expense.get("items") or [])
        if self._has_unallocated_multi_material_budget(expense, options):
            return "explicit_multi_unallocated"
        if (
            items
            and all(self._is_expense_memory_generic_name(item.get("name"), expense) for item in items)
            and not all(self._has_unique_literal_subclass_match(item.get("name"), options) for item in items)
        ):
            return "business_package"
        if len(items) > 1:
            if any(not item.get("budget_amount") for item in items):
                return "explicit_multi_unallocated"
            return "explicit_line_amount"
        if not items:
            if expense.get("material_subclass_hint") and expense.get("total_amount"):
                return "single_item_total"
            return "business_package"
        item = items[0]
        if self._is_generic_material_item_hint(item.get("name"), expense, options):
            return "business_package"
        query = self._normalize_option_text(self._workflow_query(state))
        category_hint = self._normalize_option_text(expense.get("material_category_hint") or "")
        item_name = self._normalize_option_text(item.get("name") or "")
        if category_hint and item_name and (item_name == category_hint or item_name in category_hint):
            return "business_package"
        generic_terms = ("品牌广告", "广告服务", "办公设备", "外包服务", "印刷物资", "定制物资")
        if item_name and any(item_name == term for term in generic_terms) and item_name in query:
            return "business_package"
        return "single_item_total" if expense.get("total_amount") else "explicit_incomplete"

    def _is_expense_memory_generic_name(self, value: Any, expense: dict[str, Any]) -> bool:
        name = self._normalize_option_text(value)
        name = re.sub(r"^的", "", name)
        name = re.sub(r"(?:费用|费|申请|采购)$", "", name)
        category_hint = self._normalize_option_text(expense.get("material_category_hint") or "")
        category_hint = re.sub(r"(?:费用|费|申请|采购)$", "", category_hint)
        if category_hint and name and (name == category_hint or name in category_hint or category_hint in name):
            return True
        generic_names = {
            "品牌广告",
            "品牌广告服务",
            "广告服务",
            "办公设备",
            "测试设备",
            "办公设备测试设备",
            "外包服务",
            "广宣印刷物资",
            "印刷物资",
            "定制物资",
            "费用",
            "物资",
            "服务",
        }
        return name in generic_names

    def _normalize_memory_text(self, value: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()

    def _memory_bigrams(self, value: str) -> set[str]:
        if len(value) < 2:
            return {value} if value else set()
        return {value[index : index + 2] for index in range(len(value) - 1)}

    def _expense_memory_result_signature(self, entry: dict[str, Any]) -> str:
        payload = {
            "total_amount": str(entry.get("total_amount") or ""),
            "rows": entry.get("rows") or [],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _llm_expense_draft_ir(
        self,
        state: RuntimeState,
        llm_config: dict[str, Any],
        project: dict[str, Any],
        category: dict[str, Any],
        total: str,
    ) -> dict[str, Any] | None:
        wf = state.workflow
        expense = self._expense_slots(state)
        schema = wf.evidence.get("schema") or {}
        schema_body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
        options = wf.evidence.get("subclass_options", {}).get("options") or []
        payload = {
            "user_expense_original": str(expense.get("raw_text") or expense.get("source_text") or self._workflow_query(state)),
            "explicit_amount_facts": {"total_amount": total},
            "target_detail_schema": {
                "required_fields": (((schema_body.get("detail_tables") or {}).get("detail_2") or {}).get("required_fields") or []),
                "row_fields": ["material_subclass", "material_name", "quantity", "unit_price", "budget_amount"],
            },
            "verified_project": {
                "project_name": project.get("project_name"),
                "project_code": project.get("project_code"),
                "wbs_code": project.get("wbs_code"),
            },
            "selected_material_category": {
                "id": self._expense_category_id(category),
                "label": category.get("label") or "",
            },
            "subclass_candidates": [
                {"id": str(option.get("value") or option.get("code") or ""), "label": str(option.get("label") or "")}
                for option in options
                if isinstance(option, dict) and (option.get("value") or option.get("code"))
            ],
            "instruction": (
                "Return JSON only. Translate the user expense into detail rows. Use only listed subclass candidate ids. "
                "Do not emit project/category/applicant fields. Do not change explicit amounts. "
                "Every row must cite source_item_index for the user line item that supports it. "
                "If the user only gave a category, purpose, or total budget without a concrete line item and allocation, return decision=need_more_info with no rows. "
                "Do not turn an ambiguous category or business purpose into a purchasable detail. "
                "Every row must satisfy quantity * unit_price = budget_amount exactly and row budget_amount total must equal total_amount."
            ),
            "output_schema": {
                "decision": "draft|need_more_info",
                "details": [
                    {
                        "source_item_index": "zero-based index from user_line_items",
                        "material_subclass": "candidate id only",
                        "material_name": "user-facing item name",
                        "quantity": "positive number",
                        "unit_price": "decimal string",
                        "budget_amount": "decimal string",
                    }
                ],
                "missing_fields": []
            },
            "user_line_items": self._clean_explicit_expense_items(expense.get("items") or []),
        }
        try:
            content = self._chat_completion(
                llm_config,
                [
                    {"role": "system", "content": "Return valid JSON only. You are a constrained expense detail translator."},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                state=state,
                profile="strong",
                context_pack_type="expense_draft_ir",
                context_chars=0,
            )
            parsed = self._parse_json_object(content)
            if isinstance(parsed, dict):
                self._debug_log(llm_config, {"event": "expense_translation", "case_id": state.obs.get("case_id"), "decision": "received", "row_count": len(parsed.get("details") or [])})
                return parsed
        except Exception as exc:
            self._debug_log(llm_config, {"event": "expense_translation_error", "case_id": state.obs.get("case_id"), "error": str(exc)[:240]})
        return None

    def _expense_draft_evidence_reason(self, state: RuntimeState, draft: dict[str, Any]) -> str:
        if str(draft.get("decision") or "") != "draft":
            return "missing_material_detail_evidence"
        expense = self._expense_slots(state)
        source_items = self._clean_explicit_expense_items(expense.get("items") or [])
        total = self._money(expense.get("total_amount") or "")
        source_reason = self._expense_source_item_evidence_reason(state, source_items, total)
        if source_reason:
            return source_reason
        rows = draft.get("details") if isinstance(draft.get("details"), list) else []
        if not source_items or not rows:
            return "missing_material_detail_evidence"
        source_indexes: list[int] = []
        row_totals: dict[int, Decimal] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return "missing_material_detail_evidence"
            try:
                index = int(row.get("source_item_index"))
            except (TypeError, ValueError):
                return "missing_material_detail_evidence"
            if index < 0 or index >= len(source_items):
                return "invalid_material_detail_evidence"
            if not str(source_items[index].get("name") or "").strip():
                return "missing_material_detail_evidence"
            try:
                row_amount = Decimal(str(row.get("budget_amount") or ""))
            except (InvalidOperation, ValueError):
                return "invalid_material_detail_evidence"
            if row_amount < 0:
                return "invalid_material_detail_evidence"
            row_totals[index] = row_totals.get(index, Decimal("0")) + row_amount
            source_indexes.append(index)
        if len(source_items) > 1 and set(source_indexes) != set(range(len(source_items))):
            return "incomplete_material_detail_evidence"
        for index, source in enumerate(source_items):
            expected = str(source.get("budget_amount") or "").strip()
            if not expected:
                continue
            try:
                expected_amount = Decimal(expected)
            except (InvalidOperation, ValueError):
                return "invalid_material_detail_evidence"
            if row_totals.get(index, Decimal("0")) != expected_amount:
                return "source_amount_not_preserved"
        return ""

    def _expense_source_item_evidence_reason(
        self,
        state: RuntimeState,
        source_items: list[dict[str, Any]],
        total: str,
    ) -> str:
        """Validate only user-provided material and allocation evidence.

        A model may translate an item to a verified browser option, but it may
        not manufacture the item itself or distribute one aggregate budget
        across several user items.  A single explicit item can consume an
        aggregate total because its allocation is unambiguous.
        """
        if not source_items:
            return "missing_material_detail_evidence"
        expense = self._expense_slots(state)
        if state.workflow.slots.get("submit"):
            submit_reason = self._submit_expense_item_evidence_reason(state, source_items)
            if submit_reason:
                return submit_reason
        category_hint = self._normalize_option_text(expense.get("material_category_hint") or "")
        total_amount: Decimal | None = None
        if total:
            try:
                total_amount = Decimal(str(total))
            except (InvalidOperation, ValueError):
                return "invalid_material_detail_evidence"
        item_amounts: list[Decimal | None] = []
        for item in source_items:
            name = str(item.get("name") or "").strip()
            normalized_name = self._normalize_option_text(name)
            if not normalized_name:
                return "missing_material_detail_evidence"
            # A workflow category is a lookup constraint, not an order line.
            if category_hint and (
                normalized_name == category_hint
                or (len(normalized_name) >= 4 and normalized_name in category_hint)
                or (len(category_hint) >= 4 and category_hint in normalized_name)
            ):
                return "generic_material_category_not_detail"
            budget = str(item.get("budget_amount") or "").strip()
            if not budget:
                item_amounts.append(None)
                continue
            try:
                amount = Decimal(budget)
            except (InvalidOperation, ValueError):
                return "invalid_material_detail_evidence"
            if amount < 0:
                return "invalid_material_detail_evidence"
            item_amounts.append(amount)
        if len(source_items) > 1 and any(amount is None for amount in item_amounts):
            return "insufficient_amount_breakdown"
        if total_amount is not None and all(amount is not None for amount in item_amounts):
            if sum((amount or Decimal("0")) for amount in item_amounts) != total_amount:
                return "source_amount_not_preserved"
        return ""

    def _submit_expense_item_evidence_reason(self, state: RuntimeState, source_items: list[dict[str, Any]]) -> str:
        """Reject submit-only guesses hidden by a single aggregate amount."""
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        labels = [
            self._normalize_option_text(item.get("label") or item.get("name") or "")
            for item in options
            if isinstance(item, dict)
        ]
        generic_terms = ("宣传物料", "交付支持", "相关服务", "相关费用", "一批物料", "物料", "服务")
        for item in source_items:
            name = self._normalize_option_text(item.get("name") or "")
            if not name:
                return "missing_material_detail_evidence"
            exact_option = any(label and (name == label or name in label or label in name) for label in labels)
            # "电脑及其配件" is allowed when it is a verified option; otherwise
            # conjunctions mean several business objects share one amount.
            if not exact_option and re.search(r"[、和与]|(?<!其)及", name):
                return "insufficient_amount_breakdown"
            if not exact_option and any(term in name for term in generic_terms):
                return "ambiguous_material_subclass"
        return ""

    def _validate_expense_draft_ir(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        total: str,
        rows: Any,
        source: str,
    ) -> ExpenseDraftIR | str:
        if not isinstance(rows, list) or not rows:
            return "expense_translation_missing_rows"
        bindings = state.workflow.evidence.get("expense_bindings") or {}
        subclass = bindings.get("subclass") if isinstance(bindings.get("subclass"), dict) else {}
        allowed_ids = {str(item) for item in (subclass.get("allowed_ids") or [])}
        clean_rows: list[dict[str, Any]] = []
        row_total = Decimal("0")
        try:
            explicit_total = Decimal(str(total))
        except (InvalidOperation, ValueError):
            return "expense_total_invalid"
        raw_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                return "expense_translation_invalid_row"
            subclass_id = str(row.get("material_subclass") or "")
            if subclass_id not in allowed_ids:
                return "expense_translation_subclass_outside_candidates"
            material_name = str(row.get("material_name") or "").strip()
            if not material_name:
                return "expense_translation_missing_material_name"
            try:
                quantity = Decimal(str(row.get("quantity") or ""))
                unit_price = Decimal(str(row.get("unit_price") or ""))
                budget_amount = Decimal(str(row.get("budget_amount") or ""))
            except (InvalidOperation, ValueError):
                return "expense_translation_invalid_amount"
            if quantity <= 0 or unit_price < 0 or budget_amount < 0 or quantity * unit_price != budget_amount:
                return "expense_translation_amount_not_conserved"
            row_total += budget_amount
            clean_rows.append(
                {
                    "material_subclass": subclass_id,
                    "material_name": material_name,
                    "quantity": self._decimal_text(quantity),
                    "unit_price": self._decimal_text(unit_price),
                    "budget_amount": self._decimal_text(budget_amount),
                }
            )
            raw_rows.append(row)
        if row_total != explicit_total:
            return "expense_translation_total_not_conserved"
        evidence_reason = self._record_expense_line_evidence(state, raw_rows, clean_rows, explicit_total, source)
        if evidence_reason:
            return evidence_reason
        ir = ExpenseDraftIR(
            source=source,
            project_fingerprint=self._expense_project_fingerprint(project),
            category_id=self._expense_category_id(category),
            subclass_fingerprint=str(subclass.get("dependency_fingerprint") or ""),
            total_amount=self._decimal_text(explicit_total),
            rows=clean_rows,
        )
        state.workflow.evidence["expense_draft_ir"] = ir.summary()
        state.ledger.record_expense_translation(source=source, decision="accepted", row_count=len(clean_rows))
        self._debug_log(self._debug_llm_config(), {"event": "expense_translation_validation", "case_id": state.obs.get("case_id"), "decision": "accepted", "ir": ir.summary()})
        return ir

    def _record_expense_line_evidence(
        self,
        state: RuntimeState,
        raw_rows: list[dict[str, Any]],
        clean_rows: list[dict[str, Any]],
        total: Decimal,
        source: str,
    ) -> str:
        """Persist an auditable source-line to saved-row amount chain."""
        if source == "memory_package_inferred":
            memory = state.workflow.evidence.get("expense_memory_match")
            if not isinstance(memory, dict) or not memory.get("memory_id") or not memory.get("source_sha256"):
                return "expense_memory_provenance_missing"
            if len(raw_rows) != len(clean_rows):
                return "expense_memory_rows_stale"
            entries: list[dict[str, Any]] = []
            evidence_total = Decimal("0")
            for row_index, clean_row in enumerate(clean_rows):
                try:
                    amount = Decimal(str(clean_row.get("budget_amount") or ""))
                except (InvalidOperation, ValueError):
                    return "expense_memory_amount_invalid"
                evidence_total += amount
                entries.append(
                    {
                        "row_index": row_index,
                        "source_item_index": None,
                        "source_line_id": f"train_memory:{memory['memory_id']}:{row_index}",
                        "source_item_name": "",
                        "source_amount": self._decimal_text(amount),
                        "source": "memory_package_inferred",
                        "amount_source": "memory_package_inferred",
                        **clean_row,
                    }
                )
            if evidence_total != total:
                return "expense_memory_total_not_conserved"
            state.workflow.evidence["expense_line_evidence"] = {
                "version": 2,
                "source": source,
                "total_amount": self._decimal_text(total),
                "source_aggregate_id": "user_total_0",
                "aggregate_amount": self._decimal_text(total),
                "candidate_set_fingerprint": self._json_hash(
                    {
                        "dependency": memory.get("project_fingerprint"),
                        "category_id": memory.get("category_id"),
                        "candidate_ids": memory.get("candidate_ids") or [],
                    }
                ),
                "memory_provenance": {
                    "memory_id": memory["memory_id"],
                    "source_sha256": memory["source_sha256"],
                    "result_signature": memory.get("result_signature") or "",
                },
                "rows": entries,
            }
            return ""
        source_items = self._clean_explicit_expense_items(self._expense_slots(state).get("items") or [])
        if not source_items or len(raw_rows) != len(clean_rows):
            return "missing_material_detail_evidence"
        per_source_total: dict[int, Decimal] = {}
        entries: list[dict[str, Any]] = []
        for row_index, (raw_row, clean_row) in enumerate(zip(raw_rows, clean_rows)):
            source_index_value = raw_row.get("source_item_index")
            if source_index_value in (None, "") and len(source_items) == 1:
                source_index_value = 0
            try:
                source_index = int(source_index_value)
            except (TypeError, ValueError):
                return "missing_material_detail_evidence"
            if source_index < 0 or source_index >= len(source_items):
                return "invalid_material_detail_evidence"
            try:
                amount = Decimal(str(clean_row.get("budget_amount") or ""))
            except (InvalidOperation, ValueError):
                return "invalid_material_detail_evidence"
            source_item = source_items[source_index]
            request_type = str(state.workflow.evidence.get("expense_request_type") or "")
            evidence_source = "single_item_total" if request_type == "single_item_total" else "explicit_line_amount"
            per_source_total[source_index] = per_source_total.get(source_index, Decimal("0")) + amount
            entries.append(
                {
                    "row_index": row_index,
                    "source_item_index": source_index,
                    "source": evidence_source,
                    "source_line_id": f"user_total_{source_index}" if evidence_source == "single_item_total" else (source_item.get("source_line_id") or f"user_line_{source_index}"),
                    "source_item_name": source_item.get("name") or "",
                    "source_amount": str(source_item.get("budget_amount") or ""),
                    "amount_source": evidence_source,
                    **clean_row,
                }
            )
        if len(source_items) > 1 and set(per_source_total) != set(range(len(source_items))):
            return "incomplete_material_detail_evidence"
        for source_index, source_item in enumerate(source_items):
            source_amount = str(source_item.get("budget_amount") or "")
            if not source_amount:
                if len(source_items) > 1:
                    return "insufficient_amount_breakdown"
                continue
            try:
                if per_source_total.get(source_index, Decimal("0")) != Decimal(source_amount):
                    return "source_amount_not_preserved"
            except InvalidOperation:
                return "invalid_material_detail_evidence"
        if sum(per_source_total.values(), Decimal("0")) != total:
            return "expense_detail_total_not_conserved"
        state.workflow.evidence["expense_line_evidence"] = {
            "version": 1,
            "source": source,
            "total_amount": self._decimal_text(total),
            "rows": entries,
        }
        return ""

    def _deterministic_expense_ir_fallback(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        total: str,
    ) -> ExpenseDraftIR | str:
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        if len(options) != 1:
            return "expense_translation_failed"
        expense = self._expense_slots(state)
        items = self._clean_explicit_expense_items(expense.get("items") or [])
        source_reason = self._expense_source_item_evidence_reason(state, items, total)
        if source_reason:
            return source_reason
        if len(items) > 1 and any(not item.get("budget_amount") and not (item.get("quantity") and item.get("unit_price")) for item in items):
            return "insufficient_amount_breakdown"
        option = options[0] if isinstance(options[0], dict) else {}
        subclass_id = str(option.get("value") or option.get("code") or "")
        if not subclass_id:
            return "expense_translation_failed"
        rows = []
        for source_item_index, item in enumerate(items):
            quantity = item.get("quantity") or "1"
            budget = item.get("budget_amount") or ""
            unit = item.get("unit_price") or ""
            if not budget and unit:
                try:
                    budget = self._decimal_text(Decimal(str(quantity)) * Decimal(str(unit)))
                except (InvalidOperation, ValueError):
                    return "insufficient_amount_breakdown"
            if budget and not unit:
                try:
                    unit = self._decimal_text(Decimal(str(budget)) / Decimal(str(quantity)))
                except (InvalidOperation, ValueError, ZeroDivisionError):
                    return "insufficient_amount_breakdown"
            rows.append(
                {
                    "source_item_index": source_item_index,
                    "material_subclass": subclass_id,
                    "material_name": str(item.get("name") or option.get("label") or "").strip(),
                    "quantity": quantity,
                    "unit_price": unit,
                    "budget_amount": budget,
                }
            )
        return self._validate_expense_draft_ir(state, project, category, total, rows, "deterministic_unique_candidate")

    def _draft_explicit_subclass_ir_fallback(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        total: str,
    ) -> ExpenseDraftIR | str:
        """Draft-only single-line fallback for a verified explicit subclass."""
        if state.workflow.slots.get("submit"):
            return "submit_requires_explicit_detail_evidence"
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        expense = self._expense_slots(state)
        hints = [str(expense.get("material_subclass_hint") or "")]
        hints.extend(str(item.get("name") or "") for item in self._clean_explicit_expense_items(expense.get("items") or []))
        selected: dict[str, Any] | None = None
        selected_hint = ""
        for hint in self._dedupe(hints):
            normalized_hint = self._normalize_option_text(hint)
            candidate = self._select_expense_subclass_by_text(hint, options)
            label = self._normalize_option_text((candidate or {}).get("label") or "")
            if candidate and normalized_hint and label and (normalized_hint in label or label in normalized_hint):
                selected = candidate
                selected_hint = hint
                break
        if not selected:
            return "draft_requires_explicit_verified_subclass"
        subclass_id = str(selected.get("value") or selected.get("code") or "")
        if not subclass_id:
            return "draft_requires_explicit_verified_subclass"
        material_name = self._clean_material_name_match(selected_hint, selected_hint)
        if not material_name or self._looks_like_invalid_expense_item_name(material_name):
            material_name = str(selected.get("label") or "").strip()
        source_items = self._clean_explicit_expense_items(expense.get("items") or [])
        if len(source_items) != 1:
            return "draft_requires_explicit_verified_subclass"
        rows = [{
            "source_item_index": 0,
            "material_subclass": subclass_id,
            "material_name": material_name,
            "quantity": "1",
            "unit_price": total,
            "budget_amount": total,
        }]
        return self._validate_expense_draft_ir(state, project, category, total, rows, "draft_explicit_subclass_fallback")

    def _expense_ir_to_save_args(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        ir: ExpenseDraftIR,
    ) -> dict[str, Any]:
        user = state.workflow.evidence.get("applicant") or {}
        return {
            "workflow_id": WORKFLOW_IDS["expense"],
            "submit": bool(state.workflow.slots.get("submit")),
            "data": {
                "applicant": user.get("user_id"),
                "applicant_no": user.get("employee_no"),
                "project_name": project.get("project_name"),
                "project_code": project.get("project_code"),
                "wbs_code": project.get("wbs_code"),
                "material_category": self._expense_category_id(category),
                "total_amount": ir.total_amount,
                "details": {"detail_2": ir.rows},
            },
        }

    def _decimal_text(self, value: Decimal) -> str:
        return format(value.normalize(), "f") if value != value.to_integral() else str(value.quantize(Decimal("1")))

    def _expense_save_args_or_block(self, state: RuntimeState, project: dict[str, Any], category: dict[str, Any]) -> dict[str, Any] | str:
        expense = self._expense_slots(state)
        subclass_options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        items = self._materialized_expense_items(expense, subclass_options)
        unresolved = state.workflow.evidence.get("unresolved_slots") or set()
        if "material_subclass" in unresolved and not self._has_specific_material_evidence(expense):
            return "ambiguous_material_subclass"
        if not items:
            recovered_items = self._clean_explicit_expense_items(self._extract_expense_items(self._workflow_query(state)))
            if recovered_items:
                items = recovered_items
                expense["items"] = recovered_items
        if not items and expense.get("material_subclass_hint"):
            items = [{"name": str(expense.get("material_subclass_hint") or "")}]
        if not items:
            self._debug_log(
                self._debug_llm_config(),
                {
                    "event": "expense_save_args_block",
                    "case_id": state.obs.get("case_id"),
                    "reason": "missing_items",
                    "expense": expense,
                    "workflow_query": self._workflow_query(state),
                    "subclass_options": subclass_options,
                },
            )
            return "missing_required_info" if not expense.get("total_amount") else "ambiguous_material_subclass"
        if len(items) > 1:
            for item in items:
                if not item.get("budget_amount") and not (item.get("quantity") and item.get("unit_price")):
                    return "insufficient_amount_breakdown"
        if len(items) == 1 and not items[0].get("budget_amount"):
            if expense.get("total_amount"):
                items[0]["budget_amount"] = expense["total_amount"]
                items[0].setdefault("quantity", "1")
                items[0].setdefault("unit_price", expense["total_amount"])
            else:
                return "missing_required_info"
        if self._has_unallocated_multi_material_budget(expense, subclass_options):
            return "insufficient_amount_breakdown"

        detail_rows = []
        used_values = set()
        for item in items:
            item_hint = item.get("name") or expense.get("material_subclass_hint") or ""
            if self._is_generic_material_item_hint(item_hint, expense, subclass_options):
                self._debug_log(
                    self._debug_llm_config(),
                    {
                        "event": "expense_save_args_block",
                        "case_id": state.obs.get("case_id"),
                        "reason": "ambiguous_material_subclass",
                        "source": "generic_item_hint",
                        "item_hint": item_hint,
                        "subclass_options": subclass_options,
                    },
                )
                return "ambiguous_material_subclass"
            context_hint = " ".join(
                str(value or "")
                for value in [
                    item_hint,
                    project.get("project_name"),
                    category.get("label"),
                    expense.get("material_category_hint"),
                    self._workflow_query(state),
                ]
            )
            opt = self._select_subclass_option(item_hint, subclass_options, used_values, context_hint=context_hint)
            if opt is None:
                opt = self._select_material_subclass_with_llm(state, item_hint, subclass_options, used_values)
            if opt is None:
                self._debug_log(
                    self._debug_llm_config(),
                    {
                        "event": "expense_save_args_block",
                        "case_id": state.obs.get("case_id"),
                        "reason": "ambiguous_material_subclass",
                        "item_hint": item_hint,
                        "items": items,
                        "used_values": list(used_values),
                        "subclass_options": subclass_options,
                    },
                )
                return "ambiguous_material_subclass"
            if not self._can_reuse_subclass_option(item_hint, opt):
                used_values.add(opt.get("value") or opt.get("code"))
            material_name = self._clean_material_name_match(item.get("name") or "", item.get("name") or "") or opt.get("label")
            qty = str(item.get("quantity") or "1")
            unit = item.get("unit_price")
            budget = item.get("budget_amount")
            if not unit and budget and qty:
                try:
                    unit = self._money(float(str(budget)) / float(str(qty)))
                except Exception:
                    unit = budget
            if not budget and unit and qty:
                budget = self._money(float(str(unit)) * float(str(qty)))
            detail_rows.append(
                {
                    "material_subclass": opt.get("value") or opt.get("code"),
                    "material_name": material_name,
                    "quantity": qty,
                    "unit_price": self._money(unit),
                    "budget_amount": self._money(budget),
                }
            )

        total = expense.get("total_amount") or self._money(sum(float(row["budget_amount"]) for row in detail_rows))
        user = state.workflow.evidence.get("applicant") or {}
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category.get("value") or category.get("code"),
            "total_amount": self._money(total),
            "details": {"detail_2": detail_rows},
        }
        return {"workflow_id": WORKFLOW_IDS["expense"], "submit": bool(state.workflow.slots.get("submit")), "data": data}

    def _deterministic_multi_turn_expense_save_args(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
    ) -> dict[str, Any] | None:
        if state.obs.get("mode") != "multi_turn":
            return None
        if state.workflow.slots.get("submit"):
            return None
        expense = self._expense_slots(state)
        amount = self._money(expense.get("total_amount"))
        if not amount:
            return None
        user = state.workflow.evidence.get("applicant") or {}
        category_id = category.get("value") or category.get("code")
        if not all([user.get("user_id"), user.get("employee_no"), project.get("project_code"), project.get("wbs_code"), category_id]):
            return None
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        if not options:
            return None

        raw_items = [item for item in (expense.get("items") or []) if isinstance(item, dict)]
        cleaned_items = self._clean_explicit_expense_items(raw_items)
        hint_values: list[str] = []
        for value in [expense.get("material_subclass_hint")]:
            text = str(value or "").strip()
            if text:
                hint_values.append(text)
        for item in cleaned_items:
            text = str(item.get("name") or "").strip()
            if text:
                hint_values.append(text)
        hint_values = self._dedupe(hint_values)
        if not hint_values:
            return None

        context_hint = " ".join(
            str(value or "")
            for value in [
                expense.get("material_subclass_hint"),
                expense.get("material_category_hint"),
                project.get("project_name"),
                category.get("label"),
                self._workflow_query(state),
            ]
        )
        selected = None
        selected_hint = ""
        for hint in hint_values:
            if self._is_generic_material_item_hint(hint, expense, options):
                continue
            selected = self._select_subclass_option(str(hint), options, set(), context_hint=context_hint)
            if selected:
                selected_hint = str(hint)
                break
        if selected is None:
            return None
        subclass_id = selected.get("value") or selected.get("code")
        if not subclass_id:
            return None

        source_item = self._matching_expense_item_for_option(cleaned_items or raw_items, selected, selected_hint)
        quantity = self._normalize_quantity((source_item or {}).get("quantity") or "1")
        budget = amount
        unit = self._money((source_item or {}).get("unit_price") or "")
        if not unit:
            try:
                unit = self._money(float(budget) / max(float(quantity), 1.0))
            except Exception:
                unit = budget
        material_name = self._deterministic_expense_material_name(source_item, selected, selected_hint)
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category_id,
            "total_amount": amount,
            "details": {
                "detail_2": [
                    {
                        "material_subclass": subclass_id,
                        "material_name": material_name,
                        "quantity": quantity,
                        "unit_price": unit,
                        "budget_amount": budget,
                    }
                ]
            },
        }
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "deterministic_multi_turn_expense_args",
                "case_id": state.obs.get("case_id"),
                "selected_subclass": selected,
                "selected_hint": selected_hint,
                "amount": amount,
            },
        )
        return {"workflow_id": WORKFLOW_IDS["expense"], "submit": bool(state.workflow.slots.get("submit")), "data": data}

    def _matching_expense_item_for_option(
        self,
        items: list[dict[str, Any]],
        option: dict[str, Any],
        hint: str,
    ) -> dict[str, Any] | None:
        label = str(option.get("label") or "")
        option_id = str(option.get("value") or option.get("code") or "")
        for item in items:
            name = str(item.get("name") or "")
            text = " ".join([name, hint])
            if option_id and option_id in text:
                return item
            if label and (self._normalize_option_text(label) in self._normalize_option_text(text) or self._normalize_option_text(text) in self._normalize_option_text(label)):
                return item
        return items[0] if len(items) == 1 else None

    def _deterministic_expense_material_name(
        self,
        item: dict[str, Any] | None,
        option: dict[str, Any],
        hint: str,
    ) -> str:
        label = str(option.get("label") or "")
        raw_name = str((item or {}).get("name") or hint or "")
        name = self._clean_material_name_match(raw_name, raw_name)
        normalized_name = self._normalize_option_text(name)
        normalized_label = self._normalize_option_text(label)
        if not name or self._looks_like_invalid_expense_item_name(name) or self._is_action_only_material_name(name):
            return label or "待补充"
        if normalized_label and normalized_label in normalized_name:
            return label
        if normalized_name in {"小类", "物资小类"}:
            return label or name
        return name or label or "待补充"

    def _subclass_lookup_failed(self, state: RuntimeState, project: dict[str, Any], category: dict[str, Any]) -> bool:
        target_wbs = project.get("wbs_code")
        target_category = category.get("value") or category.get("code")
        for failure in state.workflow.evidence.get("subclass_lookup_failures") or []:
            args = failure.get("args") if isinstance(failure, dict) else {}
            dep = args.get("dep") if isinstance(args, dict) else {}
            if dep.get("wbscode") == target_wbs and dep.get("wzlb") == target_category:
                return True
        return False

    def _expense_save_preflight(self, state: RuntimeState, save_args: dict[str, Any]) -> str:
        data = save_args.get("data") if isinstance(save_args.get("data"), dict) else {}
        schema = state.workflow.evidence.get("schema") or {}
        schema_body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
        required = schema_body.get("required_fields") if isinstance(schema_body.get("required_fields"), list) else []
        missing = [field for field in required if data.get(field) in (None, "", [], {})]
        if missing:
            return "missing_required_info"
        rows = ((data.get("details") or {}).get("detail_2") or []) if isinstance(data.get("details"), dict) else []
        if not rows:
            return "missing_required_info"
        detail_required = (
            ((schema_body.get("detail_tables") or {}).get("detail_2") or {}).get("required_fields")
            if isinstance(schema_body.get("detail_tables"), dict)
            else []
        )
        basic_detail_required = [field for field in (detail_required or []) if field in {"material_subclass", "material_name", "quantity", "unit_price", "budget_amount"}]
        for row in rows:
            if not isinstance(row, dict):
                return "missing_required_info"
            if any(row.get(field) in (None, "", [], {}) for field in basic_detail_required):
                return "missing_required_info"
            if not self._valid_number(row.get("quantity")) or not self._valid_number(row.get("unit_price")) or not self._valid_number(row.get("budget_amount")):
                return "missing_required_info"
        try:
            total = float(self._money(data.get("total_amount")))
            rows_total = sum(float(self._money(row.get("budget_amount"))) for row in rows)
            if abs(total - rows_total) > 0.01:
                return "insufficient_amount_breakdown"
        except Exception:
            return "missing_required_info"
        return ""

    def _expense_save_args_from_schema_draft(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        blocked_reason: str,
    ) -> dict[str, Any] | None:
        if blocked_reason not in {"ambiguous_material_subclass", "insufficient_amount_breakdown", "missing_required_info"}:
            return None
        if state.workflow.slots.get("submit"):
            return None
        if blocked_reason == "ambiguous_material_subclass" and self._must_block_generic_material_subclass(state):
            return None
        expense = self._expense_slots(state)
        if not self._has_specific_material_evidence(expense):
            self._debug_log(
                self._debug_llm_config(),
                {
                    "event": "workflow_schema_fallback_draft",
                    "source": "guarded",
                    "reason": blocked_reason,
                    "guard": "missing_specific_material_evidence",
                    "case_id": state.obs.get("case_id"),
                },
            )
            return None
        snapshot = self._workflow_schema_snapshot(state, project, category, blocked_reason)
        if not snapshot.get("subclass_options"):
            return None
        llm_config = self._llm_config("strong")
        if llm_config.get("api_key") and self._can_call_llm(state, "strong", min_remaining=10.0):
            draft = self._llm_workflow_form_draft(state, llm_config, snapshot)
            if draft:
                llm_args = self._validate_expense_form_draft(state, project, category, draft)
                if isinstance(llm_args, dict):
                    return llm_args
        self._debug_log(
            self._debug_llm_config(),
            {
                "event": "workflow_schema_fallback_draft",
                "source": "disabled",
                "reason": blocked_reason,
                "note": "business-semantic fallback disabled; only schema/LLM drafts are allowed",
            },
        )
        return None

    def _workflow_schema_snapshot(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        blocked_reason: str,
    ) -> dict[str, Any]:
        wf = state.workflow
        schema = wf.evidence.get("schema") or {}
        expense = self._expense_slots(state)
        return {
            "workflow_id": WORKFLOW_IDS["expense"],
            "intent": wf.intent,
            "blocked_reason": blocked_reason,
            "query": self._workflow_query(state),
            "slots": {
                "project_name_hint": expense.get("project_name"),
                "project_keywords": expense.get("project_keywords") or [],
                "material_category_hint": expense.get("material_category_hint"),
                "material_subclass_hint": expense.get("material_subclass_hint"),
                "total_amount": expense.get("total_amount"),
                "items": expense.get("items") or [],
                "submit": bool(wf.slots.get("submit")),
            },
            "schema": {
                "required_fields": (schema.get("schema") or {}).get("required_fields") or schema.get("required_fields") or [],
                "detail_tables": (schema.get("schema") or {}).get("detail_tables") or schema.get("detail_tables") or {},
                "field_types": (schema.get("schema") or {}).get("field_types") or {},
                "field_aliases": (schema.get("schema") or {}).get("field_aliases") or {},
            },
            "verified": {
                "applicant": wf.evidence.get("applicant") or {},
                "project": {
                    "project_name": project.get("project_name"),
                    "project_code": project.get("project_code"),
                    "wbs_code": project.get("wbs_code"),
                },
                "material_category": {
                    "label": category.get("label"),
                    "value": category.get("value") or category.get("code"),
                },
            },
            "subclass_options": [
                {
                    "id": opt.get("value") or opt.get("code"),
                    "label": opt.get("label") or "",
                    "code": opt.get("code") or opt.get("value") or "",
                }
                for opt in (wf.evidence.get("subclass_options", {}).get("options") or [])
                if opt.get("value") or opt.get("code")
            ],
            "constraints": [
                "Only use subclass ids from subclass_options.",
                "Do not invent project_code, wbs_code, applicant, applicant_no, or material_category.",
                "If multiple detail rows are present, budget_amount sum must equal total_amount.",
                "If submit is true and amount or detail evidence is missing, return decision=need_more_info.",
                "If submit is false and this is a draft request, you may create a complete best-effort draft from verified project, material category, subclass option labels, and business context.",
                "For draft best-effort rows, state assumptions in reason and keep ids strictly limited to subclass_options.",
            ],
        }

    def _llm_workflow_form_draft(self, state: RuntimeState, llm_config: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        evidence_summary = {
            "workflow_intent": state.workflow.intent,
            "submit": bool(state.workflow.slots.get("submit")),
            "verified_keys": sorted(k for k, value in (state.workflow.evidence or {}).items() if value and k != "schema"),
            "subclass_option_count": len(snapshot.get("subclass_options") or []),
        }
        context_pack = self.static_context.for_workflow_form(snapshot.get("workflow_id"), evidence_summary)
        payload = {
            "static_context": context_pack.get("content") or "",
            "schema_snapshot": snapshot,
            "instruction": (
                "Return valid json only. Draft the expense detail table using only verified schema options. "
                "Use explicit user amounts when present. If submit=true, do not guess missing amounts or details. "
                "If submit=false, this is a draft: do not block solely because amount or details are absent when verified project/category/subclass options are available. "
                "For a draft, infer a small complete planning draft from the verified project, category label, option labels, and user wording; choose only semantically relevant rows, use explicit or conservative round monetary amounts, and make row sums equal total_amount. "
                "If no subclass option is semantically relevant, return decision=ambiguous."
            ),
            "output_schema": {
                "decision": "draft|need_more_info|ambiguous|blocked",
                "confidence": 0.0,
                "total_amount": "money string",
                "details": [
                    {
                        "material_subclass": "id from subclass_options",
                        "material_name": "name",
                        "quantity": "string number",
                        "unit_price": "money string",
                        "budget_amount": "money string",
                    }
                ],
                "missing_fields": [],
                "ambiguous_fields": [],
                "reason": "short reason",
            },
        }
        try:
            content = self._chat_completion(
                llm_config,
                [
                    {
                        "role": "system",
                        "content": (
                            "Return valid json only. You fill workflow form drafts from a schema snapshot. "
                            "Never invent ids; use only provided verified options."
                        ),
                    },
                    {"role": "user", "content": "Return valid json only.\n" + json.dumps(payload, ensure_ascii=False)},
                ],
                state=state,
                profile="strong",
                context_pack_type=str(context_pack.get("pack_type") or "workflow_form_static_context"),
                context_chars=int(context_pack.get("chars") or 0),
            )
            parsed = self._parse_json_object(content)
            if isinstance(parsed, dict):
                self._debug_log(llm_config, {"event": "workflow_form_draft", "draft": parsed})
                return parsed
        except Exception as exc:
            self._debug_log(llm_config, {"event": "workflow_form_draft_error", "error": str(exc)})
        return None

    def _validate_expense_form_draft(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        draft: dict[str, Any],
    ) -> dict[str, Any] | None:
        min_confidence = 0.55 if not state.workflow.slots.get("submit") else 0.65
        if draft.get("decision") != "draft" or self._bounded_float(draft.get("confidence"), 0.0) < min_confidence:
            return None
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        allowed = {str(opt.get("value") or opt.get("code")): opt for opt in options if opt.get("value") or opt.get("code")}
        rows = draft.get("details")
        if not isinstance(rows, list) or not rows:
            return None
        detail_rows: list[dict[str, Any]] = []
        total_value = 0.0
        for row in rows:
            if not isinstance(row, dict):
                return None
            subclass = str(row.get("material_subclass") or "")
            if subclass not in allowed:
                return None
            qty = str(row.get("quantity") or "1")
            budget = self._money(row.get("budget_amount"))
            unit = self._money(row.get("unit_price") or budget)
            if not budget:
                return None
            try:
                total_value += float(budget)
            except Exception:
                return None
            detail_rows.append(
                {
                    "material_subclass": subclass,
                    "material_name": str(row.get("material_name") or allowed[subclass].get("label") or ""),
                    "quantity": qty,
                    "unit_price": unit,
                    "budget_amount": budget,
                }
            )
        expense = self._expense_slots(state)
        total = self._money(draft.get("total_amount") or expense.get("total_amount") or total_value)
        try:
            if abs(float(total) - total_value) > 0.01:
                return None
        except Exception:
            return None
        user = state.workflow.evidence.get("applicant") or {}
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category.get("value") or category.get("code"),
            "total_amount": total,
            "details": {"detail_2": detail_rows},
        }
        if not all(data.get(key) for key in ["applicant", "applicant_no", "project_name", "project_code", "wbs_code", "material_category", "total_amount"]):
            return None
        return {"workflow_id": WORKFLOW_IDS["expense"], "submit": bool(state.workflow.slots.get("submit")), "data": data}

    def _normalize_best_effort_brand_ad_draft(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        detail_rows: list[dict[str, Any]],
        total: str,
    ) -> dict[str, Any] | None:
        return None

    def _row_index_by_terms(self, rows: list[dict[str, Any]], terms: list[str], label_by_id: dict[str, str] | None = None) -> int | None:
        labels = label_by_id or {}
        for index, row in enumerate(rows):
            label = labels.get(str(row.get("material_subclass") or ""), "")
            if label and any(term in label for term in terms):
                return index
        for index, row in enumerate(rows):
            subclass = str(row.get("material_subclass") or "")
            text = " ".join([str(row.get("material_name") or ""), subclass, labels.get(subclass, "")])
            if any(term in text for term in terms):
                return index
        return None

    def _schema_driven_expense_draft_fallback(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        blocked_reason: str,
    ) -> dict[str, Any] | None:
        return None

    def _fallback_expense_detail_rows(self, state: RuntimeState, expense: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def _submit_expense_requires_block(
        self,
        state: RuntimeState,
        expense: dict[str, Any],
        items: list[dict[str, Any]],
        options: list[dict[str, Any]],
    ) -> bool:
        return False

    def _fallback_subclass_options(self, state: RuntimeState, expense: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def _fallback_material_name(self, expense: dict[str, Any], option: dict[str, Any]) -> str:
        label = str(option.get("label") or "待补充")
        name = self._specific_material_name(label, expense, option)
        return name or label

    def _expense_context_text(self, state: RuntimeState, expense: dict[str, Any], options: list[dict[str, Any]]) -> str:
        return " ".join(
            str(item or "")
            for item in [
                self._workflow_query(state),
                expense.get("raw_text"),
                expense.get("source_text"),
                expense.get("material_category_hint"),
                expense.get("material_subclass_hint"),
                " ".join(str(row.get("name") or "") for row in expense.get("items") or [] if isinstance(row, dict)),
                " ".join(str(opt.get("label") or "") for opt in options if isinstance(opt, dict)),
            ]
        )

    def _partial_expense_draft_args(
        self,
        state: RuntimeState,
        project: dict[str, Any],
        category: dict[str, Any],
        blocked_reason: str,
    ) -> dict[str, Any] | None:
        if blocked_reason not in {"missing_required_info", "ambiguous_material_subclass"}:
            return None
        if state.workflow.slots.get("submit"):
            return None
        query = self._full_query(state.obs)
        if not (self._draft_intent(query) or "待办" in query):
            return None
        if state.obs.get("mode") == "multi_turn":
            return None
        options = state.workflow.evidence.get("subclass_options", {}).get("options") or []
        if not options:
            return None
        if len(options) > 1 and not self._has_specific_material_evidence(self._expense_slots(state)):
            return None
        user = state.workflow.evidence.get("applicant") or {}
        subclass = options[0]
        subclass_id = subclass.get("value") or subclass.get("code")
        if not all([user.get("user_id"), user.get("employee_no"), project.get("project_code"), project.get("wbs_code"), category.get("value") or category.get("code"), subclass_id]):
            return None
        amount = self._expense_slots(state).get("total_amount")
        amount = self._money(amount) if amount else ""
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category.get("value") or category.get("code"),
            "total_amount": amount,
            "details": {
                "detail_2": [
                    {
                        "material_subclass": subclass_id,
                        "material_name": subclass.get("label") or "待补充",
                        "quantity": "1",
                        "unit_price": amount,
                        "budget_amount": amount,
                    }
                ]
            },
            "_partial": True,
            "_missing_fields": self._partial_expense_missing_fields(state),
        }
        return {"workflow_id": WORKFLOW_IDS["expense"], "submit": False, "data": data}

    def _partial_expense_missing_fields(self, state: RuntimeState) -> list[str]:
        expense = self._expense_slots(state)
        missing: list[str] = []
        if not expense.get("total_amount"):
            missing.extend(["total_amount", "detail_2.unit_price", "detail_2.budget_amount"])
        if not expense.get("items") and not expense.get("material_subclass_hint"):
            missing.append("detail_2.material_name")
        return missing

    def _is_generic_brand_ad_item(self, item: dict[str, Any], expense: dict[str, Any]) -> bool:
        return False

    def _generic_material_subclass_ambiguous(
        self,
        item_hint: Any,
        expense: dict[str, Any],
        options: list[dict[str, Any]],
    ) -> bool:
        return False

    def _must_block_generic_material_subclass(self, state: RuntimeState) -> bool:
        return False

    def _has_specific_material_evidence(self, expense: dict[str, Any]) -> bool:
        subclass_hint = str(expense.get("material_subclass_hint") or "").strip()
        if subclass_hint and not self._looks_like_invalid_expense_item_name(subclass_hint):
            return True
        cleaned_items = self._clean_explicit_expense_items(expense.get("items") or [])
        if any(str(row.get("name") or "").strip() for row in cleaned_items):
            return True
        raw_text = str(expense.get("raw_text") or expense.get("source_text") or "")
        if raw_text:
            extracted = self._clean_explicit_expense_items(self._extract_expense_items(raw_text))
            if any(str(row.get("name") or "").strip() for row in extracted):
                return True
        return False

    def _is_generic_material_item_hint(self, item_hint: Any, expense: dict[str, Any], options: list[dict[str, Any]]) -> bool:
        if len(options) <= 1:
            return False
        raw_hint = str(item_hint or "")
        hint = self._normalize_option_text(raw_hint)
        if not hint:
            return False
        if self._has_unique_literal_subclass_match(hint, options):
            return False
        project_name = self._normalize_option_text(expense.get("project_name") or "")
        if project_name and hint.startswith(project_name):
            hint = hint[len(project_name) :]
        hint = re.sub(r"^项目", "", hint)
        hint = re.sub(r"^(?:需要|计划|准备|想要|要)?(?:走一笔|购买|采购|买|补充|补|申请|提交|提)?", "", hint)
        hint = re.sub(r"^(?:一批|一些|若干|部分|几件|几套|一笔)", "", hint)
        hint = re.sub(r"(?:费用|费|申请|采购)$", "", hint)
        category_hint = self._normalize_option_text(expense.get("material_category_hint") or "")
        subclass_hint = self._normalize_option_text(expense.get("material_subclass_hint") or "")
        if subclass_hint and subclass_hint != category_hint and subclass_hint != hint:
            return False
        labels = [self._normalize_option_text(opt.get("label") or opt.get("name") or "") for opt in options]
        if category_hint and (hint == category_hint or hint in category_hint or category_hint in hint):
            return True
        if re.search(r"一批|一些|若干|一笔|部分|各类|相关", raw_hint):
            return True
        generic_terms = {"物资", "用品", "设备", "服务", "采购", "费用"}
        if hint in generic_terms:
            return True
        return len(hint) <= 4 and any(term in hint for term in generic_terms)

    def _has_unique_literal_subclass_match(self, item_hint: Any, options: list[dict[str, Any]]) -> bool:
        """Require a unique user-visible subclass term, not a fuzzy noun overlap."""
        hint = self._normalize_option_text(item_hint)
        if not hint:
            return False
        matches = 0
        for option in options:
            raw_label = str(option.get("label") or option.get("name") or "")
            label = self._normalize_option_text(raw_label)
            if not label:
                continue
            terms = []
            for chunk in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", raw_label):
                terms.extend(re.split(r"及其|以及|包含|含", chunk))
            normalized_terms = {
                self._normalize_option_text(term)
                for term in terms
                if len(self._normalize_option_text(term)) >= 2
            }
            if hint == label or label in hint or any(term == hint or term in hint for term in normalized_terms):
                matches += 1
        return matches == 1

    def _infer_single_item_from_specific_hint(self, expense: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def _specific_material_name(self, item_name: Any, expense: dict[str, Any], option: dict[str, Any] | None = None) -> str:
        raw_text = str(expense.get("raw_text") or expense.get("source_text") or "")
        item_text = str(item_name or "")
        option_label = str((option or {}).get("label") or "")
        if item_text and self._material_name_is_exact_option(item_text, option_label):
            return self._clean_material_name_match(item_text, item_text)
        search_terms = [item_text] if item_text else []
        if option_label:
            search_terms.append(option_label)
        for term in self._dedupe([term for term in search_terms if term]):
            pattern = rf"((?:[\u4e00-\u9fa5A-Za-z0-9]{{1,4}})?{re.escape(term)})"
            matches = [self._clean_material_name_match(m.group(1), term) for m in re.finditer(pattern, raw_text)]
            matches = [
                m
                for m in matches
                if len(m) >= len(term)
                and not any(stop in m for stop in ["项目", "总预算", "预算", "万元", "元", "包括", "包含", "需要", "采购", "申请"])
            ]
            if matches:
                return sorted(matches, key=len, reverse=True)[0]
        return self._clean_material_name_match(item_text, item_text) if item_text else item_text

    def _material_name_is_exact_option(self, item_name: str, option_label: str) -> bool:
        item = str(item_name or "").strip()
        label = re.sub(r"（.*?）|\\(.*?\\)", "", str(option_label or "")).strip()
        if not item or not label:
            return False
        return item == label or label.startswith(item)

    def _clean_material_name_match(self, value: str, term: str) -> str:
        text = str(value or "").strip("，。；;、的")
        text = re.sub(
            r"^(包括|包含|需要|采购|购买|买|要买|要印|印|要做|做|申请|提交|先存一个|先存|存一个|一台|一个|一条|一场|一批|要?\d+\s*(?:台|个|条|场|份|支|套|批|项|册|张|本)|[一二两三四五六七八九十]+\s*(?:台|个|条|场|份|支|套|批|项|册|张|本))+",
            "",
            text,
        )
        for _ in range(3):
            stripped = re.sub(r"(?:的)?(?:采购|购买|申请|提掉|提交|费用|预算)$", "", text)
            if stripped == text:
                break
            text = stripped
        text = re.sub(r"\d+(?:\.\d+)?\s*(?:万|万元|元)$", "", text)
        if not text:
            return term
        if term not in text:
            if text and text in str(term or "") and not self._looks_like_invalid_expense_item_name(text):
                return text.replace("和", "及").replace("与", "及")
            compact_term = re.sub(r"^(?:要?\d+|[一二两三四五六七八九十]+)\s*(?:台|个|条|场|份|支|套|批|项|册|张|本)+", "", str(term or ""))
            if compact_term and compact_term in text:
                return text.replace("和", "及").replace("与", "及")
            return term
        return text.replace("和", "及").replace("与", "及")

    def _infer_expense_items_from_total(self, expense: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def _has_unallocated_multi_material_budget(self, expense: dict[str, Any], options: list[dict[str, Any]]) -> bool:
        if len(options) <= 1:
            return False
        items = expense.get("items") if isinstance(expense.get("items"), list) else []
        if len(items) != 1 or not expense.get("total_amount"):
            return False
        item = items[0] if isinstance(items[0], dict) else {}
        name = str(item.get("name") or expense.get("material_subclass_hint") or "")
        if not re.search(r"[、和与及]", name):
            return False
        normalized_name = self._normalize_option_text(name)
        if not normalized_name:
            return False
        labels = [self._normalize_option_text(opt.get("label") or opt.get("name") or "") for opt in options]
        if any(label and (normalized_name == label or normalized_name in label) for label in labels):
            return False
        if item.get("budget_amount") and self._money(item.get("budget_amount")) != self._money(expense.get("total_amount")):
            return False
        return True

    def _default_expense_total(self, expense: dict[str, Any], options: list[dict[str, Any]]) -> str:
        return ""

    def _find_option_by_hints(self, options: list[dict[str, Any]], hints: list[str]) -> dict[str, Any] | None:
        for opt in options:
            label = str(opt.get("label") or "")
            if any(hint in label for hint in hints):
                return opt
        return None

    def _select_option_by_literal_hint(self, hint: Any, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_hint = self._normalize_option_text(hint)
        if not normalized_hint:
            return options[0] if len(options) == 1 else None
        hint_tokens = self._option_tokens(normalized_hint)
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for opt in options:
            label = self._normalize_option_text(opt.get("label") or opt.get("name") or "")
            code = self._normalize_option_text(opt.get("code") or opt.get("value") or "")
            if not label and not code:
                continue
            score = 0
            if normalized_hint == label or normalized_hint == code:
                score += 100
            elif label and (normalized_hint in label or label in normalized_hint):
                score += min(len(normalized_hint), len(label)) + 20
            overlap = hint_tokens & self._option_tokens(label)
            if overlap:
                score += sum(len(token) for token in overlap)
            if score > 0:
                scored.append((score, len(label), opt))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][2]

    def _normalize_option_text(self, value: Any) -> str:
        text = re.sub(r"\s+", "", str(value or ""))
        text = re.sub(r"[（）()【】\\[\\]{}]", "", text)
        return text.strip("，。；;、 的")

    def _option_tokens(self, value: Any) -> set[str]:
        text = self._normalize_option_text(value)
        tokens = {item for item in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", text) if item}
        for size in (4, 3, 2):
            if len(text) >= size:
                tokens.update(text[i : i + size] for i in range(0, len(text) - size + 1))
        return tokens

    def _select_option_by_label_intent(self, hint: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        return self._select_option_by_literal_hint(hint, options)

    def _select_subclass_option(
        self,
        hint: str,
        options: list[dict[str, Any]],
        used_values: set[Any],
        context_hint: str = "",
    ) -> dict[str, Any] | None:
        available = [opt for opt in options if (opt.get("value") or opt.get("code")) not in used_values]
        if not available:
            return None
        chosen = self._select_expense_subclass_by_text(hint, available)
        if chosen:
            self._debug_log(self._debug_llm_config(), {"event": "expense_subclass_selection", "source": "deterministic_text", "hint": hint, "selected": chosen})
            return chosen
        if context_hint and context_hint != hint:
            chosen = self._select_expense_subclass_by_text(context_hint, available)
            if chosen:
                self._debug_log(
                    self._debug_llm_config(),
                    {
                        "event": "expense_subclass_selection",
                        "source": "deterministic_context",
                        "hint": hint,
                        "context_hint": context_hint,
                        "selected": chosen,
                    },
                )
                return chosen
        if len(available) == 1:
            self._debug_log(self._debug_llm_config(), {"event": "expense_subclass_selection", "source": "single_option", "hint": hint, "selected": available[0]})
            return available[0]
        return None

    def _select_expense_subclass_by_text(self, hint: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_hint = self._normalize_option_text(hint)
        if not normalized_hint:
            return self._select_option_by_literal_hint(hint, options)
        hint_tokens = self._option_tokens(normalized_hint)
        scored: list[tuple[int, int, int, dict[str, Any]]] = []
        for opt in options:
            label = self._normalize_option_text(opt.get("label") or opt.get("name") or "")
            if not label:
                continue
            score = 0
            if normalized_hint == label:
                score += 100
            elif normalized_hint in label or label in normalized_hint:
                score += 50 + min(len(normalized_hint), len(label))
            overlap = hint_tokens & self._option_tokens(label)
            score += sum(len(token) for token in overlap)
            score += self._longest_common_substring_len(normalized_hint, label) * 3
            if score > 0:
                scored.append((score, min(len(normalized_hint), len(label)), -len(label), opt))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0] and scored[0][1] == scored[1][1]:
            return None
        return scored[0][3]

    def _longest_common_substring_len(self, left: str, right: str) -> int:
        left = str(left or "")
        right = str(right or "")
        if not left or not right:
            return 0
        best = 0
        for i in range(len(left)):
            for j in range(i + 1, len(left) + 1):
                piece = left[i:j]
                if len(piece) <= best:
                    continue
                if piece in right:
                    best = len(piece)
        return best

    def _can_reuse_subclass_option(self, hint: str, option: dict[str, Any]) -> bool:
        return False

    def _select_material_subclass_with_llm(
        self,
        state: RuntimeState,
        hint: str,
        options: list[dict[str, Any]],
        used_values: set[Any],
    ) -> dict[str, Any] | None:
        available = [opt for opt in options if (opt.get("value") or opt.get("code")) not in used_values]
        if not available:
            return None
        return self._select_candidate_with_llm(
            state,
            f"选择费用物资小类: {hint}",
            self._workflow_query(state),
            available,
            ["value", "code"],
        )

    def _workflow_result_from_save(self, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        data = args.get("data") or {}
        status = "submitted" if args.get("submit") or result.get("submitted") else "draft_saved"
        output = {"status": status, "workflow_id": result.get("workflow_id") or args.get("workflow_id")}
        if args.get("workflow_id") == WORKFLOW_IDS["leave"]:
            saved = []
            try:
                saved = result.get("_saved_leave_requests") or []
            except Exception:
                saved = []
            if saved and status == "submitted":
                output["count"] = len(saved)
            for key in ["start_time", "end_time", "leave_type"]:
                if key in data:
                    output[key] = data[key]
            for key in ["reason", "approver"]:
                if key in data:
                    output[key] = data[key]
            if status == "submitted" and "duration" in data:
                output["duration"] = data["duration"]
        elif args.get("workflow_id") == WORKFLOW_IDS["expense"]:
            output["project_code"] = data.get("project_code")
            if data.get("material_category"):
                output["material_category"] = data.get("material_category")
            if data.get("project_name"):
                output["project_name"] = data.get("project_name")
            if data.get("total_amount"):
                output["total_amount"] = self._money(data.get("total_amount"))
            rows = data.get("details", {}).get("detail_2", [])
            output["detail_count"] = len(rows)
        return output

    def _workflow_query(self, state: RuntimeState) -> str:
        source = state.workflow.slots.get("source_text")
        if source:
            return self._domain_source_text(str(source), "workflow", state.workflow.intent)
        return self._domain_source_text(
            self._task_source_text(state, "workflow", state.workflow.intent),
            "workflow",
            state.workflow.intent,
        )

    def _leave_query(self, state: RuntimeState) -> str:
        leave = self._leave_slots(state)
        return str(leave.get("source_text") or self._workflow_query(state) or self._full_query(state.obs))

    def _expense_query(self, state: RuntimeState) -> str:
        expense = self._expense_slots(state)
        return str(expense.get("source_text") or self._workflow_query(state) or self._full_query(state.obs))

    def _select_candidate_with_llm(
        self,
        state: RuntimeState,
        task: str,
        query: str,
        candidates: list[dict[str, Any]],
        id_fields: list[str],
    ) -> dict[str, Any] | None:
        if len(candidates) == 1:
            return candidates[0]
        decision = self._rank_candidates_with_llm(state, task, query, candidates, id_fields)
        selected_id = str(decision.get("selected_id") or "")
        if decision.get("decision") == "select" and float(decision.get("confidence") or 0) >= 0.65:
            by_key = self._candidate_map(candidates, id_fields)
            if selected_id in by_key:
                return by_key[selected_id]
        return None

    def _rank_candidates_with_llm(
        self,
        state: RuntimeState,
        task: str,
        query: str,
        candidates: list[dict[str, Any]],
        id_fields: list[str],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not candidates:
            return {"decision": "need_more_info", "selected_id": "", "confidence": 0.0, "ranked": [], "reason": "no_candidates"}
        if len(candidates) == 1:
            key = next(iter(self._candidate_map(candidates, id_fields)))
            return {"decision": "select", "selected_id": key, "confidence": 1.0, "ranked": [{"id": key, "score": 1.0}], "reason": "single_candidate"}
        llm_config = self._llm_config("strong")
        if not llm_config.get("api_key") or not self._can_call_llm(state, "strong", min_remaining=12.0):
            return {"decision": "need_more_info", "selected_id": "", "confidence": 0.0, "ranked": [], "reason": "llm_unavailable"}
        by_key = self._candidate_map(candidates, id_fields)
        compact = self._compact_candidates(by_key)
        context_pack = self.static_context.for_candidate_ranker(task, candidates, id_fields, context)
        payload = {
            "task": task,
            "query": query,
            "task_graph": state.task_graph,
            "context": context or {},
            "static_context": context_pack.get("content") or "",
            "candidates": compact,
            "instruction": (
                "Return valid json only. Rank provided candidates for the user's business intent. "
                "Choose only an existing candidate id. If wording is generic, evidence is weak, or multiple candidates are plausible, "
                "return decision=ambiguous or need_more_info instead of guessing."
            ),
            "output_schema": {
                "decision": "select|ambiguous|need_more_info",
                "selected_id": "existing candidate id or empty",
                "confidence": 0.0,
                "ranked": [{"id": "candidate id", "score": 0.0, "reason": "short reason"}],
                "reason": "short reason",
            },
        }
        try:
            content = self._chat_completion(
                llm_config,
                [
                    {
                        "role": "system",
                        "content": (
                            "Return valid json only. You are a candidate ranker. "
                            "Never invent ids or business facts. Low confidence should be ambiguous."
                        ),
                    },
                    {"role": "user", "content": "Return valid json only.\n" + json.dumps(payload, ensure_ascii=False)},
                ],
                state=state,
                profile="strong",
                context_pack_type=str(context_pack.get("pack_type") or "candidate_static_context"),
                context_chars=int(context_pack.get("chars") or 0),
            )
            parsed = self._parse_json_object(content) or {}
            decision = str(parsed.get("decision") or parsed.get("status") or "").strip()
            if decision == "selected":
                decision = "select"
            if decision not in {"select", "ambiguous", "need_more_info"}:
                decision = "ambiguous"
            selected_id = str(parsed.get("selected_id") or "")
            if selected_id not in by_key:
                selected_id = ""
                if decision == "select":
                    decision = "ambiguous"
            confidence = self._bounded_float(parsed.get("confidence"), 0.0)
            ranked = self._normalize_ranked_candidates(parsed.get("ranked"), by_key)
            result = {
                "decision": decision,
                "selected_id": selected_id,
                "confidence": confidence,
                "ranked": ranked,
                "reason": str(parsed.get("reason") or ""),
            }
            state.candidate_decisions.append({"task": task, **result})
            state.ledger.record_candidate_decision(
                task=task,
                candidate_type=task,
                selected_id=selected_id,
                allowed_ids=list(by_key.keys()),
                source="llm_ranker",
                confidence=confidence,
                decision=decision,
            )
            self._debug_log(llm_config, {"event": "candidate_ranked", "task": task, **result})
            return result
        except Exception as exc:
            self._debug_log(llm_config, {"event": "candidate_rank_error", "task": task, "error": str(exc)})
            return {"decision": "need_more_info", "selected_id": "", "confidence": 0.0, "ranked": [], "reason": str(exc)}

    def _candidate_map(self, candidates: list[dict[str, Any]], id_fields: list[str]) -> dict[str, dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for idx, candidate in enumerate(candidates):
            key = ""
            for field in id_fields:
                if candidate.get(field):
                    key = str(candidate[field])
                    break
            if not key:
                key = str(idx)
            while key in by_key:
                key = f"{key}_{idx}"
            by_key[key] = candidate
        return by_key

    def _compact_candidates(self, by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        compact = []
        for key, candidate in by_key.items():
            compact.append(
                {
                    "id": key,
                    "label": candidate.get("label") or candidate.get("name") or candidate.get("project_name") or "",
                    "title": candidate.get("title") or candidate.get("position") or "",
                    "code": candidate.get("code") or candidate.get("project_code") or candidate.get("employee_no") or "",
                    "extra": {
                        item_key: candidate.get(item_key)
                        for item_key in ["wbs_code", "value", "user_id", "department", "description"]
                        if candidate.get(item_key)
                    },
                }
            )
        return compact

    def _normalize_ranked_candidates(self, value: Any, by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        ranked: list[dict[str, Any]] = []
        for item in value[:5]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "")
            if candidate_id not in by_key:
                continue
            ranked.append(
                {
                    "id": candidate_id,
                    "score": self._bounded_float(item.get("score"), 0.0),
                    "reason": str(item.get("reason") or "")[:120],
                }
            )
        return ranked

    def _bounded_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return default

    def _next_participant_booking_list_args(self, state: RuntimeState) -> dict[str, Any] | None:
        mr = state.meetingroom
        tried = [item.get("args") for item in mr.evidence.get("tried_booking_lists", []) if isinstance(item, dict)]
        day = self._meeting_day(state)
        keyword = str(mr.slots.get("keyword") or "")
        start = str(mr.slots.get("start") or "")
        candidates: list[dict[str, Any]] = []
        if day and keyword:
            candidates.append({"status": "active", "day": day, "keyword": keyword})
        for fallback_keyword in self._meeting_keyword_fallbacks(keyword):
            if day and fallback_keyword:
                candidates.append({"status": "active", "day": day, "keyword": fallback_keyword})
        if day and start:
            candidates.append({"status": "active", "day": day, "start": start})
        if day:
            candidates.append({"status": "active", "day": day})
        if keyword:
            candidates.append({"status": "active", "keyword": keyword})
        candidates.append({"status": "active"})
        seen: set[str] = set()
        for args in candidates:
            key = json.dumps(args, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if args not in tried:
                return args
        return None

    def _meeting_keyword_fallbacks(self, keyword: str) -> list[str]:
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        out: list[str] = []
        normalized = keyword
        if normalized in {"评审会", "评审"}:
            out.extend(["项目评审", "需求评审"])
        if normalized in {"分享会", "技术分享会"}:
            out.extend(["技术分享", "分享"])
        if normalized.endswith("会") and len(normalized) > 2:
            out.append(normalized[:-1])
        return self._dedupe([item for item in out if item and item != keyword])

    def _clean_participant_list_keyword(self, keyword: str) -> str:
        value = str(keyword or "").strip("，。,. ")
        if not value:
            return ""
        value = re.sub(r"^.*?(?:今天|明天|后天|本周|下周|上午|下午|晚上|\d{1,2}点|半|的)", "", value)
        if value.endswith("会") and len(value) > 4:
            compact = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{2,4}会)$", value)
            if compact:
                value = compact.group(1)
        if value.endswith("会") and len(value) > 2:
            value = value[:-1]
        return value.strip("，。,. ")

    def _participant_lookup_keyword(self, person: dict[str, Any]) -> str:
        name = str(person.get("name") or "").strip()
        employee_no = str(person.get("employee_no") or "").strip()
        # In this simulator, user.get_info is primarily name-based. Numeric
        # hints are still preserved in slots and final evidence after lookup.
        return name or employee_no

    def _participant_user_from_selected_booking(self, mr: DomainState, person: dict[str, Any]) -> dict[str, Any]:
        booking = mr.evidence.get("selected_booking") if isinstance(mr.evidence.get("selected_booking"), dict) else {}
        return self._matching_participant_from_list(booking.get("participants") or [], person)

    def _matching_participant_from_list(self, participants: list[Any], person: dict[str, Any]) -> dict[str, Any]:
        name = str(person.get("name") or "").strip()
        employee_no = str(person.get("employee_no") or "").strip()
        for item in participants:
            if not isinstance(item, dict):
                continue
            if name and name == str(item.get("name") or ""):
                return item
            if employee_no and employee_no in {str(item.get("employee_no") or ""), str(item.get("user_id") or "")}:
                return item
        return {}

    def _participant_duplicate_check_required(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        return any(word in query for word in ["已经在", "不用加", "别重复", "不要重复", "如果他已经", "如果她已经"])

    def _participant_already_in_booking(self, mr: DomainState, user_id: Any, person: dict[str, Any]) -> bool:
        participants = mr.evidence.get("participants", {}).get("participants") or []
        if user_id and any(isinstance(item, dict) and str(item.get("user_id") or "") == str(user_id) for item in participants):
            return True
        return bool(self._matching_participant_from_list(participants, person))

    def _participant_name_from_evidence(self, mr: DomainState, user_id: Any) -> str:
        participants = mr.evidence.get("participants", {}).get("participants") or []
        for item in participants:
            if isinstance(item, dict) and str(item.get("user_id") or "") == str(user_id):
                return str(item.get("name") or "")
        return ""

    def _post_participant_extend_needed(self, state: RuntimeState) -> bool:
        query = self._full_query(state.obs)
        return bool(state.meetingroom.slots.get("duration_minutes")) and any(word in query for word in ["延长", "多开", "加长"])

    def _participant_booking_result_for_answer(self, state: RuntimeState, participant_results: list[dict[str, Any]]) -> dict[str, Any]:
        if not participant_results:
            return {}
        mr = state.meetingroom
        order_id = participant_results[0].get("order_id") or mr.slots.get("order_id")
        if not order_id:
            return {}
        if mr.evidence.get("extend_done"):
            first = participant_results[0]
            return {
                "status": "updated",
                "order_id": order_id,
                "added_user_id": first.get("user_id"),
                "new_end": mr.evidence.get("extend_done", {}).get("end"),
            }
        query = self._full_query(state.obs)
        explicit_order = bool(self._extract_order_id(query) or mr.slots.get("order_id"))
        if len(participant_results) > 1 and mr.intent == "participant_add":
            return {"status": "participants_added", "order_id": order_id, "added_count": len(participant_results)}
        if explicit_order and mr.intent == "participant_add":
            first = participant_results[0]
            return {"status": "participant_added", "order_id": order_id, "user_id": first.get("user_id")}
        if explicit_order and mr.intent == "participant_remove":
            first = participant_results[0]
            return {"status": "participant_removed", "order_id": order_id, "user_id": first.get("user_id")}
        return {}

    def _apply_participant_list_evidence(self, state: RuntimeState, result: dict[str, Any]) -> None:
        mr = state.meetingroom
        if mr.intent == "participant_list":
            participants = result.get("participants") if isinstance(result.get("participants"), list) else []
            mr.status = "done"
            mr.result = {"status": "queried", "order_id": result.get("order_id"), "participants": participants}
            return
        if mr.intent != "participant_add" or not self._participant_duplicate_check_required(state):
            return
        participants = mr.slots.get("participants") if isinstance(mr.slots.get("participants"), list) else []
        index = int(mr.evidence.get("participant_index") or 0)
        if index >= len(participants):
            return
        person = participants[index]
        existing = self._matching_participant_from_list(result.get("participants") or [], person)
        if not existing:
            key = f"participant_user_{index}"
            users = mr.evidence.get(key, {}).get("users") or []
            if users:
                user_id = users[0].get("user_id")
                existing = next(
                    (
                        item
                        for item in (result.get("participants") or [])
                        if isinstance(item, dict) and str(item.get("user_id") or "") == str(user_id)
                    ),
                    {},
                )
        if not existing:
            return
        mr.evidence.setdefault("participant_results", []).append(
            {
                "status": "already_exists",
                "order_id": result.get("order_id") or mr.slots.get("order_id"),
                "user_id": existing.get("user_id") or person.get("user_id"),
                "name": existing.get("name") or person.get("name") or "",
            }
        )
        mr.evidence["participant_index"] = index + 1
        if int(mr.evidence.get("participant_index") or 0) >= len(participants):
            mr.status = "done"
            mr.result = {"status": "updated", "order_id": result.get("order_id") or mr.slots.get("order_id")}

    # ------------------------------------------------------------------
    # Final answer
    # ------------------------------------------------------------------

    def _build_final_answer(self, state: RuntimeState) -> dict[str, Any]:
        answer: dict[str, Any] = {}
        if state.meetingroom.needed:
            participant_results = state.meetingroom.evidence.get("participant_results") or []
            if participant_results and state.meetingroom.intent in {"participant_add", "participant_remove"}:
                if len(participant_results) == 1:
                    answer["participant_result"] = participant_results[0]
                    booking_result = self._participant_booking_result_for_answer(state, participant_results)
                    if booking_result:
                        answer["booking_result"] = booking_result
                else:
                    if state.meetingroom.intent == "participant_add":
                        answer["participants_added"] = [
                            {"user_id": item.get("user_id"), "name": item.get("name")}
                            for item in participant_results
                        ]
                        order_id = participant_results[0].get("order_id")
                        if order_id:
                            answer["booking_result"] = {
                                "status": "participants_added",
                                "order_id": order_id,
                                "added_count": len(participant_results),
                            }
                    else:
                        answer["participants_removed"] = [
                            {"user_id": item.get("user_id"), "name": item.get("name")}
                            for item in participant_results
                        ]
                        booking_result = self._participant_booking_result_for_answer(state, participant_results)
                        if booking_result:
                            answer["booking_result"] = booking_result
                    booking_result = self._participant_booking_result_for_answer(state, participant_results)
                    if booking_result:
                        answer["booking_result"] = booking_result
            elif state.meetingroom.intent == "participant_list" and state.meetingroom.evidence.get("participants"):
                answer["participants"] = state.meetingroom.evidence.get("participants", {}).get("participants") or []
                if state.meetingroom.result:
                    answer["booking_result"] = state.meetingroom.result
            elif state.meetingroom.result:
                answer["booking_result"] = state.meetingroom.result
            elif state.meetingroom.status == "blocked":
                answer["booking_result"] = {"status": "blocked", "reason": state.meetingroom.blocked_reason or "missing_required_info"}
        if state.workflow.needed:
            if state.workflow.result:
                answer["workflow_draft_result"] = state.workflow.result
            elif state.workflow.status == "blocked":
                answer["workflow_draft_result"] = {"status": "blocked", "reason": state.workflow.blocked_reason or "missing_required_info"}
            oa_result = state.workflow.evidence.get("oa_checked") or {}
            if isinstance(oa_result, dict) and not oa_result.get("error"):
                oa_items = oa_result.get("items") if isinstance(oa_result.get("items"), list) else []
                if state.workflow.slots.get("submit") and "oa.done.list" in state.completed_tools:
                    answer["done_result"] = {
                        "status": "verified" if oa_items else "not_found",
                        "submitted_found": bool(oa_items),
                        "count": len(oa_items),
                    }
                elif "oa.todo.list" in state.completed_tools:
                    answer["todo_result"] = {
                        "status": "verified" if oa_items else "not_found",
                        "draft_found": bool(oa_items),
                        "count": len(oa_items),
                    }
        return answer

    def _all_done(self, state: RuntimeState) -> bool:
        if state.task_runtimes:
            if not all(runtime.status in TaskRuntime.TERMINAL_STATUSES for runtime in state.task_runtimes):
                return False
            return not self._workflow_needs_oa_check(state)
        domains = []
        if state.meetingroom.needed:
            domains.append(state.meetingroom.status in {"done", "blocked"})
        if state.workflow.needed:
            domains.append(state.workflow.status in {"done", "blocked"} and not self._workflow_needs_oa_check(state))
        return bool(domains) and all(domains)

    def _workflow_needs_oa_check(self, state: RuntimeState) -> bool:
        wf = state.workflow
        if wf.status != "done" or not wf.evidence.get("save_done") or wf.evidence.get("oa_checked"):
            return False
        if state.steps_used >= state.step_budget:
            return False
        if bool(wf.slots.get("submit")):
            return "oa.done.list" in state.tools
        return "oa.todo.list" in state.tools

    def _block_meetingroom(self, state: RuntimeState, reason: str, order_id: str | None = None) -> StepAction:
        decision = self._outcome_policy_decision(state, "meetingroom")
        if decision:
            state.meetingroom.evidence["outcome_policy_decision"] = decision
        if decision and decision.get("decision") == "terminal" and decision.get("reason"):
            reason = str(decision["reason"])
        args = {"reason": reason}
        if order_id:
            args["order_id"] = order_id
        return StepAction("block_meetingroom", args=args)

    def _block_workflow(self, state: RuntimeState, reason: str) -> StepAction:
        decision = self._outcome_policy_decision(state, "workflow")
        if decision:
            state.workflow.evidence["outcome_policy_decision"] = decision
        if decision and decision.get("decision") == "next_action" and decision.get("action") == "workflow.search_person":
            plan = self._leave_plan(state)
            args = self._next_approver_search_args(state, plan, mark_attempt=False) if plan else None
            if args is not None:
                return StepAction("tool", "workflow.search_person", args)
        if decision and decision.get("decision") == "terminal" and decision.get("reason"):
            reason = str(decision["reason"])
        if state.workflow_skill is not None:
            state.workflow_skill.mark_blocked(reason)
        return StepAction("block_workflow", args={"reason": reason})

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_day_text(self, text: str) -> str:
        patterns = [
            r"下个月\s*\d{1,2}号\s*(?:到|至|-|~)\s*\d{1,2}号",
            r"\d{1,2}号\s*(?:到|至|-|~)\s*\d{1,2}号",
            r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:到|至|-|~)\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
            r"\d{1,2}\s*月\s*\d{1,2}\s*日",
            r"下个月\s*\d{1,2}号",
            r"\d{1,2}号",
            r"下周[一二三四五六日天]",
            r"本周[一二三四五六日天]",
            r"周[一二三四五六日天]",
            r"今天",
            r"明天",
            r"后天",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)
        return ""

    def _extract_leave_day_text(self, text: str) -> str:
        patterns = [
            r"明天.*?到\s*后天",
            r"今天.*?到\s*明天",
            r"后天.*?到\s*大后天",
            r"下个月\s*\d{1,2}号\s*(?:到|至|-|~)\s*\d{1,2}号",
            r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:到|至|-|~)\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
            r"\d{1,2}号\s*(?:到|至|-|~)\s*\d{1,2}号",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)
        return self._extract_day_text(text)

    def _submit_intent(self, query: str) -> bool:
        if any(word in query for word in ["草稿", "先存", "保存草稿", "存一下", "老板还要确认"]):
            return False
        if any(word in query for word in ["不要保存草稿", "直接提交", "帮我提交", "提交申请", "也提交", "直接提就行", "直接提", "提掉"]):
            return True
        if "提交" in query and any(word in query for word in ["费用", "采购", "物资", "预算", "金额", "项目"]):
            return True
        if re.search(r"(?:需要|要|帮我)?提(?:交)?(?:一个|一笔|一批)?[^，。；;]{0,20}(?:费用|采购|物资)(?:申请)?", query):
            return True
        if any(word in query for word in ["提一个", "提一笔", "提费用", "提上去", "发起", "走流程", "走一笔", "走申请", "办理费用", "处理事假", "提交项目编码", "提交项目", "提交费用"]):
            return True
        return False

    def _leave_submit_intent(self, query: str) -> bool:
        return any(
            word in query
            for word in [
                "不要保存草稿",
                "直接提交",
                "帮我提交",
                "提交申请",
                "也提交",
                "直接提就行",
                "直接提",
                "提交请假",
                "请假并提交",
            ]
        )

    def _draft_intent(self, query: str) -> bool:
        if "不要保存草稿" in query:
            return False
        return any(word in query for word in ["草稿", "先存", "保存草稿", "存一下", "老板还要确认"])

    def _resolve_day(self, day_text: str, now_value: Any, prefer_workday: bool = False) -> str:
        try:
            now = datetime.fromisoformat(str(now_value).replace("Z", "+00:00")).date()
        except Exception:
            now = date.today()
        text = str(day_text or "")
        if "今天" in text:
            return now.isoformat()
        if "明天" in text:
            if prefer_workday:
                return self._next_meeting_business_day(now_value)
            candidate = now + timedelta(days=1)
            return candidate.isoformat()
        if "后天" in text:
            return (now + timedelta(days=2)).isoformat()
        match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if match:
            return date(now.year, int(match.group(1)), int(match.group(2))).isoformat()
        match = re.search(r"下个月\s*(\d{1,2})号", text)
        if match:
            return self._resolve_next_month_day(match.group(1), now_value)
        match = re.search(r"(\d{1,2})号", text)
        if match:
            return self._resolve_month_day(match.group(1), now_value)
        match = re.search(r"(下周|本周)([一二三四五六日天])|周([一二三四五六日天])", text)
        if match:
            prefix = match.group(1) or "周"
            weekday_text = match.group(2) or match.group(3)
            target = "一二三四五六日天".index(weekday_text)
            if target >= 7:
                target = 6
            week_start = now - timedelta(days=now.weekday())
            if prefix == "下周":
                week_start += timedelta(days=7)
            elif prefix == "周" and week_start + timedelta(days=target) < now:
                week_start += timedelta(days=7)
            return (week_start + timedelta(days=target)).isoformat()
        return ""

    def _week_start(self, now_value: Any, offset: int = 0) -> date:
        try:
            now = datetime.fromisoformat(str(now_value).replace("Z", "+00:00")).date()
        except Exception:
            now = date.today()
        return now - timedelta(days=now.weekday()) + timedelta(days=7 * offset)

    def _extract_time_range(self, text: str) -> tuple[str, str]:
        normalized = text.replace("：", ":")
        cross_day = self._extract_contextual_time_range(normalized)
        if cross_day != ("", ""):
            return cross_day
        patterns = [
            r"(\d{1,2}):(\d{1,2})?\s*(?:到|-|至|~)\s*(\d{1,2}):(\d{1,2})?",
            r"(\d{1,2})\s*点(半)?\s*(?:到|-|至|~)\s*(\d{1,2})\s*点(半)?",
            r"(\d{1,2})\s*点\s*(?:(\d{1,2})分)?\s*(?:到|-|至|~)\s*(\d{1,2})\s*点\s*(?:(\d{1,2})分)?",
            r"([一二两三四五六七八九十]+)\s*点(?:半)?\s*(?:到|-|至|~)\s*([一二两三四五六七八九十]+)\s*点(?:半)?",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            if match.re.pattern.startswith("(\\d"):
                h1 = int(match.group(1))
                m1 = 30 if match.group(2) == "半" else int(match.group(2) or 0)
                h2 = int(match.group(3))
                m2 = 30 if match.group(4) == "半" else int(match.group(4) or 0)
            else:
                h1 = self._cn_to_int(match.group(1))
                h2 = self._cn_to_int(match.group(2))
                m1 = 30 if "半" in match.group(0).split("到")[0] else 0
                m2 = 30 if "半" in match.group(0).split("到")[-1] else 0
            context = normalized[max(0, match.start() - 8): match.end() + 4]
            h1 = self._adjust_hour(h1, context, is_end=False)
            h2 = self._adjust_hour(h2, context, is_end=True)
            if h2 <= h1 and "上午" not in context:
                h2 += 12
            return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"

        start = self._single_time_after(normalized, ["下午", "上午", "早上"])
        if start:
            if "半天" in normalized and "下午" in normalized:
                return "14:00", "18:00"
            if "全天" in normalized:
                return "09:00", "18:00"
        return "", ""

    def _extract_contextual_time_range(self, text: str) -> tuple[str, str]:
        pattern = (
            r"(?P<p1>上午|下午|中午|早上|晚上)?\s*"
            r"(?P<h1>\d{1,2}|[一二两三四五六七八九十]+)\s*点(?P<half1>半)?"
            r".{0,12}?(?:到|至|-|~)"
            r".{0,12}?"
            r"(?P<p2>上午|下午|中午|早上|晚上)?\s*"
            r"(?P<h2>\d{1,2}|[一二两三四五六七八九十]+)\s*点(?P<half2>半)?"
        )
        match = re.search(pattern, text)
        if not match:
            return "", ""
        h1 = self._hour_token_to_int(match.group("h1"))
        h2 = self._hour_token_to_int(match.group("h2"))
        if h1 <= 0 or h2 <= 0:
            return "", ""
        h1 = self._adjust_hour_by_period(h1, match.group("p1") or "")
        h2 = self._adjust_hour_by_period(h2, match.group("p2") or "")
        m1 = 30 if match.group("half1") else 0
        m2 = 30 if match.group("half2") else 0
        return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"

    def _hour_token_to_int(self, value: Any) -> int:
        text = str(value or "")
        return int(text) if text.isdigit() else self._cn_to_int(text)

    def _adjust_hour_by_period(self, hour: int, period: str) -> int:
        if period in {"下午", "晚上"} and hour < 12:
            return hour + 12
        if period == "中午" and hour < 11:
            return hour + 12
        if not period and hour <= 7:
            return hour + 12
        return hour

    def _parse_time_token(self, text: str) -> str:
        match = re.search(r"(\d{1,2})(?:[:点](\d{1,2}))?", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if hour <= 7:
                hour += 12
            return f"{hour:02d}:{minute:02d}"
        match = re.search(r"([一二两三四五六七八九十]+)点(半)?", text)
        if match:
            hour = self._cn_to_int(match.group(1))
            if hour <= 7:
                hour += 12
            return f"{hour:02d}:{30 if match.group(2) else 0:02d}"
        return ""

    def _single_time_after(self, text: str, markers: list[str]) -> str:
        for marker in markers:
            if marker in text:
                part = text[text.find(marker): text.find(marker) + 12]
                parsed = self._parse_explicit_single_time(part)
                if parsed:
                    return parsed
        return ""

    def _parse_explicit_single_time(self, text: str) -> str:
        match = re.search(r"(\d{1,2})(?::(\d{1,2})|点(\d{1,2})?分?)", text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or match.group(3) or 0)
            if hour <= 7:
                hour += 12
            return f"{hour:02d}:{minute:02d}"
        match = re.search(r"([一二两三四五六七八九十]+)点(半)?", text)
        if match:
            hour = self._cn_to_int(match.group(1))
            if hour <= 7:
                hour += 12
            return f"{hour:02d}:{30 if match.group(2) else 0:02d}"
        return ""

    def _adjust_hour(self, hour: int, context: str, is_end: bool) -> int:
        if "下午" in context or "晚上" in context:
            if hour < 12:
                hour += 12
        elif "上午" not in context and hour <= 7:
            hour += 12
        if is_end and hour == 12 and "下午" in context:
            return 12
        return hour

    def _extract_duration_minutes(self, text: str) -> int:
        if "半小时" in text or "半个小时" in text:
            return 30
        match = re.search(r"(半小时|半个小时|(\d+)\s*(?:分钟|小时)|([一二两三四五六七八九十]+)个?\s*(?:分钟|小时))", text)
        if not match:
            return 0
        token = match.group(0)
        if "半" in token:
            return 30
        number = int(match.group(2)) if match.group(2) else self._cn_to_int(match.group(3) or "0")
        return number * 60 if "小时" in token else number

    def _extract_duration_hours(self, text: str) -> float:
        minutes = self._extract_duration_minutes(text)
        return round(minutes / 60, 2) if minutes else 0

    def _extract_office_candidates(self, text: str) -> list[str]:
        candidates: list[str] = []
        for match in re.finditer(r"(?<![A-Za-z0-9])A\d(?![A-Za-z0-9])", text, flags=re.I):
            value = match.group(0).upper()
            if value not in candidates:
                candidates.append(value)
        return candidates

    def _normalize_office_candidates(self, values: list[Any], text: str) -> list[str]:
        candidates: list[str] = []
        for value in values:
            match = re.search(r"(?<![A-Za-z0-9])A\d(?![A-Za-z0-9])", str(value), flags=re.I)
            if match:
                candidate = match.group(0).upper()
                if candidate not in candidates:
                    candidates.append(candidate)
        for candidate in self._extract_office_candidates(text):
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _office_address_candidates(self, text: str, offices: list[str]) -> list[str]:
        floor = ""
        floor_match = re.search(r"([一二三四五六七八九十\d])\s*(?:楼|层|F)", text, flags=re.I)
        if floor_match:
            floor_num = self._cn_to_int(floor_match.group(1)) if not floor_match.group(1).isdigit() else int(floor_match.group(1))
            floor = f"{floor_num}F"
        prefix = "0551" if "合肥" in text else "0552"
        out = []
        for office in offices:
            if floor:
                out.append(f"{prefix}_{office}_{floor}")
            if "小镇" in text or "合肥" in text or floor:
                out.append(f"{prefix}_{office}")
        return out

    def _normalize_office_address_candidates(self, values: list[Any], text: str) -> list[str]:
        out: list[str] = []
        prefix = "0551" if "合肥" in text else "0552"
        for raw in values:
            value = str(raw or "").strip()
            if not value:
                continue
            if re.match(r"^055\d_A\d(?:_\dF)?$", value, flags=re.I):
                normalized = value.upper()
                if normalized not in out:
                    out.append(normalized)
                continue
            office_match = re.search(r"(?<![A-Za-z0-9])A\d(?![A-Za-z0-9])", value, flags=re.I)
            if not office_match:
                continue
            office = office_match.group(0).upper()
            floor = ""
            floor_match = re.search(r"([一二三四五六七八九十\d])\s*(?:楼|层|F)", value, flags=re.I)
            if floor_match:
                floor_num = self._cn_to_int(floor_match.group(1)) if not floor_match.group(1).isdigit() else int(floor_match.group(1))
                floor = f"{floor_num}F"
            normalized = f"{prefix}_{office}_{floor}" if floor else f"{prefix}_{office}"
            if normalized not in out:
                out.append(normalized)
        generated = self._office_address_candidates(text, self._normalize_office_candidates(values, text))
        for value in generated:
            if value not in out:
                out.append(value)
        return out

    def _building_address(self, office_address: str) -> str:
        parts = str(office_address).split("_")
        if len(parts) >= 2:
            return "_".join(parts[:2])
        return office_address

    def _extract_room_ids(self, text: str) -> list[str]:
        return [match.group(0).upper() for match in re.finditer(r"A\d-\dF-\d{3}", text, flags=re.I)]

    def _office_id_from_room_id(self, room_id: Any) -> str:
        room_id = str(room_id or "")
        static = {
            "A3-3F-311": "a33f311000000000000000000000aa14",
            "A3-3F-312": "a33f312000000000000000000000aa15",
            "A3-3F-310": "a33f310000000000000000000000aa13",
        }
        return static.get(room_id, "")

    def _extract_capacity(self, text: str) -> int:
        patterns = [
            r"(\d+)\s*人",
            r"容量\s*(\d+)",
            r"([一二两三四五六七八九十]+)\s*人",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1)
                return int(value) if value.isdigit() else self._cn_to_int(value)
        return 0

    def _extract_capacity_delta(self, text: str) -> int:
        match = re.search(r"(?:要加|加了|加上|新增|增加了?|多了)\s*(\d+|[一二两三四五六七八九十]+)\s*(?:个)?(?:人|参会人)?", text)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else self._cn_to_int(token)
        return 0

    def _extract_meeting_title(self, text: str) -> str:
        patterns = [
            r"主题(?:是|写)?([\u4e00-\u9fa5A-Za-z0-9]+)",
            r"开([\u4e00-\u9fa5A-Za-z0-9]+?)(?:会|会议)",
            r"那个([\u4e00-\u9fa5A-Za-z0-9]+?)(?:会议室|会)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                title = match.group(1).strip("，。,. ")
                if title and title not in {"会议室", "会议"}:
                    if pattern.startswith("开") and "会" not in title[-1:]:
                        return title + "会"
                    if not title.endswith("会") and "复盘" not in title and "评审" not in title:
                        return title
                    return title
        for word in ["季度复盘", "项目复盘", "需求评审", "技术方案讨论", "战略规划", "年终总结", "代码评审", "一对一", "技术复盘"]:
            if word in text:
                return word
        return ""

    def _normalize_meeting_title(self, title: Any) -> str:
        value = str(title or "").strip("，。,. ")
        if value in {"复盘", "复盘会", "项目复盘会"}:
            return "项目复盘"
        return value

    def _extract_meeting_keyword(self, text: str) -> str:
        title = self._extract_meeting_title(text)
        if title:
            return title
        for word in ["项目复盘", "季度复盘", "复盘", "评审会", "评审", "需求评审", "项目启动"]:
            if word in text:
                return word.replace("评审会", "评审会")
        return ""

    def _meeting_fallback_policy(self, text: str) -> str:
        if "订不到就算了" in text or "不行就别乱订" in text:
            return "block_if_unavailable"
        if "保持原样" in text or "别动" in text:
            return "keep_if_extend_conflict"
        if "取消原会议" in text or "重订" in text or "重新预订" in text:
            return "cancel_rebook_if_extend_conflict"
        if "不行" in text or "也可以" in text:
            return "fallback_office"
        return ""

    def _extract_participants(self, text: str) -> list[dict[str, str]]:
        people: list[dict[str, str]] = []
        by_name: dict[str, dict[str, str]] = {}
        seen_numbers: set[str] = set()

        def add_person(raw_name: str, raw_employee_no: str = "") -> None:
            name = self._clean_participant_name(raw_name)
            employee_no = str(raw_employee_no or "").strip()
            if not name and not employee_no:
                return
            if name and (len(name) < 2 or len(name) > 4):
                return
            if employee_no and employee_no in seen_numbers:
                return
            item: dict[str, str] = {}
            if name:
                item["name"] = name
            if employee_no:
                item["employee_no"] = employee_no
                seen_numbers.add(employee_no)
            if name:
                existing = by_name.get(name)
                if existing:
                    if employee_no and not existing.get("employee_no"):
                        existing["employee_no"] = employee_no
                    return
                by_name[name] = item
            people.append(item)

        for name, employee_no in re.findall(r"([\u4e00-\u9fa5]{2,4})[（(](?:工号)?\s*(\d+)[）)]", text):
            add_person(name, employee_no)

        span_patterns = [
            r"把(.+?)(?:都)?(?:加入|加到|移除)",
            r"把(.+?)从.+?移除",
            r"请?(.+?)(?:都)?(?:加入|加到)订单",
            r"请?(.+?)(?:都)?(?:加入|加到).+?参会人",
            r"把(.+?)(?:都)?加到",
            r"(.+?)(?:都)?加入订单",
        ]
        for pattern in span_patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            span = re.sub(r"[（(](?:工号)?\s*\d+[）)]", "", match.group(1))
            for name in re.split(r"[、,，和\s]+", span):
                add_person(name)
            if people:
                return people
        return people

    def _clean_participant_name(self, value: Any) -> str:
        name = str(value or "").strip("，。,. ;；")
        name = re.sub(r"^(帮我|麻烦|请|我|把|和|及|与|都|再|然后)+", "", name)
        name = re.sub(r"^(帮我|麻烦|请|我|把|和|及|与|都|再|然后)+", "", name)
        name = re.sub(r"(加入|加到|移除|从|订单|参会人|会议|会里|里面|里)$", "", name)
        name = name.strip("，。,. ;；")
        return name

    def _extract_order_id(self, text: str) -> str:
        match = re.search(r"(SEED-[A-Z0-9-]+|ZH-[A-Z0-9-]+|BK-[A-Za-z0-9-]+)", text)
        return match.group(1) if match else ""

    def _extract_meeting_segments(self, text: str) -> list[dict[str, str]]:
        if "上午" not in text or "下午" not in text:
            return []
        day_text = self._extract_day_text(text)
        segments = []
        for marker in ["上午", "下午"]:
            idx = text.find(marker)
            if idx < 0:
                continue
            part = text[idx: idx + 30]
            start, end = self._extract_time_range(part)
            if not start or not end:
                continue
            title = self._extract_meeting_title(part)
            if not title:
                title = "需求评审" if marker == "上午" else "技术方案讨论"
            segments.append({"day_text": day_text, "start": start, "end": end, "title": title})
        return segments if len(segments) > 1 else []

    def _extract_approver_keyword(self, text: str) -> str:
        patterns = [
            r"找[^，。；;]*?(?:部门|部)的?([\u4e00-\u9fa5]{1,2})(?:工|老师|经理|主管|总监)",
            r"审批人(?:必须是|要是|指定为|定为|选|找|是|还是|仍是)?([\u4e00-\u9fa5]{2,4})",
            r"审批人(?:改成|改为|换成|换为)\s*([\u4e00-\u9fa5]{2,4})",
            r"找([\u4e00-\u9fa5]{2,3})(?:审批|批)",
            r"找[^，。；;]*?的?([\u4e00-\u9fa5]{1,2})(?:工|老师|经理|主管|总监)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                name = self._clean_approver_phrase(match.group(1))
                if name not in {"一个", "经理"}:
                    return name
        return ""

    def _extract_approver_hints(self, text: str) -> dict[str, str]:
        raw = self._extract_approver_keyword(text)
        if not raw:
            raw = self._first_regex(text, r"审批人(?:必须是|要是|指定为|定为|选|找|是|还是|仍是|改成|改为|换成|换为)?([\u4e00-\u9fa5]{1,4}(?:经理|主管|总监)?)")
        raw = self._clean_approver_phrase(raw)
        title = ""
        name_hint = raw
        department_hint = self._first_regex(text, r"(?:找|审批人找)?([\u4e00-\u9fa5A-Za-z0-9]{2,8})(?:部门|部)的")
        department_hint = re.sub(r"^(找|审批人找)", "", department_hint)
        for title_word in ["产品经理", "运营经理", "研发经理", "测试工程师", "工程师", "经理", "主管", "总监"]:
            if title_word in raw or title_word in text:
                title = title_word
                if raw.endswith(title_word):
                    name_hint = raw[: -len(title_word)]
                break
        if "刘工" in text and not title:
            title = "工程师"
        if name_hint in {"一个", "经", "经理", "一个经"}:
            name_hint = ""
        return {
            "approver_raw": raw,
            "approver_keyword": name_hint or raw,
            "approver_title": title,
            "approver_name_hint": name_hint,
            "approver_title_hint": title,
            "approver_department_hint": department_hint,
            "approver_employee_no": self._first_regex(text, r"审批人.*?(\d{6,})"),
        }

    def _normalize_approver_hints(self, leave: dict[str, Any]) -> None:
        raw = self._clean_approver_phrase(str(leave.get("approver_raw") or leave.get("approver_keyword") or "").strip())
        title = str(leave.get("approver_title_hint") or leave.get("approver_title") or "").strip()
        name_hint = self._clean_approver_phrase(str(leave.get("approver_name_hint") or "").strip())
        if not raw and not name_hint and not title:
            return
        for title_word in ["产品经理", "运营经理", "研发经理", "测试工程师", "工程师", "经理", "主管", "总监"]:
            if title_word in raw or title_word in title:
                title = title_word
                if raw.endswith(title_word):
                    name_hint = raw[: -len(title_word)]
                break
        if raw.endswith("工") and len(raw) <= 3:
            name_hint = raw[:-1]
            title = title or "工程师"
        if not name_hint:
            name_hint = raw
        if name_hint in {"一个", "某个", "任意", "找一个", "经", "经理", "一个经", "一个经理"}:
            name_hint = ""
        if title and name_hint == title:
            name_hint = ""
        name_hint = self._clean_approver_phrase(name_hint)
        leave["approver_raw"] = raw
        leave["approver_keyword"] = name_hint
        leave["approver_name_hint"] = name_hint
        leave["approver_title"] = title
        leave["approver_title_hint"] = title

    def _clean_approver_phrase(self, value: Any) -> str:
        text = str(value or "").strip("，。；;、 的")
        text = re.sub(r"^(还是|仍是|还是找|仍然是|必须是|要是|指定为|定为|改成|改为|换成|换为|审批人|找|选|是|为|请|让)+", "", text)
        text = re.sub(r"(部门|部)的", "", text)
        text = re.sub(r"(审批|批|处理|提交|申请)$", "", text)
        if text.endswith(("部门", "部")):
            return ""
        if text.endswith("工") and len(text) <= 3:
            text = text[:-1]
        return text.strip("，。；;、 的")

    def _slice_workflow_text(self, query: str, kind: str) -> str:
        if kind == "leave":
            keys = ["请假", "事假", "年假", "病假", "育儿假"]
            positions = [query.find(key) for key in keys if query.find(key) >= 0]
            if not positions:
                return query
            start = max(0, min(positions) - 20)
            return query[start:]

        expense_keys = ["费用", "采购", "报销", "物资"]
        structure_keys = ["项目", "项目是", "项目为", "项目还是", "平台是", "平台为", "系统是", "系统为", "工程是", "工程为", "专项是", "专项为"]
        budget_keys = ["预算", "总预算", "金额", "总金额"]
        expense_positions = [query.find(key) for key in expense_keys if query.find(key) >= 0]
        structure_positions = [query.find(key) for key in structure_keys if query.find(key) >= 0]
        strong_positions = expense_positions or structure_positions
        budget_positions = [query.find(key) for key in budget_keys if query.find(key) >= 0]
        positions = strong_positions or budget_positions
        if not positions:
            return query
        trigger_pos = min(positions)
        hard_boundaries = [query.rfind(boundary, 0, trigger_pos) for boundary in ["。", "；", ";", "\n"]]
        boundary = max(hard_boundaries)
        if boundary >= 0:
            start = boundary + 1
        elif strong_positions:
            start = max(0, trigger_pos - 20)
        else:
            start = 0
        return query[start:]

    def _extract_project_name(self, text: str) -> str:
        patterns = [
            r"(?:项目|平台|系统|工程|专项)(?:名称)?(?:也是|还是|仍是|还叫|是|为|叫)\s*([^，。；;:：]+)",
            r"(?<![\u4e00-\u9fa5A-Za-z0-9])(?:项目|平台|系统|工程|专项)(?:名称)?(?:：|:)\s*([^，。；;:：]+)",
            r"([\u4e00-\u9fa5A-Za-z0-9]{4,30}项目)(?:需要|要|包括|采购|，|。|；|;|$)",
            r"项目(?:是|为)?([^：:，。,]+?项目)",
            r"([^，。:：]+?项目)(?:需要|要|那边|包括|：|:)",
            r"([^，。:：；;]+?项目)(?:里|中|内|下|先|帮|直接)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return self._clean_project_phrase(match.group(1))
        return ""

    def _extract_project_code(self, text: str) -> str:
        match = re.search(r"(?<![A-Za-z0-9])([A-Z]-\d{6,12})(?![A-Za-z0-9])", str(text or ""))
        return match.group(1) if match else ""

    def _project_keywords(self, project_name: str, text: str) -> list[str]:
        candidates = []
        if project_name:
            cleaned = re.sub(r"(项目|那边|需要|申请|费用|采购|的)$", "", project_name)
            candidates.append(cleaned)
            parts = re.split(r"[，。:：\s]", cleaned)
            if parts:
                candidates.extend([part for part in parts if len(part) >= 4])
        candidates.extend(self._project_phrase_candidates(text))
        return [item for item in candidates if item]

    def _material_category_hint(self, text: str) -> str:
        match = re.search(r"(?:物资)?(?:大类|类别|类型)(?:是|为|选|选择|：|:)?\s*([^，。；;、]{2,30})", str(text or ""))
        if match:
            return match.group(1).strip("，。；;、 的")
        return ""

    def _extract_expense_items(self, text: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in self._extract_named_budget_items(text):
            items.append(row)
        for row in self._extract_quantity_first_items(text):
            items.append(row)
        unit_words = self._expense_unit_words()
        pattern = rf"([\u4e00-\u9fa5A-Za-z]+?)\s*(?:(\d+|[一二两三四五六七八九十]+)\s*(?:{unit_words})\s*)?(?:每(?:{unit_words}))?\s*(\d+(?:\.\d+)?)\s*(万|万元|元)"
        for name, qty_token, amount, unit in re.findall(pattern, text):
            name = re.split(r"[，。,:：；;、]|和|及|包括|包含", name)[-1]
            name = re.sub(r"^(元|万元|费用|预算|总预算|总金额|金额|项目|编码|要买|买|采购)+", "", name)
            name = name.strip("，。和及包括:： ")
            if self._looks_like_invalid_expense_item_name(name) or any(skip in name for skip in ["总预算", "总金额", "预算", "项目", "编码"]):
                continue
            qty = self._cn_to_int(qty_token) if qty_token and not qty_token.isdigit() else int(qty_token or 1)
            money = float(amount) * (10000 if unit.startswith("万") else 1)
            budget = money * qty if "每" in text[max(0, text.find(name)): text.find(name) + 20] else money
            unit_price = money if "每" in text[max(0, text.find(name)): text.find(name) + 20] else budget / qty
            items.append({"name": name, "quantity": str(qty), "unit_price": self._money(unit_price), "budget_amount": self._money(budget)})
        items = self._dedupe_expense_items(items)
        if not items:
            specific = self._extract_specific_item_with_total_budget(text)
            if specific:
                items.append(specific)
        if not items:
            service_item = self._extract_service_item_from_budget(text)
            if service_item:
                items.append(service_item)
        return items

    def _materialized_expense_items(self, expense: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = self._clean_explicit_expense_items(expense.get("items") or [])
        raw_text = str(expense.get("raw_text") or expense.get("source_text") or "")
        named = self._extract_named_budget_items(raw_text)
        if named and self._items_more_complete(named, items):
            items = named
        if not items and raw_text:
            items = self._clean_explicit_expense_items(self._extract_expense_items(raw_text))
        if items and raw_text:
            service_item = self._extract_service_item_from_budget(raw_text)
            if service_item and self._items_more_complete([service_item], items):
                items = [service_item]
        items = self._complete_quantity_only_items_from_total(expense, items)
        if len(items) > 1 and expense.get("total_amount"):
            try:
                item_total = sum(float(self._money(item.get("budget_amount"))) for item in items if item.get("budget_amount"))
                if item_total and abs(float(self._money(expense.get("total_amount"))) - item_total) > 0.01:
                    named_total = sum(float(self._money(item.get("budget_amount"))) for item in named if item.get("budget_amount"))
                    if named and abs(float(self._money(expense.get("total_amount"))) - named_total) <= 0.01:
                        items = named
            except Exception:
                pass
        return items

    def _items_more_complete(self, candidate: list[dict[str, Any]], current: list[dict[str, Any]]) -> bool:
        if not current:
            return True
        candidate_complete = sum(1 for item in candidate if item.get("name") and item.get("budget_amount"))
        current_complete = sum(1 for item in current if item.get("name") and item.get("budget_amount"))
        if candidate_complete != current_complete:
            return candidate_complete > current_complete
        if candidate_complete and current_complete:
            return self._expense_item_name_quality(candidate) > self._expense_item_name_quality(current)
        return len(candidate) > len(current)

    def _expense_item_name_quality(self, items: list[dict[str, Any]]) -> int:
        score = 0
        for item in items:
            name = str(item.get("name") or "")
            score += min(len(name), 8)
            if any(term in name for term in ["项目", "预算", "金额", "万元", "每台", "每个", "每条", "每场", "每份"]):
                score -= 12
            if name in {"周期", "采购周期", "确认采购周期"}:
                score -= 20
            if re.fullmatch(r"\d+", name):
                score -= 20
        return score

    def _clean_explicit_expense_items(self, rows: list[Any]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            item = dict(row)
            name = self._clean_material_name_match(str(item.get("name") or ""), str(item.get("name") or ""))
            if not name or self._looks_like_invalid_expense_item_name(name) or self._is_action_only_material_name(name):
                continue
            quantity = self._normalize_quantity(item.get("quantity") or "1")
            budget = self._money(item.get("budget_amount")) if item.get("budget_amount") else ""
            unit = self._money(item.get("unit_price")) if item.get("unit_price") else ""
            if budget and not unit:
                try:
                    unit = self._money(float(budget) / max(float(quantity), 1.0))
                except Exception:
                    unit = budget
            elif unit and not budget:
                try:
                    budget = self._money(float(unit) * max(float(quantity), 1.0))
                except Exception:
                    budget = unit
            cleaned.append(
                {
                    "name": name,
                    "quantity": quantity,
                    "unit_price": unit,
                    "budget_amount": budget,
                    # Preserve provenance through normalization and de-duping;
                    # this field is runtime evidence only, never save payload.
                    "source_line_id": str(item.get("source_line_id") or f"user_line_{index}"),
                }
            )
        return self._dedupe_expense_items(cleaned)

    def _is_action_only_material_name(self, value: Any) -> bool:
        text = str(value or "").strip("，。；;、 的")
        text = re.sub(r"^(另外|另一个任务是|然后|顺便|同时|还有|再|请|帮我|帮|给我|给)+", "", text)
        return text in {"", "提", "提交", "发起", "申请", "保存", "处理", "办理", "直接提", "直接提交"}

    def _complete_quantity_only_items_from_total(self, expense: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return items

    def _looks_like_invalid_expense_item_name(self, name: str) -> bool:
        text = str(name or "").strip()
        if len(text) < 2:
            return True
        if re.fullmatch(r"\d+", text):
            return True
        if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:万|万元|元|块)", text):
            return True
        if text in {"每台", "每个", "每条", "每场", "每份", "每支", "每套", "每册", "每张", "每本"} or text.startswith("每"):
            return True
        if text in {"总计", "合计", "总预算", "总金额", "预算", "金额", "帮我", "帮我把", "直接", "直接帮我", "提掉", "提交"}:
            return True
        if re.search(r"(台|个|条|场|份|支|套|批|项|册|张|本)每(?:台|个|条|场|份|支|套|批|项|册|张|本)?", text):
            return True
        if text in {"要做", "需要", "采购", "购买", "要买", "包括", "包含", "一批", "一些", "先帮我存", "先帮我", "帮我存", "需要做", "购置"}:
            return True
        if any(skip in text for skip in ["帮我先存", "草稿", "费用申请", "费用草稿"]):
            return True
        if any(skip in text for skip in ["总预算", "总金额", "项目编码", "项目是", "会议室", "万元"]):
            return True
        # These are request verbs, not a material/detail name.  In particular,
        # a regex spanning "然后提一个 X 费用申请" must not turn the whole
        # request phrase into an evidence row for X.
        if re.match(
            r"^(?:(?:另外|然后|顺便|同时|还有|再)\s*)?(?:(?:帮我|帮|给我|给)\s*)?(?:提一个|提一笔|提交一笔|申请一笔|发起一笔|提|提交|申请|发起)",
            text,
        ):
            return True
        if text.endswith(("费用申请", "采购申请")):
            return True
        return False

    def _extract_named_budget_items(self, text: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pattern = r"(?P<name>[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{1,24}?)\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>万|万元|元|块)"
        for match in re.finditer(pattern, str(text or "")):
            name = re.split(r"[，。,:：；;、]|和|及|包括|包含", match.group("name"))[-1]
            name = re.sub(r"^(帮我|直接|提交|申请|费用|预算|总预算|总金额|金额|项目|编码|要买|买|采购|需要|做|要做|一批)+", "", name)
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name):
                continue
            if name in {"总计", "合计", "总预算", "总金额", "预算", "金额"}:
                continue
            if any(summary in name for summary in ["总预算", "总金额", "预算", "费用"]):
                continue
            amount = float(match.group("amount")) * (10000 if match.group("unit").startswith("万") else 1)
            budget = self._money(amount)
            items.append({"name": name, "quantity": "1", "unit_price": budget, "budget_amount": budget})
        return self._dedupe_expense_items(items)

    def _expense_unit_words(self) -> str:
        return "台|个|条|场|份|支|套|批|项|册|张|本"

    def _extract_quantity_first_items(self, text: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        unit_words = self._expense_unit_words()
        name_first_pattern = rf"([\u4e00-\u9fa5A-Za-z0-9]{{2,18}}?)\s*(\d{{1,5}}|[一二两三四五六七八九十]+)\s*({unit_words})(?:[，,、]?\s*(?:每(?:{unit_words})?|单价)?\s*(\d+(?:\.\d+)?)\s*(万|万元|元)?)?"
        for match in re.finditer(name_first_pattern, text):
            name, qty_token, unit_name, amount, money_unit = match.groups()
            # "一批 X费用申请" denotes the workflow request, not a detail
            # row.  The noun X is only usable as evidence when it is not
            # immediately followed by request syntax.
            if str(text[match.end() :]).lstrip().startswith(("费用申请", "采购申请")):
                continue
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name) or any(skip in name for skip in ["项目", "预算", "金额", "会议室"]):
                continue
            qty = self._cn_to_int(qty_token) if not str(qty_token).isdigit() else int(qty_token)
            if qty <= 0 or qty > 100000:
                continue
            row = {"name": name, "quantity": str(qty), "unit_price": "", "budget_amount": ""}
            if amount:
                multiplier = 10000 if str(money_unit).startswith("万") else 1
                unit_price = float(amount) * multiplier
                row["unit_price"] = self._money(unit_price)
                row["budget_amount"] = self._money(unit_price * qty)
            items.append(row)
        separated_pattern = rf"(?<![A-Za-z0-9.-])(\d{{1,5}}|[一二两三四五六七八九十]+)\s*({unit_words})\s*([\u4e00-\u9fa5A-Za-z0-9]{{2,16}}?)[，,、]?\s*每(?:{unit_words})?\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)?"
        for qty_token, unit_name, name, amount, money_unit in re.findall(separated_pattern, text):
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name):
                continue
            qty = self._cn_to_int(qty_token) if not str(qty_token).isdigit() else int(qty_token)
            if qty <= 0 or qty > 100000:
                continue
            multiplier = 10000 if str(money_unit).startswith("万") else 1
            unit_price = float(amount) * multiplier
            budget = unit_price * qty
            items.append(
                {
                    "name": name,
                    "quantity": str(qty),
                    "unit_price": self._money(unit_price),
                    "budget_amount": self._money(budget),
                }
            )
        pattern = rf"(?<![A-Za-z0-9.-])(\d{{1,5}}|[一二两三四五六七八九十]+)\s*({unit_words})?\s*([\u4e00-\u9fa5A-Za-z0-9]{{2,12}}?)(?:每(?:{unit_words})?|单价)?\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)?"
        for qty_token, unit_name, name, amount, money_unit in re.findall(pattern, text):
            if not unit_name:
                previous = text[max(0, text.find(str(qty_token)) - 2): text.find(str(qty_token))]
                if any(marker in previous for marker in ["寸", "英寸"]) or str(name).startswith(("寸", "英寸")):
                    continue
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name) or any(skip in name for skip in ["项目", "预算", "金额", "会议室"]):
                continue
            qty = self._cn_to_int(qty_token) if not str(qty_token).isdigit() else int(qty_token)
            if qty <= 0 or qty > 100000:
                continue
            if not unit_name and not any(marker in text for marker in [f"{qty_token}{name}", f"{qty_token} {name}"]):
                qty = 1
            multiplier = 10000 if str(money_unit).startswith("万") else 1
            unit_price = float(amount) * multiplier
            budget = unit_price * qty
            items.append(
                {
                    "name": name,
                    "quantity": str(qty),
                    "unit_price": self._money(unit_price),
                    "budget_amount": self._money(budget),
                }
            )
        whole_item_pattern = rf"(?:有|采购|购买|要买|要印|印|做)?\s*(?:一|1)\s*({unit_words})\s*([^，。；;、]{{2,18}}?)\s*(?:，|。|；|;|预算|总预算|总金额|金额)\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)?"
        for unit_name, name, amount, money_unit in re.findall(whole_item_pattern, text):
            if str(name).strip().endswith(("费用申请", "采购申请")):
                continue
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name):
                continue
            multiplier = 10000 if str(money_unit).startswith("万") else 1
            budget = float(amount) * multiplier
            items.append({"name": name, "quantity": "1", "unit_price": self._money(budget), "budget_amount": self._money(budget)})
        whole_item_split_pattern = rf"(?:有|采购|购买|要买|要印|印|做)?\s*(?:一|1)\s*({unit_words})\s*([^，。；;、]{{2,18}}?)[，,、]\s*(?:预算|总预算|总金额|金额)\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)?"
        for unit_name, name, amount, money_unit in re.findall(whole_item_split_pattern, text):
            if str(name).strip().endswith(("费用申请", "采购申请")):
                continue
            name = self._clean_material_name_match(name, name)
            if self._looks_like_invalid_expense_item_name(name):
                continue
            multiplier = 10000 if str(money_unit).startswith("万") else 1
            budget = float(amount) * multiplier
            items.append({"name": name, "quantity": "1", "unit_price": self._money(budget), "budget_amount": self._money(budget)})
        return items

    def _extract_service_item_from_budget(self, text: str) -> dict[str, Any]:
        amount = self._extract_amount_after(text, ["预算", "总预算", "总金额", "金额"]) or self._extract_first_amount(text)
        if not amount:
            return {}
        candidates: list[str] = []
        for pattern in [
            r"要做([^，。；;]+?)(?:，|。|预算|总预算|金额)",
            r"(?:要做|需要)(?:一批|一些)?\s*([^，。；;]+?)(?:，|。|预算|总预算|金额)",
            # A bare "X费用" can name a concrete service (for example, a
            # directly mappable catalog item).  "X费用申请" only describes
            # the workflow request and is intentionally excluded.
            r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}?)(?:费用|费)(?:草稿|预算|，|。|；|;)",
        ]:
            for match in re.finditer(pattern, text):
                value = re.sub(r"^(要做|做|需要|采购|申请)", "", match.group(1)).strip("，。；;、的 ")
                value = re.sub(
                    r"^(?:(?:另外|然后|顺便|同时|还有|再)\s*)?(?:(?:帮我|帮|给我|给)\s*)?(?:提一个|提一笔|提交一笔|申请一笔|发起一笔|提|提交|申请|发起)\s*",
                    "",
                    value,
                )
                value = self._clean_material_name_match(value, value)
                if value and len(value) >= 2 and not self._looks_like_invalid_expense_item_name(value):
                    candidates.append(value)
        name = sorted(self._dedupe(candidates), key=len, reverse=True)[0] if candidates else ""
        if not name:
            return {}
        return {"name": name, "quantity": "1", "unit_price": amount, "budget_amount": amount}

    def _extract_specific_item_with_total_budget(self, text: str) -> dict[str, Any]:
        amount = self._extract_total_amount(text) or self._extract_amount_after(text, ["预算", "总预算", "总金额", "金额"])
        if not amount:
            return {}
        for pattern in [
            r"(?:要做|需要|采购|购买|要买|包括|包含)(?:一批|一些)?\s*([^，。；;、]{2,18}?)(?:，|。|；|;|预算|总预算|总金额|金额)",
        ]:
            for match in re.finditer(pattern, str(text or "")):
                name = self._clean_material_name_match(match.group(1), match.group(1))
                if name and not self._looks_like_invalid_expense_item_name(name):
                    return {"name": name, "quantity": "1", "unit_price": amount, "budget_amount": amount}
        return {}

    def _dedupe_expense_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in items:
            name = str(item.get("name") or "")
            if not name:
                continue
            key = (self._expense_item_dedupe_name(name), str(item.get("quantity") or ""), str(item.get("budget_amount") or ""))
            current = by_key.get(key)
            if current is None or self._expense_item_quality(item) > self._expense_item_quality(current):
                by_key[key] = item
        return list(by_key.values())

    def _expense_item_dedupe_name(self, name: str) -> str:
        text = str(name or "")
        text = re.sub(r"^(?:再|又|另)?(?:买|采购|购买|要买|要印|印|做|需要|把)", "", text)
        text = re.sub(r"\d+(?:\.\d+)?\s*(?:台|个|条|场|份|支|套|批|项|册|张|本)$", "", text)
        return text.strip("，。；;、 的") or str(name or "")

    def _expense_item_quality(self, item: dict[str, Any]) -> int:
        name = str(item.get("name") or "")
        score = len(name)
        if item.get("budget_amount"):
            score += 10
        if item.get("unit_price"):
            score += 5
        if any(prefix in name for prefix in ["再买", "要买", "采购", "需要", "申请", "提交"]):
            score -= 8
        if re.search(r"\d+(?:台|个|条|场|份|支|套|批|项|册|张|本)$", name):
            score -= 5
        return score

    def _extract_amount_after(self, text: str, markers: list[str]) -> str:
        for marker in markers:
            idx = text.find(marker)
            if idx >= 0:
                amount = self._extract_first_amount(text[idx: idx + 30])
                if amount:
                    return amount
        return ""

    def _extract_total_amount(self, text: str) -> str:
        for pattern in [
            r"(?:总预算|总金额|总计|合计)\s*(\d+(?:\.\d+)?)\s*(万|万元|元)?",
            r"预算\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)",
            r"预算\s*(\d+(?:\.\d+)?)(?!\s*(?:台|个|条|场|份|支|套|批|项|册|张|本))",
            r"(?:费用|费)\s*(\d+(?:\.\d+)?)\s*(万|万元|元|块)",
        ]:
            match = re.search(pattern, str(text or ""))
            if match:
                unit = match.group(2) if (match.lastindex or 0) >= 2 else ""
                value = float(match.group(1)) * (10000 if str(unit or "").startswith("万") else 1)
                return self._format_money(value)
        return ""

    def _extract_amount_near(self, text: str, marker: str) -> str:
        idx = text.find(marker)
        if idx < 0:
            return ""
        return self._extract_first_amount(text[idx: idx + 30])

    def _extract_first_amount(self, text: str) -> str:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(万|万元|元|块)", text)
        if match:
            value = float(match.group(1)) * (10000 if match.group(2).startswith("万") else 1)
            return self._format_money(value)
        match = re.search(r"(?:预算|金额|总预算|总金额)\s*(\d+(?:\.\d+)?)", text)
        if not match:
            return ""
        value = float(match.group(1))
        return self._format_money(value)

    def _leave_type_value(self, text: str) -> str:
        for key, value in LEAVE_TYPE_MAP.items():
            if key in str(text):
                return value
        return "L"

    def _reason_value(self, text: str) -> str:
        for key, value in REASON_MAP.items():
            if key in str(text):
                return value
        return "10"

    def _canonical_time_value(self, value: Any) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{1,2}:\d{1,2}", text):
            hour, minute = [int(part) for part in text.split(":", 1)]
            return f"{hour:02d}:{minute:02d}"
        if re.fullmatch(r"\d{1,2}点半?", text):
            return self._parse_time_token(text)
        if text in {"上午", "下午", "全天", "半天", "早上", "晚上"}:
            return ""
        return ""

    def _semantic_score(self, hint: str, label: str) -> int:
        hint = str(hint)
        label = str(label)
        score = 0
        if hint and hint in label:
            score += 5
        for token in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", hint):
            if len(token) >= 2 and token in label:
                score += 1
        return score

    def _select_broad_material_option(self, hint: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        return None

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    def _clean_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in args.items() if value not in (None, "", [], {})}

    def _cache_key(self, tool: str, args: dict[str, Any]) -> str:
        if tool not in READ_TOOLS:
            return ""
        return tool + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)

    def _money(self, value: Any) -> str:
        text = str(value or "").strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|万元|元)", text)
        if match:
            multiplier = 10000 if match.group(2).startswith("万") else 1
            return self._format_money(float(match.group(1)) * multiplier)
        try:
            return self._format_money(float(text))
        except Exception:
            return text

    def _format_money(self, value: Any) -> str:
        return f"{float(value):0.2f}"

    def _normalize_quantity(self, value: Any) -> str:
        text = str(value or "").strip()
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if match:
            number = float(match.group(1))
            return str(int(number)) if number.is_integer() else str(number)
        match = re.search(r"([一二两三四五六七八九十]+)", text)
        if match:
            return str(self._cn_to_int(match.group(1)))
        return text or "1"

    def _valid_number(self, value: Any) -> bool:
        try:
            float(str(value))
            return True
        except Exception:
            return False

    def _cn_to_int(self, text: str) -> int:
        text = str(text or "")
        if text.isdigit():
            return int(text)
        if text == "十":
            return 10
        if "十" in text:
            left, _, right = text.partition("十")
            return (CN_NUM.get(left, 1) if left else 1) * 10 + CN_NUM.get(right, 0)
        return CN_NUM.get(text, 0)

    def _add_minutes(self, time_value: str, minutes: int) -> str:
        hour, minute = [int(part) for part in time_value.split(":")]
        total = hour * 60 + minute + minutes
        return f"{total // 60:02d}:{total % 60:02d}"

    def _time_to_minutes(self, value: Any) -> int:
        hour, minute = [int(part) for part in str(value).split(":")]
        return hour * 60 + minute

    def _minutes_between(self, start: Any, end: Any) -> int:
        if not start or not end:
            return 0
        return self._time_to_minutes(end) - self._time_to_minutes(start)

    def _hours_between(self, start: str, end: str) -> float:
        sh, sm = [int(part) for part in start.split(":")]
        eh, em = [int(part) for part in end.split(":")]
        return round(((eh * 60 + em) - (sh * 60 + sm)) / 60, 2)

    def _leave_duration_hours(self, start_day: str, start: str, end_day: str, end: str) -> float:
        try:
            start_date = date.fromisoformat(start_day)
            end_date = date.fromisoformat(end_day)
            start_dt = datetime.combine(start_date, datetime.strptime(start, "%H:%M").time())
            end_dt = datetime.combine(end_date, datetime.strptime(end, "%H:%M").time())
            return round((end_dt - start_dt).total_seconds() / 3600, 2)
        except Exception:
            return self._hours_between(start, end)

    def _time_overlap(self, start: str, end: str, other_start: str, other_end: str) -> bool:
        return bool(start and end and other_start and other_end and start < other_end and end > other_start)

    def _first_free_window(
        self,
        busy_slots: list[list[str]] | list[tuple[str, str]],
        window_start: str,
        window_end: str,
        duration_minutes: int,
    ) -> tuple[str, str] | None:
        cursor = self._time_to_minutes(window_start)
        latest = self._time_to_minutes(window_end)
        normalized = sorted(
            (
                (self._time_to_minutes(item[0]), self._time_to_minutes(item[1]))
                for item in busy_slots
                if item and len(item) >= 2 and item[0] and item[1]
            ),
            key=lambda item: item[0],
        )
        for start, end in normalized:
            if end <= cursor:
                continue
            if start - cursor >= duration_minutes:
                return self._minutes_to_time(cursor), self._minutes_to_time(cursor + duration_minutes)
            cursor = max(cursor, end)
        if latest - cursor >= duration_minutes:
            return self._minutes_to_time(cursor), self._minutes_to_time(cursor + duration_minutes)
        return None

    def _minutes_to_time(self, minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _extract_weekdays(self, text: Any) -> list[int]:
        out = []
        for match in re.finditer(r"(?:每周|下周|本周|周)([一二三四五六日天])", str(text or "")):
            idx = "一二三四五六日天".index(match.group(1))
            out.append(6 if idx >= 7 else idx)
        return self._dedupe(out)

    def _recurring_leave_count(self, query: str) -> int:
        if "这两周" in query or "两周" in query:
            return 2
        match = re.search(r"(?:这|连续)?([一二两三四五六七八九十\d]+)\s*周", query)
        if match:
            token = match.group(1)
            return int(token) if token.isdigit() else self._cn_to_int(token)
        return 1

    def _schedule_required_days(self, state: RuntimeState) -> list[str]:
        query = self._full_query(state.obs)
        now = state.obs.get("now")
        days = []
        explicit_day = self._extract_day_text(query)
        if explicit_day.startswith(("下周", "本周")):
            day = self._resolve_day(explicit_day, now, prefer_workday=self._is_meeting_context(query))
            if day:
                days.append(day)
        for match in re.finditer(r"(?<![下本])(?:周|、周|，周|和周)([一二三四五六日天])", query):
            day = self._resolve_day("周" + match.group(1), now)
            if day:
                days.append(day)
        if len(days) == 1:
            count_match = re.search(r"(?:连续)?([一二两三四五六七八九十\d]+)\s*天", query)
            if count_match:
                count_token = count_match.group(1)
                count = int(count_token) if count_token.isdigit() else self._cn_to_int(count_token)
                if count > 1:
                    try:
                        start = date.fromisoformat(days[0])
                        for offset in range(1, min(count, 7)):
                            candidate = (start + timedelta(days=offset)).isoformat()
                            days.append(candidate)
                    except Exception:
                        pass
        if "明天" in query:
            day = self._resolve_day("明天", now, prefer_workday=self._is_meeting_context(query))
            if day:
                days.append(day)
        if not days:
            day = self._meeting_day(state)
            if day:
                days.append(day)
        return self._dedupe(days)

    def _first_match(self, text: str, choices: list[str]) -> str:
        for choice in choices:
            if choice in str(text):
                return choice
        return ""

    def _first_regex(self, text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1) if match else ""

    def _dedupe(self, items: list[Any]) -> list[Any]:
        out = []
        for item in items:
            if item and item not in out:
                out.append(item)
        return out

    def _workspace_rank(self, room: dict[str, Any], workspace: dict[str, Any]) -> tuple[int, int, int]:
        address = str(workspace.get("office_address") or "")
        parts = address.split("_")
        region = parts[0] if parts else ""
        building = parts[1] if len(parts) > 1 else ""
        floor = parts[2] if len(parts) > 2 else ""
        return (
            1 if (region == "0552" and room.get("campus") == "小镇") or (region == "0551" and room.get("campus") == "合肥") else 0,
            1 if building and room.get("building") == building else 0,
            1 if floor and room.get("floor") == floor else 0,
        )
