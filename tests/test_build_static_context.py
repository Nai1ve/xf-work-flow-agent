"""scripts/build_static_context.py 的单元测试。

覆盖：
- build_tools_index / build_meetingrooms_index 的索引结构；
- sha256_file / check_split_hash / build_manifest；
- 真实 contest/train 数据的端到端构建（数据缺失时自动跳过）。

测试优先使用内联 fixture；集成用例在 contest 数据就绪时运行。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from utils.static_context import StaticContextStore

ROOT = Path(__file__).resolve().parents[1]


def _load_build_module():
    """按文件名加载 scripts/build_static_context.py（避免与顶层包重名）。"""
    path = ROOT / "scripts" / "build_static_context.py"
    spec = importlib.util.spec_from_file_location("build_static_context_mod", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_static_context_mod"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUILD = _load_build_module()


def test_build_tools_index() -> None:
    tool_specs = {
        "meetingroom.room.list": {
            "name": "meetingroom.room.list",
            "description": "查询会议室候选",
            "args_schema": {"type": "object", "properties": {}, "required": ["day"]},
        },
        "meetingroom.booking.create": {
            "name": "meetingroom.booking.create",
            "description": "创建会议室预订",
            "args_schema": {"type": "object", "properties": {}},
        },
    }
    index = BUILD.build_tools_index(tool_specs)
    assert index["schema_version"] == "static-context-v1"
    assert index["counts"]["tools"] == 2
    assert index["counts"]["write_tools"] == len(BUILD.WRITE_TOOLS)
    assert index["write_tools"] == sorted(BUILD.WRITE_TOOLS)
    assert set(index["by_name"]) == {"meetingroom.room.list", "meetingroom.booking.create"}
    assert index["by_name"]["meetingroom.room.list"]["args_schema"]["required"] == ["day"]


def test_build_meetingrooms_index() -> None:
    meetingroom_data = {
        "rooms": {
            "0552-001": {"officeId": "u1", "building": "A1", "campus": "小镇", "capacity": 6},
            "A1-1F-106": {"officeId": "u2", "building": "", "campus": "合肥", "capacity": 10},
            "0551-001": {"officeId": "u3", "building": "A4", "campus": "合肥", "capacity": 9},
        }
    }
    index = BUILD.build_meetingrooms_index(meetingroom_data)
    assert index["counts"]["rooms"] == 3
    # 每条记录补入自描述 room_id。
    assert index["by_room_id"]["0552-001"]["room_id"] == "0552-001"
    # officeId 反向映射。
    assert index["by_office_id"]["u2"] == "A1-1F-106"
    # 空 building 的房间不进 by_building 二级索引。
    assert index["by_building"] == {"A1": ["0552-001"], "A4": ["0551-001"]}
    assert sorted(index["by_campus"]["合肥"]) == ["0551-001", "A1-1F-106"]


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"hello\n")
    expected = "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
    assert BUILD.sha256_file(path) == expected


def test_check_split_hash_same_and_diff(tmp_path: Path) -> None:
    train = tmp_path / "train.json"
    val = tmp_path / "val.json"
    train.write_text("same", encoding="utf-8")
    val.write_text("same", encoding="utf-8")
    assert BUILD.check_split_hash(train, val)["same"] is True
    val.write_text("different", encoding="utf-8")
    assert BUILD.check_split_hash(train, val)["same"] is False


def test_check_split_hash_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    present = tmp_path / "present.json"
    present.write_text("x", encoding="utf-8")
    result = BUILD.check_split_hash(missing, present)
    assert result["same"] is False
    assert result["train_missing"] is True
    assert result["val_missing"] is False


def test_build_manifest() -> None:
    manifest = BUILD.build_manifest(
        sources={"tool_specs": {"path": "a", "sha256": "abc"}},
        split_checks=[{"file": "b", "same": True}],
        counts={"tools": 2, "rooms": 3},
    )
    assert manifest["schema_version"] == "static-context-v1"
    assert manifest["counts"] == {"tools": 2, "rooms": 3}
    assert manifest["sources"]["tool_specs"]["sha256"] == "abc"
    assert manifest["split_hash_checks"][0]["same"] is True
    assert "generated_at" in manifest


@pytest.mark.skipif(
    not (ROOT / "contest" / "train" / "tool_specs.json").exists(),
    reason="contest/train 数据未就绪",
)
def test_build_real_data_then_load(tmp_path: Path) -> None:
    """端到端：真实 train 数据构建出的索引可被 StaticContextStore 正常加载。"""
    manifest = BUILD.build_static_context(
        ROOT / "contest" / "train",
        ROOT / "contest" / "val",
        tmp_path,
    )
    assert manifest["counts"]["tools"] == 22
    assert manifest["counts"]["rooms"] == 139
    assert manifest["split_hash_checks"][0]["same"] is True

    store = StaticContextStore(base_dir=tmp_path)
    assert store.counts()["tools"] == 22
    assert store.counts()["rooms"] == 139
    assert store.is_write("meetingroom.booking.create")
    assert not store.is_write("meetingroom.room.list")
