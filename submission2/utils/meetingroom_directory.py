"""会议室静态目录的轻量公开接口。

目录只保存 room/building/floor/campus/capacity/features 等可解释属性；可预订性、
冲突和权限必须在当前 case 通过 ``meetingroom.room.list`` 重新确认。该包装器让
数据层和 Skill 层使用同一份索引，避免各处自行解析 meetingroom_data。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .static_context import StaticContextStore


class MeetingroomDirectory:
    """基于编译索引的房间/位置查询目录。"""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.store = StaticContextStore(base_dir=base_dir, enabled=enabled)

    def room(self, room_id: str) -> dict[str, Any] | None:
        return self.store.room(room_id)

    def office_id(self, room_id: str) -> str | None:
        return self.store.office_id_for_room(room_id)

    def room_for_office_id(self, office_id: str) -> str | None:
        return self.store.room_id_for_office_id(office_id)

    def rooms_by_building(self, building: str) -> list[str]:
        return self.store.rooms_by_building(building)

    def rooms_by_campus(self, campus: str) -> list[str]:
        return self.store.rooms_by_campus(campus)

    def candidates(
        self,
        *,
        campus: str | None = None,
        building: str | None = None,
        floor: str | None = None,
        capacity: int | None = None,
        feature: str | None = None,
    ) -> list[dict[str, Any]]:
        """按静态属性筛选候选，仅用于排序/解释，不能直接作为可执行候选。"""
        if building:
            ids = self.rooms_by_building(building)
        elif campus:
            ids = self.rooms_by_campus(campus)
        else:
            ids = list((self.store._rooms or {}).keys())
        out: list[dict[str, Any]] = []
        for room_id in ids:
            room = self.room(room_id)
            if not room:
                continue
            if campus and room.get("campus") not in {campus, None}:
                continue
            if floor and room.get("floor") != floor:
                continue
            if capacity is not None and int(room.get("capacity", 0) or 0) < capacity:
                continue
            if feature:
                features = room.get("features") or room.get("feature") or []
                if isinstance(features, str):
                    features = [features]
                if feature not in features and not room.get(feature):
                    continue
            out.append(dict(room))
        return out


__all__ = ["MeetingroomDirectory"]
