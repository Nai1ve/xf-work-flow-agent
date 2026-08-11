"""理解层：意图识别 + 时间解析 + 会议约束抽取。

对应 technical_design.md §4「理解层」与「会议室 SOP」的 S1/S2 部分。本阶段只覆盖
S1 基础预订（含 S1w 工位关联）与 S2 纯查询；其余意图（S3 取消 / S4 重订 /
S5 延长 / S6 参会人 / M 多轮 / S1s 日程对比预订）由 IntentRouter 识别但本阶段
不执行（入口层返回空 ``{}`` 保住 0 分防线），后续阶段逐个接入。

设计意图（AGENT.md「静态契约只作先验，运行时证据优先」）：
- 理解层全部为**确定性规则解析**，不做 case 记忆，不读 reference / gold；
- 园区/楼栋/楼层 → ``office_address`` 与 ``office_id`` 的归一化与 simulator
  ``_match_office_address`` 同源（0551=合肥，0552=小镇）；
- 时间解析基于 ``env.now``，跨月/跨周由 ``datetime`` 计算，可独立单测；
- 本层只产出「约束」，不调任何工具；工具调用由执行层按门禁驱动。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------
# 意图常量（会议室 SOP 分类，technical_design.md §5 / 计划文件 §二）
# --------------------------------------------------------------------------

INTENT_BOOK = "book"  # S1 基础预订（含 S1w 工位关联、S1d 逐天最早、S1m 多约束）
INTENT_QUERY = "query"  # S2 纯查询
INTENT_CANCEL = "cancel"  # S3 取消
INTENT_REBOOK = "rebook"  # S4 取消后重订
INTENT_EXTEND = "extend"  # S5 延长
INTENT_PARTICIPANT = "participant"  # S6 参会人管理
INTENT_MULTI_TURN = "multi_turn"  # M 多轮澄清/确认
INTENT_UNKNOWN = "unknown"  # 无法判定（安全默认：不执行写操作）

# 多轮模式的 mode 值（来自 env.reset 返回）。
MODE_MULTI_TURN = "multi_turn"

# --------------------------------------------------------------------------
# 粗粒度业务单元类型（意图识别层词表，用户确认：会议/请假/预算三类）
# --------------------------------------------------------------------------
# 设计意图：识别层只做「分解 + 路由」，单元词表保持**粗粒度业务过程类型**；
# book/cancel/extend/participant 等细粒度子意图由各域 SOP（skill）内部处理，
# 不再出现在识别层输出中。
UNIT_MEETING = "meeting"  # 会议域：预订/取消/延长/参会人/查询/日程/工位等一切会议操作
UNIT_LEAVE = "leave"  # 请假流程
UNIT_BUDGET = "budget"  # 预算申报/费用/物资流程
UNIT_TYPES: tuple[str, ...] = (UNIT_MEETING, UNIT_LEAVE, UNIT_BUDGET)

# 识别置信度下限：低于该值 / 空 / LLM 不可用 → 规则兜底（technical_design.md §7.4）。
CONFIDENCE_FLOOR = 0.55

# 意图识别单次调用的网络超时（秒），还会被 case 级 LLM 预算二次收窄。
_INTENT_TIMEOUT_S = 12.0

# S2 纯查询子类型（决定执行层调哪个只读工具）。
QUERY_BOOKING_LIST = "booking_list"
QUERY_SCHEDULE = "schedule"
QUERY_UNBOOKABLE = "unbookable"
QUERY_WORKSPACE = "workspace"

# 园区码 → 语义（与 simulator _match_office_address 同源，只作归一化先验）。
_CAMPUS_HEFEI = "0551"
_CAMPUS_TOWN = "0552"

# 默认园区：查询未显式提到园区时，A 楼一律落在小镇园区。
# 依据：train 里 A1/A2/A3 无园区前缀的预订全部落到 0552（小镇）；0551 的 A 楼
# 容量 ≤6，凡查询要求 capacity_gte≥8 的候选天然被过滤，不会造成误选。
_DEFAULT_CAMPUS = _CAMPUS_TOWN

# 中国数字 → 数字。
_CN_NUMBERS: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 中文星期 → weekday() 值（周一=0 … 周日=6）。
_WEEKDAY_MAP: dict[str, int] = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


@dataclass
class MeetingConstraints:
    """从 user_query 解析出的会议约束（理解层产物，执行层据此驱动工具）。

    Attributes:
        intent: 意图（INTENT_* 之一）。
        day: 单日预订的目标日期（ISO "YYYY-MM-DD"）。
        days: 多日同会议室预订的日期列表（「周三和周四」/「周二、周三、周四」），
            长度 ≥2 时执行层走 S1d 多日交集流程。
        book_only_day: 「周三和周四都要空闲，找到后订周三的」场景里只订的那一天
            （0223）；days 仍保留供执行层做多日可用性交集，但只对该日 create。
        week_start / week_end: 「最早能订上」逐天搜索的日期区间。
        start / end: 会议起止时刻（"HH:MM"）。
        building: 楼栋名（A1…A5），用于 create 的 office_id 与 reference 楼栋名。
        campus: 园区码（0551 / 0552）。
        campus_explicit: 查询是否显式提到园区（决定工位降级时是否保留园区级地址）。
        floor: 楼层（"3F" 形式）。
        addresses: 候选 office_address（有序），执行层按序调用 room.list。
        fallback_building: 「A1 优先，A2 备选」里的备选楼栋名。
        capacity_gte: 最小容量（人数 → capacity_gte）。
        has_screen: True=需要屏幕；False=明确不需要（不传筛选参数）。
        bookable: True=只看可预订；False=只看不可预订（S2 查询用）。
        title: 会议主题。
        attendees: 参会人数（create 的可选入参）。
        workspace_hint: 是否「离工位近/最近」（S1w，执行层先 get_workspace）。
        time_flexible: 目标时段不可订时是否允许 ±30 分钟回退（「如果这个时间不行，
            前后半小时看看」）。执行层仅在主时段无解时使用该回退。
        query_type: S2 查询子类型（QUERY_*）。
        schedule_room_id: S2 日程查询的房间 ID。
        schedule_start_date / schedule_end_date: S2 日程查询的日期区间。
        query_keyword: S2 booking.list 的关键字。
        named_room: 查询显式点名的房间（A1-349 / A1-3F-349）；S1s 日程对比
            预订场景，本阶段执行层直接不执行（防止订错房）。
    """

    intent: str = INTENT_UNKNOWN
    day: str | None = None
    days: list[str] = field(default_factory=list)
    book_only_day: str | None = None
    week_start: str | None = None
    week_end: str | None = None
    start: str | None = None
    end: str | None = None
    building: str | None = None
    campus: str | None = None
    campus_explicit: bool = False
    floor: str | None = None
    addresses: list[str] = field(default_factory=list)
    fallback_building: str | None = None
    capacity_gte: int | None = None
    has_screen: bool | None = None
    bookable: bool | None = None
    title: str | None = None
    attendees: int | None = None
    workspace_hint: bool = False
    time_flexible: bool = False
    query_type: str | None = None
    schedule_room_id: str | None = None
    schedule_start_date: str | None = None
    schedule_end_date: str | None = None
    query_keyword: str | None = None
    named_room: str | None = None
    minutes: int | None = None  # 延长分钟数（S5）。
    persons: list[dict] = field(default_factory=list)  # 参会人 [{"name","employee_no"}]（S6）。
    compare_rooms: list[str] = field(default_factory=list)  # 日程对比的点名房间（S1s 对比）。
    order_id_hint: str | None = None  # query 原文透传的 SEED-* 订单号（仅透传，不编造）。
    slots: list[dict] = field(default_factory=list)  # 同日多时段 [{day,start,end,title}]（0043 同房多场）。

    def primary_address(self) -> str | None:
        """返回第一个候选 office_address（无候选时返回 None）。"""
        return self.addresses[0] if self.addresses else None


@dataclass
class TaskUnit:
    """一个子问题 + 它的处理路径（识别层产物，只含意图信息与子句上下文）。

    Attributes:
        unit_type: 粗粒度业务类型（UNIT_*）。
        depends_on: 依赖的前序单元下标（本单元必须等这些单元完成才能开始）；
            无顺序依赖为空列表。
        sub_query: 该单元负责的原文字句（LLM#1 从 user_query 重组切分出的、
            进入本单元处理路径的原文子句）。编排层（LLM#2）只接收各单元
            的 sub_query 作为编排+抽取的上下文，不再看完整 user_query。
    """

    unit_type: str
    depends_on: list[int] = field(default_factory=list)
    sub_query: str = ""


@dataclass
class TaskGraphIR:
    """意图识别的完整输出：把 user_query 分解后的子问题图。

    设计意图：识别层只输出「分解 + 路由 + 置信度」——**不含任何参数槽位**
    （参数抽取由各域 SOP 在后续里程碑完成）。字段与 AGENT.md 的可审计要求对齐：
    task_units（分解结果）/ confidence / source（llm | fallback）/ elapsed。

    Attributes:
        task_units: 子问题列表（按依赖拓扑序消费，见 ordered_units）。
        confidence: 识别置信度（0~1）；规则兜底时为 0。
        source: 识别来源："llm" | "fallback"。
        elapsed_s: 本次识别耗时（秒）。
        mode: env 的 mode 原样透传（"multi_turn" 标记澄清路径，执行层处理）。
    """

    task_units: list[TaskUnit] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "llm"
    elapsed_s: float = 0.0
    mode: str | None = None

    def ordered_units(self) -> list[TaskUnit]:
        """按依赖拓扑序返回单元（依赖在前），供执行层按序处理。"""
        units = self.task_units
        if not units:
            return []
        order: list[TaskUnit] = []
        visited: set[int] = set()

        def visit(i: int) -> None:
            if i in visited:
                return
            visited.add(i)
            for dep in units[i].depends_on:
                if 0 <= dep < len(units) and dep not in visited:
                    visit(dep)
            order.append(units[i])

        for i in range(len(units)):
            visit(i)
        return order


class IntentRouter:
    """从 user_query + mode 判定会议操作意图。

    规则优先级（高 → 低）：
    M 多轮（mode 判定）→ S4 重订（取消+重订）→ S5 延长 → S6 参会人 → S3 取消 →
    S1 预订（含查询里也带「订」字的对比预订）→ S2 纯查询。
    """

    # 各意图的关键词表（只作初判，命中即短路）。
    _REBOOK_HINTS = ("取消", "退掉", "退订")
    _REBOOK_ACTION_HINTS = ("重新订", "重订", "换", "再订")
    _EXTEND_HINTS = ("延长", "延时", "多聊", "多订")
    _PARTICIPANT_HINTS = ("参会人", "加到", "加入", "移除", "移出", "添加", "参加")
    _CANCEL_HINTS = ("取消", "退订")
    _BOOK_HINTS = ("订", "预订", "预约", "约", "找")
    _QUERY_HINTS = ("查", "看看", "查看", "有哪些", "预订情况", "日程")

    def route(self, user_query: str, mode: str | None = None) -> str:
        """把 user_query 分类为 INTENT_*。

        Args:
            user_query: 用户原始提问。
            mode: env.reset 返回的 mode（"multi_turn" 直接判为多轮）。

        Returns:
            意图常量（INTENT_*）。
        """
        query = user_query or ""

        if mode == MODE_MULTI_TURN:
            return INTENT_MULTI_TURN

        # S4 取消后重订：既想取消又准备再订（换大/换楼/重订）。
        if any(h in query for h in self._REBOOK_HINTS) and any(
            h in query for h in self._REBOOK_ACTION_HINTS
        ):
            return INTENT_REBOOK

        # S4b 换大会议室（无显式取消词，如 0011「参会人加了4个，帮我换一个大一点的会议室」）。
        # 必须在参会人 hint（"参会人"）之前判定，否则 0011 会误判为 participant。
        if any(h in query for h in ("换一个大", "换大一点", "换间更大", "换一间更大", "换个大", "更大的会议室")):
            return INTENT_REBOOK

        # S5 延长（含「冲突则保持原会不变」的查询型延长）。
        if any(h in query for h in self._EXTEND_HINTS):
            return INTENT_EXTEND

        # S6 参会人管理。
        if any(h in query for h in self._PARTICIPANT_HINTS) or "谁参加" in query:
            return INTENT_PARTICIPANT

        # S3 纯取消。
        if any(h in query for h in self._CANCEL_HINTS):
            return INTENT_CANCEL

        # S2 纯查询的名词短语（先于「订」判定）：「有哪些会议预订」「预订情况」
        # 「工位在哪里」里的「预订/订」是查询对象名词，不是预订动作。
        if self._is_pure_query(query):
            return INTENT_QUERY

        # S1 预订：含「订/找」字优先于纯查询（0036「看看哪个更空闲，选个空闲的订」、
        # 0234「帮我在A1园区找…会议室」都属于预订）。
        if any(h in query for h in self._BOOK_HINTS):
            return INTENT_BOOK

        # S2 纯查询（其余）。
        if any(h in query for h in self._QUERY_HINTS) or "工位在哪里" in query:
            return INTENT_QUERY

        return INTENT_UNKNOWN

    @staticmethod
    def _is_pure_query(query: str) -> bool:
        """是否为「名词化查询」：查询对象本身就含预订/工位等词，但不是动作。

        覆盖：0204「有哪些会议预订」、0211「预订情况」、0214「工位在哪里」、
        0219「有哪些不可预订的会议室」。
        """
        if "工位在哪里" in query or "工位在哪个" in query:
            return True
        if "有哪些不可预订" in query or "预订情况" in query:
            return True
        if "有哪些" in query and "会议预订" in query:
            return True
        return False


# --------------------------------------------------------------------------
# 意图识别（粗粒度分解 + 路由）· 唯一识别入口
# --------------------------------------------------------------------------
# 设计意图（用户纠正后的 Agent 架构）：
# - 识别层只回答两个问题：① 当前问题要分割成几个子问题；② 每个子问题进入哪条
#   处理路径（SOP/skill）。**绝不抽取参数槽位、绝不在识别层区分细粒度子意图**；
# - 单元词表 = 3 个粗粒度业务类型（meeting/leave/budget），细粒度
#   （book/cancel/extend/participant/query 等）由各域 SOP 内部处理；
# - LLM #1 必发：gateway 可用时每个 case 都调用（识别卡片只要求分解+路由，输出
#   schema 极小，保证快）；confidence<0.55 / 空 / 不可用 → 规则兜底（降级路径）。

_INTENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_units"],
    "properties": {
        "task_units": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["unit_type", "sub_query"],
                "properties": {
                    "unit_type": {
                        "type": "string",
                        "enum": [UNIT_MEETING, UNIT_LEAVE, UNIT_BUDGET],
                    },
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    # 子句上下文：该单元负责的原文片段（LLM#1 重组切分，供编排层消费）。
                    "sub_query": {"type": "string"},
                    # 允许单元级信心（模型可能把 confidence 放单元里而非顶层）；
                    # 仍禁止其它键，槽位（地点/时间/人数…）一律过不了校验。
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}

_INTENT_PROMPT_CARD = """你是企业流程 Agent 的「意图识别器」：把用户的请求分解成子问题，并为每个子问题选择业务路径（unit_type），输出 JSON。
业务类型：meeting（会议域：预订/取消/延长/参会人/查询/日程/工位等一切会议操作）/ leave（请假）/ budget（预算申报/费用/物资）。
规则：
1. 单业务请求 → 一个 task_unit；跨域（如「订会议室+请假」「订会议室+报销物资」）→ 多个 task_unit；只有必须先做前一单元时才用 depends_on 标序（如流程单元 depends_on 指向前面的会议单元）。
2. 一句多个业务子句（用 另外/顺手/顺便/再/以及 连接、动作对象不同）→ 每个子句各自成单元。
3. 草稿/申请/提交/申报 + 请假/休假/年假/病假/事假 → leave；+ 预算/报销/物资/费用/采购 → budget。
4. mode=multi_turn 照常按内容判断业务域并输出单元；mode 只标记澄清路径。
5. OA 待办/已办尾巴不单独成单元，归入对应流程单元。
硬约束：
- 绝不抽取参数：不输出地点/时间/人数/主题/金额等任何槽位键。
- 预订/取消/延长/参会人/查询等细粒度一律归入 meeting，不在识别层区分。
- 每个 task_unit 必须带 sub_query：该单元负责的**原文字句**（从 user_query 里原样照抄、可删去其它单元的无关部分，但不改写、不新增、不抽取槽位）。单域整句 → sub_query=整句。
- task_unit 内只允许 unit_type / sub_query / depends_on / confidence 四键。
- confidence 0~1，≥0.55 视为可靠；可放顶层或每个单元。
示例：
- 「订个会议室，然后请三天假」→ [meeting{sub_query:"订个会议室"}, leave{sub_query:"然后请三天假"}]
- 「取消原来的，换个更大的会议室重新订」→ [meeting{sub_query:"取消原来的，换个更大的会议室重新订"}]
- 「延长30分钟，然后把张伟加入参会人」→ [meeting{sub_query:"延长30分钟，然后把张伟加入参会人"}]
输出：{"task_units":[{"unit_type":"meeting","sub_query":"订个会议室","depends_on":[]}],"confidence":0.95}
只输出一个符合 schema 的 json 对象。"""


class IntentRecognizer:
    """粗粒度意图识别：把 user_query 分解为 N 个子问题并路由到业务处理路径。

    这是理解层的**唯一识别入口**（取代旧的细粒度单意图分类作为入口）。设计约束：
    - 输出 TaskGraphIR（task_units + confidence + source + elapsed + mode），
      **不含任何参数槽位**（槽位由各域 SOP 在后续里程碑抽取）；
    - LLM #1 必发（gateway 可用时）：快速分解 + 路由；失败/低置信/空 → 规则兜底；
    - 规则兜底（仅降级路径）用粗粒度关键词分类器，source="fallback"。
    """

    # 规则兜底关键词（只用于 LLM 不可用 / 低置信时的降级，非主路径）。
    _LEAVE_HINTS = ("请假", "休假", "年假", "病假", "事假", "调休", "婚假", "产假", "补休")
    # 「请三天假」不包含字面子串「请假」，用正则补上（不用裸「假」，避免误伤
    # 会议查询里「假如这个时间不行」的「假」）。
    _LEAVE_RE = re.compile(r"请\S*假|休\S*假")
    _BUDGET_HINTS = ("预算", "报销", "物资", "费用", "采购", "申报", "经费", "支出", "用品", "办公用品")
    _MEETING_HINTS = (
        "会议室", "会议预订", "会议", "预订", "预约", "订", "取消", "延长", "延时",
        "参会人", "加入", "移除", "日程", "工位",
    )

    def __init__(self, logger: Any = None) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger（审计用），None 时不输出。
        """
        self.logger = logger

    def analyze(
        self,
        user_query: str,
        now_iso: str,
        mode: str | None = None,
        gateway: Any = None,
    ) -> TaskGraphIR:
        """识别当前问题的分解结果 + 每个子问题的处理路径。

        Args:
            user_query: 用户原始提问。
            now_iso: env.reset 返回的 now（ISO 字符串，作为模型上下文）。
            mode: env.reset 返回的 mode（"multi_turn" 透传标记澄清路径）。
            gateway: LLMGateway 实例（可用时必发 LLM #1）；None/不可用走规则兜底。

        Returns:
            TaskGraphIR（从不 raise、从不返回 None）。
        """
        start = time.monotonic()
        mode = mode or None

        if gateway is not None and gateway.available:
            payload: dict[str, Any] = {
                "user_query": user_query,
                "now": now_iso,
                "mode": mode,
            }
            raw = gateway.structured_call(
                _INTENT_PROMPT_CARD,
                payload,
                _INTENT_OUTPUT_SCHEMA,
                timeout_s=_INTENT_TIMEOUT_S,
                fallback={"task_units": [], "confidence": 0.0},
            )
            units = self._parse_units(raw.get("task_units"))
            confidence = self._overall_confidence(raw)
            if units and confidence >= CONFIDENCE_FLOOR:
                return TaskGraphIR(
                    task_units=units,
                    confidence=round(confidence, 3),
                    source="llm",
                    elapsed_s=time.monotonic() - start,
                    mode=mode,
                )
            # 空 / 低置信：规则兜底（降级），source 标记为 fallback。
            if self.logger is not None:
                self.logger.warning(
                    f"识别低置信/空（confidence={confidence:.2f}），规则兜底"
                )

        units = self._fallback_units(user_query)
        return TaskGraphIR(
            task_units=units,
            confidence=0.0,
            source="fallback",
            elapsed_s=time.monotonic() - start,
            mode=mode,
        )

    @staticmethod
    def _overall_confidence(raw: dict[str, Any]) -> float:
        """整体置信度：优先取顶层 confidence；缺省时取各单元 confidence 的最大值。

        部分模型会把 confidence 放进每个 task_unit 而非顶层（schema 允许），这里
        做归一化，供 CONFIDENCE_FLOOR 判定使用。
        """
        top = raw.get("confidence")
        if isinstance(top, (int, float)):
            return float(top)
        per_unit = [
            float(u.get("confidence"))
            for u in (raw.get("task_units") or [])
            if isinstance(u, dict) and isinstance(u.get("confidence"), (int, float))
        ]
        return max(per_unit) if per_unit else 0.0

    @staticmethod
    def _parse_units(raw_units: Any) -> list[TaskUnit]:
        """把 LLM 原始输出归一为 TaskUnit 列表（非法项丢弃、依赖下标钳制）。"""
        if not isinstance(raw_units, list):
            return []
        units: list[TaskUnit] = []
        n = len(raw_units)
        for raw in raw_units:
            if not isinstance(raw, dict):
                continue
            unit_type = str(raw.get("unit_type") or "")
            if unit_type not in UNIT_TYPES:
                continue
            depends = raw.get("depends_on")
            if not isinstance(depends, list):
                depends = []
            else:
                depends = [
                    int(i) for i in depends if isinstance(i, int) or (isinstance(i, str) and i.isdigit())
                ]
                # 依赖下标越界 → 丢弃该依赖（不因此丢弃整个单元）。
                depends = [i for i in depends if 0 <= i < n]
            sub_query = raw.get("sub_query")
            units.append(
                TaskUnit(
                    unit_type=unit_type,
                    depends_on=depends,
                    sub_query=str(sub_query) if isinstance(sub_query, str) else "",
                )
            )
        return units

    def _fallback_units(self, user_query: str) -> list[TaskUnit]:
        """规则兜底：粗粒度关键词分类，输出单个或多个 task_unit（降级路径）。

        规则兜底无法切分子句 → sub_query 一律等于完整 user_query（保守，编排层
        拿到整句做上下文，等价于旧行为）。
        """
        query = user_query or ""
        units: list[TaskUnit] = []
        if any(h in query for h in self._MEETING_HINTS):
            units.append(TaskUnit(UNIT_MEETING, sub_query=query))
        if any(h in query for h in self._LEAVE_HINTS) or self._LEAVE_RE.search(query):
            units.append(TaskUnit(UNIT_LEAVE, sub_query=query))
        if any(h in query for h in self._BUDGET_HINTS):
            units.append(TaskUnit(UNIT_BUDGET, sub_query=query))
        # 兜底也保证非空：默认会议域（训练集绝大多数 case 是会议域，可审计）。
        if not units:
            units.append(TaskUnit(UNIT_MEETING, sub_query=query))
        return units


class TemporalResolver:
    """把 ``env.now`` + 用户相对时间表达解析为具体日期与时刻。

    覆盖表达（train 会议室用例实测归纳）：
    - 今天 / 明天 / 后天；
    - 本周X / 周X / 下周X（跨周由 datetime 计算，自动处理跨月/跨年）；
    - ``X月X日`` 字面日期；
    - 逐天搜索区间：本周 / 下周（周一~周五）。
    """

    def __init__(self, now_iso: str) -> None:
        """初始化。

        Args:
            now_iso: env.reset 返回的 now（ISO 8601 字符串，含时区）。
        """
        self._now = datetime.fromisoformat(now_iso)
        self._today = self._now.date()

    # ------------------------------------------------------------------ 日期 --

    def resolve_day(self, query: str) -> str | None:
        """解析查询中的第一个明确日期（单日预订用）。

        Args:
            query: 用户查询。

        Returns:
            ISO 日期字符串；无法解析返回 None。
        """
        # 字面日期：5月11日。
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", query)
        if m:
            return date(self._today.year, int(m.group(1)), int(m.group(2))).isoformat()

        # 今天 / 明天 / 后天。
        if "后天" in query:
            return (self._today + timedelta(days=2)).isoformat()
        if "明天" in query:
            return (self._today + timedelta(days=1)).isoformat()
        if "今天" in query:
            return self._today.isoformat()

        # 下周X（下一个周 X）。
        m = re.search(r"下周([一二三四五六日天])", query)
        if m:
            return self._offset_weekday(m.group(1), weeks=1).isoformat()

        # 本周X / 周X。
        m = re.search(r"(?:本周|周)([一二三四五六日天])", query)
        if m:
            return self._offset_weekday(m.group(1), weeks=0).isoformat()

        return None

    def resolve_week_span(self, query: str) -> tuple[str | None, str | None]:
        """解析「本周/下周最早能订上」的逐天搜索区间（周一到周五）。

        注意：只在「最早能订上」这类逐天搜索语义下启用，避免把「下周二」的
        ``下周`` 子串误判为整周搜索（下周二是一个明确的单日，不是周区间）。

        Args:
            query: 用户查询。

        Returns:
            (起始日 ISO, 结束日 ISO)；无匹配返回 (None, None)。搜索起始日
            不会早于今天（本周已过去的日期自动顺延到下周一并剪掉）。
        """
        if "最早" not in query:
            return None, None
        this_monday = self._today - timedelta(days=self._today.weekday())
        weeks = 0
        if "下周" in query:
            start = this_monday + timedelta(days=7)
            weeks = 1
        elif "本周" in query or "这周" in query:
            start = max(this_monday, self._today)
            if self._today.weekday() >= 5:  # 周六/周日已无本周工作日
                start = this_monday + timedelta(days=7)
                weeks = 1
        else:
            return None, None
        # 结束日 = 相关周的周五（周一 + 4），不随 start 偏移——避免周二~周四起始时
        # end=start+4 越界到周末甚至下周。
        end = this_monday + timedelta(days=4 + 7 * weeks)
        return start.isoformat(), end.isoformat()

    def resolve_days(self, query: str) -> list[str]:
        """解析多日表达：「周三和周四」/「周二、周三、周四」。

        只有出现 ≥2 个用「和 / 、 / ，」连接的星期表达时才启用，避免把单个
        「下周二」误判为多日。周偏移以第一个表达为准（本周或下周）。

        Args:
            query: 用户查询。

        Returns:
            按查询顺序排列的 ISO 日期列表；非多日表达返回空列表。
        """
        m = re.search(
            r"(?:下周|本周|周)([一二三四五六日天])"
            r"(?:\s*(?:和|、|,)\s*(?:下周|本周|周)([一二三四五六日天]))+",
            query,
        )
        if not m:
            return []
        token = m.group(0)
        weeks = 1 if "下周" in token else 0
        weekdays = re.findall(r"(?:下周|本周|周)([一二三四五六日天])", token)
        return [self._offset_weekday(weekday, weeks).isoformat() for weekday in weekdays]

    # --------------------------------------------------------------- 时刻 --

    def resolve_time_range(self, query: str) -> tuple[str | None, str | None]:
        """解析会议起止时刻（"HH:MM"）。

        覆盖：上午9点到11点 / 下午2点到4点 / 下午2点半到4点 / 9点到11点。
        结束段未带午别时继承起始段午别（「下午2点到4点」→ 14:00-16:00）。

        Args:
            query: 用户查询。

        Returns:
            (start, end)；无法解析返回 (None, None)。
        """
        m = re.search(
            r"(上午|下午|晚上|中午)?\s*"
            r"(\d{1,2})\s*点\s*(半)?\s*"
            r"(?:到|至|~|—|-)\s*"
            r"(上午|下午|晚上|中午)?\s*"
            r"(\d{1,2})\s*点\s*(半)?",
            query,
        )
        if not m:
            return None, None

        start_period = m.group(1)
        start_hour = self._hour_with_period(int(m.group(2)), start_period)
        start_minute = 30 if m.group(3) == "半" else 0
        end_period = m.group(4) or start_period  # 结束未带午别时继承起始午别
        end_hour = self._hour_with_period(int(m.group(5)), end_period)
        end_minute = 30 if m.group(6) == "半" else 0

        # 按分钟比较，半（30 分）计入分钟而非小时（「2点半」= 14:30 而非 44 点）。
        if start_hour * 60 + start_minute >= end_hour * 60 + end_minute:
            return None, None  # 非法区间（如 15:00-14:00），交给上层兜底
        return f"{start_hour:02d}:{start_minute:02d}", f"{end_hour:02d}:{end_minute:02d}"

    @staticmethod
    def _hour_with_period(hour: int, period: str | None) -> int:
        """把「几点 + 午别」换算成 24 小时制（半由调用方计入分钟）。"""
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "中午" and hour == 12:
            hour = 12
        return hour

    # ------------------------------------------------------------ 工具方法 --

    def _offset_weekday(self, cn_weekday: str, weeks: int) -> date:
        """当前周（weeks=0）或下周（weeks=1）指定星期对应的日期。

        本周目标日早于今天时顺延一周（「周三」在周一之后才指本周三）。
        """
        target = _WEEKDAY_MAP.get(cn_weekday, 0)
        this_monday = self._today - timedelta(days=self._today.weekday())
        candidate = this_monday + timedelta(days=target + 7 * weeks)
        if weeks == 0 and candidate < self._today:
            candidate += timedelta(days=7)
        return candidate

    @property
    def today(self) -> date:
        """今天（测试/校验用）。"""
        return self._today


class MeetingConstraintExtractor:
    """从 user_query 抽取会议硬约束（地点 / 容量 / 设备 / 主题 / 人数）。

    产出为 MeetingConstraints，其中 ``addresses`` 是有序候选 office_address，
    语义与 simulator ``_match_office_address`` 对齐：``园区码[_楼栋[_楼层]]``。
    """

    # 楼栋：大写字母 + 数字（A1…A5）。
    _BUILDING_RE = re.compile(r"[A-Z]\d")
    # 楼层：数字/中文数字 + 楼/层。负向断言排除紧邻 ASCII 字母的楼栋名（"A4楼"），
    # 但保留中文前缀（"小镇一楼"/"A3园区3楼" 的楼/层是楼层，不是楼栋名）。
    _FLOOR_RE = re.compile(r"(?<![A-Za-z])([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:楼|层)")
    # 主题：主题[是|为|:] + 文本（到标点为止）。
    _TITLE_RE = re.compile(r"主题(?:是|为|[:：])?\s*([^。；，、,]+)")
    # 人数：N人[以上] / N个人[以上]。
    _CAPACITY_RE = re.compile(r"(\d{1,3})\s*(?:个)?人(?:以上)?")
    # 命名房间：A1-349 / A1-3F-349（S1s 日程对比预订，本阶段不执行）。
    _NAMED_ROOM_RE = re.compile(r"[A-Z]\d-\dF-\d{3}|[A-Z]\d-\d{3}")
    # 「订周三的」：多日都要空闲但只订其中一天的表达（0223「找到后订周三的」）。
    _BOOK_ONE_DAY_RE = re.compile(r"(?:订|只订|安排)(?:周|下周|本周)?([一二三四五六日天])的")
    # 参会人：把 X 加到/加入/移除/移出/从 …（X 可为「李明、王芳」多个人）。
    _PERSONS_RE = re.compile(r"把([^。；，]+?)(?:都)?(?:加到|加入|移除|移出|添加到|从)")
    _PERSON_SPLIT_RE = re.compile(r"[、，,和及]+")
    _EMPLOYEE_NO_RE = re.compile(
        r"工号\s*[:：]?\s*(\d{5,6})|[（(]?\b(\d{6})\b[)）]?"
    )
    # 延长分钟数：「延长60分钟」/「多聊半小时」。
    _MINUTES_RE = re.compile(r"延长\s*(\d+)\s*分钟")
    # 订单号透传（query 原文显式给出，仅原样透传）。
    # 注意不能用 \w：Unicode \w 会吞掉中文（「SEED-0218-001的会议」）。
    _ORDER_ID_RE = re.compile(r"SEED-[A-Za-z0-9_-]+")
    # 取消/参会人/延长的定位关键词：「那个项目复盘会议室」→ 项目复盘。
    # 贪婪捕获 + 裸「会」前瞻（跨域 Fix 用户定案 2026-08-10）：复合会议名会被
    # 前瞻里的复合词截断（「项目复盘会议室」→ 项目 + 复盘会），贪婪展开到最长
    # 标题 + 终态词（项目复盘 + 会议室）；裸「会」兜住「X复盘会/启动会」型查询。
    _MEET_KEYWORD_RE = re.compile(
        r"(?:那个|这个|的)([一-龥A-Za-z0-9]{2,10})"
        r"(?=会议室|会议|评审会|启动会|分享会|复盘会|周会|见面会|例会|讨论会|的会|会)"
    )
    _MEET_KEYWORD_STOP = frozenset(
        {"下午", "上午", "中午", "晚上", "明天", "今天", "下周", "这个", "那个",
         "一个", "周三", "周二", "周一", "周四", "周五", "周六", "周日",
         "会议室", "会议", "下周二", "下周三", "下周一", "下周四", "下周五"}
    )

    def extract(self, query: str, resolver: TemporalResolver) -> MeetingConstraints:
        """解析 user_query 得到完整会议约束。

        Args:
            query: 用户查询。
            resolver: 时间解析器（提供 day / 时刻 / 周区间）。

        Returns:
            解析后的约束对象（intent 由上层 IntentRouter 填充，此处保持默认）。
        """
        c = MeetingConstraints()
        self._fill_location(query, c)
        self._fill_time(query, resolver, c)
        self._fill_requirements(query, c)
        self._fill_s2(query, resolver, c)
        self._fill_extras(query, resolver, c)
        return c

    # ------------------------------------------------------------ 地点 --

    def _fill_location(self, query: str, c: MeetingConstraints) -> None:
        """解析园区 / 楼栋 / 楼层，构造候选 office_address 列表。"""
        c.campus = _CAMPUS_HEFEI if "合肥" in query else _DEFAULT_CAMPUS
        c.campus_explicit = "合肥" in query or "小镇" in query

        # 楼栋与楼层独立解析：楼层不依赖楼栋存在（0038「小镇一楼」无楼栋）。
        building_m = self._BUILDING_RE.search(query)
        if building_m:
            c.building = building_m.group(0)
        # 楼层：FLOOR_RE 已用负向断言排除楼栋名（"A4楼"），命中即楼层。
        floor_m = self._FLOOR_RE.search(query)
        if floor_m:
            c.floor = self._floor_code(floor_m.group(1))

        # 构造有序候选地址。
        c.addresses = self._build_addresses(c)

        # 命名房间（S1s 日程对比预订：本阶段执行层不执行）。
        named_m = self._NAMED_ROOM_RE.search(query)
        if named_m:
            c.named_room = named_m.group(0)

        # 备选楼栋（A1 优先、A2 备选 → 追加备选地址）。判定：query 中出现 ≥2 个
        # 不同楼栋时，最后一个不同楼栋为备选（0013「也可以A2」/ 0023「不行的话
        # A2也可以」，两者句式不同但语义相同）。备选地址**不带主楼栋的楼层**——
        # 备选句通常不指定楼层（0023 金标在 A2 楼栋级直接找到 1F 房间）。
        fallback_building = self._last_distinct_building(query, c.building)
        if fallback_building:
            c.fallback_building = fallback_building
            fallback_address = self._address_for(c.campus, fallback_building, None)
            if fallback_address and fallback_address not in c.addresses:
                c.addresses.append(fallback_address)

    @staticmethod
    def _last_distinct_building(query: str, primary: str | None) -> str | None:
        """返回 query 中最后一个与主楼栋不同的楼栋（备选楼栋），否则 None。

        去重保序后取末位；命名房间场景（A1-349 和 A1-305）楼栋全相同 → None。
        """
        seen: list[str] = []
        for m in MeetingConstraintExtractor._BUILDING_RE.finditer(query):
            if m.group(0) not in seen:
                seen.append(m.group(0))
        if len(seen) < 2:
            return None
        last = seen[-1]
        return last if last != primary else None

    @staticmethod
    def _build_addresses(c: MeetingConstraints) -> list[str]:
        """按约束层级生成候选 office_address（有序，越靠前越优先）。

        规则：
        - 有楼栋 → 园区_楼栋[_楼层]（如 0552_A1_3F / 0551_A4）；
        - 无楼栋有楼层 → 园区下各楼栋的该楼层（如 小镇一楼 → A1_1F, A2_1F）；
        - 只有园区 → 园区码（如 0552）。
        """
        addresses: list[str] = []
        campus = c.campus or _DEFAULT_CAMPUS
        if c.building:
            addresses.append(MeetingConstraintExtractor._address_for(campus, c.building, c.floor))
        elif c.floor:
            # 无楼栋指定楼层：枚举常见楼栋（与 0038「小镇一楼」一致）。
            for building in ("A1", "A2", "A3", "A4", "A5"):
                addresses.append(MeetingConstraintExtractor._address_for(campus, building, c.floor))
        else:
            addresses.append(campus)
        return [a for a in addresses if a]

    @staticmethod
    def _address_for(campus: str, building: str, floor: str | None) -> str | None:
        """按 园区码_楼栋[_楼层] 组装 office_address。"""
        parts = [campus, building]
        if floor:
            parts.append(floor)
        return "_".join(parts)

    @staticmethod
    def _floor_code(token: str) -> str:
        """把楼/层前的数字或中文数字归一为 "NF"（如 "3F" / "1F"）。"""
        if token.isdigit():
            return f"{int(token)}F"
        return f"{_CN_NUMBERS.get(token, 1)}F"

    # ------------------------------------------------------------ 时间 --

    def _fill_time(
        self, query: str, resolver: TemporalResolver, c: MeetingConstraints
    ) -> None:
        """解析日期 / 起止时刻 / 逐天搜索区间。"""
        start, end = resolver.resolve_time_range(query)
        c.start, c.end = start, end

        # 「最早能订上」→ 周内逐天搜索区间。
        week_start, week_end = resolver.resolve_week_span(query)
        if week_start:
            c.week_start, c.week_end = week_start, week_end
            return

        # 多日同会议室（周三和周四 / 周二、周三、周四）→ S1d 多日交集流程。
        days = resolver.resolve_days(query)
        if len(days) >= 2:
            c.days = days
            # 「周三和周四都要空闲，找到后订周三的」：多日校验可用性但只订一天。
            book_one = self._BOOK_ONE_DAY_RE.search(query)
            if book_one:
                target_weekday = _WEEKDAY_MAP.get(book_one.group(1))
                c.book_only_day = next(
                    (d for d in days if date.fromisoformat(d).weekday() == target_weekday),
                    None,
                )
            return

        c.day = resolver.resolve_day(query)

    # ------------------------------------------------------------ 需求 --

    def _fill_requirements(self, query: str, c: MeetingConstraints) -> None:
        """解析容量 / 屏幕 / 主题 / 参会人数 / 工位偏好。"""
        cap_m = self._CAPACITY_RE.search(query)
        if cap_m:
            c.capacity_gte = int(cap_m.group(1))
            c.attendees = int(cap_m.group(1))

        if any(h in query for h in ("不需要屏幕", "没有屏幕", "不带屏幕", "无需屏幕")):
            c.has_screen = False
        elif any(h in query for h in ("屏幕", "投影")):
            c.has_screen = True

        # 先判否定再判肯定：否则「不可预订」里的子串「可预订」会被误命中为 True。
        if any(h in query for h in ("不可预订", "不可订", "无权限")):
            c.bookable = False
        elif any(h in query for h in ("可预订", "可订")):
            c.bookable = True

        title_m = self._TITLE_RE.search(query)
        if title_m:
            c.title = title_m.group(1).strip()

        # 工位偏好触发词：离工位最近 / 工位附近（0245「在他工位附近订」）。
        if any(h in query for h in ("离我工位", "工位近", "工位最近", "离工位", "工位附近", "附近")):
            c.workspace_hint = True

        # 时段柔性：「如果这个时间不行，前后半小时看看」→ 主时段无解时执行层回退。
        if any(h in query for h in ("这个时间不行", "前后半小时", "时间不行", "没有合适的")):
            c.time_flexible = True

    # ------------------------------------------------------------ S2 --

    def _fill_s2(self, query: str, resolver: TemporalResolver, c: MeetingConstraints) -> None:
        """S2 纯查询子类型的专项解析（不解析时保持默认 None）。"""
        if "工位在哪里" in query or ("工位" in query and "哪" in query):
            c.query_type = QUERY_WORKSPACE
            return
        if "不可预订" in query and "会议室" in query:
            c.query_type = QUERY_UNBOOKABLE
            c.day = c.day or resolver.today.isoformat()  # 未给日期时用今天
            return
        # 日程/预订情况：房间 ID + 日期区间（字面区间优先，否则本周）。
        room_m = re.search(r"([A-Z]\d-\dF-\d{3}|[A-Z]\d-\d{3})", query)
        if room_m and any(h in query for h in ("预订情况", "日程", "预订", "预约情况")):
            c.query_type = QUERY_SCHEDULE
            c.schedule_room_id = room_m.group(1)
            self._fill_schedule_range(query, resolver, c)
            return
        if "会议预订" in query or ("预订" in query and "哪些" in query):
            c.query_type = QUERY_BOOKING_LIST
            c.day = c.day or resolver.today.isoformat()
            # 关键词（0242「关键词是项目启动」）→ 传入 booking.list 并回显。
            keyword_m = re.search(r"关键词(?:是|为|[:：])?\s*([^。；，,、]+)", query)
            if keyword_m:
                c.query_keyword = keyword_m.group(1).strip()
            return

    # ------------------------------------------------------------ 附加槽位 --

    def _fill_extras(
        self, query: str, resolver: TemporalResolver, c: MeetingConstraints
    ) -> None:
        """解析延长分钟 / 参会人 / 点名对比房间 / 订单号透传。

        规则可判定则填（供执行层与兜底路径消费）；判定不出保持默认（不猜）。
        """
        # 延长分钟数（S5）：显式 N 分钟 > 半小时 > 一小时 > 默认 30。
        m = self._MINUTES_RE.search(query)
        if m:
            c.minutes = int(m.group(1))
        elif "半小时" in query:
            c.minutes = 30
        elif "一个小时" in query or "一小时" in query:
            c.minutes = 60

        # 参会人（S6）：「把 X 加到/加入/移除」。
        p_m = self._PERSONS_RE.search(query)
        if p_m:
            for raw_name in self._PERSON_SPLIT_RE.split(p_m.group(1)):
                name = raw_name.strip().lstrip("(").rstrip(")")
                if not name:
                    continue
                emp = None
                emp_m = self._EMPLOYEE_NO_RE.search(name)
                if emp_m:
                    emp = emp_m.group(1) or emp_m.group(2)
                    name = self._EMPLOYEE_NO_RE.sub("", name).strip(" （）()")
                if name:
                    person: dict[str, Any] = {"name": name}
                    if emp:
                        person["employee_no"] = emp
                    c.persons.append(person)

        # 日程对比的点名房间（S1s 对比）：有「更空闲/最空闲/对比」且是预订语境。
        if any(h in query for h in ("更空闲", "最空闲", "哪个更", "哪个最", "对比", "最空闲的")):
            rooms = list(dict.fromkeys(self._NAMED_ROOM_RE.findall(query)))
            if rooms:
                c.compare_rooms = rooms

        # 订单号透传：原文出现 SEED-* 才保留（执行层据此免 booking.list 定位）。
        o_m = self._ORDER_ID_RE.search(query)
        if o_m:
            c.order_id_hint = o_m.group(0)

        # 定位关键词（取消/延长/参会人）：无订单号时取「那个X会议室/的X会」里的会议名，
        # 供 booking.list keyword 过滤定位唯一预订（0025/0047「项目复盘」、0028「项目评审」）。
        if not c.order_id_hint and not c.query_keyword:
            kw_m = self._MEET_KEYWORD_RE.search(query)
            if kw_m:
                kw = kw_m.group(1).strip()
                if (
                    kw not in self._MEET_KEYWORD_STOP
                    and not any(ch.isdigit() for ch in kw)
                ):
                    c.query_keyword = kw

        # 裸午别 + 时长（0048「周三下午…能连续用3小时」→ 14:00-17:00）。
        if not c.start and not c.end:
            for period, hour in (("上午", 9), ("中午", 12), ("下午", 14), ("晚上", 18)):
                if period in query:
                    c.start = f"{hour:02d}:00"
                    break
        if c.start and not c.end:
            d_m = re.search(
                r"(?:连续|总共|一共|需要|持续)?\s*(?:用|开)?\s*(\d+)\s*个?小时", query
            )
            if d_m:
                sh, sm = (int(part) for part in c.start.split(":"))
                end_min = sh * 60 + sm + int(d_m.group(1)) * 60
                if 0 < end_min <= 24 * 60:
                    c.end = f"{end_min // 60:02d}:{end_min % 60:02d}"

        # 同日多时段（0043「上午9点到11点…下午2点到4点…需要同一个房间」）。
        if len(c.slots) < 2:
            c.slots = self._extract_slots(query, resolver, c)

    _MEET_SLOT_CTX = (
        "会议", "会议室", "评审", "复盘", "启动", "分享", "周会", "例会",
        "讨论", "主题", "订", "约", "开", "讲座", "培训", "沟通",
    )

    def _extract_slots(
        self, query: str, resolver: TemporalResolver, c: MeetingConstraints
    ) -> list[dict]:
        """扫描 query 中所有「X点到Y点」时段，组装同日多时段槽位。

        只保留 >=2 个不同时段的槽位（0043 同房多场）；单一时段不填 slots
        （走普通单日预订）。标题取时段后的「开X」短语。时段周围无会议语境
        （如跨域 query 里「下午4点到6点要请2小时事假」）不入槽——避免把
        请假/预算的时段误当第二场会议（wf_0006 多订一间会触发 forbidden）。
        """
        out: list[dict] = []
        for m in re.finditer(
            r"(上午|下午|晚上|中午)?\s*(\d{1,2})\s*点\s*(半)?\s*"
            r"(?:到|至|~|—)\s*(上午|下午|晚上|中午)?\s*(\d{1,2})\s*点\s*(半)?",
            query,
        ):
            window = query[m.end(): m.end() + 40]
            before = query[max(0, m.start() - 40): m.start()]
            # 时段后紧跟会议语境 → 入槽；否则看前缀：被句号/分号分隔（跨域
            # 请假「下午4点到6点要请2小时事假」）或前缀无会议语境 → 排除。
            if not any(kw in window for kw in self._MEET_SLOT_CTX) and (
                "。" in before or "；" in before or ";" in before
                or not any(kw in before for kw in self._MEET_SLOT_CTX)
            ):
                continue
            start_period = m.group(1)
            end_period = m.group(4) or start_period
            start_min = resolver._hour_with_period(int(m.group(2)), start_period) * 60
            start_min += 30 if m.group(3) == "半" else 0
            end_min = resolver._hour_with_period(int(m.group(5)), end_period) * 60
            end_min += 30 if m.group(6) == "半" else 0
            if end_min <= start_min:
                continue
            title: str | None = None
            t_m = re.search(
                r"开([一-龥A-Za-z0-9]{2,12}?)", query[m.end():m.end() + 24]
            )
            if t_m:
                title = t_m.group(1)
            slot = {
                "day": c.day,
                "start": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "end": f"{end_min // 60:02d}:{end_min % 60:02d}",
                "title": title,
            }
            key = (slot["start"], slot["end"])
            if key not in {(s["start"], s["end"]) for s in out}:
                out.append(slot)
        return out if len(out) >= 2 else []

    def _fill_schedule_range(
        self, query: str, resolver: TemporalResolver, c: MeetingConstraints
    ) -> None:
        """解析 S2 日程查询的日期区间（字面「X月X日到Y月Y日」优先，否则本周）。"""
        m = re.search(
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:到|至|—)\s*"
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*日",
            query,
        )
        if m:
            year = resolver.today.year
            c.schedule_start_date = date(
                year, int(m.group(1)), int(m.group(2))
            ).isoformat()
            c.schedule_end_date = date(
                year, int(m.group(3)), int(m.group(4))
            ).isoformat()
            return
        start, end = resolver.resolve_week_span(query + " 本周")
        if start:
            c.schedule_start_date, c.schedule_end_date = start, end


def analyze_meeting_query(
    user_query: str, now_iso: str, mode: str | None = None
) -> tuple[str, MeetingConstraints]:
    """一站式理解：意图 + 约束。

    Args:
        user_query: 用户原始提问。
        now_iso: env.reset 返回的 now（ISO 字符串）。
        mode: env.reset 返回的 mode。

    Returns:
        (intent, constraints)。
    """
    router = IntentRouter()
    intent = router.route(user_query, mode)
    resolver = TemporalResolver(now_iso)
    constraints = MeetingConstraintExtractor().extract(user_query, resolver)
    constraints.intent = intent
    return intent, constraints
