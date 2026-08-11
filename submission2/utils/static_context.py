"""StaticContextStore：离线静态上下文存储（先验合同 / 目录元数据）。

对应 technical_design.md §3.1。加载 ``scripts/build_static_context.py`` 生成的
索引（默认 ``submission/static_context/``）：

- ``tools.index.json``      工具的名称 / 描述 / args_schema + 写工具名单；
- ``meetingrooms.index.json``   会议室的静态属性目录 + 二级索引。

设计意图（AGENT.md「静态契约只作先验，真实工具证据优先」）：
- 静态索引是「合同 / 目录」元数据，不是 case 记忆，不绑定任何 case；
- 会议室可用性 / 时间冲突 / 授权一律以运行时工具证据为准（``room.list`` /
  ``list_tools``），本 store 只提供归一化与候选过滤的先验；
- 不把会议室 ID 写成固定答案表：本 store 按 room_id / building / campus 组织，
  供执行层在拿到运行时候选后做二次过滤，绝不做「case → 固定 room_id」映射。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.logger import ConsoleLogger

# 静态上下文索引的 schema 版本（与 scripts/build_static_context.py 共享单一来源）。
SCHEMA_VERSION = "static-context-v1"

# 构建脚本产出的索引文件名（相对 base_dir）。
_MANIFEST_FILE = "manifest.json"
_TOOLS_INDEX_FILE = "tools.index.json"
_MEETINGROOMS_INDEX_FILE = "meetingrooms.index.json"


class StaticContextStore:
    """离线静态上下文存储：加载索引并提供查询。

    Attributes:
        base_dir: 索引所在目录（默认 submission/static_context）。
        enabled: 是否启用静态上下文；False 或索引缺失时降级为「空 store」，
                 即所有查询返回空结果，不抛异常（感知层以运行时为准仍可工作）。
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        enabled: bool = True,
        logger: ConsoleLogger | None = None,
    ) -> None:
        """初始化并加载索引。

        Args:
            base_dir: 索引目录；None 时取包内默认 submission/static_context。
            enabled: 是否加载静态上下文；False 时直接空 store。
            logger: 感知层日志器；None 时静默。
        """
        self.base_dir = (
            Path(base_dir) if base_dir is not None
            else Path(__file__).resolve().parents[1] / "static_context"
        )
        self.enabled = enabled
        self._log = logger
        self._manifest: dict[str, Any] = {}
        self._tools: dict[str, Any] = {}
        self._write_tools: set[str] = set()
        self._rooms: dict[str, Any] = {}
        self._by_office_id: dict[str, str] = {}
        self._by_building: dict[str, list[str]] = {}
        self._by_campus: dict[str, list[str]] = {}
        self._load()

    # ------------------------------------------------------------------ 加载 --

    def _load(self) -> None:
        """按 manifest 加载各索引；单项失败不影响其它索引（逐项容错）。

        静态上下文属于「锦上添花」的先验：目录缺失或损坏时，Agent 仍可完全
        依赖运行时证据工作，因此这里一律降级而非抛错。
        """
        if not self.enabled:
            self._log_info("静态上下文已禁用（enabled=False），降级为运行时只读")
            return
        if not self.base_dir.is_dir():
            self._log_warning(
                f"静态上下文目录不存在: {self.base_dir}，降级为运行时只读"
            )
            return
        self._manifest = self._load_json(_MANIFEST_FILE)
        self._tools = self._load_json(_TOOLS_INDEX_FILE)
        self._write_tools = set(self._tools.get("write_tools") or [])
        rooms_index = self._load_json(_MEETINGROOMS_INDEX_FILE)
        self._rooms = rooms_index.get("by_room_id") or {}
        self._by_office_id = rooms_index.get("by_office_id") or {}
        self._by_building = rooms_index.get("by_building") or {}
        self._by_campus = rooms_index.get("by_campus") or {}
        self._log_info(
            f"静态上下文已加载: tools={len(self._tools.get('by_name') or {})} "
            f"write_tools={len(self._write_tools)} rooms={len(self._rooms)} dir={self.base_dir}"
        )

    def _load_json(self, filename: str) -> dict[str, Any]:
        """读取 base_dir 下的 JSON 索引；缺失或损坏时返回空 dict。"""
        path = self.base_dir / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._log_warning(f"静态上下文索引读取失败，跳过: {path}")
            return {}

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)

    def _log_warning(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)

    # ------------------------------------------------------------------ 工具 --

    def tool_spec(self, name: str) -> dict[str, Any] | None:
        """返回工具的静态 spec（含 args_schema），未知工具返回 None。

        Args:
            name: 工具名。

        Returns:
            静态 spec dict，或 None。
        """
        return (self._tools.get("by_name") or {}).get(name)

    def tool_names(self) -> set[str]:
        """返回静态索引中已知的工具名集合。"""
        return set((self._tools.get("by_name") or {}).keys())

    def is_known_tool(self, name: str) -> bool:
        """name 是否在静态索引中。"""
        return name in (self._tools.get("by_name") or {})

    def is_write(self, name: str) -> bool:
        """name 是否属于写类工具（执行层写操作门禁的先验）。"""
        return name in self._write_tools

    # -------------------------------------------------------------- 会议室 --

    def room(self, room_id: str) -> dict[str, Any] | None:
        """按内部 room_id 返回会议室静态属性。

        Args:
            room_id: 内部房间 ID（如 "0552-001"）。

        Returns:
            房间静态属性 dict，或 None。
        """
        return self._rooms.get(room_id)

    def office_id_for_room(self, room_id: str) -> str | None:
        """内部 room_id → officeId UUID。

        Args:
            room_id: 内部房间 ID。

        Returns:
            officeId UUID，或 None（房间未知）。
        """
        record = self._rooms.get(room_id)
        if not record:
            return None
        return record.get("officeId")

    def room_id_for_office_id(self, office_id: str) -> str | None:
        """officeId UUID → 内部 room_id（反向映射）。

        Args:
            office_id: 32 位 officeId UUID。

        Returns:
            内部 room_id，或 None。
        """
        return self._by_office_id.get(office_id)

    def rooms_by_building(self, building: str) -> list[str]:
        """按楼栋名列出该楼内的 room_id 列表（候选过滤先验）。

        Args:
            building: 楼栋名（如 "A1"）。

        Returns:
            room_id 列表；无匹配时为空列表。
        """
        return list(self._by_building.get(building) or [])

    def rooms_by_campus(self, campus: str) -> list[str]:
        """按园区名列出该园区内的 room_id 列表（候选过滤先验）。

        Args:
            campus: 园区名（如 "合肥"）。

        Returns:
            room_id 列表；无匹配时为空列表。
        """
        return list(self._by_campus.get(campus) or [])

    # --------------------------------------------------------------- 汇总 --

    def counts(self) -> dict[str, int]:
        """索引规模汇总（用于日志 / 状态展示）。"""
        return {
            "tools": len(self._tools.get("by_name") or {}),
            "write_tools": len(self._write_tools),
            "rooms": len(self._rooms),
        }

    def status(self) -> dict[str, Any]:
        """感知层状态摘要（供入口层打印控制台日志）。

        Returns:
            {"enabled", "base_dir", "schema_version", "counts", "files_loaded"}。
        """
        return {
            "enabled": self.enabled,
            "base_dir": str(self.base_dir),
            "schema_version": self._manifest.get("schema_version") or SCHEMA_VERSION,
            "counts": self.counts(),
            "files_loaded": {
                "manifest": bool(self._manifest),
                "tools_index": bool(self._tools),
                "meetingrooms_index": bool(self._rooms) or bool(self._by_office_id),
            },
        }
