"""ToolContractReconciler + EffectiveToolRegistry：运行时工具契约对账与门禁。

对应 technical_design.md §3.1 的 ToolContractReconciler。职责：

- ``reconcile()`` 在每次 run 开始时，把静态索引（StaticContextStore）与
  ``env.list_tools()`` 的实际返回做 join，产出 ``EffectiveToolRegistry``；
- 对账原则：**运行时 schema 为权威**，静态索引只补充安全元数据（写工具名单）；
- 对账产出四类工具集合：

    available_tools       运行时已公开（可调用）；
    available_unmapped    运行时公开但静态索引未知（可用，但标记待审查）；
    disabled_tools        静态索引存在但运行时未公开（调用即 forbidden，先剔除）；
    schema_changed_tools  运行时 args_schema 与静态不一致（如 booking.create）。

防 forbidden 的第一道闸：``validate_call`` 在真正调 ``env.call_tool`` 之前做
「工具名是否已公开 / 必填参数是否齐全 / 参数类型是否正确」三项检查，命中即
拦截，避免把未授权工具调用送进 env（env 会记为 forbidden 违例并 AS 归零）。
"""

from __future__ import annotations

from typing import Any

from utils.logger import ConsoleLogger
from utils.static_context import StaticContextStore

# simulator 契约的「二选一必填」适配。
# 来源：contest/simulator/simulator/tools/meetingroom.py 的 booking_create——
# 运行时只要求 day/start/end/title + office_id 或 room_id 二选一，与
# tool_specs.json 的 required 列表（两者都必填）不同。validate_call 遇到此类
# 工具时，按 common 必填 + 任一 alternative 满足来校验，而非机械要求全部 required。
# 这是对 simulator 实际契约的如实建模，不是 case 特判。
_ALTERNATIVE_REQUIRED: dict[str, dict[str, Any]] = {
    "meetingroom.booking.create": {
        "common": ["day", "start", "end", "title"],
        "alternatives": [["office_id"], ["room_id"]],
    },
}


class ToolContractReconciler:
    """把静态工具索引与运行时 list_tools() 对账，产出 EffectiveToolRegistry。"""

    def __init__(
        self,
        static_store: StaticContextStore,
        logger: ConsoleLogger | None = None,
    ) -> None:
        """初始化。

        Args:
            static_store: 已加载的静态上下文（先验合同元数据）。
            logger: 感知层日志器；None 时静默。
        """
        self._static = static_store
        self._log = logger

    def reconcile(self, runtime_tools: Any) -> "EffectiveToolRegistry":
        """对账静态索引与运行时工具集。

        Args:
            runtime_tools: ``env.list_tools()`` 的返回（list[dict] 或 dict[name → spec]）。

        Returns:
            对账后的有效工具注册表。
        """
        runtime = self._runtime_by_name(runtime_tools)
        static_names = self._static.tool_names()

        mapped: set[str] = set()
        unmapped: set[str] = set()
        changed: set[str] = set()
        combined: dict[str, dict[str, Any]] = {}

        for name, runtime_spec in runtime.items():
            if name in static_names:
                mapped.add(name)
                if self._schema_changed(name, runtime_spec):
                    changed.add(name)
            else:
                unmapped.add(name)
            combined[name] = self._merge_spec(name, runtime_spec)

        disabled = static_names - set(runtime)
        reconciliation = {
            "available_tools": sorted(mapped | unmapped),
            "available_unmapped": sorted(unmapped),
            "disabled_tools": sorted(disabled),
            "schema_changed_tools": sorted(changed),
        }
        registry = EffectiveToolRegistry(combined, self._static, reconciliation)
        if self._log is not None:
            extra = f" disabled={sorted(disabled)}" if disabled else ""
            self._log.info(
                f"对账完成: available={len(mapped) + len(unmapped)} "
                f"unmapped={len(unmapped)} disabled={len(disabled)} "
                f"schema_changed={len(changed)}{extra}"
            )
        return registry

    @staticmethod
    def _runtime_by_name(runtime_tools: Any) -> dict[str, dict[str, Any]]:
        """把 list[dict] 或 dict 输入归一为 {name: spec}。

        官方 env 的 list_tools() 返回 list[dict]；_load_tool_specs 也兼容 dict
        输入，这里做同样的归一化以增强健壮性。
        """
        if isinstance(runtime_tools, dict):
            return dict(runtime_tools)
        result: dict[str, dict[str, Any]] = {}
        for item in runtime_tools or []:
            if isinstance(item, dict) and item.get("name"):
                result[item["name"]] = item
        return result

    def _schema_changed(self, name: str, runtime_spec: dict[str, Any]) -> bool:
        """运行时 args_schema 与静态索引不一致即视为 schema 变更。"""
        static_spec = self._static.tool_spec(name)
        if static_spec is None:
            return False
        return runtime_spec.get("args_schema") != static_spec.get("args_schema")

    def _merge_spec(self, name: str, runtime_spec: dict[str, Any]) -> dict[str, Any]:
        """合并运行时 schema（权威）与静态安全元数据（写名单）。"""
        static_spec = self._static.tool_spec(name) or {}
        return {
            "name": name,
            "description": (
                runtime_spec.get("description") or static_spec.get("description") or ""
            ),
            "args_schema": (
                runtime_spec.get("args_schema") or static_spec.get("args_schema") or {}
            ),
            "write": self._static.is_write(name),
            # risk/cost 是离线编译的调度提示；合法性和实际步数仍由运行时
            # schema / runner budget 决定，不能用它们替代工具校验。
            "risk": static_spec.get("risk") or ("high" if self._static.is_write(name) else "low"),
            "cost": static_spec.get("cost") or (2 if self._static.is_write(name) else 1),
            "contract_source": (
                "runtime+static" if self._static.is_known_tool(name) else "runtime_unmapped"
            ),
        }


class EffectiveToolRegistry:
    """对账后的有效工具注册表：可用性查询 + 读/写门禁 + 调用前校验。

    它是执行层调用 ``env.call_tool`` 之前的唯一工具事实来源：

    - ``is_available`` / ``can_execute_read`` / ``can_execute_write`` 决定某工具
      是否可执行；
    - ``validate_call`` 做防 forbidden 的调用前校验（未公开工具 / 缺参 / 类型错）。
    """

    def __init__(
        self,
        specs: dict[str, dict[str, Any]],
        static_store: StaticContextStore,
        reconciliation: dict[str, Any],
    ) -> None:
        """初始化。

        Args:
            specs: 对账合并后的工具 spec（name → 合并 spec）。
            static_store: 静态上下文（供写名单查询）。
            reconciliation: reconcile() 产出的四类工具集合。
        """
        self._specs = specs
        self._static = static_store
        self._available = set(reconciliation["available_tools"])
        self._unmapped = set(reconciliation["available_unmapped"])
        self._disabled = set(reconciliation["disabled_tools"])
        self._changed = set(reconciliation["schema_changed_tools"])

    # ------------------------------------------------------------ 可用性 --

    def spec(self, name: str) -> dict[str, Any] | None:
        """返回合并后的工具 spec（运行时 schema + 静态写标记）。"""
        return self._specs.get(name)

    def is_available(self, name: str) -> bool:
        """工具是否在运行时已公开（可调用）。"""
        return name in self._available

    def is_unmapped(self, name: str) -> bool:
        """工具是否为运行时独有（静态索引未知）。"""
        return name in self._unmapped

    def is_disabled(self, name: str) -> bool:
        """工具是否为静态存在但运行时未公开（禁止调用）。"""
        return name in self._disabled

    def is_write(self, name: str) -> bool:
        """工具是否属于写类工具（来自静态写名单）。"""
        spec = self._specs.get(name)
        return bool(spec and spec.get("write"))

    def can_execute_read(self, name: str) -> bool:
        """是否可执行读操作：运行时已公开即可（运行时证据为准）。

        运行时不存在的工具返回 False——读操作也不允许调未授权工具。
        """
        return self.is_available(name)

    def can_execute_write(self, name: str) -> bool:
        """是否可执行写操作：已公开 + 静态已知（mapped）+ 写类工具。

        拒绝「静态存在但运行时未公开」与「运行时独有」的工具执行写操作，
        这是防 forbidden 的门禁之一。
        """
        return self.is_available(name) and not self.is_unmapped(name) and self.is_write(name)

    # ---------------------------------------------------------- 调用校验 --

    def validate_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """调用前校验：未公开 / 缺必填 / 类型错 → ok=False。

        Args:
            name: 工具名。
            args: 调用参数 dict。

        Returns:
            {"ok": bool, "errors": [str], "warnings": [str]}。
            errors 非空时执行层不应发起调用。
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not self.is_available(name):
            errors.append(f"工具未在运行时公开，禁止调用: {name}")
            return {"ok": False, "errors": errors, "warnings": warnings}
        if self.is_unmapped(name):
            warnings.append(f"工具为运行时独有，静态索引未知: {name}")

        spec = self._specs.get(name) or {}
        schema = spec.get("args_schema") or {}
        properties = schema.get("properties") or {}

        adapter = _ALTERNATIVE_REQUIRED.get(name)
        if adapter is not None:
            # 应用「二选一必填」契约：common 全必填 + 任一 alternative 满足。
            for key in adapter["common"]:
                if key not in args:
                    errors.append(f"缺少必填参数: {key}")
            alternatives_ok = any(
                all(item in args for item in alternative)
                for alternative in adapter["alternatives"]
            )
            if not alternatives_ok:
                display = " 或 ".join("+".join(a) for a in adapter["alternatives"])
                errors.append(f"必填参数需满足其一: {display}")
        else:
            for key in schema.get("required") or []:
                if key not in args:
                    errors.append(f"缺少必填参数: {key}")

        for key, value in args.items():
            expected_type = (properties.get(key) or {}).get("type")
            if expected_type and not self._matches_type(value, expected_type):
                errors.append(
                    f"参数类型不符: {key} 期望 {expected_type}，"
                    f"实际 {type(value).__name__}"
                )

        return {"ok": not errors, "errors": errors, "warnings": warnings}

    @staticmethod
    def _matches_type(value: Any, expected_type: str) -> bool:
        """按 args_schema 的 type 做宽松类型匹配（bool 不算 int，避免 1/0 混淆）。"""
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        return True  # 未知类型不拦截，交给运行时兜底

    # -------------------------------------------------------------- 状态 --

    def status(self) -> dict[str, Any]:
        """对账状态摘要（供入口层打印控制台日志）。"""
        return {
            "available": len(self._available),
            "available_unmapped": sorted(self._unmapped),
            "disabled": sorted(self._disabled),
            "schema_changed": sorted(self._changed),
        }


# V2 对外契约名称；实现仍由 EffectiveToolRegistry 承担，避免两套门禁漂移。
ToolRegistry = EffectiveToolRegistry


__all__ = ["ToolContractReconciler", "EffectiveToolRegistry", "ToolRegistry"]
