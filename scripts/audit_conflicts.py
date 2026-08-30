#!/usr/bin/env python3
"""只读 Gold 关系审计器。

该脚本用于离线分析 Train/Val 的业务关联和评测契约，输出普通中文文本日志。
它不会被 Agent 导入，也不会把 case、Gold 轨迹或参考答案带入提交包；运行时
策略只能依赖用户语义、Schema 和实时工具证据。
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
UUID_RE = re.compile(r"^[0-9a-f]{24,}$", re.I)


def load_cases(split: str) -> list[dict[str, Any]]:
    cases_dir = ROOT / "contest" / split / "cases"
    result: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            payload["_source"] = str(path)
            result.append(payload)
    return result


def tags(case: dict[str, Any]) -> set[str]:
    return {str(value) for value in case.get("tags") or []}


def ref_workflow(case: dict[str, Any]) -> dict[str, Any]:
    answer = case.get("reference_final_answer") or {}
    for key in ("workflow_draft_result", "workflow_result"):
        value = answer.get(key)
        if isinstance(value, dict):
            return value
    return {}


def ref_booking(case: dict[str, Any]) -> dict[str, Any]:
    value = (case.get("reference_final_answer") or {}).get("booking_result")
    return value if isinstance(value, dict) else {}


def has_phrase(case: dict[str, Any], *phrases: str) -> bool:
    text = str(case.get("user_query") or "")
    return any(phrase in text for phrase in phrases)


def classify_observability(cluster: str) -> str:
    if cluster in {
        "审批人唯一性", "项目/物料候选唯一性", "会议候选与冲突", "费用金额守恒",
        "跨域任务隔离", "显式提交/草稿",
    }:
        return "运行时可观测：进入 generic/hybrid"
    if cluster in {"office_id 表示", "兼容日期"}:
        return "部分可观测：进入 hybrid，保留 legacy 回退"
    return "不可观测或批次冲突：仅 legacy 隔离"


def summarize(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        t = tags(case)
        query = str(case.get("user_query") or "")
        if "ambiguous_approver" in t or "approver_ambiguity" in t:
            clusters["审批人唯一性"].append(case)
        if t & {"project_ambiguity", "project_code_ambiguity", "subcategory_ambiguity", "subclass_ambiguity", "equipment_ambiguity", "category_ambiguity"}:
            clusters["项目/物料候选唯一性"].append(case)
        if t & {"meeting_blocked", "no_bookable", "conflict_resolution", "conflict_avoidance", "fallback_office", "fallback"}:
            clusters["会议候选与冲突"].append(case)
        if "cross_domain" in t:
            clusters["跨域任务隔离"].append(case)
        if "expense_material" in t or "detail_table" in t:
            clusters["费用金额守恒"].append(case)
        if t & {"submit", "draft", "draft_only", "expense_submit", "leave_submit"}:
            clusters["显式提交/草稿"].append(case)
        booking = ref_booking(case)
        if booking.get("office_id") is not None:
            clusters["office_id 表示"].append(case)
        if "明天" in query or "后天" in query or "下周" in query:
            clusters["兼容日期"].append(case)
        if "延长" in query or "extend" in t:
            clusters["延长恢复分支"].append(case)

    report: dict[str, dict[str, Any]] = {}
    for name, items in sorted(clusters.items()):
        if name == "费用金额守恒":
            rows_with_count = sum(1 for c in items if ref_workflow(c).get("detail_count") is not None)
            detail_counts = Counter(str(ref_workflow(c).get("detail_count")) for c in items if ref_workflow(c).get("detail_count") is not None)
            support = f"有 detail_count 的 {rows_with_count} 个，分布 {dict(detail_counts)}"
        elif name == "office_id 表示":
            uuid_count = sum(1 for c in items if UUID_RE.match(str(ref_booking(c).get("office_id") or "")))
            semantic_count = sum(1 for c in items if ref_booking(c).get("office_id") and not UUID_RE.match(str(ref_booking(c).get("office_id"))))
            support = f"UUID {uuid_count}，语义地址 {semantic_count}，其余未提供 {len(items)-uuid_count-semantic_count}"
        elif name == "兼容日期":
            target_days = Counter(str(ref_booking(c).get("day") or ref_workflow(c).get("start_date") or "") for c in items)
            support = f"参考日期分布 {dict(target_days)}"
        else:
            support = "按 tags/用户语义聚类；需结合实时 Schema 与工具结果复核"
        report[name] = {
            "count": len(items),
            "support": support,
            "observability": classify_observability(name),
            "examples": [Path(c["_source"]).stem for c in items[:5]],
        }
    return report


def render(all_reports: dict[str, dict[str, dict[str, Any]]]) -> str:
    lines = [
        "NL2Workflow V2 离线冲突审计（只读，不进入提交包）",
        "说明：样本名称仅用于审计定位；运行时不得读取本报告、Case ID 或 Gold。",
        "",
    ]
    total = sum(len(load_cases(split)) for split in all_reports)
    lines.append(f"总样本数：{total}")
    lines.append("")
    lines.append("冲突簇 | 数量 | 运行时可观测性 | 审计摘要 | 定位样本")
    lines.append("--- | ---: | --- | --- | ---")
    for split, report in all_reports.items():
        lines.append(f"[{split}]")
        for name, item in report.items():
            examples = ", ".join(item["examples"]) or "-"
            lines.append(
                f"{name} | {item['count']} | {item['observability']} | {item['support']} | {examples}"
            )
    lines.extend(
        [
            "",
            "晋级规则：Schema/工具/明确语义可证明的规则进入 generic；可观测且有稳定支持的规则进入 hybrid；",
            "无法由运行时事实区分的值冲突只保留 legacy_current，并在 profile 日志中记录命中。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 Train/Val Gold 关系并输出中文文本报告")
    parser.add_argument("--split", action="append", choices=("train", "val"), default=None)
    parser.add_argument("--output", default=str(ROOT / "reports" / "conflict_audit.log"))
    args = parser.parse_args()
    splits = args.split or ["train", "val"]
    all_reports: dict[str, dict[str, dict[str, Any]]] = {}
    for split in splits:
        all_reports[split] = summarize(load_cases(split))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(all_reports), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
