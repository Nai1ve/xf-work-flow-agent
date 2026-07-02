"""
Run a contestant MyAgent against local contest cases.

Examples:
    python simulator/test_runner.py --agent my_submission/my_agent.py --case beta_mr_wf_0001 --verbose
    python simulator/test_runner.py --agent my_submission/my_agent.py --limit 20 --output results.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from typing import Any

from env import IFTKEnv
from requirements_policy import validate_requirements_file


DEFAULT_TIMEOUT_SECONDS = 60


class ContestantEnv:
    """Public environment surface exposed to contestant agents."""

    __slots__ = ("__env",)

    def __init__(self, env: IFTKEnv):
        self.__env = env

    def reset(self, case_id: str) -> dict:
        return self.__env.reset(case_id)

    def list_tools(self) -> list[dict]:
        return self.__env.list_tools()

    def call_tool(self, name: str, args: dict) -> dict:
        return self.__env.call_tool(name, args)

    def reply(self, message: str) -> dict:
        return self.__env.reply(message)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"contestant environment does not expose {name}")


def load_agent_class(agent_path: Path):
    if not agent_path.exists():
        raise FileNotFoundError(f"agent file not found: {agent_path}")

    module_dir = str(agent_path.parent.resolve())
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)

    spec = importlib.util.spec_from_file_location("contestant_agent", agent_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import agent file: {agent_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    agent_class = getattr(module, "MyAgent", None)
    if agent_class is None:
        raise AttributeError(f"{agent_path} must define MyAgent")
    return agent_class


def run_agent_case(agent_class, cases_dir: Path, case_ref: str) -> dict[str, Any]:
    env = IFTKEnv(cases_dir)
    agent = agent_class(ContestantEnv(env))
    final_answer = agent.run(case_ref)
    if not isinstance(final_answer, dict):
        raise TypeError("MyAgent.run() must return a final_answer dict")
    return env.done(final_answer)


def parse_case_ref(case_ref: str) -> tuple[str, str | None]:
    if "::" not in case_ref:
        return case_ref, None
    case_id, variant_id = case_ref.split("::", 1)
    return case_id, variant_id


def build_case_refs(case_refs: list[str], limit: int | None = None) -> list[str]:
    if limit is not None:
        return case_refs[:limit]
    return case_refs


def list_case_ids(cases_dir: Path) -> list[str]:
    return [path.stem for path in sorted(cases_dir.glob("*.json"))]


def load_case_data(cases_dir: Path, case_id: str) -> dict[str, Any]:
    with open(cases_dir / f"{case_id}.json", encoding="utf-8") as f:
        return json.load(f)


def list_variant_refs(cases_dir: Path, case_id: str) -> list[str]:
    case_data = load_case_data(cases_dir, case_id)
    return [
        f"{case_id}::{variant['variant_id']}"
        for variant in case_data.get("robustness_variants", [])
    ]


def aggregate_case_scores(case_id: str, base_score: dict[str, Any], variant_scores: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"case_id": case_id, **base_score}
    if variant_scores:
        pass_rate = sum(1 for score in variant_scores if score.get("task_passed")) / len(variant_scores)
        rs = round(10.0 * pass_rate, 2)
    else:
        rs = 10.0 if base_score.get("task_passed") else 0.0

    result["RS"] = rs
    result["total"] = round(
        float(result.get("TSR", 0))
        + float(result.get("AS", 0))
        + float(result.get("ES", 0))
        + rs,
        2,
    )
    result["variant_results"] = variant_scores
    return result


def run_one(agent_class, cases_dir: Path, case_ref: str, timeout: int | None = None) -> dict[str, Any]:
    case_id, variant_id = parse_case_ref(case_ref)

    started = time.time()
    try:
        if timeout is None:
            score = run_agent_case(agent_class, cases_dir, case_ref)
        else:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_agent_case, agent_class, cases_dir, case_ref)
                score = future.result(timeout=timeout)
        result = {"case_id": case_id, **score}
        if variant_id is not None:
            result["variant_id"] = variant_id
        return result
    except FuturesTimeoutError:
        result = {
            "case_id": case_id,
            "error": f"case timed out after {timeout} seconds",
            "total": 0,
            "TSR": 0,
            "AS": 0,
            "ES": 0,
            "RS": 0,
            "task_passed": False,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        if variant_id is not None:
            result["variant_id"] = variant_id
        return result
    except Exception as exc:
        result = {
            "case_id": case_id,
            "error": str(exc),
            "total": 0,
            "TSR": 0,
            "AS": 0,
            "ES": 0,
            "RS": 0,
            "task_passed": False,
            "elapsed_seconds": round(time.time() - started, 2),
        }
        if variant_id is not None:
            result["variant_id"] = variant_id
        return result


def run_case_group(
    agent_class,
    cases_dir: Path,
    case_ref: str,
    timeout: int | None = None,
    include_variants: bool = True,
) -> dict[str, Any]:
    case_id, variant_id = parse_case_ref(case_ref)
    if variant_id is not None or not include_variants:
        return run_one(agent_class, cases_dir, case_ref, timeout=timeout)

    base_score = run_one(agent_class, cases_dir, case_id, timeout=timeout)
    variant_scores = [
        run_one(agent_class, cases_dir, variant_ref, timeout=timeout)
        for variant_ref in list_variant_refs(cases_dir, case_id)
    ]
    return aggregate_case_scores(case_id, base_score, variant_scores)


def run_cases(
    agent_class,
    cases_dir: Path,
    case_refs: list[str],
    parallel: int = 1,
    timeout: int | None = None,
    include_variants: bool = True,
) -> list[dict[str, Any]]:
    if parallel <= 1:
        return [
            run_case_group(agent_class, cases_dir, case_ref, timeout=timeout, include_variants=include_variants)
            for case_ref in case_refs
        ]

    results: list[dict[str, Any]] = [None] * len(case_refs)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        future_map = {
            executor.submit(run_case_group, agent_class, cases_dir, case_ref, timeout, include_variants): idx
            for idx, case_ref in enumerate(case_refs)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            results[idx] = future.result()
    return results


def print_result(result: dict[str, Any], verbose: bool) -> None:
    if verbose:
        print(f"[Case] {result['case_id']}" + (f"::{result['variant_id']}" if result.get("variant_id") else ""))
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
        print(
            "  total={total:.2f} TSR={TSR:.2f} AS={AS:.2f} ES={ES:.2f} RS={RS:.2f}".format(
                total=float(result.get("total", 0)),
                TSR=float(result.get("TSR", 0)),
                AS=float(result.get("AS", 0)),
                ES=float(result.get("ES", 0)),
                RS=float(result.get("RS", 0)),
            )
        )
        print(
            "  task_passed={task_passed} steps={steps} elapsed={elapsed:.2f}s".format(
                task_passed=result.get("task_passed", False),
                steps=result.get("steps_used", "-"),
                elapsed=float(result.get("elapsed_seconds", 0)),
            )
        )
        variant_results = result.get("variant_results") or []
        if variant_results:
            passed = sum(1 for item in variant_results if item.get("task_passed"))
            print(f"  variants_passed={passed}/{len(variant_results)}")
        return

    status = "PASS" if result.get("task_passed") else "FAIL"
    suffix = f"::{result['variant_id']}" if result.get("variant_id") else ""
    error = f" error={result['error']}" if result.get("error") else ""
    print(f"{status} {result['case_id']}{suffix}: {float(result.get('total', 0)):.2f}{error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a contestant MyAgent against local contest cases.")
    parser.add_argument("--agent", required=True, help="Path to contestant my_agent.py")
    parser.add_argument("--case", action="append", dest="cases", help="Case id, repeatable. Supports case::variant.")
    parser.add_argument("--cases-dir", default=str(Path(__file__).resolve().parent.parent / "cases"))
    parser.add_argument("--limit", type=int, help="Run the first N cases when --case is omitted.")
    parser.add_argument("--output", help="Write JSON results to this path.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed per-case results.")
    parser.add_argument("--parallel", type=int, default=1, help="Run cases in parallel with N workers.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-case timeout in seconds.")
    parser.add_argument("--skip-variants", action="store_true", help="Do not expand robustness_variants for base cases.")
    parser.add_argument("--validate-requirements", action="store_true", default=True)
    parser.add_argument("--skip-requirements-check", action="store_false", dest="validate_requirements")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    agent_path = Path(args.agent)
    cases_dir = Path(args.cases_dir)
    agent_class = load_agent_class(agent_path)

    if args.validate_requirements:
        requirement_path = agent_path.parent / "requirements.txt"
        errors = validate_requirements_file(requirement_path)
        if errors:
            raise SystemExit("requirements.txt validation failed:\n- " + "\n- ".join(errors))

    case_refs = args.cases or list_case_ids(cases_dir)
    case_refs = build_case_refs(case_refs, args.limit)
    if not case_refs:
        raise ValueError(f"no cases found under {cases_dir}")

    results = run_cases(
        agent_class,
        cases_dir,
        case_refs,
        parallel=max(1, args.parallel),
        timeout=args.timeout,
        include_variants=not args.skip_variants,
    )
    for result in results:
        print_result(result, args.verbose)

    avg = sum(float(item.get("total", 0)) for item in results) / len(results)
    passed = sum(1 for item in results if item.get("task_passed"))
    print(f"\nAverage: {avg:.2f}/100  Passed: {passed}/{len(results)}")

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Results saved to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
