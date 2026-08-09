"""企业流程 Agent 主入口（v3 · 感知层 + 理解层 + S1/S2 执行层）。

架构规格见仓库根目录 `technical_design.md`：
- 本文件只承载 `MyAgent` 入口：`run()` 永不 raise、永不返回 None；
- 感知层（StaticContextStore + ToolContractReconciler，§3.1）：reset →
  list_tools → 对账 → 输出对账摘要；
- 理解层（understanding.py）：IntentRecognizer 先把 user_query 分解为 N 个子问题
  （task_units）并路由到处理路径（meeting/leave/budget），**不做参数抽取**；
- 执行层（executor.py）：本里程碑为最小桥接——meeting 单元临时用现有细粒度
  IntentRouter + MeetingConstraintExtractor 喂 S1/S2 执行器（不追分），leave/budget
  单元返回安全空 {}，其流程 SOP 下一里程碑接入；
- 本入口保持为薄封装，分层模块都在 `submission/utils/`。

控制台日志说明：日志走 stderr（官方 runner 会合并进运行日志），按层分节，
仅供本地评估时人工审查；不输出任何 case 答案（reference / success_check /
gold_trajectory 一律不写进日志）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from utils.executor import MeetingroomExecutor
from utils.llm_gateway import LLMGateway
from utils.logger import ConsoleLogger, configure_console
from utils.meeting_skill import MeetingSkill
from utils.static_context import StaticContextStore
from utils.tool_contract import ToolContractReconciler
from utils.understanding import UNIT_MEETING


class MyAgent:
    """与官方 runner 的契约入口。

    Args:
        env: IFTKEnv 的受控代理（仅暴露 reset / list_tools / call_tool / reply）。
    """

    def __init__(self, env: Any) -> None:
        """初始化感知层组件。

        注意：按 submission_spec 约束，__init__ 不调用 env.reset（reset 在 run 内做）。
        """
        self.env = env
        # 幂等：根 logger 只挂一个控制台 handler，多线程并发实例化安全。
        configure_console()
        self.logger = ConsoleLogger().child("感知层")
        self.static_context = StaticContextStore(
            enabled=self._static_context_enabled(),
            logger=self.logger,
        )
        self.reconciler = ToolContractReconciler(
            self.static_context,
            logger=self.logger.child("对账"),
        )

    def _static_context_enabled(self) -> bool:
        """从 config.json 读取 static_context_enabled（默认 True）。

        静态上下文属于先验增强：即使被关闭，Agent 仍完全依赖运行时证据工作。
        """
        config_path = Path(__file__).resolve().parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return bool(config.get("runtime", {}).get("static_context_enabled", True))
        except (OSError, ValueError):
            return True

    def _meeting_llm_mode(self) -> str:
        """读取 meeting.llm.mode（默认 "sequential"）：env 覆盖 > config.json。

        用户定稿架构为**顺序流水线**（识别 → 编排 → 执行，无并发）——该模式
        即当前唯一路径；保留读取仅为配置可审计（MEETING_LLM_MODE 供 bench 验证）。
        """
        override = os.environ.get("MEETING_LLM_MODE")
        if override:
            return override
        config_path = Path(__file__).resolve().parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return str(
                config.get("meeting", {}).get("llm", {}).get("mode", "sequential")
            )
        except (OSError, ValueError):
            return "sequential"

    def run(self, case_id: str) -> dict[str, Any]:
        """执行单个 case，返回结构化 final_answer。

        流程：reset → list_tools → 对账（感知层）→ 意图/约束解析（理解层）→
        S1/S2 执行（执行层）→ final_answer。非 S1/S2 意图返回空 {}（0 分防线）。
        顶层兜底保证任何异常都落到 {}，避免 case 记 0 分。

        Args:
            case_id: 目标 case 标识（如 "beta_mr_0001"）。

        Returns:
            结构化 final_answer dict。
        """
        try:
            run_start = time.monotonic()
            self.logger.section(f"case={case_id}")
            obs = self.env.reset(case_id)
            # list_tools 不消耗步数预算，是 run 开始时的前置认知（设计文档 §2.1）。
            runtime_tools = self.env.list_tools()
            registry = self.reconciler.reconcile(runtime_tools)
            self.logger.info(f"对账结果: {json.dumps(registry.status(), ensure_ascii=False)}")

            user_query = obs.get("user_query") or ""
            now_iso = obs.get("now") or ""
            mode = obs.get("mode")
            if not user_query or not now_iso:
                self.logger.warning("obs 缺少 user_query/now，返回空")
                return {}

            # —— 理解层：顺序流水线 ——
            # 识别层 LLM#1（gateway）→ 编排层 LLM#2（skill 内部另建 gateway），
            # 每层独立计时；总耗时在此处汇总。
            understand_log = self.logger.child("理解层")
            # gateway 在 run 内构建：预算按 case 隔离，实例复用也不串预算。
            gateway = LLMGateway(logger=understand_log.child("LLM#1"))
            meeting_skill = MeetingSkill(logger=understand_log)
            ir, meeting_plan = meeting_skill.run(user_query, now_iso, mode, gateway)
            units_desc = [
                {
                    "unit_type": u.unit_type,
                    "depends_on": u.depends_on,
                    "sub_query": u.sub_query,
                }
                for u in ir.task_units
            ]
            understand_log.info(
                f"识别: units={json.dumps(units_desc, ensure_ascii=False)} "
                f"confidence={ir.confidence} source={ir.source} "
                f"elapsed={ir.elapsed_s:.2f}s mode={ir.mode or '-'}"
            )
            understand_log.info(
                f"会议编排: ops={[op.action for op in meeting_plan.ops]} "
                f"source={meeting_plan.source} confidence={meeting_plan.confidence} "
                f"elapsed={meeting_plan.elapsed_s:.2f}s"
            )
            # 各部分模型时间：识别（LLM#1）与编排（LLM#2）各自 gateway 统计。
            llm1_stats = gateway.stats_summary()
            llm2_stats = (
                meeting_skill.last_planner_gateway.stats_summary()
                if meeting_skill.last_planner_gateway is not None
                else None
            )
            understand_log.info(
                f"LLM#1 统计: {json.dumps(llm1_stats, ensure_ascii=False)}"
            )
            if llm2_stats is not None:
                understand_log.info(
                    f"LLM#2 统计: {json.dumps(llm2_stats, ensure_ascii=False)}"
                )

            # —— 执行层：meeting 单元走 execute_ops；leave/budget 安全空 ——
            executor = MeetingroomExecutor(
                self.env,
                registry,
                self.static_context,
                logger=self.logger.child("执行层"),
            )
            executor_log = self.logger.child("执行层")
            exec_start = time.monotonic()
            final_answer: dict[str, Any] = {}
            executed_meeting = False
            for unit in ir.ordered_units():
                if unit.unit_type != UNIT_MEETING:
                    # 请假/预算流程 SOP 下一里程碑接入；本里程碑安全空，不猜测。
                    executor_log.info(
                        f"unit={unit.unit_type}: 流程 SOP 未接入，跳过（安全空）"
                    )
                    continue
                # LLM#2 的会议计划覆盖全部 meeting 单元（0245 把「查工位+附近订房」
                # 拆成两个 meeting 单元）。只在首个 meeting 单元执行一次——否则同一
                # 计划重复执行会耗尽步数预算（StepLimitExceeded → 顶层兜底丢结果）。
                if not executed_meeting:
                    executed_meeting = True
                    result = executor.execute_ops(meeting_plan)
                    if result:
                        final_answer = result
            exec_elapsed = time.monotonic() - exec_start
            if final_answer:
                result = final_answer.get("booking_result") or final_answer
                executor_log.info(f"final_answer: {json.dumps(result, ensure_ascii=False)}")
            else:
                executor_log.info("final_answer: {}（本阶段不执行）")

            # —— 计时汇总：整体 + 识别 + 编排 + 执行 ——
            total_elapsed = time.monotonic() - run_start
            skill_timings = meeting_skill.last_timings
            self.logger.info(
                "计时汇总: "
                f"整体={total_elapsed:.2f}s "
                f"识别(LLM#1)={skill_timings.get('recognize_s', 0.0):.2f}s "
                f"编排(LLM#2)={skill_timings.get('orchestrate_s', 0.0):.2f}s "
                f"执行={exec_elapsed:.2f}s "
                f"LLM#1={llm1_stats.get('llm_total_s', 0.0):.2f}s "
                f"LLM#2={(llm2_stats or {}).get('llm_total_s', 0.0):.2f}s"
            )
            return final_answer
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底：永不 raise
            self.logger.warning(f"run 异常，兜底返回 {{}}: {exc!r}")
            return {}
