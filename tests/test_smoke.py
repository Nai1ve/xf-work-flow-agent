"""冒烟测试：验证提交入口契约 + 感知层接入。

最小骨架阶段只做接口级验证；本文件同时覆盖感知层在 run 中的接入
（reset → list_tools → 对账 → 返回 {}）。分层单元测试见
test_static_context.py / test_tool_contract.py / test_build_static_context.py。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_my_agent_module():
    path = ROOT / "submission" / "my_agent.py"
    spec = importlib.util.spec_from_file_location("contestant_agent", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["contestant_agent"] = module
    spec.loader.exec_module(module)
    return module


class _FakeEnv:
    """最小受控环境：只暴露 runner 允许的 reset / list_tools / call_tool / reply。"""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self, case_id: str) -> dict[str, Any]:
        self.resets += 1
        return {"case_id": case_id, "mode": "single_turn", "step_budget": 8}

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "meetingroom.room.list",
                "description": "查询会议室候选",
                "args_schema": {
                    "type": "object",
                    "properties": {"day": {"type": "string", "format": "date"}},
                    "required": ["day"],
                },
            }
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("感知层阶段不应调用工具")

    def reply(self, message: str) -> dict[str, Any]:
        raise AssertionError("感知层阶段不应回复")


def test_my_agent_contract() -> None:
    """MyAgent 类存在且暴露 run(case_id) -> dict。"""
    module = _load_my_agent_module()
    assert hasattr(module, "MyAgent")
    agent = module.MyAgent(_FakeEnv())
    assert callable(getattr(agent, "run"))


def test_run_returns_dict_without_raising() -> None:
    """run() 永不 raise、永不返回 None（0 分防线）。"""
    module = _load_my_agent_module()
    agent = module.MyAgent(_FakeEnv())
    result = agent.run("beta_mr_0001")
    assert isinstance(result, dict)


def test_run_reconciles_runtime_tools(caplog) -> None:
    """run 中完成 reset → list_tools → 对账，并输出感知层对账日志。"""
    module = _load_my_agent_module()
    agent = module.MyAgent(_FakeEnv())
    with caplog.at_level("INFO", logger="agent"):
        agent.run("beta_mr_0001")
    assert any("对账完成" in record.message for record in caplog.records)


# =====================================================================
# ④0245：LLM#2 的会议计划覆盖全部 meeting 单元，只在首个单元执行一次
# =====================================================================

class _FakeQueryEnv(_FakeEnv):
    """带 user_query / now 的受控环境，用于推进到执行层。"""

    def reset(self, case_id: str) -> dict[str, Any]:
        self.resets += 1
        return {
            "case_id": case_id,
            "mode": "single_turn",
            "step_budget": 8,
            "user_query": "帮我查一下李帅的工位，然后在他工位附近订下周二下午2点到3点"
                          "一个8人以上带屏幕的会议室，主题是一对一。",
            "now": "2026-05-12T08:00:00",
        }


class _FakeMeetingIR:
    def __init__(self, units: list[tuple[str, list[int], str]]) -> None:
        self.task_units = [
            SimpleNamespace(unit_type=t, depends_on=d, sub_query=q) for t, d, q in units
        ]
        self.confidence = 0.9
        self.source = "llm"
        self.elapsed_s = 1.0
        self.mode = "single_turn"

    def ordered_units(self) -> list[Any]:
        return self.task_units


class _FakeMeetingPlan:
    def __init__(self) -> None:
        self.ops = [
            SimpleNamespace(action="query", target=None),
            SimpleNamespace(action="book", target=None),
        ]
        self.source = "llm"
        self.confidence = 0.9
        self.elapsed_s = 1.0


def _count_execute_ops_env(monkeypatch, units: list[tuple[str, list[int], str]]) -> dict:
    """monkeypatch MeetingSkill/MeetingroomExecutor，返回 execute_ops 计数。"""
    module = _load_my_agent_module()
    state: dict[str, int] = {"execute_ops_calls": 0}

    class _FakeExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def execute_ops(self, plan: Any) -> dict[str, Any]:
            state["execute_ops_calls"] += 1
            return {"booking_result": {"status": "success", "room_id": "0552-005"}}

    class _FakeSkill:
        last_planner_gateway = None
        last_timings = {"recognize_s": 0.1, "orchestrate_s": 0.1}

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
            return _FakeMeetingIR(units), _FakeMeetingPlan()

    monkeypatch.setattr(module, "MeetingroomExecutor", _FakeExecutor)
    monkeypatch.setattr(module, "MeetingSkill", _FakeSkill)
    return module, state


def test_meeting_plan_executed_once_for_multiple_meeting_units(monkeypatch) -> None:
    """0245 拆成两个 meeting 单元 → execute_ops 只调用一次，结果保留。"""
    module, state = _count_execute_ops_env(
        monkeypatch,
        [
            ("meeting", [], "查工位"),
            ("meeting", [0], "订房"),
        ],
    )
    agent = module.MyAgent(_FakeQueryEnv())
    result = agent.run("beta_mr_0245")
    assert state["execute_ops_calls"] == 1
    assert result["booking_result"]["status"] == "success"


def test_non_meeting_units_never_execute_ops(monkeypatch) -> None:
    """leave/budget 单元安全空：跳过，不触发 execute_ops。"""
    module, state = _count_execute_ops_env(monkeypatch, [("leave", [], "请假")])
    agent = module.MyAgent(_FakeQueryEnv())
    result = agent.run("beta_mr_0001")
    assert state["execute_ops_calls"] == 0
    assert result == {}
