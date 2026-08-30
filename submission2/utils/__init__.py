"""submission.utils — 企业流程 Agent 的分层实现模块。

按 `technical_design.md` §2 的五层架构逐步填充：
感知层 / 理解层 / 规划层 / 执行层 / 输出层 + LLMGateway + 规则降级。
"""
"""V2 runtime utility package."""

from .dag_runtime import (
    AgentState,
    DagTask,
    DomainResult,
    NodeOutcome,
    NodeSpec,
    NodeStatus,
    SkillSpec,
    TaskDag,
    TaskSpec,
)
from .meetingroom_directory import MeetingroomDirectory
from .tool_contract import EffectiveToolRegistry, ToolContractReconciler, ToolRegistry

__all__ = [
    "AgentState",
    "DagTask",
    "DomainResult",
    "NodeOutcome",
    "NodeSpec",
    "NodeStatus",
    "SkillSpec",
    "TaskDag",
    "TaskSpec",
    "MeetingroomDirectory",
    "EffectiveToolRegistry",
    "ToolContractReconciler",
    "ToolRegistry",
]
