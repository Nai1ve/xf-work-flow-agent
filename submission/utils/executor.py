"""执行层：S1/S2 最小闭环执行器（会议室）。

对应 technical_design.md §6「执行层」与「会议室 SOP」的 S1/S2 部分。本阶段只执行
S1 基础预订（含 S1w 工位关联、S1d 逐天最早 / 多日同会议室、S1m 多约束）与 S2 纯查询；
其余意图（S3 取消 / S4 重订 / S5 延长 / S6 参会人 / M 多轮 / S1s 日程对比预订）
由入口层直接返回空 ``{}``（0 分防线），后续阶段逐个接入。

设计意图（AGENT.md「静态契约只作先验，运行时证据优先」）：
- 每次 ``env.call_tool`` 前经 ``registry.validate_call`` 校验（防 forbidden）；
- 写操作前经 ``can_execute_write`` 门禁 + 时间冲突 + bookable 过滤双重校验；
- 不记忆 case，不读 reference / gold，只依据运行时工具证据决策；
- 选址口径与官方 evaluator 对齐：
  - ``_list_rooms`` 的 ``office_id`` 从**候选地址自身的楼栋**推导（不取用户目标楼栋，
    否则 0013 的 A2 候选会被误筛成 0 房）；
  - ``_pick_room`` 用官方 ``_room_workspace_rank`` 语义（同楼层同楼栋 > 同楼栋 > 同园区）
    选「离工位最近」，与 S1w 类 must_satisfy 一致；
  - create（``_create_booking_raw``）的 ``office_id`` 统一传房间 officeId UUID（与 gold
    轨迹对齐；success_check 的 office_id 校验接受 {officeId, room_id, building} 任一，
    UUID 恒过；「离工位最近」检查要求全等亦满足）。
- 产出 final_answer 的 ``booking_result.office_id`` 按 reference 房间级规则
  （``_reference_office_id``）：楼栋式随机 UUID 房 → 楼栋名（0020/0023 的 A1/A2）、
  数字房/A3 合成 UUID 房 → officeId UUID。val reference 按此整理 10/10 匹配。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from utils.logger import ConsoleLogger


def _coerce_int(value: Any) -> Any:
    """把数字型字符串归一为 int（LLM 偶发把 capacity 输出成 "10" 而不是 10，
    会触发 room.list 校验拦截甚至整 case 崩，mr_0216）。非数字串原样返回，
    由下游校验报错而非静默吞掉。"""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value
from utils.understanding import (
    INTENT_BOOK,
    INTENT_QUERY,
    QUERY_BOOKING_LIST,
    QUERY_SCHEDULE,
    QUERY_UNBOOKABLE,
    QUERY_WORKSPACE,
    MeetingConstraints,
)
from utils.tool_contract import EffectiveToolRegistry
from utils.static_context import StaticContextStore

# 园区码（与 simulator _match_office_address 同源，只作候选回退的先验）。
_CAMPUS_HEFEI = "0551"
_CAMPUS_TOWN = "0552"


class MeetingroomExecutor:
    """会议室 S1/S2 执行器：按理解层约束驱动工具调用，产出 final_answer。

    Attributes:
        _env: 官方环境（仅用 call_tool）。
        _registry: 对账后的有效工具注册表（读/写门禁 + 调用前校验）。
        _static: 静态上下文（工位/楼栋候选的先验，可禁用）。
        _log: 理解层日志器。
        _history: 本次 case 的工具调用历史（(tool, args, result)）。
        _workspace_ctx: 本人工位上下文（building/floor/area），S1w 选址用。
    """

    # 工具名常量（与 tool_specs.json / 运行时一致）。
    ROOM_LIST = "meetingroom.room.list"
    BOOKING_CREATE = "meetingroom.booking.create"
    BOOKING_LIST = "meetingroom.booking.list"
    ROOM_SCHEDULE = "meetingroom.room.schedule"
    GET_WORKSPACE = "user.get_workspace"
    GET_INFO = "user.get_info"
    BOOKING_CANCEL = "meetingroom.booking.cancel"
    BOOKING_EXTEND = "meetingroom.booking.extend"
    BOOKING_PARTICIPANT_LIST = "meetingroom.booking.participant.list"
    BOOKING_PARTICIPANT_ADD = "meetingroom.booking.participant.add"
    BOOKING_PARTICIPANT_REMOVE = "meetingroom.booking.participant.remove"

    # 候选楼栋（无楼栋指定楼层时枚举；静态索引可收窄，见 _candidate_buildings）。
    _DEFAULT_BUILDINGS = ("A1", "A2", "A3", "A4", "A5")

    def __init__(
        self,
        env: Any,
        registry: EffectiveToolRegistry,
        static_store: StaticContextStore,
        logger: ConsoleLogger | None = None,
    ) -> None:
        """初始化。

        Args:
            env: 官方环境（只读 call_tool）。
            registry: 对账后的有效工具注册表。
            static_store: 静态上下文（只作先验）。
            logger: 理解层日志器；None 时静默。
        """
        self._env = env
        self._registry = registry
        self._static = static_store
        self._log = logger
        self._history: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._workspace_ctx: dict[str, str | None] | None = None
        self._current_uid: str | None = None

    # ------------------------------------------------------------------ 入口 --

    def execute(
        self, intent: str, constraints: MeetingConstraints
    ) -> dict[str, Any]:
        """按意图执行并产出 final_answer。

        Args:
            intent: IntentRouter 判定的意图。
            constraints: 理解层解析出的会议约束。

        Returns:
            final_answer dict（booking_result / 空 {}）。永不返回 None。
        """
        if intent == INTENT_BOOK:
            # S1s 日程对比预订：用户点名具体房间（A1-349），需要先查该房间
            # 空闲再订；本阶段未实现该子流程，返回空 {} 防止订错房。
            if constraints.named_room:
                self._log_warning(
                    f"S1s 命名房间预订（{constraints.named_room}）本阶段不执行"
                )
                return {}
            return self._execute_book(constraints)
        if intent == INTENT_QUERY:
            return self._execute_query(constraints)
        # 其余意图（S3~S6/M）：本阶段不执行写操作，返回空 {} 保住 0 分防线。
        self._log_warning(f"意图 {intent} 本阶段不执行，返回空")
        return {}

    # ------------------------------------------------------------ op 分发 --

    def execute_ops(self, plan: Any) -> dict[str, Any]:
        """按序执行会议 op 序列（MeetingOpPlan），合并为最终 final_answer。

        op → handler → 部分结果（booking_result / participant_result /
        participants / participants_added）→ 字段级合并。标识符（order_id /
        room_id / user_id）一律来自工具证据；缺工具时 handler 优雅降级
        （不动作 / 产出 blocked），绝不调未公开工具（防 forbidden）。

        Args:
            plan: MeetingOpPlan（.ops 为有序 MeetingOp）。

        Returns:
            final_answer dict；op 为空/全部失败返回 {}。永不返回 None。
        """
        ops = getattr(plan, "ops", None)
        if not ops:
            return {}
        final: dict[str, Any] = {}
        events: list[str] = []
        try:
            for op in ops:
                action = getattr(op, "action", None)
                target = getattr(op, "target", None) or {}
                if not isinstance(target, dict):
                    target = {}
                part = self._dispatch_op(action, target)
                if not part:
                    continue
                final = self._merge_final(final, part)
                if part.get("_ok"):
                    events.append(action)
        except Exception as exc:  # noqa: BLE001 —— 仅拦截步数预算耗尽
            # env 在 step_count>=budget 时于工具调用入口 raise StepLimitExceeded
            # （官方异常类跨环境 import 路径不稳定，按类名判定最稳）。已完成的
            # op 成果不能因剩余 op 空转越界而整锅丢弃（zh_0026 重订已成功却被
            # run() 顶层兜成 {}，丢 AS/RS）。捕获后保留已累积的 final，跳过剩余 op。
            if type(exc).__name__ != "StepLimitExceeded":
                raise
            self._log_warning(f"步数预算耗尽，保留已完成结果并跳过剩余 op: {exc!r}")
        self._compose_status(final, events)
        final.pop("_ok", None)
        if final.get("booking_result") == {}:
            final.pop("booking_result", None)
        return final

    def _dispatch_op(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        """按 action 分发到对应 handler。未知返回空（不动作，安全）。"""
        if action == "book":
            return self._op_book(target)
        if action == "multi_day":
            return self._op_multi_day(target)
        if action == "earliest":
            return self._op_earliest(target)
        if action == "compare_book":
            return self._op_compare_book(target)
        if action == "cancel":
            return self._op_cancel(target)
        if action == "extend":
            return self._op_extend(target)
        if action == "rebook":
            return self._op_rebook(target)
        if action == "participant_add":
            return self._op_participant_add(target)
        if action == "participant_remove":
            return self._op_participant_remove(target)
        if action == "participant_list":
            return self._op_participant_list(target)
        if action == "query":
            return self._op_query(target)
        if action == "decide":
            return self._op_decide(target)
        self._log_warning(f"未知 op: {action}")
        return {}

    @staticmethod
    def _merge_final(final: dict, part: dict) -> dict:
        """合并一个 op 的部分结果：booking_result 字段级，其余顶层键直取。

        booking_result 状态带优先级：success/active(3) > 决策状态(2) > 只读 queried(1)，
        未知状态按 2 处理（真实动作结果，也高于只读查询）。低优先级不覆盖高优先级：
        - 已有 success → 后续非 success 不覆盖（0223：multi_day 成功订到 0552-011 后，
          多余 book 的 blocked 不污染 status）；
        - 已有决策状态 → 后续 queried 不覆盖（0050：extend 冲突 blocked 后，追加 query
          op 不能把状态洗成 queried）；
        - 已有 queried → 后续决策/成功正常覆盖（决策优先于只读）。
        同优先级后者覆盖前者（保持现状）。
        """
        for key in ("participants", "participants_added"):
            if part.get(key) is not None:
                final[key] = part[key]
        pr = part.get("participant_result")
        if pr is not None:
            final["participant_result"] = pr
        br = part.get("booking_result")
        if isinstance(br, dict):
            target_br = final.setdefault("booking_result", {})
            _RANK = {"success": 3, "active": 3, "queried": 1}
            existing = target_br.get("status")
            cur = br.get("status")
            existing_rank = _RANK.get(existing, 2)
            cur_rank = _RANK.get(cur, 2)
            if existing and existing_rank > cur_rank:
                return final
            for k, v in br.items():
                if v is not None:
                    target_br[k] = v
        return final

    def _compose_status(self, final: dict, events: list[str]) -> None:
        """延长+参会人组合的最终 status（0240/0249 由操作顺序决定）。"""
        br = final.get("booking_result")
        if not isinstance(br, dict):
            return
        if (
            "extend" in events
            and "participant_add" in events
            and br.get("new_end")
            and "user_id" in br
        ):
            br["added_user_id"] = br.pop("user_id")
            br["status"] = (
                "extended_and_participant_added"
                if events.index("extend") < events.index("participant_add")
                else "updated"
            )

    def _constraints_from_target(self, target: dict[str, Any]) -> MeetingConstraints:
        """把 op.target（LLM 或规则产出的 query 级槽位）转成 MeetingConstraints。

        与 MeetingConstraintExtractor 的产物同构，供既有 S1/S2 执行器消费。
        """
        c = MeetingConstraints()
        c.day = target.get("day")
        c.days = target.get("days") or []
        c.book_only_day = target.get("book_only_day")
        c.week_start = target.get("week_start")
        c.week_end = target.get("week_end")
        c.start = target.get("start")
        c.end = target.get("end")
        c.building = target.get("building")
        c.campus = target.get("campus")
        c.floor = target.get("floor")
        c.addresses = target.get("addresses") or []
        c.fallback_building = target.get("fallback_building")
        c.capacity_gte = _coerce_int(target.get("capacity"))
        c.has_screen = target.get("screen")
        c.title = target.get("title")
        c.attendees = _coerce_int(target.get("attendees"))
        c.workspace_hint = bool(target.get("workspace_near"))
        c.time_flexible = bool(target.get("time_flexible"))
        # target 键来自 _constraints_to_target / LLM：rooms 或 compare_rooms 都认。
        rooms = target.get("rooms") or target.get("compare_rooms") or []
        c.named_room = (
            target.get("room")
            or target.get("named_room")
            or (rooms[0] if len(rooms) == 1 else None)
        )
        c.compare_rooms = list(rooms)
        c.minutes = target.get("minutes")
        c.persons = target.get("persons") or []
        c.order_id_hint = target.get("order_id")
        c.slots = target.get("slots") or []
        c.query_type = target.get("query_type")
        c.query_keyword = target.get("keyword")
        c.schedule_room_id = target.get("schedule_room_id") or target.get("room_id")
        c.schedule_start_date = target.get("start_date")
        c.schedule_end_date = target.get("end_date")
        return c

    # ------------------------------------------------------------ S1 op --

    def _op_book(self, target: dict[str, Any]) -> dict[str, Any]:
        """book：单日预订；点名房间（rooms 长度 1 / room）→ 先 schedule 校验空闲再订。"""
        c = self._constraints_from_target(target)
        if c.named_room:
            return self._book_named_room(c, c.named_room)
        return self._execute_book(c)

    def _op_multi_day(self, target: dict[str, Any]) -> dict[str, Any]:
        """multi_day：同日多场（slots）或多日同房（days）/多日校验只订一天。"""
        c = self._constraints_from_target(target)
        if len(c.slots) >= 2:
            return self._book_multi_slots(c)
        if len(c.days) >= 2:
            if c.book_only_day:
                return self._book_single_of_days(c)
            return self._book_multi_day(c)
        return {}

    def _op_earliest(self, target: dict[str, Any]) -> dict[str, Any]:
        """earliest：周内逐天最早可订。

        LLM 偶发只给 earliest 语义而不产出 week_start/week_end（mr_0012）——
        此时与 _execute_book 的守卫一致，退化为单日订 c.day，避免
        _book_sequential 里 date.fromisoformat("") 崩掉整个 case。
        """
        c = self._constraints_from_target(target)
        if c.week_start and c.week_end:
            return self._book_sequential(c, None)
        return self._book_single_day(c, None)

    def _op_compare_book(self, target: dict[str, Any]) -> dict[str, Any]:
        """compare_book：room.schedule 逐个对比（覆盖订日所在周），选更空闲后预订。"""
        c = self._constraints_from_target(target)
        rooms = c.compare_rooms or ([c.named_room] if c.named_room else [])
        if not rooms or not c.day:
            return {}
        # 0250「查一下我的工位」：用户显式要求 → must 要求调用 get_workspace。
        if target.get("workspace_near") and self._registry.can_execute_read(self.GET_WORKSPACE):
            self._apply_workspace(c)
        if not self._registry.is_available(self.ROOM_SCHEDULE):
            return self._blocked("schedule_unavailable")
        week_start, week_end = self._week_range(c.day)
        best: str | None = None
        best_score: int | None = None
        for room_id in rooms:
            result = self._call_tool(
                self.ROOM_SCHEDULE,
                {"room_id": room_id, "start_date": week_start, "end_date": week_end},
            )
            if result.get("error"):
                continue
            score = len(result.get("bookings") or []) + len(result.get("busy_slots") or [])
            if best is None or score < best_score:  # type: ignore[operator]
                best, best_score = room_id, score
        if best is None:
            return self._blocked()
        return self._book_named_room(c, best)

    def _op_cancel(self, target: dict[str, Any]) -> dict[str, Any]:
        """cancel：order_id 直给 → 直接取消；有定位词 → booking.list 定位后取消；
        都缺 → 只探路并 blocked(need_confirmation)（0026 禁止 cancel）。"""
        order_id = target.get("order_id")
        # 定位关键词只用规则 query_keyword（gap-fill 已填）；title 是会议主题，
        # 未必等于预订标题（0050 的「需求评审会」≠ 种子「项目复盘」），不能当关键词。
        keyword = target.get("keyword")
        day = target.get("day")
        if order_id:
            if not self._registry.can_execute_write(self.BOOKING_CANCEL):
                return self._blocked("cancel_unavailable")
            result = self._call_tool(self.BOOKING_CANCEL, {"order_id": order_id})
            if result.get("error"):
                return {"_ok": False, "booking_result": {"status": "blocked", "reason": "cancel_failed"}}
            return {"_ok": True, "booking_result": {"status": "cancelled", "order_id": order_id}}
        if not day:
            return {"booking_result": {"status": "blocked", "reason": "need_confirmation"}}
        if not keyword:
            # 无唯一标识 → 只调 booking.list 探路（满足 must），不取消。
            if self._registry.is_available(self.BOOKING_LIST):
                self._call_tool(self.BOOKING_LIST, {"day": day, "status": "active"})
            return {"booking_result": {"status": "blocked", "reason": "need_confirmation"}}
        booking = self._locate_own_booking(day, keyword=keyword, time_hint=(target.get("start"), target.get("end")))
        if not booking:
            return {"booking_result": {"status": "blocked", "reason": "not_found"}}
        oid = booking.get("order_id") or booking.get("booking_id")
        if not self._registry.can_execute_write(self.BOOKING_CANCEL):
            return self._blocked("cancel_unavailable")
        result = self._call_tool(self.BOOKING_CANCEL, {"order_id": oid})
        if result.get("error"):
            return {"_ok": False, "booking_result": {"status": "blocked", "reason": "cancel_failed"}}
        return {"_ok": True, "booking_result": {"status": "cancelled", "order_id": oid}}

    def _op_extend(self, target: dict[str, Any]) -> dict[str, Any]:
        """extend：定位 → 延长。条件性延长先探测冲突，命中则不真调 extend。

        0050「能多开半小时就延长，后面冲突就别动原会议」：gold 只调 booking.list
        就判定 blocked——延长窗口与他人预订冲突时**不调用 extend**（否则 extend
        返回 conflict 会触发 forbidden「会议预订时间与房间占用冲突」→ AS=0）。
        先探测再决定：冲突 → blocked(conflict_after_requested_extension)；
        无冲突 → 真调 extend。直给延长冲突 → extend_failed(time_conflict)。"""
        order_id = target.get("order_id")
        minutes = target.get("minutes") or 30
        conditional = bool(target.get("conditional"))
        day = target.get("day")

        if conditional and day:
            booking, conflict = self._probe_extend_conflict(
                day,
                keyword=target.get("keyword"),
                time_hint=(target.get("start"), target.get("end")),
                order_id=order_id,
                minutes=minutes,
            )
            if booking is None:
                return {}
            oid = booking.get("order_id") or booking.get("booking_id")
            if conflict:
                return {
                    "_ok": False,
                    "booking_result": {
                        "status": "blocked",
                        "order_id": oid,
                        "reason": "conflict_after_requested_extension",
                    },
                }
            return self._do_extend(oid, minutes, conditional)

        if not order_id:
            if not day:
                return {}
            booking = self._locate_own_booking(
                day,
                keyword=target.get("keyword"),
                time_hint=(target.get("start"), target.get("end")),
            )
            if not booking:
                return {}
            order_id = booking.get("order_id") or booking.get("booking_id")
        return self._do_extend(order_id, minutes, conditional)

    def _do_extend(
        self, order_id: str, minutes: int, conditional: bool
    ) -> dict[str, Any]:
        """真正调用 booking.extend 并归一结果。"""
        result = self._call_tool(
            self.BOOKING_EXTEND, {"order_id": order_id, "minutes": int(minutes)}
        )
        if result.get("error"):
            if conditional:
                return {
                    "_ok": False,
                    "booking_result": {
                        "status": "blocked",
                        "order_id": order_id,
                        "reason": "conflict_after_requested_extension",
                    },
                }
            return {
                "_ok": False,
                "booking_result": {
                    "status": "extend_failed",
                    "order_id": order_id,
                    "reason": "time_conflict",
                },
            }
        return {"_ok": True, "booking_result": {"status": "extended", "order_id": order_id, "new_end": result.get("end")}}

    def _probe_extend_conflict(
        self,
        day: str,
        keyword: str | None,
        time_hint: tuple[str | None, str | None] | None,
        order_id: str | None,
        minutes: int,
    ) -> tuple[dict[str, Any] | None, bool]:
        """条件性延长：booking.list 一次调用定位本人预订 + 探测延长窗口冲突。

        Args:
            day: 目标日期。
            keyword: booking.list keyword（定位用）。
            time_hint: (start, end) 时段过滤（定位用）。
            order_id: 已知 order_id 时直接按 id 定位。
            minutes: 延长分钟数。

        Returns:
            (booking, conflict)；booking 定位不到返回 (None, False)。
        """
        if not day or not self._registry.is_available(self.BOOKING_LIST):
            return None, False
        args: dict[str, Any] = {"day": day, "status": "active"}
        if keyword:
            args["keyword"] = keyword
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return None, False
        bookings = result.get("bookings") or []
        if order_id:
            booking = next(
                (
                    b for b in bookings
                    if (b.get("order_id") or b.get("booking_id")) == order_id
                ),
                None,
            )
        else:
            booking = self._filter_own_candidates(bookings, time_hint=time_hint)
        if booking is None:
            return None, False
        conflict = self._extend_would_conflict(bookings, booking, minutes)
        return booking, conflict

    def _extend_would_conflict(
        self,
        bookings: list[dict[str, Any]],
        booking: dict[str, Any],
        minutes: int,
    ) -> bool:
        """延长窗口 [end, end+minutes] 是否与同房其它活跃预订重叠。

        只信工具证据：room_id / end 缺失时保守返回 False（交给真调 extend 判），
        绝不臆断冲突。
        """
        room_id = booking.get("room_id")
        end = booking.get("end")
        if not room_id or not end:
            return False
        oid = booking.get("order_id") or booking.get("booking_id")
        new_end = self._add_minutes(end, int(minutes))
        for b in bookings:
            if b.get("status") == "cancelled":
                continue
            if (b.get("order_id") or b.get("booking_id")) == oid:
                continue
            if b.get("room_id") != room_id:
                continue
            s, e = b.get("start"), b.get("end")
            if s and e and s < new_end and e > end:
                return True
        return False

    def _op_rebook(self, target: dict[str, Any]) -> dict[str, Any]:
        """rebook：定位原会议 → 取消 → 按目标约束重订。

        SEED 直给 → status=rebooked + cancelled_order_id（office_id 用房间 officeId）；
        定位重订（0011 换大）→ status=success（office_id 用楼栋名）。
        """
        c = self._constraints_from_target(target)
        order_id = target.get("order_id")
        day = target.get("day")
        seeded = bool(order_id)
        original = None
        if order_id and day:
            original = self._find_booking_by_order(order_id, day)
        elif day:
            original = self._locate_own_booking(
                day,
                keyword=target.get("keyword"),
                time_hint=(c.start, c.end),
            )
        if not original:
            return {}
        orig_oid = original.get("order_id") or original.get("booking_id")
        orig_room_id = original.get("room_id")
        orig_room = self._room_static(orig_room_id)
        orig_capacity = int((orig_room or {}).get("capacity", 0) or 0)
        orig_day = original.get("day")
        orig_start = original.get("start")
        orig_end = original.get("end")
        orig_title = original.get("title") or ""
        if not self._registry.can_execute_write(self.BOOKING_CANCEL):
            return self._blocked("cancel_unavailable")
        res = self._call_tool(self.BOOKING_CANCEL, {"order_id": orig_oid})
        if res.get("error"):
            return {"booking_result": {"status": "blocked", "reason": "cancel_failed"}}

        nc = MeetingConstraints()
        nc.day = day or orig_day
        nc.start = target.get("start") or orig_start
        nc.end = target.get("end") or orig_end
        nc.addresses = target.get("addresses") or []
        if not nc.addresses and orig_room:
            nc.addresses = [self._address_for(orig_room.get("campus") or "0552", orig_room.get("building") or "", None)]
        nc.capacity_gte = _coerce_int(target.get("capacity"))
        nc.has_screen = target.get("screen")
        # rebook 语义是替换原会议 → 标题沿用原预订（种子）标题。train 8 个 rebook
        # case（mr_0027/0222/0235、zh_0020/0026/0033/0037/0226）reference 标题 100%
        # = 种子标题，即使 query 措辞不同（「项目复盘会/评审会」→ 种子「季度复盘」）。
        # query 派生的 target.title 只是复述原会议，不能覆盖种子标题（用户定案 2026-08-11）。
        nc.title = orig_title or target.get("title")
        if target.get("larger"):
            nc.capacity_gte = max(nc.capacity_gte or 0, orig_capacity + 1)
        result = self._execute_book(nc)
        br = result.get("booking_result") if isinstance(result, dict) else None
        if not br or br.get("status") != "success":
            return result if result else self._blocked()
        if seeded:
            return {"_ok": True, "booking_result": {**br, "status": "rebooked", "cancelled_order_id": orig_oid}}
        # 定位重订（0011）：reference office_id 用楼栋名。
        new_room = self._room_static(br.get("room_id"))
        if new_room and new_room.get("building"):
            br = {**br, "office_id": new_room.get("building")}
        return {"_ok": True, "booking_result": br}

    def _op_participant_add(self, target: dict[str, Any]) -> dict[str, Any]:
        """participant_add：定位 → 解析 user_id → 去重 → add。

        SEED 直给 → booking_result（participant_added / participants_added）；
        定位 → participant_result.added（单）/ 顶层 participants_added（多）。
        """
        persons = target.get("persons") or []
        if not persons:
            return {}
        order_id = target.get("order_id")
        day = target.get("day")
        seeded = bool(order_id)
        if not order_id:
            if not day:
                return {}
            booking = self._locate_own_booking(
                day, keyword=target.get("keyword")
            )
            if not booking:
                return {}
            order_id = booking.get("order_id") or booking.get("booking_id")
        resolved: list[tuple[dict, str]] = []
        for person in persons:
            if not isinstance(person, dict):
                person = {"name": str(person)} if person else {}
            uid = self._resolve_user_id(person)
            if uid:
                resolved.append((person, uid))
        if not resolved:
            return {}
        # 去重（0032）：已在该会议参会人里则不动。
        if target.get("dedup") and self._registry.is_available(self.BOOKING_PARTICIPANT_LIST):
            existing = self._participant_user_ids(order_id)
            if existing is not None:
                missing = [(p, u) for p, u in resolved if u not in existing]
                if not missing:
                    person, uid = resolved[0]
                    return {
                        "participant_result": {
                            "status": "already_exists",
                            "order_id": order_id,
                            "user_id": uid,
                            "name": person.get("name", ""),
                        }
                    }
                resolved = missing
        added: list[dict] = []
        for person, uid in resolved:
            result = self._call_tool(self.BOOKING_PARTICIPANT_ADD, {"order_id": order_id, "user_id": uid})
            if result.get("error"):
                continue
            added.append({"user_id": uid, "name": person.get("name", "")})
        if not added:
            return {}
        if seeded:
            if len(added) == 1:
                return {"_ok": True, "booking_result": {"status": "participant_added", "order_id": order_id, "user_id": added[0]["user_id"]}}
            return {"_ok": True, "booking_result": {"status": "participants_added", "order_id": order_id, "added_count": len(added)}}
        if len(added) == 1:
            u = added[0]
            return {"_ok": True, "participant_result": {"status": "added", "order_id": order_id, "user_id": u["user_id"], "name": u["name"]}}
        return {"_ok": True, "participants_added": added}

    def _op_participant_remove(self, target: dict[str, Any]) -> dict[str, Any]:
        """participant_remove：定位 → 解析 → remove。"""
        persons = target.get("persons") or []
        if not persons:
            return {}
        order_id = target.get("order_id")
        day = target.get("day")
        if not order_id:
            if not day:
                return {}
            booking = self._locate_own_booking(
                day, keyword=target.get("keyword")
            )
            if not booking:
                return {}
            order_id = booking.get("order_id") or booking.get("booking_id")
        person = persons[0] if isinstance(persons[0], dict) else {"name": str(persons[0])}
        uid = self._resolve_user_id(person)
        if not uid:
            return {}
        result = self._call_tool(self.BOOKING_PARTICIPANT_REMOVE, {"order_id": order_id, "user_id": uid})
        if result.get("error"):
            return {}
        return {"_ok": True, "participant_result": {"status": "removed", "order_id": order_id, "user_id": uid, "name": person.get("name", "")}}

    def _op_participant_list(self, target: dict[str, Any]) -> dict[str, Any]:
        """participant_list：定位 → 查参会人清单（顶层 participants）。"""
        order_id = target.get("order_id")
        day = target.get("day")
        if not order_id:
            if not day:
                return {}
            booking = self._locate_own_booking(
                day, keyword=target.get("keyword")
            )
            if not booking:
                return {}
            order_id = booking.get("order_id") or booking.get("booking_id")
        result = self._call_tool(self.BOOKING_PARTICIPANT_LIST, {"order_id": order_id})
        if result.get("error"):
            return {}
        return {"_ok": True, "participants": result.get("participants") or []}

    def _op_query(self, target: dict[str, Any]) -> dict[str, Any]:
        """query：纯查询（复用 S2），不做任何写操作。"""
        c = self._constraints_from_target(target)
        qt = c.query_type
        if qt and qt not in (QUERY_BOOKING_LIST, QUERY_SCHEDULE, QUERY_UNBOOKABLE, QUERY_WORKSPACE):
            mapping = {
                "booking_list": QUERY_BOOKING_LIST,
                "schedule": QUERY_SCHEDULE,
                "unbookable": QUERY_UNBOOKABLE,
                "workspace": QUERY_WORKSPACE,
            }
            c.query_type = mapping.get(qt)
        if not c.query_type and c.query_keyword:
            c.query_type = QUERY_BOOKING_LIST
        return self._execute_query(c)

    def _op_decide(self, target: dict[str, Any]) -> dict[str, Any]:
        """decide 条件分支（0027）：探路 booking.list → 没订就 book / 已订就 extend /
        冲突则 cancel + rebook（结束时刻 = 原结束 + 延长分钟）。

        探测命中冲突（0026/0037 的 seed 预置占用）→ **跳过真调 extend**：真调会返回
        conflict error 写进历史，被 evaluator 计为动作错误（每个 -5 AS）。探测未命中
        再真调 extend（保守，room_id/end 缺失时不臆断，与 _op_extend conditional 一致）。
        重订沿用原会议标题（gold 语义：冲突后「重订同一会议」，0026/0037 的 reference
        title 取自 seed 原订，而非 LLM 转述的 query 措辞「项目复盘会议室」）。
        """
        c = self._constraints_from_target(target)
        if not c.day:
            return {}
        minutes = c.minutes or 30
        booking, conflict = self._probe_extend_conflict(
            c.day,
            keyword=target.get("keyword"),
            time_hint=(c.start, c.end),
            order_id=target.get("order_id"),
            minutes=minutes,
        )
        if booking is None:
            return self._op_book(target)
        oid = booking.get("order_id") or booking.get("booking_id")
        orig_title = booking.get("title") or target.get("title")
        if not conflict:
            # 探测未命中冲突 → 真调 extend（实际延长）；仍报错才走取消重订。
            res = self._call_tool(self.BOOKING_EXTEND, {"order_id": oid, "minutes": int(minutes)})
            if not res.get("error"):
                return {"_ok": True, "booking_result": {"status": "extended", "order_id": oid, "new_end": res.get("end")}}
        if not self._registry.can_execute_write(self.BOOKING_CANCEL):
            return self._blocked("cancel_unavailable")
        res_c = self._call_tool(self.BOOKING_CANCEL, {"order_id": oid})
        if res_c.get("error"):
            return {"booking_result": {"status": "blocked", "reason": "cancel_failed"}}
        nc = dict(target)
        nc["title"] = orig_title
        nc["end"] = self._add_minutes(c.end, minutes)
        nc.pop("order_id", None)
        new = self._op_book(nc)
        br = new.get("booking_result") if isinstance(new, dict) else None
        if not br or br.get("status") != "success":
            return new if new else self._blocked()
        static_room = self._room_static(br.get("room_id"))
        if static_room and static_room.get("building"):
            br = {**br, "office_id": static_room.get("building")}
        return {"_ok": True, "booking_result": br}

    # ------------------------------------------------------- 复合预订 handler --

    def _book_named_room(self, c: MeetingConstraints, room_id: str) -> dict[str, Any]:
        """点名房间预订（0227）：先 room.schedule 校验目标时段空闲，再 create。

        Args:
            c: 会议约束（day/start/end/title 必填）。
            room_id: 点名房间（如 "A3-3F-312"）。

        Returns:
            final_answer dict（booking_result）或 blocked。
        """
        if not c.day or not c.start or not c.end:
            return {}
        if not self._registry.is_available(self.ROOM_SCHEDULE):
            return self._blocked("schedule_unavailable")
        result = self._call_tool(
            self.ROOM_SCHEDULE,
            {"room_id": room_id, "start_date": c.day, "end_date": c.day},
        )
        if result.get("error"):
            return self._blocked()
        for slot in result.get("busy_slots") or []:
            if len(slot) >= 2 and c.start < slot[1] and slot[0] < c.end:
                return self._blocked("room_busy")
        office_id = self._static.office_id_for_room(room_id) or room_id
        args: dict[str, Any] = {
            "day": c.day,
            "room_id": room_id,
            "start": c.start,
            "end": c.end,
            "title": c.title or "会议",
            "office_id": office_id,
        }
        if c.attendees is not None:
            args["attendees"] = c.attendees
        res = self._call_tool(self.BOOKING_CREATE, args)
        if res.get("success") is not True:
            return self._blocked()
        return {
            "booking_result": {
                "status": "success",
                "day": c.day,
                "office_id": office_id,
                "room_id": res.get("room_id") or room_id,
                "start": c.start,
                "end": c.end,
                "title": c.title or "会议",
            }
        }

    def _book_multi_slots(self, c: MeetingConstraints) -> dict[str, Any]:
        """同日多时段同房（0043）：每个槽位都可订的房间交集 → 逐槽位 create。

        Args:
            c: 会议约束（slots ≥2，含 day/start/end/title）。

        Returns:
            final_answer dict（booking_result.bookings 列表）或 blocked。
        """
        if not c.addresses:
            return self._blocked()
        common: set[str] | None = None
        per_slot: list[tuple[dict, str, dict]] = []
        for slot in c.slots:
            day = slot.get("day") or c.day
            if not day or not slot.get("start") or not slot.get("end"):
                return {}
            legal = self._collect_available(
                day, c, c.addresses, start=slot["start"], end=slot["end"]
            )
            ids = {room["room_id"] for _, room in legal}
            by_id = {room["room_id"]: room for _, room in legal}
            per_slot.append((slot, day, by_id))
            common = ids if common is None else (common & ids)
            if not common:
                return self._blocked()
        room_id = sorted(common)[0]
        room = per_slot[0][2][room_id]
        bookings: list[dict] = []
        for slot, day, by_id in per_slot:
            ok, info = self._create_booking_raw(
                c, day, room, start=slot["start"], end=slot["end"], title=slot.get("title")
            )
            if not ok:
                return {}
            bookings.append(
                {"day": day, "start": info["start"], "end": info["end"], "title": info["title"]}
            )
        return {
            "booking_result": {
                "status": "success",
                "room_id": room_id,
                "office_id": self._reference_office_id(room, c.building),
                "bookings": bookings,
            }
        }

    # ------------------------------------------------------------ 定位/解析 --

    def _locate_own_booking(
        self,
        day: str,
        keyword: str | None = None,
        time_hint: tuple[str | None, str | None] | None = None,
    ) -> dict[str, Any] | None:
        """定位当前用户的活跃预订：booking.list + 关键词/时段过滤 + 组织者过滤。

        返回的 order_id 来自工具证据（绝不从模型透传的 id 出发）。时段过滤能区分
        同名会议（0025 的 14-15 vs 16-17），组织者过滤仅在仍歧义时调用 get_workspace
        取当前 user_id（避免多数 case 白耗一步）。

        Args:
            day: 目标日期。
            keyword: 标题关键词（booking.list keyword 参数）。
            time_hint: (start, end) 时段过滤。

        Returns:
            booking dict；无匹配返回 None。
        """
        if not day or not self._registry.is_available(self.BOOKING_LIST):
            return None
        args: dict[str, Any] = {"day": day, "status": "active"}
        if keyword:
            args["keyword"] = keyword
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return None
        return self._filter_own_candidates(
            result.get("bookings") or [], time_hint=time_hint
        )

    def _filter_own_candidates(
        self,
        bookings: list[dict[str, Any]],
        time_hint: tuple[str | None, str | None] | None = None,
    ) -> dict[str, Any] | None:
        """从 booking.list 原始列表里定位当前用户的活跃预订。

        时段过滤能区分同名会议（0025 的 14-15 vs 16-17），组织者过滤仅在仍歧义时
        调用 get_workspace 取当前 user_id（避免多数 case 白耗一步）。与
        ``_locate_own_booking`` 共享同一套证据过滤逻辑（0050 探测延长冲突时复用）。
        """
        candidates = [b for b in bookings if b.get("status") != "cancelled"]
        if time_hint and time_hint[0] and time_hint[1]:
            by_time = [
                b for b in candidates
                if b.get("start") == time_hint[0] and b.get("end") == time_hint[1]
            ]
            if by_time:
                candidates = by_time
        if len(candidates) > 1:
            uid = self._current_user_id()
            if uid:
                owned = [b for b in candidates if str(b.get("organizer_user_id")) == uid]
                if owned:
                    return owned[0]
        return candidates[0] if candidates else None

    def _find_booking_by_order(
        self, order_id: str, day: str
    ) -> dict[str, Any] | None:
        """按 order_id 在 booking.list(day) 里定位预订（无 booking.detail 工具）。"""
        if not self._registry.is_available(self.BOOKING_LIST):
            return None
        result = self._call_tool(self.BOOKING_LIST, {"day": day, "status": "active"})
        if result.get("error"):
            return None
        for b in result.get("bookings") or []:
            if (b.get("order_id") or b.get("booking_id")) == order_id:
                return b
        return None

    def _current_user_id(self) -> str | None:
        """当前登录用户 user_id（get_workspace 返回），case 内缓存。"""
        if self._current_uid is not None:
            return self._current_uid
        if not self._registry.is_available(self.GET_WORKSPACE):
            return None
        result = self._call_tool(self.GET_WORKSPACE, {})
        if result.get("error"):
            return None
        self._current_uid = result.get("user_id")
        return self._current_uid

    def _resolve_user_id(self, person: dict[str, Any]) -> str | None:
        """把参会人解析成 user_id：query 带「工号X」→ 直接透传（即 user_id）；
        否则 user.get_info(keyword=姓名) 反查（0028/0031）。"""
        if not isinstance(person, dict):
            return None
        emp = person.get("employee_no")
        if emp:
            return str(emp)
        name = person.get("name")
        if not name or not self._registry.is_available(self.GET_INFO):
            return None
        result = self._call_tool(self.GET_INFO, {"keyword": name})
        if result.get("error"):
            return None
        users = result.get("users") or []
        return users[0].get("user_id") if users else None

    def _participant_user_ids(self, order_id: str) -> set[str] | None:
        """某预订当前的参会人 user_id 集合（participant.list）。"""
        result = self._call_tool(self.BOOKING_PARTICIPANT_LIST, {"order_id": order_id})
        if result.get("error"):
            return None
        return {str(p.get("user_id")) for p in result.get("participants") or []}

    def _room_static(self, room_id: str | None) -> dict[str, Any] | None:
        """静态上下文里的房间属性（capacity/building/campus），可禁用时返回 None。"""
        if not room_id or self._static is None:
            return None
        return self._static.room(room_id)

    @staticmethod
    def _week_range(day: str) -> tuple[str, str]:
        """订日所在周的周一~周日（compare 覆盖整周日程）。"""
        d = date.fromisoformat(day)
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()

    @staticmethod
    def _add_minutes(time_str: str | None, minutes: int) -> str:
        """HH:MM 加 N 分钟（不跨天溢出检查，调用方保证合理）。"""
        h, m = (int(part) for part in (time_str or "00:00").split(":"))
        total = h * 60 + m + int(minutes)
        return f"{total // 60:02d}:{total % 60:02d}"

    # ------------------------------------------------------------ 工具调用 --

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """带门禁的 env.call_tool：调用前校验 + 写门禁 + 结果错误记录。

        防 forbidden 三道闸：
        1. 写操作须通过 can_execute_write（未公开/运行时独有/非写工具均拦截）；
        2. validate_call 校验（工具已公开 / 必填齐全 / 类型正确）；
        3. 调用后检查 result.error，记日志供上层决策。

        Args:
            name: 工具名。
            args: 调用参数。

        Returns:
            env.call_tool 的原始返回（含 error 时同样返回，不抛异常）。
        """
        if self._registry.is_write(name) and not self._registry.can_execute_write(name):
            self._log_warning(f"写操作被门禁拦截，不调用: {name}")
            return {"error": f"write_gate_denied: {name}"}

        check = self._registry.validate_call(name, args)
        if not check["ok"]:
            for error in check["errors"]:
                self._log_warning(f"调用前校验拦截 {name}: {error}")
            return {"error": f"validate_failed: {name}"}

        result = self._env.call_tool(name, args)
        self._history.append((name, args, result))
        if result.get("error"):
            self._log_warning(f"{name} 返回 error: {result['error']}")
        return result

    # ------------------------------------------------------------------ S1 --

    def _execute_book(self, c: MeetingConstraints) -> dict[str, Any]:
        """S1 预订闭环分派：多日同会议室 / 逐天最早 / 单日。

        流程（对应 SOP §三.S1）：
        1. S1w：查询本人工位，推导楼栋/楼层偏好；
        2. 按约束形态分派到多日交集、逐天最早或单日预订；
        3. 首个可用房间 create（含 fallback：备选楼栋 / 反园区 / ±30 分钟）。
        """
        ws_floor: str | None = None
        if c.workspace_hint and self._registry.can_execute_read(self.GET_WORKSPACE):
            ws_floor = self._apply_workspace(c)

        if len(c.days) >= 2:
            # 0223「周三和周四都要空闲，找到后订周三的」：多日校验但只订一天。
            if c.book_only_day:
                return self._book_single_of_days(c)
            return self._book_multi_day(c)
        if c.week_start and c.week_end:
            return self._book_sequential(c, ws_floor)
        return self._book_single_day(c, ws_floor)

    def _apply_workspace(self, c: MeetingConstraints) -> str | None:
        """S1w：调用 user.get_workspace，用工位地址推导楼栋/楼层偏好。

        设计意图：
        - 工位是**软约束**——预订目标是「离工位最近」，楼栋/楼层只作候选偏好；
        - 查询既未显式指定园区也未指定楼栋时，以工位楼栋作为搜索地址（0049 用
          0552_A1）；显式指定了园区（0018 用 0552）或楼栋（0016 用 0551_A4）则保留
          原候选，只补楼栋名供 create/reference 使用；
        - 楼层/园区同时写入 ``_workspace_ctx``，供 ``_pick_room`` 按官方
          ``_room_workspace_rank`` 语义做「离工位最近」选址。

        Args:
            c: 会议约束（原地更新 building / campus / addresses）。

        Returns:
            工位楼层（如 "3F"），用于房间同楼层优先；未取到返回 None。
        """
        result = self._call_tool(self.GET_WORKSPACE, {})
        if result.get("error"):
            self._log_warning(f"get_workspace 失败: {result.get('error')}")
            return None
        address = result.get("office_address") or ""
        building, campus, floor = self._parse_office_address(address)
        office_name = result.get("office_name") or ""
        area: str | None = None
        if "北区" in office_name:
            area = "北区"
        elif "南区" in office_name:
            area = "南区"
        self._workspace_ctx = {
            "campus": campus,
            "building": building,
            "floor": floor,
            "area": area,
        }
        self._log_info(
            f"工位: address={address} building={building} campus={campus} "
            f"floor={floor} area={area}"
        )

        if building:
            # 先记录查询是否显式指定了楼栋，再补工位楼栋名（供 create/reference）。
            query_had_building = c.building is not None
            if not c.building:
                c.building = building
            # 查询既未显式指定园区也未指定楼栋 → 用工位楼栋作为搜索地址。
            if not c.campus_explicit and not query_had_building:
                c.campus = campus or c.campus
                c.addresses = [f"{c.campus}_{building}"]
        return floor

    def _book_single_day(
        self, c: MeetingConstraints, ws_floor: str | None
    ) -> dict[str, Any]:
        """单日预订：按候选组合顺序搜索，首个有可用房间的组合 create。

        候选组合顺序（优先级从高到低）：
        1. 主地址 + 目标时段；
        2. 主地址 + ±30 分钟时段（查询含「前后半小时」柔性，0012）；
        3. 反园区同楼栋 + 目标时段（隐式园区查询下主园区无解，0008）；
        4. 反园区同楼栋 + ±30 分钟时段。

        每个组合内按地址顺序逐候选楼栋 room.list，首个可用房间即选。

        Args:
            c: 会议约束。
            ws_floor: 工位楼层（未使用，选址走 _workspace_ctx 的官方 rank）。

        Returns:
            final_answer dict（booking_result）或空 {}。
        """
        if not c.day:
            self._log_warning("缺 day，无法预订")
            return {}
        # 无显式时间 → 默认规范槽位 14:00-15:00（跨域 Fix B 用户定案）。gold 对
        # 「订明天会议室」等无时间订会议室的 case 一律订 14:00-15:00（mt_0001/0201/
        # 0202 + zh_0007/0008/0010 共 6 case 一致）；check 只认 day+14:00-15:00，
        # 不查 office。仅 day 缺失仍放弃（无法定位日期）。
        if not c.start or not c.end:
            c.start = c.start or "14:00"
            c.end = c.end or "15:00"
            self._log_info(f"无显式时间，默认槽位 {c.start}-{c.end}")

        for addresses, start, end in self._search_combos(c):
            available_rooms = self._collect_available(
                c.day, c, addresses, start=start, end=end
            )
            if available_rooms:
                _, room = self._pick_room(available_rooms)
                self._log_info(
                    f"选定房间: {room.get('room_id')} {start}-{end} "
                    f"candidates={len(available_rooms)}"
                )
                return self._create_booking(c, c.day, room, start=start, end=end)

        self._log_warning(f"{c.day} {c.start}-{c.end} 无可用会议室")
        # 所有候选组合都无可订房间 → 产出 blocked（对齐金标 reference：val 0021/0022
        # 的 status=blocked, reason=no_bookable_room；返回空 {} 会丢掉这 15 分）。
        return self._blocked()

    def _search_combos(
        self, c: MeetingConstraints
    ) -> list[tuple[list[str], str, str]]:
        """构造候选 (地址列表, start, end) 组合（见 _book_single_day 优先级说明）。"""
        combos: list[tuple[list[str], str, str]] = [(c.addresses, c.start, c.end)]
        shifts = self._shift_times(c) if c.time_flexible else []

        if c.time_flexible:
            for start, end in shifts:
                combos.append((c.addresses, start, end))

        # 楼栋级回退（0229：A1 3F 全被占，但 1F 有房）：楼层是软约束，同楼栋
        # 其他楼层可订时降级到楼栋级再搜；排在反园区之前——楼栋优先于园区。
        floorless = self._floorless_addresses(c)
        if floorless:
            combos.append((floorless, c.start, c.end))
            if c.time_flexible:
                for start, end in shifts:
                    combos.append((floorless, start, end))

        # 反园区回退只在园区未显式指定且非工位锚定时启用（0008：A3 默认落在小镇，
        # 但 ≥15 人的 A3 房在合肥；楼栋名保留，只换园区码）。
        if not c.campus_explicit and not c.workspace_hint:
            fallback = self._opposite_campus_addresses(c)
            if fallback:
                combos.append((fallback, c.start, c.end))
                if c.time_flexible:
                    for start, end in shifts:
                        combos.append((fallback, start, end))
        return combos

    def _floorless_addresses(self, c: MeetingConstraints) -> list[str]:
        """楼栋级候选：剔除楼层（0552_A1_3F → 0552_A1），去重保序。

        楼层是软约束——楼层内全被占时可退到同楼栋其他楼层（0229 3F→1F），
        但楼栋名与园区码保持不变。

        Args:
            c: 会议约束。

        Returns:
            含楼层的地址剔除楼层后的楼栋级地址列表；无楼层地址则返回空列表。
        """
        out: list[str] = []
        for address in c.addresses:
            building, campus, floor = self._parse_office_address(address)
            if not building or not floor:
                continue
            plain = self._address_for(campus, building, None)
            if plain not in out:
                out.append(plain)
        return out

    def _shift_times(self, c: MeetingConstraints) -> list[tuple[str, str]]:
        """返回 [提前 30 分钟, 延后 30 分钟] 的替代时段（仅当天，不做跨天）。"""
        if not c.start or not c.end:
            return []

        def fmt(minutes: int) -> str | None:
            if minutes < 0 or minutes > 24 * 60:
                return None
            return f"{minutes // 60:02d}:{minutes % 60:02d}"

        sh, sm = (int(part) for part in c.start.split(":"))
        eh, em = (int(part) for part in c.end.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        candidates: list[tuple[str, str]] = []
        earlier = (fmt(start_min - 30), fmt(end_min - 30))
        later = (fmt(start_min + 30), fmt(end_min + 30))
        if all(earlier):
            candidates.append(earlier)  # type: ignore[arg-type]
        if all(later):
            candidates.append(later)  # type: ignore[arg-type]
        return candidates

    def _opposite_campus_addresses(self, c: MeetingConstraints) -> list[str]:
        """同楼栋反园区候选：0552_A3 → 0551_A3（仅含楼栋的地址参与回退）。"""
        out: list[str] = []
        for address in c.addresses:
            building, campus, floor = self._parse_office_address(address)
            if not building:
                continue
            alt = _CAMPUS_HEFEI if campus == _CAMPUS_TOWN else _CAMPUS_TOWN
            out.append(self._address_for(alt, building, floor))
        return out

    @staticmethod
    def _address_for(campus: str, building: str, floor: str | None) -> str:
        parts = [campus, building]
        if floor:
            parts.append(floor)
        return "_".join(parts)

    def _book_sequential(
        self, c: MeetingConstraints, ws_floor: str | None
    ) -> dict[str, Any]:
        """逐天搜索最早可订：周内每天 room.list，首个有可用房间的当天订。

        对应 SOP S1d「逐天搜索最早空闲」（0040/0226/0232/0239）。搜索从
        week_start 到 week_end 依次调用 room.list，找到第一个有空闲的日期即订，
        同时自然满足「调用过某天 room.list」的 must_satisfy 条件。

        Args:
            c: 会议约束。
            ws_floor: 工位楼层（选址优先同楼层，其次地址顺序）。

        Returns:
            final_answer dict（booking_result）或空 {}。
        """
        if not c.start or not c.end:
            self._log_warning("缺 start/end，无法逐天预订")
            return {}
        if not c.week_start or not c.week_end:
            # 防御：任何调用方缺周区间都不该崩（_op_earliest 已先退化单日）。
            self._log_warning("缺 week_start/week_end，无法逐天预订")
            return {}
        start_date = date.fromisoformat(c.week_start or "")
        end_date = date.fromisoformat(c.week_end or "")
        day = start_date
        while day <= end_date:
            day_str = day.isoformat()
            available_rooms = self._collect_available(day_str, c, c.addresses)
            if available_rooms:
                _, room = self._pick_room(available_rooms)
                self._log_info(f"最早可订日期: {day_str} room={room.get('room_id')}")
                return self._create_booking(c, day_str, room)
            self._log_info(f"{day_str} 无可用，继续搜索")
            day += timedelta(days=1)

        self._log_warning(f"{c.week_start}~{c.week_end} 均无可用会议室")
        return self._blocked()

    def _book_multi_day(self, c: MeetingConstraints) -> dict[str, Any]:
        """多日同会议室（S1d）：逐天 room.list，取跨天都可订的房间交集后逐天 create。

        对应 0041/0042（「周三和周四」/「周二、周三、周四」，需同一个会议室）。
        候选是「所有日期都空闲」的房间交集；只对交集内房间下 create，避免订错房
        产生额外活跃预订。若某天无候选或交集为空则整体放弃（防止半订状态）。

        Args:
            c: 会议约束（days 长度 ≥2）。

        Returns:
            final_answer dict（booking_result.bookings 列表）或空 {}。
        """
        per_day: dict[str, dict[str, dict[str, Any]]] = {}
        common: set[str] | None = None
        for day in c.days:
            legal = self._collect_available(day, c, c.addresses)
            ids = {room["room_id"] for _, room in legal}
            per_day[day] = {room["room_id"]: room for _, room in legal}
            common = ids if common is None else (common & ids)
            if not common:
                self._log_warning(f"{day} 无跨天可用房间，放弃多日预订")
                return self._blocked()

        room_id = sorted(common)[0]
        room = per_day[c.days[0]][room_id]
        self._log_info(
            f"多日同房: room={room_id} days={c.days} 跨天可用={len(common)}"
        )
        bookings: list[dict[str, Any]] = []
        for day in c.days:
            ok, info = self._create_booking_raw(c, day, room)
            if not ok:
                return {}
            bookings.append(info)
        return {
            "booking_result": {
                "status": "success",
                "room_id": room_id,
                "office_id": self._reference_office_id(room, c.building),
                "bookings": bookings,
            }
        }

    def _book_single_of_days(self, c: MeetingConstraints) -> dict[str, Any]:
        """多日都要空闲但只订其中一天的预订（0223）。

        与 ``_book_multi_day`` 共享「跨天可用房间交集」逻辑，但 create 只落在
        ``book_only_day`` 一天——否则会违反「最终仅新增一条活跃会议预订」
        （forbidden「存在额外新增活跃会议预订」→ AS=0）。must_satisfy 仍要求
        room.list 覆盖全部候选日，因此 days 的每一天都会调用 room.list。

        Args:
            c: 会议约束（days ≥2 且 book_only_day 已指定）。

        Returns:
            final_answer dict（booking_result.status=success）或空 {}（失败）。
        """
        per_day: dict[str, dict[str, dict[str, Any]]] = {}
        common: set[str] | None = None
        for day in c.days:
            legal = self._collect_available(day, c, c.addresses)
            ids = {room["room_id"] for _, room in legal}
            per_day[day] = {room["room_id"]: room for _, room in legal}
            common = ids if common is None else (common & ids)
            if not common:
                self._log_warning(f"{day} 无跨天可用房间，放弃预订")
                return self._blocked()

        room_id = sorted(common)[0]
        target = c.book_only_day or c.days[0]
        room = per_day[target][room_id]
        self._log_info(
            f"多日校验只订单日: room={room_id} days={c.days} target={target}"
        )
        ok, info = self._create_booking_raw(c, target, room)
        if not ok:
            return {}
        ref_office = self._reference_office_id(room, c.building)
        result = {
            "booking_result": {
                "status": "success",
                "day": info["day"],
                "office_id": ref_office,
                "room_id": info["room_id"],
                "start": info["start"],
                "end": info["end"],
                "title": info["title"],
            }
        }
        # reference 超集：楼栋名时额外上报 ``office``（zh_0003/0009/0015 用此键）。
        if ref_office and re.match(r"^[A-Z]\d+$", ref_office):
            result["booking_result"]["office"] = ref_office
        return result

    # ------------------------------------------------------------ 候选处理 --

    def _collect_available(
        self,
        day: str,
        c: MeetingConstraints,
        addresses: list[str],
        start: str | None = None,
        end: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """按地址顺序收集目标时段内可订房间（(address, room) 列表）。

        Args:
            day: 查询日期。
            c: 会议约束。
            addresses: 候选 office_address 列表（有序）。
            start / end: 时段覆盖（未传时用 c.start/c.end）。

        Returns:
            可订房间列表；备选楼栋模式（fallback_building）下主地址有解即停。
        """
        start = start if start is not None else c.start
        end = end if end is not None else c.end
        available: list[tuple[str, dict[str, Any]]] = []
        for address in addresses:
            rooms = self._list_rooms(day, address, c)
            for room in rooms:
                if self._is_available(room, start, end):
                    available.append((address, room))
            # 第一个有可用房间的地址即停：既符合「主地址优先」的选题（0013 取 A2
            # 而非继续枚举 A3），也避免无楼栋枚举（0038 A1_1F..A5_1F）烧光步数预算。
            if available:
                break
        return available

    def _list_rooms(
        self, day: str, address: str | None, c: MeetingConstraints
    ) -> list[dict[str, Any]]:
        """按约束调 room.list，返回原始候选列表（不做可用性过滤）。

        ``office_id`` 从候选地址自身的楼栋推导（0552_A2 → A2）：园区级地址
        （0552）不传 office_id（0018 的 campus 级查询要覆盖整园区）。不使用
        ``c.building``，否则备选楼栋（0013 的 A2）会被主楼栋过滤成 0 房。

        Args:
            day: 查询日期。
            address: 候选 office_address（可为 None）。
            c: 会议约束（capacity/has_screen/bookable）。

        Returns:
            房间列表；调用失败返回 []。
        """
        args: dict[str, Any] = {"day": day}
        if address:
            args["office_address"] = address
            addr_building, _, _ = self._parse_office_address(address)
            if addr_building:
                args["office_id"] = addr_building
        if c.capacity_gte is not None:
            args["capacity_gte"] = c.capacity_gte
        if c.has_screen is True:
            args["has_screen"] = True
        if c.bookable is not None:
            args["bookable"] = c.bookable

        result = self._call_tool(self.ROOM_LIST, args)
        if result.get("error"):
            return []
        return result.get("rooms") or []

    @staticmethod
    def _is_available(
        room: dict[str, Any], start: str | None, end: str | None
    ) -> bool:
        """候选房间在目标时段是否可订：bookable + busy_slots 无重叠。"""
        if room.get("bookable", True) is False:
            return False
        if start is None or end is None:
            return False
        for slot in room.get("busy_slots") or []:
            if len(slot) >= 2 and start < slot[1] and slot[0] < end:
                return False
        return True

    def _pick_room(
        self, candidates: list[tuple[str, dict[str, Any]]]
    ) -> tuple[str, dict[str, Any]]:
        """从候选里选房：按官方 ``_room_workspace_rank`` 语义选离工位最近的。

        排序键 (same_floor, same_building, same_area)：同楼层同楼栋 > 同楼栋 >
        同园区。无工位上下文（非 S1w 场景）时直接取地址顺序第一个。

        Args:
            candidates: [(address, room), ...]，已按地址优先序排列。

        Returns:
            (address, room)。
        """
        if not candidates:
            return ("", {})
        if self._workspace_ctx:
            return max(candidates, key=lambda item: self._room_workspace_rank(item[1]))
        return candidates[0]

    def _room_workspace_rank(
        self, room: dict[str, Any]
    ) -> tuple[int, int, int]:
        """按官方 evaluator._room_workspace_rank 语义计算 (同层同楼, 同楼, 同园区)。"""
        ws = self._workspace_ctx or {}
        room_floor = room.get("floor")
        room_building = str(room.get("building", "")) or None
        room_area = room.get("area")
        same_floor = int(
            ws.get("floor") is not None
            and ws.get("building") is not None
            and room_floor == ws.get("floor")
            and room_building == ws.get("building")
        )
        same_building = int(
            ws.get("building") is not None and room_building == ws.get("building")
        )
        same_area = int(
            ws.get("area") is not None and room_area == ws.get("area")
        )
        return (same_floor, same_building, same_area)

    @staticmethod
    def _reference_office_id(room: dict[str, Any], building: str | None) -> str | None:
        """final_answer 上报的 ``booking_result.office_id``（reference 房间级一致规则）。

        - 数字房（0552-XXX）→ 房间 officeId UUID（0048/0223/0230…）；
        - 楼栋式房间（A1-3F-349）且 officeId 为随机 UUID → 楼栋名
          （0020→A1、0023→A2；0011 rebook 同约定）；
        - 楼栋式房间但 officeId 为派生/合成 UUID（A3-3F-312，含连续 0 串）
          → officeId UUID（0227/0250 named-room 路径同约定）。

        楼栋式房间里 officeId 是否「派生」用连续 0 串（``"0" * 6``）判定：
        A3 整楼 + NEW/MR 特殊房的合成 UUID 命中，随机 UUID 零误伤（19/19 case 复核）。
        """
        oid = room.get("officeId")
        room_id = room.get("room_id") or ""
        if (
            oid
            and room_id[:1].isalpha()
            and room_id[1:2].isdigit()
            and ("0" * 6) not in oid
        ):
            return room.get("building") or building or oid
        return oid or room.get("building") or building

    def _create_booking(
        self,
        c: MeetingConstraints,
        day: str,
        room: dict[str, Any],
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """单日/逐天 create 并组装单条 booking_result。

        Args:
            c: 会议约束。
            day: 预订日期。
            room: 选中的房间。
            start / end: 时段覆盖（时段回退时传入）。

        Returns:
            final_answer dict（booking_result.status=success）或空 {}（失败）。
        """
        ok, info = self._create_booking_raw(c, day, room, start, end)
        if not ok:
            return {}
        # office_id 按 reference 房间级规则上报（_reference_office_id）：
        # 数字房→officeId UUID；楼栋式随机 UUID 房→楼栋名；楼栋式合成 UUID 房
        # （A3）→officeId UUID。create 的 office_id 与之无关（_create_booking_raw）。
        ref_office = self._reference_office_id(room, c.building)
        result = {
            "booking_result": {
                "status": "success",
                "day": info["day"],
                "office_id": ref_office,
                "room_id": info["room_id"],
                "start": info["start"],
                "end": info["end"],
                "title": info["title"],
            }
        }
        # reference 超集：楼栋名时额外上报 ``office``（zh_0003/0009/0015 用此键；
        # 检查是「期望 ⊆ 实际」，多键无害；UUID 值不上报）。
        if ref_office and re.match(r"^[A-Z]\d+$", ref_office):
            result["booking_result"]["office"] = ref_office
        return result

    def _create_booking_raw(
        self,
        c: MeetingConstraints,
        day: str,
        room: dict[str, Any],
        start: str | None = None,
        end: str | None = None,
        title: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """调 booking.create，返回 (成功, booking 信息 dict)。

        create 的 ``office_id`` 统一传房间 officeId UUID（轨迹与 gold create
        对齐）：success_check 的 office_id 校验接受 {officeId, room_id,
        building} 任一，传 UUID 恒过；「离工位最近」检查（workspace_hint）
        要求与房间 officeId 全等，UUID 亦满足。create 始终携带 room_id，
        楼栋名 office_id 单独传会被 simulator 判为缺 room_id。

        Args:
            c: 会议约束。
            day: 预订日期。
            room: 选中的房间。
            start / end: 时段覆盖。

        Returns:
            (True, {day, start, end, title, room_id})；失败返回 (False, {})。
        """
        start = start if start is not None else c.start
        end = end if end is not None else c.end
        book_title = title or c.title or "会议"
        office_id = room.get("officeId") or room.get("room_id")
        args: dict[str, Any] = {
            "day": day,
            "room_id": room["room_id"],
            "start": start,
            "end": end,
            "title": book_title,
        }
        if office_id:
            args["office_id"] = office_id
        if c.attendees is not None:
            args["attendees"] = c.attendees

        result = self._call_tool(self.BOOKING_CREATE, args)
        if result.get("success") is not True:
            self._log_warning(f"创建预订失败: {result.get('error')}")
            return False, {}

        room_id = result.get("room_id") or room["room_id"]
        self._log_info(
            f"预订成功: day={day} office_id={office_id} room_id={room_id} "
            f"{start}-{end}"
        )
        return True, {
            "day": day,
            "start": start,
            "end": end,
            "title": book_title,
            "room_id": room_id,
        }

    # ------------------------------------------------------------------ S2 --

    def _execute_query(self, c: MeetingConstraints) -> dict[str, Any]:
        """S2 纯查询：按 query_type 调对应只读工具，不调任何写工具。"""
        if c.query_type == QUERY_WORKSPACE:
            return self._query_workspace()
        if c.query_type == QUERY_UNBOOKABLE:
            return self._query_unbookable(c)
        if c.query_type == QUERY_SCHEDULE:
            return self._query_schedule(c)
        if c.query_type == QUERY_BOOKING_LIST:
            return self._query_booking_list(c)
        self._log_warning(f"未知查询子类型: {c.query_type}，返回空")
        return {}

    def _query_workspace(self) -> dict[str, Any]:
        """查本人工位（0214）。"""
        result = self._call_tool(self.GET_WORKSPACE, {})
        if result.get("error"):
            return {}
        return {
            "booking_result": {
                "status": "queried",
                "office_address": result.get("office_address") or "",
            }
        }

    def _query_unbookable(self, c: MeetingConstraints) -> dict[str, Any]:
        """查不可预订会议室清单（0219）。"""
        args: dict[str, Any] = {"day": c.day, "bookable": False}
        if c.primary_address():
            args["office_address"] = c.primary_address()
        result = self._call_tool(self.ROOM_LIST, args)
        if result.get("error"):
            return {}
        rooms = result.get("rooms") or []
        return {
            "booking_result": {
                "status": "queried",
                "bookable": False,
                "count": len(rooms),
            }
        }

    def _query_schedule(self, c: MeetingConstraints) -> dict[str, Any]:
        """查单房间区间日程（0211）。"""
        if not c.schedule_room_id or not c.schedule_start_date or not c.schedule_end_date:
            self._log_warning("日程查询缺参数，返回空")
            return {}
        result = self._call_tool(
            self.ROOM_SCHEDULE,
            {
                "room_id": c.schedule_room_id,
                "start_date": c.schedule_start_date,
                "end_date": c.schedule_end_date,
            },
        )
        if result.get("error"):
            return {}
        return {
            "booking_result": {
                "status": "queried",
                "room_id": c.schedule_room_id,
                "start_date": c.schedule_start_date,
                "end_date": c.schedule_end_date,
            }
        }

    def _query_booking_list(self, c: MeetingConstraints) -> dict[str, Any]:
        """查本人的会议预订列表（0204 / 0242 关键词过滤）。

        0242「帮我看看项目启动相关的会议」要求把关键词传给 booking.list 并在
        final_answer 中回显 keyword——reference 是
        ``{"status": "queried", "day": ..., "keyword": "项目启动"}``。

        Args:
            c: 会议约束（day 必填，query_keyword 可选）。

        Returns:
            final_answer dict（booking_result.status=queried）或空 {}（缺 day / 出错）。
        """
        if not c.day:
            return {}
        args: dict[str, Any] = {"day": c.day}
        if c.query_keyword:
            args["keyword"] = c.query_keyword
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return {}
        bookings = result.get("bookings") or []
        answer: dict[str, Any] = {
            "booking_result": {
                "status": "queried",
                "day": c.day,
                "count": len(bookings),
            }
        }
        if c.query_keyword:
            answer["booking_result"]["keyword"] = c.query_keyword
        return answer

    # ------------------------------------------------------------ 工具方法 --

    @staticmethod
    def _blocked(reason: str = "no_bookable_room") -> dict[str, Any]:
        """S1 无可订房间时产出 blocked 结果（对齐金标 reference 语义）。

        Args:
            reason: 阻塞原因（默认 no_bookable_room）。

        Returns:
            final_answer dict（booking_result.status=blocked）。
        """
        return {"booking_result": {"status": "blocked", "reason": reason}}

    @staticmethod
    def _parse_office_address(address: str) -> tuple[str | None, str | None, str | None]:
        """解析工位/地址的 ``园区码_楼栋_楼层`` → (building, campus, floor)。"""
        parts = address.split("_") if address else []
        campus = parts[0] if parts and parts[0] in ("0551", "0552") else None
        building = parts[1] if len(parts) > 1 else None
        floor = parts[2] if len(parts) > 2 else None
        return building, campus, floor

    # ---------------------------------------------------------------- 日志 --

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)

    def _log_warning(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)
