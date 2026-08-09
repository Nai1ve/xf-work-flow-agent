"""pytest 共享配置与 fixture。

- 把 ``submission/`` 加入 sys.path：utils 包内模块互相使用绝对导入
  （``from utils.logger import ...``），因此必须把 submission/ 挂到 sys.path
  前方，与官方 runner 的做法（把 agent 所在目录加进 sys.path）保持一致。
- ``write_minimal_static_context``：写一个最小的静态上下文目录（2 工具 / 2 房间），
  供 store 与对账相关测试复用，不依赖 gitignored 的 contest 数据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "submission"))

# 最小静态上下文 fixture：2 个工具（1 读 1 写）+ 2 个会议室（含 officeId 映射）。
_MINIMAL_TOOLS_INDEX = {
    "schema_version": "static-context-v1",
    "counts": {"tools": 2, "write_tools": 1},
    "write_tools": ["meetingroom.booking.create"],
    "by_name": {
        "meetingroom.room.list": {
            "name": "meetingroom.room.list",
            "description": "按日期和地点条件查询会议室候选",
            "args_schema": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "format": "date"},
                    "capacity_gte": {"type": "integer"},
                },
                "required": ["day"],
            },
        },
        "meetingroom.booking.create": {
            "name": "meetingroom.booking.create",
            "description": "创建会议室预订",
            "args_schema": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "format": "date"},
                    "office_id": {"type": "string"},
                    "room_id": {"type": "string"},
                    "start": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                    "end": {"type": "string", "pattern": "^\\d{2}:\\d{2}$"},
                    "title": {"type": "string"},
                },
                "required": ["day", "office_id", "room_id", "start", "end", "title"],
            },
        },
    },
}

_MINIMAL_ROOMS_INDEX = {
    "schema_version": "static-context-v1",
    "counts": {"rooms": 2},
    "by_room_id": {
        "0552-001": {
            "officeId": "office-uuid-1",
            "name": "107洽谈室-XZ-A1南区",
            "capacity": 6,
            "campus": "小镇",
            "location": "讯飞小镇",
            "building": "A1",
            "bookable": True,
            "hasScreen": True,
            "features": [],
            "room_id": "0552-001",
        },
        "A1-1F-106": {
            "officeId": "office-uuid-2",
            "name": "106会议室",
            "capacity": 10,
            "campus": "合肥",
            "location": "合肥总部",
            "building": "",
            "bookable": True,
            "hasScreen": False,
            "features": [],
            "room_id": "A1-1F-106",
        },
    },
    "by_office_id": {"office-uuid-1": "0552-001", "office-uuid-2": "A1-1F-106"},
    "by_building": {"A1": ["0552-001"]},
    "by_campus": {"小镇": ["0552-001"], "合肥": ["A1-1F-106"]},
}


def write_minimal_static_context(base_dir: Path) -> Path:
    """把最小静态上下文写入 base_dir，返回 base_dir（供 store / 对账测试复用）。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "tools.index.json": _MINIMAL_TOOLS_INDEX,
        "meetingrooms.index.json": _MINIMAL_ROOMS_INDEX,
        "manifest.json": {
            "schema_version": "static-context-v1",
            "counts": {"tools": 2, "write_tools": 1, "rooms": 2},
            "sources": {},
            "split_hash_checks": [],
            "generated_at": "test",
        },
    }
    for filename, content in files.items():
        (base_dir / filename).write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return base_dir
