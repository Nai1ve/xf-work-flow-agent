"""工位楼栋级 room.list 探测的独立开关回归。"""

from __future__ import annotations

from typing import Any

from utils.executor import MeetingroomExecutor
from utils.profiles import ExecutionProfile, ProfileConfig
from utils.understanding import INTENT_BOOK, MeetingConstraints


class _Registry:
    def is_write(self, _name: str) -> bool:
        return False

    def can_execute_write(self, _name: str) -> bool:
        return True

    def validate_call(self, _name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "errors": []}


class _Env:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name != "meetingroom.room.list":
            return {}
        address = args.get("office_address")
        if address == "0551_A4_4F":
            return {
                "rooms": [
                    {
                        "room_id": "A4-4F-001",
                        "officeId": "room-office-001",
                        "building": "A4",
                        "floor": "4F",
                        "bookable": True,
                        "busy_slots": [],
                    }
                ]
            }
        # 楼栋级探测结果故意不同，验证它不会被混入显式楼层候选。
        return {
            "rooms": [
                {
                    "room_id": "A4-1F-001",
                    "officeId": "room-office-002",
                    "building": "A4",
                    "floor": "1F",
                    "bookable": True,
                    "busy_slots": [],
                }
            ]
        }


def _constraints() -> MeetingConstraints:
    return MeetingConstraints(
        intent=INTENT_BOOK,
        day="2026-04-21",
        start="14:00",
        end="15:00",
        addresses=["0551_A4_4F"],
        capacity_gte=10,
        workspace_hint=True,
    )


def test_workspace_building_probe_is_opt_in_and_observe_only() -> None:
    env = _Env()
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        meeting_workspace_building_probe_v3=True,
    )
    executor = MeetingroomExecutor(env, _Registry(), None, profile_config=config)

    candidates = executor._collect_available(
        "2026-04-21", _constraints(), ["0551_A4_4F"]
    )

    assert [args["office_address"] for name, args in env.calls if name == "meetingroom.room.list"] == [
        "0551_A4",
        "0551_A4_4F",
    ]
    assert [room["room_id"] for _address, room in candidates] == ["A4-4F-001"]


def test_workspace_building_probe_is_disabled_by_default() -> None:
    env = _Env()
    executor = MeetingroomExecutor(env, _Registry(), None)

    executor._collect_available("2026-04-21", _constraints(), ["0551_A4_4F"])

    assert [args["office_address"] for name, args in env.calls if name == "meetingroom.room.list"] == [
        "0551_A4_4F"
    ]


def test_workspace_building_probe_is_deduplicated_per_day_and_address() -> None:
    env = _Env()
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        meeting_workspace_building_probe_v3=True,
    )
    executor = MeetingroomExecutor(env, _Registry(), None, profile_config=config)
    constraints = _constraints()

    executor._collect_available(
        "2026-04-21", constraints, ["0551_A4_4F", "0551_A4_4F"]
    )

    addresses = [
        args["office_address"]
        for name, args in env.calls
        if name == "meetingroom.room.list"
    ]
    assert addresses.count("0551_A4") == 1
