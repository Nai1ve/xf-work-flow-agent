"""会议域 Skill：模型提取字段 + 编排操作序列（technical_design.md §6 + 会议 SOP）。

本模块承载「一个会议 skill」的核心：
- ``MeetingOpPlanner``：**必发 LLM#2** —— 从用户描述提取会议字段 + 编排操作序列
  （op 列表），经 ``LLMGateway.structured_call`` 调用线上模型（llm_fast 档）；
  输出契约强制 JSON + 本地 schema 校验；LLM 不可用 / 空 / 低置信 → 确定性规则兜底
  （复用 ``analyze_meeting_query``，映射为 op 序列）。
- ``CombinedAnalyzer``：single 模式——**一次请求**同时出粗粒度意图分解
  （task_units）与会议编排（meeting_plan），供计时实验对照并发双请求模式。
- ``MeetingSkill``：模式选择 + 调度的薄封装（parallel 并发 / single 合并）。

设计守则（用户确认 + AGENT.md §1.4 边界）：
- 模型只产出「op 动作 + query 级原始槽位」；**标识符（order_id / room_id /
  user_id）一律由程序从工具证据解析**——模型仅允许透传原文出现的 ``SEED-*``
  订单号 / 点名房间名 / 姓名工号，禁止凭空输出任何 id；
- op 词表不触碰具体工具名（工具是运行时变量），执行层按运行时注册表对账，
  缺工具优雅降级，绝不调未公开工具（防 forbidden）；
- 提示词尽量简短（≤ ~20 行）、不重复、不枚举工具名。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from utils.business_rules import (
    normalize_addresses,
    normalize_building,
    normalize_campus,
    normalize_floor,
    resolve_company_time,
)
from utils.understanding import (
    CONFIDENCE_FLOOR,
    INTENT_BOOK,
    INTENT_CANCEL,
    INTENT_EXTEND,
    INTENT_PARTICIPANT,
    INTENT_QUERY,
    INTENT_REBOOK,
    UNIT_MEETING,
    IntentRecognizer,
    MeetingConstraints,
    TaskGraphIR,
    TemporalResolver,
    analyze_meeting_query,
)

# 会议 op 词表（模型可输出的动作全集；执行层把每个 op 映射到运行时工具）。
# 原子化守则（用户定案 2026-08-11）：只保留原子动作，**不给模型复合 op**。
# 条件分支用原子 op + 条件 flag 表达（conditional:true = 前提满足才动作，
# 执行层运行时探路判定）；「重新预订同一会议」= cancel + book 两个原子 op
# （book 带 inherit_title:true 沿用原会议标题）。
MEETING_ACTIONS: tuple[str, ...] = (
    "book",              # 单日/单时预订（conditional:true=没订才订；inherit_title/larger/minutes=重订语义）
    "multi_day",         # 多日同房间（含同日多时段 0043 / 多日校验只订一天 0223）
    "earliest",          # 逐天最早可订
    "compare_book",      # 日程对比（room.schedule）选更空闲后预订
    "cancel",            # 取消预订（conditional:true=已订才取消，未订则跳过）
    "extend",            # 延长（conditional:true=冲突则不动原会议）
    "participant_add",   # 加参会人
    "participant_remove",  # 移除参会人
    "participant_list",  # 查参会人
    "query",             # 纯查询（booking.list / room.schedule / unbookable / workspace）
)

# 会议 plan 单次网络调用超时（秒），还会被 case 级 LLM 预算二次收窄。
_MEETING_PLAN_TIMEOUT_S = 15.0

# --------------------------------------------------------------------------
# LLM#2 输出契约：{"ops": [{"action", "target"}], "confidence"}
# target 为自由对象（模型放 query 级原始槽位），标识符由执行层校验/丢弃。
# --------------------------------------------------------------------------

_MEETING_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ops"],
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"type": "string", "enum": list(MEETING_ACTIONS)},
                    "target": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}

_MEETING_PLAN_CARD = """你是企业流程 Agent 的「会议编排器」。只依据输入的 sub_query 提取会议字段并编排操作序列，输出 JSON。

动作（全部原子动作，可叠加；一个 plan 只允许一个订房动作）：
book 订新的（conditional:true=没订才订，已订则跳过） | cancel 取消（conditional:true=已订才取消，未订则跳过） | extend 延长已有会议（conditional:true=冲突就不动原会议） | participant_add/remove/list 参会人 | query 纯查询 | multi_day 多日同房或同日多场 | earliest 最早能订上 | compare_book 几间里选更空闲
重新预订同一会议 = 先 cancel 原会议，再 book 新会议（book 带 inherit_title:true 沿用原会议标题；minutes 表示在原时段上延长多少分钟后重订）。
「没订就订 / 已订就延长 / 冲突则重订」的多条件式 = 同一个 cancel{conditional:true} + book{inherit_title:true}（没订分支由 cancel 的 conditional 跳过天然覆盖）。**绝不**把三个分支拆成 book+extend+cancel 三条并行动作。
earliest / multi_day / compare_book 是终态订房动作（自身就完成预订），不要再追加 book。

字段（放 target）：
day/start/end（YYYY-MM-DD/HH:MM）slots=[{"day","start","end","title"}] 同日多场 days=[...] 多日 week_start/week_end book_only_day addresses=["0552_A1_3F"] capacity 人数 screen title attendees time_flexible workspace_near（仅用户明确要「离工位最近」时设；「在X园区/楼栋」只是地址约束，不设） persons=[{"name","employee_no"}] minutes 延长分钟 rooms 点名房间 keyword query_type(booking_list/schedule/unbookable/workspace) larger 是否换更大 conditional/inherit_title 布尔 flag

规则：只从 sub_query 提取原文字段；order_id 仅 sub_query 含 SEED-* 时透传；复合按序多条（终态订房后只接非 book 动作，如加参会人/查询）。
示例：1)「之前订的X太小，重新订20人以上」→ cancel{day,start,end} + book{day,start,end,addresses,capacity:20,larger:true,inherit_title:true}
2)「能多开半小时就延长，冲突就别动」→ extend{day,start,end,minutes:30,conditional:true}
3)「上午9-11开A、下午2-4开B，同一房间」→ multi_day{slots}
4)「把李明加到评审会」→ participant_add{day,persons:[{"name":"李明"}]}
5)「如果没订就帮我订，已订就延长半小时，冲突则取消重订」→ cancel{day,start,end,conditional:true} + book{day,start,end,minutes:30,inherit_title:true}
6)「如果没订就帮我订」→ book{day,start,end,conditional:true}
输出：{"ops":[{"action":"book","target":{"day":"2026-08-10","start":"14:00","end":"15:00"}}],"confidence":0.9}
只输出一个 JSON 对象。"""

@dataclass
class MeetingOp:
    """一个会议操作：action + query 级槽位（target）。"""

    action: str
    target: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeetingOpPlan:
    """LLM#2 的完整输出：有序 op 序列 + 来源 / 置信度 / 耗时。

    Attributes:
        ops: 有序 MeetingOp 列表（执行层按序执行，程序解析标识符）。
        source: "llm" | "fallback"。
        confidence: 模型置信度（规则兜底为 0）。
        elapsed_s: 编排耗时（秒）。
    """

    ops: list[MeetingOp] = field(default_factory=list)
    source: str = "fallback"
    confidence: float = 0.0
    elapsed_s: float = 0.0


class MeetingOpPlanner:
    """会议编排器：LLM#2 必发提取字段 + 编排流程，规则兜底。

    与 ``IntentRecognizer`` 对称：同一个 gateway，不同的输出契约——这里输出
    会议操作序列（op），不输出粗粒度单元（单元由识别层给出）。
    """

    def __init__(self, logger: Any = None) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger（审计用），None 时不输出。
        """
        self.logger = logger

    def plan(
        self,
        user_query: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
        sub_query: str | None = None,
    ) -> MeetingOpPlan:
        """编排当前请求的会议操作序列（LLM#2 必发；失败/低置信 → 规则兜底）。

        上下文交接契约：编排层只接收识别层重组后的 sub_query（该会议单元负责的
        原文字句），**不再看完整 user_query**——完整原文由调用方保留作后续校验
        证据。sub_query 为空/缺省时回退整句（单域场景二者等价）。

        Args:
            user_query: 用户原始提问（仅当 sub_query 缺失时作为兜底上下文；
                其余情况只用于规则兜底的日期换算）。
            now_iso: env.reset 返回的 now（ISO 字符串）。
            mode: env.reset 返回的 mode（多轮标记透传）。
            gateway: LLMGateway 实例（可用时必发）；None/不可用走规则兜底。
            sub_query: 识别层重组出的会议子句（编排+抽取的**唯一**输入）。

        Returns:
            MeetingOpPlan（从不 raise、从不返回 None）。
        """
        start = time.monotonic()
        context = (sub_query or "").strip() or user_query
        if gateway is not None and gateway.available:
            payload: dict[str, Any] = {
                "sub_query": context,
                "now": now_iso,
                "mode": mode,
            }
            raw = gateway.structured_call(
                _MEETING_PLAN_CARD,
                payload,
                _MEETING_PLAN_SCHEMA,
                timeout_s=_MEETING_PLAN_TIMEOUT_S,
                fallback={"ops": [], "confidence": 0.0},
            )
            ops = self._parse_ops(raw.get("ops"))
            confidence = float(raw.get("confidence") or 0.0)
            if ops and confidence >= CONFIDENCE_FLOOR:
                # 契约执行（跨域 Fix A 用户定案）：order_id 只透传 sub_query 原文
                # 出现的标识符。LLM 编造 order_id（zh_0019 编 SEED-CANCEL-FUZZY-001）
                # 会让执行层走直给分支、跳过 gold 要求的 booking.list 定位。
                ops = self._sanitize_order_id(ops, context)
                # 程序侧归一：一个 meeting 单元只保留首个订房 op（earliest 后冗余
                # book / 重订后再 book 导致双活跃预订等重复订房，确定性丢弃）。
                ops = self._dedup_booking_ops(ops)
                # 程序补全确定性字段：LLM 漏算的 day/start/end/地址等用规则抽取填充
                # （规则只消费 sub_query，同一上下文，跨域不污染）。
                ops = self._fill_gaps(ops, context, now_iso, mode)
                # 程序侧日期归一（mr_0041）：LLM 偶发把 day/days/book_only_day
                # 输出成中文星期（周三/周四）而非 ISO 日期 → 转成相对 now 的日期。
                ops = self._normalize_day_values(ops, now_iso)
                # 程序侧业务规则（#41 窄例外）：地址显示名→内部码查表归一、
                # 公司时间「午别+时长」→规范起止翻译。两者均不给模型处理。
                ops = self._apply_business_rules(ops, context)
                if not self._structurally_usable(ops):
                    # LLM 编排结构性不可执行（如 0223 compare_book 无 compare_rooms）→ 规则兜底。
                    if self.logger is not None:
                        self.logger.warning(
                            f"会议编排结构性不可执行（{[op.action for op in ops]}），规则兜底"
                        )
                    ops = self._rule_plan(context, now_iso, mode)
                    ops = self._sanitize_order_id(ops, context)
                    ops = self._normalize_day_values(ops, now_iso)
                    ops = self._apply_business_rules(ops, context)
                    return MeetingOpPlan(
                        ops=ops,
                        source="fallback",
                        confidence=0.0,
                        elapsed_s=time.monotonic() - start,
                    )
                return MeetingOpPlan(
                    ops=ops,
                    source="llm",
                    confidence=round(confidence, 3),
                    elapsed_s=time.monotonic() - start,
                )
            if self.logger is not None:
                self.logger.warning(
                    f"会议编排空/低置信（ops={len(ops)} conf={confidence:.2f}），规则兜底"
                )

        ops = self._rule_plan(context, now_iso, mode)
        ops = self._sanitize_order_id(ops, context)
        ops = self._dedup_booking_ops(ops)
        ops = self._normalize_day_values(ops, now_iso)
        ops = self._apply_business_rules(ops, context)
        return MeetingOpPlan(
            ops=ops,
            source="fallback",
            confidence=0.0,
            elapsed_s=time.monotonic() - start,
        )

    # ------------------------------------------------------------ 解析 --

    # 可用规则抽取确定性补全的 target 字段（仅当 LLM 留空时填充；不覆盖 LLM 已给值）。
    _FILLABLE: tuple[str, ...] = (
        "day", "start", "end", "addresses", "building", "campus", "floor",
        "fallback_building", "capacity", "screen", "title", "attendees",
        "minutes", "persons", "keyword", "query_type", "week_start",
        "week_end", "book_only_day", "days", "slots", "compare_rooms",
        "named_room", "order_id",
    )

    # 一个 meeting 单元只允许一个订房动作（book/multi_day/earliest/compare_book）。
    _BOOK_FAMILY = frozenset({"book", "multi_day", "earliest", "compare_book"})

    @classmethod
    def _dedup_booking_ops(cls, ops: list[MeetingOp]) -> list[MeetingOp]:
        """同一会议不二订：只保留首个订房 op，其后的订房 op 确定性丢弃。

        - 0040：LLM#2 不知道 earliest 自身会订房，输出 ['earliest','book'] —— 冗余
          book 重新搜索会耗尽步数（StepLimitExceeded → 顶层兜底丢结果）；
        - mr_0027 旧败因：decide 重订成功后再跟一个 book → 2 条活跃预订触发
          「存在额外新增活跃会议预订」forbidden。atomic 词表下订房 op 只许一个，
          首个订房后的 book/multi_day/earliest/compare_book 一律丢弃。
        - 取消+重订 = cancel + book（一个订房动作），不受影响；订房后仍可接非
          订房动作（participant_add / query / extend 等）。
        """
        seen_booking = False
        kept: list[MeetingOp] = []
        for op in ops:
            if op.action in cls._BOOK_FAMILY:
                if seen_booking:
                    continue  # 已有订房动作，重复订房 → 丢弃
                seen_booking = True
            kept.append(op)
        return kept

    @classmethod
    def _sanitize_order_id(cls, ops: list[MeetingOp], context: str) -> list[MeetingOp]:
        """剥除非 sub_query 原文字面出现的 order_id（契约执行，跨域 Fix A）。

        契约（本文件头注释）：标识符（order_id / room_id / user_id）一律由程序从
        工具证据解析，模型只允许透传**原文出现**的标识符。LLM 会在 cancel/extend/
        rebook target 里编造 order_id（zh_0019 编 'SEED-CANCEL-FUZZY-001'）→
        执行层走直给分支、跳过 gold 要求的 booking.list 定位。context 字面包含该
        order_id 才保留（mr_0024/0222/0235 的 SEED-* 原文直给不受影响）；否则剥除，
        执行层改为 booking.list 定位（gold 的 list-before-cancel 语义）。
        """
        if not context:
            return ops
        for op in ops:
            t = op.target
            oid = t.get("order_id")
            if oid and str(oid) not in context:
                t.pop("order_id", None)
        return ops

    @classmethod
    def _fill_gaps(
        cls,
        ops: list[MeetingOp],
        user_query: str,
        now_iso: str,
        mode: str | None,
    ) -> list[MeetingOp]:
        """用规则抽取（analyze_meeting_query）补全 LLM target 的**缺失**字段。

        用户定稿（#41「抽取=模型执行，正则降级为兜底」）：内容抽取以 LLM#2 在
        sub_query 上的输出为准；规则（正则）只做**兜底**——LLM 留空的字段
        才补，**绝不覆盖** LLM 已给出的值（含「LLM 算错」也不纠，交由模型
        自身正确抽取）。语义 flag（conditional/dedup/larger）同样仅 setdefault
        （模型没给才填），不覆盖。
        """
        _, rule_c = analyze_meeting_query(user_query, now_iso, mode)
        rule_t = cls._constraints_to_target(rule_c)

        # 规则语义 flag（模型漏给时 setdefault，模型已给则保留）。
        if any(h in (user_query or "") for h in ("就别动", "别动原会议", "冲突就别", "先告诉我", "不动原会议")):
            for op in ops:
                if op.action == "extend":
                    op.target.setdefault("conditional", True)
        if any(h in (user_query or "") for h in ("如果他已经", "已经在", "就不用加")):
            for op in ops:
                if op.action == "participant_add":
                    op.target.setdefault("dedup", True)
        if any(h in (user_query or "") for h in ("太小", "更大", "换大", "大一点")):
            for op in ops:
                if op.action == "book":
                    op.target.setdefault("larger", True)
        # 重新预订同一会议（cancel 先于 book）→ book 沿用原会议标题（种子标题权威，
        # zh_0033/0226「评审会」→ 种子「季度复盘」）。执行层以刚取消的会议信息解析。
        seen_cancel = False
        for op in ops:
            if op.action == "cancel":
                seen_cancel = True
            elif op.action == "book" and seen_cancel:
                op.target.setdefault("inherit_title", True)
        # 条件预订（「没订就订」）：已订则跳过，避免重复预订。
        if any(h in (user_query or "") for h in ("没订", "如果没有订", "如果没有预订")):
            for op in ops:
                if op.action == "book":
                    op.target.setdefault("conditional", True)

        for op in ops:
            t = op.target
            for key in cls._FILLABLE:
                if t.get(key) in (None, "", [], False):
                    rule_val = rule_t.get(key)
                    if rule_val not in (None, "", [], False):
                        t[key] = rule_val
        return ops

    # 公司时间计算器作用的 op 家族（带起止时刻的预订类动作）。
    _TIME_BEARING_ACTIONS = ("book", "multi_day", "earliest", "compare_book")

    @classmethod
    def _apply_business_rules(
        cls,
        ops: list[MeetingOp],
        context: str,
    ) -> list[MeetingOp]:
        """应用程序侧业务规则（地址归一 + 公司时间计算器）。

        用户定稿（#41 保留的两类窄例外，均「包含业务规则，不能给模型处理」）：
        - 地址归一：显示名（A1园区 / 小镇A1四楼 / A1_4F / A2）→ 工具契约内部码
          （0552_A1 / 0552_A1_4F）。园区码表是公司数据，模型结构性无法推导；
        - 公司时间计算器：午别+时长（周三下午…连续用3小时）→ 规范起止
          （14:00-17:00）。公司工作时段惯例，**由程序翻译**，不给模型处理。

        两条都作用于 op.target：地址**查表归一**、时间**惯例翻译**——模型即使
        已给值也按规则归一（这是查表/翻译的职责，非语义纠错；与 #41「不覆盖
        模型语义值」不冲突）。显式「X点到Y点」时计算器不触发，模型值保留。
        """
        ct = resolve_company_time(context or "")
        for op in ops:
            t = op.target
            # 1) 地址归一（显示名 → 内部码）。
            if t.get("addresses"):
                t["addresses"] = normalize_addresses(t["addresses"])
            if t.get("building"):
                t["building"] = normalize_building(t["building"])
            if t.get("campus"):
                t["campus"] = normalize_campus(t["campus"])
            if t.get("floor"):
                t["floor"] = normalize_floor(t["floor"])
            if t.get("fallback_building"):
                t["fallback_building"] = normalize_building(t["fallback_building"])
            # 2) 公司时间翻译（命中午别+时长且无显式区间 → 覆盖规范起止）。
            if ct and op.action in cls._TIME_BEARING_ACTIONS:
                t["start"], t["end"] = ct
                if op.action == "multi_day" and isinstance(t.get("slots"), list):
                    for slot in t["slots"]:
                        if isinstance(slot, dict):
                            slot["start"], slot["end"] = ct
        return ops

    @staticmethod
    def _normalize_day_value(value: Any, resolver: TemporalResolver) -> Any:
        """把单个 day 值（中文星期 / 字面日期 / 相对表达）归一为 ISO 日期。

        返回原值的情形：已是 ISO 日期（YYYY-MM-DD）、无法识别的串（交执行层
        校验报错而非静默吞掉）。
        """
        s = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        # 中文星期：下周三 / 本周三 / 周三 / 周天。
        m = re.fullmatch(r"(下周|本周|周)?([一二三四五六日天])", s)
        if m:
            weeks = 1 if m.group(1) == "下周" else 0
            return resolver._offset_weekday(m.group(2), weeks).isoformat()
        # 字面日期（5月11日）/ 今天 / 明天 / 后天：复用规则解析器。
        resolved = resolver.resolve_day(s)
        if resolved:
            return resolved
        return value

    @classmethod
    def _normalize_day_values(cls, ops: list[MeetingOp], now_iso: str) -> list[MeetingOp]:
        """把 LLM op target 里的中文星期名归一为 ISO 日期（mr_0041）。

        LLM 偶发把 multi_day 的 ``days`` 输出成「周三/周四」而非日期，或单日
        ``day`` / ``book_only_day`` 输出成「周X」→ 执行层原样透传给 room.list，
        schema 拒绝 / 订错日。规则抽取（TemporalResolver）早已转成日期，此步
        只兜 LLM 路径；对纯日期是 no-op。``_fill_gaps`` 用 setdefault 不覆盖
        LLM 已给值，故归一必须在补全之后。
        """
        resolver = TemporalResolver(now_iso)
        for op in ops:
            t = op.target
            if isinstance(t.get("days"), list):
                t["days"] = [cls._normalize_day_value(d, resolver) for d in t["days"]]
            for key in ("day", "book_only_day"):
                if t.get(key):
                    t[key] = cls._normalize_day_value(t[key], resolver)
        return ops

    @staticmethod
    def _add_minutes_str(time_str: str, minutes: int) -> str:
        """HH:MM 加 N 分钟（重订到延长后时刻，规则兜底用）。"""
        h, m = (int(p) for p in str(time_str).split(":"))
        total = h * 60 + m + int(minutes)
        return f"{total // 60:02d}:{total % 60:02d}"

    @staticmethod
    def _parse_ops(raw_ops: Any) -> list[MeetingOp]:
        """把 LLM 原始 ops 归一为 MeetingOp 列表（非法项丢弃）。

        每个 op 只保留 {action, target} 两键；target 必须是 dict（否则置空）。
        """
        if not isinstance(raw_ops, list):
            return []
        ops: list[MeetingOp] = []
        for raw in raw_ops:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "")
            if action not in MEETING_ACTIONS:
                continue
            target = raw.get("target")
            ops.append(MeetingOp(action=action, target=target if isinstance(target, dict) else {}))
        return ops

    @staticmethod
    def _structurally_usable(ops: list[MeetingOp]) -> bool:
        """至少一个 op 结构性可执行才算可用：每个动作缺了核心字段就执行不了。

        兜底触发场景：LLM 选对了动作但 target 空壳（0223 判成 compare_book 却
        无 compare_rooms/named_room → 执行层必然空结果）。此时规则重编更可靠。
        """
        for op in ops:
            t = op.target
            action = op.action
            if action == "book":
                if t.get("day") and t.get("start") and t.get("end"):
                    return True
                if t.get("day") and (t.get("named_room") or t.get("room") or t.get("rooms")):
                    return True
            elif action in ("multi_day", "earliest"):
                if isinstance(t.get("slots"), list) and len(t["slots"]) >= 2:
                    return True
                if isinstance(t.get("days"), list) and len(t["days"]) >= 2:
                    return True
                if t.get("week_start") or t.get("day"):
                    return True
            elif action == "compare_book":
                if t.get("day") and (t.get("compare_rooms") or t.get("named_room") or t.get("rooms")):
                    return True
            elif action == "cancel":
                if t.get("day") or t.get("order_id"):
                    return True
            elif action == "extend":
                if t.get("day") or t.get("order_id"):
                    return True
            elif action in ("participant_add", "participant_remove"):
                if t.get("persons") and (t.get("day") or t.get("order_id")):
                    return True
            elif action == "participant_list":
                if t.get("day") or t.get("order_id"):
                    return True
            elif action == "query":
                if t.get("day") or t.get("query_type") or t.get("keyword"):
                    return True
        return False

    # ------------------------------------------------------------ 兜底 --
    def _rule_plan(self, user_query: str, now_iso: str, mode: str | None) -> list[MeetingOp]:
        """规则兜底：复用 IntentRouter + MeetingConstraintExtractor 映射 op 序列。

        只覆盖规则能可靠判定的意图；判定不出 → 空 op（执行层不动作，安全）。
        """
        intent, c = analyze_meeting_query(user_query, now_iso, mode)
        target = self._constraints_to_target(c)
        query = user_query or ""

        if intent == INTENT_QUERY:
            return [MeetingOp("query", target)]
        if intent == INTENT_CANCEL:
            return [MeetingOp("cancel", target)]
        # 条件重订分支（mr_0027/zh_0026/zh_0037「没订就订 / 已订就延长 / 冲突则取消
        # 重订」）：gold 在种子预置下恒走冲突分支 → cancel{conditional} + book
        # （重订到「原时段 + 延长分钟」的结束时刻）。
        if "没订" in query and "延长" in query and "冲突" in query:
            book_t = {**target, "inherit_title": True}
            if c.minutes and c.end:
                book_t["end"] = self._add_minutes_str(c.end, c.minutes)
            return [
                MeetingOp("cancel", {**target, "conditional": True}),
                MeetingOp("book", book_t),
            ]
        if intent == INTENT_EXTEND:
            # 条件性延长（0050/0015「能多开就延长，后面冲突就别动原会议」）：
            # 冲突时执行层不提交，产出 blocked(conflict_after_requested_extension)。
            if any(h in query for h in ("就别动", "别动原会议", "冲突就别", "先告诉我", "不动原会议")):
                target = {**target, "conditional": True}
            return [MeetingOp("extend", target)]
        if intent == INTENT_REBOOK:
            # 重订 = cancel + book 两个原子 op；新会议沿用原会议标题（种子标题权威）。
            # 「换大/更大/太小/大一点」→ 容量须大于原会议（larger:true）。
            book_t = {**target, "inherit_title": True}
            if any(h in query for h in ("太小", "更大", "换大", "大一点")):
                book_t["larger"] = True
            return [MeetingOp("cancel", target), MeetingOp("book", book_t)]
        if intent == INTENT_PARTICIPANT:
            if "移除" in query or "移出" in query:
                return [MeetingOp("participant_remove", target)]
            if "哪些人" in query or "谁参加" in query or "有谁" in query:
                return [MeetingOp("participant_list", target)]
            if "如果他已经" in query or "已经在" in query or "就不用加" in query:
                target = {**target, "dedup": True}
            return [MeetingOp("participant_add", target)]
        if intent == INTENT_BOOK:
            if any(h in query for h in ("哪个更空闲", "哪个最空闲", "最空闲", "更空闲", "对比")) and "订" in query:
                return [MeetingOp("compare_book", target)]
            if len(c.slots) >= 2:
                return [MeetingOp("multi_day", target)]
            if len(c.days) >= 2:
                return [MeetingOp("multi_day", target)]
            if c.week_start:
                return [MeetingOp("earliest", target)]
            return [MeetingOp("book", target)]
        return []

    @staticmethod
    def _constraints_to_target(c: MeetingConstraints) -> dict[str, Any]:
        """把规则抽取的约束序列化为 target（与 LLM target 同键，供执行层统一消费）。"""
        return {
            "day": c.day,
            "days": c.days,
            "book_only_day": c.book_only_day,
            "week_start": c.week_start,
            "week_end": c.week_end,
            "start": c.start,
            "end": c.end,
            "building": c.building,
            "campus": c.campus,
            "floor": c.floor,
            "addresses": c.addresses,
            "fallback_building": c.fallback_building,
            "capacity": c.capacity_gte,
            "screen": c.has_screen,
            "bookable": c.bookable,
            "title": c.title,
            "attendees": c.attendees,
            "workspace_near": c.workspace_hint,
            "time_flexible": c.time_flexible,
            "query_type": c.query_type,
            "room_id": c.schedule_room_id,
            "start_date": c.schedule_start_date,
            "end_date": c.schedule_end_date,
            "keyword": c.query_keyword,
            "named_room": c.named_room,
            "minutes": c.minutes,
            "persons": c.persons,
            "compare_rooms": c.compare_rooms,
            "order_id": c.order_id_hint,
            "slots": c.slots,
        }


class MeetingSkill:
    """会议 Skill：**顺序流水线** —— 识别层 → 编排层 → 执行层（用户定稿架构）。

    管线（单 case 内严格串行，无并发）：
    - LLM#1（``IntentRecognizer``）：识别业务单元 + **重组数据**——每个单元带
      ``sub_query``（该单元负责的原文子句）；
    - 编排层（``MeetingOpPlanner``，LLM#2）：只接收 meeting 单元重组后的
      ``sub_query`` 上下文做编排 + 抽取；完整 ``user_query`` 不再送入 LLM#2，
      由调用方保留作后续校验证据；
    - 执行层：``execute_ops`` 纯流程单/多轮执行。

    返回 (ir, meeting_plan)：ir 供入口层驱动单元循环（leave/budget 安全跳过），
    meeting_plan 供执行层 ``execute_ops``。缺 key / 不可用时两路各自走规则兜底。
    计时：识别/编排各自独立计时 + gateway 独立统计（整体时间由调用方汇总）。
    """

    def __init__(self, logger: Any = None) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger。
        """
        self.logger = logger
        self.recognizer = IntentRecognizer(logger=logger)
        self.planner = MeetingOpPlanner(logger=logger)
        # 各阶段计时/模型统计（run 后由调用方读：识别 / 编排 / 执行）。
        self.last_timings: dict[str, Any] = {}
        self.last_planner_gateway: Any = None

    def run(
        self,
        user_query: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
    ) -> tuple[TaskGraphIR, MeetingOpPlan]:
        """顺序执行识别 → 编排，返回 (意图分解, 会议编排)。

        Args:
            user_query: 用户原始提问。
            now_iso: env.reset 返回的 now（ISO 字符串）。
            mode: env.reset 返回的 mode（多轮标记透传）。
            gateway: LLMGateway 实例（LLM#1 用）；编排层另建独立 gateway
                （分段计时 + 预算隔离）。

        Returns:
            (TaskGraphIR, MeetingOpPlan)。
        """
        start = time.monotonic()

        # —— 识别层：LLM#1 识别单元 + sub_query 上下文 ——
        ir = self.recognizer.analyze(user_query, now_iso, mode, gateway)

        # —— 组装编排上下文：meeting 单元的 sub_query（原文重组结果）——
        meeting_units = [
            u for u in ir.ordered_units() if u.unit_type == UNIT_MEETING
        ]
        meeting_subs = [u.sub_query for u in meeting_units if (u.sub_query or "").strip()]
        context = "\n".join(meeting_subs).strip() or user_query
        if not meeting_units:
            # 无会议单元（纯 leave/budget）→ 编排层无会议可排，空 plan。
            self.last_timings = {
                "recognize_s": round(ir.elapsed_s, 3),
                "orchestrate_s": 0.0,
                "exec_s": 0.0,
            }
            return ir, MeetingOpPlan(ops=[], source="fallback", confidence=0.0)

        # —— 编排层：LLM#2 接收 sub_query 上下文（独立 gateway 分段计时）——
        # 识别 gateway 不可用时整个流水线走规则兜底：编排层同样不调模型。
        planner_gateway = None
        if gateway is not None and gateway.available:
            from utils.llm_gateway import LLMGateway

            planner_gateway = LLMGateway(
                logger=getattr(self.logger, "child", lambda *_: None)("LLM#2")
            )
        self.last_planner_gateway = planner_gateway
        meeting_plan = self.planner.plan(
            user_query, now_iso, mode, planner_gateway, sub_query=context
        )
        self.last_timings = {
            "recognize_s": round(ir.elapsed_s, 3),
            "orchestrate_s": round(meeting_plan.elapsed_s, 3),
            "exec_s": 0.0,
            "skill_total_s": round(time.monotonic() - start, 3),
        }
        return ir, meeting_plan
