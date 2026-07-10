#!/usr/bin/env python3
"""Summarize MyAgent runtime timing debug logs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "avg": round(mean(values), 3),
        "p50": round(median(values), 3),
        "p90": round(percentile(values, 0.9), 3),
        "max": round(max(values), 3),
    }


def read_events(paths: list[Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    item["_log_path"] = str(path)
                    events.append(item)
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    finishes = [item for item in events if item.get("event") == "finish"]
    llm_calls = [item for item in events if item.get("event") == "llm_call"]
    tools = [item for item in events if item.get("event") in {"tool", "tool_cache", "reply"}]
    read_plans = [item for item in events if item.get("event") == "read_plan"]
    read_batches = [item for item in events if item.get("event") == "read_batch"]
    read_tasks = [item for item in events if item.get("event") == "read_task"]

    by_llm_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in llm_calls:
        key = "|".join(
            [
                str(item.get("profile") or ""),
                str(item.get("model") or ""),
                str(item.get("context_pack_type") or ""),
            ]
        )
        by_llm_key[key].append(item)

    def ledger_value(item: dict[str, Any], key: str) -> float:
        summary = item.get("ledger_summary") if isinstance(item.get("ledger_summary"), dict) else {}
        try:
            return float(summary.get(key) or 0)
        except Exception:
            return 0.0

    return {
        "cases": {
            "count": len(finishes),
            "elapsed_seconds": stats([float(item.get("elapsed_seconds") or 0) for item in finishes]),
            "llm_elapsed_seconds": stats([float(item.get("llm_elapsed_seconds") or 0) for item in finishes]),
            "fast_llm_elapsed_seconds": stats([float(item.get("llm_elapsed_fast_seconds") or 0) for item in finishes]),
            "strong_llm_elapsed_seconds": stats([float(item.get("llm_elapsed_strong_seconds") or 0) for item in finishes]),
            "tool_elapsed_seconds": stats([float(item.get("tool_elapsed_seconds") or 0) for item in finishes]),
            "read_elapsed_seconds": stats([float(item.get("read_elapsed_seconds") or 0) for item in finishes]),
            "read_plan_batches": stats([float(item.get("read_plan_batches") or 0) for item in finishes]),
            "read_tasks_total": stats([float(item.get("read_tasks_total") or 0) for item in finishes]),
            "read_tasks_cached": stats([float(item.get("read_tasks_cached") or 0) for item in finishes]),
            "read_tasks_parallel_eligible": stats(
                [float(item.get("read_tasks_parallel_eligible") or 0) for item in finishes]
            ),
            "ledger_entries": stats([ledger_value(item, "entries") for item in finishes]),
            "ledger_reads": stats([ledger_value(item, "reads") for item in finishes]),
            "ledger_writes": stats([ledger_value(item, "writes") for item in finishes]),
            "ledger_preflights": stats([ledger_value(item, "preflights") for item in finishes]),
            "ledger_candidate_decisions": stats([ledger_value(item, "candidate_decisions") for item in finishes]),
            "non_llm_elapsed_seconds": stats([float(item.get("non_llm_elapsed_seconds") or 0) for item in finishes]),
            "steps_used": stats([float(item.get("steps_used") or 0) for item in finishes]),
        },
        "llm_calls": {
            "count": len(llm_calls),
            "success": sum(1 for item in llm_calls if item.get("success")),
            "failure": sum(1 for item in llm_calls if not item.get("success")),
            "elapsed_seconds": stats([float(item.get("elapsed_seconds") or 0) for item in llm_calls]),
            "prompt_chars": stats([float(item.get("prompt_chars") or 0) for item in llm_calls]),
            "context_chars": stats([float(item.get("context_chars") or 0) for item in llm_calls]),
            "by_profile_model_context": {
                key: {
                    "count": len(items),
                    "success": sum(1 for item in items if item.get("success")),
                    "failure": sum(1 for item in items if not item.get("success")),
                    "elapsed_seconds": stats([float(item.get("elapsed_seconds") or 0) for item in items]),
                    "prompt_chars": stats([float(item.get("prompt_chars") or 0) for item in items]),
                    "context_chars": stats([float(item.get("context_chars") or 0) for item in items]),
                }
                for key, items in sorted(by_llm_key.items())
            },
        },
        "actions": {
            "count": len(tools),
            "elapsed_seconds": stats([float(item.get("elapsed_seconds") or 0) for item in tools]),
        },
        "read_plan": {
            "plans": len(read_plans),
            "batches": len(read_batches),
            "tasks": len(read_tasks),
            "task_success": sum(1 for item in read_tasks if item.get("success")),
            "task_failure": sum(1 for item in read_tasks if item.get("success") is False),
            "task_elapsed_seconds": stats([float(item.get("elapsed_seconds") or 0) for item in read_tasks]),
            "batch_task_count": stats([float(item.get("task_count") or 0) for item in read_batches]),
            "ready_task_count": stats([float(item.get("ready_count") or 0) for item in read_plans]),
            "parallel_batches": sum(1 for item in read_batches if item.get("parallel_requested")),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    events = read_events(args.logs)
    summary = summarize(events)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
