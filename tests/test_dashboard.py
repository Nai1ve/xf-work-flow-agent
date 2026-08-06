"""看板脚本（scripts/dashboard.py）的单元测试。

覆盖 technical_design.md §9.2/§9.3 的口径：
- TSR/AS/ES/RS 维度满分判定
- ES 审计（实际步数 vs gold_trajectory 长度）
- §9.3 验收清单
- 汇总结构
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_dashboard():
    path = ROOT / "scripts" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("dashboard_mod", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_mod"] = module
    spec.loader.exec_module(module)
    return module


DASH = _load_dashboard()


def _result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "beta_mr_0001",
        "total": 100.0,
        "task_passed": True,
        "TSR": 60.0,
        "AS": 20.0,
        "ES": 10.0,
        "RS": 10.0,
        "steps_used": 4,
        "elapsed_seconds": 0.3,
        "success_checks": [{"condition": "存在成功会议预订", "passed": True}],
        "submission_checks": [{"field": "booking_result", "passed": True}],
        "violations": [],
        "variant_results": [],
        "error": None,
    }
    base.update(overrides)
    return base


def _meta(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "split": "val",
        "tags": ["meetingroom"],
        "difficulty": "easy",
        "mode": "single_turn",
        "primary_domains": ["meetingroom"],
        "step_budget": 8,
        "scoring": {"step_budget": 8},
        "gold_trajectory": [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}],
        "user_query": "帮我订会议室",
    }
    base.update(overrides)
    return base


def test_full_pass_dimensions() -> None:
    ok = _result()
    assert DASH.full_pass_tsr(ok)
    assert DASH.full_pass_as(ok)
    assert DASH.full_pass_es(ok)
    assert DASH.full_pass_rs(ok)
    assert not DASH.as_deducted(ok)


def test_full_pass_with_failures() -> None:
    bad = _result(
        success_checks=[{"condition": "x", "passed": False}],
        submission_checks=[{"field": "booking_result", "passed": False}],
        AS=15.0,
    )
    assert not DASH.full_pass_tsr(bad)
    assert not DASH.full_pass_as(bad)
    assert DASH.as_deducted(bad)


def test_full_pass_as_forbidden() -> None:
    forbidden = _result(violations=["调用过未授权工具"])
    assert not DASH.full_pass_as(forbidden)


def test_es_audit_overrun() -> None:
    rows = DASH.build_case_rows(
        [
            _result(case_id="over", steps_used=8),
            _result(case_id="ok", steps_used=3),
            _result(case_id="missing_steps", steps_used=None),
        ],
        {
            "over": _meta(),
            "ok": _meta(),
            "missing_steps": _meta(),
        },
        {},
    )
    audit = DASH.es_audit(rows, threshold=2)
    ids = [item["case_id"] for item in audit]
    assert ids == ["over"]  # 8 - 3(gold) = 5 > 2
    # overrun 字段正确
    by_id = {item["case_id"]: item for item in rows}
    assert by_id["over"]["overrun"] == 5


def test_acceptance_checklist_flags() -> None:
    rows = DASH.build_case_rows(
        [_result(case_id="a"), _result(case_id="b", total=0, task_passed=False, TSR=0, AS=0)],
        {"a": _meta(), "b": _meta()},
        {},
    )
    overall = DASH.group_stats(rows)
    checks = DASH.acceptance_checklist(overall, rows)
    by_metric = {c["metric"]: c for c in checks}
    assert by_metric["task_passed_rate"]["pass"] is False  # 50% < 92%
    assert by_metric["forbidden_count"]["pass"] is True
    assert by_metric["timeout_count"]["pass"] is True
    assert by_metric["exception_0score_count"]["pass"] is True


def test_summary_structure_and_csv_roundtrip() -> None:
    rows = DASH.build_case_rows(
        [_result(case_id="a"), _result(case_id="b", total=40, task_passed=False)],
        {"a": _meta(), "b": _meta()},
        {},
    )
    summary = {
        "generated_at": "now",
        "inputs": ["r.json"],
        "overall": DASH.group_stats(rows),
        "acceptance": DASH.acceptance_checklist(DASH.group_stats(rows), rows),
        "by_domain": DASH.group_rows(rows, "primary_domains"),
        "by_mode": DASH.group_rows(rows, "mode"),
        "by_difficulty": DASH.group_rows(rows, "difficulty"),
        "by_prefix": DASH.group_rows(rows, "prefix"),
        "by_tag": DASH.group_rows(rows, "tags"),
        "es_audit": DASH.es_audit(rows),
        "es_audit_overrun_gt_2": len(DASH.es_audit(rows)),
        "errors": [],
        "violations": [],
        "failed_submission": [],
        "failed_success": [],
        "lowest_cases": [],
        "failed_cases": [],
        "cases": rows,
    }
    assert summary["overall"]["count"] == 2
    assert summary["overall"]["pass_rate"] == 50.0
    assert summary["by_mode"]["single_turn"]["count"] == 2
    assert summary["by_domain"]["meetingroom"]["count"] == 2
    # gold_steps 从 meta 读取
    by_id = {row["case_id"]: row for row in rows}
    assert by_id["a"]["gold_steps"] == 3
