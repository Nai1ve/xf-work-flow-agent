#!/usr/bin/env python3
"""P0 计时实验：parallel（LLM#1∥LLM#2 并发） vs single（合并一次请求）定默认模式。

在 val 会议 case 上各跑一遍真实 runner（线上 LLM），对比每 case 墙钟与 TSR/AS。
判定：明显更快者胜；差距 <20% 时取 TSR/AS 更高者。结果供 config.json 的
`meeting.llm.mode` 定稿（默认先 parallel，bench 后修正）。

用法：
    .venv/bin/python scripts/bench_meeting_llm.py [--limit N] [--outdir reports/bench]
    MEETING_LLM_MODE=... 由脚本自行注入，不需要手动设。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_AGENT = ROOT / "scripts" / "run_agent.py"
PY = os.environ.get("PY", str(ROOT / ".venv" / "bin" / "python"))
MODES = ("parallel", "single")


def meeting_cases(limit: int | None = None) -> list[str]:
    cases_dir = ROOT / "tmp" / "contest_val" / "cases"
    names = sorted(p.stem for p in cases_dir.glob("beta_mr*.json"))
    if limit:
        names = names[:limit]
    return names


def run_mode(mode: str, cases: list[str], outdir: Path) -> dict[str, dict]:
    out = outdir / f"bench_{mode}.json"
    log = outdir / f"bench_{mode}.stdout"
    env = dict(os.environ)
    env["MEETING_LLM_MODE"] = mode
    cmd = [
        PY, str(RUN_AGENT),
        "--agent", str(ROOT / "submission" / "my_agent.py"),
        "--split", "val",
        "--parallel", "1",
        "--python", PY,
        "--no-analysis",
        "--output", str(out),
    ]
    for case in cases:
        cmd += ["--case", case]
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as f:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    wall = time.monotonic() - start
    results: dict[str, dict] = {}
    if proc.returncode == 0 and out.exists():
        for item in json.loads(out.read_text(encoding="utf-8")):
            results[str(item.get("case_id"))] = {
                "elapsed": float(item.get("elapsed_seconds", 0) or 0),
                "TSR": float(item.get("TSR", 0)),
                "AS": float(item.get("AS", 0)),
                "total": float(item.get("total", 0)),
            }
    print(f"[bench] mode={mode} cases={len(cases)} wall={wall:.1f}s -> {out}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--outdir", default=str(ROOT / "reports" / "bench"))
    args = parser.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cases = meeting_cases(args.limit)
    print(f"[bench] val 会议 case: {cases}")

    runs = {m: run_mode(m, cases, outdir) for m in MODES}
    if not all(runs.values()):
        print("[bench] 某模式无结果（runner 失败？），退出")
        return 1

    print(f"\n{'case':<20} {'parallel(el/TSR)':>18} {'single(el/TSR)':>18}  {'快者'}")
    for case in cases:
        p = runs["parallel"].get(case, {})
        s = runs["single"].get(case, {})
        p_el, s_el = p.get("elapsed", 0), s.get("elapsed", 0)
        winner = "par" if p_el <= s_el else "single"
        print(
            f"{case:<20} {p_el:>8.2f}s/{p.get('TSR', 0):>3.0f} "
            f"{s_el:>8.2f}s/{s.get('TSR', 0):>3.0f}  {winner}"
        )

    def avg(d: dict, key: str) -> float:
        vals = [v.get(key, 0) for v in d.values()]
        return sum(vals) / len(vals) if vals else 0

    p_avg = avg(runs["parallel"], "elapsed")
    s_avg = avg(runs["single"], "elapsed")
    p_tsr = avg(runs["parallel"], "TSR")
    s_tsr = avg(runs["single"], "TSR")
    p_as = avg(runs["parallel"], "AS")
    s_as = avg(runs["single"], "AS")
    p_total = avg(runs["parallel"], "total")
    s_total = avg(runs["single"], "total")
    print(f"\n平均墙钟  parallel={p_avg:.2f}s  single={s_avg:.2f}s  "
          f"(差 {abs(p_avg - s_avg) / max(p_avg, s_avg) * 100:.0f}%)")
    print(f"平均 TSR  parallel={p_tsr:.2f}  single={s_tsr:.2f}")
    print(f"平均 AS   parallel={p_as:.2f}  single={s_as:.2f}")
    print(f"平均总分  parallel={p_total:.2f}  single={s_total:.2f}")

    if p_avg < s_avg * 0.8:
        rec = "parallel"
        why = f"并发显著更快（-{(1 - p_avg / s_avg) * 100:.0f}%）"
    elif s_avg < p_avg * 0.8:
        rec = "single"
        why = f"合并显著更快（-{(1 - s_avg / p_avg) * 100:.0f}%）"
    else:
        rec = "parallel" if p_total >= s_total else "single"
        why = "时间差 <20%，按 TSR/AS 总分取更高者"
    print(f"\n推荐默认模式: {rec}（{why}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
