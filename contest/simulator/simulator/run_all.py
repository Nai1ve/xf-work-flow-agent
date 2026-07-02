"""
Run all cases and generate leaderboard.
"""

import json
from pathlib import Path
from env import IFTKEnv
from baseline_agent import BaselineAgent
from test_runner import aggregate_case_scores


def load_case(case_file: Path) -> dict:
    with open(case_file, encoding="utf-8") as f:
        return json.load(f)


def main():
    cases_dir = Path(__file__).resolve().parent.parent / "cases"
    env = IFTKEnv(cases_dir)
    agent = BaselineAgent(env)

    results = []
    for case_file in sorted(cases_dir.glob("*.json")):
        case_data = load_case(case_file)
        case_id = case_file.stem
        print(f"\n{'='*60}")
        print(f"Running case: {case_id}")
        print('='*60)
        try:
            base_score = agent.run(case_id)
            variant_scores = []
            for variant in case_data.get("robustness_variants", []):
                variant_id = variant["variant_id"]
                print(f"\n[Variant] {variant_id}")
                score = agent.run(f"{case_id}::{variant_id}")
                variant_scores.append({"variant_id": variant_id, **score})

            results.append(aggregate_case_scores(case_id, base_score, variant_scores))
        except Exception as e:
            print(f"[ERROR] {e}")
            results.append({"case_id": case_id, "error": str(e), "total": 0})

    print(f"\n{'='*60}")
    print("LEADERBOARD")
    print('='*60)
    for r in sorted(results, key=lambda x: x.get("total", 0), reverse=True):
        print(f"{r['case_id']:30s} | Total: {r.get('total', 0):5.1f} | "
              f"TSR: {r.get('TSR', 0):4.1f} | AS: {r.get('AS', 0):4.1f} | "
              f"ES: {r.get('ES', 0):4.1f} | RS: {r.get('RS', 0):4.1f}")

    # Save to JSON
    with open("leaderboard.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nResults saved to leaderboard.json")


if __name__ == "__main__":
    main()
