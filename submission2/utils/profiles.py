"""运行时 Profile 与 Gold 冲突隔离。

本模块只保存可解释的执行策略，不保存 case、Gold 或答案常量。旧实现被
``legacy_current`` 冻结，新的规则通过 ``hybrid_compat`` / ``generic_v2``
逐能力启用；这样可以在改造期间保留可回滚路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionProfile(str, Enum):
    """Agent 的行为档位。"""

    LEGACY_CURRENT = "legacy_current"
    HYBRID_COMPAT = "hybrid_compat"
    GENERIC_V2 = "generic_v2"


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
    calendar_profile: str = "normal"
    legacy_budget_templates: bool = True
    legacy_special_prompts: bool = True
    office_projection: str = "late_bound"
    # 会议“延长后恢复重订”没有明确分钟数时的公司默认。该值是流程配置，
    # 不是从 Gold/Case 推导；可用环境变量覆盖以便隐藏集 A/B。
    meeting_default_extend_minutes: int = 30
    capability_profiles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ProfileConfig":
        raw = (os.environ.get("AGENT_EXECUTION_PROFILE") or "hybrid_compat").strip().lower()
        try:
            profile = ExecutionProfile(raw)
        except ValueError:
            profile = ExecutionProfile.HYBRID_COMPAT
        default_calendar = "normal" if profile == ExecutionProfile.GENERIC_V2 else "simulator_compat"
        calendar = (os.environ.get("AGENT_CALENDAR_PROFILE") or default_calendar).strip().lower()
        if calendar not in {"normal", "simulator_compat"}:
            calendar = "normal"
        # 兼容层默认保留旧预算模板；generic 明确关闭，避免把隐藏明细当作事实。
        legacy_templates = profile != ExecutionProfile.GENERIC_V2
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
        return cls(
            profile=profile,
            calendar_profile=calendar,
            legacy_budget_templates=legacy_templates,
            legacy_special_prompts=profile == ExecutionProfile.LEGACY_CURRENT,
            meeting_default_extend_minutes=default_extend,
        )

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
            mode = "legacy" if profile != ExecutionProfile.GENERIC_V2 else "configured"
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
            elif semantic_context.get("event_leave_default") and profile != ExecutionProfile.GENERIC_V2:
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
