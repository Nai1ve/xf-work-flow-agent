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
from datetime import timedelta
from dataclasses import dataclass, field
from typing import Any

from utils.business_rules import (
    normalize_addresses,
    normalize_building,
    normalize_campus,
    normalize_floor,
    normalize_office_address,
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
    MODE_MULTI_TURN,
    UNIT_MEETING,
    IntentRecognizer,
    MeetingConstraints,
    TaskGraphIR,
    TemporalResolver,
    analyze_meeting_query,
)
from utils.meeting_clarify import (
    CONFIRM_REPLY,
    apply_clarified,
    build_meeting_specs,
    build_order_id_spec,
)
from utils.clarifier import clarify_slots

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
book 订新的（conditional:true=没订才订，已订则跳过） | cancel 取消（conditional:true=已订才取消，未订则跳过） | extend 延长已有会议（conditional:true=冲突就不动原会议） | participant_add/remove/list 参会人 | query 纯查询 | multi_day 多日同房或同日多场 | earliest 最早能订上 | compare_book 仅当用户要对比挑选（哪间更空/更合适）
重新预订同一会议 = 先 cancel 原会议，再 book 新会议（book 带 inherit_title:true 沿用原会议标题；minutes 表示在原时段上延长多少分钟后重订）。
「没订就订 / 已订就延长 / 冲突则重订」的多条件式 = 同一个 cancel{conditional:true} + book{inherit_title:true}（没订分支由 cancel 的 conditional 跳过天然覆盖）。**绝不**把三个分支拆成 book+extend+cancel 三条并行动作。
earliest / multi_day / compare_book 是终态订房动作（自身就完成预订），不要再追加 book。
**备选楼栋**（A1不行/没有合适的就A2/优先A1/先A1）＝同一个 book 里 addresses 按主→备顺序排列（先搜主楼栋、主楼栋无合适房才轮到备选），**不是** compare_book。

字段（放 target）：
day/days/book_only_day 输出 sub_query 原文日期短语（下周二/明天/本周三/5月11日/周三），系统按 now 自动换算成具体日期，**不要**自己算成 YYYY-MM-DD（算错会订错日）；**本周/下周 这类整周词不能当 day**（不是具体一天）；start/end（HH:MM）slots=[{"day","start","end","title"}] 同日多场（slot 的 day 同样用原文短语）days=[...] 多日（每项都是原文短语）week_start/week_end/start_date/end_date 保持 YYYY-MM-DD（区间/查询范围，给不出就省略让系统按 now 算）book_only_day 与 day 同规则（原文短语）addresses=["0552_A1_3F"] capacity 人数 screen title attendees time_flexible workspace_near（仅用户明确要「离工位最近」时设 true；与 addresses 是**独立约束**，可并存） persons=[{"name","employee_no"}] minutes 延长分钟 rooms 点名房间（短名如 "A1-349" 也行，系统会规范成 "A1-3F-349"） keyword query_type(booking_list/schedule/unbookable/workspace) larger 是否换更大 conditional/inherit_title 布尔 flag

规则：只从 sub_query 提取原文字段；order_id 仅 sub_query 含 SEED-* 时透传；复合按序多条（终态订房后只接非 book 动作，如加参会人/查询）。
**地址约束规则**：addresses 是**地址约束数组**——子句提到「在X园区/楼/栋/楼层」就**必须**填入 addresses；「离工位最近」→ workspace_near:true。两者**不互斥**，同时出现时**都必须提取**。
示例：1)「之前订的X太小，重新订20人以上」→ cancel{day,start,end} + book{day,start,end,addresses,capacity:20,larger:true,inherit_title:true}
2)「能多开半小时就延长，冲突就别动」→ extend{day,start,end,minutes:30,conditional:true}
3)「上午9-11开A、下午2-4开B，同一房间」→ multi_day{slots}
4)「把李明加到评审会」→ participant_add{day,persons:[{"name":"李明"}]}
5)「如果没订就帮我订，已订就延长半小时，冲突则取消重订」→ cancel{day,start,end,conditional:true} + book{day,start,end,minutes:30,inherit_title:true}
6)「如果没订就帮我订」→ book{day,start,end,conditional:true}
7)「先看看A1-349会议室周四下午3-5点空不空，空就订」→ book{day,start,end,rooms:["A1-349"]}（点名订房动作内部会先查该房间日程，空才订；**不要**拆成 query+book 两步）
8)「帮我查A1-3F-349本周（5月11日到5月15日）的预订情况」→ query{query_type:"schedule",room_id:"A1-3F-349",start_date:"2026-05-11",end_date:"2026-05-15"}
9)「合肥A4楼10人带屏幕会议室，离我工位近一点」→ book{day,start,end,addresses:["合肥A4楼"],capacity:10,screen:true,workspace_near:true}
10)「A1 没有合适的就 A2，也要带屏幕」→ book{day,start,end,addresses:["A1","A2"],screen:true}（备选楼栋按主→备顺序放一个 book 里，能订即止；**不要**用 compare_book）
输出：{"ops":[{"action":"book","target":{"day":"下周二","start":"14:00","end":"15:00"}}],"confidence":0.9}
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
                ops = self._normalize_day_values(ops, now_iso, context)
                # 程序侧业务规则（#41 窄例外）：地址显示名→内部码查表归一、
                # 公司时间「午别+时长」→规范起止翻译。两者均不给模型处理。
                ops = self._apply_business_rules(ops, context)
                # 统一流程（换大房，防御性）：query 含换大语义且计划带 participant_add
                # 但无 cancel（LLM 常把「加人+换大」误判成 participant_add+book，产生
                # 额外预订触发 forbidden）→ 按规则重编 rebook（cancel + book{inherit_title,
                # larger}）。正确日期 case（mr_0011/0222）LLM 已 emit cancel → 不受影响；
                # 规则判定不出 rebook（意图 unknown）→ 保持 LLM 计划不动。
                if self._is_rebook_larger_miss(context, ops):
                    rule_ops = self._rule_plan(context, now_iso, mode)
                    rule_ops = self._sanitize_order_id(rule_ops, context)
                    rule_ops = self._normalize_day_values(rule_ops, now_iso, context)
                    rule_ops = self._apply_business_rules(rule_ops, context)
                    if rule_ops:
                        ops = rule_ops
                        if self.logger is not None:
                            self.logger.warning(
                                f"会议「换大房」缺 cancel（{[op.action for op in ops]}），规则重编 rebook"
                            )
                # 结构性门控已删除（用户定案 2026-08-19）：执行层是真正的校验器，op
                # 直接进执行层，解不了就返回 blocked/need_confirmation 诚实状态，不做
                # 「校验器按动作名+字段名判形状、把能执行的 op 误拒再兜底」。空/低置信
                # 兜底（下方 271 行）与换大房重编（_is_rebook_larger_miss）仍保留。
                ops = self._tag_seeded_rebook(ops, user_query)
                ops = self._tag_workspace_near(ops, context)
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
        ops = self._normalize_day_values(ops, now_iso, context)
        ops = self._apply_business_rules(ops, context)
        ops = self._tag_seeded_rebook(ops, user_query)
        ops = self._tag_workspace_near(ops, context)
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
        "named_room", "order_id", "campus_explicit", "capacity_exact",
    )

    # 一个 meeting 单元只允许一个订房动作（book/multi_day/earliest/compare_book）。
    _BOOK_FAMILY = frozenset({"book", "multi_day", "earliest", "compare_book"})

    # 显式换位置短语（「换到A2园区」→ 目标 A2 权威）：命中后重订 book-op 的
    # 地址只留换去目标，弃旧位置。目标可能带修饰（换到**一个**大一点的 / 换到
    # **小镇**A2），捕获到首个空白/标点为止再归一。
    _MOVE_TO_RE = re.compile(r"(?:换到|搬到|改到|挪到|换去|转到)\s*(?:一个\s*)?([^\s，。；、]+)")

    @classmethod
    def _tag_seeded_rebook(cls, ops: list[MeetingOp], user_query: str) -> list[MeetingOp]:
        """完整原文含 SEED-* 且计划是重订组合 → cancel 打 seeded 标记。

        reference 约定：用户**显式**点名原预订（SEED-0222-001 / SEED-0235-001）的
        重订 → status=rebooked + cancelled_order_id + officeId UUID；泛称原会议
        （「订过一个评审会」「原会议」，zh_0033/mr_0027）→ status=success + 楼栋名。
        识别层（LLM#1）偶发把 sub_query 里的 SEED 标识符截掉（mr_0235），编排层只
        看 sub_query 就丢了这个信号——但 plan() 的 user_query 是**完整原文**，SEED-*
        仍在。这里在编排层把信号传回 cancel op.target["seeded"]，执行层定位分支据此
        定 seeded（rereference 约定 train 该族 gold，val 无 SEED-in-query 反例）。
        """
        if not re.search(r"SEED-", user_query or ""):
            return ops
        has_book = any(op.action in cls._BOOK_FAMILY for op in ops)
        for op in ops:
            if op.action == "cancel" and has_book:
                op.target["seeded"] = True
        return ops

    # 「离工位最近」确定性关键词（用户定案 2026-08-19）：query 含近工位语义 →
    # 订房 op 强制 workspace_near=True。workspace_hint 只由 LLM#2 输出（无规则兜底），
    # LLM 偶发漏抽 → 执行层不调 user.get_workspace、不走离工位选址（zh_0009 掉分源）。
    # 词表覆盖数据族：离(我)工位最近 / 离我工位近一点 / 近一点 / 最近的会议室。
    _WORKSPACE_NEAR_RE = re.compile(
        r"(?:离(?:我|我们)?工位(?:最)?近(?:一点|的)?|近一点|最近(?:的)?会议室)"
    )

    @classmethod
    def _tag_workspace_near(cls, ops: list[MeetingOp], context: str) -> list[MeetingOp]:
        """query 含「离工位最近/近一点/最近的会议室」→ 订房 op 补 workspace_near。

        只加不删：LLM 已给 workspace_near=True 不重复；query 无关键词 → 不动（防止
        把「最空闲/最早」等时间语义误判成离工位）。gold 检查 user.get_workspace 的
        workspace-near 族（train mr_0016/0049/0217/0237、zh_0009/0018/0221/0230，
        val mr_0020/0230、zh_0215）全部含这些短语 → 确定性覆盖。
        """
        if not cls._WORKSPACE_NEAR_RE.search(context or ""):
            return ops
        for op in ops:
            if op.action in cls._BOOK_FAMILY:
                op.target["workspace_near"] = True
        return ops

    @classmethod
    def _is_rebook_larger_miss(cls, context: str, ops: list[MeetingOp]) -> bool:
        """换大房被误判为加人/漏 cancel：query 含换大语义 + 计划缺 cancel。

        「加人 + 换大房」的真实语义是取消原会议另订更大的（cancel + book{larger}），
        而 LLM 常输出 participant_add + book 或 book{larger} 独走 —— 原会议不取消、
        又新订一间 → 双活跃预订触发「存在额外新增活跃会议预订」forbidden。触发：
        (1) query 有换大/太小/更大/大一点；(2) 计划无 cancel；且 (3) 计划带
        participant_add（老判据）**或** book-family op 直接带 larger:true（扩展，
        LLM 偶发 book{larger} 无 cancel 无 participant_add，zh_0219 防御）。
        """
        if "cancel" in [op.action for op in ops]:
            return False
        if not any(h in (context or "") for h in ("太小", "更大", "换大", "大一点")):
            return False
        if "participant_add" in [op.action for op in ops]:
            return True
        return any(
            op.action in cls._BOOK_FAMILY and op.target.get("larger") for op in ops
        )

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
        rule_intent, rule_c = analyze_meeting_query(user_query, now_iso, mode)
        # 方案 A（2026-08-14 用户定案，与 _rule_plan 同源）：单轮预订缺 day →
        # 默认当日，让 LLM 漏 day 的 book 仍能走完整搜索判定（zh_0223/0224 的
        # LLM 空壳触发规则兜底在 _rule_plan 覆盖；这里兜 LLM 给了非空但无 day 的
        # 单轮 booking）。只补 _BOOK_FAMILY 的 day（下方 FILLABLE 通用补全不区分
        # 动作，cancel/extend 的 day 保持规则值，不在此默认）。
        if rule_intent == INTENT_BOOK and not rule_c.day:
            rule_c.day = TemporalResolver(now_iso).today.isoformat()
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

        # 统一订房候选队列（用户定案「执行层补全」，统一流程）：query 声明备选楼栋
        # （先A1不行就A2 / 优先A1 / A1没有合适的就A2）时，候选地址按规则权威顺序
        # （主楼栋[+楼层] + 备选楼栋）在前、LLM 给出的地址去重追加在后。LLM 常把
        # addresses 给成「备选楼栋+楼层」的过度具体地址（mr_0023 56 分：只搜
        # 0552_A2_1F，缺 0552_A1_4F 与 0552_A2 两次楼栋级 room.list）→ 主楼栋搜索
        # 被跳过、gold 的两栋探测落空。搜索顺序是**流程**（程序权威，#41 窄例外：
        # 只补候选序列，不覆盖 LLM 的容量/标题/时间等语义值）。
        if rule_c.fallback_building:
            rule_addresses = rule_t.get("addresses") or []
            if rule_addresses:
                for op in ops:
                    if op.action in cls._BOOK_FAMILY:
                        cur = [a for a in (op.target.get("addresses") or []) if a]
                        merged = list(rule_addresses)
                        for a in cur:
                            if a not in merged:
                                merged.append(a)
                        op.target["addresses"] = merged
                        op.target.setdefault("fallback_building", rule_c.fallback_building)
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
            # 1b) 显式换位置（「换到/搬到/改到/挪到 X」）→ X 是权威搜索目标。
            #     重订组合下 LLM 常把**旧位置**（原会议所在，mr_0235「会议室在A1」）
            #     当主地址、把目标当备选（building=A1 主、fallback=A2），先搜旧楼栋
            #     会订错房（0552_A1→0552-011，gold 要 0552_A2→A2-1F-147）。显式
            #     换位置时目标唯一：订房 op 地址仅留目标，弃旧楼栋。
            if cls._MOVE_TO_RE and op.action in cls._BOOK_FAMILY and any(
                o.action == "cancel" for o in ops
            ):
                mv = cls._MOVE_TO_RE.search(context or "")
                if mv:
                    target_code = normalize_office_address(mv.group(1))
                    if target_code:
                        t["addresses"] = [target_code]
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
        """把单个 day 值（原文日期短语）归一为 ISO 日期（方案1，确定性）。

        与 leave 对齐：LLM 只输出 sub_query 的原文日期短语（下周二/明天/本周三/
        5月11日/周三），日期换算完全交给程序（TemporalResolver）——消除「下周二」
        被 LLM 预解析成 04-21 或 04-28 的方差。**搜索式**解析，容忍短语前后缀
        （「下周二下午」「周三的」），与解析器覆盖的旧形式保持兼容。

        返回原值的情形：已是 ISO 日期（YYYY-MM-DD）、无法识别的串（交执行层
        校验报错而非静默吞掉）。
        """
        s = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        # 大后天必须先于「后天」判断（resolve_day 的「后天」子串会误判为 +2 天）。
        if "大后天" in s:
            return (resolver._today + timedelta(days=3)).isoformat()
        # 下周X / 下个星期三 / 下星期二 / 下星期X。
        m = re.search(r"下(?:周|个星期|星期)([一二三四五六日天])", s)
        if m:
            return resolver._offset_weekday(m.group(1), weeks=1).isoformat()
        # 本周X / 这周X / 星期X / 周X。
        m = re.search(r"(?:本周|这周|星期|周)([一二三四五六日天])", s)
        if m:
            return resolver._offset_weekday(m.group(1), weeks=0).isoformat()
        # 字面日期（5月11日）/ 下个月X日 / 今天 / 明天 / 后天：复用规则解析器。
        resolved = resolver.resolve_day(s)
        if resolved:
            return resolved
        return value

    @classmethod
    def _normalize_day_values(
        cls,
        ops: list[MeetingOp],
        now_iso: str,
        context: str | None = None,
    ) -> list[MeetingOp]:
        """把 LLM op target 里的原文日期短语归一为 ISO 日期（方案1）。

        LLM#2 的卡片契约：``day/days/book_only_day`` 只输出 sub_query 原文日期
        短语，日期换算完全由本步确定性完成（消除「下周二」→ 04-21/04-28 的
        LLM 方差）。对已经是 ISO 的值是 no-op；``week_start/week_end`` 若是
        「本周/下周」这类整周词，展开成周一~周五区间（最早能订上语义）。

        **安全网**（方案1 的强制兜底）：LLM 违反卡片指令仍把 ``day`` 预解析成
        ISO（下周二 → 04-28 订错日）时，只要 sub_query 里**只有一个**可解析的
        日期短语，就以规则抽取的确定性日期覆盖。多日期上下文（取消周三/周四再
        订、周三和周四多日、下周X 比较订房）不触发，避免误覆盖其它日期的 op；
        只作用于 ``book`` 动作（方差实测全部落在普通订房上）。
        """
        resolver = TemporalResolver(now_iso)
        # 规则抽取的确定性 booking 日（安全网权威值；仅单日期上下文才启用）。
        rule_day: Any = None
        if context:
            try:
                _, rule_c = analyze_meeting_query(context, now_iso, None)
                rule_day = cls._constraints_to_target(rule_c).get("day")
            except Exception:
                rule_day = None
            if rule_day is not None and cls._count_day_refs(context) != 1:
                rule_day = None
        for op in ops:
            t = op.target
            if isinstance(t.get("days"), list):
                t["days"] = [cls._normalize_day_value(d, resolver) for d in t["days"]]
            if isinstance(t.get("slots"), list):
                for slot in t["slots"]:
                    if isinstance(slot, dict) and slot.get("day"):
                        slot["day"] = cls._normalize_day_value(slot["day"], resolver)
            for key in ("day", "book_only_day"):
                if not t.get(key):
                    continue
                raw = str(t[key]).strip()
                t[key] = cls._normalize_day_value(t[key], resolver)
                # 整周词（本周/下周/这周）不是具体一天：用规则抽取的 booking 日兜底
                # （mr_0236 LLM 把比较区间「下周」误当成 day → 执行层 isoformat 崩溃）。
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(t[key]).strip()) and re.fullmatch(
                    r"(本周|这周|下周)", raw
                ):
                    if rule_day:
                        t[key] = rule_day
                    else:
                        span = cls._expand_week_word(raw, resolver)
                        if span:
                            t[key] = span[0]
                if (
                    key == "day"
                    and op.action == "book"
                    and rule_day
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw)
                    and str(t[key]).strip() != rule_day
                ):
                    t[key] = rule_day
            for key in ("week_start", "week_end"):
                v = t.get(key)
                if not v:
                    continue
                sv = str(v).strip()
                if re.fullmatch(r"(本周|这周|下周)", sv):
                    span = cls._expand_week_word(sv, resolver)
                    if span:
                        t[key] = span[0] if key == "week_start" else span[1]
                else:
                    t[key] = cls._normalize_day_value(v, resolver)
        return ops

    @staticmethod
    def _count_day_refs(context: str) -> int:
        """统计上下文里可解析的**单日**日期短语数量（安全网门控）。

        只统计能换算成一个具体日期的表达（大后天/后天/明天/今天/下周X/本周X/
        星期X/周X/X月X日）。整周搜索词（本周/下周不带星期）、多日连接（周三和
        周四）、比较订房（下周哪个更空闲里的「周X」）都会如实计入，从而关闭
        安全网。``大后天`` 先计、``后天`` 减去大后天，避免子串重复计数。
        """
        n = 0
        n += context.count("大后天")
        n += context.count("后天") - context.count("大后天")
        n += context.count("明天")
        n += context.count("今天")
        n += len(re.findall(r"下(?:周|个星期|星期)[一二三四五六日天]", context))
        n += len(re.findall(r"(?:本周|这周|星期)[一二三四五六日天]", context))
        n += len(re.findall(r"(?<![下本这])周[一二三四五六日天]", context))
        n += len(re.findall(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]", context))
        return n

    @staticmethod
    def _expand_week_word(value: str, resolver: TemporalResolver) -> tuple[str, str] | None:
        """「本周/下周」→ (周一, 周五) ISO 区间（最早能订上语义）。

        与 ``resolve_week_span`` 一致：本周起始不早于今天，周六/周日已无本周
        工作日则顺延到下周。LLM 若给 ``week_start``/``week_end`` 整周词（而非
        具体日期短语）时才走这里。
        """
        s = str(value).strip()
        weeks = 1 if "下" in s else 0
        this_monday = resolver._today - timedelta(days=resolver._today.weekday())
        start = this_monday + timedelta(days=7 * weeks)
        if weeks == 0:
            start = max(start, resolver._today)
            if start.weekday() >= 5:
                start = this_monday + timedelta(days=7)
        end = start + timedelta(days=4)
        return start.isoformat(), end.isoformat()

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

    # ------------------------------------------------------------ 兜底 --
    def _rule_plan(self, user_query: str, now_iso: str, mode: str | None) -> list[MeetingOp]:
        """规则兜底：复用 IntentRouter + MeetingConstraintExtractor 映射 op 序列。

        只覆盖规则能可靠判定的意图；判定不出 → 空 op（执行层不动作，安全）。
        """
        intent, c = analyze_meeting_query(user_query, now_iso, mode)
        # 方案 A（2026-08-14 用户定案）：单轮预订缺 day → 默认当日。缺日期不再
        # 「提前短路」（executor 曾对空 day 直接放弃，zh_0223/0224「订不到就算了/
        # 不行就别乱订」连 room.list 都不发）；默认当日走完整搜索判定——A1_4F
        # 无 bookable 房 → blocked/no_bookable_room 对齐 gold。**只在本路径默认**：
        # 多轮澄清（_multi_turn_plan 预订分支）不经这里，day 靠澄清拿到正确值；
        # cancel/extend/rebook 也不默认（已有会议定位不靠它）。LLM 已给 day 时
        # c.day 非空不覆盖。
        if intent == INTENT_BOOK and not c.day:
            c.day = TemporalResolver(now_iso).today.isoformat()
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
            # 「先别动」覆盖 mt_0011/0205「冲突就先别动」措辞（就/先 混用）。
            if any(h in query for h in ("就别动", "先别动", "别动原会议", "冲突就别", "先告诉我", "不动原会议")):
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
            "campus_explicit": c.campus_explicit,
            "floor": c.floor,
            "capacity_exact": c.capacity_exact,
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
        env: Any = None,
    ) -> tuple[TaskGraphIR, MeetingOpPlan]:
        """顺序执行识别 → 编排，返回 (意图分解, 会议编排)。

        Args:
            user_query: 用户原始提问。
            now_iso: env.reset 返回的 now（ISO 字符串）。
            mode: env.reset 返回的 mode（多轮标记透传）。
            gateway: LLMGateway 实例（LLM#1 用）；编排层另建独立 gateway
                （分段计时 + 预算隔离）。
            env: 受控环境（多轮 case 需 env.reply 走澄清/确认；其余传 None）。

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

        # —— 多轮澄清路径（mode=multi_turn 且有 env.reply）：确定性，不调 LLM#2 ——
        # route() 在 multi_turn 下无条件返回 INTENT_MULTI_TURN，会遮掉取消/延长等
        # 真实意图；故用 mode=None 的 analyze_meeting_query 判真实意图后分派：
        # - 预订：缺槽澄清 + 确认解锁 + 确定性 book（mt_0001/0201/0202/0003/0009）；
        # - 取消/延长等：无缺槽，走既有确定性 _rule_plan（mt_0004/0011/0204/0205）。
        if mode == MODE_MULTI_TURN and hasattr(env, "reply") and callable(getattr(env, "reply")):
            meeting_plan = self._multi_turn_plan(user_query, now_iso, env)
            self.last_planner_gateway = None
            self.last_timings = {
                "recognize_s": round(ir.elapsed_s, 3),
                "orchestrate_s": round(meeting_plan.elapsed_s, 3),
                "exec_s": 0.0,
                "skill_total_s": round(time.monotonic() - start, 3),
            }
            return ir, meeting_plan

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

    def _multi_turn_plan(
        self, user_query: str, now_iso: str, env: Any
    ) -> MeetingOpPlan:
        """多轮澄清路径：真实意图判定 → 预订澄清+确认 / 取消延长走确定性规则。

        mode=multi_turn 的 route() 短路到 INTENT_MULTI_TURN，这里用 mode=None
        重新路由拿到真实意图（book/cancel/extend…）：
        - 预订（INTENT_BOOK）：按约束提取器的缺槽声明澄清（build_meeting_specs）
          → 发确认语解锁 booking.create → 确定性 book op（gold 步数与轨迹对齐：
          clarify×N + confirm + room.list + create）；
        - 其余（cancel/extend/query/participant/rebook）：query 已带全部所需槽位，
          无缺槽可问 → 复用 _rule_plan（mode=None 避免短路）。

        Returns:
            MeetingOpPlan（source="rule_clarify" 标记多轮澄清路径）。
        """
        start = time.monotonic()
        intent, c = analyze_meeting_query(user_query, now_iso, None)
        if intent != INTENT_BOOK:
            # 取消/延长/查询/参会人等非预订意图：query 可能缺订单号（多轮 case 的
            # missing_slots=['order_id']）→ 先澄清订单号，再走确定性规则（gateway
            # =None → planner 内部 _rule_plan + 全部后处理），把澄清出的订单号注入
            # 各 op target（取消/延长直给分支免 booking.list 定位）。
            clarified = clarify_slots(env, user_query or "", [build_order_id_spec(c)])
            plan = self.planner.plan(
                user_query, now_iso, None, None, sub_query=user_query
            )
            order_id = clarified.get("order_id") or c.order_id_hint
            if order_id and plan.ops:
                for op in plan.ops:
                    # clarified 标记：order_id 经对话澄清而非 query 直给 → 执行层
                    # extend 需先定位/探测（mt_0011/0205），非条件延长才做富化定位
                    # （mt_0204）；单轮直给订单号则直延省一次 list。
                    op.target = {
                        **op.target,
                        "order_id": order_id,
                        "clarified": True,
                    }
            return plan

        # 预订：缺槽澄清（缺槽判定对齐 gold missing_slots，见 build_meeting_specs）。
        clarified = clarify_slots(env, user_query or "", build_meeting_specs(now_iso, c))
        apply_clarified(c, clarified)
        # 确认门：gold 要求 booking.create 前发确认语（CONFIRM_PATTERNS 解锁）。
        env.reply(CONFIRM_REPLY)
        target = MeetingOpPlanner._constraints_to_target(c)
        plan = MeetingOpPlan(
            ops=[MeetingOp("book", target)],
            source="rule_clarify",
            confidence=0.9,
        )
        plan.elapsed_s = round(time.monotonic() - start, 3)
        return plan
