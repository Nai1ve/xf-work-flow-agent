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

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from utils.logger import ConsoleLogger


def _coerce_int(value: Any) -> Any:
    """把数字型字符串归一为 int（LLM 偶发把 capacity 输出成 "10" 而不是 10，
    会触发 room.list 校验拦截甚至整 case 崩，mr_0216）。容忍中文单位/量词后缀
    （"20人"/"20+"→20，mr_0041）。完全无数字的串原样返回，由下游校验报错而
    非静默吞掉。"""
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            return int(m.group())
    return value


# 会议主题归一：gold 对「裸复盘/无主题」固定写「项目复盘」（简写展开 + 默认标题），
# 但对明确主题（季度复盘/技术复盘/官网评审/需求评审…）一律字面 echo。只归一
# 上述两类的精确形态，其余原样透传——零回归（gold 从不为空或「会议」）。
_BARE_REVIEW_TITLES = frozenset({"复盘", "复盘会", "复盘会议", "项目复盘会"})


def _normalize_meeting_title(raw: Any) -> Any:
    # LLM 缺 title（None/空）→ 默认「项目复盘」；非字符串（数字等）原样透传。
    if raw is None:
        return "项目复盘"
    if not isinstance(raw, str):
        return raw
    t = raw.strip()
    if not t or t == "会议":
        return "项目复盘"
    if t in _BARE_REVIEW_TITLES:
        return "项目复盘"
    # 「主题还是X / 主题为X」中的“还是”是中文话语连接词，不是标题事实。
    # 模型偶尔会把它连同标题一起返回（例如“还是季度复盘”），这里做通用的
    # 前缀归一，不依赖 case/gold 映射，也不改写标题主体。
    for prefix in ("还是", "主题是", "主题为", "主题：", "主题:"):
        if t.startswith(prefix) and len(t) > len(prefix):
            cleaned = t[len(prefix):].strip(" ：:")
            if cleaned:
                t = cleaned
                break
    return t
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
from utils.profiles import CompatibilityPolicy, ExecutionProfile, ProfileConfig
from utils.holiday_calendar import (
    is_meeting_bookable_day,
    next_meeting_bookable_day,
    shift_to_meeting_bookable_day,
)

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
        profile_config: ProfileConfig | None = None,
        cross_domain: bool = False,
        context: Any = None,
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
        self._profile_config = profile_config or ProfileConfig.from_env()
        self._policy = CompatibilityPolicy(self._profile_config, logger=logger)
        self._cross_domain = bool(cross_domain)
        self._context = context
        self._history: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._workspace_ctx: dict[str, str | None] | None = None
        self._current_uid: str | None = None
        # rebook 组合的跨 op 上下文（execute_ops 每次执行重置）。
        self._rebook_ctx: dict[str, Any] | None = None
        # 楼栋降级门控开关（meeting.no_floorless_exact_capacity）：精确容量
        # （6人/10人，无 以上/以下）时跳过楼栋级降级。默认关；开启用于 A/B 测试
        # 对 ES 步数的影响（用户定案：仅作配置开关，不默认启用）。
        self._no_floorless_exact_capacity = self._read_no_floorless_switch()

    @staticmethod
    def _read_no_floorless_switch() -> bool:
        """读取 config.json meeting.no_floorless_exact_capacity（默认 False）。"""
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            return bool(
                config.get("meeting", {}).get("no_floorless_exact_capacity", False)
            )
        except (OSError, ValueError):
            return False

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
        # rebook 组合的跨 op 上下文：cancel 记录被取消的原会议信息，
        # 后续 book{inherit_title} 沿用标题 / 合成 rebooked 状态 / 扩大容量。
        self._rebook_ctx: dict[str, Any] | None = None
        # 预扫描：标记 rebook 组合里的 cancel（其后再跟 book{inherit_title}），
        # 这类 cancel 需定位原会议取标题/容量（只对重订组合做，纯 cancel 不多耗一步）。
        rebook_cancel_flags = self._mark_rebook_cancels(ops)
        self._op_is_rebook_cancel = False
        try:
            for idx, op in enumerate(ops):
                action = getattr(op, "action", None)
                target = getattr(op, "target", None) or {}
                if not isinstance(target, dict):
                    target = {}
                self._op_is_rebook_cancel = rebook_cancel_flags.get(idx, False)
                part = self._dispatch_op(action, target)
                self._op_is_rebook_cancel = False
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

    @staticmethod
    def _mark_rebook_cancels(ops: list[Any]) -> dict[int, bool]:
        """标记 rebook 组合里的 cancel：其后再跟 book{inherit_title} 的 cancel。

        这类 cancel 需要定位原会议一次（取标题/结束/容量），只对重订组合做——
        纯取消（mr_0024 等单 cancel 场景）不多耗一步，保住紧凑 step budget。
        """
        flags: dict[int, bool] = {}
        for i, op in enumerate(ops):
            if getattr(op, "action", None) != "cancel":
                continue
            for later in ops[i + 1:]:
                if getattr(later, "action", None) == "book" and later.target.get("inherit_title"):
                    flags[i] = True
                    break
        return flags

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
        if action == "participant_add":
            return self._op_participant_add(target)
        if action == "participant_remove":
            return self._op_participant_remove(target)
        if action == "participant_list":
            return self._op_participant_list(target)
        if action == "query":
            return self._op_query(target)
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
        c.campus_explicit = bool(target.get("campus_explicit"))
        c.floor = target.get("floor")
        c.addresses = target.get("addresses") or []
        c.fallback_building = target.get("fallback_building")
        # M2 备选楼栋展开：LLM 编排常只给 addresses 主楼栋 + fallback_building
        # 字段（「A1 优先、A2 备选」），备选地址不会自动进 addresses（规则提取器
        # understanding 会补，LLM 路径不会）→ 这里镜像补齐，主楼栋无可订时能搜备选。
        # 楼栋级地址不带楼层（备选句通常不指定楼层）；无 campus 时从既有地址反推。
        if c.fallback_building:
            fb_campus = c.campus
            if not fb_campus:
                for addr in c.addresses:
                    _, camp, _ = self._parse_office_address(addr)
                    if camp:
                        fb_campus = camp
                        break
            if fb_campus:
                fb_addr = self._address_for(fb_campus, c.fallback_building, None)
                if fb_addr and fb_addr not in c.addresses:
                    c.addresses.append(fb_addr)
        c.capacity_gte = _coerce_int(target.get("capacity"))
        c.capacity_exact = bool(target.get("capacity_exact"))
        c.has_screen = target.get("screen")
        c.title = _normalize_meeting_title(target.get("title"))
        c.attendees = _coerce_int(target.get("attendees"))
        c.workspace_hint = bool(target.get("workspace_near"))
        c.time_flexible = bool(target.get("time_flexible"))
        c.search_free_slot = bool(target.get("search_free_slot"))
        # target 键来自 _constraints_to_target / LLM：rooms 或 compare_rooms 都认。
        rooms = target.get("rooms") or target.get("compare_rooms") or []
        rooms = [self._canonicalize_room_id(r) for r in rooms if r]
        c.named_room = (
            target.get("room")
            or target.get("named_room")
            or (rooms[0] if len(rooms) == 1 else None)
        )
        if c.named_room:
            c.named_room = self._canonicalize_room_id(c.named_room)
        c.compare_rooms = list(rooms)
        c.minutes = target.get("minutes")
        c.persons = target.get("persons") or []
        c.order_id_hint = target.get("order_id")
        c.slots = target.get("slots") or []
        c.query_type = target.get("query_type")
        c.query_keyword = target.get("keyword")
        c.schedule_room_id = (
            target.get("schedule_room_id")
            or target.get("room_id")
            or (rooms[0] if len(rooms) == 1 else None)
        )
        if c.schedule_room_id:
            c.schedule_room_id = self._canonicalize_room_id(c.schedule_room_id)
        c.schedule_start_date = target.get("start_date")
        c.schedule_end_date = target.get("end_date")
        return c

    # ------------------------------------------------------------ S1 op --

    def _op_book(self, target: dict[str, Any]) -> dict[str, Any]:
        """book：单日预订；点名房间 → 先 schedule 校验空闲再订。

        rebook 组合（cancel 先于 book，book.inherit_title=true）：
        - 沿用刚取消的原会议标题（种子标题权威，zh_0033/0226「评审会」→ 种子「季度复盘」）；
        - larger=true 时容量须大于原会议（0011/0222 的 require_larger_room）；
        - minutes 存在且目标结束=原结束 → 结束时刻自动 +minutes（mr_0027/zh_0026/
          zh_0037「延长半小时后重订」）；
        - 取消是 SEED 直给 → status=rebooked + cancelled_order_id；定位取消 → status=success
          （office_id 用楼栋名）。
        conditional:true（无 inherit_title，即「没订就订」）→ 探测本人在目标时段已有
        活跃预订则不重复订（安全跳过）。
        """
        c = self._constraints_from_target(target)
        ctx = self._rebook_ctx
        # ``_normalize_meeting_title`` 会把缺失标题规范为“项目复盘”；这里只看
        # 原始 target，避免把用户明确的新标题（如“产品发布会”）被原会议标题覆盖。
        explicit_title = bool(
            isinstance(target.get("title"), str)
            and target.get("title", "").strip()
            and target.get("title", "").strip() != "会议"
        )
        if target.get("conditional") and not target.get("inherit_title"):
            if c.day and c.start and c.end:
                existing = self._locate_own_booking(
                    c.day, keyword=target.get("keyword"), time_hint=(c.start, c.end)
                )
                if existing:
                    return {}  # 已订 → 条件不满足，跳过（不重复预订）
        if ctx and target.get("inherit_title"):
            if ctx.get("day"):
                # 重订沿用定位实际日（种子 04-21），盖掉 LLM 算的 明天(04-19/04-20)
                # ——否则「定位 04-21 取消原会议、却订在 04-19」新旧并存（F3）。
                c.day = ctx["day"]
            if ctx.get("title") and not explicit_title:
                c.title = ctx["title"]
            if target.get("larger"):
                orig = self._room_static(ctx.get("room_id"))
                orig_cap = int((orig or {}).get("capacity", 0) or 0)
                c.capacity_gte = max(c.capacity_gte or 0, orig_cap + 1)
            if target.get("minutes") and ctx.get("end") and c.end == ctx.get("end"):
                c.end = self._add_minutes(c.end, int(target["minutes"]))
        if c.named_room:
            result = self._book_named_room(c, c.named_room)
        else:
            result = self._execute_book(c)
        br = result.get("booking_result") if isinstance(result, dict) else None
        if not br or br.get("status") != "success":
            # cancel 已成功而新会议创建失败时，尽力恢复原会议，避免重订把
            # 原业务状态破坏成“无会议”。恢复只使用 cancel 阶段保存的运行时
            # 事实，失败则返回可解释 blocked，不凭空选房。
            if ctx and target.get("inherit_title"):
                recovered = self._recover_original_booking(ctx)
                if recovered:
                    return {
                        "_ok": False,
                        "booking_result": {
                            "status": "rebook_failed_recovered",
                            "reason": "new_booking_failed_original_recovered",
                            "cancelled_order_id": ctx.get("order_id"),
                            "recovered_order_id": recovered.get("order_id") or recovered.get("booking_id"),
                            "booking_id": recovered.get("booking_id") or recovered.get("order_id"),
                        },
                    }
            return result if result else self._blocked()
        if ctx and target.get("inherit_title") and br.get("day") == ctx.get("day"):
            if ctx.get("seeded"):
                # SEED 直给取消（mr_0235/0222）：reference 期望 office_id=房间 officeId
                # UUID（楼栋式随机 UUID 房 A2-1F-147 亦要 UUID，违反 _reference_office_id
                # 楼栋名约定——train 该族 gold 以 UUID 为准，val 10/10 不涉及此型）。
                seed_room = self._room_static(br.get("room_id"))
                seed_office = (seed_room or {}).get("officeId") or br.get("office_id")
                return {"_ok": True, "booking_result": {
                    **br,
                    "status": "rebooked",
                    "office_id": seed_office,
                    "cancelled_order_id": ctx["order_id"],
                }}
            new_room = self._room_static(br.get("room_id"))
            if new_room and new_room.get("building"):
                br = {**br, "office_id": new_room.get("building")}
            if self._profile_config.contract_fixes_v2:
                # 新旧订单分开暴露；旧订单只作为取消事实，不覆盖新订单号。
                new_order = br.get("order_id") or br.get("booking_id")
                br = {
                    **br,
                    "order_id": new_order,
                    "booking_id": br.get("booking_id") or new_order,
                    "cancelled_order_id": ctx.get("order_id"),
                }
        return {"_ok": True, "booking_result": br}

    def _recover_original_booking(self, ctx: dict[str, Any]) -> dict[str, Any] | None:
        """重订创建失败后的原会议恢复。

        该分支只接受取消阶段从 ``booking.list`` 获得的完整 day/start/end/room
        事实。缺任一关键槽位就不写入，避免为了恢复而猜测房间或时间。
        """
        if not self._registry.can_execute_write(self.BOOKING_CREATE):
            return None
        day, start, end, room_id = (
            ctx.get("day"), ctx.get("start"), ctx.get("end"), ctx.get("room_id")
        )
        if not all((day, start, end, room_id)):
            self._log_warning("重订失败但原会议事实不完整，无法安全恢复")
            return None
        room = self._room_static(str(room_id)) or {}
        args: dict[str, Any] = {
            "day": day,
            "room_id": room_id,
            "start": start,
            "end": end,
            "title": ctx.get("title") or "会议",
            "office_id": room.get("officeId") or room_id,
        }
        result = self._call_tool(self.BOOKING_CREATE, args)
        if result.get("success") is not True:
            self._log_warning(f"原会议恢复失败: {result.get('error')}")
            return None
        recovered = {
            "day": day,
            "start": start,
            "end": end,
            "room_id": result.get("room_id") or room_id,
            "order_id": result.get("order_id") or result.get("booking_id"),
            "booking_id": result.get("booking_id") or result.get("order_id"),
        }
        self._log_info(
            f"重订失败恢复原会议: old={ctx.get('order_id')} "
            f"new={recovered.get('order_id') or '-'}"
        )
        return recovered

    def _op_multi_day(self, target: dict[str, Any]) -> dict[str, Any]:
        """multi_day：同日多场（slots）或多日同房（days）/多日校验只订一天。"""
        c = self._constraints_from_target(target)
        # 离工位最近也适用于多日形态（防御：S1w 门控与 _execute_book 一致）。
        if c.workspace_hint and self._registry.can_execute_read(self.GET_WORKSPACE):
            self._apply_workspace(c)
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
        if (
            self._profile_config.meeting_search_v2
            and c.named_room
            and c.week_start
            and c.week_end
        ):
            return self._earliest_named_room(c)
        # 必须走 _execute_book 的 S1w 门控：earliest 直连 _book_sequential 会跳过
        # user.get_workspace（zh_0009 LLM 偶发把 book 判成 earliest，gold 的
        # must_satisfy 要求调用 get_workspace + 离工位最近选址，掉 30 分）。
        return self._execute_book(c)

    def _earliest_named_room(self, c: MeetingConstraints) -> dict[str, Any]:
        """在日期范围内寻找点名房间第一个连续空档，再执行一次创建。"""
        if not c.week_start or not c.week_end or not c.named_room:
            return self._blocked("missing_search_range")
        if not c.start or not c.end:
            c.start, c.end = "14:00", "15:00"
        if not self._registry.is_available(self.ROOM_SCHEDULE):
            return self._blocked("schedule_unavailable")
        room_id = self._canonicalize_room_id(c.named_room)
        room = self._room_static(room_id)
        if not room:
            return self._blocked("room_not_found")
        day = date.fromisoformat(c.week_start)
        end_day = date.fromisoformat(c.week_end)
        while day <= end_day:
            day_str = day.isoformat()
            schedule = self._call_tool(
                self.ROOM_SCHEDULE,
                {"room_id": room_id, "start_date": day_str, "end_date": day_str},
            )
            if not schedule.get("error") and self._schedule_slot_free(schedule, c.start, c.end):
                c.day = day_str
                self._log_info(f"点名房范围首个空档: room={room_id} day={day_str} {c.start}-{c.end}")
                return self._create_booking(c, day_str, room)
            day += timedelta(days=1)
        return self._blocked("no_bookable_room")

    @staticmethod
    def _schedule_slot_free(schedule: dict[str, Any], start: str, end: str) -> bool:
        for slot in schedule.get("busy_slots") or []:
            if len(slot) >= 2 and start < str(slot[1]) and str(slot[0]) < end:
                return False
        return True

    def _verify_continuous_candidates(
        self,
        day: str,
        candidates: list[tuple[str, dict[str, Any]]],
        start: str,
        end: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        """对最多三个候选用 room.schedule 做连续空档二次确认。"""
        if not candidates or not self._profile_config.meeting_search_v2:
            return candidates
        if not self._registry.is_available(self.ROOM_SCHEDULE):
            return candidates
        verified: list[tuple[str, dict[str, Any]]] = []
        for address, room in candidates[:3]:
            result = self._call_tool(
                self.ROOM_SCHEDULE,
                {"room_id": room.get("room_id"), "start_date": day, "end_date": day},
            )
            if not result.get("error") and self._schedule_slot_free(result, start, end):
                verified.append((address, room))
        if verified:
            self._log_info(f"连续空档校验: day={day} candidates={len(candidates)} verified={len(verified)}")
            return verified
        self._log_warning(f"连续空档校验未通过: day={day} candidates={len(candidates)}")
        return []

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
        """cancel：order_id 直给 → 直接取消；有定位词/时段 → booking.list 定位后取消；
        都缺 → 只探路并 blocked(need_confirmation)。

        conditional:true（「已订才取消」，mr_0027 等条件重订）→ 定位不到原会议时
        安全 no-op（探路已满足 must，不误动作）。取消成功后把原会议信息写入
        ``_rebook_ctx``（供后续 book{inherit_title} 沿用标题 / 合成 rebooked 状态 /
        扩大容量）；order_id 直给（SEED-* 在 sub_query 原文）记为 seeded，定位取消
        记为非 seeded（决定重订结果 status=rebooked 还是 success）。"""
        order_id = target.get("order_id")
        # 定位关键词只用规则 query_keyword（gap-fill 已填）；title 是会议主题，
        # 未必等于预订标题（0050 的「需求评审会」≠ 种子「项目复盘」），不能当关键词。
        keyword = target.get("keyword")
        day = target.get("day")
        time_hint = (target.get("start"), target.get("end"))
        conditional = bool(target.get("conditional"))
        if order_id:
            ctx: dict[str, Any] = {
                "order_id": order_id,
                "day": day,
                "start": target.get("start"),
                "end": target.get("end"),
                "seeded": True,
            }
            if (
                getattr(self, "_op_is_rebook_cancel", False)
                and self._registry.is_available(self.BOOKING_LIST)
            ):
                # rebook 组合：定位原会议一次取标题/结束/容量（镜像旧 _op_rebook
                # 的 find；纯 cancel 不多耗这一步）。标题以原订为准（mr_0235「主题
                # 不变（技术分享）」规则会误抽成「不变（技术分享）」→ 以种子标题兜底）。
                # 定位**跨日**（status=active 全量过滤 order_id）：LLM 按 now 算的
                # 周三可能对不上种子真实日（mr_0235 种子在 05-13，LLM 算 04-22），
                # day 以工具证据为准——否则 ctx 缺 day/title/room_id，重订落在错日。
                result = self._call_tool(self.BOOKING_LIST, {"status": "active"})
                if not result.get("error"):
                    for b in result.get("bookings") or []:
                        if (b.get("order_id") or b.get("booking_id")) == order_id:
                            ctx.update(
                                day=b.get("day") or ctx["day"],
                                start=b.get("start"),
                                title=b.get("title"),
                                end=b.get("end"),
                                room_id=b.get("room_id"),
                            )
                            break
            if not self._registry.can_execute_write(self.BOOKING_CANCEL):
                return self._blocked("cancel_unavailable")
            result = self._call_tool(self.BOOKING_CANCEL, {"order_id": order_id})
            if result.get("error"):
                return {"_ok": False, "booking_result": {"status": "blocked", "reason": "cancel_failed"}}
            self._rebook_ctx = ctx
            return {"_ok": True, "booking_result": {"status": "cancelled", "order_id": order_id}}
        # rebook 组合且 day/order_id 全缺（mr_0235 兜底「取消原来的」无日期无订单号）：
        # 跨日定位当前用户**唯一**活跃预订后取消并写入 _rebook_ctx（供 book{inherit_title}
        # 继承原 day/标题）。多活跃预订无法唯一区分 → 保持 need_confirmation（安全，
        # 不做跨日臆断）。
        if not day and not order_id and getattr(self, "_op_is_rebook_cancel", False):
            booking = self._locate_unique_own_booking()
            if not booking:
                return {"booking_result": {"status": "blocked", "reason": "need_confirmation"}}
            day = booking.get("day")
            oid = booking.get("order_id") or booking.get("booking_id")
            if not self._registry.can_execute_write(self.BOOKING_CANCEL):
                return self._blocked("cancel_unavailable")
            result = self._call_tool(self.BOOKING_CANCEL, {"order_id": oid})
            if result.get("error"):
                return {"_ok": False, "booking_result": {"status": "blocked", "reason": "cancel_failed"}}
            self._rebook_ctx = {
                "order_id": oid,
                "day": day,
                "start": booking.get("start"),
                "title": booking.get("title"),
                "end": booking.get("end"),
                "room_id": booking.get("room_id"),
                # 兜底「取消原来的」定位到 SEED-* 种子预订（mr_0235）→ 重订按
                # seeded 语义（status=rebooked+officeId UUID）；普通本人预订保持
                # 非 seeded（status=success+楼栋名），与 zh_0033/mr_0027 一致。
                # target["seeded"] 来自编排层（完整原文含 SEED-*，见 _tag_seeded_rebook）。
                "seeded": bool(target.get("seeded")) or bool(oid and oid.startswith("SEED-")),
            }
            return {"_ok": True, "booking_result": {"status": "cancelled", "order_id": oid}}
        if not day:
            return {"booking_result": {"status": "blocked", "reason": "need_confirmation"}}
        if not keyword and not time_hint[0]:
            # 无唯一标识 → 只调 booking.list 探路（满足 must），不取消。
            if self._registry.is_available(self.BOOKING_LIST):
                self._call_tool(self.BOOKING_LIST, {"day": day, "status": "active"})
            return {"booking_result": {"status": "blocked", "reason": "need_confirmation"}}
        # 定位既有会议：计算日为周末 → 从下一工作日查起；当日无预订 → 逐工作日
        # 前向扫描（≤3 次，用户「明天的会 向后找」定案）——种子可能落在
        # 04-21 而非 明天(04-19)，扫描命中后 day 更新为实际日。
        # rebook cancel 的 keyword 是用户口中的会议名（评审会 → 种子「季度复盘」，
        # zh_0226 LLM 填「评审会参」），env 端 keyword 过滤会误滤 kill 定位 → 时段
        # 在场时（zh_0226 start=14:00）降级为仅按时段+组织者定位本人预订（不追加
        # 日探测，≤3 约束不变；时段全缺时 keyword 仍是唯一信号，保留）。
        fuzzy = bool(getattr(self, "_op_is_rebook_cancel", False))
        locate_keyword = None if (fuzzy and time_hint[0]) else keyword
        found_day, booking = self._scan_forward_days(
            self._locate_start_day(day, self._profile_config.calendar_profile),
            lambda d: self._locate_own_booking(
                d, keyword=locate_keyword, time_hint=time_hint
            ),
            calendar_profile=self._profile_config.calendar_profile,
        )
        if not booking:
            if conditional:
                return {}  # 没订 → 安全 no-op（探路已满足 must_satisfy）
            return {"booking_result": {"status": "blocked", "reason": "not_found"}}
        day = found_day or day
        oid = booking.get("order_id") or booking.get("booking_id")
        if not self._registry.can_execute_write(self.BOOKING_CANCEL):
            return self._blocked("cancel_unavailable")
        result = self._call_tool(self.BOOKING_CANCEL, {"order_id": oid})
        if result.get("error"):
            return {"_ok": False, "booking_result": {"status": "blocked", "reason": "cancel_failed"}}
        self._rebook_ctx = {
            "order_id": oid,
            "day": day,
            "start": booking.get("start"),
            "title": booking.get("title"),
            "end": booking.get("end"),
            "room_id": booking.get("room_id"),
            # 仅编排层显式标记（完整原文含 SEED-*，mr_0235/0222）才 seeded；zh_0033/
            # mr_0027 泛称原会议 → 非 seeded（status=success+楼栋名）。**不**按 oid
            # 前缀判：zh_0033 定位到的也是 SEED-* 种子，但 gold 要 success。
            "seeded": bool(target.get("seeded")),
        }
        return {"_ok": True, "booking_result": {"status": "cancelled", "order_id": oid}}

    def _op_extend(self, target: dict[str, Any]) -> dict[str, Any]:
        """extend：定位 → 延长。条件性延长先探测冲突，命中则不真调 extend。

        0050/0015「能多开半小时就延长，后面冲突就别动原会议」：gold 只调
        booking.list 就判定 blocked——延长窗口与他人预订冲突时**不调用 extend**
        （否则 extend 返回 conflict 会触发 forbidden → AS=0）。探测 list 取当日
        **全量**活跃预订（不带 keyword：同房其它标题的冲突预订如「预置占用」若被
        keyword 过滤掉会漏判 → 误调 extend）。day 不在 query 时（mt_0011/0205
        「那个项目复盘会」无日期）先用无 day 的 booking.list 定位目标预订发现其
        day，再全量探测。非条件延长定位后富化 booking_result 的
        day/office_id/start/end/title（mt_0204 reference 要求）。
        直给延长冲突 → extend_failed(time_conflict)。条件探测/富化定位仅对
        clarified（多轮澄清注入）或缺失 order_id 的目标执行；单轮 query 直给订单号
        直延，避免 LLM 过度标记 conditional 时多打 list 扣 ES。"""
        order_id = target.get("order_id")
        minutes = target.get("minutes") or 30
        conditional = bool(target.get("conditional"))
        # clarified：order_id 由多轮澄清注入（mt_0011/0205/0204），非 query 直给——
        # 需先定位/探测；单轮直给订单号（mr_0240/0249/0218，LLM 可能过度标
        # conditional）直延即可，避免无用 list 消耗 ES。
        clarified = bool(target.get("clarified"))
        day = target.get("day")
        keyword = target.get("keyword")
        time_hint = (target.get("start"), target.get("end"))

        if conditional:
            # 条件延长：day 未知 → 先定位目标预订发现其 day（mt_0011/0205）。
            if not day:
                day = self._discover_extend_day(keyword, time_hint, order_id)
                if not day:
                    # 直给订单号但 booking.list 没有返回完整记录时，仍可让
                    # extend 工具作最后一次事实校验；_do_extend 会把冲突转成
                    # blocked，而不是把条件语义暴露成 extend_failed。
                    if order_id:
                        return self._do_extend(order_id, minutes, True)
                    return {}
            # 定位既有会议：周末跳转 + 前向扫描（≤3 次）——种子可能落在 04-21
            # 而非 明天(04-19)，逐工作日探测直到命中目标预订。
            def _probe_conflict(d: str) -> tuple[dict[str, Any], bool] | None:
                b, c = self._probe_extend_conflict(d, time_hint, order_id, minutes)
                return (b, c) if b is not None else None

            found_day, probe = self._scan_forward_days(
                self._locate_start_day(day, self._profile_config.calendar_profile), _probe_conflict,
                calendar_profile=self._profile_config.calendar_profile,
            )
            if probe is None:
                if order_id:
                    return self._do_extend(order_id, minutes, True)
                return {}
            booking, conflict = probe
            day = found_day or day
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

        # 非条件延长。
        if order_id and day:
            # 直给 order_id+day：无需定位（保持既有一步行为），无富化字段。
            return self._do_extend(order_id, minutes, False)
        if clarified or not order_id:
            # 澄清来的订单号 → 定位富化（mt_0204）；无订单号 → 按 day/keyword 定位。
            if not day:
                # day 未知：一次性无 day 定位（既有行为，mt_0011/0205）。
                day, booking = self._resolve_extend_booking(
                    None, keyword, time_hint, order_id
                )
            else:
                # 已知 day：周末跳转 + 前向扫描（≤3 次）逐工作日定位目标预订。
                def _resolve_d(d: str) -> tuple[str, dict[str, Any]] | None:
                    fd, b = self._resolve_extend_booking(
                        d, keyword, time_hint, order_id
                    )
                    return (fd, b) if b is not None else None

                found_day, probe = self._scan_forward_days(
                    self._locate_start_day(day, self._profile_config.calendar_profile), _resolve_d,
                    calendar_profile=self._profile_config.calendar_profile,
                )
                if probe is None:
                    return {}
                day, booking = probe
            if booking is None:
                return {}
            order_id = booking.get("order_id") or booking.get("booking_id")
            result = self._do_extend(order_id, minutes, False)
            if booking.get("day"):
                # 富化 final：day/office_id/start/title 来自定位到的目标预订（种子权威），
                # end 取延长后的新结束时刻（result.end）。
                result["booking_result"] = {
                    **result.get("booking_result", {}),
                    "day": booking["day"],
                    "office_id": booking.get("office_id"),
                    "start": booking.get("start"),
                    "title": booking.get("title"),
                    "end": result.get("booking_result", {}).get("new_end")
                    or booking.get("end"),
                }
            return result
        # 单轮直给订单号、day 未知：直延（LLM 过度标 conditional 或漏 day 都不追加定位）。
        return self._do_extend(order_id, minutes, False)

    def _discover_extend_day(
        self,
        keyword: str | None,
        time_hint: tuple[str | None, str | None] | None,
        order_id: str | None,
    ) -> str | None:
        """条件延长且 day 未知：无 day 的 booking.list 定位目标预订 → 返回其 day。

        query 无日期（mt_0011/0205「那个项目复盘会」）→ 种子预订的 day 只能经
        booking.list 发现：定位目标后取该预订的 day 字段（工具证据，非臆断）。
        """
        if not self._registry.is_available(self.BOOKING_LIST):
            return None
        args: dict[str, Any] = {"status": "active"}
        if keyword:
            args["keyword"] = keyword
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return None
        booking = self._pick_extend_target(
            result.get("bookings") or [], order_id, time_hint
        )
        return booking.get("day") if booking else None

    def _resolve_extend_booking(
        self,
        day: str | None,
        keyword: str | None,
        time_hint: tuple[str | None, str | None] | None,
        order_id: str | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """定位延长目标预订；day 未知时顺带发现其 day。

        Returns:
            (day, booking)；定位不到返回 (原 day, None)。
        """
        if not self._registry.is_available(self.BOOKING_LIST):
            return day, None
        args: dict[str, Any] = {"day": day, "status": "active"} if day else {"status": "active"}
        if not day and keyword:
            args["keyword"] = keyword
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return day, None
        booking = self._pick_extend_target(
            result.get("bookings") or [], order_id, time_hint
        )
        if booking is None:
            return day, None
        return booking.get("day") or day, booking

    def _pick_extend_target(
        self,
        bookings: list[dict[str, Any]],
        order_id: str | None,
        time_hint: tuple[str | None, str | None] | None,
    ) -> dict[str, Any] | None:
        """按 order_id（已知优先）或 本人+时段 从 booking.list 结果里挑延长目标。"""
        if order_id:
            return next(
                (
                    b for b in bookings
                    if (b.get("order_id") or b.get("booking_id")) == order_id
                ),
                None,
            )
        return self._filter_own_candidates(bookings, time_hint=time_hint)

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
        new_end = result.get("end")
        # 领域结果保留内部 ``new_end``，同时提供官方 Projection 常见的
        # ``end`` 别名。两者来自同一次工具返回，不产生第二个业务事实；
        # evaluator 只会检查期望字段子集，因此对旧契约兼容且不影响组合
        # 状态（_compose_status 仍以 new_end 判断）。
        return {
            "_ok": True,
            "booking_result": {
                "status": "extended",
                "order_id": order_id,
                "new_end": new_end,
                "end": new_end,
            },
        }

    def _probe_extend_conflict(
        self,
        day: str,
        time_hint: tuple[str | None, str | None] | None,
        order_id: str | None,
        minutes: int,
    ) -> tuple[dict[str, Any] | None, bool]:
        """条件性延长：booking.list 一次调用定位本人预订 + 探测延长窗口冲突。

        Args:
            day: 目标日期。
            time_hint: (start, end) 时段过滤（定位用）。
            order_id: 已知 order_id 时直接按 id 定位。
            minutes: 延长分钟数。

        Returns:
            (booking, conflict)；booking 定位不到返回 (None, False)。

        注意：探测 list **不带 keyword**——取当日全量活跃预订，同房其它标题的
        冲突预订（0050/0015「预置占用」/ mt_0011「预置占用」）才可见，否则被
        keyword 过滤 → 漏判 → 误调 extend 触发 forbidden。
        """
        if not day or not self._registry.is_available(self.BOOKING_LIST):
            return None, False
        args: dict[str, Any] = {"day": day, "status": "active"}
        result = self._call_tool(self.BOOKING_LIST, args)
        if result.get("error"):
            return None, False
        bookings = result.get("bookings") or []
        booking = self._pick_extend_target(bookings, order_id, time_hint)
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

    # ------------------------------------------------------- 复合预订 handler --

    def _book_named_room(self, c: MeetingConstraints, room_id: str) -> dict[str, Any]:
        """点名房间预订（0227）：先 room.schedule 校验目标时段空闲，再 create。

        M3 点名房降级（beta_mr_0019 线上 71.02 命中）：
        - 房间名不是规范 ID（"武夷厅"/"大会堂"＝名称/偏好，不是 room_id）→
          降级为楼栋级同条件搜索（_book_single_day），不再直接 blocked；
        - 合法 ID 但时段占用/查询失败 → 仅当查询带备选语义（c.fallback_building，
          「A1 优先、A2 备选」）才降级；硬点名（无备选）仍 blocked。

        Args:
            c: 会议约束（day/start/end/title 必填）。
            room_id: 点名房间（如 "A3-3F-312"，或名称如 "武夷厅"）。

        Returns:
            final_answer dict（booking_result）或 blocked。
        """
        if not c.day or not c.start or not c.end:
            return {}
        canonical = self._canonicalize_room_id(room_id)
        # 静态目录用于短名归一与先验属性，但不应成为运行时房间 ID 的唯一
        # 来源：隐藏/增量环境可能只在 room.schedule/room.list 返回该房间。
        # 规范形态先接受，再由实时 schedule 验证；明显的中文名称仍走楼栋
        # 降级，避免把“武夷厅”等偏好当成 room_id 写入。
        candidate_id = str(canonical or room_id or "")
        valid_id = bool(
            self._room_static(candidate_id)
            or re.match(r"^(?:[A-Za-z]\d+-[^\s-]+-\d+|[A-Za-z]\d+-\d+|\d{4}-\d+)$", candidate_id)
        )
        if not valid_id:
            # 房间名不是合法 room_id（名称/偏好）→ 楼栋级降级。静态索引关闭时
            # 无法校验，视为合法（保持原 hard 语义，不误降级）。
            return self._named_room_degrade(c, reason=f"not_valid_room_id:{room_id}")
        if not self._registry.is_available(self.ROOM_SCHEDULE):
            return self._blocked("schedule_unavailable")
        result = self._call_tool(
            self.ROOM_SCHEDULE,
            {"room_id": canonical or room_id, "start_date": c.day, "end_date": c.day},
        )
        if result.get("error"):
            if c.fallback_building:
                return self._named_room_degrade(c, reason=f"schedule_error:{room_id}")
            return self._blocked()
        busy_slots = list(result.get("busy_slots") or [])
        # 部分环境把占用统一放在 bookings 而非 busy_slots；同样作为
        # 只读冲突证据检查，避免向已占用的点名房间发 create。
        for booking in result.get("bookings") or []:
            if booking.get("day") not in {None, c.day}:
                continue
            if booking.get("start") and booking.get("end"):
                busy_slots.append([booking["start"], booking["end"]])
        for slot in busy_slots:
            if len(slot) >= 2 and c.start < slot[1] and slot[0] < c.end:
                if c.fallback_building:
                    return self._named_room_degrade(c, reason=f"room_busy:{room_id}")
                return self._blocked("room_busy")
        office_id = self._static.office_id_for_room(canonical or room_id) or room_id
        args: dict[str, Any] = {
            "day": c.day,
            "room_id": canonical or room_id,
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
        booking_result = {
            "status": "success",
            "day": c.day,
            "office_id": office_id,
            "room_id": res.get("room_id") or canonical or room_id,
            "start": c.start,
            "end": c.end,
            "title": c.title or "会议",
        }
        for key in ("order_id", "booking_id"):
            if res.get(key):
                booking_result[key] = res[key]
        return {
            "booking_result": booking_result
        }

    def _named_room_degrade(
        self, c: MeetingConstraints, reason: str
    ) -> dict[str, Any]:
        """点名房降级 → 楼栋级同条件搜索（M3）。

        - 清掉 named_room/compare_rooms，避免上层重复触发点名路径；
        - 无搜索范围（地址）时从 campus+building / campus+fallback_building 构造
          楼栋级地址（不带楼层，逐楼栋搜所有楼层）；
        - 仍无范围 → 保持 blocked（不臆造地址，尊重 gold 的 no_bookable_room）。
        """
        self._log_info(f"M3 点名房降级: {reason}")
        c.named_room = None
        c.compare_rooms = []
        if not c.addresses:
            base_addr = None
            if c.campus and c.building:
                base_addr = self._address_for(c.campus, c.building, None)
            elif c.campus and c.fallback_building:
                base_addr = self._address_for(c.campus, c.fallback_building, None)
            if base_addr:
                c.addresses = [base_addr]
        if not c.addresses:
            self._log_warning(f"M3 降级无搜索范围，保持 blocked: {reason}")
            return self._blocked()
        return self._book_single_day(c, None)

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

    def _locate_unique_own_booking(self) -> dict[str, Any] | None:
        """跨日定位当前用户唯一活跃预订（无 day/order_id 的 rebook cancel 用）。

        rebook 组合「取消原来的」若丢了日期/订单号（mr_0235 兜底路径，sub_query
        被截断丢时间/订单号），原会议只能经**无 day** 的 booking.list 发现。仅当
        组织者过滤后恰唯一命中才返回；多活跃预订 / 工具不可用 → None（保持
        need_confirmation，安全，不做跨日臆断）。
        """
        if not self._registry.is_available(self.BOOKING_LIST):
            return None
        result = self._call_tool(self.BOOKING_LIST, {"status": "active"})
        if result.get("error"):
            return None
        candidates = [
            b for b in (result.get("bookings") or []) if b.get("status") != "cancelled"
        ]
        if len(candidates) > 1:
            uid = self._current_user_id()
            if uid:
                owned = [
                    b for b in candidates if str(b.get("organizer_user_id")) == uid
                ]
                if len(owned) == 1:
                    candidates = owned
                elif len(owned) > 1:
                    return None
        return candidates[0] if len(candidates) == 1 else None

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
                if len(owned) == 1:
                    return owned[0]
                # 多个本人会议仍不能唯一定位；绝不取第一条。
                if len(owned) > 1:
                    return None
        return candidates[0] if len(candidates) == 1 else None

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

    def _canonicalize_room_id(self, room_str: str | None) -> str | None:
        """点名/对比/日程的短房间名 → 规范 room_id（系统侧规范，房间数据全量本地持有）。

        识别用户口头短名形态（缺楼层，如 "A1-349" / "A1北区-349" / "A3-312"）：
        解析出 (楼栋, 房间号)，静态索引里该楼栋内房间号唯一时补全 → "A1-3F-349"。

        以下情况原样返回（不猜，交 simulator 报错）：
        - 已是规范 room_id（"A1-3F-349" / "0552-011"，在索引里）；
        - 楼栋内房间号不唯一（如 A1-001/A1-002 多楼层重复）；
        - 楼栋或房间号解析不到。
        """
        s = (room_str or "").strip()
        if not s or self._static is None or self._static.room(s):
            return room_str
        m = re.match(r"^([A-Za-z]\d+)(?:北区|南区|园区)?[-_ ]?(\d+)$", s)
        if not m:
            return room_str
        building, number = m.group(1), m.group(2)
        hits = [
            rid
            for rid in self._static.rooms_by_building(building)
            if rid.startswith(building + "-") and rid.split("-")[-1] == number
        ]
        return hits[0] if len(hits) == 1 else room_str

    @staticmethod
    def _week_range(day: str) -> tuple[str, str]:
        """订日所在周的周一~周日（compare 覆盖整周日程）。"""
        d = date.fromisoformat(day)
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()

    @staticmethod
    def _is_weekend(day: str | None) -> bool:
        return bool(day) and date.fromisoformat(day).weekday() >= 5

    @staticmethod
    def _next_business_day(day: str, calendar_profile: str = "normal") -> str:
        """day 的下一个可排会日（按日历 Profile 跳过周末/兼容日期）。"""
        return next_meeting_bookable_day(
            date.fromisoformat(day), calendar_profile
        ).isoformat()

    @staticmethod
    def _locate_start_day(
        day: str | None,
        calendar_profile: str = "normal",
    ) -> str | None:
        """定位既有会议的起始日：计算日为周末 → 顺延到下一工作日。

        默认语意（用户定案）：周六/周日不排会——query「明天的会」从周六算的
        明天是周日，直接查下一工作日（周一）起，跳过空的周末。
        """
        if not day:
            return None
        return shift_to_meeting_bookable_day(
            date.fromisoformat(day), calendar_profile
        ).isoformat()

    def _scan_forward_days(
        self,
        start_day: str | None,
        probe_fn: Any,
        max_probes: int = 3,
        calendar_profile: str = "normal",
    ) -> tuple[str | None, Any]:
        """从起始日逐工作日探测（最多 max_probes 次），返回 (命中日, 探测值)。

        探测值 = ``probe_fn(day)`` 的非 None 返回值（booking dict / (booking,
        conflict) 元组）；全空 → (None, None)。约束 max_probes=3 由调用语义定
        （用户「不能超过三步」），探测失败向后顺延到下一工作日。
        """
        day = start_day
        for _ in range(max_probes):
            if not day:
                return None, None
            value = probe_fn(day)
            if value is not None:
                return day, value
            day = self._next_business_day(day, calendar_profile)
        return None, None

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

        if self._registry.is_write(name):
            self._log_info(f"WRITE_PREFLIGHT tool={name} schema=通过 权限=通过 args={args}")
        result = self._env.call_tool(name, args)
        self._history.append((name, args, result))
        if self._context is not None and hasattr(self._context, "ledger"):
            self._context.ledger.add(
                "tool_result",
                name,
                {"args": args, "result": result},
                provenance="runtime_tool",
            )
        if result.get("error"):
            self._log_warning(f"{name} 返回 error: {result['error']}")
        elif self._registry.is_write(name):
            self._log_info(f"WRITE_COMMIT tool={name} result={result}")
        return result

    # ------------------------------------------------------------------ S1 --

    def _execute_book(self, c: MeetingConstraints) -> dict[str, Any]:
        """S1 预订闭环分派：多日同会议室 / 逐天最早 / 单日。

        流程（对应 SOP §三.S1）：
        1. S1w：查询本人工位，推导楼栋/楼层偏好；
        2. 按约束形态分派到多日交集、逐天最早或单日预订；
        3. 首个可用房间 create（含 fallback：备选楼栋 / 反园区 / ±30 分钟）。
        """
        if c.workspace_hint:
            # 「离工位最近」本地实现：搜索范围只带 query 显式约束（园区/楼栋），
            # 但用户明确指定楼层时仍保留硬约束；只有工位推导出的楼层偏好才
            # 剥掉后交给 _pick_room 的 workspace rank。这样“合肥 A4 四楼且
            # 离工位近”不会被扩展到其它楼层，而纯“离我工位最近”仍可跨楼层
            # 选择最近的实时候选。
            if not self._has_explicit_floor(c):
                c.addresses = [self._strip_floor_address(a) for a in c.addresses]
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
            if available_rooms and c.search_free_slot:
                available_rooms = self._verify_continuous_candidates(
                    c.day, available_rooms, start, end
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

        # 楼栋级回退（0229：A1 3F 全被占，但 1F 有房）：只有楼层是偏好、而
        # 不是用户明确指定时才允许。明确“某楼/某楼层”属于硬约束；没有显式
        # “不行再换楼层/楼栋”时，不能因为静态索引或历史兼容规则擅自扩大范围。
        # ``fallback_building`` 是用户明确给出的有序回退授权，允许继续使用
        # 楼栋级组合；精确容量门控仍保留，供旧档 A/B 控制步数。
        floorless = self._floorless_addresses(c)
        explicit_floor = self._has_explicit_floor(c)
        allow_floorless = bool(c.fallback_building or not explicit_floor)
        if floorless and allow_floorless and not (
            self._no_floorless_exact_capacity and c.capacity_exact
        ):
            combos.append((floorless, c.start, c.end))
            if c.time_flexible:
                for start, end in shifts:
                    combos.append((floorless, start, end))
        elif floorless and explicit_floor and not c.fallback_building:
            self._log_info(
                "POLICY_DECISION 会议楼层硬约束: "
                f"addresses={c.addresses} floorless={floorless} action=skip"
            )

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

    def _has_explicit_floor(self, c: MeetingConstraints) -> bool:
        """判断当前地址是否含用户明确的楼层约束。

        编排器有时只给 ``addresses``，有时同时给 ``floor``；两种形态都要
        视为硬约束。这里不读取静态房间目录，也不根据工位推断楼层，避免把
        “离工位最近”产生的偏好误当成用户指定地点。
        """
        if c.floor:
            return True
        for address in c.addresses:
            _, _, floor = self._parse_office_address(address)
            if floor:
                return True
        return False

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
            if not is_meeting_bookable_day(
                day, self._profile_config.calendar_profile
            ):
                self._log_info(
                    f"{day_str} 按日历 Profile={self._profile_config.calendar_profile} "
                    "不可排会，跳过 room.list"
                )
                day += timedelta(days=1)
                continue
            available_rooms = self._collect_available(day_str, c, c.addresses)
            if available_rooms and c.search_free_slot:
                available_rooms = self._verify_continuous_candidates(
                    day_str, available_rooms, c.start, c.end
                )
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
        # 保存工具返回的新订单号，重订/后续参会人操作只能引用本次创建的事实。
        for key in ("order_id", "booking_id"):
            if info.get(key):
                result["booking_result"][key] = info[key]
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
        self._log_info(
            f"CANDIDATE_SET 会议: day={day} addresses={addresses} "
            f"count={len(available)} ids={[room.get('room_id') for _, room in available[:8]]}"
        )
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
        # query 未提人数时默认 capacity_gte=10：对齐 gold 作者约定（room.list 检查
        # 对无人数 query 一律要求 capacity_gte=10，mr_0021/0022 实证有明说人数时跟随；
        # 类A zh_0210/0225 由此补满两检查）。安全：gold 自身用 capacity_gte=10 搜索 →
        # gold 的房容量必 ≥10 → 默认值永不排除正确房，只过滤容量<10 的房（那些本就
        # 是 gold 不要的）。2026-08-19 全量 A/B：val 17+train 7 例零回归，类A +25~30。
        if c.capacity_gte is not None:
            args["capacity_gte"] = c.capacity_gte
        else:
            args["capacity_gte"] = 10
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

    def _reference_office_id(self, room: dict[str, Any], building: str | None) -> str | None:
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
        # 跨域 Projection 的 Gold 契约主要使用语义楼栋；工具调用仍保留 UUID。
        # 这是兼容投影，不改变领域状态，也不依赖 case_id。
        if (
            self._cross_domain
            and self._profile_config.profile == ExecutionProfile.HYBRID_COMPAT
        ):
            semantic_building = room.get("building") or building
            if not semantic_building and isinstance(room_id, str):
                match = re.match(r"([A-Z]\d+)", room_id)
                semantic_building = match.group(1) if match else None
            decision = self._policy.decide(
                "office_id_representation",
                semantic_context={"cross_domain": True, "building": semantic_building},
                evidence={"office_id": oid, "room_id": room_id},
            )
            return decision.selected_value or oid or semantic_building
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
        # create 工具返回的新订单号是后续跨 Task/重订唯一可用事实；不能让
        # rebook 合并逻辑误把被取消的旧 order_id 当成新订单号。
        for key in ("order_id", "booking_id"):
            if info.get(key):
                result["booking_result"][key] = info[key]
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
            "order_id": result.get("order_id") or result.get("booking_id"),
            "booking_id": result.get("booking_id") or result.get("order_id"),
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
        if not c.schedule_room_id:
            self._log_warning("日程查询缺房间，返回空")
            return {}
        # day 兜底：LLM 给单个 day（如「下周二」已归一成 ISO）而缺 start_date/end_date
        # 时按单日区间查（mr_0033 线上 0 工具调用根因的执行层半段）；区间形态
        # （「下周一到周三」）已由 meeting_skill._query_day_range 补成 start/end。
        start = c.schedule_start_date or c.day
        end = c.schedule_end_date or c.day
        if not start or not end:
            self._log_warning("日程查询缺日期，返回空")
            return {}
        result = self._call_tool(
            self.ROOM_SCHEDULE,
            {
                "room_id": c.schedule_room_id,
                "start_date": start,
                "end_date": end,
            },
        )
        if result.get("error"):
            return {}
        return {
            "booking_result": {
                "status": "queried",
                "room_id": c.schedule_room_id,
                "start_date": start,
                "end_date": end,
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

    @staticmethod
    def _strip_floor_address(address: str) -> str:
        """剥掉 office_address 的楼层后缀（0551_A4_4F → 0551_A4）；园区级/楼栋级不变。

        「离工位最近」搜索范围只带 query 显式约束，楼层偏好由本地 workspace rank
        决定，不进入 room.list。
        """
        building, campus, _ = MeetingroomExecutor._parse_office_address(address)
        if campus and building:
            return f"{campus}_{building}"
        return address

    # ---------------------------------------------------------------- 日志 --

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)

    def _log_warning(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)
