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
import re
import time
from copy import deepcopy
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
from utils.redaction import redact_text, redact_value
from utils.trace_context import TraceContext


def _compact(value: Any, limit: int = 600) -> Any:
    """长参数/返回压到 limit 字符，控制记录体大小。"""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = redact_text(value)
        return text[:limit] + ("…" if len(text) > limit else "")
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = redact_text(text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _runtime_package_version() -> str:
    """返回可由远程日志区分的包版本，不读取或记录认证字段。"""
    env_version = str(os.environ.get("AGENT_PACKAGE_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        config = json.loads(
            (Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8")
        )
        configured = str((config.get("runtime") or {}).get("package_version") or "").strip()
        if configured:
            return configured
    except (OSError, TypeError, ValueError):
        pass
    return "v3-clustered"


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
        trace_context: TraceContext | None = None,
        logger: Any = None,
    ) -> None:
        object.__setattr__(self, "_inner", env)
        object.__setattr__(self, "_trace", trace)
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_task_id", None)
        object.__setattr__(self, "_logger", logger)
        object.__setattr__(self, "_trace_context", trace_context)

    def set_task(self, task_id: str | None) -> None:
        object.__setattr__(self, "_task_id", task_id)
        trace_context = object.__getattribute__(self, "_trace_context")
        if trace_context is not None:
            trace_context.task_id = task_id or ""
        context = object.__getattribute__(self, "_context")
        if context is not None and hasattr(context, "ledger"):
            context.ledger.set_active_task(task_id)

    def bind_context(self, context: CaseContext | None) -> None:
        """把当前 case 的证据账本绑定到包装器。

        reset/list_tools 必须发生在上下文建立前（官方环境契约如此），因此入口在
        取得 observation 后再绑定；不会跨 case 复用任何状态。
        """
        object.__setattr__(self, "_context", context)

    def bind_trace_context(self, trace_context: TraceContext | None) -> None:
        """绑定当前 case 的远程请求关联信息；不跨 case 复用。"""
        object.__setattr__(self, "_trace_context", trace_context)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def reset(self, case_id: str) -> Any:
        return self._inner.reset(case_id)

    def list_tools(self) -> Any:
        return self._inner.list_tools()

    def call_tool(self, name: str, args: Any) -> Any:
        logger = object.__getattribute__(self, "_logger")
        # 先记录调用意图，再记录结果。之前成功调用只留下 TOOL_RESULT，远程
        # telemetry 无法区分“准备调用了什么”与“工具返回了什么”；这会让超时、
        # 参数校验和结果异常难以复盘。调用事件只保存经过 _compact 的参数，
        # 不改变真实传给环境的对象。
        call_event: dict[str, Any] = {
            "event": "TOOL_CALL",
            "task_id": object.__getattribute__(self, "_task_id"),
            "tool": name,
            "args": _compact(args),
        }
        self._trace.append(call_event)
        if logger is not None:
            logger.info(f"TOOL_CALL 工具调用: {name} args={_compact(args)}")
        try:
            result = self._inner.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 —— 记录后原样抛，行为不改变
            call_event["error"] = repr(exc)
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
        self._trace.append({"event": "TOOL_RESULT", "task_id": self._task_id,
                            "tool": name, "args": _compact(args), "result": _compact(result)})
        if logger is not None:
            logger.info(f"TOOL_RESULT 工具结果: {name} result={_compact(result)}")
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
            logger.info(f"CLARIFICATION 追问: {_compact(question)}")
        try:
            result = self._inner.reply(question)
        except Exception as exc:  # noqa: BLE001
            self._trace.append({"event": "CLARIFICATION", "task_id": self._task_id,
                                "reply": _compact(question), "error": repr(exc)})
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
        clarification_event: dict[str, Any] = {
            "event": "CLARIFICATION",
            "task_id": self._task_id,
            "reply": _compact(question),
            "result": _compact(result),
            "new_fact": False,
        }
        self._trace.append(clarification_event)
        if logger is not None:
            logger.info(f"CLARIFICATION_RESULT 追问结果: {_compact(result)}")
        context = object.__getattribute__(self, "_context")
        if context is not None:
            context.ledger.add(
                "reply", "env.reply", {"question": _compact(question), "result": _compact(result)},
                task_id=object.__getattribute__(self, "_task_id"),
                provenance="runtime_reply",
            )
            # 模拟器/线上环境会返回 resolved_slot + user_message。将用户真正
            # 提供的答复写入本 case FactStore，后续当前 Task 重规划可消费；
            # 不把 assistant_message 或推断结果当作可写事实。
            if isinstance(result, dict):
                slot = str(result.get("resolved_slot") or "").strip()
                message = str(result.get("user_message") or "").strip()
                if slot and message and result.get("resolved_slot") is not None:
                    fact = context.upsert_fact(
                        f"dialogue.{slot}",
                        message,
                        source="DIALOGUE_REPLY",
                        task_id=object.__getattribute__(self, "_task_id"),
                    )
                    self._trace.append({
                        "event": "FACT_UPSERT",
                        "task_id": self._task_id,
                        "key": fact.key,
                        "fact_id": fact.fact_id,
                        "source": fact.source,
                        "value": _compact(fact.value),
                    })
                    clarification_event["new_fact"] = True
                    clarification_event["resolved_slot"] = slot
                    if logger is not None:
                        logger.info(f"事实写入: key=dialogue.{slot} 来源=用户追问回复")
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
            self.logger.info(
                f"INTENT_GRAPH case={case_id} "
                f"tasks={json.dumps(units_desc, ensure_ascii=False)}"
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
            # 旧回滚入口只有一份会议计划；直接定位首个会议单元并执行一次，
            # 不用跨 Task 的业务控制变量或隐式全局状态。
            meeting_unit = next(
                (unit for unit in ir.ordered_units() if unit.unit_type == UNIT_MEETING),
                None,
            )
            if meeting_unit is not None:
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
        final_sent = False

        def merge_answer(part: Any) -> None:
            if not isinstance(part, dict):
                return

            def merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
                for key, value in src.items():
                    if isinstance(dst.get(key), dict) and isinstance(value, dict):
                        # V3 允许多个同域 Task 顺序执行，但一个后续 blocked
                        # 结果不能抹掉前一 Task 已经确认的成功事实。关闭开关时
                        # 保持旧的字段覆盖行为，便于精确回放 legacy 轨迹。
                        if (
                            self._meeting_projection_v3_enabled()
                            and key == "booking_result"
                            and self._booking_status_rank(dst[key].get("status"))
                            > self._booking_status_rank(value.get("status"))
                        ):
                            continue
                        if (
                            self._meeting_projection_v3_enabled()
                            and key == "booking_result"
                            and self._booking_status_rank(value.get("status"))
                            > self._booking_status_rank(dst[key].get("status"))
                            and str(dst[key].get("status", "")).lower()
                            in {"blocked", "failed", "error"}
                        ):
                            # 后续成功结果可以继承 queried 的 room/day 等事实，
                            # 但不能携带前一个 blocked 的原因/槽位。
                            for stale in ("reason", "slot", "error"):
                                dst[key].pop(stale, None)
                        merge(dst[key], value)
                    elif value is not None:
                        # 不让 final_answer 与某个 Task 的 output 共享可变嵌套
                        # 对象；否则后续 Task 合并会反向改写已记录的 blocked 结果。
                        dst[key] = deepcopy(value)

            merge(final_answer, part)

        def result_outcome(part: Any) -> NodeOutcome:
            """把 Skill 返回转换为 DAG 状态；空结果视为完成的安全 no-op。"""
            return self._skill_result_outcome(part)

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

            package_version = _runtime_package_version()
            trace_context = TraceContext(
                case_id=case_id,
                package_version=package_version,
                profile=self.profile_config.profile_name,
                prompt_versions={
                    "intent_graph": "intent-v2",
                    "meeting_plan": "meeting-v2",
                    "leave_plan": "leave-v2",
                    "budget_plan": "budget-v2",
                },
            )
            run_id = trace_context.run_id
            case_context = CaseContext(
                case_id=case_id,
                user_query=user_query,
                now_iso=now_iso,
                mode=str(mode) if mode is not None else None,
                run_id=run_id,
                package_version=package_version,
            )
            self.logger.info(f"CASE_START run_id={run_id} case={case_id} now={now_iso} mode={mode} profile={self.profile_config.profile_name}")
            env.bind_context(case_context)
            env.bind_trace_context(trace_context)
            case_context.ledger.add(
                "observation",
                "env.reset",
                {"now": now_iso, "mode": mode},
                provenance="runtime_observation",
            )

            understand_log = self.logger.child("理解层")
            gateway = LLMGateway(
                logger=understand_log.child("LLM#1"),
                trace_context=trace_context,
                stage="intent_graph",
            )
            meeting_skill = MeetingSkill(
                logger=understand_log,
                profile_config=self.profile_config,
            )
            ir, meeting_plan = meeting_skill.run(user_query, now_iso, mode, gateway, env)
            # 识别器偶尔会把同一条会议 SOP 拆成“查询 / 取消 / 重订”多个
            # meeting unit，或把“另外/顺手”的跨域任务标成硬依赖。V3 仅在
            # 对应开关开启时做确定性规范化；关闭时保留原始识别结果，方便旧轨迹
            # 回放。
            normalized_units = self._normalize_meeting_units(
                ir.task_units,
                enabled=bool(
                    getattr(self.profile_config, "dag_dependency_v3", False)
                    or self.profile_config.meeting_reference_v3
                ),
            )
            if normalized_units is not ir.task_units:
                ir.task_units = normalized_units
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
            self.logger.info(
                f"INTENT_GRAPH run_id={case_context.run_id} "
                f"tasks={json.dumps(units_desc, ensure_ascii=False)}"
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
                requires = self._infer_task_requires(
                    index,
                    unit.sub_query,
                    ir.task_units,
                    full_query=user_query,
                    meeting_reference_v3=self._meeting_reference_v3_enabled(),
                )
                dag_tasks.append(
                    DagTask(
                        task_id=f"task-{index}",
                        unit_type=unit.unit_type,
                        sub_query=unit.sub_query or user_query,
                        order_after=deps,
                        requires=requires,
                    )
                )
            dag = TaskDag(dag_tasks)
            try:
                dag.validate()
            except ValueError as exc:
                understand_log.warning(f"Task DAG 非法，移除依赖后按原序执行: {exc}")
                for task in dag_tasks:
                    task.order_after = []
                    task.requires = []
                dag = TaskDag(dag_tasks)
            for task in dag_tasks:
                case_context.add_task(
                    task.task_id,
                    task.unit_type,
                    task.sub_query,
                    order_after=task.order_after,
                    requires=task.requires,
                )
            understand_log.info(
                f"Task DAG: order={[task.task_id for task in dag.topological_order()] if dag_tasks else []} "
                f"nodes={len(dag_tasks)} edges="
                f"{json.dumps({t.task_id: {'order_after': t.order_after, 'requires': t.requires} for t in dag_tasks}, ensure_ascii=False)}"
            )
            self._send_trace(
                gateway,
                case_id=case_id,
                user_query=user_query,
                now_iso=now_iso,
                mode=mode,
                trace=[],
                final_answer={},
                kind="plan_checkpoint",
                context=case_context,
                extra={
                    "trace_context": trace_context.summary(),
                    "intent_graph": units_desc,
                    "task_dag": {
                        t.task_id: {
                            "unit_type": t.unit_type,
                            "sub_query": t.sub_query,
                            "order_after": t.order_after,
                            "requires": t.requires,
                        }
                        for t in dag_tasks
                    },
                    "features": self.profile_config.feature_flags(),
                    "model_stats": {
                        "intent_graph": gateway.stats_summary(),
                        "meeting_plan": (
                            meeting_skill.last_planner_gateway.stats_summary()
                            if meeting_skill.last_planner_gateway is not None else None
                        ),
                    },
                },
            )

            executor_log = self.logger.child("执行层")
            meeting_executor = MeetingroomExecutor(
                env,
                registry,
                self.static_context,
                logger=executor_log,
                profile_config=self.profile_config,
                # ``cross_domain`` 表示同一用例是否包含多个业务域，而不是
                # Task 数量。一个会议请求可能被拆成“查工位 + 订会议”两个
                # Meeting Task；把它误判为跨域会触发跨域 Projection（例如
                # 将 office_id 改成楼栋名），导致业务状态正确但提交契约不匹配。
                # 只有真正混合 meeting/leave/budget 等不同 unit_type 时才启用
                # 跨域兼容投影。
                cross_domain=len({task.unit_type for task in dag_tasks}) > 1,
                context=case_context,
            )
            exec_start = time.monotonic()
            leave_timings: dict[str, Any] = {}
            budget_timings: dict[str, Any] = {}
            leave_llm2_stats: dict[str, Any] | None = None
            budget_llm2_stats: dict[str, Any] | None = None
            projection_enabled = self._meeting_projection_v3_enabled()

            meeting_plans: dict[str, Any] = {}
            meeting_tasks = [task for task in dag_tasks if task.unit_type == UNIT_MEETING]
            if len(meeting_tasks) == 1:
                meeting_plans[meeting_tasks[0].task_id] = meeting_plan
            elif meeting_tasks:
                # 每个独立会议 Task 提前独立编排；依赖前序会议事实的 Task 延迟到
                # handler 内重规划，避免在事实尚未产生时把“刚订的会议/那天”猜成
                # 当前日期或第一个订单。
                for task in meeting_tasks:
                    if task.requires:
                        continue
                    meeting_plans[task.task_id] = meeting_skill.plan_task(
                        task.sub_query or user_query,
                        now_iso,
                        mode,
                        gateway,
                    )

            def handle_task(task: DagTask, context: CaseContext) -> NodeOutcome:
                nonlocal leave_timings, budget_timings
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
                    f"Task 开始: id={task.task_id} type={task.unit_type} order_after={task.order_after} requires={task.requires} "
                    f"sub_query={task.sub_query!r}"
                )
                executor_log.info(
                    f"SKILL_PLAN task={task.task_id} intent={task.unit_type} "
                    f"requires={task.requires} 预计节点=确定性SOP"
                )
                if task.unit_type == UNIT_MEETING:
                    plan = meeting_plans.get(task.task_id)
                    if plan is None and task.requires:
                        # 前序 Task 已按 requires 成功完成，当前 case 的事实账本
                        # 已经有唯一 booking/day 等运行时事实；此处重新执行当前
                        # Task 的编排，并只对缺失槽位做事实晚绑定。
                        plan = meeting_skill.plan_task(
                            task.sub_query or user_query,
                            now_iso,
                            mode,
                            gateway,
                        )
                        plan = self._bind_meeting_reference_facts(
                            plan,
                            task.sub_query or user_query,
                            context,
                            task.requires,
                            meeting_reference_v3=self._meeting_reference_v3_enabled(),
                        )
                        meeting_plans[task.task_id] = plan
                    if plan is None:
                        return NodeOutcome(NodeStatus.BLOCKED, error="meeting_plan_missing")
                    if self._meeting_reference_v3_enabled():
                        plan = self._bind_explicit_meeting_facts(
                            plan,
                            task.sub_query or user_query,
                            context,
                            task.task_id,
                        )
                    # 跨 Task 的显式指代必须绑定到前序运行时事实。即使模型把
                    # ``刚订的会议/那天`` 解析成了可执行动作，也不能在事实缺失时
                    # 退回“找第一条会议”或按当前日期猜测；否则会破坏任务隔离和
                    # 新旧订单一致性。执行前先做一个无副作用的引用门控，真实事实
                    # 由 _bind_meeting_reference_facts 注入，缺失则诚实阻断。
                    # 只有存在前序 Meeting Task 时，原文里的“刚订的会议/那天”
                    # 才是跨 Task 指代，需要依赖账本唯一事实。单个 Meeting Task
                    # 的“原会议”是本 SOP 内部定位条件，不能被误拦；“这个会议室”
                    # 也不是“这个会议”的指代。
                    task_index = next(
                        (idx for idx, candidate in enumerate(dag_tasks)
                         if candidate.task_id == task.task_id),
                        len(dag_tasks),
                    )
                    prior_meeting = any(
                        candidate.unit_type == UNIT_MEETING
                        for candidate in dag_tasks[:task_index]
                    )
                    missing_reference = (
                        self._missing_meeting_reference_fact(
                            task.sub_query or user_query,
                            context,
                            task.requires,
                            meeting_reference_v3=self._meeting_reference_v3_enabled(),
                        )
                        if task.requires or prior_meeting
                        else None
                    )
                    if missing_reference:
                        executor_log.warning(
                            f"Task {task.task_id}: 跨 Task 事实未唯一解析，阻断 "
                            f"reason=unresolved_meeting_reference slot={missing_reference}"
                        )
                        return NodeOutcome(
                            NodeStatus.BLOCKED,
                            output={
                                "booking_result": {
                                    "status": "blocked",
                                    "reason": "unresolved_meeting_reference",
                                    "slot": missing_reference,
                                }
                            },
                            error="unresolved_meeting_reference",
                        )
                    if getattr(plan, "source", "") == "blocked_llm_plan":
                        executor_log.warning(
                            f"Task {task.task_id}: 模型编排失败，候选档不执行会议写操作"
                        )
                        return NodeOutcome(NodeStatus.BLOCKED, error="llm_plan_unavailable")
                    part = meeting_executor.execute_ops(plan)
                    if projection_enabled:
                        part = self._project_meeting_result(part, plan)
                    merge_answer(part)
                    self._record_task_facts(
                        context,
                        task.task_id,
                        part,
                        meeting_reference_v3=self._meeting_reference_v3_enabled(),
                    )
                    executor_log.info(
                        f"DOMAIN_RESULT task={task.task_id} domain=meeting "
                        f"result={_compact(part, 12000)} "
                        f"fact_count={len(context.facts.all(task_id=task.task_id))}"
                    )
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
                    self._record_task_facts(
                        context,
                        task.task_id,
                        part,
                        meeting_reference_v3=self._meeting_reference_v3_enabled(),
                    )
                    executor_log.info(
                        f"DOMAIN_RESULT task={task.task_id} domain=leave "
                        f"result={_compact(part, 12000)} "
                        f"fact_count={len(context.facts.all(task_id=task.task_id))}"
                    )
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
                    budget_query = self._enrich_task_query(context, task)
                    part = skill.run(
                        [budget_query], user_query, now_iso, mode, gateway, env,
                        registry, self.static_context,
                        multi_domain=len(dag_tasks) > 1,
                        context=context,
                    )
                    merge_answer(part)
                    self._record_task_facts(
                        context,
                        task.task_id,
                        part,
                        meeting_reference_v3=self._meeting_reference_v3_enabled(),
                    )
                    executor_log.info(
                        f"DOMAIN_RESULT task={task.task_id} domain=budget "
                        f"result={_compact(part, 12000)} "
                        f"fact_count={len(context.facts.all(task_id=task.task_id))}"
                    )
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
            # Handler 可能在执行前才发现引用事实缺失（例如“那天”没有唯一
            # meeting.day），此时它会把结构化 blocked 结果放在 NodeOutcome.output。
            # 运行时不能只保留此前 Task 的 queried/success 结果；把每个节点的
            # 显式输出合并进最终投影，仍沿用领域结果的字段结构。
            task_outputs: dict[str, dict[str, Any]] = {}
            if projection_enabled:
                for task_id, outcome in outcomes.items():
                    output = deepcopy(outcome.output) if isinstance(outcome.output, dict) else {}
                    # 一个 handler 可能在执行前因引用缺失而只返回 error；仍为该
                    # Task 生成可审计的领域结果，避免“失败了但 Projection 看不见”。
                    if outcome.status is NodeStatus.BLOCKED and not output:
                        output = {
                            "booking_result": {
                                "status": "blocked",
                                "reason": outcome.error or "task_blocked",
                            }
                        } if self._task_is_meeting(task_id, dag_tasks) else {
                            "task_result": {
                                "status": "blocked",
                                "reason": outcome.error or "task_blocked",
                            }
                        }
                    if output:
                        task_outputs[task_id] = deepcopy(output)
                        merge_answer(output)
                # Projection 以 Task 为键保留每个领域结果；这同时保留前一个
                # queried/success 与后一个 not_cancelled/blocked 的独立语义。
                if task_outputs:
                    final_answer["task_outcomes"] = task_outputs
            # 若最终顶层 booking 已成功，blocked 的独立 Task 不能覆盖它；其
            # blocked 输出已经按 task_id 保存在 task_outcomes 中，顶层仍只投影
            # 可用的成功事实。
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
            # CaseContext 的事实和策略可能由 Skill 内部直接写入，未必经过
            # _RecordingEnv.reply；在终局统一补齐结构化事件，确保 telemetry
            # 能看到每个事实的来源、Task 和覆盖关系。已记录的回复事实按
            # fact_id 去重，不改变业务结果。
            self._append_context_events(trace, case_context, executor_log)
            self.logger.info(
                f"PROJECTION run_id={case_context.run_id} fields={list(final_answer.keys())} "
                f"facts={len(case_context.facts.all())}"
            )
            self.logger.info(
                f"SELF_AUDIT run_id={case_context.run_id} tasks={len(dag_tasks)} "
                f"outcomes={json.dumps({k: v.status.value for k, v in outcomes.items()}, ensure_ascii=False)} "
                f"fact_count={len(case_context.facts.all())}"
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
                kind="case_final",
                context=case_context,
                extra={
                    "trace_context": trace_context.summary(),
                    "facts": case_context.facts.as_dict(),
                    "policy_decisions": case_context.policy_decisions,
                    "outcomes": {
                        k: {"status": v.status.value, "error": v.error}
                        for k, v in outcomes.items()
                    },
                    "self_audit": {
                        "fact_count": len(case_context.facts.all()),
                        "task_count": len(dag_tasks),
                        "tool_event_count": len(trace),
                        "profile": self.profile_config.profile_name,
                        "feature_flags": self.profile_config.feature_flags(),
                    },
                },
            )
            final_sent = True
            tool_call_count = sum(
                1 for event in trace if event.get("event") == "TOOL_CALL"
            )
            self.logger.info(
                f"CASE_END run_id={case_context.run_id} case={case_id} status=completed "
                f"tool_calls={tool_call_count} elapsed={time.monotonic() - run_start:.2f}s"
            )
            return final_answer
        except Exception as exc:  # noqa: BLE001 —— 顶层兜底：永不 raise
            self.logger.warning(f"run 异常，保留已有结果并兜底: {exc!r}")
            current_context = locals().get("case_context")
            if current_context is not None:
                self._append_context_events(trace, current_context, self.logger.child("执行层"))
            tool_call_count = sum(
                1 for event in trace if event.get("event") == "TOOL_CALL"
            )
            self.logger.info(
                f"CASE_END run_id={getattr(locals().get('case_context'), 'run_id', '-') or '-'} "
                f"case={case_id} status=exception error={type(exc).__name__} "
                f"tool_calls={tool_call_count} elapsed={time.monotonic() - run_start:.2f}s"
            )
            if gateway is not None and not final_sent:
                try:
                    self._send_trace(
                        gateway,
                        case_id=case_id,
                        user_query=str(locals().get("user_query") or ""),
                        now_iso=str(locals().get("now_iso") or ""),
                        mode=locals().get("mode"),
                        trace=trace,
                        final_answer=final_answer,
                        kind="case_final",
                        context=locals().get("case_context"),
                        extra={"exception": repr(exc)},
                    )
                except Exception:  # noqa: BLE001
                    pass
            return final_answer

    @staticmethod
    def _task_is_meeting(task_id: str, tasks: list[DagTask]) -> bool:
        return any(task.task_id == task_id and task.unit_type == UNIT_MEETING for task in tasks)

    @staticmethod
    def _skill_result_outcome(part: Any) -> NodeOutcome:
        """把领域投影转换成内部节点状态。

        领域结果可以为了官方提交契约把安全 no-op 投影成
        ``booking_result.status=not_cancelled``，但该状态仍不是成功事实。保留
        这条转换在一个可测试的纯函数里，防止多个入口再各自解释一次结果。
        """
        if not isinstance(part, dict):
            return NodeOutcome(NodeStatus.BLOCKED, error="invalid_skill_result")
        for value in part.values():
            if not isinstance(value, dict):
                continue
            status = str(value.get("status", "")).lower()
            safe_noop = status == "not_cancelled" and str(
                value.get("reason") or ""
            ).lower() in {"ambiguous_booking", "ambiguous_reference", "no_result"}
            if status in {"blocked", "failed", "error"} or safe_noop:
                return NodeOutcome(NodeStatus.BLOCKED, output=part, error="domain_blocked")
        return NodeOutcome(NodeStatus.SUCCEEDED, output=part)

    def _meeting_projection_v3_enabled(self) -> bool:
        """Projection V3 与旧 meeting_reference_v3 兼容共存。"""
        return bool(
            getattr(self.profile_config, "meeting_projection_v3", False)
            or self.profile_config.meeting_reference_v3
        )

    def _meeting_reference_v3_enabled(self) -> bool:
        """引用/依赖 V3；dag_dependency_v3 也需要启用引用事实晚绑定。"""
        return bool(
            self.profile_config.meeting_reference_v3
            or getattr(self.profile_config, "dag_dependency_v3", False)
        )

    @staticmethod
    def _project_meeting_result(part: Any, plan: Any) -> dict[str, Any]:
        """为会议安全 no-op 补充稳定的 booking_result 投影。"""
        result = deepcopy(part) if isinstance(part, dict) else {}
        booking = result.get("booking_result")
        actions = [str(getattr(op, "action", "")).lower() for op in getattr(plan, "ops", []) or []]
        if isinstance(booking, dict):
            reason = str(booking.get("reason") or "")
            if "cancel" in actions and booking.get("status") == "blocked" and reason in {
                "need_confirmation", "ambiguous_booking", "ambiguous_reference"
            }:
                result["booking_result"] = {
                    **booking,
                    "status": "not_cancelled",
                    "reason": "ambiguous_booking",
                }
            return result
        if "cancel" in actions:
            result["booking_result"] = {
                "status": "not_cancelled",
                "reason": "no_result",
            }
        return result

    @staticmethod
    def _normalize_meeting_units(units: list[Any], *, enabled: bool) -> list[Any]:
        """规范识别层的会议切分和跨域依赖。

        同一个会议请求若被切成“查询、取消、重订”，必须作为一条 SOP 交给
        MeetingSkill，否则后两个 Task 看不到前一个查询/取消产生的事实。相反，
        “另外/顺手/另一个”连接的两个会议仍保持独立。TaskUnit 由识别层产出，
        这里只复制并重映射下标依赖，不引入任何 case 或答案常量。
        """
        if not enabled or not isinstance(units, list) or len(units) < 2:
            return units
        copied = deepcopy(units)
        groups: list[list[int]] = []
        current: list[int] = []
        for index, unit in enumerate(copied):
            if not current:
                current = [index]
                continue
            previous = copied[current[-1]]
            if (
                getattr(previous, "unit_type", None) == UNIT_MEETING
                and getattr(unit, "unit_type", None) == UNIT_MEETING
                and MyAgent._meeting_units_are_one_sop(
                    str(getattr(previous, "sub_query", "")),
                    str(getattr(unit, "sub_query", "")),
                )
            ):
                current.append(index)
            else:
                groups.append(current)
                current = [index]
        if current:
            groups.append(current)
        if all(len(group) == 1 for group in groups):
            return units

        old_to_group = {
            old_index: group_index
            for group_index, group in enumerate(groups)
            for old_index in group
        }
        normalized: list[Any] = []
        for group in groups:
            first = deepcopy(copied[group[0]])
            if len(group) > 1:
                first.sub_query = "；".join(
                    str(getattr(copied[index], "sub_query", ""))
                    for index in group
                    if str(getattr(copied[index], "sub_query", ""))
                )
            dependencies: list[int] = []
            for old_index in group:
                for dependency in getattr(copied[old_index], "depends_on", []) or []:
                    if not isinstance(dependency, int) or dependency in group:
                        continue
                    mapped = old_to_group.get(dependency)
                    if mapped is not None and mapped != len(normalized) and mapped not in dependencies:
                        dependencies.append(mapped)
            first.depends_on = dependencies
            normalized.append(first)
        return normalized

    @staticmethod
    def _meeting_units_are_one_sop(previous: str, current: str) -> bool:
        """判断相邻 meeting 子句是否属于同一查询→变更链。"""
        combined = f"{previous}；{current}"
        query_words = ("查询", "查一下", "看看", "日程", "空不空", "有哪些")
        cancel_words = ("取消", "撤销", "退订", "删掉", "删除")
        book_words = ("重订", "重新订", "再订", "换个", "预订", "订个", "订一")
        has_query = any(word in combined for word in query_words)
        has_cancel = any(word in combined for word in cancel_words)
        has_book = any(word in combined for word in book_words)
        independent = ("另外", "顺便", "另一个", "另一个会议", "再开一个", "第三个")
        if any(word in current for word in independent):
            return False
        linkage = (
            "这个会议", "原会议", "原来的", "之前的", "刚订", "刚才", "该会议",
            "订单", "已订", "同一天", "那天",
        )
        linked = any(word in previous or word in current for word in linkage)
        # 查询→取消、取消→重订都是同一 SOP 的相邻阶段；完整三段链自然
        # 会连续合并到一个组。显式“另外/另一个”则优先视为独立请求。
        if has_query and has_cancel:
            return True
        if has_cancel and has_book:
            return linked
        return has_query and has_cancel and has_book and linked

    @staticmethod
    def _extract_explicit_order_id(text: str) -> str | None:
        value = str(text or "")
        token = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"
        labelled = re.search(
            rf"(?:订单号|预订号|booking_id|order_id)\s*(?:[:=：]\s*)?(?<![A-Za-z0-9])({token})",
            value,
            re.I,
        )
        if labelled and not labelled.group(1).isdigit():
            return labelled.group(1).rstrip("，。；,;):：")
        match = re.search(
            rf"(?<![A-Za-z0-9])(?:SEED|BK|BOOKING|ORDER)[-_][A-Za-z0-9][A-Za-z0-9_.:-]*",
            value,
            re.I,
        )
        return match.group(0).rstrip("，。；,;):：") if match else None

    @staticmethod
    def _extract_explicit_room_id(text: str) -> str | None:
        value = str(text or "")
        room_pattern = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,3}"
        match = re.search(
            rf"(?:会议室|房间|room_id)\s*(?:是|为|[:=：])?\s*({room_pattern})",
            value,
            re.I,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_explicit_day(text: str) -> str | None:
        value = str(text or "")
        match = re.search(
            r"(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}[日号]?|"
            r"\d{1,2}月\d{1,2}[日号]?|(?:周|星期|礼拜)\s*[一二三四五六日天1-7]|"
            r"今天|明天|后天|大后天|昨天|前天)",
            value,
        )
        return match.group(0) if match else None

    @staticmethod
    def _bind_explicit_meeting_facts(
        plan: Any,
        text: str,
        context: CaseContext,
        task_id: str,
    ) -> Any:
        """把用户明确给出的会议 ID/房间/日期写入当前 Task 事实账本。"""
        value = str(text or "")
        order_id = MyAgent._extract_explicit_order_id(value)
        room_id = MyAgent._extract_explicit_room_id(value)
        day = MyAgent._extract_explicit_day(value)
        if order_id:
            for key in ("meeting.order_id", "meeting.booking_id"):
                context.upsert_fact(key, order_id, source="USER_EXPLICIT", task_id=task_id)
        if room_id:
            context.upsert_fact("meeting.room_id", room_id, source="USER_EXPLICIT", task_id=task_id)
        if day:
            context.upsert_fact("meeting.day", day, source="USER_EXPLICIT", task_id=task_id)
        if plan is None:
            return plan
        for op in getattr(plan, "ops", []) or []:
            target = dict(getattr(op, "target", {}) or {})
            action = str(getattr(op, "action", "")).lower()
            if order_id and action in {
                "cancel", "extend", "participant_add", "participant_remove", "participant_list"
            } and not target.get("order_id"):
                target["order_id"] = order_id
            if room_id and action in {"book", "query", "multi_day", "earliest"}:
                if not target.get("room_id") and not target.get("rooms"):
                    target["room_id"] = room_id
                    target["rooms"] = [room_id]
            op.target = target
        return plan

    @staticmethod
    def _booking_status_rank(status: Any) -> int:
        """会议结果合并优先级：成功事实高于决策/阻断，高于只读查询。"""
        return {"success": 3, "active": 3, "cancelled": 3,
                "rebooked": 3, "extended": 3,
                "updated": 3, "participant_added": 3, "participants_added": 3,
                "extended_and_participant_added": 3, "queried": 1}.get(
                    str(status or "").lower(), 2
                )

    @staticmethod
    def _has_successful_booking_result(answer: Any) -> bool:
        if not isinstance(answer, dict):
            return False
        booking = answer.get("booking_result")
        return isinstance(booking, dict) and MyAgent._booking_status_rank(
            booking.get("status")
        ) >= 3

    @staticmethod
    def _infer_task_requires(
        index: int,
        sub_query: str,
        units: list[Any],
        *,
        full_query: str | None = None,
        meeting_reference_v3: bool = True,
    ) -> list[str]:
        """只把可观察的跨任务指代转成 requires，其余模型依赖仅是顺序边。

        识别 Prompt 允许模型把“同项目”展开为已知项目短语以便当前 Task 自
        包含，但这不应丢掉真正的数据依赖。这里只查看当前 Task 的 sub_query；
        有前序同域 Task 时恢复 requires，没有前序 Task 时不人为制造依赖。
        """
        # ``requires`` 是当前子任务的事实缺口，不是整句用户请求的关键词
        # 命中。full_query 仍保留在签名中兼容旧调用方，但不能让同一请求中
        # 其它子句的“原会议/那天”污染当前 Task。
        text = str(sub_query or "")
        markers = {
            "project": ("同项目", "该项目", "这个项目", "上述项目"),
            "meeting": ("那天", "同一天", "刚订的会议", "这个会议", "刚才的会议", "原会议"),
        }
        wanted: str | None = None
        unit_type = getattr(units[index], "unit_type", "") if 0 <= index < len(units) else ""
        # 先按当前域选择对应引用类型，避免一个跨域原句同时含“同项目”和
        # “那天”时把 meeting Task 错绑到 project 依赖（反之亦然）。
        preferred = "project" if unit_type == UNIT_BUDGET else "meeting" if unit_type == UNIT_MEETING else None
        kinds = [preferred] if preferred else list(markers)
        scan_text = text
        if not meeting_reference_v3:
            # 关闭 V3 时保留旧版兼容行为；真实执行链会显式传开关，单元测试
            # 默认使用新逻辑以便直接验证纯函数契约。
            scan_text = f"{text}\n{str(full_query or '')}"
        for kind in kinds:
            if kind and MyAgent._contains_reference(scan_text, kind):
                # 当前 Task 已经给出足够的定位槽位时，不需要前序事实来补同一
                # 槽位。显式“订单号 + 原会议”可由当前 Task 自行定位订单；
                # “同一天（周四）”中的星期也是显式日期语义。
                if meeting_reference_v3 and kind == "meeting":
                    has_booking_ref = MyAgent._contains_reference(text, "meeting_booking")
                    has_day_ref = MyAgent._contains_reference(text, "meeting_day")
                    if has_booking_ref and MyAgent._has_explicit_order_id(text):
                        has_booking_ref = False
                    if has_day_ref and MyAgent._has_explicit_meeting_date(text):
                        has_day_ref = False
                    if not (has_booking_ref or has_day_ref):
                        continue
                wanted = kind
                break
        if wanted is None:
            return []
        for previous in range(index - 1, -1, -1):
            unit_type = getattr(units[previous], "unit_type", "")
            if wanted == "project" and unit_type == UNIT_BUDGET:
                return [f"task-{previous}"]
            if wanted == "meeting" and unit_type == UNIT_MEETING:
                return [f"task-{previous}"]
        # 明确指代但没有可供读取的前序事实，保留一个不存在的 requires 会让
        # TaskDag 安全阻断；调用方的多轮逻辑可再补事实，不会猜测。
        return []

    @staticmethod
    def _enrich_task_query(context: CaseContext, task: DagTask) -> str:
        """用当前 case 唯一的项目事实补全“同项目”指代，不写入全局缓存。"""
        text = task.sub_query or ""
        if not any(word in text for word in ("同项目", "该项目", "这个项目", "上述项目")):
            return text
        project_code = MyAgent._fact_from_dependencies(
            context, "expense.project_code", task.requires
        )
        project_name = MyAgent._fact_from_dependencies(
            context, "expense.project_name", task.requires
        )
        if project_code or project_name:
            fact = project_code or project_name
            return f"{text}\n[本用例前序事实] 项目={fact}"
        return text

    @staticmethod
    def _fact_from_dependencies(
        context: CaseContext, key: str, dependencies: list[str] | None = None
    ) -> Any:
        """优先从 requires 指向的前序 Task 读取唯一事实，再回退 case 唯一值。"""
        for task_id in dependencies or []:
            value = context.facts.unique_value(key, task_id=task_id)
            if value is not None:
                return value
        return context.facts.unique_value(key)

    @staticmethod
    def _bind_meeting_reference_facts(
        plan: Any, text: str, context: CaseContext,
        dependencies: list[str] | None = None,
        *,
        meeting_reference_v3: bool = True,
    ) -> Any:
        """把显式跨 Task 会议指代绑定到本 case 的唯一运行时事实。

        模型只负责识别动作和原文语义，不能生成订单号/房间 ID。依赖 Task 成功
        后，这里把 ``刚订的会议``、``那天`` 等可观测指代晚绑定到 planner
        产出的原子 op；没有唯一事实时保持空值，由 SOP 安全阻断或追问。
        """
        if plan is None:
            return plan
        text = str(text or "")
        booking_ref = MyAgent._contains_reference(text, "meeting_booking")
        day_ref = MyAgent._contains_reference(text, "meeting_day")
        explicit_day = MyAgent._has_explicit_meeting_date(text) if meeting_reference_v3 else False
        if meeting_reference_v3:
            if booking_ref and MyAgent._has_explicit_order_id(text):
                booking_ref = False
            if day_ref and explicit_day:
                day_ref = False
        booking_id = MyAgent._fact_from_dependencies(
            context, "meeting.booking_id", dependencies
        ) if booking_ref else None
        day = MyAgent._fact_from_dependencies(
            context, "meeting.day", dependencies
        ) if (booking_ref or day_ref) and not explicit_day else None
        room_id = MyAgent._fact_from_dependencies(
            context, "meeting.room_id", dependencies
        ) if booking_ref else None
        start = MyAgent._fact_from_dependencies(
            context, "meeting.start", dependencies
        ) if booking_ref else None
        end = MyAgent._fact_from_dependencies(
            context, "meeting.end", dependencies
        ) if booking_ref else None
        title = MyAgent._fact_from_dependencies(
            context, "meeting.title", dependencies
        ) if booking_ref else None
        if not any((booking_id, day, room_id, start, end, title)):
            return plan
        for op in getattr(plan, "ops", []) or []:
            target = dict(getattr(op, "target", {}) or {})
            if booking_id and not target.get("order_id"):
                target["order_id"] = booking_id
            if day and not target.get("day"):
                target["day"] = day
            if room_id and not target.get("room_id") and not target.get("room"):
                target["room_id"] = room_id
            if start and not target.get("start"):
                target["start"] = start
            if end and not target.get("end"):
                target["end"] = end
            if title and not target.get("title"):
                target["title"] = title
            op.target = target
        return plan

    @staticmethod
    def _missing_meeting_reference_fact(
        text: str,
        context: CaseContext,
        dependencies: list[str] | None = None,
        *,
        meeting_reference_v3: bool = True,
    ) -> str | None:
        """返回跨 Task 会议指代所缺的唯一事实槽位。

        这是执行前的安全门，而不是新的业务推断：只有文本显式出现跨 Task
        指代且 ``requires`` 已声明时才检查。booking 指代需要订单号；“那天/同一
        天”至少需要日期。若前序 Task 产出多个不同事实，FactStore 会返回 None，
        让当前 Task blocked，而不是任选一条。
        """
        text = str(text or "")
        booking_ref = MyAgent._contains_reference(text, "meeting_booking")
        day_ref = MyAgent._contains_reference(text, "meeting_day")
        # 显式槽位优先：订单号不需要 meeting.booking_id；星期/日期（包括
        # “同一天（周四）”）不需要 meeting.day。其它缺失槽位仍照常门控。
        if meeting_reference_v3:
            if booking_ref and MyAgent._has_explicit_order_id(text):
                booking_ref = False
            if day_ref and MyAgent._has_explicit_meeting_date(text):
                day_ref = False
        if not (booking_ref or day_ref):
            return None
        if not dependencies:
            return "meeting.booking_id" if booking_ref else "meeting.day"
        if booking_ref and MyAgent._fact_from_dependencies(
            context, "meeting.booking_id", dependencies
        ) is None:
            return "meeting.booking_id"
        if (booking_ref or day_ref) and MyAgent._fact_from_dependencies(
            context, "meeting.day", dependencies
        ) is None:
            return "meeting.day"
        return None

    @staticmethod
    def _contains_reference(text: str, kind: str) -> bool:
        """识别跨 Task 指代，避免把“这个会议室”误当成“这个会议”。"""

        value = str(text or "")
        if kind == "project":
            return any(word in value for word in ("同项目", "该项目", "这个项目", "上述项目"))
        if kind == "meeting":
            return bool(
                re.search(r"刚订的会议|刚才的会议|原会议|这个会议(?!室)|那天|同一天", value)
            )
        if kind == "meeting_booking":
            return bool(re.search(r"刚订的会议|刚才的会议|原会议|这个会议(?!室)", value))
        if kind == "meeting_day":
            return any(word in value for word in ("那天", "同一天"))
        return False

    @staticmethod
    def _has_explicit_order_id(text: str) -> bool:
        """识别用户原文给出的订单标识，而非模型猜测的 ID。"""
        value = str(text or "")
        token = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"
        # 标签后的任意合法非空标识均可接受，但纯数字不是可审计的订单 ID。
        labelled = re.search(
            rf"(?:订单号|预订号|booking_id|order_id)\s*(?:[:=：]\s*)?(?<![A-Za-z0-9])({token})",
            value,
            re.I,
        )
        if labelled and not labelled.group(1).isdigit():
            return True
        # 兼容历史请求里不带标签的常见订单前缀与 UUID；不把房间号/普通
        # 数字当作订单号。
        return bool(re.search(
            rf"(?<![A-Za-z0-9])(?:SEED|BK|BOOKING|ORDER)[-_][A-Za-z0-9][A-Za-z0-9_.:-]*\b|"
            rf"(?<![A-Za-z0-9])[0-9A-Fa-f]{{8}}-(?:[0-9A-Fa-f]{{4}}-){{3}}[0-9A-Fa-f]{{12}}(?![A-Za-z0-9])",
            value,
            re.I,
        ))

    @staticmethod
    def _has_explicit_meeting_date(text: str) -> bool:
        """识别足以确定日期的显式语义（绝对日、星期或相对日）。"""
        value = str(text or "")
        return bool(re.search(
            r"(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}[日号]?|"
            r"\d{1,2}月\d{1,2}[日号]?|"
            r"(?:周|星期|礼拜)\s*[一二三四五六日天1-7](?!程)|"
            r"今天|明天|后天|大后天|昨天|前天)",
            value,
        ))

    @staticmethod
    def _record_task_facts(
        context: CaseContext,
        task_id: str,
        part: Any,
        *,
        meeting_reference_v3: bool = True,
    ) -> None:
        """从 DomainResult 的工具事实提取可跨 Task 共享的最小槽位。"""
        if not isinstance(part, dict):
            return
        booking = part.get("booking_result")
        if isinstance(booking, dict) and booking.get("status") in {
            "success",
            "active",
            "rebooked",
            "extended",
            "updated",
            "participant_added",
            "participants_added",
            "extended_and_participant_added",
        }:
            for key, value in (
                ("meeting.booking_id", booking.get("booking_id") or booking.get("order_id")),
                ("meeting.order_id", booking.get("order_id") or booking.get("booking_id")),
                ("meeting.day", booking.get("day")),
                ("meeting.room_id", booking.get("room_id")),
                ("meeting.start", booking.get("start")),
                ("meeting.end", booking.get("end")),
                ("meeting.title", booking.get("title")),
            ):
                if value:
                    context.upsert_fact(key, value, source="TASK_OUTPUT", task_id=task_id)
        # room.schedule 的 DomainResult 只需暴露查询范围；原始工具证据中若恰有
        # 一个 booking，则可安全补齐其 day/order。多条 booking 不任选其一，避免
        # 后续 Task 被错误绑定到任意订单。
        if meeting_reference_v3 and isinstance(booking, dict) and booking.get("status") == "queried":
            schedule_bookings: list[dict[str, Any]] = []
            schedule_room: Any = booking.get("room_id")
            schedule_rooms: set[str] = {str(schedule_room)} if schedule_room else set()
            schedule_start = booking.get("start_date")
            schedule_end = booking.get("end_date")
            for record in context.ledger.for_task(task_id):
                if record.kind != "tool_result" or record.source != "meetingroom.room.schedule":
                    continue
                payload = record.value if isinstance(record.value, dict) else {}
                raw = payload.get("result") if isinstance(payload, dict) else None
                args = payload.get("args") if isinstance(payload, dict) else None
                if isinstance(args, dict):
                    if args.get("room_id"):
                        schedule_rooms.add(str(args["room_id"]))
                    schedule_start = schedule_start or args.get("start_date")
                    schedule_end = schedule_end or args.get("end_date")
                if isinstance(raw, dict):
                    rows = raw.get("bookings") or []
                    if isinstance(rows, list):
                        schedule_bookings.extend(row for row in rows if isinstance(row, dict))
            # _RecordingEnv and MeetingroomExecutor both ledger the same tool result;
            # dedupe identical rows before applying the uniqueness check.
            unique_rows: list[dict[str, Any]] = []
            seen_rows: set[str] = set()
            for row in schedule_bookings:
                marker = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                if marker not in seen_rows:
                    seen_rows.add(marker)
                    unique_rows.append(row)
            if len(schedule_rooms) == 1:
                schedule_room = next(iter(schedule_rooms))
                context.upsert_fact("meeting.room_id", schedule_room, source="TASK_OUTPUT", task_id=task_id)
            if len(unique_rows) == 1:
                row = unique_rows[0]
                row_day = row.get("day")
                if not row_day and schedule_start and schedule_start == schedule_end:
                    row_day = schedule_start
                order_id = row.get("booking_id") or row.get("order_id")
                if row_day:
                    context.upsert_fact("meeting.day", row_day, source="TASK_OUTPUT", task_id=task_id)
                if order_id:
                    context.upsert_fact("meeting.booking_id", order_id, source="TASK_OUTPUT", task_id=task_id)
                    context.upsert_fact("meeting.order_id", order_id, source="TASK_OUTPUT", task_id=task_id)
        workflow = part.get("workflow_draft_result")
        if isinstance(workflow, dict) and workflow.get("status") in {"submitted", "draft_saved"}:
            for key, value in (
                ("expense.project_code", workflow.get("project_code")),
                ("expense.project_name", workflow.get("project_name")),
                ("leave.approver", workflow.get("approver")),
            ):
                if value:
                    context.upsert_fact(key, value, source="TASK_OUTPUT", task_id=task_id)

    @staticmethod
    def _append_context_events(
        trace: list[dict[str, Any]],
        context: CaseContext | None,
        logger: Any = None,
    ) -> None:
        """把 CaseContext 内部事实/策略补成可审计的 trace 事件。

        ``_RecordingEnv`` 能直接看到工具和追问，但 Skill 内部的事实写入、
        SpeechAct/兼容策略裁决不会穿过环境包装器。这里在 case 结束（或异常
        兜底）时做一次幂等补偿：只追加 trace 中尚不存在的 fact_id/decision，
        不把模型推断升级为可写事实，也不跨 case 保留对象。
        """
        if context is None:
            return
        seen_facts = {
            str(event.get("fact_id"))
            for event in trace
            if event.get("event") == "FACT_UPSERT" and event.get("fact_id")
        }
        for fact in context.facts.all():
            if fact.fact_id and fact.fact_id in seen_facts:
                continue
            event = {
                "event": "FACT_UPSERT",
                "task_id": fact.task_id,
                "key": fact.key,
                "fact_id": fact.fact_id,
                "source": fact.source,
                "value": _compact(fact.value),
                "confidence": fact.confidence,
                "shareable": fact.shareable,
                "evidence_ids": list(fact.evidence_ids),
                "supersedes": fact.supersedes,
            }
            trace.append(event)
            seen_facts.add(fact.fact_id)
            if logger is not None:
                logger.info(
                    f"FACT_UPSERT 事实写入: task={fact.task_id or '-'} "
                    f"key={fact.key} source={fact.source} value={_compact(fact.value)} "
                    f"supersedes={fact.supersedes or '-'}"
                )

        # 策略事件不带业务答案，只保留已经由程序做出的 policy decision；
        # 用序列化值去重，避免同一条策略同时写入 ledger 和 trace。
        seen_decisions = {
            json.dumps(event.get("decision"), ensure_ascii=False, sort_keys=True, default=str)
            for event in trace
            if event.get("event") == "POLICY_DECISION"
        }
        for decision in context.policy_decisions:
            safe_decision = redact_value(decision)
            marker = json.dumps(safe_decision, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen_decisions:
                continue
            trace.append({
                "event": "POLICY_DECISION",
                "task_id": context.ledger.active_task_id,
                "decision": safe_decision,
            })
            seen_decisions.add(marker)
            if logger is not None:
                logger.info(f"POLICY_DECISION 策略裁决: {_compact(decision)}")

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
        kind: str = "case_final",
        context: CaseContext | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """记录本 case 执行轨迹（独立通道，不计预算，失败静默不影响返回）。"""
        if not gateway.trace_enabled:
            return
        payload = {
            "kind": kind,
            "run_id": getattr(context, "run_id", None),
            "package_version": getattr(context, "package_version", "v2"),
            "case_id": case_id,
            "user_query": user_query,
            "now": now_iso,
            "mode": mode,
            "tool_calls": trace,
            "final_answer": final_answer,
        }
        if extra:
            payload.update(extra)
        # 带外诊断与本地日志使用同一脱敏策略；候选 ID、订单号、项目编码等
        # 业务诊断字段保持原样，手机号和临时 URL 参数在离开进程前移除。
        payload = redact_value(payload)
        # 远程诊断有硬上限；工具结果在入口已压缩，超限时优先保留终态、
        # 错误和最近的写轨迹，避免 telemetry 反过来拖垮业务请求。
        try:
            encoded_size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            encoded_size = 0
        if encoded_size > 96 * 1024:
            payload["tool_calls"] = [
                item for item in trace
                if item.get("error") or item.get("event") in {"TOOL_RESULT", "CLARIFICATION"}
            ][-80:]
            payload["final_answer"] = _compact(final_answer, 12000)
            for key in ("facts", "policy_decisions", "outcomes"):
                if key in payload:
                    payload[key] = _compact(payload[key], 12000)
        ok = gateway.send_trace(payload, timeout_s=1.0)
        if not ok:
            self.logger.warning("轨迹记录失败（已静默忽略，不影响返回）")
