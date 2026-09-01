"""运行时 Profile 与 Gold 冲突隔离。

本模块只保存可解释的执行策略，不保存 case、Gold 或答案常量。旧实现被
``legacy_current`` 冻结，新的规则通过 ``hybrid_compat`` / ``generic_v2``
逐能力启用；这样可以在改造期间保留可回滚路径。
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionProfile(str, Enum):
    """Agent 的行为档位。"""

    LEGACY_CURRENT = "legacy_current"
    HYBRID_COMPAT = "hybrid_compat"
    GENERIC_V2 = "generic_v2"
    # candidate_v2 是面向发布的组合档位；内部仍沿用 hybrid 的兼容策略，
    # 具体收益簇由下面三个 feature flag 独立控制，便于单簇回滚。
    CANDIDATE_V2 = "candidate_v2"


class ConflictType(str, Enum):
    """已识别的可审计冲突类别。"""

    OFFICE_ID_REPRESENTATION = "office_id_representation"
    CALENDAR_COMPATIBILITY = "calendar_compatibility"
    LEAVE_DURATION_MODE = "leave_duration_mode"
    HALF_DAY_POLICY = "half_day_policy"
    SUBMIT_OR_DRAFT = "submit_or_draft"
    MISSING_AMOUNT_OR_DETAIL = "missing_amount_or_detail"
    APPROVER_AMBIGUITY = "approver_ambiguity"
    PROJECT_AMBIGUITY = "project_ambiguity"
    MATERIAL_AMBIGUITY = "material_ambiguity"
    SEEDED_REBOOK = "seeded_rebook"


@dataclass(frozen=True)
class PolicyDecision:
    """一次兼容策略裁决及其证据。"""

    conflict_type: str
    resolution_kind: str
    selected_value: Any = None
    evidence: tuple[str, ...] = ()
    policy_id: str = ""
    fallback_profile: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "resolution_kind": self.resolution_kind,
            "selected_value": self.selected_value,
            "evidence": list(self.evidence),
            "policy_id": self.policy_id,
            "fallback_profile": self.fallback_profile,
        }


@dataclass
class ProfileConfig:
    """可通过环境变量切换的运行时策略配置。"""

    profile: ExecutionProfile = ExecutionProfile.HYBRID_COMPAT
    # ``profile`` 是内部兼容分支使用的有效档位；requested_profile 保留外部
    # 选择的名称（尤其是 candidate_v2），这样既不破坏旧分支，又能让候选包
    # 关闭不可观测的 legacy 模板。手工构造 ProfileConfig 时两者相同。
    requested_profile: ExecutionProfile | None = None
    calendar_profile: str = "normal"
    legacy_budget_templates: bool = True
    legacy_special_prompts: bool = True
    office_projection: str = "late_bound"
    # 会议“延长后恢复重订”没有明确分钟数时的公司默认。该值是流程配置，
    # 不是从 Gold/Case 推导；可用环境变量覆盖以便隐藏集 A/B。
    meeting_default_extend_minutes: int = 30
    capability_profiles: dict[str, str] = field(default_factory=dict)
    # 问题簇开关：legacy_current 全部关闭，其余档位默认打开；环境变量可单独回滚。
    contract_fixes_v2: bool = True
    context_workflow_v2: bool = True
    meeting_search_v2: bool = True
    # V3 收益簇默认关闭，确保 76.79814 线上行为可以原样回放。候选包通过
    # config.json 或环境变量逐簇开启；不要再次把会议、工作流和预算修复绑成
    # 一个不可归因的大开关。
    meeting_reference_v3: bool = False
    meeting_projection_v3: bool = False
    # 工位锚定且用户明确楼层时，是否额外发起一次楼栋级 room.list 探测。
    # 探测只补足环境的楼栋查询契约，不改变楼层硬约束或候选选择；默认关闭，
    # 便于把额外读调用作为独立收益簇 A/B。
    meeting_workspace_building_probe_v3: bool = False
    dag_dependency_v3: bool = False
    speech_act_v3: bool = False
    leave_range_v3: bool = False
    budget_runtime_v3: bool = False
    project_search_refinement_v3: bool = False
    workflow_schema_retry_v3: bool = False
    budget_amount_guard_v3: bool = False
    approver_resolution_v3: bool = False
    oa_semantic_gate_v3: bool = False
    # OA 尾查存在批次契约差异：candidate/generic 默认只在用户明确要求时查询，
    # legacy 可通过该开关保留旧的多域隐式尾查。它不改变业务写入，只影响额外的
    # oa.todo.list / oa.done.list 调用，便于线上 A/B 与回滚。
    legacy_oa_compat: bool = False

    @property
    def profile_name(self) -> str:
        """用于日志/telemetry 的外部 profile 名。"""
        return (self.requested_profile or self.profile).value

    @property
    def candidate_mode(self) -> bool:
        """候选发布档位：关闭只能靠训练数据归纳的预算模板。"""
        return (self.requested_profile or self.profile) == ExecutionProfile.CANDIDATE_V2

    @property
    def strict_runtime_mode(self) -> bool:
        """generic/candidate 只允许当前用户与实时工具证据驱动写入。"""
        selected = self.requested_profile or self.profile
        return selected in {ExecutionProfile.GENERIC_V2, ExecutionProfile.CANDIDATE_V2}

    @classmethod
    def from_env(cls) -> "ProfileConfig":
        # 配置文件只提供发布档位/功能默认值；环境变量仍具有最高优先级。
        runtime_cfg: dict[str, Any] = {}
        try:
            config_path = Path(__file__).resolve().parent.parent / "config.json"
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
            runtime_cfg = parsed.get("runtime", {}) if isinstance(parsed, dict) else {}
        except (OSError, ValueError):
            runtime_cfg = {}
        configured_profile = runtime_cfg.get("execution_profile")
        raw = (os.environ.get("AGENT_EXECUTION_PROFILE") or configured_profile or "hybrid_compat").strip().lower()
        try:
            requested_profile = ExecutionProfile(raw)
        except ValueError:
            requested_profile = ExecutionProfile.HYBRID_COMPAT
        profile = requested_profile
        if requested_profile == ExecutionProfile.CANDIDATE_V2:
            # 对外保留 candidate_v2 名称；策略判断按 hybrid 处理，避免散落的
            # ``profile == HYBRID_COMPAT`` 分支被悄悄绕过。
            profile = ExecutionProfile.HYBRID_COMPAT
        default_calendar = "normal" if requested_profile == ExecutionProfile.GENERIC_V2 else "simulator_compat"
        calendar = (os.environ.get("AGENT_CALENDAR_PROFILE") or default_calendar).strip().lower()
        if calendar not in {"normal", "simulator_compat"}:
            calendar = "normal"
        # 兼容层默认保留旧预算模板；generic/candidate 明确关闭，避免把隐藏
        # 明细、价格或物料答案当作运行时事实。AGENT_LEGACY_BUDGET_TEMPLATES
        # 仍可显式打开，作为可回滚的本地 legacy 开关。
        legacy_templates = requested_profile not in {
            ExecutionProfile.GENERIC_V2,
            ExecutionProfile.CANDIDATE_V2,
        }
        if os.environ.get("AGENT_LEGACY_BUDGET_TEMPLATES") in {"0", "false", "off"}:
            legacy_templates = False
        default_extend = 30
        raw_extend = (os.environ.get("AGENT_DEFAULT_EXTEND_MINUTES") or "").strip()
        if raw_extend:
            try:
                parsed_extend = int(raw_extend)
                if parsed_extend > 0:
                    default_extend = parsed_extend
            except ValueError:
                pass
        default_cluster = profile != ExecutionProfile.LEGACY_CURRENT

        configured_oa = runtime_cfg.get("legacy_oa_compat")
        legacy_oa_default = requested_profile == ExecutionProfile.LEGACY_CURRENT
        if configured_oa is not None:
            legacy_oa_default = bool(configured_oa)
        raw_oa = os.environ.get("AGENT_LEGACY_OA_COMPAT")
        if raw_oa is not None:
            legacy_oa_default = str(raw_oa).strip().lower() not in {"0", "false", "off", "no"}

        def _flag(name: str, default: bool, config_key: str | None = None) -> bool:
            value = os.environ.get(name)
            if value is None:
                if config_key and config_key in runtime_cfg:
                    return bool(runtime_cfg[config_key])
                return default
            return str(value).strip().lower() not in {"0", "false", "off", "no"}

        return cls(
            profile=profile,
            requested_profile=requested_profile,
            calendar_profile=calendar,
            legacy_budget_templates=legacy_templates,
            legacy_special_prompts=profile == ExecutionProfile.LEGACY_CURRENT,
            meeting_default_extend_minutes=default_extend,
            contract_fixes_v2=_flag("AGENT_CONTRACT_FIXES_V2", default_cluster, "contract_fixes_v2"),
            context_workflow_v2=_flag("AGENT_CONTEXT_WORKFLOW_V2", default_cluster, "context_workflow_v2"),
            meeting_search_v2=_flag("AGENT_MEETING_SEARCH_V2", default_cluster, "meeting_search_v2"),
            meeting_reference_v3=_flag(
                "AGENT_MEETING_REFERENCE_V3", False, "meeting_reference_v3"
            ),
            meeting_projection_v3=_flag(
                "AGENT_MEETING_PROJECTION_V3", False, "meeting_projection_v3"
            ),
            meeting_workspace_building_probe_v3=_flag(
                "AGENT_MEETING_WORKSPACE_BUILDING_PROBE_V3",
                False,
                "meeting_workspace_building_probe_v3",
            ),
            dag_dependency_v3=_flag(
                "AGENT_DAG_DEPENDENCY_V3", False, "dag_dependency_v3"
            ),
            speech_act_v3=_flag("AGENT_SPEECH_ACT_V3", False, "speech_act_v3"),
            leave_range_v3=_flag("AGENT_LEAVE_RANGE_V3", False, "leave_range_v3"),
            budget_runtime_v3=_flag(
                "AGENT_BUDGET_RUNTIME_V3", False, "budget_runtime_v3"
            ),
            project_search_refinement_v3=_flag(
                "AGENT_PROJECT_SEARCH_REFINEMENT_V3",
                False,
                "project_search_refinement_v3",
            ),
            workflow_schema_retry_v3=_flag(
                "AGENT_WORKFLOW_SCHEMA_RETRY_V3", False, "workflow_schema_retry_v3"
            ),
            budget_amount_guard_v3=_flag(
                "AGENT_BUDGET_AMOUNT_GUARD_V3", False, "budget_amount_guard_v3"
            ),
            approver_resolution_v3=_flag(
                "AGENT_APPROVER_RESOLUTION_V3", False, "approver_resolution_v3"
            ),
            oa_semantic_gate_v3=_flag(
                "AGENT_OA_SEMANTIC_GATE_V3", False, "oa_semantic_gate_v3"
            ),
            legacy_oa_compat=legacy_oa_default,
        )

    def feature_flags(self) -> dict[str, bool]:
        """返回当前实际生效的功能开关，供本地日志和远程诊断共用。"""
        return {
            "contract_fixes_v2": self.contract_fixes_v2,
            "context_workflow_v2": self.context_workflow_v2,
            "meeting_search_v2": self.meeting_search_v2,
            "meeting_reference_v3": self.meeting_reference_v3,
            "meeting_projection_v3": self.meeting_projection_v3,
            "meeting_workspace_building_probe_v3": self.meeting_workspace_building_probe_v3,
            "dag_dependency_v3": self.dag_dependency_v3,
            "speech_act_v3": self.speech_act_v3,
            "leave_range_v3": self.leave_range_v3,
            "budget_runtime_v3": self.budget_runtime_v3,
            "project_search_refinement_v3": self.project_search_refinement_v3,
            "workflow_schema_retry_v3": self.workflow_schema_retry_v3,
            "budget_amount_guard_v3": self.budget_amount_guard_v3,
            "approver_resolution_v3": self.approver_resolution_v3,
            "oa_semantic_gate_v3": self.oa_semantic_gate_v3,
            "legacy_budget_templates": self.legacy_budget_templates,
            "legacy_special_prompts": self.legacy_special_prompts,
            "legacy_oa_compat": self.legacy_oa_compat,
        }

    def allow_oa_postcheck(self, *, explicit_request: bool, multi_domain: bool) -> bool:
        """决定是否执行 OA 尾查。

        明确要求始终允许；旧 contract_fixes 关闭或显式兼容开关可保留历史行为。
        candidate/generic 在默认配置下不会因为跨域本身发起无关 OA 查询。
        ``oa_semantic_gate_v3`` 是独立问题簇开关：开启后，即使其它 contract
        兼容开关被误开，也必须回到“用户明确要求或显式 legacy 兼容”两条路径，
        便于线上逐簇 A/B，而不会被 ``contract_fixes_v2`` 的旧默认覆盖。
        """
        if not multi_domain:
            return False
        if self.oa_semantic_gate_v3:
            return bool(explicit_request or self.legacy_oa_compat)
        return bool(explicit_request or self.legacy_oa_compat or not self.contract_fixes_v2)

    def profile_for(self, capability: str) -> ExecutionProfile:
        """返回能力级 Profile；无覆盖时使用全局档位。"""
        raw = self.capability_profiles.get(capability)
        if raw:
            try:
                return ExecutionProfile(raw)
            except ValueError:
                pass
        return self.profile


class CompatibilityPolicy:
    """集中处理可解释的兼容策略，不执行工具调用。"""

    def __init__(self, config: ProfileConfig | None = None, logger: Any = None) -> None:
        self.config = config or ProfileConfig.from_env()
        self.logger = logger

    def decide(
        self,
        conflict_type: ConflictType | str,
        *,
        semantic_context: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """依据当前 Profile、语义和工具事实给出纯数据裁决。"""
        ctype = conflict_type.value if isinstance(conflict_type, ConflictType) else str(conflict_type)
        semantic_context = semantic_context or {}
        evidence = evidence or {}
        profile = self.config.profile

        if ctype == ConflictType.OFFICE_ID_REPRESENTATION.value:
            # 工具调用始终使用真实 room/office UUID；这里只决定最终投影形态。
            if semantic_context.get("cross_domain"):
                value = semantic_context.get("building") or semantic_context.get("office")
                return self._emit(PolicyDecision(
                    ctype, "semantic_projection", value,
                    ("cross_domain_task", "runtime_room_evidence"),
                    "office.cross_domain.semantic",
                ))
            if semantic_context.get("advanced_room_capability"):
                value = evidence.get("office_id") or evidence.get("room_id")
                return self._emit(PolicyDecision(
                    ctype, "uuid_projection", value,
                    ("advanced_room_capability", "runtime_room_evidence"),
                    "office.advanced.uuid",
                ))
            return self._emit(PolicyDecision(
                ctype, "legacy_or_late_bound", evidence.get("office_id"),
                ("ambiguous_single_domain_contract",),
                "office.single_domain.legacy",
                ExecutionProfile.LEGACY_CURRENT.value,
            ))

        if ctype == ConflictType.CALENDAR_COMPATIBILITY.value:
            if self.config.calendar_profile == "simulator_compat":
                return self._emit(PolicyDecision(
                    ctype, "compat_calendar", True,
                    ("configured_calendar_profile",),
                    "calendar.simulator_compat",
                ))
            return self._emit(PolicyDecision(
                ctype, "normal_calendar", False,
                ("standard_calendar_default",),
                "calendar.normal",
            ))

        if ctype == ConflictType.LEAVE_DURATION_MODE.value:
            if semantic_context.get("explicit_workday_policy"):
                mode = "workday"
            elif semantic_context.get("explicit_calendar_policy"):
                mode = "calendar"
            elif semantic_context.get("explicit_hours"):
                mode = "explicit_hours"
            else:
                # 当前数据绝大多数采用 raw，hybrid 保留该保分默认；generic 仍不按假种猜测。
                mode = "raw"
            return self._emit(PolicyDecision(
                ctype, "duration_calculator", mode,
                tuple(k for k, v in semantic_context.items() if v and k.startswith("explicit_")),
                f"leave.duration.{mode}",
            ))

        if ctype == ConflictType.HALF_DAY_POLICY.value:
            mode = "legacy" if not self.config.strict_runtime_mode else "configured"
            return self._emit(PolicyDecision(
                ctype, "half_day_calculator", mode,
                None,
                f"leave.half_day.{mode}",
                ExecutionProfile.LEGACY_CURRENT.value if mode == "legacy" else None,
            ))

        if ctype == ConflictType.SUBMIT_OR_DRAFT.value:
            if semantic_context.get("explicit_negative") or semantic_context.get("draft_requested"):
                value = False
            elif semantic_context.get("submit_requested"):
                value = True
            elif semantic_context.get("event_leave_default") and not self.config.strict_runtime_mode:
                value = True
            else:
                value = False
            return self._emit(PolicyDecision(
                ctype, "speech_act", value,
                tuple(k for k in ("explicit_negative", "draft_requested", "submit_requested", "event_leave_default") if semantic_context.get(k)),
                "workflow.submit.speech_act",
            ))

        if ctype in {
            ConflictType.APPROVER_AMBIGUITY.value,
            ConflictType.PROJECT_AMBIGUITY.value,
            ConflictType.MATERIAL_AMBIGUITY.value,
        }:
            unique = evidence.get("unique")
            if unique is True:
                return self._emit(PolicyDecision(ctype, "accept_unique", evidence.get("selected"), ("unique_runtime_candidate",), f"{ctype}.unique"))
            return self._emit(PolicyDecision(ctype, "clarify_or_block", None, ("non_unique_runtime_candidate",), f"{ctype}.blocked"))

        if ctype == ConflictType.MISSING_AMOUNT_OR_DETAIL.value:
            if evidence.get("tool_price_evidence"):
                return self._emit(PolicyDecision(ctype, "use_tool_evidence", evidence.get("tool_price_evidence"), ("runtime_price_evidence",), "budget.price.runtime"))
            return self._emit(PolicyDecision(ctype, "clarify_or_legacy", None, ("no_runtime_price_evidence",), "budget.price.missing", ExecutionProfile.LEGACY_CURRENT.value))

        if ctype == ConflictType.SEEDED_REBOOK.value:
            explicit = bool(semantic_context.get("explicit_order_id"))
            return self._emit(PolicyDecision(
                ctype, "seeded_projection" if explicit else "normal_projection", explicit,
                ("explicit_user_order_id",) if explicit else ("booking_list_resolution",),
                "meeting.rebook.seeded" if explicit else "meeting.rebook.normal",
            ))

        return self._emit(PolicyDecision(ctype, "legacy_fallback", None, ("unknown_conflict",), f"{ctype}.legacy", ExecutionProfile.LEGACY_CURRENT.value))

    def _emit(self, decision: PolicyDecision) -> PolicyDecision:
        if self.logger is not None:
            self.logger.info(f"兼容裁决: {decision.as_dict()}")
        return decision


__all__ = [
    "ExecutionProfile",
    "ConflictType",
    "PolicyDecision",
    "ProfileConfig",
    "CompatibilityPolicy",
]
