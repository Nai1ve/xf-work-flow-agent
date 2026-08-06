#!/usr/bin/env python3
"""Tag 级回归看板：按 technical_design.md §9.2/§9.3 聚合 runner 结果。

输入：runner 结果 JSON（train / val，可多个，或 --compare-to 做回归对比）。
输出：
  - JSON：结构化汇总（overall / by_domain / by_mode / by_difficulty / by_tag /
           es_audit / acceptance / comparison / cases）
  - Markdown：人读报告
  - CSV：逐 case 明细

关键口径（对齐 technical_design.md §9.2/§9.3）：
  - TSR 通过 = 所有 success_checks 通过；AS 通过 = 无 violation 且所有
    submission_checks 通过；ES 通过 = 满分 10；RS 通过 = 满分 10。
  - 通过率矩阵按 分组 × 维度 展开，列即 TSR/AS/ES/RS 通过率。
  - ES 审计：实际步数 vs gold_trajectory 长度（或 scoring.optimal_steps），
    超出 2 步的 case 单列。
  - 验收清单：task_passed ≥ 92%、AS 扣分 case ≤ 3%、forbidden = 0、
    平均 ES ≥ 8、超时 = 0、异常 0 分 case = 0。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from summarize_run_results import (  # noqa: E402
    as_float,
    case_prefix,
    failed_submission_fields,
    failed_success_conditions,
    load_all_meta,
    load_case_meta,
    numeric_stats,
    pct,
    read_results,
    variant_counts,
)

ROOT = Path(__file__).resolve().parents[1]

# 验收阈值（technical_design.md §9.3）
ACCEPTANCE = {
    "task_passed_rate_min": 92.0,
    "as_deducted_rate_max": 3.0,
    "forbidden_max": 0,
    "avg_es_min": 8.0,
    "timeout_max": 0,
    "exception_max": 0,
}


def full_pass_tsr(item: dict[str, Any]) -> bool:
    """TSR 满分 = 所有 success_checks 通过。"""
    checks = item.get("success_checks") or []
    return bool(checks) and all(c.get("passed") for c in checks)


def full_pass_as(item: dict[str, Any]) -> bool:
    """AS 满分 = 无 violation 且所有 submission_checks 通过。"""
    if item.get("violations"):
        return False
    checks = item.get("submission_checks") or []
    return bool(checks) and all(c.get("passed") for c in checks)


def full_pass_es(item: dict[str, Any]) -> bool:
    return as_float(item.get("ES")) >= 10.0 - 1e-6


def full_pass_rs(item: dict[str, Any]) -> bool:
    return as_float(item.get("RS")) >= 10.0 - 1e-6


def as_deducted(item: dict[str, Any]) -> bool:
    return as_float(item.get("AS")) < 20.0 - 1e-6


def is_timeout(item: dict[str, Any]) -> bool:
    return "timed out" in str(item.get("error") or "")


def is_exception(item: dict[str, Any]) -> bool:
    return bool(item.get("error")) and as_float(item.get("total")) == 0


def build_case_rows(
    results: list[dict[str, Any]],
    meta_by_case: dict[str, dict[str, Any]],
    compare_by_case: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in results:
        case_id = str(item.get("case_id") or "")
        meta = meta_by_case.get(case_id, {})
        previous = compare_by_case.get(case_id)
        delta = None
        if previous is not None:
            delta = round(as_float(item.get("total")) - as_float(previous.get("total")), 2)
        variant_total, variant_passed = variant_counts(item)

        gold_steps = None
        optimal_steps = None
        scoring = meta.get("scoring") or {}
        if isinstance(scoring, dict):
            optimal_steps = scoring.get("optimal_steps")
            if isinstance(optimal_steps, bool):
                optimal_steps = None
            if optimal_steps is not None:
                try:
                    optimal_steps = int(optimal_steps)
                except (TypeError, ValueError):
                    optimal_steps = None
        gold_trajectory = meta.get("gold_trajectory")
        if isinstance(gold_trajectory, list):
            gold_steps = len(gold_trajectory)

        steps_used = item.get("steps_used")
        overrun = None
        baseline = optimal_steps if optimal_steps is not None else gold_steps
        if baseline is not None and steps_used is not None:
            try:
                overrun = int(steps_used) - int(baseline)
            except (TypeError, ValueError):
                overrun = None

        rows.append(
            {
                "case_id": case_id,
                "source_path": item.get("_source_path") or "",
                "split": meta.get("split") or "",
                "prefix": case_prefix(case_id),
                "difficulty": meta.get("difficulty") or "",
                "mode": meta.get("mode") or "",
                "primary_domains": meta.get("primary_domains") or [],
                "tags": meta.get("tags") or [],
                "step_budget": meta.get("step_budget"),
                "gold_steps": gold_steps,
                "optimal_steps": optimal_steps,
                "user_query": meta.get("user_query") or "",
                "total": as_float(item.get("total")),
                "task_passed": bool(item.get("task_passed")),
                "TSR": as_float(item.get("TSR")),
                "AS": as_float(item.get("AS")),
                "ES": as_float(item.get("ES")),
                "RS": as_float(item.get("RS")),
                "steps_used": steps_used,
                "overrun": overrun,
                "elapsed_seconds": item.get("elapsed_seconds"),
                "error": item.get("error") or "",
                "violations": item.get("violations") or [],
                "failed_submission": failed_submission_fields(item),
                "failed_success": failed_success_conditions(item),
                "variant_count": variant_total,
                "variant_passed": variant_passed,
                "tsr_pass": full_pass_tsr(item),
                "as_pass": full_pass_as(item),
                "es_pass": full_pass_es(item),
                "rs_pass": full_pass_rs(item),
                "as_deducted": as_deducted(item),
                "is_timeout": is_timeout(item),
                "is_exception": is_exception(item),
                "delta_vs_compare": delta,
            }
        )
    return rows


def group_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(items)
    passed = sum(1 for item in items if item["task_passed"])
    return {
        "count": count,
        "passed": passed,
        "pass_rate": pct(passed, count),
        "avg_total": numeric_stats([item["total"] for item in items])["avg"],
        "avg_TSR": numeric_stats([item["TSR"] for item in items])["avg"],
        "avg_AS": numeric_stats([item["AS"] for item in items])["avg"],
        "avg_ES": numeric_stats([item["ES"] for item in items])["avg"],
        "avg_RS": numeric_stats([item["RS"] for item in items])["avg"],
        "avg_steps": numeric_stats([as_float(item["steps_used"]) for item in items])["avg"],
        "avg_elapsed": numeric_stats([as_float(item["elapsed_seconds"]) for item in items])["avg"],
        "tsr_pass_rate": pct(sum(1 for item in items if item["tsr_pass"]), count),
        "as_pass_rate": pct(sum(1 for item in items if item["as_pass"]), count),
        "es_pass_rate": pct(sum(1 for item in items if item["es_pass"]), count),
        "rs_pass_rate": pct(sum(1 for item in items if item["rs_pass"]), count),
        "as_deducted_rate": pct(sum(1 for item in items if item["as_deducted"]), count),
        "forbidden_count": sum(1 for item in items if item["violations"]),
        "errors": sum(1 for item in items if item["error"]),
        "timeouts": sum(1 for item in items if item["is_timeout"]),
        "exceptions": sum(1 for item in items if item["is_exception"]),
    }


def group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            values = value or ["<none>"]
        else:
            values = [value or "<none>"]
        for item in values:
            buckets[str(item)].append(row)
    return {name: group_stats(items) for name, items in sorted(buckets.items())}


def es_audit(rows: list[dict[str, Any]], threshold: int = 2) -> list[dict[str, Any]]:
    """ES 审计：实际步数超出最优（gold/optimal）threshold 步以上的 case。"""
    return sorted(
        [
            {
                "case_id": row["case_id"],
                "split": row["split"],
                "mode": row["mode"],
                "total": row["total"],
                "task_passed": row["task_passed"],
                "TSR": row["TSR"],
                "ES": row["ES"],
                "steps_used": row["steps_used"],
                "gold_steps": row["gold_steps"],
                "optimal_steps": row["optimal_steps"],
                "overrun": row["overrun"],
                "failed_submission": row["failed_submission"],
                "failed_success": row["failed_success"],
            }
            for row in rows
            if row["overrun"] is not None and row["overrun"] > threshold
        ],
        key=lambda item: item["overrun"],
        reverse=True,
    )


def acceptance_checklist(overall: dict[str, Any], all_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks = [
        {
            "metric": "task_passed_rate",
            "value": overall["pass_rate"],
            "target": f">= {ACCEPTANCE['task_passed_rate_min']}%",
            "pass": overall["pass_rate"] >= ACCEPTANCE["task_passed_rate_min"],
        },
        {
            "metric": "as_deducted_rate",
            "value": overall["as_deducted_rate"],
            "target": f"<= {ACCEPTANCE['as_deducted_rate_max']}%",
            "pass": overall["as_deducted_rate"] <= ACCEPTANCE["as_deducted_rate_max"],
        },
        {
            "metric": "forbidden_count",
            "value": overall["forbidden_count"],
            "target": "== 0",
            "pass": overall["forbidden_count"] <= ACCEPTANCE["forbidden_max"],
        },
        {
            "metric": "avg_ES",
            "value": overall["avg_ES"],
            "target": f">= {ACCEPTANCE['avg_es_min']}",
            "pass": overall["avg_ES"] >= ACCEPTANCE["avg_es_min"],
        },
        {
            "metric": "timeout_count",
            "value": overall["timeouts"],
            "target": "== 0",
            "pass": overall["timeouts"] <= ACCEPTANCE["timeout_max"],
        },
        {
            "metric": "exception_0score_count",
            "value": overall["exceptions"],
            "target": "== 0",
            "pass": overall["exceptions"] <= ACCEPTANCE["exception_max"],
        },
    ]
    return checks


def build_summary(
    results: list[dict[str, Any]],
    meta_by_case: dict[str, dict[str, Any]],
    compare_results: list[dict[str, Any]] | None,
    top: int,
) -> dict[str, Any]:
    compare_by_case = {str(item.get("case_id") or ""): item for item in compare_results or []}
    rows = build_case_rows(results, meta_by_case, compare_by_case)
    overall = group_stats(rows)
    deltas = [row["delta_vs_compare"] for row in rows if row["delta_vs_compare"] is not None]
    summary: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": sorted({str(item.get("_source_path") or "") for item in results}),
        "overall": overall,
        "acceptance": acceptance_checklist(overall, rows),
        "by_domain": group_rows(rows, "primary_domains"),
        "by_mode": group_rows(rows, "mode"),
        "by_difficulty": group_rows(rows, "difficulty"),
        "by_prefix": group_rows(rows, "prefix"),
        "by_tag": group_rows(rows, "tags"),
        "es_audit": es_audit(rows)[:top],
        "es_audit_overrun_gt_2": len([r for r in rows if r["overrun"] is not None and r["overrun"] > 2]),
        "errors": Counter(str(item.get("error") or "") for item in rows).most_common(),
        "violations": Counter(
            v for item in rows for v in (item.get("violations") or [])
        ).most_common(),
        "failed_submission": Counter(
            f for item in rows for f in item["failed_submission"]
        ).most_common(),
        "failed_success": Counter(
            c for item in rows for c in item["failed_success"]
        ).most_common(50),
        "lowest_cases": sorted(rows, key=lambda row: row["total"])[:top],
        "failed_cases": [row for row in rows if not row["task_passed"]][:top],
        "cases": rows,
    }
    if compare_results is not None:
        summary["comparison"] = {
            "compare_count": len(compare_results),
            "matched_count": len(deltas),
            "average_delta": round(mean(deltas), 2) if deltas else 0.0,
            "regressed_cases": sorted(
                [
                    row
                    for row in rows
                    if row["delta_vs_compare"] is not None and row["delta_vs_compare"] < 0
                ],
                key=lambda row: row["delta_vs_compare"],
            )[:top],
            "improved_cases": sorted(
                [
                    row
                    for row in rows
                    if row["delta_vs_compare"] is not None and row["delta_vs_compare"] > 0
                ],
                key=lambda row: row["delta_vs_compare"],
                reverse=True,
            )[:top],
        }
    return summary


def render_matrix(title: str, groups: dict[str, Any], min_cases: int) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| 分组 | Case 数 | 通过率 | avg TSR | avg AS | avg ES | avg RS | "
        "TSR% | AS% | ES% | RS% | AS扣分% | forbidden | 均步数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, item in sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True):
        if item["count"] < min_cases:
            continue
        lines.append(
            f"| `{name}` | {item['count']} | {item['pass_rate']:.1f}% | "
            f"{item['avg_TSR']:.1f} | {item['avg_AS']:.1f} | {item['avg_ES']:.1f} | {item['avg_RS']:.1f} | "
            f"{item['tsr_pass_rate']:.0f}% | {item['as_pass_rate']:.0f}% | "
            f"{item['es_pass_rate']:.0f}% | {item['rs_pass_rate']:.0f}% | "
            f"{item['as_deducted_rate']:.0f}% | {item['forbidden_count']} | {item['avg_steps']:.2f} |"
        )
    return lines


def render_acceptance(summary: dict[str, Any]) -> list[str]:
    lines = ["### 验收清单（§9.3）", "", "| 指标 | 当前值 | 目标 | 达标 |", "| --- | ---: | ---: | :---: |"]
    for check in summary["acceptance"]:
        mark = "✅" if check["pass"] else "❌"
        lines.append(f"| {check['metric']} | {check['value']:.2f} | {check['target']} | {mark} |")
    return lines


def render_es_audit(summary: dict[str, Any]) -> list[str]:
    lines = [
        "### ES 审计（超最优步数 > 2 的 case）",
        "",
        f"总数：{summary['es_audit_overrun_gt_2']}",
        "",
        "| Case | split | mode | 总分 | 通过 | steps | gold | optimal | overrun | ES |",
        "| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["es_audit"]:
        lines.append(
            f"| `{row['case_id']}` | {row['split']} | {row['mode']} | {row['total']:.2f} | "
            f"{'是' if row['task_passed'] else '否'} | {row['steps_used']} | "
            f"{row['gold_steps'] or '-'} | {row['optimal_steps'] or '-'} | {row['overrun']} | {row['ES']:.1f} |"
        )
    return lines


def render_comparison(summary: dict[str, Any]) -> list[str]:
    comparison = summary["comparison"]
    lines = [
        "### 对比基线",
        "",
        f"- 匹配 case：{comparison['matched_count']}，平均变化：{comparison['average_delta']:+.2f}",
        "",
        "#### 回退最多",
        "",
    ]
    for row in comparison["regressed_cases"][:10]:
        lines.append(
            f"- `{row['case_id']}`：{row['delta_vs_compare']:+.2f}（当前 {row['total']:.2f}）"
        )
    lines.extend(["", "#### 提升最多", ""])
    for row in comparison["improved_cases"][:10]:
        lines.append(
            f"- `{row['case_id']}`：{row['delta_vs_compare']:+.2f}（当前 {row['total']:.2f}）"
        )
    return lines


def write_markdown(summary: dict[str, Any], path: Path, min_cases: int) -> None:
    overall = summary["overall"]
    lines = [
        "# Tag 级回归看板",
        "",
        f"- 生成时间：`{summary['generated_at']}`",
        f"- 输入：`{', '.join(summary['inputs'])}`",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| Case 数 | {overall['count']} |",
        f"| 平均分 | {overall['avg_total']:.2f} |",
        f"| 通过 | {overall['passed']}/{overall['count']} |",
        f"| 通过率 | {overall['pass_rate']:.2f}% |",
        f"| avg TSR | {overall['avg_TSR']:.2f} |",
        f"| avg AS | {overall['avg_AS']:.2f} |",
        f"| avg ES | {overall['avg_ES']:.2f} |",
        f"| avg RS | {overall['avg_RS']:.2f} |",
        f"| AS 扣分 case | {overall['as_deducted_rate']:.2f}% |",
        f"| forbidden | {overall['forbidden_count']} |",
        f"| 异常 0 分 | {overall['exceptions']} |",
        f"| 超时 | {overall['timeouts']} |",
        f"| 均步数 | {overall['avg_steps']:.2f} |",
        f"| 均耗时 | {overall['avg_elapsed']:.2f}s |",
        "",
    ]
    lines.extend(render_acceptance(summary))
    lines.extend(["", ""])
    lines.extend(render_matrix("按主域", summary["by_domain"], min_cases))
    lines.extend(["", ""])
    lines.extend(render_matrix("按 mode", summary["by_mode"], 1))
    lines.extend(["", ""])
    lines.extend(render_matrix("按难度", summary["by_difficulty"], 1))
    lines.extend(["", ""])
    lines.extend(render_matrix("按 tag", summary["by_tag"], min_cases))
    lines.extend(["", ""])
    lines.extend(render_es_audit(summary))

    if summary.get("comparison"):
        lines.extend(["", ""])
        lines.extend(render_comparison(summary))

    lines.extend(
        [
            "",
            "## 高频失败条件（failed success_checks）",
            "",
        ]
    )
    if summary["failed_success"]:
        for name, count in summary["failed_success"][:20]:
            lines.append(f"- {count} 次：{name}")
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 高频 submission 失败字段",
            "",
        ]
    )
    if summary["failed_submission"]:
        for name, count in summary["failed_submission"][:20]:
            lines.append(f"- {count} 次：`{name}`")
    else:
        lines.append("- 无")

    lines.extend(["", "## 低分 Case（前 15）", ""])
    for row in summary["lowest_cases"][:15]:
        sub = ", ".join(row["failed_submission"]) or "-"
        vio = ", ".join(row["violations"]) or "-"
        lines.append(
            f"- `{row['case_id']}`：{row['total']:.2f}，TSR={row['TSR']:.1f} AS={row['AS']:.1f} "
            f"ES={row['ES']:.1f}，submission=[{sub}]，violations=[{vio}]"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(summary: dict[str, Any], path: Path) -> None:
    rows = summary["cases"]
    fieldnames = [
        "case_id",
        "split",
        "prefix",
        "difficulty",
        "mode",
        "primary_domains",
        "tags",
        "total",
        "task_passed",
        "TSR",
        "AS",
        "ES",
        "RS",
        "steps_used",
        "gold_steps",
        "optimal_steps",
        "overrun",
        "elapsed_seconds",
        "tsr_pass",
        "as_pass",
        "es_pass",
        "rs_pass",
        "as_deducted",
        "violations",
        "failed_submission",
        "failed_success",
        "error",
        "delta_vs_compare",
        "user_query",
        "source_path",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{key: row.get(key) for key in fieldnames},
                    "primary_domains": ",".join(str(item) for item in row.get("primary_domains") or []),
                    "tags": ",".join(str(item) for item in row.get("tags") or []),
                    "violations": ",".join(str(item) for item in row.get("violations") or []),
                    "failed_submission": ",".join(row.get("failed_submission") or []),
                    "failed_success": ",".join(row.get("failed_success") or []),
                    "task_passed": "true" if row.get("task_passed") else "false",
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--split", choices=["auto", "train", "val"], default="auto")
    parser.add_argument("--compare-to", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "analysis")
    parser.add_argument("--label")
    parser.add_argument("--min-cases", type=int, default=3, help="tag 矩阵最少 case 数才展示")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    results = read_results(args.results)
    compare_results = read_results([args.compare_to]) if args.compare_to else None
    meta_by_case = load_all_meta(args.split)
    summary = build_summary(results, meta_by_case, compare_results, args.top)

    if args.label:
        stem = args.label
    elif len(args.results) == 1:
        stem = args.results[0].stem
    else:
        stem = "combined_dashboard"

    json_output = args.output_dir / f"{stem}_dashboard.json"
    md_output = args.output_dir / f"{stem}_dashboard.md"
    csv_output = args.output_dir / f"{stem}_dashboard.csv"

    json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, md_output, args.min_cases)
    write_csv(summary, csv_output)

    overall = summary["overall"]
    print(
        "Dashboard: cases={count} avg={avg:.2f} passed={passed}/{count} pass_rate={rate:.2f}%".format(
            count=overall["count"],
            avg=overall["avg_total"],
            passed=overall["passed"],
            rate=overall["pass_rate"],
        )
    )
    print(f"JSON: {json_output}")
    print(f"Markdown: {md_output}")
    print(f"CSV: {csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
