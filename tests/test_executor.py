"""执行层单元测试：MeetingroomExecutor 的 S1 预订 / S2 查询最小闭环。

用可编程 FakeEnv 替换官方 env，验证执行层的关键决策（设计意图在
submission/utils/executor.py 模块 docstring）：
- ``_collect_available`` 第一个有可用房间的地址即停（省步数 + 主地址优先）；
- ``_search_combos`` 楼层无解时降级楼栋级（软约束楼层）；
- ``_create_booking_raw`` create 统一传房间 officeId UUID（轨迹与 gold 对齐）；
- final_answer 的 office_id 按 reference 房间级规则（楼栋式随机房→楼栋名、
  数字房/A3 合成房→officeId UUID）；
- 写门禁：未公开/运行时独有工具不调用；
- S2 查询只调只读工具、不调任何写工具。

测试不触达官方 env，也不依赖 gitignored 的 contest 数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.executor import MeetingroomExecutor
from utils.static_context import StaticContextStore
from utils.tool_contract import ToolContractReconciler
from utils.understanding import (
    INTENT_BOOK,
    INTENT_QUERY,
    QUERY_BOOKING_LIST,
    QUERY_SCHEDULE,
    QUERY_UNBOOKABLE,
    QUERY_WORKSPACE,
    MeetingConstraints,
)

from conftest import write_minimal_static_context

# 运行时 list_tools 返回的工具集（与最小静态上下文交叉，create 为静态已知写工具）。
_TOOL_NAMES = (
    "meetingroom.room.list",
    "meetingroom.booking.create",
    "meetingroom.booking.list",
    "meetingroom.room.schedule",
    "user.get_workspace",
)


def _room(
    room_id: str,
    *,
    building: str = "",
    campus: str = "0552",
    floor: str = "",
    office_id: str = "uuid-" + "x",
    capacity: int = 10,
    bookable: bool = True,
    busy_slots: list | None = None,
    area: str | None = None,
) -> dict:
    """构造一条 room.list 返回的房间记录（字段与官方 tool_specs 对齐）。"""
    return {
        "room_id": room_id,
        "name": room_id,
        "building": building,
        "campus": campus,
        "floor": floor,
        "officeId": office_id,
        "capacity": capacity,
        "hasScreen": True,
        "bookable": bookable,
        "busy_slots": busy_slots or [],
        "area": area,
    }


class FakeEnv:
    """可编程假 env：room.list 按 office_address 返回预设房间，写调用被记录。"""

    def __init__(
        self,
        rooms_by_address: dict[str, list[dict]] | None = None,
        workspace_address: str = "0552_A1_3F",
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rooms_by_address = rooms_by_address or {}
        self.workspace_address = workspace_address
        self.create_success = True

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": "",
                "args_schema": {"type": "object", "properties": {}, "required": []},
            }
            for name in _TOOL_NAMES
        ]

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name == "meetingroom.room.list":
            return {"rooms": list(self.rooms_by_address.get(args.get("office_address"), []))}
        if name == "meetingroom.booking.create":
            if self.create_success:
                return {"success": True, "room_id": args.get("room_id")}
            return {"success": False, "error": "create failed"}
        if name == "user.get_workspace":
            return {"office_address": self.workspace_address}
        if name == "meetingroom.booking.list":
            return {"bookings": []}
        if name == "meetingroom.room.schedule":
            return {"schedule": []}
        return {"error": f"unhandled: {name}"}


@pytest.fixture
def static_store(tmp_path: Path) -> StaticContextStore:
    """最小静态上下文 store（2 工具 / 2 房间，见 conftest）。"""
    base = write_minimal_static_context(tmp_path)
    return StaticContextStore(base_dir=base, enabled=True)


@pytest.fixture
def registry(static_store: StaticContextStore) -> ToolContractReconciler:
    """对账后的有效注册表（reconcile 内部返回 EffectiveToolRegistry）。"""
    env = FakeEnv()
    return ToolContractReconciler(static_store).reconcile(env.list_tools())


def _executor(env: FakeEnv, registry) -> MeetingroomExecutor:
    return MeetingroomExecutor(env, registry, None)


# ---------------------------------------------------------------------- 预订 --


class TestBookSingleDay:
    """S1 单日预订闭环。"""

    def test_books_room_and_reports_office_id(self, registry) -> None:
        """A1 园区单候选：room.list → booking.create，office_id 用房间 officeId。"""
        room = _room("A1-3F-349", building="A1", campus="0552", office_id="uuid-a1")
        env = FakeEnv(rooms_by_address={"0552_A1": [room]})
        executor = _executor(env, registry)

        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-04-28",
            start="14:00",
            end="16:00",
            addresses=["0552_A1"],
            capacity_gte=10,
            title="项目复盘",
        )
        answer = executor.execute(INTENT_BOOK, c)

        tools = [name for name, _ in env.calls]
        assert tools == [
            "meetingroom.room.list",
            "meetingroom.booking.create",
        ]
        create_args = env.calls[-1][1]
        assert create_args["day"] == "2026-04-28"
        assert create_args["room_id"] == "A1-3F-349"
        assert create_args["start"] == "14:00"
        assert create_args["end"] == "16:00"
        assert create_args["title"] == "项目复盘"
        # 楼栋式房间 + 非合成 officeId → final_answer 上报楼栋名（0020/0023 同型）。
        assert answer["booking_result"]["office_id"] == "A1"
        assert answer["booking_result"]["status"] == "success"

    def test_book_defaults_to_1400_slot_when_time_missing(self, registry) -> None:
        """跨域 Fix B（zh_0007/0008/0010）：day 有时、start/end 缺失 → 默认规范槽位
        14:00-15:00（gold 无时间订会议室一律 14:00-15:00），不再「缺 day/start/end 无法预订」。"""
        room = _room("A1-3F-349", building="A1", campus="0552", office_id="uuid-a1")
        env = FakeEnv(rooms_by_address={"0552_A1": [room]})
        executor = _executor(env, registry)

        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-05-13",
            addresses=["0552_A1"],
            title="会议",
        )
        answer = executor.execute(INTENT_BOOK, c)

        create_args = env.calls[-1][1]
        assert create_args["start"] == "14:00"
        assert create_args["end"] == "15:00"
        assert answer["booking_result"]["status"] == "success"

    def test_book_refuses_when_day_missing(self, registry) -> None:
        """day 也缺失（无日期可定位）→ 仍放弃预订（不全量兜底）。"""
        env = FakeEnv(rooms_by_address={"0552_A1": [_room("A1-3F-349", building="A1", campus="0552")]})
        executor = _executor(env, registry)

        c = MeetingConstraints(
            intent=INTENT_BOOK,
            start="14:00",
            end="15:00",
            addresses=["0552_A1"],
            title="会议",
        )
        answer = executor.execute(INTENT_BOOK, c)
        assert "booking_result" not in answer or not answer.get("booking_result")

    def test_break_at_first_available_address(self, registry) -> None:
        """第一个有可用房间的地址即停：A1_1F 全忙、A2_1F 有房 → 只查 A1/A2。"""
        busy = _room("A1-1F-101", building="A1", floor="1F", office_id="uuid-a1", busy_slots=[["09:00", "18:00"]])
        free = _room("A2-1F-147", building="A2", floor="1F", office_id="uuid-a2")
        env = FakeEnv(
            rooms_by_address={
                "0552_A1_1F": [busy],
                "0552_A2_1F": [free],
                "0552_A3_1F": [_room("A3-1F-301", building="A3", floor="1F", office_id="uuid-a3")],
            }
        )
        executor = _executor(env, registry)

        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-05-09",
            start="10:00",
            end="12:00",
            addresses=["0552_A1_1F", "0552_A2_1F", "0552_A3_1F"],
            capacity_gte=10,
            has_screen=True,
            title="季度总结",
        )
        answer = executor.execute(INTENT_BOOK, c)

        listed = [
            args.get("office_address") for name, args in env.calls if name == "meetingroom.room.list"
        ]
        # A1 忙 → 继续 A2；A2 有房 → 停，不再枚举 A3。
        assert listed == ["0552_A1_1F", "0552_A2_1F"]
        assert answer["booking_result"]["room_id"] == "A2-1F-147"

    def test_busy_room_skipped(self, registry) -> None:
        """候选里混入被占房间：busy_slots 与目标时段重叠者被过滤。"""
        room = _room(
            "0552-007",
            building="A1",
            office_id="uuid-007",
            busy_slots=[["14:00", "16:00"]],  # 与 14:00-17:00 重叠 → 不可订
        )
        env = FakeEnv(rooms_by_address={"0552_A1": [room]})
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-05-06",
            start="14:00",
            end="17:00",
            addresses=["0552_A1"],
            title="培训",
        )
        answer = executor.execute(INTENT_BOOK, c)
        # 目标时段全部被占 → blocked（对齐金标 0021/0022 语义，不再是空 {}）。
        assert answer["booking_result"]["status"] == "blocked"
        assert answer["booking_result"]["reason"] == "no_bookable_room"

    def test_no_room_anywhere_returns_blocked(self, registry) -> None:
        """所有候选组合都无可订房间 → booking_result.status=blocked。"""
        busy = _room("A1-4F-401", building="A1", floor="4F", office_id="uuid-a1",
                     busy_slots=[["14:00", "15:00"]])
        env = FakeEnv(
            rooms_by_address={
                "0552_A1_4F": [busy],
                "0552_A1": [busy],  # 楼栋级降级也无解
            }
        )
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-04-21",
            start="14:00",
            end="15:00",
            addresses=["0552_A1_4F"],
            capacity_gte=6,
            title="项目复盘",
        )
        answer = executor.execute(INTENT_BOOK, c)
        assert answer["booking_result"]["status"] == "blocked"
        assert answer["booking_result"]["reason"] == "no_bookable_room"
        # 未创建任何预订。
        assert not any(name == "meetingroom.booking.create" for name, _ in env.calls)

    def test_floorless_fallback_combo(self, registry) -> None:
        """楼层无解时 _search_combos 追加楼栋级候选（软约束楼层）。"""
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-05-14",
            start="15:00",
            end="17:00",
            addresses=["0552_A1_3F"],
            capacity_gte=10,
            title="头脑风暴",
        )
        combos = executor._search_combos(c)
        # 主地址(楼层级)在前，楼栋级(去楼层)紧随其后，不早于反园区。
        assert combos[0][0] == ["0552_A1_3F"]
        assert ["0552_A1"] in [combo[0] for combo in combos]

    def test_workspace_hint_create_uses_office_id(self, registry) -> None:
        """S1w：create 的 office_id 传房间 officeId（官方最近工位检查要求全等）。"""
        # A3 楼真实房间 officeId 均为派生合成 UUID（含连续 0 串）→ final_answer 上报 UUID。
        room = _room("A3-4F-402", building="A3", campus="0552", floor="4F", office_id="a34f4020000000000000000000000001")
        env = FakeEnv(rooms_by_address={"0552_A3": [room]}, workspace_address="0552_A3_4F")
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-05-18",
            start="15:00",
            end="17:00",
            addresses=["0552_A3"],
            capacity_gte=8,
            has_screen=True,
            title="需求讨论",
            workspace_hint=True,
        )
        answer = executor.execute(INTENT_BOOK, c)
        create_args = env.calls[-1][1]
        # 轨迹 create 统一传房间 officeId UUID（与 gold create 对齐）。
        assert create_args.get("office_id") == "a34f4020000000000000000000000001"
        # A3 合成 officeId → final_answer 上报 UUID（0227/0250 同型）。
        assert answer["booking_result"]["office_id"] == "a34f4020000000000000000000000001"

    def test_write_gate_blocks_unmapped_write(self, static_store, registry) -> None:
        """写门禁：把 create 从运行时移除后，预订被拦截（防 forbidden 三道闸之一）。"""
        room = _room("A1-3F-349", building="A1", office_id="uuid-a1")
        env = FakeEnv(rooms_by_address={"0552_A1": [room]})
        # 运行时 list_tools 不含 booking.create → create 成为「静态存在但未公开」。
        runtime_tools = [t for t in env.list_tools() if t["name"] != "meetingroom.booking.create"]
        reg = ToolContractReconciler(static_store).reconcile(runtime_tools)
        executor = MeetingroomExecutor(env, reg, None)

        c = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-04-28",
            start="14:00",
            end="16:00",
            addresses=["0552_A1"],
            title="复盘",
        )
        answer = executor.execute(INTENT_BOOK, c)
        # create 被写门禁拦截 → 不调用 → 返回空。
        assert not any(name == "meetingroom.booking.create" for name, _ in env.calls)
        assert answer == {}


class TestSearchCombos:
    """_search_combos 的候选组合构造。"""

    def test_campus_fallback_only_when_implicit(self, registry) -> None:
        """园区隐式时追加反园区候选；显式指定则不加。"""
        env = FakeEnv()
        executor = _executor(env, registry)
        implicit = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-04-21",
            start="14:00",
            end="15:00",
            addresses=["0552_A3"],
            capacity_gte=15,
        )
        assert ["0551_A3"] in [c[0] for c in executor._search_combos(implicit)]

        explicit = MeetingConstraints(
            intent=INTENT_BOOK,
            day="2026-04-21",
            start="14:00",
            end="15:00",
            addresses=["0551_A3"],
            campus="0551",
            campus_explicit=True,
            capacity_gte=15,
        )
        combos = executor._search_combos(explicit)
        assert all("0552" not in addr for combo in combos for addr in combo[0])


class TestCollectAvailable:
    """_collect_available 的可用性过滤。"""

    def test_filters_busy_and_not_bookable(self, registry) -> None:
        env = FakeEnv(
            rooms_by_address={
                "0552_A1": [
                    _room("R-busy", building="A1", busy_slots=[["14:00", "16:00"]]),
                    _room("R-no-book", building="A1", bookable=False),
                    _room("R-free", building="A1"),
                ]
            }
        )
        executor = _executor(env, registry)
        c = MeetingConstraints(day="2026-04-28", start="14:00", end="16:00", addresses=["0552_A1"])
        available = executor._collect_available("2026-04-28", c, ["0552_A1"])
        ids = [room["room_id"] for _, room in available]
        assert ids == ["R-free"]


class TestParseOfficeAddress:
    """工位/地址解析：_parse_office_address → (building, campus, floor)。"""

    def test_full_address(self) -> None:
        executor = _executor(FakeEnv(), None)
        assert executor._parse_office_address("0552_A1_3F") == ("A1", "0552", "3F")

    def test_building_only(self) -> None:
        executor = _executor(FakeEnv(), None)
        assert executor._parse_office_address("0551_A4") == ("A4", "0551", None)

    def test_empty(self) -> None:
        executor = _executor(FakeEnv(), None)
        assert executor._parse_office_address("") == (None, None, None)


# ---------------------------------------------------------------------- 查询 --


class TestQuery:
    """S2 纯查询：只调只读工具，不调写工具。"""

    def test_query_workspace(self, registry) -> None:
        env = FakeEnv(workspace_address="0552_A1_3F")
        executor = _executor(env, registry)
        c = MeetingConstraints(intent=INTENT_QUERY, query_type=QUERY_WORKSPACE)
        answer = executor.execute(INTENT_QUERY, c)
        assert answer["booking_result"]["status"] == "queried"
        assert answer["booking_result"]["office_address"] == "0552_A1_3F"
        assert env.calls[0][0] == "user.get_workspace"

    def test_query_unbookable(self, registry) -> None:
        env = FakeEnv(rooms_by_address={"0552": [_room("R1", bookable=False)]})
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_QUERY,
            query_type=QUERY_UNBOOKABLE,
            day="2026-04-21",
            addresses=["0552"],
        )
        answer = executor.execute(INTENT_QUERY, c)
        assert answer["booking_result"]["count"] == 1
        assert answer["booking_result"]["bookable"] is False

    def test_query_schedule(self, registry) -> None:
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_QUERY,
            query_type=QUERY_SCHEDULE,
            schedule_room_id="A1-3F-349",
            schedule_start_date="2026-04-20",
            schedule_end_date="2026-04-26",
        )
        answer = executor.execute(INTENT_QUERY, c)
        assert answer["booking_result"]["status"] == "queried"
        assert answer["booking_result"]["room_id"] == "A1-3F-349"

    def test_query_booking_list(self, registry) -> None:
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_QUERY,
            query_type=QUERY_BOOKING_LIST,
            day="2026-04-21",
        )
        answer = executor.execute(INTENT_QUERY, c)
        assert answer["booking_result"]["status"] == "queried"
        assert answer["booking_result"]["day"] == "2026-04-21"
        # 查询全程不调写工具。
        assert all(name != "meetingroom.booking.create" for name, _ in env.calls)


class TestBookOneOfDays:
    """S1d 变体：多日都要空闲但只订其中一天（0223「找到后订周三的」）。"""

    def test_books_only_on_book_only_day(self, registry) -> None:
        """room.list 覆盖两天，create 只落在 book_only_day 一天。"""
        room = _room("A1-3F-101", building="A1", floor="3F", office_id="uuid-a1")
        env = FakeEnv(
            rooms_by_address={
                "0552_A1_3F": [room],  # 两天共用同一组房间
            }
        )
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_BOOK,
            days=["2026-05-13", "2026-05-14"],
            book_only_day="2026-05-13",
            start="14:00",
            end="16:00",
            addresses=["0552_A1_3F"],
            capacity_gte=10,
            has_screen=True,
            title="跨天评审",
        )
        answer = executor.execute(INTENT_BOOK, c)

        listed = [args.get("day") for name, args in env.calls if name == "meetingroom.room.list"]
        creates = [args for name, args in env.calls if name == "meetingroom.booking.create"]
        # 两天都查了 room.list（must_satisfy 要求 day=13 和 day=14 都 list 过）。
        assert listed == ["2026-05-13", "2026-05-14"]
        # 但只订一天（否则「最终仅新增一条活跃会议预订」被违反）。
        assert len(creates) == 1
        assert creates[0]["day"] == "2026-05-13"
        result = answer["booking_result"]
        assert result["status"] == "success"
        assert result["day"] == "2026-05-13"
        assert result["room_id"] == "A1-3F-101"
        assert result["office_id"] == "A1"  # 楼栋式 + 非合成 officeId → 楼栋名


class TestQueryBookingListKeyword:
    """S2 booking.list 关键词：0242「关键词是项目启动」。"""

    def test_passes_keyword_and_echoes(self, registry) -> None:
        """keyword 传入 booking.list，并在 booking_result 中回显。"""
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_QUERY,
            query_type=QUERY_BOOKING_LIST,
            day="2026-05-18",
            query_keyword="项目启动",
        )
        answer = executor.execute(INTENT_QUERY, c)

        call = next(args for name, args in env.calls if name == "meetingroom.booking.list")
        assert call["day"] == "2026-05-18"
        assert call["keyword"] == "项目启动"
        assert answer["booking_result"]["status"] == "queried"
        assert answer["booking_result"]["keyword"] == "项目启动"

    def test_no_keyword_no_arg(self, registry) -> None:
        """未给关键词时不传 keyword，也不回显。"""
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(
            intent=INTENT_QUERY,
            query_type=QUERY_BOOKING_LIST,
            day="2026-05-18",
        )
        answer = executor.execute(INTENT_QUERY, c)
        call = next(args for name, args in env.calls if name == "meetingroom.booking.list")
        assert "keyword" not in call
        assert "keyword" not in answer["booking_result"]


class TestExecuteDispatch:
    """execute 的分派：非 S1/S2 意图返回空 {}（0 分防线）。"""

    def test_unknown_intent_returns_empty(self, registry) -> None:
        env = FakeEnv()
        executor = _executor(env, registry)
        c = MeetingConstraints(intent="cancel")
        assert executor.execute("cancel", c) == {}
        assert env.calls == []


# =====================================================================
# 会议 op handlers（execute_ops）—— P1：cancel / extend / rebook /
# participant_* / compare_book / decide，标识符一律来自工具证据。
# =====================================================================

from types import SimpleNamespace  # noqa: E402

_OP_WRITE_TOOLS = (
    "meetingroom.booking.create",
    "meetingroom.booking.cancel",
    "meetingroom.booking.extend",
    "meetingroom.booking.participant.add",
    "meetingroom.booking.participant.remove",
)
_OP_READ_TOOLS = (
    "meetingroom.room.list",
    "meetingroom.booking.list",
    "meetingroom.room.schedule",
    "user.get_workspace",
    "user.get_info",
    "meetingroom.booking.participant.list",
)
_OP_TOOL_NAMES = _OP_WRITE_TOOLS + _OP_READ_TOOLS


def _op_static_context(base_dir: Path) -> Path:
    """更全的静态上下文：5 写工具 + 3 房间（原会议 room 含容量/楼栋，供 rebook）。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    tools = {
        "schema_version": "static-context-v1",
        "counts": {"tools": len(_OP_TOOL_NAMES), "write_tools": len(_OP_WRITE_TOOLS)},
        "write_tools": list(_OP_WRITE_TOOLS),
        "by_name": {
            name: {
                "name": name,
                "description": "",
                "args_schema": {"type": "object", "properties": {}, "required": []},
            }
            for name in _OP_TOOL_NAMES
        },
    }
    rooms = {
        "schema_version": "static-context-v1",
        "counts": {"rooms": 3},
        "by_room_id": {
            "A1-3F-305": {
                "room_id": "A1-3F-305",
                "officeId": "office-305",
                "name": "原会议房",
                "capacity": 8,
                "campus": "小镇",
                "building": "A1",
                "bookable": True,
                "hasScreen": True,
                "features": [],
            },
            "0552-001": {
                "room_id": "0552-001",
                "officeId": "office-uuid-1",
                "name": "107洽谈室-XZ-A1南区",
                "capacity": 6,
                "campus": "小镇",
                "building": "A1",
                "bookable": True,
                "hasScreen": True,
                "features": [],
            },
            "A1-1F-106": {
                "room_id": "A1-1F-106",
                "officeId": "office-uuid-2",
                "name": "106会议室",
                "capacity": 10,
                "campus": "合肥",
                "building": "",
                "bookable": True,
                "hasScreen": False,
                "features": [],
            },
        },
        "by_office_id": {"office-305": "A1-3F-305", "office-uuid-1": "0552-001", "office-uuid-2": "A1-1F-106"},
        "by_building": {"A1": ["A1-3F-305", "0552-001"]},
        "by_campus": {"小镇": ["A1-3F-305", "0552-001"], "合肥": ["A1-1F-106"]},
    }
    for filename, content in (
        ("tools.index.json", tools),
        ("meetingrooms.index.json", rooms),
        ("manifest.json", {"schema_version": "static-context-v1"}),
    ):
        (base_dir / filename).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return base_dir


@pytest.fixture
def op_store(tmp_path: Path) -> StaticContextStore:
    return StaticContextStore(base_dir=_op_static_context(tmp_path), enabled=True)


@pytest.fixture
def op_registry(op_store: StaticContextStore) -> ToolContractReconciler:
    env = OpFakeEnv()
    return ToolContractReconciler(op_store).reconcile(env.list_tools())


class StepLimitExceeded(Exception):
    """模拟官方 env 的 StepLimitExceeded（类名必须一致，执行层按 __name__ 判定）。"""


class OpFakeEnv:
    """execute_ops 专用假 env：booking/cancel/extend/participant/get_info/schedule 可配置。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.bookings: list[dict] = []          # booking.list 返回
        self.users: dict[str, str] = {}          # name → user_id（get_info 反查）
        self.rooms_by_address: dict[str, list[dict]] = {}
        self.schedule: dict[str, dict] = {}      # room_id → {bookings,busy_slots}
        self.participants: dict[str, list[dict]] = {}  # order_id → [ {user_id} ]
        self.workspace_uid = "119063"
        self.cancel_error: str | None = None
        self.extend_error: str | None = None
        self.create_success = True
        # 步数上限（模拟官方 step_budget）：已执行到该次数后，下一次 call_tool 抛
        # _StepLimitExceeded（与真实 env 在 step_count>=budget 时于调用入口 raise 一致）。
        self.step_limit_after: int | None = None

    def list_tools(self) -> list[dict]:
        return [
            {"name": name, "description": "", "args_schema": {"type": "object", "properties": {}, "required": []}}
            for name in _OP_TOOL_NAMES
        ]

    def call_tool(self, name: str, args: dict) -> dict:
        if self.step_limit_after is not None and len(self.calls) >= self.step_limit_after:
            raise StepLimitExceeded(f"Step budget {self.step_limit_after} exceeded.")
        self.calls.append((name, args))
        if name == "meetingroom.room.list":
            return {"rooms": list(self.rooms_by_address.get(args.get("office_address"), []))}
        if name == "meetingroom.booking.list":
            # 真实 env 的 booking.list 按 day/keyword 过滤（title 包含匹配）。
            items = list(self.bookings)
            if args.get("day"):
                items = [b for b in items if b.get("day") == args["day"]]
            if args.get("keyword"):
                items = [b for b in items if args["keyword"] in (b.get("title") or "")]
            return {"bookings": items}
        if name == "meetingroom.booking.create":
            if not self.create_success:
                return {"success": False, "error": "create failed"}
            return {
                "success": True,
                "room_id": args.get("room_id"),
                "booking_id": f"BK-{args.get('room_id')}-{str(args.get('start')).replace(':', '')}",
            }
        if name == "meetingroom.booking.cancel":
            if self.cancel_error:
                return {"error": self.cancel_error}
            return {"cancelled": True, "order_id": args.get("order_id")}
        if name == "meetingroom.booking.extend":
            if self.extend_error:
                return {"error": self.extend_error, "conflict": True}
            return {"success": True, "order_id": args.get("order_id"), "end": "15:30"}
        if name == "meetingroom.room.schedule":
            return self.schedule.get(args.get("room_id"), {"bookings": [], "busy_slots": []})
        if name == "user.get_workspace":
            return {"user_id": self.workspace_uid, "office_address": "0552_A1_3F"}
        if name == "user.get_info":
            uid = self.users.get(args.get("keyword"))
            return {"users": [{"user_id": uid}] if uid else []}
        if name == "meetingroom.booking.participant.list":
            return {"participants": list(self.participants.get(args.get("order_id"), []))}
        if name == "meetingroom.booking.participant.add":
            return {"success": True, "order_id": args.get("order_id"), "user_id": args.get("user_id")}
        if name == "meetingroom.booking.participant.remove":
            return {"success": True, "order_id": args.get("order_id"), "user_id": args.get("user_id")}
        return {"error": f"unhandled: {name}"}


def _op_executor(env: OpFakeEnv, registry, op_store) -> MeetingroomExecutor:
    return MeetingroomExecutor(env, registry, op_store)


def _plan(*ops: tuple[str, dict]) -> SimpleNamespace:
    """构造 MeetingOpPlan 同构对象：[(action, target), ...]。"""
    return SimpleNamespace(
        ops=[SimpleNamespace(action=a, target=t) for a, t in ops]
    )


def _booking(order_id: str, *, day: str = "2026-04-21", start: str = "14:00",
             end: str = "15:00", title: str = "项目复盘", room_id: str = "A1-3F-305",
             organizer: str = "119063", status: str = "active") -> dict:
    return {
        "order_id": order_id,
        "booking_id": order_id,
        "room_id": room_id,
        "day": day,
        "start": start,
        "end": end,
        "title": title,
        "status": status,
        "organizer_user_id": organizer,
    }


class TestOpCancel:
    def test_direct_order_id(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("cancel", {"order_id": "SEED-CANCEL-001", "day": "2026-04-21"})))
        assert result["booking_result"]["status"] == "cancelled"
        assert result["booking_result"]["order_id"] == "SEED-CANCEL-001"
        call = next(args for n, args in env.calls if n == "meetingroom.booking.cancel")
        assert call["order_id"] == "SEED-CANCEL-001"

    def test_locate_by_keyword(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-CANCEL-001"), _booking("SEED-CANCEL-002", room_id="A1-3F-349", organizer="200101")]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("cancel", {"day": "2026-04-21", "keyword": "项目复盘"})))
        assert result["booking_result"]["status"] == "cancelled"
        assert result["booking_result"]["order_id"] == "SEED-CANCEL-001"
        # 定位先 booking.list，再 cancel 证据里的 order_id。
        list_call = next(args for n, args in env.calls if n == "meetingroom.booking.list")
        assert list_call["keyword"] == "项目复盘"

    def test_owner_filter_when_ambiguous(self, op_registry, op_store) -> None:
        """同名多订：get_workspace 取当前用户过滤（组织者优先），不误删他人会议。"""
        env = OpFakeEnv()
        env.bookings = [
            _booking("SEED-OWN-001", start="14:00", end="15:00", organizer="119063"),
            _booking("SEED-OTHER-001", start="14:00", end="15:00", organizer="200101"),
            _booking("SEED-OWN-002", start="16:00", end="17:00", organizer="119063"),
        ]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("cancel", {"day": "2026-04-21", "keyword": "项目复盘", "start": "14:00", "end": "15:00"})))
        assert result["booking_result"]["order_id"] == "SEED-OWN-001"

    def test_no_identifier_need_confirmation(self, op_registry, op_store) -> None:
        """无 order_id 无定位词 → 探路但不下手（0026 禁止取消）。"""
        env = OpFakeEnv()
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("cancel", {"day": "2026-04-21"})))
        assert result["booking_result"]["status"] == "blocked"
        assert result["booking_result"]["reason"] == "need_confirmation"
        assert not any(n == "meetingroom.booking.cancel" for n, _ in env.calls)

    def test_not_found_blocked(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-001", title="别的会")]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("cancel", {"day": "2026-04-21", "keyword": "不存在的会"})))
        assert result["booking_result"]["status"] == "blocked"
        assert result["booking_result"]["reason"] == "not_found"


class TestOpExtend:
    def test_direct_success(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("extend", {"order_id": "SEED-EXT-001", "minutes": 30})))
        assert result["booking_result"]["status"] == "extended"
        assert result["booking_result"]["order_id"] == "SEED-EXT-001"
        call = next(args for n, args in env.calls if n == "meetingroom.booking.extend")
        assert call["minutes"] == 30

    def test_conditional_conflict_keeps_original(self, op_registry, op_store) -> None:
        """条件性延长：冲突 → 不动原会议，blocked(conflict_after_requested_extension)。"""
        env = OpFakeEnv()
        env.extend_error = "Time conflict"
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("extend", {"order_id": "SEED-EXT-001", "minutes": 30, "conditional": True})))
        assert result["booking_result"]["status"] == "blocked"
        assert result["booking_result"]["reason"] == "conflict_after_requested_extension"
        assert result["booking_result"]["order_id"] == "SEED-EXT-001"

    def test_unconditional_conflict_failed(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.extend_error = "Time conflict"
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("extend", {"order_id": "SEED-EXT-001", "minutes": 30})))
        assert result["booking_result"]["status"] == "extend_failed"
        assert result["booking_result"]["reason"] == "time_conflict"

    def test_locate_then_extend(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-EXT-002", start="14:00", end="15:00")]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(("extend", {"day": "2026-04-21", "keyword": "项目复盘", "start": "14:00", "end": "15:00", "minutes": 30})))
        assert result["booking_result"]["status"] == "extended"
        assert result["booking_result"]["order_id"] == "SEED-EXT-002"

    def test_conditional_probe_conflict_no_extend_call(self, op_registry, op_store) -> None:
        """0050：条件延长，同房延长窗口与他人预订冲突 → 探测即 blocked，不真调 extend。"""
        env = OpFakeEnv()
        env.bookings = [
            _booking("SEED-EXT-003", room_id="A1-3F-305", start="14:00", end="15:00"),
            _booking("SEED-OTHER-001", room_id="A1-3F-305", start="15:00", end="15:30", organizer="200101", title="预置占用 1"),
        ]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "extend", {"day": "2026-04-21", "start": "14:00", "end": "15:00", "minutes": 30, "conditional": True},
        )))
        assert result["booking_result"]["status"] == "blocked"
        assert result["booking_result"]["reason"] == "conflict_after_requested_extension"
        assert result["booking_result"]["order_id"] == "SEED-EXT-003"
        # 关键：冲突时绝不调 extend（否则 conflict 违规 → AS=0）
        assert not any(n == "meetingroom.booking.extend" for n, _ in env.calls)
        # 探测只用 booking.list 证据
        assert any(n == "meetingroom.booking.list" for n, _ in env.calls)

    def test_conditional_probe_no_conflict_calls_extend(self, op_registry, op_store) -> None:
        """条件延长，延长窗口无冲突 → 探测通过后才真调 extend。"""
        env = OpFakeEnv()
        env.bookings = [
            _booking("SEED-EXT-004", room_id="A1-3F-305", start="14:00", end="15:00"),
            # 同房 15:30-16:00 才有人用，延长到 15:30 不冲突
            _booking("SEED-OTHER-002", room_id="A1-3F-305", start="15:30", end="16:00", organizer="200101"),
        ]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "extend", {"day": "2026-04-21", "start": "14:00", "end": "15:00", "minutes": 30, "conditional": True},
        )))
        assert result["booking_result"]["status"] == "extended"
        assert result["booking_result"]["order_id"] == "SEED-EXT-004"
        assert any(n == "meetingroom.booking.extend" for n, _ in env.calls)


class TestOpRebook:
    def test_locate_cancel_recreate(self, op_registry, op_store) -> None:
        """0011 换大：定位 → cancel 原单 → 按更大容量 room.list → create。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-REBOOK-001", title="季度复盘")]
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-349", building="A1", capacity=14)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "rebook",
            {"day": "2026-04-21", "keyword": "季度复盘", "start": "14:00", "end": "15:00",
             "addresses": ["0552_A1"], "capacity": 9, "title": "季度复盘"},
        )))
        assert result["booking_result"]["status"] == "success"
        assert result["booking_result"]["room_id"] == "A1-3F-349"
        names = [n for n, _ in env.calls]
        assert names.index("meetingroom.booking.cancel") < names.index("meetingroom.booking.create")
        cancel_args = next(args for n, args in env.calls if n == "meetingroom.booking.cancel")
        assert cancel_args["order_id"] == "SEED-REBOOK-001"
        room_list_args = next(args for n, args in env.calls if n == "meetingroom.room.list")
        assert room_list_args["capacity_gte"] == 9  # 容量过滤发生在 room.list

    def test_larger_bumps_capacity(self, op_registry, op_store) -> None:
        """larger：新容量 > 原会议静态容量（0011 原 8 人 → 至少 9）。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-REBOOK-002", room_id="A1-3F-305", title="季度复盘")]
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-349", building="A1", capacity=14)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "rebook",
            {"day": "2026-04-21", "keyword": "季度复盘", "start": "14:00", "end": "15:00",
             "addresses": ["0552_A1"], "larger": True},
        )))
        assert result["booking_result"]["status"] == "success"
        room_list_args = next(args for n, args in env.calls if n == "meetingroom.room.list")
        assert room_list_args["capacity_gte"] >= 9  # 原 8 人 + 1，落在 room.list 过滤

    def test_keeps_seed_title_over_query_phrase(self, op_registry, op_store) -> None:
        """zh_0020：query 复述「项目复盘会」但种子标题是「季度复盘」→ rebook 沿用种子标题。
        （train 8 个 rebook case reference 标题 100% = 种子标题。）"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-REBOOK-LARGER-001", room_id="A1-3F-305", title="季度复盘")]
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-349", building="A1", capacity=14)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "rebook",
            {"day": "2026-04-21", "keyword": "季度复盘", "start": "14:00", "end": "15:00",
             "addresses": ["0552_A1"], "capacity": 9, "title": "项目复盘会"},
        )))
        assert result["booking_result"]["status"] == "success"
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["title"] == "季度复盘"  # 种子标题，而非 query 短语


class TestOpParticipant:
    def test_add_resolves_user_id(self, op_registry, op_store) -> None:
        """participant.add：user.get_info 反查 user_id 而非姓名。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-PART-001")]
        env.users = {"李明": "200200"}
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "participant_add",
            {"day": "2026-04-21", "keyword": "项目复盘", "persons": [{"name": "李明"}]},
        )))
        assert result["participant_result"]["status"] == "added"
        assert result["participant_result"]["user_id"] == "200200"
        info_call = next(args for n, args in env.calls if n == "user.get_info")
        assert info_call["keyword"] == "李明"
        add_call = next(args for n, args in env.calls if n == "meetingroom.booking.participant.add")
        assert add_call["user_id"] == "200200"

    def test_add_employee_no_passthrough(self, op_registry, op_store) -> None:
        """工号直给（员工号即 user_id），不查 get_info。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-PART-002")]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "participant_add",
            {"day": "2026-04-21", "keyword": "项目复盘", "persons": [{"name": "李四", "employee_no": "10086"}]},
        )))
        assert result["participant_result"]["user_id"] == "10086"
        assert not any(n == "user.get_info" for n, _ in env.calls)

    def test_add_dedup_already_exists(self, op_registry, op_store) -> None:
        """0032 去重：已在参会人 → 不重复 add。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-PART-003")]
        env.users = {"李明": "200200"}
        env.participants["SEED-PART-003"] = [{"user_id": "200200"}]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "participant_add",
            {"day": "2026-04-21", "keyword": "项目复盘", "persons": [{"name": "李明"}], "dedup": True},
        )))
        assert result["participant_result"]["status"] == "already_exists"
        assert not any(n == "meetingroom.booking.participant.add" for n, _ in env.calls)

    def test_remove(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-PART-004")]
        env.users = {"王芳": "200201"}
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "participant_remove",
            {"day": "2026-04-21", "keyword": "项目复盘", "persons": [{"name": "王芳"}]},
        )))
        assert result["participant_result"]["status"] == "removed"
        assert result["participant_result"]["user_id"] == "200201"
        remove_call = next(args for n, args in env.calls if n == "meetingroom.booking.participant.remove")
        assert remove_call["user_id"] == "200201"

    def test_list(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-PART-005")]
        env.participants["SEED-PART-005"] = [{"user_id": "200200", "name": "李明"}]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "participant_list",
            {"day": "2026-04-21", "keyword": "项目复盘"},
        )))
        assert result["participants"] == [{"user_id": "200200", "name": "李明"}]


class TestOpCompareBook:
    def test_picks_less_busy_room(self, op_registry, op_store) -> None:
        """0250：两房整周日程 → 选更空闲（bookings 少）的那间订。"""
        env = OpFakeEnv()
        env.schedule["A3-3F-311"] = {"bookings": [{"day": "2026-04-21", "start": "09:00", "end": "10:00"}]}
        env.schedule["A3-3F-312"] = {"bookings": []}
        env.rooms_by_address["0552_A3"] = [_room("A3-3F-312", building="A3", capacity=12)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "compare_book",
            {"day": "2026-04-23", "start": "14:00", "end": "16:00", "title": "年终总结",
             "compare_rooms": ["A3-3F-311", "A3-3F-312"]},
        )))
        assert result["booking_result"]["status"] == "success"
        assert result["booking_result"]["room_id"] == "A3-3F-312"
        schedule_calls = [args for n, args in env.calls if n == "meetingroom.room.schedule"]
        # 对比阶段：两房各一次整周日程（周一起点）；随后选中的房间有一次订日校验。
        week_calls = [a for a in schedule_calls if a.get("start_date") != a.get("end_date")]
        assert {a["room_id"] for a in week_calls} == {"A3-3F-311", "A3-3F-312"}
        assert all(a["start_date"] == "2026-04-20" for a in week_calls)  # 整周起点（周一）


class TestOpDecide:
    def test_not_booked_books(self, op_registry, op_store) -> None:
        """0027：没订 → book。"""
        env = OpFakeEnv()
        env.rooms_by_address["0552_A1"] = [_room("0552-001", building="A1", capacity=6)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "decide",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "评审",
             "addresses": ["0552_A1"], "minutes": 30},
        )))
        assert result["booking_result"]["status"] == "success"
        assert any(n == "meetingroom.booking.create" for n, _ in env.calls)

    def test_booked_extend_conflict_rebook(self, op_registry, op_store) -> None:
        """0027：已订 + 延长冲突 → cancel 原单 + 重订（结束时刻 = 原结束 + 延长分钟）。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-0027-001", start="14:00", end="15:00")]
        env.extend_error = "Time conflict"
        env.rooms_by_address["0552_A1"] = [_room("0552-001", building="A1", capacity=6)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "decide",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "评审",
             "addresses": ["0552_A1"], "minutes": 30},
        )))
        assert result["booking_result"]["status"] == "success"
        names = [n for n, _ in env.calls]
        assert names.index("meetingroom.booking.cancel") < names.index("meetingroom.booking.create")
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["end"] == "15:30"  # 原结束 + 30 分钟

    def test_rebook_keeps_original_title(self, op_registry, op_store) -> None:
        """0026：decide 冲突重订沿用原会议标题，不用 query 措辞（「项目复盘」≠「项目复盘会议室」）。"""
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-0026-001", start="14:00", end="15:00", title="项目复盘")]
        env.extend_error = "Time conflict"
        env.rooms_by_address["0552_A1"] = [_room("0552-001", building="A1", capacity=6)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "decide",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "项目复盘会议室",
             "addresses": ["0552_A1"], "minutes": 30},
        )))
        assert result["booking_result"]["status"] == "success"
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["title"] == "项目复盘"  # seed 原订标题，而非 query 措辞

    def test_probe_conflict_skips_extend_call(self, op_registry, op_store) -> None:
        """0026：探测命中 seed 预置占用冲突 → 不真调 extend（避免 error 入史拉低 AS），直接取消重订。"""
        env = OpFakeEnv()
        env.bookings = [
            _booking("SEED-0026-001", start="14:00", end="15:00", title="项目复盘", room_id="0552-001"),
            _booking("SEED-0026-OCC", room_id="0552-001", start="15:00", end="15:30", title="预置占用", organizer="200101"),
        ]
        env.rooms_by_address["0552_A1"] = [_room("0552-001", building="A1", capacity=6)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "decide",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "项目复盘会议室",
             "addresses": ["0552_A1"], "minutes": 30},
        )))
        names = [n for n, _ in env.calls]
        assert "meetingroom.booking.extend" not in names  # 探测即冲突，不真调 extend
        assert result["booking_result"]["status"] == "success"
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["end"] == "15:30"
        assert create_args["title"] == "项目复盘"


class TestOpEarliest:
    """earliest op 的周区间缺省处理（mr_0012：LLM 偶发只给语义不产 week_start/week_end）。"""

    def test_earliest_without_week_range_books_day(self, op_registry, op_store) -> None:
        """缺 week_start/week_end → 退化为单日订 c.day，不崩（修复前 date.fromisoformat("") 崩）。"""
        env = OpFakeEnv()
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-305", building="A1", capacity=10)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "earliest",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "评审",
             "addresses": ["0552_A1"]},
        )))
        assert result["booking_result"]["status"] == "success"
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["day"] == "2026-04-21"

    def test_earliest_with_week_range_sequential(self, op_registry, op_store) -> None:
        """有 week_start/week_end → 逐天 room.list 找最早可订（周四 first → 订周四）。"""
        env = OpFakeEnv()
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-305", building="A1", capacity=10)]
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan((
            "earliest",
            {"week_start": "2026-04-20", "week_end": "2026-04-26", "start": "14:00",
             "end": "15:00", "title": "评审", "addresses": ["0552_A1"]},
        )))
        assert result["booking_result"]["status"] == "success"
        list_days = [args["day"] for n, args in env.calls if n == "meetingroom.room.list"]
        assert "2026-04-20" in list_days  # 周一起逐天搜
        create_args = next(args for n, args in env.calls if n == "meetingroom.booking.create")
        assert create_args["day"] == "2026-04-20"  # 首个可订日

    def test_capacity_str_normalized_to_int(self, op_registry, op_store) -> None:
        """LLM 把 capacity 输出成字符串（"10"）→ 归一为 int，room.list 校验不再拦截（mr_0216）。"""
        env = OpFakeEnv()
        env.rooms_by_address["0552_A1"] = [_room("A1-3F-305", building="A1", capacity=10)]
        executor = _op_executor(env, op_registry, op_store)
        executor.execute_ops(_plan((
            "book",
            {"day": "2026-04-21", "start": "14:00", "end": "15:00", "title": "评审",
             "addresses": ["0552_A1"], "capacity": "10"},
        )))
        list_args = next(args for n, args in env.calls if n == "meetingroom.room.list")
        assert list_args["capacity_gte"] == 10
        assert isinstance(list_args["capacity_gte"], int)


class TestOpStepLimit:
    """zh_0026：decide 重订已成功但剩余 op 触发步数上限 → 保留已完成成果，不整锅丢。"""

    def test_preserves_completed_rebook(self, op_registry, op_store) -> None:
        env = OpFakeEnv()
        env.bookings = [_booking("SEED-0027-001", start="14:00", end="15:00")]
        env.extend_error = "Time conflict"
        env.rooms_by_address["0552_A1"] = [_room("0552-001", building="A1", capacity=6)]
        # decide 恰好用满 5 步（list→extend→cancel→room.list→create），剩余 op 的
        # 探路调用触发上限——decide 的成果必须保留（对应 zh_0026 13 步预算场景）。
        env.step_limit_after = 5
        executor = _op_executor(env, op_registry, op_store)
        result = executor.execute_ops(_plan(
            ("decide", {"day": "2026-04-21", "start": "14:00", "end": "15:00",
                        "title": "评审", "addresses": ["0552_A1"], "minutes": 30}),
            ("extend", {"day": "2026-04-21", "start": "14:00", "end": "15:00",
                        "minutes": 30, "conditional": True}),
        ))
        # decide 的重订成果保留：新订 15:30 成功，原单已取消。
        assert result["booking_result"]["status"] == "success"
        assert result["booking_result"]["end"] == "15:30"
        names = [n for n, _ in env.calls]
        assert names.index("meetingroom.booking.cancel") < names.index("meetingroom.booking.create")
        # 上限在 extend op 的探路调用处触发——booking.create 只发生一次（decide 那笔）。
        assert names.count("meetingroom.booking.create") == 1

    def test_other_exception_still_propagates(self, op_registry, op_store) -> None:
        """非步数上限异常必须照常抛出（只拦 StepLimitExceeded，不吞真实错误）。"""
        env = OpFakeEnv()

        def _boom(name: str, args: dict) -> dict:
            raise RuntimeError("boom")
        env.call_tool = _boom
        executor = _op_executor(env, op_registry, op_store)
        with pytest.raises(RuntimeError):
            executor.execute_ops(_plan(
                ("cancel", {"order_id": "SEED-CANCEL-001", "day": "2026-04-21"}),
            ))


class TestMergeFinal:
    """③0223：冗余 op 的 blocked/failed 不覆盖已有成功状态（字段级合并守则）。"""

    def test_blocked_does_not_override_success(self) -> None:
        """multi_day 已 success 后，多余 book op 的 blocked 不污染 status/booking。"""
        final = {
            "booking_result": {
                "status": "success",
                "room_id": "0552-011",
                "day": "2026-05-13",
            }
        }
        blocked = {"booking_result": {"status": "blocked", "reason": "no_bookable_room"}}
        merged = MeetingroomExecutor._merge_final(dict(final), blocked)
        assert merged["booking_result"]["status"] == "success"
        assert merged["booking_result"]["room_id"] == "0552-011"

    def test_accepts_blocked_when_no_success_yet(self) -> None:
        """目标尚无成功状态 → blocked 正常写入（首个 op 就失败的情况）。"""
        merged = MeetingroomExecutor._merge_final({}, {"booking_result": {"status": "blocked"}})
        assert merged["booking_result"]["status"] == "blocked"

    def test_success_overrides_earlier_blocked(self) -> None:
        """先 blocked 后成功 → success 覆盖（0245 双执行场景的合并方向）。"""
        final = {"booking_result": {"status": "blocked", "reason": "no_bookable_room"}}
        ok = {"booking_result": {"status": "success", "room_id": "0552-005"}}
        merged = MeetingroomExecutor._merge_final(dict(final), ok)
        assert merged["booking_result"]["status"] == "success"

    def test_failed_statuses_also_preserve_success(self) -> None:
        """cancel_failed / extend_failed / not_found 同样不覆盖已有成功。"""
        final = {"booking_result": {"status": "success", "room_id": "0552-011"}}
        for bad in ("failed", "cancel_failed", "extend_failed", "not_found", "need_confirmation"):
            merged = MeetingroomExecutor._merge_final(
                dict(final), {"booking_result": {"status": bad}}
            )
            assert merged["booking_result"]["status"] == "success", bad

    # —— 0050：queried（只读查询）不覆盖决策状态；决策覆盖 queried ——

    def test_queried_does_not_override_blocked(self) -> None:
        """extend 冲突 blocked 后，追加 query op 不能把状态洗成 queried（0050）。"""
        final = {"booking_result": {"status": "blocked", "reason": "extend_conflict"}}
        queried = {"booking_result": {"status": "queried", "keyword": "项目启动"}}
        merged = MeetingroomExecutor._merge_final(dict(final), queried)
        assert merged["booking_result"]["status"] == "blocked"
        assert merged["booking_result"]["reason"] == "extend_conflict"

    def test_pure_query_keeps_queried(self) -> None:
        """纯查询首个 op 无已有状态 → queried 正常写入。"""
        merged = MeetingroomExecutor._merge_final(
            {}, {"booking_result": {"status": "queried", "day": "2026-05-05"}}
        )
        assert merged["booking_result"]["status"] == "queried"

    def test_blocked_overrides_earlier_queried(self) -> None:
        """先 queried 后决策状态 → 决策覆盖只读查询（[query, extend→blocked]）。"""
        final = {"booking_result": {"status": "queried", "keyword": "项目启动"}}
        blocked = {"booking_result": {"status": "blocked", "reason": "extend_conflict"}}
        merged = MeetingroomExecutor._merge_final(dict(final), blocked)
        assert merged["booking_result"]["status"] == "blocked"

    def test_queried_does_not_override_other_action_status(self) -> None:
        """extended / canceled 等真实动作结果（未知状态按决策级）也不被 queried 覆盖。"""
        for kept in ("extended", "canceled", "updated"):
            final = {"booking_result": {"status": kept}}
            merged = MeetingroomExecutor._merge_final(
                dict(final), {"booking_result": {"status": "queried"}}
            )
            assert merged["booking_result"]["status"] == kept, kept
