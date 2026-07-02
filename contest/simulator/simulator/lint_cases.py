"""Case lint helpers for contest cases."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

try:
    from .env import IFTKEnv
except ImportError:
    from env import IFTKEnv


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _contains_expected(payload: Any, expected_parts: list[str]) -> bool:
    haystack = json.dumps(payload, ensure_ascii=False)
    return all(part in haystack for part in expected_parts)


def _validate_refs(
    case_data: dict[str, Any],
    workflow_data: dict[str, Any],
    meetingroom_data: dict[str, Any],
) -> dict[str, Any]:
    state = case_data.get("world_state", {})
    meetingroom_refs = state.get("meetingroom_refs", [])
    rooms = meetingroom_data.get("rooms", {})
    missing_meetingroom_refs = [ref for ref in meetingroom_refs if ref not in rooms]

    workflow_refs = case_data.get("workflow_refs", [])
    shared_catalog_ids = {
        item.get("workflow_id")
        for item in workflow_data.get("workflow_catalog", [])
    }
    shared_schema_ids = set(workflow_data.get("workflow_schemas", {}).keys())
    missing_workflow_refs = [ref for ref in workflow_refs if ref not in shared_catalog_ids]
    missing_workflow_schemas = [
        ref for ref in workflow_refs if str(ref) not in shared_schema_ids
    ]

    refs_ok = (
        not missing_meetingroom_refs
        and not missing_workflow_refs
        and not missing_workflow_schemas
    )
    return {
        "refs_ok": refs_ok,
        "missing_meetingroom_refs": missing_meetingroom_refs,
        "missing_workflow_refs": missing_workflow_refs,
        "missing_workflow_schemas": missing_workflow_schemas,
    }


def _run_gold_trajectory(env: IFTKEnv, case_data: dict[str, Any]) -> dict[str, Any]:
    step_reports = []
    errors = []
    expected_mismatches = []

    for index, step in enumerate(case_data.get("gold_trajectory", []), start=1):
        tool = step["tool"]
        args = step.get("args", {})
        expected_parts = step.get("expected_observation_contains", [])
        if tool == "__reply__":
            result = env.reply(args.get("message", ""))
        else:
            result = env.call_tool(tool, args)

        has_error = isinstance(result, dict) and "error" in result
        matched = _contains_expected(result, expected_parts)
        report = {
            "index": index,
            "tool": tool,
            "has_error": has_error,
            "expected_match": matched,
            "result": copy.deepcopy(result),
        }
        step_reports.append(report)
        if has_error:
            errors.append({"index": index, "tool": tool, "error": result.get("error")})
        if expected_parts and not matched:
            expected_mismatches.append(
                {
                    "index": index,
                    "tool": tool,
                    "expected_observation_contains": expected_parts,
                }
            )

    return {
        "gold_steps": step_reports,
        "gold_errors": errors,
        "gold_expected_mismatches": expected_mismatches,
        "gold_trajectory_ok": not errors and not expected_mismatches,
    }


def _has_time_conflict(start: str, end: str, busy_slots: list[list[str]]) -> bool:
    return any(not (end <= slot_start or start >= slot_end) for slot_start, slot_end in busy_slots)


def _infer_room_floor(room: dict[str, Any]) -> str | None:
    import re

    floor = room.get("floor")
    if floor:
        return str(floor)

    room_id = str(room.get("room_id", ""))
    room_id_match = re.search(r"-(\d+F)-", room_id)
    if room_id_match:
        return room_id_match.group(1)

    name = str(room.get("name", ""))
    name_match = re.search(r"(\d+)楼", name)
    if name_match:
        return f"{name_match.group(1)}F"
    return None


def _infer_room_area(room: dict[str, Any]) -> str | None:
    area = room.get("area")
    if area:
        return str(area)

    name = str(room.get("name", ""))
    if "北区" in name:
        return "北区"
    if "南区" in name:
        return "南区"
    return None


def _workspace_context(case_data: dict[str, Any]) -> dict[str, str | None]:
    state = case_data.get("world_state", {})
    users = state.get("users", [])
    current_user_id = state.get("current_user_id")
    user = next((item for item in users if item.get("user_id") == current_user_id), None)
    if user is None:
        user = users[0] if users else None
    workspace = user.get("workspace", {}) if user else {}

    office_address = str(workspace.get("office_address", ""))
    office_name = str(workspace.get("office_name", ""))
    parts = office_address.split("_") if office_address else []
    campus = None
    if parts:
        if parts[0] == "0552":
            campus = "小镇"
        elif parts[0] == "0551":
            campus = "合肥"

    area = workspace.get("area")
    if not area:
        if "北区" in office_name:
            area = "北区"
        elif "南区" in office_name:
            area = "南区"

    return {
        "campus": campus,
        "building": parts[1] if len(parts) >= 2 else None,
        "floor": parts[2] if len(parts) >= 3 else None,
        "area": str(area) if area else None,
    }


def _room_workspace_rank(room: dict[str, Any], workspace: dict[str, str | None]) -> tuple[int, int, int]:
    room_floor = _infer_room_floor(room)
    room_building = str(room.get("building", "")) or None
    room_area = _infer_room_area(room)
    same_floor = int(
        workspace.get("floor") is not None
        and workspace.get("building") is not None
        and room_floor == workspace.get("floor")
        and room_building == workspace.get("building")
    )
    same_building = int(
        workspace.get("building") is not None
        and room_building == workspace.get("building")
    )
    same_area = int(
        workspace.get("area") is not None
        and room_area == workspace.get("area")
    )
    return (same_floor, same_building, same_area)


def _validate_unique_meetingroom_candidate(
    case_data: dict[str, Any],
    gold_report: dict[str, Any],
) -> dict[str, Any]:
    if "meetingroom" not in set(case_data.get("primary_domains", [])):
        return {"meetingroom_candidate_unique": True, "legal_room_candidates": []}

    create_step = next(
        (step for step in case_data.get("gold_trajectory", []) if step.get("tool") == "meetingroom.booking.create"),
        None,
    )
    create_index = next(
        (
            idx
            for idx, step in enumerate(case_data.get("gold_trajectory", []), start=1)
            if step.get("tool") == "meetingroom.booking.create"
        ),
        None,
    )
    room_list_result = None
    if create_index is not None:
        for step in gold_report.get("gold_steps", []):
            if step.get("tool") != "meetingroom.room.list":
                continue
            if step.get("index", 0) >= create_index:
                continue
            room_list_result = step.get("result")

    if not create_step or not room_list_result:
        return {"meetingroom_candidate_unique": True, "legal_room_candidates": []}

    start = create_step.get("args", {}).get("start")
    end = create_step.get("args", {}).get("end")
    if not start or not end:
        return {"meetingroom_candidate_unique": True, "legal_room_candidates": []}

    legal_candidates = [
        room
        for room in room_list_result.get("rooms", [])
        if room.get("bookable", True) and not _has_time_conflict(start, end, room.get("busy_slots", []))
    ]

    must_satisfy = case_data.get("success_check", {}).get("must_satisfy", [])
    if any("预订的是离工位最近的会议室" in cond for cond in must_satisfy):
        workspace = _workspace_context(case_data)
        ranked = [
            (_room_workspace_rank(room, workspace), room)
            for room in legal_candidates
        ]
        best_rank = max(rank for rank, _ in ranked)
        best_rooms = [room.get("room_id") for rank, room in ranked if rank == best_rank]
        return {
            "meetingroom_candidate_unique": len(best_rooms) == 1,
            "legal_room_candidates": [room.get("room_id") for room in legal_candidates],
            "best_room_candidates": best_rooms,
        }

    return {
        "meetingroom_candidate_unique": len(legal_candidates) == 1,
        "legal_room_candidates": [room.get("room_id") for room in legal_candidates],
    }


def lint_case_file(
    case_file: str | Path,
    *,
    tool_specs_path: str | Path,
    workflow_data_path: str | Path,
    meetingroom_data_path: str | Path,
) -> dict[str, Any]:
    case_path = Path(case_file)
    report: dict[str, Any] = {
        "case_id": case_path.stem,
        "case_file": str(case_path),
        "loadable": False,
    }

    case_data = _load_json(case_path)
    report["case_id"] = case_data.get("case_id", case_path.stem)
    workflow_data = _load_json(Path(workflow_data_path))
    meetingroom_data = _load_json(Path(meetingroom_data_path))
    report.update(_validate_refs(case_data, workflow_data, meetingroom_data))

    env = IFTKEnv(
        case_path.parent,
        tool_specs_path=tool_specs_path,
        workflow_data_path=workflow_data_path,
        meetingroom_data_path=meetingroom_data_path,
    )

    try:
        env.reset(report["case_id"])
        report["loadable"] = True
    except Exception as exc:  # pragma: no cover - defensive
        report["load_error"] = str(exc)
        report.setdefault("gold_trajectory_ok", False)
        report.setdefault("task_passed", False)
        return report

    gold_report = _run_gold_trajectory(env, case_data)
    report.update(gold_report)
    report.update(_validate_unique_meetingroom_candidate(case_data, gold_report))

    reference_final_answer = case_data.get("reference_final_answer", {})
    score = env.done(reference_final_answer)
    report["score"] = score
    report["success_checks_passed"] = all(
        item["passed"] for item in score.get("success_checks", [])
    )
    report["submission_checks_passed"] = all(
        item["passed"] for item in score.get("submission_checks", [])
    )
    report["task_passed"] = score.get("task_passed", False)
    report["ok"] = (
        report["loadable"]
        and report["refs_ok"]
        and report["gold_trajectory_ok"]
        and report["meetingroom_candidate_unique"]
        and report["task_passed"]
    )
    return report


def lint_cases_dir(
    cases_dir: str | Path,
    *,
    tool_specs_path: str | Path,
    workflow_data_path: str | Path,
    meetingroom_data_path: str | Path,
) -> dict[str, Any]:
    cases_path = Path(cases_dir)
    reports = [
        lint_case_file(
            case_file,
            tool_specs_path=tool_specs_path,
            workflow_data_path=workflow_data_path,
            meetingroom_data_path=meetingroom_data_path,
        )
        for case_file in sorted(cases_path.glob("*.json"))
    ]
    ok_count = sum(1 for report in reports if report["ok"])
    return {
        "cases_dir": str(cases_path),
        "total_cases": len(reports),
        "ok_cases": ok_count,
        "failed_cases": len(reports) - ok_count,
        "reports": reports,
    }


def main() -> int:
    base_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Lint contest case files.")
    parser.add_argument(
        "--cases-dir",
        default=str(base_dir / "cases"),
        help="Directory containing case JSON files.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to write the full JSON report.",
    )
    args = parser.parse_args()

    report = lint_cases_dir(
        args.cases_dir,
        tool_specs_path=base_dir / "tool_specs.json",
        workflow_data_path=base_dir / "data" / "workflow_data.json",
        meetingroom_data_path=base_dir / "data" / "meetingroom_data.json",
    )

    print(
        f"Linted {report['total_cases']} cases: "
        f"{report['ok_cases']} ok, {report['failed_cases']} failed"
    )
    for item in report["reports"]:
        status = "OK" if item["ok"] else "FAIL"
        print(f"[{status}] {item['case_id']}")
        if not item["refs_ok"]:
            print(
                f"  missing refs: meetingroom={item['missing_meetingroom_refs']} "
                f"workflow={item['missing_workflow_refs']} "
                f"workflow_schemas={item['missing_workflow_schemas']}"
            )
        if not item.get("gold_trajectory_ok", True):
            print(f"  gold errors: {item.get('gold_errors', [])}")
            print(f"  expected mismatches: {item.get('gold_expected_mismatches', [])}")
        if not item.get("meetingroom_candidate_unique", True):
            print(f"  legal meetingroom candidates: {item.get('legal_room_candidates', [])}")
        if item.get("load_error"):
            print(f"  load error: {item['load_error']}")
        if item.get("score") and not item["task_passed"]:
            print(
                f"  score failed: success={item['success_checks_passed']} "
                f"submission={item['submission_checks_passed']} "
                f"violations={item['score'].get('violations', [])}"
            )

    if args.json_out:
        json_out = Path(args.json_out)
        json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"JSON report written to {json_out}")

    return 0 if report["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
