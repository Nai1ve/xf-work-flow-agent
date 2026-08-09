#!/usr/bin/env python3
"""离线冒烟：规则路径（无 LLM）下，MeetingOpPlanner → execute_ops 对真实 val 会议 case 的行为。

用法：
    .venv/bin/python scripts/smoke_meeting_ops.py [case_id ...]
    不带参数则跑全部 val mr/wf_0006 会议 case。

输出每个 case 的：规则 op 序列、工具调用历史、final_answer。用于快速抓 handler 逻辑 bug，
不消耗 LLM。LLM 路径的正确性另行用真实 runner 验证。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "submission"))
sys.path.insert(0, str(ROOT / "tmp" / "contest_val"))
sys.path.insert(0, str(ROOT / "tmp" / "contest_val" / "simulator"))

from utils.executor import MeetingroomExecutor  # noqa: E402
from utils.logger import ConsoleLogger  # noqa: E402
from utils.meeting_skill import MeetingOpPlanner  # noqa: E402
from utils.static_context import StaticContextStore  # noqa: E402
from utils.tool_contract import ToolContractReconciler  # noqa: E402


def main(case_ids: list[str]) -> int:
    from env import IFTKEnv  # noqa: E402  (simulator)

    runner_tree = ROOT / "tmp" / "contest_val"
    env = IFTKEnv(
        cases_dir=str(runner_tree / "cases"),
        tool_specs_path=str(runner_tree / "tool_specs.json"),
        workflow_data_path=str(runner_tree / "data" / "workflow_data.json"),
        meetingroom_data_path=str(runner_tree / "data" / "meetingroom_data.json"),
    )
    logger = ConsoleLogger(quiet=True) if hasattr(ConsoleLogger, "quiet") else None
    static = StaticContextStore(enabled=True, logger=logger)
    reconciler = ToolContractReconciler(static, logger=None)

    all_cases = case_ids or sorted(p.stem for p in (runner_tree / "cases").glob("beta_mr*.json"))
    fails = 0
    for case_id in all_cases:
        print(f"\n{'=' * 70}\n== {case_id}")
        obs = env.reset(case_id)
        registry = reconciler.reconcile(env.list_tools())
        query = obs.get("user_query") or ""
        now = obs.get("now") or ""
        mode = obs.get("mode")
        planner = MeetingOpPlanner(logger=None)
        plan = planner.plan(query, now, mode, gateway=None)  # None → 规则兜底
        ops = [op.action for op in plan.ops]
        print(f"query: {query}")
        print(f"ops: {ops}")
        executor = MeetingroomExecutor(env, registry, static, logger=None)
        final = executor.execute_ops(plan) if ops else {}
        print(f"history:")
        for name, args, result in executor._history:
            print(f"  {name}({json.dumps(args, ensure_ascii=False)}) -> {json.dumps(result, ensure_ascii=False)}")
        print(f"final_answer: {json.dumps(final, ensure_ascii=False)}")
        if not final:
            print("  [WARN] final_answer 为空")
            fails += 1
    print(f"\n{fails} cases returned empty final_answer")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main([a for a in sys.argv[1:]]))
