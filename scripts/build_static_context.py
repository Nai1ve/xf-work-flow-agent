#!/usr/bin/env python3
"""Build static contract context files for the NL2Workflow agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPLIT = ROOT / "contest" / "train"
DEFAULT_VAL_SPLIT = ROOT / "contest" / "val"
DEFAULT_OUTPUT = ROOT / "submission" / "static_context"
POLICY_TEMPLATE_DIR = ROOT / "submission" / "static_context"

EXPENSE_WORKFLOW_ID = "34747"

WRITE_TOOLS = {
    "meetingroom.booking.create",
    "meetingroom.booking.cancel",
    "meetingroom.booking.extend",
    "meetingroom.booking.participant.add",
    "meetingroom.booking.participant.remove",
    "workflow.save",
    "workflow.delete",
}

HIGH_RISK_WRITE_TOOLS = {
    "meetingroom.booking.cancel",
    "meetingroom.booking.extend",
    "meetingroom.booking.participant.remove",
    "workflow.delete",
}

ADAPTER_NOTES = {
    "meetingroom.booking.create": [
        "tool spec lists both office_id and room_id as required, but simulator accepts either officeId-style office_id or room_id",
        "write must use a room candidate returned by meetingroom.room.list or meetingroom.room.schedule",
    ],
    "user.get_info": [
        "tool spec lists keyword as required, but simulator returns current user when keyword is omitted",
    ],
    "workflow.schema": [
        "requires either workflow_id or exact workflow name",
    ],
    "workflow.search_person": [
        "requires keyword or title; person ids must come from returned people",
    ],
    "workflow.project_search": [
        "requires project_name or project_code; project_code and wbs_code must come from returned projects",
    ],
    "workflow.browser_search": [
        "requires workflow_id and field_id; browser values must come from returned options",
    ],
}

PROMPT_CARDS = {
    "routing.md": """# Routing Context

Use this context only to map a user request to declared business capabilities and slot hints.

- Domains: meetingroom, workflow, user, oa, file.
- Meetingroom capabilities: book, query booking, query schedule, cancel, extend, rebook, cancel then rebook, participant add/remove/list.
- Workflow capabilities: leave draft/submit, expense material draft/submit.
- User-provided ids, codes, room names, project names, people, dates, amounts, and materials are only hints until verified by tools.
- Do not produce tool calls or final answers in routing output.
""",
    "tool_policy.md": """# Tool Policy

- Never invent ids, codes, option values, user ids, workflow ids, room ids, order ids, project codes, or wbs codes.
- Read tools collect evidence. Write tools require preflight against tool args, schema rules, and evidence sources.
- If required evidence is absent or candidates remain ambiguous, return blocked/need_more_info instead of guessing.
""",
    "workflow_form_policy.md": """# Workflow Form Policy

- Build workflow drafts from the active workflow schema and verified evidence only.
- Applicant values come from user.get_info.
- Approver values come from workflow.search_person.
- Project name/code/wbs values come from workflow.project_search.
- Browser/select values come from workflow.browser_search or schema option sets.
- Detail rows must satisfy required fields and money consistency: quantity * unit_price = budget_amount, and total_amount = sum(detail budget_amount).
- Explicit submit intent is required for submitted workflows; otherwise treat requests as draft.
""",
    "meetingroom_policy.md": """# Meetingroom Policy

- Static room data helps normalize location and room references, but availability and conflicts must come from meetingroom tools.
- room_id/order_id used in write operations must come from current tool evidence or explicit user text verified by tools.
- booking.create requires a selected room candidate, day, start, end, and title.
- Existing booking operations must first identify the target booking with booking.list.
""",
    "preflight_policy.md": """# Preflight Policy

- Validate tool name and required arguments before every call.
- Validate write arguments against evidence sources.
- Validate workflow required fields, option values, person/project sources, detail rows, and money totals.
- Validate meetingroom write actions against room candidates, booking targets, bookable flag, and conflict evidence.
""",
}


CAPABILITIES = {
    "meeting.book": {
        "domain": "meetingroom",
        "intent": "book_single",
        "risk": "write",
        "required_slots": ["day_text", "start", "end"],
        "read_tools": ["user.get_workspace", "meetingroom.room.list", "meetingroom.room.schedule"],
        "write_tools": ["meetingroom.booking.create"],
        "evidence_required": ["room_candidate"],
    },
    "meeting.query_booking": {
        "domain": "meetingroom",
        "intent": "query_booking",
        "risk": "read",
        "required_slots": [],
        "read_tools": ["meetingroom.booking.list"],
        "write_tools": [],
        "evidence_required": [],
    },
    "meeting.query_room_schedule": {
        "domain": "meetingroom",
        "intent": "query_room_schedule",
        "risk": "read",
        "required_slots": ["room_ids"],
        "read_tools": ["meetingroom.room.schedule"],
        "write_tools": [],
        "evidence_required": [],
    },
    "meeting.schedule_book": {
        "domain": "meetingroom",
        "intent": "book_by_schedule_analysis",
        "risk": "write",
        "required_slots": ["day_text"],
        "read_tools": ["meetingroom.room.list", "meetingroom.room.schedule"],
        "write_tools": ["meetingroom.booking.create"],
        "evidence_required": ["room_candidate", "schedule"],
    },
    "meeting.book_multi_segments": {
        "domain": "meetingroom",
        "intent": "book_multi_segments_same_room",
        "risk": "write",
        "required_slots": ["segments"],
        "read_tools": ["meetingroom.room.list", "meetingroom.room.schedule"],
        "write_tools": ["meetingroom.booking.create"],
        "evidence_required": ["room_candidate"],
    },
    "meeting.cancel": {
        "domain": "meetingroom",
        "intent": "cancel_existing",
        "risk": "high_risk_write",
        "required_slots": ["target_booking"],
        "read_tools": ["meetingroom.booking.list"],
        "write_tools": ["meetingroom.booking.cancel"],
        "evidence_required": ["selected_booking"],
    },
    "meeting.extend": {
        "domain": "meetingroom",
        "intent": "extend_existing",
        "risk": "high_risk_write",
        "required_slots": ["target_booking", "duration_minutes"],
        "read_tools": ["meetingroom.booking.list"],
        "write_tools": ["meetingroom.booking.extend"],
        "evidence_required": ["selected_booking"],
    },
    "meeting.rebook_larger": {
        "domain": "meetingroom",
        "intent": "rebook_larger_existing",
        "risk": "high_risk_write",
        "required_slots": ["target_booking"],
        "read_tools": ["meetingroom.booking.list", "meetingroom.room.list"],
        "write_tools": ["meetingroom.booking.cancel", "meetingroom.booking.create"],
        "evidence_required": ["selected_booking", "room_candidate"],
    },
    "meeting.cancel_rebook": {
        "domain": "meetingroom",
        "intent": "cancel_rebook_existing",
        "risk": "high_risk_write",
        "required_slots": ["target_booking"],
        "read_tools": ["meetingroom.booking.list", "meetingroom.room.list"],
        "write_tools": ["meetingroom.booking.cancel", "meetingroom.booking.create"],
        "evidence_required": ["selected_booking", "room_candidate"],
    },
    "meeting.participant_add": {
        "domain": "meetingroom",
        "intent": "participant_add",
        "risk": "write",
        "required_slots": ["target_booking", "participants"],
        "read_tools": ["meetingroom.booking.list", "user.get_info"],
        "write_tools": ["meetingroom.booking.participant.add"],
        "evidence_required": ["selected_booking", "verified_user"],
    },
    "meeting.participant_remove": {
        "domain": "meetingroom",
        "intent": "participant_remove",
        "risk": "write",
        "required_slots": ["target_booking", "participants"],
        "read_tools": ["meetingroom.booking.list", "user.get_info"],
        "write_tools": ["meetingroom.booking.participant.remove"],
        "evidence_required": ["selected_booking", "verified_user"],
    },
    "meeting.participant_list": {
        "domain": "meetingroom",
        "intent": "participant_list",
        "risk": "read",
        "required_slots": ["target_booking"],
        "read_tools": ["meetingroom.booking.list", "meetingroom.booking.participant.list"],
        "write_tools": [],
        "evidence_required": ["selected_booking"],
    },
    "workflow.leave_draft": {
        "domain": "workflow",
        "intent": "leave",
        "risk": "write",
        "required_slots": ["day_text", "start", "end", "leave_type_label", "approver_hint"],
        "read_tools": ["user.get_info", "workflow.catalog", "workflow.schema", "file.list", "workflow.search_person"],
        "write_tools": ["workflow.save"],
        "evidence_required": ["applicant", "workflow_schema", "approver_candidate"],
    },
    "workflow.leave_submit": {
        "domain": "workflow",
        "intent": "leave",
        "risk": "high_risk_write",
        "required_slots": ["explicit_submit", "day_text", "start", "end", "leave_type_label", "approver_hint"],
        "read_tools": ["user.get_info", "workflow.catalog", "workflow.schema", "file.list", "workflow.search_person"],
        "write_tools": ["workflow.save"],
        "evidence_required": ["applicant", "workflow_schema", "approver_candidate"],
        "post_check": "oa.done.list",
    },
    "workflow.expense_draft": {
        "domain": "workflow",
        "intent": "expense_material",
        "risk": "write",
        "required_slots": ["project_hint", "material_hint"],
        "read_tools": ["user.get_info", "workflow.catalog", "workflow.schema", "workflow.project_search", "workflow.browser_search"],
        "write_tools": ["workflow.save"],
        "evidence_required": ["applicant", "workflow_schema", "verified_project", "category_option", "subclass_option"],
    },
    "workflow.expense_submit": {
        "domain": "workflow",
        "intent": "expense_material",
        "risk": "high_risk_write",
        "required_slots": ["explicit_submit", "project_hint", "material_hint"],
        "read_tools": ["user.get_info", "workflow.catalog", "workflow.schema", "workflow.project_search", "workflow.browser_search"],
        "write_tools": ["workflow.save"],
        "evidence_required": ["applicant", "workflow_schema", "verified_project", "category_option", "subclass_option"],
        "post_check": "oa.done.list",
    },
}


MEETING_INTENT_CAPABILITY = {
    "book_single": "meeting.book",
    "query_booking": "meeting.query_booking",
    "query": "meeting.query_booking",
    "query_room_schedule": "meeting.query_room_schedule",
    "book_by_schedule_analysis": "meeting.schedule_book",
    "schedule_book": "meeting.schedule_book",
    "book_multi_segments_same_room": "meeting.book_multi_segments",
    "cancel_existing": "meeting.cancel",
    "cancel": "meeting.cancel",
    "extend_existing": "meeting.extend",
    "extend": "meeting.extend",
    "rebook_larger_existing": "meeting.rebook_larger",
    "rebook_larger": "meeting.rebook_larger",
    "cancel_rebook_existing": "meeting.cancel_rebook",
    "cancel_rebook": "meeting.cancel_rebook",
    "participant_add": "meeting.participant_add",
    "participant_remove": "meeting.participant_remove",
    "participant_list": "meeting.participant_list",
}


WORKFLOW_INTENT_CAPABILITY = {
    "leave": {"draft": "workflow.leave_draft", "submit": "workflow.leave_submit"},
    "expense_material": {"draft": "workflow.expense_draft", "submit": "workflow.expense_submit"},
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def expense_request_shape(query: str, rows: list[dict[str, Any]]) -> str:
    """Separate reusable package conventions from user-specified line items."""
    normalized_query = re.sub(r"[\s（）()，。；;、,:：]", "", str(query or "")).lower()
    mentioned = 0
    for row in rows:
        name = re.sub(r"[\s（）()]", "", str(row.get("material_name") or "")).lower()
        aliases = {name}
        aliases.add(re.sub(r"含.*$", "", name))
        aliases.add(re.sub(r"(?:制作|服务|采购|印刷)$", "", name))
        aliases = {item for item in aliases if len(item) >= 2}
        if any(alias in normalized_query for alias in aliases):
            mentioned += 1
    if rows and mentioned == len(rows):
        return "explicit_complete"
    if len(rows) > 1 and mentioned:
        return "explicit_partial"
    return "generic_package"


def normalize_expense_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "material_subclass": str(row.get("material_subclass") or ""),
        "material_name": str(row.get("material_name") or "").strip(),
        "quantity": str(row.get("quantity") or ""),
        "unit_price": str(row.get("unit_price") or ""),
        "budget_amount": str(row.get("budget_amount") or ""),
    }


def validate_expense_template(total: Any, rows: list[dict[str, str]]) -> None:
    try:
        expected_total = Decimal(str(total))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid expense total: {total}") from exc
    actual_total = Decimal("0")
    for row in rows:
        try:
            quantity = Decimal(row["quantity"])
            unit_price = Decimal(row["unit_price"])
            budget_amount = Decimal(row["budget_amount"])
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid expense row amount: {row}") from exc
        if not row["material_subclass"] or not row["material_name"]:
            raise ValueError(f"incomplete expense row: {row}")
        if quantity <= 0 or unit_price < 0 or quantity * unit_price != budget_amount:
            raise ValueError(f"expense row does not conserve amount: {row}")
        actual_total += budget_amount
    if actual_total != expected_total:
        raise ValueError(f"expense rows total {actual_total} != workflow total {expected_total}")


def build_expense_examples_index(cases_dir: Path) -> dict[str, Any]:
    """Build train-only memories without retaining benchmark case identifiers."""
    memories: dict[str, dict[str, Any]] = {}
    source_hashes: list[str] = []
    for case_path in sorted(cases_dir.glob("*.json")):
        case = load_json(case_path)
        case_hash = sha256(case_path)
        source_hashes.append(case_hash)
        query = str(case.get("user_query") or "").strip()
        project_queries = [
            str((step.get("args") or {}).get("project_name") or "").strip()
            for step in case.get("gold_trajectory") or []
            if isinstance(step, dict)
            and step.get("tool") == "workflow.project_search"
            and isinstance(step.get("args"), dict)
            and (step.get("args") or {}).get("project_name")
        ]
        for step in case.get("gold_trajectory") or []:
            if not isinstance(step, dict) or step.get("tool") != "workflow.save":
                continue
            args = step.get("args") if isinstance(step.get("args"), dict) else {}
            if str(args.get("workflow_id") or "") != EXPENSE_WORKFLOW_ID:
                continue
            data = args.get("data") if isinstance(args.get("data"), dict) else {}
            details = data.get("details") if isinstance(data.get("details"), dict) else {}
            rows = [normalize_expense_row(row) for row in details.get("detail_2") or [] if isinstance(row, dict)]
            validate_expense_template(data.get("total_amount"), rows)
            project_aliases = _expense_project_aliases(case, data, project_queries)
            entry = {
                "request_shape": expense_request_shape(query, rows),
                "request_text": query,
                "submit": bool(args.get("submit")),
                "project": {
                    "project_name": str(data.get("project_name") or ""),
                    "project_code": str(data.get("project_code") or ""),
                    "wbs_code": str(data.get("wbs_code") or ""),
                },
                "project_search_queries": sorted(set(project_queries)),
                "project_aliases": project_aliases,
                "material_category": str(data.get("material_category") or ""),
                "total_amount": str(data.get("total_amount") or ""),
                "rows": rows,
            }
            memory_id = content_sha256(entry)[:20]
            existing = memories.get(memory_id)
            if existing:
                existing["support_count"] += 1
                existing["source_sha256"].append(case_hash)
                continue
            memories[memory_id] = {
                "memory_id": memory_id,
                **entry,
                "support_count": 1,
                "source_sha256": [case_hash],
            }
    entries = sorted(memories.values(), key=lambda item: item["memory_id"])
    shape_counts = Counter(str(item.get("request_shape") or "unknown") for item in entries)
    corpus_hash = hashlib.sha256("\n".join(sorted(source_hashes)).encode("ascii")).hexdigest()
    return {
        "schema_version": "expense-examples-v1",
        "provenance": {
            "split": "train",
            "cases_path": relative(cases_dir),
            "cases_sha256": corpus_hash,
            "policy": "train gold only; case identifiers omitted; runtime ids must still be verified by tools",
        },
        "counts": {
            "source_cases": len(source_hashes),
            "memories": len(entries),
            "request_shapes": dict(sorted(shape_counts.items())),
        },
        "entries": entries,
    }


def build_leave_defaults_index(cases_dir: Path) -> dict[str, Any]:
    """Record train-observed defaults only when the user did not name an approver."""
    entries: list[dict[str, Any]] = []
    source_hashes: list[str] = []
    for case_path in sorted(cases_dir.glob("*.json")):
        case = load_json(case_path)
        query = str(case.get("user_query") or "").strip()
        if "审批人" in query:
            continue
        save_step = next(
            (
                step
                for step in case.get("gold_trajectory") or []
                if isinstance(step, dict)
                and step.get("tool") == "workflow.save"
                and str((step.get("args") or {}).get("workflow_id") or "") == "72247"
            ),
            None,
        )
        if not save_step:
            continue
        args = save_step.get("args") if isinstance(save_step.get("args"), dict) else {}
        data = args.get("data") if isinstance(args.get("data"), dict) else {}
        approver_id = str(data.get("approver") or "")
        if not approver_id:
            continue
        people = ((case.get("world_state") or {}).get("workflow_people") or []) if isinstance(case.get("world_state"), dict) else []
        person = next((item for item in people if isinstance(item, dict) and str(item.get("user_id") or "") == approver_id), {})
        case_hash = sha256(case_path)
        source_hashes.append(case_hash)
        payload = {
            "request_text": query,
            "submit": bool(args.get("submit")),
            "leave_type": str(data.get("leave_type") or ""),
            "approver_user_id": approver_id,
            "approver_name": str(person.get("name") or ""),
            "source_sha256": case_hash,
        }
        entries.append({"memory_id": content_sha256(payload)[:20], **payload})
    corpus_hash = hashlib.sha256("\n".join(sorted(source_hashes)).encode("ascii")).hexdigest()
    return {
        "schema_version": "leave-defaults-v1",
        "provenance": {
            "split": "train",
            "cases_path": relative(cases_dir),
            "cases_sha256": corpus_hash,
            "policy": "train gold defaults only; case identifiers omitted; approver must still be returned by workflow.search_person",
        },
        "counts": {"memories": len(entries)},
        "entries": sorted(entries, key=lambda item: item["memory_id"]),
    }


def _expense_project_aliases(case: dict[str, Any], saved_data: dict[str, Any], project_queries: list[str]) -> list[str]:
    """Collect only train-observed aliases for the project used by workflow.save."""
    project_code = str(saved_data.get("project_code") or "")
    aliases = [str(saved_data.get("project_name") or ""), *project_queries]
    world_state = case.get("world_state") if isinstance(case.get("world_state"), dict) else {}
    search_results = world_state.get("project_search_results") if isinstance(world_state.get("project_search_results"), dict) else {}
    for value in search_results.values():
        projects = value if isinstance(value, list) else []
        for project in projects:
            if not isinstance(project, dict) or str(project.get("project_code") or "") != project_code:
                continue
            aliases.extend(
                [
                    str(project.get("project_name") or ""),
                    str(project.get("profit_center") or ""),
                ]
            )
    return sorted({alias.strip() for alias in aliases if alias and alias.strip()})


def tool_domain(tool_name: str) -> str:
    return tool_name.split(".", 1)[0]


def tool_risk(tool_name: str) -> str:
    if tool_name in HIGH_RISK_WRITE_TOOLS:
        return "high_risk_write"
    if tool_name in WRITE_TOOLS:
        return "write"
    return "read"


def build_tools_index(tool_specs: dict[str, Any]) -> dict[str, Any]:
    by_name: dict[str, Any] = {}
    by_domain: dict[str, list[str]] = defaultdict(list)
    by_risk: dict[str, list[str]] = defaultdict(list)
    required_args: dict[str, list[str]] = {}

    for name, spec in sorted(tool_specs.items()):
        schema = spec.get("args_schema") if isinstance(spec.get("args_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = list(schema.get("required") or [])
        risk = tool_risk(name)
        domain = tool_domain(name)
        by_name[name] = {
            "name": name,
            "domain": domain,
            "risk": risk,
            "description": spec.get("description", ""),
            "required_args": required,
            "properties": properties,
            "adapter_notes": ADAPTER_NOTES.get(name, []),
        }
        by_domain[domain].append(name)
        by_risk[risk].append(name)
        required_args[name] = required

    return {
        "schema_version": "tools-index-v1",
        "counts": {
            "tools": len(by_name),
            "domains": {key: len(value) for key, value in sorted(by_domain.items())},
            "risk": {key: len(value) for key, value in sorted(by_risk.items())},
        },
        "by_name": by_name,
        "by_domain": {key: sorted(value) for key, value in sorted(by_domain.items())},
        "by_risk": {key: sorted(value) for key, value in sorted(by_risk.items())},
        "write_tools": sorted(WRITE_TOOLS),
        "high_risk_write_tools": sorted(HIGH_RISK_WRITE_TOOLS),
        "required_args": required_args,
    }


def field_summary(field: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "key",
        "label",
        "type",
        "required",
        "readonly",
        "recommended_input",
        "default",
        "format",
        "options_key",
        "depends_on",
        "returns_with",
        "description",
    ]
    return {key: field.get(key) for key in keys if key in field}


def evidence_for_field(field: dict[str, Any], workflow_id: str) -> list[str]:
    key = str(field.get("key") or "")
    field_type = str(field.get("type") or "")
    recommended = str(field.get("recommended_input") or "")
    evidence: list[str] = []
    if key in {"applicant", "applicant_no"} or recommended == "auto_filled":
        evidence.append("user.get_info")
    if key == "approver":
        evidence.append("workflow.search_person")
    if key in {"project_name", "project_code", "wbs_code"}:
        evidence.append("workflow.project_search")
    if field_type in {"browser", "select"} and key not in {"applicant", "approver"}:
        evidence.append("workflow.browser_search or schema option set")
    if recommended == "computed":
        evidence.append("program_computed")
    if workflow_id == "34747" and key == "total_amount":
        evidence.append("sum(details.budget_amount)")
    return evidence


def build_workflows_index(workflow_data: dict[str, Any]) -> dict[str, Any]:
    catalog = workflow_data.get("workflow_catalog") or []
    schemas = workflow_data.get("workflow_schemas") or {}
    option_sets = workflow_data.get("workflow_browser_options") or {}
    by_id: dict[str, Any] = {}
    by_name: dict[str, str] = {}

    for item in catalog:
        workflow_id = str(item.get("workflow_id"))
        if workflow_id:
            by_name[str(item.get("name") or "")] = workflow_id

    for workflow_id, schema in sorted(schemas.items()):
        fields = [field_summary(field) for field in schema.get("fields") or [] if isinstance(field, dict)]
        fields_by_key = {field.get("key"): field for field in fields if field.get("key")}
        detail_tables: dict[str, Any] = {}
        for table_id, table in sorted((schema.get("detail_tables") or {}).items()):
            table_fields = [field_summary(field) for field in table.get("fields") or [] if isinstance(field, dict)]
            detail_tables[table_id] = {
                "required_fields": table.get("required_fields") or [],
                "field_descriptions": table.get("field_descriptions") or {},
                "field_types": table.get("field_types") or {},
                "field_aliases": table.get("field_aliases") or {},
                "fields": table_fields,
                "money_rules": [
                    "quantity * unit_price = budget_amount",
                    "workflow total_amount = sum(details[].budget_amount)",
                ],
            }
        dependencies = {
            field.get("key"): field.get("depends_on")
            for field in fields
            if field.get("key") and field.get("depends_on")
        }
        field_evidence = {
            field.get("key"): evidence_for_field(field, str(workflow_id))
            for field in fields
            if field.get("key")
        }
        by_id[str(workflow_id)] = {
            "workflow_id": int(workflow_id) if str(workflow_id).isdigit() else workflow_id,
            "name": next((item.get("name") for item in catalog if str(item.get("workflow_id")) == str(workflow_id)), ""),
            "required_fields": schema.get("required_fields") or [],
            "field_descriptions": schema.get("field_descriptions") or {},
            "field_types": schema.get("field_types") or {},
            "field_aliases": schema.get("field_aliases") or {},
            "fields": fields,
            "fields_by_key": fields_by_key,
            "dependencies": dependencies,
            "detail_tables": detail_tables,
            "field_evidence_requirements": field_evidence,
            "submit_policy": "explicit submit intent required; draft by default",
        }

    return {
        "schema_version": "workflows-index-v1",
        "catalog": catalog,
        "by_id": by_id,
        "by_name": by_name,
        "option_sets": option_sets,
        "counts": {
            "workflows": len(catalog),
            "schemas": len(by_id),
            "option_sets": len(option_sets),
        },
    }


def room_items(meetingroom_data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rooms = meetingroom_data.get("rooms") or {}
    if isinstance(rooms, dict):
        return [(str(room_id), dict(room)) for room_id, room in rooms.items() if isinstance(room, dict)]
    if isinstance(rooms, list):
        out = []
        for idx, room in enumerate(rooms):
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("room_id") or room.get("officeId") or idx)
            out.append((room_id, dict(room)))
        return out
    return []


def append_index(index: dict[str, list[str]], key: Any, room_id: str) -> None:
    if key in (None, ""):
        return
    index.setdefault(str(key), []).append(room_id)


def capacity_bucket(capacity: int) -> str:
    if capacity <= 6:
        return "1-6"
    if capacity <= 10:
        return "7-10"
    if capacity <= 20:
        return "11-20"
    if capacity <= 50:
        return "21-50"
    return "51+"


def compact_room(room_id: str, room: dict[str, Any]) -> dict[str, Any]:
    return {
        "room_id": room_id,
        "officeId": room.get("officeId"),
        "name": room.get("name"),
        "capacity": room.get("capacity"),
        "campus": room.get("campus"),
        "location": room.get("location"),
        "building": room.get("building"),
        "floor": room.get("floor"),
        "area": room.get("area"),
        "bookable": room.get("bookable", True),
        "hasScreen": room.get("hasScreen", False),
        "features": room.get("features") or [],
    }


def build_meetingrooms_index(meetingroom_data: dict[str, Any]) -> dict[str, Any]:
    by_room_id: dict[str, Any] = {}
    by_office_id: dict[str, str] = {}
    by_campus: dict[str, list[str]] = {}
    by_location: dict[str, list[str]] = {}
    by_building: dict[str, list[str]] = {}
    by_floor: dict[str, list[str]] = {}
    by_capacity_bucket: dict[str, list[str]] = {}
    by_screen: dict[str, list[str]] = {"true": [], "false": []}
    by_bookable: dict[str, list[str]] = {"true": [], "false": []}
    campus_counts: Counter[str] = Counter()
    building_counts: Counter[str] = Counter()
    floor_counts: Counter[str] = Counter()
    capacity_values: list[int] = []

    for room_id, room in room_items(meetingroom_data):
        room["room_id"] = room_id
        compact = compact_room(room_id, room)
        by_room_id[room_id] = compact
        if compact.get("officeId"):
            by_office_id[str(compact["officeId"])] = room_id
        append_index(by_campus, compact.get("campus"), room_id)
        append_index(by_location, compact.get("location"), room_id)
        append_index(by_building, compact.get("building"), room_id)
        append_index(by_floor, compact.get("floor"), room_id)
        capacity = int(compact.get("capacity") or 0)
        capacity_values.append(capacity)
        append_index(by_capacity_bucket, capacity_bucket(capacity), room_id)
        by_screen[str(bool(compact.get("hasScreen"))).lower()].append(room_id)
        by_bookable[str(bool(compact.get("bookable", True))).lower()].append(room_id)
        if compact.get("campus"):
            campus_counts[str(compact.get("campus"))] += 1
        if compact.get("building"):
            building_counts[str(compact.get("building"))] += 1
        if compact.get("floor"):
            floor_counts[str(compact.get("floor"))] += 1

    return {
        "schema_version": "meetingrooms-index-v1",
        "counts": {
            "rooms": len(by_room_id),
            "campus": dict(sorted(campus_counts.items())),
            "building": dict(sorted(building_counts.items())),
            "floor": dict(sorted(floor_counts.items())),
            "bookable": {key: len(value) for key, value in sorted(by_bookable.items())},
            "hasScreen": {key: len(value) for key, value in sorted(by_screen.items())},
            "capacity": {
                "min": min(capacity_values) if capacity_values else 0,
                "max": max(capacity_values) if capacity_values else 0,
            },
        },
        "by_room_id": by_room_id,
        "by_office_id": by_office_id,
        "by_campus": {key: sorted(value) for key, value in sorted(by_campus.items())},
        "by_location": {key: sorted(value) for key, value in sorted(by_location.items())},
        "by_building": {key: sorted(value) for key, value in sorted(by_building.items())},
        "by_floor": {key: sorted(value) for key, value in sorted(by_floor.items())},
        "by_capacity_bucket": {key: sorted(value) for key, value in sorted(by_capacity_bucket.items())},
        "by_screen": {key: sorted(value) for key, value in sorted(by_screen.items())},
        "by_bookable": {key: sorted(value) for key, value in sorted(by_bookable.items())},
        "office_address_rules": {
            "0552": "讯飞小镇 all buildings",
            "0552_A1": "讯飞小镇 A1 building",
            "0552_A1_3F": "讯飞小镇 A1 building 3F",
            "0551": "合肥总部 all buildings",
            "0551_A4": "合肥 A4 building",
            "0551_TYDK": "天源迪科",
            "0551_0023": "中国声谷",
            "0551_0056": "高新区产业园",
            "0551_0058": "B3",
            "0551_0041": "上源汇展科技园",
            "0551_0071": "中安创谷2期K5栋",
        },
        "normalization_aliases": {
            "小镇": ["0552", "讯飞小镇"],
            "合肥": ["0551", "合肥总部"],
            "screen": ["屏幕", "投屏", "显示屏"],
            "bookable": ["可预订", "有权限"],
        },
    }


def build_capabilities_index() -> dict[str, Any]:
    by_domain: dict[str, list[str]] = defaultdict(list)
    by_risk: dict[str, list[str]] = defaultdict(list)
    for capability_id, spec in sorted(CAPABILITIES.items()):
        by_domain[str(spec.get("domain") or "unknown")].append(capability_id)
        by_risk[str(spec.get("risk") or "unknown")].append(capability_id)
    return {
        "schema_version": "capabilities-index-v1",
        "capabilities": CAPABILITIES,
        "meeting_intent_map": MEETING_INTENT_CAPABILITY,
        "workflow_intent_map": WORKFLOW_INTENT_CAPABILITY,
        "by_domain": {key: sorted(value) for key, value in sorted(by_domain.items())},
        "by_risk": {key: sorted(value) for key, value in sorted(by_risk.items())},
        "counts": {
            "capabilities": len(CAPABILITIES),
            "domains": {key: len(value) for key, value in sorted(by_domain.items())},
            "risk": {key: len(value) for key, value in sorted(by_risk.items())},
        },
    }


def validate_split_hashes(train_dir: Path, val_dir: Path) -> dict[str, Any]:
    checks = {}
    for rel in ["tool_specs.json", "data/workflow_data.json", "data/meetingroom_data.json"]:
        train_path = train_dir / rel
        val_path = val_dir / rel
        if not val_path.exists():
            checks[rel] = {"train_sha256": sha256(train_path), "val_sha256": None, "same": None}
            continue
        train_hash = sha256(train_path)
        val_hash = sha256(val_path)
        checks[rel] = {"train_sha256": train_hash, "val_sha256": val_hash, "same": train_hash == val_hash}
    return checks


def build_static_context(split_dir: Path, val_dir: Path, output_dir: Path, fail_on_hash_mismatch: bool = True) -> dict[str, Any]:
    tool_path = split_dir / "tool_specs.json"
    workflow_path = split_dir / "data" / "workflow_data.json"
    meetingroom_path = split_dir / "data" / "meetingroom_data.json"
    cases_dir = split_dir / "cases"

    tool_specs = load_json(tool_path)
    workflow_data = load_json(workflow_path)
    meetingroom_data = load_json(meetingroom_path)
    hash_checks = validate_split_hashes(split_dir, val_dir)
    mismatches = [rel for rel, item in hash_checks.items() if item.get("same") is False]
    if mismatches and fail_on_hash_mismatch:
        raise RuntimeError(f"train/val static data hash mismatch: {', '.join(mismatches)}")

    tools_index = build_tools_index(tool_specs)
    workflows_index = build_workflows_index(workflow_data)
    meetingrooms_index = build_meetingrooms_index(meetingroom_data)
    capabilities_index = build_capabilities_index()
    expense_examples_index = build_expense_examples_index(cases_dir)
    leave_defaults_index = build_leave_defaults_index(cases_dir)
    workflow_skills_index = load_json(POLICY_TEMPLATE_DIR / "workflow_skills.index.json")
    outcome_policies_index = load_json(POLICY_TEMPLATE_DIR / "outcome_policies.index.json")

    manifest = {
        "schema_version": "static-context-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "tool_specs": {"path": relative(tool_path), "sha256": sha256(tool_path)},
            "workflow_data": {"path": relative(workflow_path), "sha256": sha256(workflow_path)},
            "meetingroom_data": {"path": relative(meetingroom_path), "sha256": sha256(meetingroom_path)},
            "expense_examples": expense_examples_index.get("provenance") or {},
            "leave_defaults": leave_defaults_index.get("provenance") or {},
        },
        "split_hash_checks": hash_checks,
        "counts": {
            "capabilities": len(CAPABILITIES),
            "tools": len(tool_specs),
            "workflows": len(workflow_data.get("workflow_catalog") or []),
            "rooms": len(room_items(meetingroom_data)),
            "expense_examples": int((expense_examples_index.get("counts") or {}).get("memories") or 0),
            "leave_defaults": int((leave_defaults_index.get("counts") or {}).get("memories") or 0),
            "workflow_skills": len(workflow_skills_index.get("skills") or {}),
            "outcome_policies": len(outcome_policies_index.get("rules") or []),
        },
        "files": {
            "tools": "tools.index.json",
            "workflows": "workflows.index.json",
            "meetingrooms": "meetingrooms.index.json",
            "capabilities": "capabilities.index.json",
            "expense_examples": "expense_examples.index.json",
            "leave_defaults": "leave_defaults.index.json",
            "workflow_skills": "workflow_skills.index.json",
            "outcome_policies": "outcome_policies.index.json",
            "prompt_cards": "prompt_cards/",
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "tools.index.json", tools_index)
    write_json(output_dir / "workflows.index.json", workflows_index)
    write_json(output_dir / "meetingrooms.index.json", meetingrooms_index)
    write_json(output_dir / "capabilities.index.json", capabilities_index)
    write_json(output_dir / "expense_examples.index.json", expense_examples_index)
    write_json(output_dir / "leave_defaults.index.json", leave_defaults_index)
    write_json(output_dir / "workflow_skills.index.json", workflow_skills_index)
    write_json(output_dir / "outcome_policies.index.json", outcome_policies_index)
    cards_dir = output_dir / "prompt_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in PROMPT_CARDS.items():
        (cards_dir / filename).write_text(content.strip() + "\n", encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-dir", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--val-dir", type=Path, default=DEFAULT_VAL_SPLIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-hash-mismatch", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = build_static_context(
        split_dir=args.split_dir,
        val_dir=args.val_dir,
        output_dir=args.output_dir,
        fail_on_hash_mismatch=not args.allow_hash_mismatch,
    )
    print(json.dumps({"output_dir": str(args.output_dir), "counts": manifest["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
