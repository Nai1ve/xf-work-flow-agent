#!/usr/bin/env python3
"""Build a deep static profile for NL2Workflow train/val data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_PREFIX = ROOT / "reports" / "analysis" / "dataset_deep_profile_20260708"
WRITE_TOOLS = {
    "meetingroom.booking.create",
    "meetingroom.booking.cancel",
    "meetingroom.booking.extend",
    "meetingroom.booking.participant.add",
    "meetingroom.booking.participant.remove",
    "workflow.save",
    "workflow.delete",
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pct(part: int | float, total: int | float) -> float:
    return round(100 * part / total, 2) if total else 0.0


def quantiles(values: list[int | float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)

    def q(pos: float) -> float:
        if len(ordered) == 1:
            return float(ordered[0])
        idx = (len(ordered) - 1) * pos
        low = math.floor(idx)
        high = math.ceil(idx)
        if low == high:
            return float(ordered[low])
        return float(ordered[low] + (ordered[high] - ordered[low]) * (idx - low))

    return {
        "count": len(values),
        "min": min(values),
        "p25": round(q(0.25), 2),
        "median": round(q(0.5), 2),
        "p75": round(q(0.75), 2),
        "max": max(values),
        "avg": round(statistics.mean(values), 2),
    }


def sorted_counter(counter: Counter[Any]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def top_items(counter: Counter[Any], limit: int = 20) -> list[tuple[Any, int]]:
    return counter.most_common(limit)


def case_prefix(case_id: str) -> str:
    if case_id.startswith("beta_mr_wf_"):
        return "beta_mr_wf"
    parts = case_id.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else case_id


def tool_domain(tool: str) -> str:
    if tool == "__reply__":
        return "reply"
    return tool.split(".", 1)[0]


def is_write_tool(tool: str) -> bool:
    return tool in WRITE_TOOLS


def nested_statuses(value: Any) -> list[str]:
    statuses: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("status"), str):
            statuses.append(value["status"])
        for child in value.values():
            statuses.extend(nested_statuses(child))
    elif isinstance(value, list):
        for child in value:
            statuses.extend(nested_statuses(child))
    return statuses


def flattened_leaf_keys(value: Any, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.extend(flattened_leaf_keys(child, path))
    elif isinstance(value, list):
        for child in value:
            keys.extend(flattened_leaf_keys(child, f"{prefix}[]"))
    else:
        keys.append(prefix)
    return keys


def canonicalize_success_condition(text: str) -> str:
    text = re.sub(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?", "<datetime>", text)
    text = re.sub(r"\b\d{2}:\d{2}\b", "<time>", text)
    text = re.sub(r"\b\d{5,}\b", "<id>", text)
    text = re.sub(r"[a-f0-9]{32}", "<hash>", text)
    return text


def classify_case(case: dict[str, Any]) -> list[str]:
    labels: set[str] = set()
    case_id = str(case.get("case_id", ""))
    tags = set(case.get("tags") or [])
    domains = set(case.get("primary_domains") or [])
    gold_tools = [
        step.get("tool")
        for step in case.get("gold_trajectory", [])
        if isinstance(step, dict)
    ]
    save_steps = [
        step
        for step in case.get("gold_trajectory", [])
        if isinstance(step, dict) and step.get("tool") == "workflow.save"
    ]
    must = " ".join((case.get("success_check") or {}).get("must_satisfy") or [])
    ref = case.get("reference_final_answer") or {}
    query = str(case.get("user_query") or "")

    labels.update(tags)
    for domain in domains:
        labels.add(f"domain:{domain}")
    labels.add(f"prefix:{case_prefix(case_id)}")

    if case.get("mode") == "multi_turn":
        labels.add("multi_turn")
    if len(domains) > 1 or "cross_domain" in tags:
        labels.add("cross_domain")
    if "__reply__" in gold_tools:
        labels.add("reply_required")
    if any(token in must + query for token in ["确认", "别直接", "不要直接", "不允许", "先告诉"]):
        labels.add("confirmation_or_cautious")
    if any(token in must + query for token in ["候选", "不存在新的成功会议预订", "订不到", "冲突", "不可用"]):
        labels.add("candidate_or_blocked")

    if "meetingroom.booking.create" in gold_tools:
        labels.add("meeting_create")
    if "meetingroom.booking.cancel" in gold_tools:
        labels.add("meeting_cancel")
    if "meetingroom.booking.extend" in gold_tools:
        labels.add("meeting_extend")
    if "meetingroom.booking.list" in gold_tools:
        labels.add("meeting_lookup_existing")
    if any(str(tool).startswith("meetingroom.booking.participant.") for tool in gold_tools):
        labels.add("participants")
    if "meetingroom.room.schedule" in gold_tools or "meetingroom.room.bookings" in gold_tools:
        labels.add("room_schedule_check")

    if save_steps:
        labels.add("workflow_save")
    if "workflow.delete" in gold_tools:
        labels.add("workflow_delete")
    if "workflow.project_search" in gold_tools:
        labels.add("project_lookup")
    if "workflow.browser_search" in gold_tools:
        labels.add("browser_option_lookup")

    for step in save_steps:
        args = step.get("args") or {}
        workflow_id = str(args.get("workflow_id") or args.get("name") or "unknown")
        submit = args.get("submit")
        if workflow_id == "72247" or "请假" in query or "leave" in tags:
            labels.add("workflow_leave")
            labels.add("workflow_leave_submit" if submit else "workflow_leave_draft")
        elif workflow_id == "34747" or "费用" in query or "物资" in query or "expense" in tags:
            labels.add("workflow_expense")
            labels.add("workflow_expense_submit" if submit else "workflow_expense_draft")
        else:
            labels.add(f"workflow_id:{workflow_id}")

    if any(status in {"blocked", "failed", "not_found", "conflict"} for status in nested_statuses(ref)):
        labels.add("final_non_success")

    return sorted(labels)


def load_cases(split_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((split_dir / "cases").glob("*.json")):
        case = load_json(path)
        case["_path"] = str(path)
        case["_file"] = path.name
        cases.append(case)
    return cases


def summarize_tools(tool_specs: dict[str, Any]) -> dict[str, Any]:
    by_domain: Counter[str] = Counter()
    required_len: Counter[int] = Counter()
    prop_len: Counter[int] = Counter()
    rows = []
    for name, spec in sorted(tool_specs.items()):
        schema = spec.get("args_schema") or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        by_domain[tool_domain(name)] += 1
        required_len[len(required)] += 1
        prop_len[len(props)] += 1
        rows.append(
            {
                "name": name,
                "domain": tool_domain(name),
                "kind": "write" if is_write_tool(name) else "read",
                "required": required,
                "properties": list(props),
                "property_types": {
                    key: value.get("type") if isinstance(value, dict) else None
                    for key, value in props.items()
                },
                "description": spec.get("description", ""),
            }
        )
    return {
        "tool_count": len(tool_specs),
        "by_domain": dict(by_domain),
        "required_arg_count_distribution": dict(sorted(required_len.items())),
        "property_count_distribution": dict(sorted(prop_len.items())),
        "tools": rows,
    }


def summarize_workflow_data(data: dict[str, Any]) -> dict[str, Any]:
    schema_rows = []
    browser_options = data.get("workflow_browser_options") or {}
    for workflow_id, schema in sorted((data.get("workflow_schemas") or {}).items()):
        fields = schema.get("fields") or []
        detail_tables = schema.get("detail_tables") or {}
        field_types = Counter(field.get("type") for field in fields)
        recommended = Counter(field.get("recommended_input") for field in fields)
        detail_rows = []
        for table_id, table in sorted(detail_tables.items()):
            table_fields = table.get("fields") or []
            detail_rows.append(
                {
                    "table_id": table_id,
                    "required_fields": table.get("required_fields") or [],
                    "field_count": len(table_fields),
                    "field_types": dict(Counter(field.get("type") for field in table_fields)),
                    "fields": [
                        {
                            "key": field.get("key"),
                            "label": field.get("label"),
                            "type": field.get("type"),
                            "required": field.get("required"),
                            "depends_on": field.get("depends_on") or [],
                        }
                        for field in table_fields
                    ],
                }
            )
        schema_rows.append(
            {
                "workflow_id": workflow_id,
                "required_fields": schema.get("required_fields") or [],
                "field_count": len(fields),
                "field_types": dict(field_types),
                "recommended_input": dict(recommended),
                "fields": [
                    {
                        "key": field.get("key"),
                        "label": field.get("label"),
                        "type": field.get("type"),
                        "required": field.get("required"),
                        "readonly": field.get("readonly"),
                        "recommended_input": field.get("recommended_input"),
                        "depends_on": field.get("depends_on") or [],
                    }
                    for field in fields
                ],
                "option_lists": {
                    key: len(value)
                    for key, value in schema.items()
                    if key.endswith("_options") and isinstance(value, list)
                },
                "detail_tables": detail_rows,
            }
        )
    return {
        "catalog": data.get("workflow_catalog") or [],
        "schema_count": len(data.get("workflow_schemas") or {}),
        "schemas": schema_rows,
        "browser_option_keys": sorted(browser_options),
        "browser_option_counts": {key: len(value) for key, value in sorted(browser_options.items())},
    }


def summarize_meetingroom_data(data: dict[str, Any]) -> dict[str, Any]:
    rooms = data.get("rooms") or {}
    capacities = [room.get("capacity") for room in rooms.values() if isinstance(room.get("capacity"), int)]
    features: Counter[str] = Counter()
    for room in rooms.values():
        features.update(room.get("features") or [])
    office_ids = Counter(room.get("officeId") for room in rooms.values())
    building_capacity: dict[str, list[int]] = defaultdict(list)
    for room in rooms.values():
        building_capacity[str(room.get("building"))].append(int(room.get("capacity") or 0))
    return {
        "room_count": len(rooms),
        "unique_office_ids": len(office_ids),
        "duplicate_office_ids": {key: count for key, count in office_ids.items() if count > 1},
        "capacity": quantiles(capacities),
        "capacity_distribution": dict(sorted(Counter(capacities).items())),
        "campus": dict(Counter(room.get("campus") for room in rooms.values())),
        "location": dict(Counter(room.get("location") for room in rooms.values())),
        "building": dict(Counter(str(room.get("building")) for room in rooms.values())),
        "floor": dict(Counter(str(room.get("floor")) for room in rooms.values())),
        "bookable": {str(k): v for k, v in Counter(room.get("bookable") for room in rooms.values()).items()},
        "has_screen": {str(k): v for k, v in Counter(room.get("hasScreen") for room in rooms.values()).items()},
        "features": dict(features),
        "building_capacity_avg": {
            key: round(statistics.mean(values), 2) for key, values in sorted(building_capacity.items())
        },
    }


def summarize_split(split_name: str, split_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases = load_cases(split_dir)
    case_rows: list[dict[str, Any]] = []
    counters: dict[str, Counter[Any]] = defaultdict(Counter)
    number_lists: dict[str, list[int | float]] = defaultdict(list)
    sequence_counter: Counter[str] = Counter()
    tool_bigram_counter: Counter[str] = Counter()
    workflow_save_keys: dict[str, Counter[str]] = defaultdict(Counter)
    workflow_detail_keys: dict[str, Counter[str]] = defaultdict(Counter)
    workflow_submit: Counter[str] = Counter()
    browser_search: Counter[str] = Counter()
    project_search_keys: Counter[str] = Counter()
    meeting_create_keys: Counter[str] = Counter()
    meeting_list_keys: Counter[str] = Counter()
    meeting_room_filters: Counter[str] = Counter()
    final_leaf_keys: Counter[str] = Counter()
    final_statuses: Counter[str] = Counter()
    success_canonical: Counter[str] = Counter()
    forbidden_canonical: Counter[str] = Counter()

    for case in cases:
        case_id = str(case.get("case_id"))
        gold = [step for step in case.get("gold_trajectory") or [] if isinstance(step, dict)]
        tools = [str(step.get("tool", "<missing>")) for step in gold]
        labels = classify_case(case)
        budget = int((case.get("scoring") or {}).get("step_budget") or 0)
        write_count = sum(1 for tool in tools if is_write_tool(tool))
        reply_count = tools.count("__reply__")
        read_count = len(tools) - write_count - reply_count
        domains = sorted({tool_domain(tool) for tool in tools})
        sequence = " -> ".join(tools)
        ref = case.get("reference_final_answer") or {}
        must = (case.get("success_check") or {}).get("must_satisfy") or []
        forbidden = (case.get("success_check") or {}).get("forbidden") or []

        counters["difficulty"][case.get("difficulty", "<missing>")] += 1
        counters["mode"][case.get("mode", "single_turn")] += 1
        counters["prefix"][case_prefix(case_id)] += 1
        for domain in case.get("primary_domains") or []:
            counters["primary_domain"][domain] += 1
        for tag in case.get("tags") or []:
            counters["tags"][tag] += 1
        for label in labels:
            counters["labels"][label] += 1
        for tool in tools:
            counters["gold_tools"][tool] += 1
            counters["gold_tool_presence_by_case"][tool] += 0
        for tool in sorted(set(tools)):
            counters["gold_tool_presence_by_case"][tool] += 1
        for idx in range(len(tools) - 1):
            tool_bigram_counter[f"{tools[idx]} -> {tools[idx + 1]}"] += 1
        for key in ref:
            counters["final_answer_top_keys"][key] += 1
        for key in flattened_leaf_keys(ref):
            final_leaf_keys[key] += 1
        for status in nested_statuses(ref):
            final_statuses[status] += 1
        for condition in must:
            success_canonical[canonicalize_success_condition(condition)] += 1
        for condition in forbidden:
            forbidden_canonical[canonicalize_success_condition(condition)] += 1

        number_lists["step_budget"].append(budget)
        number_lists["gold_steps"].append(len(gold))
        number_lists["gold_read_steps"].append(read_count)
        number_lists["gold_write_steps"].append(write_count)
        number_lists["gold_reply_steps"].append(reply_count)
        number_lists["budget_slack"].append(budget - len(gold))
        number_lists["query_chars"].append(len(str(case.get("user_query") or "")))
        number_lists["must_satisfy_count"].append(len(must))
        number_lists["forbidden_count"].append(len(forbidden))
        number_lists["world_meetingroom_refs"].append(len((case.get("world_state") or {}).get("meetingroom_refs") or []))
        number_lists["world_seed_bookings"].append(len((case.get("world_state") or {}).get("meetingroom_seed_bookings") or []))
        number_lists["world_workflow_people"].append(len((case.get("world_state") or {}).get("workflow_people") or []))
        number_lists["dialogue_missing_slots"].append(
            len((case.get("dialogue_state") or {}).get("missing_slots") or [])
        )
        sequence_counter[sequence] += 1

        for step in gold:
            tool = step.get("tool")
            args = step.get("args") or {}
            if tool == "workflow.save":
                workflow_id = str(args.get("workflow_id") or args.get("name") or "<missing>")
                workflow_submit[str(bool(args.get("submit")))] += 1
                data = args.get("data") or {}
                if isinstance(data, dict):
                    for key, value in data.items():
                        if isinstance(value, list):
                            workflow_detail_keys[f"{workflow_id}:{key}"].update(
                                leaf.split(".")[-1]
                                for item in value
                                for leaf in flattened_leaf_keys(item)
                            )
                        else:
                            workflow_save_keys[workflow_id][key] += 1
            elif tool == "workflow.browser_search":
                dep = args.get("dep") or {}
                dep_key = ",".join(f"{k}={v}" for k, v in sorted(dep.items())) if isinstance(dep, dict) else str(dep)
                browser_search[f"workflow_id={args.get('workflow_id')} field_id={args.get('field_id')} dep={dep_key or '<none>'}"] += 1
            elif tool == "workflow.project_search":
                project_search_keys.update(args.keys())
            elif tool == "meetingroom.booking.create":
                meeting_create_keys.update(args.keys())
            elif tool == "meetingroom.room.list":
                meeting_list_keys.update(args.keys())
                location = (
                    args.get("office_id")
                    or args.get("office_address")
                    or args.get("office_name")
                    or "<none>"
                )
                meeting_room_filters[str(location)] += 1

        row = {
            "split": split_name,
            "case_id": case_id,
            "file": case.get("_file", ""),
            "prefix": case_prefix(case_id),
            "difficulty": case.get("difficulty", ""),
            "mode": case.get("mode", "single_turn"),
            "scenario": case.get("scenario", ""),
            "primary_domains": ",".join(case.get("primary_domains") or []),
            "tags": ",".join(case.get("tags") or []),
            "labels": ",".join(labels),
            "now": case.get("now", ""),
            "query_chars": len(str(case.get("user_query") or "")),
            "step_budget": budget,
            "gold_steps": len(gold),
            "gold_read_steps": read_count,
            "gold_write_steps": write_count,
            "gold_reply_steps": reply_count,
            "budget_slack": budget - len(gold),
            "unique_tool_count": len(set(tools)),
            "tool_domains": ",".join(domains),
            "tool_sequence": sequence,
            "final_answer_keys": ",".join(ref.keys()),
            "final_statuses": ",".join(nested_statuses(ref)),
            "must_satisfy_count": len(must),
            "forbidden_count": len(forbidden),
            "world_meetingroom_refs": len((case.get("world_state") or {}).get("meetingroom_refs") or []),
            "world_seed_bookings": len((case.get("world_state") or {}).get("meetingroom_seed_bookings") or []),
            "world_workflow_people": len((case.get("world_state") or {}).get("workflow_people") or []),
            "dialogue_missing_slots": len((case.get("dialogue_state") or {}).get("missing_slots") or []),
        }
        case_rows.append(row)

    split_summary = {
        "split": split_name,
        "case_count": len(cases),
        "hashes": {
            "workflow_data": sha256(split_dir / "data" / "workflow_data.json"),
            "meetingroom_data": sha256(split_dir / "data" / "meetingroom_data.json"),
            "tool_specs": sha256(split_dir / "tool_specs.json"),
        },
        "distributions": {
            key: dict(counter)
            for key, counter in counters.items()
            if key not in {"gold_tools", "gold_tool_presence_by_case"}
        },
        "numeric": {key: quantiles(values) for key, values in number_lists.items()},
        "histograms": {
            "step_budget": dict(sorted(Counter(number_lists["step_budget"]).items())),
            "gold_steps": dict(sorted(Counter(number_lists["gold_steps"]).items())),
            "budget_slack": dict(sorted(Counter(number_lists["budget_slack"]).items())),
            "gold_write_steps": dict(sorted(Counter(number_lists["gold_write_steps"]).items())),
            "gold_reply_steps": dict(sorted(Counter(number_lists["gold_reply_steps"]).items())),
        },
        "gold_tools": {
            "call_counts": sorted_counter(counters["gold_tools"]),
            "presence_by_case": sorted_counter(counters["gold_tool_presence_by_case"]),
            "sequence_top": [{"sequence": seq, "count": count} for seq, count in sequence_counter.most_common(30)],
            "bigram_top": [{"bigram": seq, "count": count} for seq, count in tool_bigram_counter.most_common(30)],
        },
        "gold_args": {
            "workflow_save_top_level_fields": {
                key: dict(value.most_common()) for key, value in sorted(workflow_save_keys.items())
            },
            "workflow_detail_fields": {
                key: dict(value.most_common()) for key, value in sorted(workflow_detail_keys.items())
            },
            "workflow_submit_counts": dict(workflow_submit),
            "browser_search_top": sorted_counter(browser_search),
            "project_search_arg_keys": dict(project_search_keys),
            "meeting_create_arg_keys": dict(meeting_create_keys),
            "meeting_room_list_arg_keys": dict(meeting_list_keys),
            "meeting_room_list_filters": dict(meeting_room_filters),
        },
        "success_checks": {
            "must_satisfy_top": sorted_counter(success_canonical),
            "forbidden_top": sorted_counter(forbidden_canonical),
        },
        "final_answer": {
            "top_keys": dict(counters["final_answer_top_keys"]),
            "leaf_keys_top": sorted_counter(final_leaf_keys),
            "statuses": dict(final_statuses),
        },
    }
    return split_summary, case_rows


def compare_counter(train: dict[str, int], val: dict[str, int], train_total: int, val_total: int) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(set(train) | set(val)):
        train_count = int(train.get(key, 0))
        val_count = int(val.get(key, 0))
        train_pct = pct(train_count, train_total)
        val_pct = pct(val_count, val_total)
        rows.append(
            {
                "key": key,
                "train": train_count,
                "train_pct": train_pct,
                "val": val_count,
                "val_pct": val_pct,
                "delta_pp": round(val_pct - train_pct, 2),
                "val_over_train_ratio": round((val_pct / train_pct), 2) if train_pct else None,
            }
        )
    return sorted(rows, key=lambda row: (abs(row["delta_pp"]), row["val"]), reverse=True)


def build_comparison(train: dict[str, Any], val: dict[str, Any]) -> dict[str, Any]:
    train_total = train["case_count"]
    val_total = val["case_count"]
    compare_keys = ["difficulty", "mode", "prefix", "primary_domain", "tags", "labels", "final_answer_top_keys"]
    comparisons = {}
    for key in compare_keys:
        train_counter = train["distributions"].get(key) or train["final_answer"].get("top_keys") or {}
        val_counter = val["distributions"].get(key) or val["final_answer"].get("top_keys") or {}
        if key == "final_answer_top_keys":
            train_counter = train["final_answer"]["top_keys"]
            val_counter = val["final_answer"]["top_keys"]
        comparisons[key] = compare_counter(train_counter, val_counter, train_total, val_total)

    train_tool_presence = {row["key"]: row["count"] for row in train["gold_tools"]["presence_by_case"]}
    val_tool_presence = {row["key"]: row["count"] for row in val["gold_tools"]["presence_by_case"]}
    comparisons["gold_tool_presence"] = compare_counter(
        train_tool_presence,
        val_tool_presence,
        train_total,
        val_total,
    )
    return {
        "case_ratio_val_over_train": round(val_total / train_total, 3) if train_total else None,
        "hash_equal": {
            key: train["hashes"][key] == val["hashes"][key] for key in train["hashes"]
        },
        "counter_comparisons": comparisons,
        "numeric_comparison": {
            key: {
                "train": train["numeric"][key],
                "val": val["numeric"][key],
                "avg_delta": round(val["numeric"][key].get("avg", 0) - train["numeric"][key].get("avg", 0), 2),
            }
            for key in sorted(set(train["numeric"]) & set(val["numeric"]))
        },
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def md_counter_table(counter: dict[str, int], total: int, limit: int = 20) -> list[str]:
    rows = []
    for key, count in Counter(counter).most_common(limit):
        rows.append([f"`{key}`", count, f"{pct(count, total)}%"])
    return md_table(["项", "数量", "占比"], rows)


def md_comparison_table(rows: list[dict[str, Any]], limit: int = 20) -> list[str]:
    body = []
    for row in rows[:limit]:
        ratio = row["val_over_train_ratio"]
        body.append(
            [
                f"`{row['key']}`",
                f"{row['train']} ({row['train_pct']}%)",
                f"{row['val']} ({row['val_pct']}%)",
                row["delta_pp"],
                "" if ratio is None else ratio,
            ]
        )
    return md_table(["项", "train", "val", "val-train pp", "val/train"], body)


def format_quantile(q: dict[str, Any]) -> str:
    if not q or not q.get("count"):
        return "-"
    return (
        f"min {q['min']}, p25 {q['p25']}, median {q['median']}, "
        f"p75 {q['p75']}, max {q['max']}, avg {q['avg']}"
    )


def write_markdown(report: dict[str, Any], output: Path) -> None:
    train = report["splits"]["train"]
    val = report["splits"]["val"]
    comp = report["comparison"]
    tool_specs = report["tool_specs"]
    workflow = report["workflow_data"]
    meetingroom = report["meetingroom_data"]
    lines: list[str] = []

    lines.extend(
        [
            "# NL2Workflow 训练/验证数据深度画像报告",
            "",
            "生成日期：2026-07-08",
            "",
            "本报告只做静态数据画像，用于架构判断和回归设计。`gold_trajectory`、`success_check`、`reference_final_answer` 是公开数据里的金标准字段，只能用于统计和误差归因，不能进入线上执行逻辑或 case 记忆表。",
            "",
            "## 1. 数据源与完整性",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["对象", "train", "val", "是否一致"],
            [
                ["case 数", train["case_count"], val["case_count"], "否"],
                ["`data/workflow_data.json` SHA256", train["hashes"]["workflow_data"][:16], val["hashes"]["workflow_data"][:16], comp["hash_equal"]["workflow_data"]],
                ["`data/meetingroom_data.json` SHA256", train["hashes"]["meetingroom_data"][:16], val["hashes"]["meetingroom_data"][:16], comp["hash_equal"]["meetingroom_data"]],
                ["`tool_specs.json` SHA256", train["hashes"]["tool_specs"][:16], val["hashes"]["tool_specs"][:16], comp["hash_equal"]["tool_specs"]],
            ],
        )
    )
    lines.extend(
        [
            "",
            "结论：train/val 的业务静态数据和工具规格完全一致，差异来自 case 采样、query 改写、world_state、gold 路径和 success/final 约束。架构上应把 `data` 与 `tool_specs` 当作稳定 schema/候选层，把 case 当作任务分布层。",
            "",
            "## 2. Case 总体分布",
            "",
            "### 2.1 难度、模式、前缀",
            "",
            "难度分布：",
        ]
    )
    lines.extend(md_comparison_table(comp["counter_comparisons"]["difficulty"], 10))
    lines.extend(["", "对话模式："])
    lines.extend(md_comparison_table(comp["counter_comparisons"]["mode"], 10))
    lines.extend(["", "文件前缀："])
    lines.extend(md_comparison_table(comp["counter_comparisons"]["prefix"], 20))
    lines.extend(
        [
            "",
            "### 2.2 主域与标签",
            "",
            "主域分布：",
        ]
    )
    lines.extend(md_comparison_table(comp["counter_comparisons"]["primary_domain"], 20))
    lines.extend(["", "高频标签与派生分类差异："])
    lines.extend(md_comparison_table(comp["counter_comparisons"]["labels"], 35))
    lines.extend(
        [
            "",
            "读法：`val-train pp` 为验证集占比减训练集占比。验证集虽只有 50 条，但 hard、workflow、跨域、candidate/blocked、project/browser option lookup 的权重是否上升，直接决定架构优先级。",
            "",
            "## 3. 步骤长度与预算",
            "",
        ]
    )
    numeric_rows = []
    for key, label in [
        ("step_budget", "step_budget"),
        ("gold_steps", "gold_trajectory 步数"),
        ("gold_read_steps", "金标准读工具步"),
        ("gold_write_steps", "金标准写工具步"),
        ("gold_reply_steps", "金标准 reply 步"),
        ("budget_slack", "预算冗余 step_budget - gold_steps"),
        ("query_chars", "用户 query 字符数"),
        ("must_satisfy_count", "must_satisfy 条数"),
        ("forbidden_count", "forbidden 条数"),
    ]:
        numeric_rows.append(
            [
                label,
                format_quantile(train["numeric"][key]),
                format_quantile(val["numeric"][key]),
                comp["numeric_comparison"][key]["avg_delta"],
            ]
        )
    lines.extend(md_table(["指标", "train", "val", "平均差值"], numeric_rows))
    lines.extend(["", "gold 步数直方图："])
    lines.extend(
        md_table(
            ["步数", "train", "val"],
            [
                [step, train["histograms"]["gold_steps"].get(step, 0), val["histograms"]["gold_steps"].get(step, 0)]
                for step in sorted(set(train["histograms"]["gold_steps"]) | set(val["histograms"]["gold_steps"]))
            ],
        )
    )
    lines.extend(["", "预算冗余直方图："])
    lines.extend(
        md_table(
            ["冗余", "train", "val"],
            [
                [slack, train["histograms"]["budget_slack"].get(slack, 0), val["histograms"]["budget_slack"].get(slack, 0)]
                for slack in sorted(set(train["histograms"]["budget_slack"]) | set(val["histograms"]["budget_slack"]))
            ],
        )
    )
    lines.extend(
        [
            "",
            "架构含义：gold 步数接近预算的题目，不能把 LLM 多轮思考放在工具执行之后；需要先用轻量 task graph 决策哪些 read 是必须的，写操作前保留 preflight 和 final answer 的固定预算。",
            "",
            "## 4. 金标准轨迹画像",
            "",
            "### 4.1 工具调用频率",
            "",
            "按调用次数：",
        ]
    )
    lines.extend(md_counter_table({r["key"]: r["count"] for r in train["gold_tools"]["call_counts"]}, sum(r["count"] for r in train["gold_tools"]["call_counts"]), 15))
    lines.extend(["", "验证集按调用次数："])
    lines.extend(md_counter_table({r["key"]: r["count"] for r in val["gold_tools"]["call_counts"]}, sum(r["count"] for r in val["gold_tools"]["call_counts"]), 15))
    lines.extend(["", "按 case 覆盖率对比："])
    lines.extend(md_comparison_table(comp["counter_comparisons"]["gold_tool_presence"], 25))
    lines.extend(["", "高频工具序列 train："])
    lines.extend(
        md_table(
            ["序列", "数量"],
            [[f"`{row['sequence']}`", row["count"]] for row in train["gold_tools"]["sequence_top"][:15]],
        )
    )
    lines.extend(["", "高频工具序列 val："])
    lines.extend(
        md_table(
            ["序列", "数量"],
            [[f"`{row['sequence']}`", row["count"]] for row in val["gold_tools"]["sequence_top"][:15]],
        )
    )
    lines.extend(
        [
            "",
            "### 4.2 Workflow 金标准字段",
            "",
            "workflow.save 顶层字段覆盖：",
        ]
    )
    for split_name, split in [("train", train), ("val", val)]:
        lines.append("")
        lines.append(f"{split_name}:")
        for workflow_id, fields in split["gold_args"]["workflow_save_top_level_fields"].items():
            total = sum(fields.values())
            lines.append(f"- workflow `{workflow_id}` save 次数字段总计 `{total}`，字段：`{fields}`")
        detail = split["gold_args"]["workflow_detail_fields"]
        if detail:
            for key, fields in detail.items():
                lines.append(f"- detail `{key}` 字段：`{fields}`")
        lines.append(f"- submit 布尔分布：`{split['gold_args']['workflow_submit_counts']}`")
    lines.extend(
        [
            "",
            "workflow.browser_search 高频目标：",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["split", "目标", "次数"],
            [["train", f"`{row['key']}`", row["count"]] for row in train["gold_args"]["browser_search_top"][:12]]
            + [["val", f"`{row['key']}`", row["count"]] for row in val["gold_args"]["browser_search_top"][:12]],
        )
    )
    lines.extend(
        [
            "",
            "### 4.3 Meetingroom 金标准参数",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["参数族", "train", "val"],
            [
                ["booking.create args", f"`{train['gold_args']['meeting_create_arg_keys']}`", f"`{val['gold_args']['meeting_create_arg_keys']}`"],
                ["room.list args", f"`{train['gold_args']['meeting_room_list_arg_keys']}`", f"`{val['gold_args']['meeting_room_list_arg_keys']}`"],
                ["room.list location filters top train", f"`{dict(Counter(train['gold_args']['meeting_room_list_filters']).most_common(8))}`", ""],
                ["room.list location filters top val", "", f"`{dict(Counter(val['gold_args']['meeting_room_list_filters']).most_common(8))}`"],
            ],
        )
    )
    lines.extend(
        [
            "",
            "注意：`meetingroom.booking.create` 的 tool spec 要求 `room_id`，但公开 gold 中存在只给 `office_id` 或把 room/office 标识混用的历史形态。执行器不应按公开 case 固定 room_id，而应用 `room.list/schedule` 的真实候选建立兼容映射。",
            "",
            "## 5. Final Answer 与成功条件",
            "",
            "final_answer 顶层 key：",
        ]
    )
    lines.extend(md_comparison_table(comp["counter_comparisons"]["final_answer_top_keys"], 20))
    lines.extend(["", "final_answer 状态分布："])
    lines.extend(
        md_table(
            ["状态", "train", "val"],
            [
                [f"`{key}`", train["final_answer"]["statuses"].get(key, 0), val["final_answer"]["statuses"].get(key, 0)]
                for key in sorted(set(train["final_answer"]["statuses"]) | set(val["final_answer"]["statuses"]))
            ],
        )
    )
    lines.extend(["", "高频 must_satisfy 模板 train："])
    lines.extend(md_table(["模板", "数量"], [[f"`{row['key']}`", row["count"]] for row in train["success_checks"]["must_satisfy_top"][:18]]))
    lines.extend(["", "高频 must_satisfy 模板 val："])
    lines.extend(md_table(["模板", "数量"], [[f"`{row['key']}`", row["count"]] for row in val["success_checks"]["must_satisfy_top"][:18]]))
    lines.extend(["", "高频 forbidden 模板："])
    lines.extend(
        md_table(
            ["模板", "train", "val"],
            [
                [f"`{key}`", Counter({r["key"]: r["count"] for r in train["success_checks"]["forbidden_top"]}).get(key, 0), Counter({r["key"]: r["count"] for r in val["success_checks"]["forbidden_top"]}).get(key, 0)]
                for key in sorted(
                    set(r["key"] for r in train["success_checks"]["forbidden_top"][:20])
                    | set(r["key"] for r in val["success_checks"]["forbidden_top"][:20])
                )
            ],
        )
    )
    lines.extend(
        [
            "",
            "## 6. `data` 画像",
            "",
            "### 6.1 Workflow data",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["workflow_id", "required", "字段类型", "推荐输入", "选项列表", "明细表"],
            [
                [
                    f"`{schema['workflow_id']}`",
                    f"`{schema['required_fields']}`",
                    f"`{schema['field_types']}`",
                    f"`{schema['recommended_input']}`",
                    f"`{schema['option_lists']}`",
                    ", ".join(f"`{t['table_id']}` {t['required_fields']}" for t in schema["detail_tables"]) or "-",
                ]
                for schema in workflow["schemas"]
            ],
        )
    )
    lines.extend(["", "browser option keys："])
    lines.extend(md_table(["key", "选项数"], [[f"`{key}`", count] for key, count in workflow["browser_option_counts"].items()]))
    lines.extend(
        [
            "",
            "Workflow data 的核心信号：公开数据只有两个流程 schema，但费用类流程已经包含项目 lookup、物资大类/小类 browser、费用类型、财务二级科目和 detail table。隐藏集若增加 workflow 类型或字段变化，硬编码 `72247/34747/detail_2` 会失效；执行器应按 catalog/schema/browser options 动态组装。",
            "",
            "### 6.2 Meetingroom data",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["指标", "值"],
            [
                ["room_count", meetingroom["room_count"]],
                ["unique_office_ids", meetingroom["unique_office_ids"]],
                ["capacity", format_quantile(meetingroom["capacity"])],
                ["campus", f"`{meetingroom['campus']}`"],
                ["bookable", f"`{meetingroom['bookable']}`"],
                ["has_screen", f"`{meetingroom['has_screen']}`"],
                ["features", f"`{meetingroom['features']}`"],
            ],
        )
    )
    lines.extend(["", "building 分布："])
    lines.extend(md_counter_table(meetingroom["building"], meetingroom["room_count"], 20))
    lines.extend(["", "capacity 分布："])
    lines.extend(md_counter_table({str(k): v for k, v in meetingroom["capacity_distribution"].items()}, meetingroom["room_count"], 30))
    lines.extend(
        [
            "",
            "Meetingroom data 的核心信号：候选空间不大但存在地址别名、building 为空/None、不可订房间、容量和屏幕约束、以及 seed booking 冲突。架构上需要把自然语言地点映射、候选过滤、冲突验证、room_id/officeId 归一化拆成独立层。",
            "",
            "## 7. `tool_specs` 画像",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["指标", "值"],
            [
                ["工具总数", tool_specs["tool_count"]],
                ["按域", f"`{tool_specs['by_domain']}`"],
                ["required 参数数分布", f"`{tool_specs['required_arg_count_distribution']}`"],
                ["properties 数分布", f"`{tool_specs['property_count_distribution']}`"],
            ],
        )
    )
    lines.extend(["", "工具明细："])
    lines.extend(
        md_table(
            ["工具", "类型", "required", "properties"],
            [
                [
                    f"`{tool['name']}`",
                    tool["kind"],
                    f"`{tool['required']}`",
                    f"`{tool['properties']}`",
                ]
                for tool in tool_specs["tools"]
            ],
        )
    )
    lines.extend(
        [
            "",
            "Tool spec 的核心信号：工具 schema 只能给参数形状，不能替代业务 schema。`workflow.schema`、`workflow.browser_search`、`workflow.project_search`、`meetingroom.room.list/schedule/booking.list` 的返回值才是写操作证据链。",
            "",
            "## 8. Train vs Val 差异总结",
            "",
            "- `data` 和 `tool_specs` 完全相同，验证集不是新工具或新静态业务库，而是同工具/同数据下的任务组合和表达变化。",
            "- 验证集规模是训练集的 25%，适合做代表性回归，但不适合做 case 记忆。",
            "- 需要优先看 val 相对 train 上升的类别：这些类别是公开验证对架构施压最大的地方。",
            "- gold 轨迹显示高分路径不是单纯 final_answer 文本，而是 read evidence -> write action -> final_answer 一致。",
            "- 多轮、候选/blocked、跨域、browser/project lookup、明细金额守恒，是最容易在隐藏集扩展的能力轴。",
            "",
            "## 9. 架构建议",
            "",
            "1. 用 task graph 作为第一层，而不是按前缀或 case 类型路由。每个子任务记录 domain、intent、缺槽、风险和依赖。",
            "2. Workflow executor 必须 schema-driven：catalog/schema 确定流程和字段，browser/project/person search 提供候选，preflight 校验 required、枚举、金额和 detail rows。",
            "3. Meetingroom executor 拆成地点解析、候选召回、冲突验证、写操作四层；room_id、office_id、officeId 统一做证据归一化。",
            "4. Final answer 只从 ledger 生成，不让 LLM 直接编最终答案；否则容易工具做对但答案字段不一致。",
            "5. LLM 应主要用于 task graph、候选排序、schema 字段草案，不负责直接编造 id/code/value。",
            "6. 预算策略要按 gold 步数分布设计：复杂题先压缩 read set，写前保留 preflight，超过时间阈值后进入稳定 blocked/partial 收口。",
            "",
            "## 10. 输出文件",
            "",
            f"- JSON 统计：`{output.with_suffix('.json')}`",
            f"- Case 明细 CSV：`{output.with_name(output.name.replace('profile', 'cases')).with_suffix('.csv')}`",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_case_csv(rows: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        output.write_text("", encoding="utf-8")
        return
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-dir", type=Path, default=ROOT / "contest" / "train")
    parser.add_argument("--val-dir", type=Path, default=ROOT / "contest" / "val")
    parser.add_argument("--out-prefix", type=Path, default=DEFAULT_OUT_PREFIX)
    args = parser.parse_args()

    train_summary, train_rows = summarize_split("train", args.train_dir)
    val_summary, val_rows = summarize_split("val", args.val_dir)
    train_tools = load_json(args.train_dir / "tool_specs.json")
    val_tools = load_json(args.val_dir / "tool_specs.json")
    if train_tools != val_tools:
        raise RuntimeError("train and val tool_specs differ; script expects one shared profile")
    train_workflow = load_json(args.train_dir / "data" / "workflow_data.json")
    val_workflow = load_json(args.val_dir / "data" / "workflow_data.json")
    if train_workflow != val_workflow:
        raise RuntimeError("train and val workflow_data differ; script expects one shared profile")
    train_meetingroom = load_json(args.train_dir / "data" / "meetingroom_data.json")
    val_meetingroom = load_json(args.val_dir / "data" / "meetingroom_data.json")
    if train_meetingroom != val_meetingroom:
        raise RuntimeError("train and val meetingroom_data differ; script expects one shared profile")

    report = {
        "splits": {"train": train_summary, "val": val_summary},
        "comparison": build_comparison(train_summary, val_summary),
        "tool_specs": summarize_tools(train_tools),
        "workflow_data": summarize_workflow_data(train_workflow),
        "meetingroom_data": summarize_meetingroom_data(train_meetingroom),
    }

    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out_prefix.with_suffix(".json")
    md_path = args.out_prefix.with_suffix(".md")
    csv_path = args.out_prefix.with_name(args.out_prefix.name.replace("profile", "cases")).with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_case_csv(train_rows + val_rows, csv_path)
    write_markdown(report, md_path)
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
