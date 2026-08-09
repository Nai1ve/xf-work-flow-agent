"""StaticContextStore（submission/utils/static_context.py）的单元测试。

覆盖：
- 索引加载与各类查询（工具 spec / 写名单 / 会议室目录与二级索引）；
- 降级路径：enabled=False、目录缺失、索引文件缺失 —— 一律空 store 不崩溃。

测试全部使用 tests/conftest.py 的内联 fixture，不依赖 gitignored 的 contest 数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import write_minimal_static_context
from utils.static_context import SCHEMA_VERSION, StaticContextStore


@pytest.fixture
def store(tmp_path: Path) -> StaticContextStore:
    return StaticContextStore(base_dir=write_minimal_static_context(tmp_path))


def test_load_and_counts(store: StaticContextStore) -> None:
    assert store.counts() == {"tools": 2, "write_tools": 1, "rooms": 2}
    assert store.status()["schema_version"] == SCHEMA_VERSION
    assert store.status()["files_loaded"] == {
        "manifest": True,
        "tools_index": True,
        "meetingrooms_index": True,
    }


def test_tool_queries(store: StaticContextStore) -> None:
    spec = store.tool_spec("meetingroom.room.list")
    assert spec is not None
    assert spec["description"] == "按日期和地点条件查询会议室候选"
    assert store.tool_names() == {"meetingroom.room.list", "meetingroom.booking.create"}
    assert store.is_known_tool("meetingroom.booking.create")
    assert not store.is_known_tool("user.get_info")
    # 写名单只含 create，room.list 不是写类。
    assert store.is_write("meetingroom.booking.create")
    assert not store.is_write("meetingroom.room.list")


def test_room_queries(store: StaticContextStore) -> None:
    room = store.room("0552-001")
    assert room is not None
    assert room["capacity"] == 6
    assert room["room_id"] == "0552-001"  # build 阶段补入的自描述字段
    assert store.office_id_for_room("0552-001") == "office-uuid-1"
    assert store.room_id_for_office_id("office-uuid-2") == "A1-1F-106"
    assert store.rooms_by_building("A1") == ["0552-001"]
    assert sorted(store.rooms_by_campus("合肥")) == ["A1-1F-106"]
    assert store.room("unknown") is None
    assert store.room_id_for_office_id("unknown") is None
    # 空 building 的房间不进入 by_building 二级索引（先验无法按楼栋过滤）。
    assert "A1-1F-106" not in store.rooms_by_building("A1")


def test_disabled_returns_empty(tmp_path: Path) -> None:
    store = StaticContextStore(
        base_dir=write_minimal_static_context(tmp_path), enabled=False
    )
    assert store.counts() == {"tools": 0, "write_tools": 0, "rooms": 0}
    assert store.tool_names() == set()
    assert store.tool_spec("meetingroom.room.list") is None
    assert store.status()["enabled"] is False


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    store = StaticContextStore(base_dir=tmp_path / "no-such-dir")
    assert store.counts() == {"tools": 0, "write_tools": 0, "rooms": 0}
    assert store.room("0552-001") is None


def test_partial_files_no_crash(tmp_path: Path) -> None:
    # 只有 tools 索引，没有 rooms 索引：tools 正常加载，rooms 为空。
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "tools.index.json").write_text(
        '{"by_name": {"x": {}}, "write_tools": []}', encoding="utf-8"
    )
    store = StaticContextStore(base_dir=tmp_path)
    assert store.is_known_tool("x")
    assert store.counts()["rooms"] == 0
    assert store.room("0552-001") is None
