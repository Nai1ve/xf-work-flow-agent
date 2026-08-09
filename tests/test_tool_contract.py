"""ToolContractReconciler + EffectiveToolRegistry 的单元测试。

覆盖（technical_design.md §3.1 对账逻辑）：
- 全匹配 / 运行时缺工具 → disabled / 运行时多工具 → unmapped / schema 不一致；
- validate_call 防 forbidden：未公开 / 缺必填 / 类型错；
- booking.create「office_id 或 room_id 二选一」契约适配；
- 读 / 写门禁（can_execute_read / can_execute_write）。

测试全部使用 tests/conftest.py 的内联 fixture，不依赖 gitignored 的 contest 数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _MINIMAL_TOOLS_INDEX, write_minimal_static_context
from utils.static_context import StaticContextStore
from utils.tool_contract import EffectiveToolRegistry, ToolContractReconciler

ROOM_LIST = "meetingroom.room.list"
BOOKING_CREATE = "meetingroom.booking.create"


def make_reconciler(tmp_path: Path) -> ToolContractReconciler:
    store = StaticContextStore(base_dir=write_minimal_static_context(tmp_path))
    return ToolContractReconciler(store)


def runtime_spec(name: str, args_schema: dict) -> dict:
    return {"name": name, "description": f"desc of {name}", "args_schema": args_schema}


def static_schema(name: str) -> dict:
    """取 fixture 静态索引中该工具的 args_schema（与构建产物同源）。"""
    return _MINIMAL_TOOLS_INDEX["by_name"][name]["args_schema"]


# 测试用运行时 schema 常量，缩短各场景的 spec 构造。
_EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}
_ROOM_LIST_DAY_SCHEMA = {
    "type": "object",
    "properties": {"day": {"type": "string"}},
    "required": ["day"],
}


def test_reconcile_full_match(tmp_path: Path) -> None:
    # 运行时 schema 与静态完全一致 → 无 schema_changed、无 disabled/unmapped。
    registry = make_reconciler(tmp_path).reconcile(
        [
            runtime_spec(ROOM_LIST, static_schema(ROOM_LIST)),
            runtime_spec(BOOKING_CREATE, static_schema(BOOKING_CREATE)),
        ]
    )
    assert registry.is_available(ROOM_LIST)
    assert registry.is_available(BOOKING_CREATE)
    assert registry.status() == {
        "available": 2,
        "available_unmapped": [],
        "disabled": [],
        "schema_changed": [],
    }


def test_reconcile_runtime_missing_tool(tmp_path: Path) -> None:
    # 运行时只公开 room.list：booking.create 应进入 disabled（防 forbidden）。
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _EMPTY_SCHEMA)]
    )
    assert registry.status()["disabled"] == [BOOKING_CREATE]
    assert registry.is_disabled(BOOKING_CREATE)
    assert registry.status()["available"] == 1


def test_reconcile_runtime_extra_tool(tmp_path: Path) -> None:
    # 运行时多一个静态未知工具：进入 available_unmapped。
    registry = make_reconciler(tmp_path).reconcile(
        [
            runtime_spec(ROOM_LIST, _EMPTY_SCHEMA),
            runtime_spec(BOOKING_CREATE, _EMPTY_SCHEMA),
            runtime_spec("user.get_workspace", _EMPTY_SCHEMA),
        ]
    )
    assert registry.status()["available_unmapped"] == ["user.get_workspace"]
    assert registry.is_unmapped("user.get_workspace")
    assert registry.status()["available"] == 3


def test_reconcile_schema_changed(tmp_path: Path) -> None:
    # 运行时 args_schema 与静态不一致 → schema_changed。
    runtime_schema = {
        "type": "object",
        "properties": {"day": {"type": "string"}},
        "required": ["day", "extra"],
    }
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, runtime_schema)]
    )
    assert registry.status()["schema_changed"] == [ROOM_LIST]


def test_validate_call_rejects_disabled(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _EMPTY_SCHEMA)]
    )
    result = registry.validate_call(BOOKING_CREATE, {"day": "2026-04-21"})
    assert result["ok"] is False
    assert any("未在运行时公开" in error for error in result["errors"])


def test_validate_call_rejects_unknown(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _EMPTY_SCHEMA)]
    )
    assert registry.validate_call("nonexistent.tool", {})["ok"] is False


def test_validate_call_missing_required(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _ROOM_LIST_DAY_SCHEMA)]
    )
    result = registry.validate_call(ROOM_LIST, {})
    assert result["ok"] is False
    assert any("day" in error for error in result["errors"])


def test_validate_call_type_error(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _ROOM_LIST_DAY_SCHEMA)]
    )
    result = registry.validate_call(ROOM_LIST, {"day": 123})
    assert result["ok"] is False
    assert any("类型不符" in error for error in result["errors"])


def test_validate_call_ok(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [runtime_spec(ROOM_LIST, _ROOM_LIST_DAY_SCHEMA)]
    )
    result = registry.validate_call(ROOM_LIST, {"day": "2026-04-21"})
    assert result["ok"] is True
    assert result["errors"] == []


def test_booking_create_or_adapter(tmp_path: Path) -> None:
    # booking.create 运行时契约：day/start/end/title + office_id 或 room_id 二选一。
    schema = {
        "type": "object",
        "properties": {
            "day": {"type": "string"},
            "office_id": {"type": "string"},
            "room_id": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["day", "office_id", "room_id", "start", "end", "title"],
    }
    registry = make_reconciler(tmp_path).reconcile([runtime_spec(BOOKING_CREATE, schema)])

    base = {"day": "2026-04-21", "start": "14:00", "end": "16:00", "title": "产品评审"}
    assert registry.validate_call(BOOKING_CREATE, {**base, "room_id": "0552-001"})["ok"] is True
    assert registry.validate_call(BOOKING_CREATE, {**base, "office_id": "office-uuid-1"})["ok"] is True
    # 两者都不提供 → 拦截。
    missing_both = registry.validate_call(BOOKING_CREATE, base)
    assert missing_both["ok"] is False
    assert any("office_id" in error for error in missing_both["errors"])
    # 缺 day → 拦截。
    assert registry.validate_call(BOOKING_CREATE, {"room_id": "0552-001"})["ok"] is False


def test_read_write_gating(tmp_path: Path) -> None:
    registry = make_reconciler(tmp_path).reconcile(
        [
            runtime_spec(ROOM_LIST, _EMPTY_SCHEMA),
            runtime_spec(BOOKING_CREATE, _EMPTY_SCHEMA),
        ]
    )
    assert registry.can_execute_read(ROOM_LIST) is True
    assert registry.can_execute_write(BOOKING_CREATE) is True
    assert registry.can_execute_write(ROOM_LIST) is False


def test_unmapped_write_denied(tmp_path: Path) -> None:
    # 运行时独有（静态未知）的工具不允许写操作。
    registry = make_reconciler(tmp_path).reconcile(
        [
            runtime_spec(ROOM_LIST, _EMPTY_SCHEMA),
            runtime_spec("user.get_workspace", _EMPTY_SCHEMA),
        ]
    )
    assert registry.can_execute_read("user.get_workspace") is True
    assert registry.can_execute_write("user.get_workspace") is False
