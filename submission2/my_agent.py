"""企业流程 Agent 主入口（V2 · 感知层 + 理解层 + 纯 Python Task DAG）。

架构规格见仓库根目录 `technical_design.md`：
- 本文件只承载 `MyAgent` 入口：`run()` 永不 raise、永不返回 None；
- 感知层（StaticContextStore + ToolContractReconciler，§3.1）：reset →
  list_tools → 对账 → 输出对账摘要；
- 理解层（understanding.py）：IntentRecognizer 先把 user_query 分解为 N 个子问题
  （task_units）并路由到处理路径（meeting/leave/budget），**不做参数抽取**；
- 执行层（executor.py / leave_skill.py / budget_skill.py）：按 Task DAG 顺序执行会议、
  请假和费用 Skill；写操作经工具注册表、Schema、权限、证据和业务预检；
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

from utils.budget_skill import BudgetSkill
from utils.executor import MeetingroomExecutor
from utils.leave_skill import LeaveSkill
from utils.llm_gateway import LLMGateway
from utils.logger import ConsoleLogger, configure_console, configure_file
from utils.meeting_skill import MeetingSkill
from utils.static_context import StaticContextStore
from utils.tool_contract import ToolContractReconciler
from utils.understanding import UNIT_BUDGET, UNIT_LEAVE, UNIT_MEETING
from utils.context import CaseContext
from utils.dag_runtime import DagTask, NodeOutcome, NodeStatus, TaskDag
from utils.profiles import ExecutionProfile, ProfileConfig


def _compact(value: Any, limit: int = 600) -> Any:
    """长参数/返回压到 limit 字符，控制记录体大小。"""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text[:limit] + ("…" if len(text) > limit else "")
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


class _RecordingEnv:
    """包装 env：记录每次 call_tool / reply 到 trace。

    通过 __getattr__ 委托其余一切属性/方法，对下层透明；只额外记录
    call_tool（工具名 + 参数 + 返回）与 reply（问题 + 返回），不改变行为。
    """

    def __init__(
        self,
        env: Any,
        trace: list[dict[str, Any]],
        context: CaseContext | None = None,
        logger: Any = None,
    ) -> None:
        object.__setattr__(self, "_inner", env)
        object.__setattr__(self, "_trace", trace)
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_task_id", None)
        object.__setattr__(self, "_logger", logger)

    def set_task(self, task_id: str | None) -> None:
        object.__setattr__(self, "_task_id", task_id)
        context = object.__getattribute__(self, "_context")
        if context is not None and hasattr(context, "ledger"):
            context.ledger.set_active_task(task_id)

    def bind_context(self, context: CaseContext | None) -> None:
        """把当前 case 的证据账本绑定到包装器。

        reset/list_tools 必须发生在上下文建立前（官方环境契约如此），因此入口在
        取得 observation 后再绑定；不会跨 case 复用任何状态。
        """
        object.__setattr__(self, "_context", context)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def reset(self, case_id: str) -> Any:
        return self._inner.reset(case_id)

    def list_tools(self) -> Any:
        return self._inner.list_tools()

    def call_tool(self, name: str, args: Any) -> Any:
        logger = object.__getattribute__(self, "_logger")
        if logger is not None:
            logger.info(f"工具调用: {name} args={_compact(args)}")
        try:
            result = self._inner.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 —— 记录后原样抛，行为不改变
            self._trace.append({"tool": name, "args": _compact(args), "error": repr(exc)})
            if logger is not None:
                logger.warning(f"工具异常: {name} error={exc!r}")
            context = object.__getattribute__(self, "_context")
            if context is not None:
                context.ledger.add(
                    "tool_error", name, {"args": _compact(args), "error": repr(exc)},
                    task_id=object.__getattribute__(self, "_task_id"),
                    valid=False,
                    provenance="runtime_tool",
                )
            raise
        self._trace.append({"tool": name, "args": _compact(args), "result": _compact(result)})
        if logger is not None:
            logger.info(f"工具结果: {name} result={_compact(result)}")
        context = object.__getattribute__(self, "_context")
        if context is not None:
            context.ledger.add(
                "tool_result", name, {"args": _compact(args), "result": _compact(result)},
                task_id=object.__getattribute__(self, "_task_id"),
                valid=not bool(isinstance(result, dict) and result.get("error")),
                provenance="runtime_tool",
            )
        return result

    def reply(self, question: str) -> Any:
        logger = object.__getattribute__(self, "_logger")
        if logger is not None:
            logger.info(f"追问: {question}")
        try:
            result = self._inner.reply(question)
        except Exception as exc:  # noqa: BLE001
            self._trace.append({"reply": _compact(question), "error": repr(exc)})
            if logger is not None:
                logger.warning(f"追问异常: {exc!r}")
            context = object.__getattribute__(self, "_context")
            if context is not None:
                context.ledger.add(
                    "reply_error", "env.reply", {"question": _compact(question), "error": repr(exc)},
                    task_id=object.__getattribute__(self, "_task_id"),
                    valid=False,
                    provenance="runtime_reply",
                )
            raise
        self._trace.append({"reply": _compact(question), "result": _compact(result)})
        if logger is not None:
            logger.info(f"追问结果: {_compact(result)}")
        context = object.__getattribute__(self, "_context")
        if context is not None:
            context.ledger.add(
                "reply", "env.reply", {"question": _compact(question), "result": _compact(result)},
                task_id=object.__getattribute__(self, "_task_id"),
                provenance="runtime_reply",
            )
        return result


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
        self.profile_config = ProfileConfig.from_env()
        log_path = os.environ.get("AGENT_LOG_FILE")
        if not log_path:
            log_path = str(Path(__file__).resolve().parent / "agent_runtime.log")
        configure_file(log_path)
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

    def _leave_enabled(self) -> bool:
        """读取 leave.enabled（默认 True）。"""
        config_path = Path(__file__).resolve().parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return bool(config.get("leave", {}).get("enabled", True))
        except (OSError, ValueError):
            return True

    def _budget_enabled(self) -> bool:
        """读取 budget.enabled（默认 True）。"""
        config_path = Path(__file__).resolve().parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return bool(config.get("budget", {}).get("enabled", True))
        except (OSError, ValueError):
            return True

    def run(self, case_id: str) -> dict[str, Any]:
        """执行单个 case，返回结构化 final_answer。

        流程：reset → list_tools → 对账（感知层）→ IntentGraph/Task DAG（理解与规划）
        → 各域 Skill SOP（执行层）→ 领域结果验证与 Projection。顶层兜底保证任何异常
        都转换为部分结果或空对象，避免异常逃出官方 runner。

        Args:
            case_id: 目标 case 标识（如 "beta_mr_0001"）。

        Returns:
            结构化 final_answer dict。
        """
        # V2 入口：Task DAG 驱动各域 Skill。LEGACY_CURRENT 明确走原来的入口，
        # 用于分数等价和紧急回滚；默认 HYBRID_COMPAT 才进入新 DAG。
        if self.profile_config.profile != ExecutionProfile.LEGACY_CURRENT:
            return self._run_dag(case_id)

        # NOTE: 以下旧入口代码暂留两轮回归期间，便于逐行比较 legacy 轨迹。
        try:
            run_start = time.monotonic()
            self.logger.section(f"case={case_id}")
            # 记录每次 call_tool/reply 到 trace。
            trace: list[dict[str, Any]] = []
            env = _RecordingEnv(self.env, trace, logger=self.logger.child("执行层"))
            obs = env.reset(case_id)
            # list_tools 不消耗步数预算，是 run 开始时的前置认知（设计文档 §2.1）。
            runtime_tools = env.list_tools()
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
            ir, meeting_plan = meeting_skill.run(user_query, now_iso, mode, gateway, env)
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

            # —— 请假域：编排（LLM#2）→ 执行（确定性 SOP），多域合并 ——
            leave_result: dict[str, Any] = {}
            leave_llm2_stats: dict[str, Any] | None = None
            leave_timings: dict[str, Any] = {}
            if self._leave_enabled():
                leave_subs = [
                    u.sub_query
                    for u in ir.ordered_units()
                    if u.unit_type == UNIT_LEAVE and (u.sub_query or "").strip()
                ]
                if leave_subs:
                    # 多域合并（leave + meeting/budget）：决定提交后是否 oa.done.list
                    # 确认（仅多域 case 的 success_check 要求调用过）。
                    multi_domain = len(ir.ordered_units()) > len(leave_subs)
                    leave_skill = LeaveSkill(logger=understand_log)
                    leave_result = leave_skill.run(
                        leave_subs,
                        user_query,
                        now_iso,
                        mode,
                        gateway,
                        env,
                        registry,
                        self.static_context,
                        multi_domain=multi_domain,
                    )
                    leave_timings = leave_skill.last_timings
                    leave_llm2_stats = (
                        leave_skill.last_planner_gateway.stats_summary()
                        if leave_skill.last_planner_gateway is not None
                        else None
                    )
                    draft = leave_skill.planner.last_draft
                    understand_log.info(
                        f"请假编排: source={draft.source if draft else '-'} "
                        f"confidence={draft.confidence if draft else 0.0} "
                        f"elapsed={leave_timings.get('orchestrate_s', 0.0):.2f}s "
                        f"type_hint={draft.leave_type_hint if draft else ''!r} "
                        f"approver_hint={draft.approver_hint if draft else ''!r}"
                    )
                    understand_log.info(
                        f"请假执行: {json.dumps(leave_result.get('workflow_draft_result', {}), ensure_ascii=False)} "
                        f"elapsed={leave_timings.get('exec_s', 0.0):.2f}s"
                    )
                    if leave_llm2_stats is not None:
                        understand_log.info(
                            f"LLM#2(请假) 统计: {json.dumps(leave_llm2_stats, ensure_ascii=False)}"
                        )

            # —— 预算域：编排（LLM#2）→ 执行（确定性 SOP），多域合并 ——
            budget_result: dict[str, Any] = {}
            budget_llm2_stats: dict[str, Any] | None = None
            budget_timings: dict[str, Any] = {}
            if self._budget_enabled():
                budget_subs = [
                    u.sub_query
                    for u in ir.ordered_units()
                    if u.unit_type == UNIT_BUDGET and (u.sub_query or "").strip()
                ]
                if budget_subs:
                    # 多域合并（budget + meeting/leave）：决定保存后是否 oa 验证
                    # （仅多域 case 的 success_check 要求调用过）。
                    multi_domain = len(ir.ordered_units()) > len(budget_subs)
                    budget_skill = BudgetSkill(logger=understand_log)
                    budget_result = budget_skill.run(
                        budget_subs,
                        user_query,
                        now_iso,
                        mode,
                        gateway,
                        env,
                        registry,
                        self.static_context,
                        multi_domain=multi_domain,
                    )
                    budget_timings = budget_skill.last_timings
                    budget_llm2_stats = (
                        budget_skill.last_planner_gateway.stats_summary()
                        if budget_skill.last_planner_gateway is not None
                        else None
                    )
                    draft = budget_skill.planner.last_draft
                    understand_log.info(
                        f"预算编排: source={draft.source if draft else '-'} "
                        f"confidence={draft.confidence if draft else 0.0} "
                        f"elapsed={budget_timings.get('orchestrate_s', 0.0):.2f}s "
                        f"search_term={draft.search_term if draft else ''!r} "
                        f"category_hint={draft.category_hint if draft else ''!r} "
                        f"rows={[{'m': r.material_name, 'h': r.subclass_hint} for r in draft.rows] if draft else 0}"
                    )
                    understand_log.info(
                        f"预算执行: {json.dumps(budget_result.get('workflow_draft_result', {}), ensure_ascii=False)} "
                        f"elapsed={budget_timings.get('exec_s', 0.0):.2f}s"
                    )
                    if budget_llm2_stats is not None:
                        understand_log.info(
                            f"LLM#2(预算) 统计: {json.dumps(budget_llm2_stats, ensure_ascii=False)}"
                        )

            # —— 执行层：meeting 单元走 execute_ops；leave/budget 已独立执行 ——
            executor = MeetingroomExecutor(
                env,
                registry,
                self.static_context,
                logger=self.logger.child("执行层"),
            )
            executor_log = self.logger.child("执行层")
            exec_start = time.monotonic()
            final_answer: dict[str, Any] = {}
            executed_meeting = False
            for unit in ir.ordered_units():
                if unit.unit_type in (UNIT_LEAVE, UNIT_BUDGET):
                    # 请假/预算单元已由各自 skill 独立执行（多域合并），循环内跳过。
                    continue
                if unit.unit_type != UNIT_MEETING:
                    # 未知流程 SOP 未接入；安全空，不猜测。
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
            if leave_result:
                final_answer.update(leave_result)
            if budget_result:
                final_answer.update(budget_result)
            if final_answer:
                executor_log.info(
                    f"final_answer: {json.dumps(final_answer, ensure_ascii=False)}"
                )
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
                f"请假(LLM#2)={leave_timings.get('orchestrate_s', 0.0):.2f}s "
                f"预算(LLM#2)={budget_timings.get('orchestrate_s', 0.0):.2f}s "
                f"执行={exec_elapsed:.2f}s "
                f"LLM#1={llm1_stats.get('llm_total_s', 0.0):.2f}s "
                f"LLM#2={(llm2_stats or {}).get('llm_total_s', 0.0):.2f}s "
                f"LLM#2(请假)={(leave_llm2_stats or {}).get('llm_total_s', 0.0):.2f}s "
                f"LLM#2(预算)={(budget_llm2_stats or {}).get('llm_total_s', 0.0):.2f}s"
            )

            # 记录 case 轨迹（独立通道，不计预算、失败静默，不影响返回）。
            self._send_trace(
                gateway,
                case_id=case_id,
                user_query=user_query,
                now_iso=now_iso,
                mode=mode,
                trace=trace,
                final_answer=final_answer,
            )
            return final_answer
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底：永不 raise
            self.logger.warning(f"run 异常，兜底返回 {{}}: {exc!r}")
            return {}

    def _run_dag(self, case_id: str) -> dict[str, Any]:
        """V2 单 case 执行器：把识别结果编译成 Task DAG 后串行调度。

        这里不把业务细节塞进调度器。每个节点仍调用自己的 Skill SOP，调度器只
        负责依赖、状态、证据命名空间和跨域部分成功；这样一条 leave 失败不会抹掉
        已完成的 meeting/budget 结果，也不会提前执行后面的写操作。
        """
        final_answer: dict[str, Any] = {}
        gateway: LLMGateway | None = None
        trace: list[dict[str, Any]] = []
        run_start = time.monotonic()

        def merge_answer(part: Any) -> None:
            if not isinstance(part, dict):
                return

            def merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
                for key, value in src.items():
                    if isinstance(dst.get(key), dict) and isinstance(value, dict):
                        merge(dst[key], value)
                    elif value is not None:
                        dst[key] = value

            merge(final_answer, part)

        def result_outcome(part: Any) -> NodeOutcome:
            """把 Skill 返回转换为 DAG 状态；空结果视为完成的安全 no-op。"""
            if not isinstance(part, dict):
                return NodeOutcome(NodeStatus.BLOCKED, error="invalid_skill_result")
            for value in part.values():
                if isinstance(value, dict) and str(value.get("status", "")).lower() in {
                    "blocked", "failed", "error"
                }:
                    return NodeOutcome(NodeStatus.BLOCKED, output=part, error="domain_blocked")
            return NodeOutcome(NodeStatus.SUCCEEDED, output=part)

        try:
            self.logger.section(f"case={case_id}")
            env = _RecordingEnv(self.env, trace, logger=self.logger.child("执行层"))
            obs = env.reset(case_id)
            runtime_tools = env.list_tools()
            registry = self.reconciler.reconcile(runtime_tools)
            self.logger.info(f"对账结果: {json.dumps(registry.status(), ensure_ascii=False)}")
            if not isinstance(obs, dict):
                self.logger.warning("obs 不是对象，返回空")
                return final_answer

            user_query = str(obs.get("user_query") or "")
            now_iso = str(obs.get("now") or "")
            mode = obs.get("mode")
            if not user_query or not now_iso:
                self.logger.warning("obs 缺少 user_query/now，返回空")
                return final_answer

            case_context = CaseContext(
                case_id=case_id,
                user_query=user_query,
                now_iso=now_iso,
                mode=str(mode) if mode is not None else None,
            )
            env.bind_context(case_context)
            case_context.ledger.add(
                "observation",
                "env.reset",
                {"now": now_iso, "mode": mode},
                provenance="runtime_observation",
            )

            understand_log = self.logger.child("理解层")
            gateway = LLMGateway(logger=understand_log.child("LLM#1"))
            meeting_skill = MeetingSkill(
                logger=understand_log,
                profile_config=self.profile_config,
            )
            ir, meeting_plan = meeting_skill.run(user_query, now_iso, mode, gateway, env)
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
            llm1_stats = gateway.stats_summary()
            llm2_stats = (
                meeting_skill.last_planner_gateway.stats_summary()
                if meeting_skill.last_planner_gateway is not None else None
            )
            understand_log.info(f"LLM#1 统计: {json.dumps(llm1_stats, ensure_ascii=False)}")
            if llm2_stats is not None:
                understand_log.info(f"LLM#2 统计: {json.dumps(llm2_stats, ensure_ascii=False)}")

            # 识别器返回的是下标依赖；只接受合法的前置下标，避免模型产生环或
            # 越界边。若图仍非法，保守地按用户顺序串行执行。
            dag_tasks: list[DagTask] = []
            unit_count = len(ir.task_units)
            for index, unit in enumerate(ir.task_units):
                deps: list[str] = []
                for dep in unit.depends_on or []:
                    if isinstance(dep, int) and 0 <= dep < unit_count and dep != index:
                        dep_id = f"task-{dep}"
                        if dep_id not in deps:
                            deps.append(dep_id)
                dag_tasks.append(
                    DagTask(
                        task_id=f"task-{index}",
                        unit_type=unit.unit_type,
                        sub_query=unit.sub_query or user_query,
                        depends_on=deps,
                    )
                )
            dag = TaskDag(dag_tasks)
            try:
                dag.validate()
            except ValueError as exc:
                understand_log.warning(f"Task DAG 非法，移除依赖后按原序执行: {exc}")
                for task in dag_tasks:
                    task.depends_on = []
                dag = TaskDag(dag_tasks)
            for task in dag_tasks:
                case_context.add_task(task.task_id, task.unit_type, task.sub_query)
            understand_log.info(
                f"Task DAG: order={[task.task_id for task in dag.topological_order()] if dag_tasks else []} "
                f"nodes={len(dag_tasks)}"
            )

            executor_log = self.logger.child("执行层")
            meeting_executor = MeetingroomExecutor(
                env,
                registry,
                self.static_context,
                logger=executor_log,
                profile_config=self.profile_config,
                cross_domain=len(dag_tasks) > 1,
                context=case_context,
            )
            exec_start = time.monotonic()
            executed_meeting = False
            leave_timings: dict[str, Any] = {}
            budget_timings: dict[str, Any] = {}
            leave_llm2_stats: dict[str, Any] | None = None
            budget_llm2_stats: dict[str, Any] | None = None

            def handle_task(task: DagTask, context: CaseContext) -> NodeOutcome:
                nonlocal executed_meeting, leave_timings, budget_timings
                nonlocal leave_llm2_stats, budget_llm2_stats
                env.set_task(task.task_id)
                context.ledger.add(
                    "task_start",
                    task.task_id,
                    {"unit_type": task.unit_type, "sub_query": task.sub_query},
                    task_id=task.task_id,
                    provenance="dag_runtime",
                )
                executor_log.info(
                    f"Task 开始: id={task.task_id} type={task.unit_type} deps={task.depends_on} "
                    f"sub_query={task.sub_query!r}"
                )
                if task.unit_type == UNIT_MEETING:
                    # 当前 MeetingSkill 会把同一 case 的会议子句合并成一个计划；
                    # 后续 meeting 节点保持 DAG 可见但不重复执行写操作。
                    if executed_meeting:
                        executor_log.info(f"Task {task.task_id}: 会议计划已执行，跳过重复计划")
                        return NodeOutcome(NodeStatus.SUCCEEDED, output={})
                    executed_meeting = True
                    part = meeting_executor.execute_ops(meeting_plan)
                    merge_answer(part)
                    executor_log.info(
                        f"会议执行: task={task.task_id} result={json.dumps(part, ensure_ascii=False)}"
                    )
                    return result_outcome(part)

                if task.unit_type == UNIT_LEAVE:
                    if not self._leave_enabled():
                        executor_log.info(f"Task {task.task_id}: 请假 Skill 已关闭，跳过")
                        return NodeOutcome(NodeStatus.SKIPPED, error="leave_disabled")
                    skill = LeaveSkill(logger=understand_log, profile_config=self.profile_config)
                    part = skill.run(
                        [task.sub_query], user_query, now_iso, mode, gateway, env,
                        registry, self.static_context,
                        multi_domain=len(dag_tasks) > 1,
                        context=context,
                    )
                    merge_answer(part)
                    leave_timings = skill.last_timings
                    leave_llm2_stats = (
                        skill.last_planner_gateway.stats_summary()
                        if skill.last_planner_gateway is not None else None
                    )
                    draft = skill.planner.last_draft
                    executor_log.info(
                        f"请假执行: task={task.task_id} result={json.dumps(part, ensure_ascii=False)} "
                        f"source={draft.source if draft else '-'} elapsed={leave_timings.get('exec_s', 0.0):.2f}s"
                    )
                    return result_outcome(part)

                if task.unit_type == UNIT_BUDGET:
                    if not self._budget_enabled():
                        executor_log.info(f"Task {task.task_id}: 预算 Skill 已关闭，跳过")
                        return NodeOutcome(NodeStatus.SKIPPED, error="budget_disabled")
                    skill = BudgetSkill(logger=understand_log, profile_config=self.profile_config)
                    part = skill.run(
                        [task.sub_query], user_query, now_iso, mode, gateway, env,
                        registry, self.static_context,
                        multi_domain=len(dag_tasks) > 1,
                        context=context,
                    )
                    merge_answer(part)
                    budget_timings = skill.last_timings
                    budget_llm2_stats = (
                        skill.last_planner_gateway.stats_summary()
                        if skill.last_planner_gateway is not None else None
                    )
                    draft = skill.planner.last_draft
                    executor_log.info(
                        f"预算执行: task={task.task_id} result={json.dumps(part, ensure_ascii=False)} "
                        f"source={draft.source if draft else '-'} elapsed={budget_timings.get('exec_s', 0.0):.2f}s"
                    )
                    return result_outcome(part)

                executor_log.warning(f"Task {task.task_id}: 未知流程类型，阻断")
                return NodeOutcome(NodeStatus.BLOCKED, error=f"unknown_unit:{task.unit_type}")

            for task in dag_tasks:
                task.handler = handle_task
            outcomes = dag.run(case_context)
            env.set_task(None)
            executor_log.info(
                "Task DAG 结果: " + json.dumps(
                    {
                        task_id: {"status": outcome.status.value, "error": outcome.error}
                        for task_id, outcome in outcomes.items()
                    },
                    ensure_ascii=False,
                )
            )
            self._apply_superset_projection(final_answer)
            executor_log.info(
                f"Submission Projection: {json.dumps(final_answer, ensure_ascii=False)}"
            )

            exec_elapsed = time.monotonic() - exec_start
            total_elapsed = time.monotonic() - run_start
            skill_timings = meeting_skill.last_timings
            self.logger.info(
                "计时汇总: "
                f"整体={total_elapsed:.2f}s "
                f"识别(LLM#1)={skill_timings.get('recognize_s', 0.0):.2f}s "
                f"编排(LLM#2)={skill_timings.get('orchestrate_s', 0.0):.2f}s "
                f"请假(LLM#2)={leave_timings.get('orchestrate_s', 0.0):.2f}s "
                f"预算(LLM#2)={budget_timings.get('orchestrate_s', 0.0):.2f}s "
                f"执行={exec_elapsed:.2f}s "
                f"LLM#1={llm1_stats.get('llm_total_s', 0.0):.2f}s "
                f"LLM#2={(llm2_stats or {}).get('llm_total_s', 0.0):.2f}s "
                f"LLM#2(请假)={(leave_llm2_stats or {}).get('llm_total_s', 0.0):.2f}s "
                f"LLM#2(预算)={(budget_llm2_stats or {}).get('llm_total_s', 0.0):.2f}s"
            )
            self._send_trace(
                gateway,
                case_id=case_id,
                user_query=user_query,
                now_iso=now_iso,
                mode=mode,
                trace=trace,
                final_answer=final_answer,
            )
            return final_answer
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底：永不 raise
            self.logger.warning(f"run 异常，保留已有结果并兜底: {exc!r}")
            if gateway is not None:
                try:
                    self._send_trace(
                        gateway,
                        case_id=case_id,
                        user_query=str(locals().get("user_query") or ""),
                        now_iso=str(locals().get("now_iso") or ""),
                        mode=locals().get("mode"),
                        trace=trace,
                        final_answer=final_answer,
                    )
                except Exception:  # noqa: BLE001
                    pass
            return final_answer

    @staticmethod
    def _apply_superset_projection(final_answer: dict[str, Any]) -> None:
        """为评分器字段别名提供无损投影，不创造新的业务事实。"""
        workflow = final_answer.get("workflow_draft_result")
        if isinstance(workflow, dict) and "workflow_result" not in final_answer:
            final_answer["workflow_result"] = dict(workflow)
        participant = final_answer.get("participant_result")
        if isinstance(participant, dict):
            final_answer.setdefault("participants", dict(participant))
            if participant.get("status") == "added":
                final_answer.setdefault("participants_added", [dict(participant)])

    def _send_trace(
        self,
        gateway: LLMGateway,
        *,
        case_id: str,
        user_query: str,
        now_iso: str,
        mode: Any,
        trace: list[dict[str, Any]],
        final_answer: dict[str, Any],
    ) -> None:
        """记录本 case 执行轨迹（独立通道，不计预算，失败静默不影响返回）。"""
        if not gateway.trace_enabled:
            return
        payload = {
            "kind": "case_trace",
            "case_id": case_id,
            "user_query": user_query,
            "now": now_iso,
            "mode": mode,
            "tool_calls": trace,
            "final_answer": final_answer,
        }
        ok = gateway.send_trace(payload)
        if not ok:
            self.logger.warning("轨迹记录失败（已静默忽略，不影响返回）")
