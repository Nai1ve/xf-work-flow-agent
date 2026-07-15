#!/usr/bin/env python3
"""Summarize NL2Workflow runner result JSON files."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(part: int | float, total: int | float) -> float:
    return round(100.0 * float(part) / float(total), 2) if total else 0.0


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def numeric_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "min": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "avg": round(mean(values), 2),
        "p50": round(median(values), 2),
        "p90": round(percentile(values, 0.9), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def case_prefix(case_id: str) -> str:
    if case_id.startswith("beta_mr_wf_"):
        return "beta_mr_wf"
    if case_id.startswith("beta_zh_"):
        return "beta_zh"
    if case_id.startswith("beta_mt_"):
        return "beta_mt"
    if case_id.startswith("beta_mr_"):
        return "beta_mr"
    if case_id.startswith("beta_wf_"):
        return "beta_wf"
    parts = case_id.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else case_id or "unknown"


def load_case_meta(split: str) -> dict[str, dict[str, Any]]:
    cases_dir = ROOT / "contest" / split / "cases"
    meta: dict[str, dict[str, Any]] = {}
    if not cases_dir.is_dir():
        return meta
    for path in sorted(cases_dir.glob("*.json")):
        try:
            data = load_json(path)
        except Exception:
            continue
        scoring = data.get("scoring") if isinstance(data.get("scoring"), dict) else {}
        meta[path.stem] = {
            "split": split,
            "tags": data.get("tags") or [],
            "difficulty": data.get("difficulty") or "",
            "mode": data.get("mode") or "single_turn",
            "primary_domains": data.get("primary_domains") or [],
            "step_budget": scoring.get("step_budget") or data.get("step_budget"),
            "user_query": data.get("user_query") or "",
        }
    return meta


def load_all_meta(split: str) -> dict[str, dict[str, Any]]:
    if split == "auto":
        merged: dict[str, dict[str, Any]] = {}
        for item in ("train", "val"):
            merged.update(load_case_meta(item))
        return merged
    return load_case_meta(split)


def failed_submission_fields(result: dict[str, Any]) -> list[str]:
    return [
        str(check.get("field") or "<unknown>")
        for check in result.get("submission_checks", []) or []
        if isinstance(check, dict) and not check.get("passed")
    ]


def failed_success_conditions(result: dict[str, Any]) -> list[str]:
    return [
        str(check.get("condition") or "<unknown>")
        for check in result.get("success_checks", []) or []
        if isinstance(check, dict) and not check.get("passed")
    ]


def variant_counts(result: dict[str, Any]) -> tuple[int, int]:
    if "variant_count" in result or "variant_passed" in result:
        return int(as_float(result.get("variant_count"))), int(as_float(result.get("variant_passed")))
    variants = result.get("variant_results") or []
    if not isinstance(variants, list):
        return 0, 0
    return len(variants), sum(1 for item in variants if isinstance(item, dict) and item.get("task_passed"))


def summarize_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(items)
    passed = sum(1 for item in items if item.get("task_passed"))
    variant_total = 0
    variant_passed = 0
    for item in items:
        total, passed_count = variant_counts(item)
        variant_total += total
        variant_passed += passed_count
    return {
        "count": count,
        "passed": passed,
        "pass_rate": pct(passed, count),
        "avg_total": numeric_stats([as_float(item.get("total")) for item in items])["avg"],
        "avg_TSR": numeric_stats([as_float(item.get("TSR")) for item in items])["avg"],
        "avg_AS": numeric_stats([as_float(item.get("AS")) for item in items])["avg"],
        "avg_ES": numeric_stats([as_float(item.get("ES")) for item in items])["avg"],
        "avg_RS": numeric_stats([as_float(item.get("RS")) for item in items])["avg"],
        "avg_steps": numeric_stats([as_float(item.get("steps_used")) for item in items])["avg"],
        "avg_elapsed": numeric_stats([as_float(item.get("elapsed_seconds")) for item in items])["avg"],
        "errors": sum(1 for item in items if item.get("error")),
        "variant_count": variant_total,
        "variant_passed": variant_passed,
        "variant_pass_rate": pct(variant_passed, variant_total),
    }


def read_results(paths: list[Path]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in paths:
        data = load_json(path)
        if not isinstance(data, list):
            raise TypeError(f"runner results must be a JSON list: {path}")
        for item in data:
            if isinstance(item, dict):
                copied = dict(item)
                copied["_source_path"] = str(path)
                results.append(copied)
    return results


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
        rows.append(
            {
                "case_id": case_id,
                "source_path": item.get("_source_path") or "",
                "split": meta.get("split") or "",
                "prefix": case_prefix(case_id),
                "difficulty": meta.get("difficulty") or "",
                "mode": meta.get("mode") or "",
                "tags": meta.get("tags") or [],
                "primary_domains": meta.get("primary_domains") or [],
                "step_budget": meta.get("step_budget"),
                "user_query": meta.get("user_query") or "",
                "total": as_float(item.get("total")),
                "task_passed": bool(item.get("task_passed")),
                "TSR": as_float(item.get("TSR")),
                "AS": as_float(item.get("AS")),
                "ES": as_float(item.get("ES")),
                "RS": as_float(item.get("RS")),
                "steps_used": item.get("steps_used"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "error": item.get("error") or "",
                "violations": item.get("violations") or [],
                "failed_submission": failed_submission_fields(item),
                "failed_success": failed_success_conditions(item),
                "variant_count": variant_total,
                "variant_passed": variant_passed,
                "delta_vs_compare": delta,
            }
        )
    return rows


def counter_from_rows(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            counter.update(str(item) for item in value if item)
        elif value:
            counter[str(value)] += 1
    return counter.most_common()


def grouped(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(key)
        if isinstance(value, list):
            values = value or ["<none>"]
        else:
            values = [value or "<none>"]
        for item in values:
            buckets[str(item)].append(row)
    return {name: summarize_group(items) for name, items in sorted(buckets.items())}


def build_summary(
    results: list[dict[str, Any]],
    meta_by_case: dict[str, dict[str, Any]],
    compare_results: list[dict[str, Any]] | None,
    top: int,
) -> dict[str, Any]:
    compare_by_case = {str(item.get("case_id") or ""): item for item in compare_results or []}
    rows = build_case_rows(results, meta_by_case, compare_by_case)
    deltas = [row["delta_vs_compare"] for row in rows if row["delta_vs_compare"] is not None]
    summary: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": sorted({str(item.get("_source_path") or "") for item in results}),
        "overall": summarize_group(results),
        "score_distribution": {
            "total": numeric_stats([row["total"] for row in rows]),
            "TSR": numeric_stats([row["TSR"] for row in rows]),
            "AS": numeric_stats([row["AS"] for row in rows]),
            "ES": numeric_stats([row["ES"] for row in rows]),
            "RS": numeric_stats([row["RS"] for row in rows]),
        },
        "by_prefix": grouped(rows, "prefix"),
        "by_tag": grouped(rows, "tags"),
        "by_difficulty": grouped(rows, "difficulty"),
        "by_mode": grouped(rows, "mode"),
        "errors": counter_from_rows(rows, "error"),
        "violations": counter_from_rows(rows, "violations"),
        "failed_submission": counter_from_rows(rows, "failed_submission"),
        "failed_success": counter_from_rows(rows, "failed_success")[:50],
        "lowest_cases": sorted(rows, key=lambda row: row["total"])[:top],
        "failed_cases": [row for row in rows if not row["task_passed"]][:top],
        "cases": rows,
    }
    if compare_results is not None:
        summary["comparison"] = {
            "compare_count": len(compare_results),
            "matched_count": len(deltas),
            "average_delta": round(mean(deltas), 2) if deltas else 0.0,
            "improved_cases": sorted(
                [row for row in rows if row["delta_vs_compare"] is not None and row["delta_vs_compare"] > 0],
                key=lambda row: row["delta_vs_compare"],
                reverse=True,
            )[:top],
            "regressed_cases": sorted(
                [row for row in rows if row["delta_vs_compare"] is not None and row["delta_vs_compare"] < 0],
                key=lambda row: row["delta_vs_compare"],
            )[:top],
        }
    return summary


def table_for_group(title: str, groups: dict[str, Any]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Group | Cases | Avg | Passed | Pass Rate | Avg Steps | Avg Elapsed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, item in sorted(groups.items()):
        lines.append(
            f"| `{name}` | {item['count']} | {item['avg_total']:.2f} | "
            f"{item['passed']}/{item['count']} | {item['pass_rate']:.2f}% | "
            f"{item['avg_steps']:.2f} | {item['avg_elapsed']:.2f}s |"
        )
    return lines


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    overall = summary["overall"]
    lines = [
        "# Runner Result Summary",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Inputs: `{', '.join(summary['inputs'])}`",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Cases | {overall['count']} |",
        f"| Average total | {overall['avg_total']:.2f} |",
        f"| Passed | {overall['passed']}/{overall['count']} |",
        f"| Pass rate | {overall['pass_rate']:.2f}% |",
        f"| Average TSR | {overall['avg_TSR']:.2f} |",
        f"| Average AS | {overall['avg_AS']:.2f} |",
        f"| Average ES | {overall['avg_ES']:.2f} |",
        f"| Average RS | {overall['avg_RS']:.2f} |",
        f"| Average steps | {overall['avg_steps']:.2f} |",
        f"| Average elapsed | {overall['avg_elapsed']:.2f}s |",
        f"| Errors | {overall['errors']} |",
        "",
    ]
    lines.extend(table_for_group("By Prefix", summary["by_prefix"]))
    lines.extend([""])
    lines.extend(table_for_group("By Tag", summary["by_tag"]))
    lines.extend([""])
    lines.extend(table_for_group("By Difficulty", summary["by_difficulty"]))
    lines.extend([""])
    lines.extend(table_for_group("By Mode", summary["by_mode"]))

    for title, key in [
        ("Errors", "errors"),
        ("Violations", "violations"),
        ("Failed Submission Checks", "failed_submission"),
        ("Failed Success Checks", "failed_success"),
    ]:
        lines.extend(["", f"## {title}", ""])
        values = summary.get(key) or []
        if not values:
            lines.append("- None")
        else:
            for name, count in values[:20]:
                lines.append(f"- {count}: {name}")

    lines.extend(
        [
            "",
            "## Lowest Cases",
            "",
            "| Case | Total | Passed | TSR | AS | ES | RS | Error |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in summary["lowest_cases"]:
        error = str(row.get("error") or "").replace("|", "\\|")
        lines.append(
            f"| `{row['case_id']}` | {row['total']:.2f} | {'yes' if row['task_passed'] else 'no'} | "
            f"{row['TSR']:.2f} | {row['AS']:.2f} | {row['ES']:.2f} | {row['RS']:.2f} | {error} |"
        )

    if summary.get("comparison"):
        comparison = summary["comparison"]
        lines.extend(
            [
                "",
                "## Comparison",
                "",
                f"- Matched cases: `{comparison['matched_count']}`",
                f"- Average delta: `{comparison['average_delta']:+.2f}`",
                "",
                "### Regressed Cases",
                "",
            ]
        )
        for row in comparison["regressed_cases"][:20]:
            lines.append(f"- `{row['case_id']}`: {row['delta_vs_compare']:+.2f}, current {row['total']:.2f}")
        lines.extend(["", "### Improved Cases", ""])
        for row in comparison["improved_cases"][:20]:
            lines.append(f"- `{row['case_id']}`: {row['delta_vs_compare']:+.2f}, current {row['total']:.2f}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(summary: dict[str, Any], path: Path) -> None:
    rows = summary["cases"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "case_id",
        "split",
        "prefix",
        "difficulty",
        "mode",
        "tags",
        "total",
        "task_passed",
        "TSR",
        "AS",
        "ES",
        "RS",
        "steps_used",
        "elapsed_seconds",
        "variant_count",
        "variant_passed",
        "delta_vs_compare",
        "error",
        "user_query",
        "source_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{key: row.get(key) for key in fieldnames},
                    "tags": ",".join(str(item) for item in row.get("tags") or []),
                    "task_passed": "true" if row.get("task_passed") else "false",
                }
            )


def default_stem(results: list[Path], label: str | None) -> str:
    if label:
        return label
    if len(results) == 1:
        return results[0].stem
    return "combined_run_results"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--split", choices=["auto", "train", "val"], default="auto")
    parser.add_argument("--compare-to", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "analysis")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--md-output", type=Path)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    results = read_results(args.results)
    compare_results = read_results([args.compare_to]) if args.compare_to else None
    summary = build_summary(results, load_all_meta(args.split), compare_results, args.top)

    stem = default_stem(args.results, args.label)
    json_output = args.json_output or args.output_dir / f"{stem}_summary.json"
    md_output = args.md_output or args.output_dir / f"{stem}_summary.md"
    csv_output = args.csv_output or args.output_dir / f"{stem}_cases.csv"

    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, md_output)
    write_csv(summary, csv_output)

    overall = summary["overall"]
    print(
        "Summary: cases={count} avg={avg:.2f} passed={passed}/{count} pass_rate={rate:.2f}%".format(
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
