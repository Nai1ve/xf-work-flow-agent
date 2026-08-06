"""冒烟测试：验证提交入口契约与 harness 可用性。

最小骨架阶段只做接口级验证；后续随各层模块补充分层单元测试。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
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
    def __init__(self) -> None:
        self.resets = 0

    def reset(self, case_id: str) -> dict[str, Any]:
        self.resets += 1
        return {"case_id": case_id, "mode": "single_turn", "step_budget": 8}


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
