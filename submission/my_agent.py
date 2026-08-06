"""企业流程 Agent 主入口（v3 最小骨架）。

架构规格见仓库根目录 `technical_design.md`：
- 本文件只承载 `MyAgent` 入口：`run()` 永不 raise、永不返回 None。
- 感知 / 理解 / 规划 / 执行 / 输出各层模块，后续按设计文档拆入 `submission/utils/`，
  本入口保持为薄封装。
"""

from __future__ import annotations

from typing import Any


class MyAgent:
    """与官方 runner 的契约入口。

    Args:
        env: IFTKEnv 的受控代理（仅暴露 reset / list_tools / call_tool / reply）。
    """

    def __init__(self, env: Any) -> None:
        self.env = env

    def run(self, case_id: str) -> dict[str, Any]:
        """执行单个 case，返回结构化 final_answer。

        最小骨架：先 `reset` 拿到观察，后续在此接入「理解 → 规划 → 执行 → 输出」
        链路。顶层兜底保证任何异常都落到 `{}`，避免 case 记 0 分。
        """
        try:
            obs = self.env.reset(case_id)
            del obs  # 观察留待理解层消费
            return {}
        except Exception:
            return {}
