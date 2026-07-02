#!/usr/bin/env python3
"""使用官方本地 runner 在训练集或验证集上运行 Agent。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


ROOT = Path(__file__).resolve().parents[1]
PYTHON311_CANDIDATES = [
    Path("/opt/homebrew/bin/python3.11"),
    Path("/usr/local/bin/python3.11"),
    Path(sys.executable),
]


def resolve_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in PYTHON311_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return "python3.11"


def prepare_runner_tree(split: str, force: bool = False) -> Path:
    source_split = ROOT / "contest" / split
    source_simulator = ROOT / "contest" / "simulator"
    if not source_split.exists():
        raise FileNotFoundError(f"missing split directory: {source_split}")
    if not source_simulator.exists():
        raise FileNotFoundError(f"missing simulator directory: {source_simulator}")

    target = ROOT / "tmp" / f"contest_{split}"
    if target.exists() and force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    for name in ["simulator", "cases", "data"]:
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)

    shutil.copytree(source_simulator / "simulator", target / "simulator")
    shutil.copytree(source_split / "cases", target / "cases")
    shutil.copytree(source_split / "data", target / "data")
    shutil.copy2(source_split / "tool_specs.json", target / "tool_specs.json")
    return target


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_stage(message: str, log_file: TextIO | None = None, stage: str = "run") -> None:
    line = f"[{timestamp()}] [{stage}] {message}"
    print(line, flush=True)
    if log_file is not None:
        log_file.write(line + "\n")
        log_file.flush()


def stream_command(command: list[str], cwd: Path, env: dict[str, str], log_file: TextIO) -> int:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        log_file.write(line)
        log_file.flush()
    return process.wait()


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_case_meta(split: str) -> dict[str, dict[str, Any]]:
    cases_dir = ROOT / "contest" / split / "cases"
    meta: dict[str, dict[str, Any]] = {}
    for path in cases_dir.glob("*.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        meta[path.stem] = {
            "tags": data.get("tags", []),
            "user_query": data.get("user_query", ""),
            "step_budget": data.get("step_budget"),
        }
    return meta


def prefix_group(case_id: str) -> str:
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
    return "other"


def summarize_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"count": 0, "average": 0, "passed": 0}
    return {
        "count": len(items),
        "average": round(sum(float(item.get("total", 0)) for item in items) / len(items), 2),
        "passed": sum(1 for item in items if item.get("task_passed")),
        "avg_TSR": round(sum(float(item.get("TSR", 0)) for item in items) / len(items), 2),
        "avg_AS": round(sum(float(item.get("AS", 0)) for item in items) / len(items), 2),
        "avg_ES": round(sum(float(item.get("ES", 0)) for item in items) / len(items), 2),
        "avg_steps": round(
            sum(float(item.get("steps_used", 0) or 0) for item in items) / len(items),
            2,
        ),
        "avg_elapsed": round(
            sum(float(item.get("elapsed_seconds", 0) or 0) for item in items) / len(items),
            2,
        ),
    }


def build_summary(
    results: list[dict[str, Any]],
    split: str,
    case_meta: dict[str, dict[str, Any]],
    compare_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tag_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    submission_failures: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    failed_success_conditions: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []

    compare_by_case = {item.get("case_id"): item for item in compare_results or []}
    deltas: list[float] = []

    for item in results:
        case_id = str(item.get("case_id", ""))
        meta = case_meta.get(case_id, {})
        tags = meta.get("tags") or []
        groups[prefix_group(case_id)].append(item)
        for tag in tags:
            tag_groups[str(tag)].append(item)

        failed_submission = [
            check.get("field", "")
            for check in item.get("submission_checks", []) or []
            if not check.get("passed")
        ]
        for field in failed_submission:
            submission_failures[str(field)] += 1

        for violation in item.get("violations", []) or []:
            violations[str(violation)] += 1

        failed_success = [
            check.get("condition", "")
            for check in item.get("success_checks", []) or []
            if not check.get("passed")
        ]
        for condition in failed_success:
            failed_success_conditions[str(condition)] += 1

        previous = compare_by_case.get(case_id)
        delta = None
        if previous is not None:
            delta = round(float(item.get("total", 0)) - float(previous.get("total", 0)), 2)
            deltas.append(delta)

        cases.append(
            {
                "case_id": case_id,
                "total": item.get("total", 0),
                "task_passed": item.get("task_passed", False),
                "TSR": item.get("TSR", 0),
                "AS": item.get("AS", 0),
                "ES": item.get("ES", 0),
                "RS": item.get("RS", 0),
                "steps_used": item.get("steps_used"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "tags": tags,
                "failed_success": failed_success,
                "failed_submission": failed_submission,
                "violations": item.get("violations", []) or [],
                "delta_vs_compare": delta,
                "error": item.get("error"),
            }
        )

    overall = summarize_group(results)
    summary = {
        "generated_at": timestamp(),
        "split": split,
        "overall": {
            **overall,
            "pass_rate": round(overall["passed"] / overall["count"], 4) if overall["count"] else 0,
        },
        "prefix_groups": {
            name: summarize_group(items)
            for name, items in sorted(groups.items())
        },
        "tag_groups": {
            name: summarize_group(items)
            for name, items in sorted(tag_groups.items())
        },
        "submission_failures": submission_failures.most_common(),
        "violations": violations.most_common(),
        "failed_success_conditions": failed_success_conditions.most_common(30),
        "lowest_cases": sorted(cases, key=lambda item: float(item.get("total", 0)))[:20],
        "passed_cases": [item["case_id"] for item in cases if item.get("task_passed")],
        "cases": cases,
    }
    if compare_results is not None:
        summary["comparison"] = {
            "compare_count": len(compare_results),
            "matched_count": len(deltas),
            "average_delta": round(sum(deltas) / len(deltas), 2) if deltas else 0,
            "improved_cases": sorted(
                [
                    {"case_id": item["case_id"], "delta": item["delta_vs_compare"], "total": item["total"]}
                    for item in cases
                    if item["delta_vs_compare"] is not None and item["delta_vs_compare"] > 0
                ],
                key=lambda item: item["delta"],
                reverse=True,
            )[:20],
            "regressed_cases": sorted(
                [
                    {"case_id": item["case_id"], "delta": item["delta_vs_compare"], "total": item["total"]}
                    for item in cases
                    if item["delta_vs_compare"] is not None and item["delta_vs_compare"] < 0
                ],
                key=lambda item: item["delta"],
            )[:20],
        }
    return summary


def write_markdown(summary: dict[str, Any], path: Path, result_path: Path, log_path: Path) -> None:
    overall = summary["overall"]
    lines: list[str] = [
        f"# 验证运行分析报告（{summary['generated_at']}）",
        "",
        "## 运行产物",
        "",
        f"- 结果 JSON：`{result_path}`",
        f"- 运行日志：`{log_path}`",
        f"- 数据 split：`{summary['split']}`",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| Case 数 | {overall['count']} |",
        f"| 平均分 | {overall['average']:.2f} |",
        f"| 通过数 | {overall['passed']}/{overall['count']} |",
        f"| 通过率 | {overall['pass_rate'] * 100:.1f}% |",
        f"| 平均 TSR | {overall['avg_TSR']:.2f} |",
        f"| 平均 AS | {overall['avg_AS']:.2f} |",
        f"| 平均 ES | {overall['avg_ES']:.2f} |",
        f"| 平均步数 | {overall['avg_steps']:.2f} |",
        f"| 平均耗时 | {overall['avg_elapsed']:.2f}s |",
        "",
        "## Prefix 分组",
        "",
        "| 分组 | Case 数 | 平均分 | 通过 | 平均步数 | 平均耗时 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, group in summary["prefix_groups"].items():
        lines.append(
            f"| `{name}` | {group['count']} | {group['average']:.2f} | "
            f"{group['passed']}/{group['count']} | {group.get('avg_steps', 0):.2f} | {group.get('avg_elapsed', 0):.2f}s |"
        )

    if summary.get("comparison"):
        comparison = summary["comparison"]
        lines.extend(
            [
                "",
                "## 对比基线",
                "",
                f"- 匹配 case：{comparison['matched_count']}",
                f"- 平均变化：{comparison['average_delta']:+.2f}",
                "",
                "### 提升最多",
                "",
            ]
        )
        for item in comparison["improved_cases"][:10]:
            lines.append(f"- `{item['case_id']}`：{item['delta']:+.2f}，当前 {item['total']}")
        lines.extend(["", "### 回退最多", ""])
        for item in comparison["regressed_cases"][:10]:
            lines.append(f"- `{item['case_id']}`：{item['delta']:+.2f}，当前 {item['total']}")

    lines.extend(["", "## Submission 失败统计", ""])
    if summary["submission_failures"]:
        for field, count in summary["submission_failures"]:
            lines.append(f"- `{field}`：{count}")
    else:
        lines.append("- 无")

    lines.extend(["", "## Violation 统计", ""])
    if summary["violations"]:
        for violation, count in summary["violations"]:
            lines.append(f"- {violation}：{count}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 高频失败条件", ""])
    if summary["failed_success_conditions"]:
        for condition, count in summary["failed_success_conditions"][:20]:
            lines.append(f"- {count} 次：{condition}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 低分 Case", ""])
    for item in summary["lowest_cases"][:15]:
        failed_submission = ", ".join(item["failed_submission"]) or "-"
        violations = ", ".join(item["violations"]) or "-"
        lines.append(
            f"- `{item['case_id']}`：{float(item['total']):.2f}，"
            f"TSR={float(item['TSR']):.2f} AS={float(item['AS']):.2f} ES={float(item['ES']):.2f}，"
            f"submission={failed_submission}，violations={violations}"
        )

    lines.extend(["", "## 逐 Case 明细", ""])
    lines.append("| Case | 分数 | 通过 | 失败 submission | violations |")
    lines.append("| --- | ---: | --- | --- | --- |")
    for item in summary["cases"]:
        failed_submission = ", ".join(item["failed_submission"]) or "-"
        violations = ", ".join(item["violations"]) or "-"
        lines.append(
            f"| `{item['case_id']}` | {float(item['total']):.2f} | "
            f"{'是' if item['task_passed'] else '否'} | {failed_submission} | {violations} |"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_summary(summary: dict[str, Any]) -> None:
    overall = summary["overall"]
    print("\n[Analysis] 总览")
    print(
        "  average={average:.2f} passed={passed}/{count} pass_rate={rate:.1f}% avg_steps={steps:.2f} avg_elapsed={elapsed:.2f}s".format(
            average=overall["average"],
            passed=overall["passed"],
            count=overall["count"],
            rate=overall["pass_rate"] * 100,
            steps=overall.get("avg_steps", 0),
            elapsed=overall.get("avg_elapsed", 0),
        )
    )
    if summary["submission_failures"]:
        print("  submission_failures=" + ", ".join(f"{field}:{count}" for field, count in summary["submission_failures"][:6]))
    if summary["violations"]:
        print("  violations=" + ", ".join(f"{name}:{count}" for name, count in summary["violations"][:6]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=str(ROOT / "contest" / "simulator" / "simulator" / "baseline_agent.py"))
    parser.add_argument("--split", choices=["train", "val"], default="val")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-variants", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--python", help="Python 3.11 executable.")
    parser.add_argument("--output", help="Output JSON path.")
    parser.add_argument("--log-output", help="运行 stdout/stderr 日志路径。默认和结果 JSON 同名 .stdout。")
    parser.add_argument("--summary-output", help="结构化汇总 JSON 路径。默认写入 reports/analysis。")
    parser.add_argument("--analysis-output", help="Markdown 分析报告路径。默认写入 reports/analysis。")
    parser.add_argument("--compare-to", help="可选：和已有 runner JSON 结果做逐 case 对比。")
    parser.add_argument("--no-analysis", action="store_true", help="只运行，不生成汇总分析。")
    parser.add_argument("--refresh-tree", action="store_true")
    args = parser.parse_args()

    output = Path(args.output) if args.output else ROOT / "reports" / "baseline" / f"{Path(args.agent).stem}_{args.split}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    log_output = Path(args.log_output) if args.log_output else output.with_suffix(".stdout")
    log_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output = Path(args.summary_output) if args.summary_output else ROOT / "reports" / "analysis" / f"{output.stem}_summary.json"
    analysis_output = Path(args.analysis_output) if args.analysis_output else ROOT / "reports" / "analysis" / f"{output.stem}_analysis.md"

    started_at = time.time()
    with log_output.open("w", encoding="utf-8") as log_file:
        log_stage("开始验证运行", log_file, "start")
        log_stage(f"workspace={ROOT}", log_file, "config")
        log_stage(f"agent={Path(args.agent).resolve()}", log_file, "config")
        log_stage(f"split={args.split} parallel={args.parallel} timeout={args.timeout} verbose={args.verbose}", log_file, "config")
        if args.cases:
            log_stage(f"cases={','.join(args.cases)}", log_file, "config")
        elif args.limit is not None:
            log_stage(f"limit={args.limit}", log_file, "config")
        else:
            log_stage("cases=ALL", log_file, "config")

        stage_started = time.time()
        runner_tree = prepare_runner_tree(args.split, force=args.refresh_tree)
        log_stage(f"runner_tree={runner_tree} prepared in {time.time() - stage_started:.2f}s", log_file, "prepare")

        command = [
            resolve_python(args.python),
            "simulator/test_runner.py",
            "--agent",
            str(Path(args.agent).resolve()),
            "--parallel",
            str(args.parallel),
            "--timeout",
            str(args.timeout),
            "--skip-requirements-check",
            "--output",
            str(output.resolve()),
        ]
        if args.limit is not None:
            command.extend(["--limit", str(args.limit)])
        if args.cases:
            for case in args.cases:
                command.extend(["--case", case])
        if args.skip_variants:
            command.append("--skip-variants")
        if args.verbose:
            command.append("--verbose")

        env = os.environ.copy()
        log_stage("执行命令：" + " ".join(command), log_file, "runner")
        log_stage(f"运行目录：{runner_tree}", log_file, "runner")
        stage_started = time.time()
        returncode = stream_command(command, runner_tree, env, log_file)
        log_stage(f"runner finished returncode={returncode} elapsed={time.time() - stage_started:.2f}s", log_file, "runner")
        log_stage(f"结果文件：{output}", log_file, "artifact")
        log_stage(f"运行日志：{log_output}", log_file, "artifact")

        if not args.no_analysis and output.exists():
            stage_started = time.time()
            results = load_json(output)
            if not isinstance(results, list):
                raise TypeError(f"runner output must be a list: {output}")
            compare_results = None
            if args.compare_to:
                compare_path = Path(args.compare_to)
                compare_results = load_json(compare_path)
                if not isinstance(compare_results, list):
                    raise TypeError(f"compare output must be a list: {compare_path}")
                log_stage(f"compare_to={compare_path}", log_file, "analysis")
            summary = build_summary(results, args.split, load_case_meta(args.split), compare_results)
            summary_output.parent.mkdir(parents=True, exist_ok=True)
            summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            write_markdown(summary, analysis_output, output, log_output)
            log_stage(f"summary_json={summary_output}", log_file, "analysis")
            log_stage(f"analysis_md={analysis_output}", log_file, "analysis")
            log_stage(f"analysis finished elapsed={time.time() - stage_started:.2f}s", log_file, "analysis")
            print_summary(summary)
        elif not output.exists():
            log_stage("结果 JSON 不存在，跳过分析。", log_file, "analysis")

        log_stage(f"全部完成 elapsed={time.time() - started_at:.2f}s", log_file, "done")
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
