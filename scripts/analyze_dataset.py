#!/usr/bin/env python3
"""分析 NL2Workflow 比赛数据集，并可汇总 runner 跑分结果。"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_cases(split_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted((split_dir / "cases").glob("*.json")):
        case = load_json(path)
        case["_path"] = str(path)
        case["_file"] = path.name
        cases.append(case)
    return cases


def classify_case(case: dict[str, Any]) -> list[str]:
    query = case.get("user_query", "")
    must_text = " ".join(case.get("success_check", {}).get("must_satisfy", []))
    ref_text = json.dumps(case.get("reference_final_answer", {}), ensure_ascii=False)
    gold_tools = [
        step.get("tool")
        for step in case.get("gold_trajectory", [])
        if isinstance(step, dict)
    ]

    labels: list[str] = []
    if "meetingroom.booking.create" in gold_tools or "存在成功会议预订" in must_text:
        labels.append("meeting_create")
    if "meetingroom.booking.cancel" in gold_tools or "存在已取消会议" in must_text:
        labels.append("meeting_cancel_rebook")
    if "meetingroom.booking.extend" in gold_tools:
        labels.append("meeting_extend")
    if (
        "meetingroom.booking.participant.add" in gold_tools
        or "meetingroom.booking.participant.remove" in gold_tools
        or "participant" in ref_text
    ):
        labels.append("participants")
    if (
        "存在会议室候选" in must_text
        or "不存在新的成功会议预订" in must_text
        or any(token in query for token in ["候选", "别直接订", "先告诉", "订不到就算", "别动"])
    ):
        labels.append("candidate_or_blocked")
    if (
        "workflow.save" in gold_tools
        or any(token in query for token in ["流程", "请假", "费用", "物资", "采购", "申请"])
    ):
        labels.append("workflow")
    if "workflow_id=72247" in must_text or "请假" in query:
        labels.append("leave")
    if (
        "workflow_id=34747" in must_text
        or "费用类物资" in must_text
        or any(token in query for token in ["费用", "物资", "采购", "广告", "印刷", "外包", "设备"])
    ):
        labels.append("expense")
    if case.get("mode", "single_turn") == "multi_turn":
        labels.append("multi_turn")
    return labels or ["other"]


def condition_bucket(condition: str) -> str:
    text = re.sub(r"[，,].*", "", condition)
    return re.sub(r"\s+", " ", text).strip()


def split_summary(split_name: str, split_dir: Path) -> dict[str, Any]:
    cases = load_cases(split_dir)
    if not cases:
        return {"split": split_name, "case_count": 0}

    budgets = [case["scoring"]["step_budget"] for case in cases]
    difficulties = Counter(case.get("difficulty", "<missing>") for case in cases)
    modes = Counter(case.get("mode", "single_turn") for case in cases)
    labels = Counter()
    label_sets = Counter()
    ref_keys = Counter()
    gold_tools = Counter()
    must_buckets = Counter()
    forbidden = Counter()
    workflow_saves = Counter()
    workflow_submit = Counter()
    browser_fields = Counter()
    room_list_arg_sets = Counter()
    room_list_filters = Counter()
    meeting_actions = Counter()
    multi_turn_cases = []

    for case in cases:
        case_labels = classify_case(case)
        labels.update(case_labels)
        label_sets[tuple(case_labels)] += 1

        for key in (case.get("reference_final_answer") or {}).keys():
            ref_keys[key] += 1

        for condition in case.get("success_check", {}).get("must_satisfy", []):
            must_buckets[condition_bucket(condition)] += 1
        for condition in case.get("success_check", {}).get("forbidden", []):
            forbidden[condition] += 1

        for step in case.get("gold_trajectory", []):
            if not isinstance(step, dict):
                continue
            tool = step.get("tool", "<unknown>")
            args = step.get("args", {})
            gold_tools[tool] += 1
            if isinstance(tool, str) and tool.startswith("meetingroom."):
                meeting_actions[tool] += 1
            if tool == "workflow.save":
                workflow_saves[args.get("workflow_id") or args.get("name") or "<missing>"] += 1
                workflow_submit[bool(args.get("submit"))] += 1
            elif tool == "workflow.browser_search":
                dep = tuple(sorted((args.get("dep") or {}).items()))
                browser_fields[(args.get("workflow_id"), args.get("field_id"), dep)] += 1
            elif tool == "meetingroom.room.list":
                room_list_arg_sets[tuple(sorted(args.keys()))] += 1
                room_list_filters[
                    args.get("office_id")
                    or args.get("office_address")
                    or args.get("office_name")
                    or "<none>"
                ] += 1

        if case.get("mode", "single_turn") == "multi_turn":
            state = case.get("dialogue_state", {})
            multi_turn_cases.append(
                {
                    "case_id": case.get("case_id"),
                    "difficulty": case.get("difficulty"),
                    "step_budget": case.get("scoring", {}).get("step_budget"),
                    "missing_slots": state.get("missing_slots", []),
                    "confirmation_required_before": state.get("confirmation_required_before", []),
                    "query": case.get("user_query"),
                }
            )

    workflow_data = load_json(split_dir / "data" / "workflow_data.json")
    meetingroom_data = load_json(split_dir / "data" / "meetingroom_data.json")
    rooms = meetingroom_data.get("rooms", {})

    return {
        "split": split_name,
        "case_count": len(cases),
        "difficulty": dict(difficulties),
        "mode": dict(modes),
        "step_budget": {
            "min": min(budgets),
            "max": max(budgets),
            "avg": round(statistics.mean(budgets), 2),
            "distribution": dict(sorted(Counter(budgets).items())),
        },
        "labels": dict(labels),
        "label_sets_top": [
            {"labels": list(label_set), "count": count}
            for label_set, count in label_sets.most_common(20)
        ],
        "reference_final_answer_keys": dict(ref_keys),
        "gold_tools_top": gold_tools.most_common(30),
        "must_satisfy_top": must_buckets.most_common(40),
        "forbidden_top": forbidden.most_common(30),
        "workflow": {
            "catalog": workflow_data.get("workflow_catalog", []),
            "schema_ids": sorted(workflow_data.get("workflow_schemas", {}).keys()),
            "save_workflow_counts": dict(workflow_saves),
            "save_submit_counts": {str(key): value for key, value in workflow_submit.items()},
            "browser_search_fields_top": [
                {"workflow_id": key[0], "field_id": key[1], "dep": dict(key[2]), "count": count}
                for key, count in browser_fields.most_common(25)
            ],
        },
        "meetingroom": {
            "room_count": len(rooms),
            "campus": dict(Counter(room.get("campus") for room in rooms.values())),
            "building_top": Counter(room.get("building") for room in rooms.values()).most_common(20),
            "bookable": {str(key): value for key, value in Counter(room.get("bookable", True) for room in rooms.values()).items()},
            "capacity_top": Counter(room.get("capacity") for room in rooms.values()).most_common(25),
            "actions": dict(meeting_actions),
            "room_list_arg_sets_top": [
                {"args": list(arg_set), "count": count}
                for arg_set, count in room_list_arg_sets.most_common(25)
            ],
            "room_list_filters_top": room_list_filters.most_common(25),
        },
        "multi_turn_cases": multi_turn_cases,
    }


def summarize_runner_results(results_path: Path, split_dir: Path) -> dict[str, Any]:
    results = load_json(results_path)
    result_by_case = {item["case_id"]: item for item in results}
    cases = load_cases(split_dir)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    difficulty: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    worst = []

    for case in cases:
        case_id = case.get("case_id")
        result = result_by_case.get(case_id)
        if result is None:
            continue
        for label in classify_case(case):
            groups[label].append(result)
        difficulty[case.get("difficulty", "<missing>")].append(result)
        mode[case.get("mode", "single_turn")].append(result)
        worst.append(
            {
                "case_id": case_id,
                "total": result.get("total", 0),
                "task_passed": result.get("task_passed", False),
                "difficulty": case.get("difficulty"),
                "labels": classify_case(case),
                "query": case.get("user_query"),
                "error": result.get("error"),
            }
        )

    def stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        totals = [float(item.get("total", 0)) for item in items]
        return {
            "count": len(items),
            "avg_total": round(statistics.mean(totals), 2) if totals else 0,
            "passed": sum(1 for item in items if item.get("task_passed")),
        }

    return {
        "results_path": str(results_path),
        "overall": stats(results),
        "by_difficulty": {key: stats(value) for key, value in sorted(difficulty.items())},
        "by_mode": {key: stats(value) for key, value in sorted(mode.items())},
        "by_label": {key: stats(value) for key, value in sorted(groups.items())},
        "worst_cases": sorted(worst, key=lambda item: float(item.get("total", 0)))[:25],
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# NL2Workflow 数据集分析报告",
        "",
        "## 数据集划分",
        "",
    ]
    for split in report["splits"]:
        split_title = "训练集" if split["split"] == "train" else "验证集"
        lines.extend(
            [
                f"### {split_title}（{split['split']}）",
                "",
                f"- Case 数量：{split['case_count']}",
                f"- 难度分布：`{split['difficulty']}`",
                f"- 对话模式：`{split['mode']}`",
                f"- 步数预算：最小 `{split['step_budget']['min']}`，最大 `{split['step_budget']['max']}`，平均 `{split['step_budget']['avg']}`",
                f"- 任务标签：`{split['labels']}`",
                f"- 最终答案字段：`{split['reference_final_answer_keys']}`",
                "",
                "高频工具调用：",
            ]
        )
        for tool, count in split["gold_tools_top"][:15]:
            lines.append(f"- `{tool}`: {count}")
        lines.extend(["", "高频成功条件："])
        for condition, count in split["must_satisfy_top"][:15]:
            lines.append(f"- {count}: {condition}")
        lines.extend(["", "高频 forbidden 条件："])
        for condition, count in split["forbidden_top"][:10]:
            lines.append(f"- {count}: {condition}")
        lines.append("")

    if report.get("runner_results"):
        rr = report["runner_results"]
        lines.extend(
            [
                "## 跑分结果",
                "",
                f"- 结果来源：`{rr['results_path']}`",
                f"- 整体表现：平均分 `{rr['overall']['avg_total']}`，通过 `{rr['overall']['passed']}/{rr['overall']['count']}`",
                "",
                "按任务标签统计：",
            ]
        )
        for label, stats in rr["by_label"].items():
            lines.append(
                f"- `{label}`: 平均分 `{stats['avg_total']}`，通过 `{stats['passed']}/{stats['count']}`"
            )
        lines.extend(["", "低分 case："])
        for item in rr["worst_cases"][:15]:
            labels = ", ".join(item["labels"])
            lines.append(
                f"- `{item['case_id']}` 总分 `{item['total']}`，标签 `{labels}`：{item['query']}"
            )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-dir", default=str(ROOT / "contest" / "train"))
    parser.add_argument("--val-dir", default=str(ROOT / "contest" / "val"))
    parser.add_argument("--runner-results", help="Optional test_runner JSON output to summarize.")
    parser.add_argument("--runner-split", choices=["train", "val"], default="val")
    parser.add_argument("--json-output", default=str(ROOT / "reports" / "analysis" / "dataset_analysis.json"))
    parser.add_argument("--md-output", default=str(ROOT / "reports" / "analysis" / "dataset_analysis.md"))
    args = parser.parse_args()

    train_dir = Path(args.train_dir)
    val_dir = Path(args.val_dir)
    report: dict[str, Any] = {
        "splits": [
            split_summary("train", train_dir),
            split_summary("val", val_dir),
        ]
    }

    if args.runner_results:
        split_dir = train_dir if args.runner_split == "train" else val_dir
        report["runner_results"] = summarize_runner_results(Path(args.runner_results), split_dir)

    json_output = Path(args.json_output)
    md_output = Path(args.md_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    md_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_output)
    print(f"已写入 JSON 报告：{json_output}")
    print(f"已写入 Markdown 报告：{md_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
