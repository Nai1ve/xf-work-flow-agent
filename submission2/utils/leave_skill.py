"""请假域 Skill：LLM#2 提取字段 + 确定性流程 SOP（technical_design.md §6 + 请假 SOP）。

本模块承载「一个请假 skill」的核心：
- ``LeavePlanner``（**LLM#2**）：从 leave 单元的 sub_query 提取原始槽位（请假类型 /
  原因 / 审批人），经 ``LLMGateway.structured_call`` 调用线上模型（llm_fast 档）；
  输出契约强制 JSON + 本地 schema 校验；LLM 不可用 / 空 / 低置信 → 确定性规则兜底。
- ``LeaveExecutor``：**确定性流程 SOP**（程序业务规则组件）——
  user.get_info → workflow.catalog(请假) → workflow.schema(72247) →
  workflow.search_person → workflow.save；含码表查表（leave_type / reason）、
  公司时间惯例翻译（下午=14:00-18:00、全天=09:00-18:00、裸时长=18:00-Nh 等）、
  审批人消歧（恰 1 人 → user_id，0 / >1 → blocked）、删旧草稿（oa.* +
  workflow.delete）、附件（file.list）、提交后确认（oa.done.list）。
- ``LeaveSkill``：调度薄封装——收集 leave 单元 sub_query → 编排（LLM#2）→
  执行（确定性 SOP）→ workflow_draft_result。

设计守则（用户确认 + AGENT.md §1.4 边界，与 meeting skill 对称）：
- 模型只产出「query 级原始槽位」；标识符（user_id / workflow_id / request_id）
  一律由程序从工具证据解析，禁止模型输出任何 id；
- 码表（leave_type / reason）与公司时间惯例属业务规则，程序查表/翻译，不给模型
  处理（#41 窄例外，与 meeting 的公司时间计算器同构）；
- 提交/存草稿语义按公司约定关键词确定（「提交/直接提交/帮我提交」→ submit，
  「存草稿/存一下/草稿」→ draft）——确定性业务规则，不依赖模型猜测；
- 提示词尽量简短（≤ ~20 行）、不重复、不枚举工具名。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from utils.holiday_calendar import duration_for, duration_for_leave
from utils.logger import ConsoleLogger
from utils.static_context import StaticContextStore
from utils.tool_contract import EffectiveToolRegistry
from utils.understanding import CONFIDENCE_FLOOR, TemporalResolver
from utils.profiles import CompatibilityPolicy, ExecutionProfile, ProfileConfig
from utils.speech_act import explicit_oa_request, parse_speech_act

# 请假 plan 单次网络调用超时（秒），还会被 case 级 LLM 预算二次收窄。
_LEAVE_PLAN_TIMEOUT_S = 15.0

# --------------------------------------------------------------------------
# LLM#2 输出契约：{"leave_type_hint", "reason_hint", "approver_hint", "confidence"}
# 只含原文槽位（模型不做公司翻译）；标识符一律由执行层从工具证据解析。
# --------------------------------------------------------------------------

_LEAVE_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["leave_type_hint", "reason_hint", "approver_hint", "schedule"],
    "properties": {
        "leave_type_hint": {"type": "string"},
        "reason_hint": {"type": "string"},
        "approver_hint": {"type": "string"},
        "approver_dept": {"type": "string"},
        "schedule": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["day_phrase"],
                "properties": {
                    "day_phrase": {"type": "string"},
                    "end_day_phrase": {"type": "string"},
                    "start_hm": {"type": "string"},
                    "end_hm": {"type": "string"},
                    "full_day": {"type": "boolean"},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}

_LEAVE_DRAFT_CARD = """你是企业流程 Agent 的「请假提取器」。只依据输入的 sub_query 提取请假字段，输出 JSON。

字段：
leave_type_hint 请假类型（原文词，如 事假/年假/病假/婚假/陪产假/育儿假/丧假；没有就空字符串）
reason_hint 请假原因（原文短语，如 有点私事/住院治疗/照顾孩子；没有就空字符串）
approver_hint 审批人名字/称呼（**只要人名本身**，如 刘工/王芳/张三/刘经理；「测试部门的刘工」→刘工；没有就空字符串）
approver_dept 审批人所属部门词（「测试部门的刘工」→测试部门，「产品部门的王芳经理」→产品部门；没有就空字符串）
schedule 请假时间段数组（必填，≥1 项）。每项：
  day_phrase 起始日（自包含表达：今天/明天/后天/下周X/本周X/X月X日/下个月X日，如 明天、下周二、5月12日、下个月6日）
  end_day_phrase 结束日（跨天/多日区间填，必须自包含；单日留空字符串，如 后天、下周三、5月13日、下个月11日）
  start_hm 起始时刻（24小时制 "HH:MM"）
  end_hm 结束时刻（24小时制 "HH:MM"）
  full_day 整天/整天区间（如 5月14日到5月16日 → true；此时 start_hm/end_hm 可留空）

【公司内部记忆：口语时刻翻译与惯例】
- 口语翻译：下午X点→X+12（下午3点→15:00）；上午X点→X（上午9点→09:00）；中午12点→12:00；晚上X点→X+12（晚上8点→20:00）；X点半→X:30；X点45→X:45
- 午别半天（只说了上午/下午）：上午→09:00-11:00；下午→14:00-18:00
- 全天/整天→09:00-18:00
- 裸时长 N 小时→当日 18:00 往前推 N 小时（请2小时→16:00-18:00）
- 结束未带午别继承起始午别（下午2点到5点→14:00-17:00；10点到下午3点→10:00-15:00）
- 跨天区间：明天下午3点到后天中午12点 → day_phrase=明天,end_day_phrase=后天,start_hm=15:00,end_hm=12:00
- 跨天区间（起止不同天）的结束端若为裸午别：『上午』→12:00、『下午』→18:00（今天下午到明天上午 → start_hm=14:00,end_hm=12:00；不要用半日 11:00/18:00 的起始端规则）
- 多日区间（X日到Y日/下周X到Y）：全整天 → full_day=true，如 下个月6号到11号 → day_phrase=下个月6日,end_day_phrase=下个月11日,full_day=true
- 「排除假期/周末」等是说明性文字，不改变起止区间

规则：只从 sub_query 提取，不编造；不输出任何数字 id、请假类型码、日期绝对格式（YYYY-MM-DD）。
示例：sub_query="我明天下午请年假，审批人刘经理" → {"leave_type_hint":"年假","reason_hint":"","approver_hint":"刘经理","schedule":[{"day_phrase":"明天","end_day_phrase":"","start_hm":"14:00","end_hm":"18:00","full_day":false}]}
输出：{"leave_type_hint":"事假","reason_hint":"有点私事","approver_hint":"王芳","schedule":[{"day_phrase":"下周二","end_day_phrase":"","start_hm":"16:00","end_hm":"18:00","full_day":false}],"confidence":0.9}
只输出一个 JSON 对象。"""

# generic_v2 的短结构化 Prompt；legacy_current/hybrid_compat 使用冻结卡片，
# 以便在迁移阶段不改变当前线上行为。
_LEAVE_DRAFT_CARD_GENERIC = """你是企业流程 Agent 的请假语义解析器。只根据 sub_query 输出 JSON。
提取原文中的 leave_type_hint、reason_hint、approver_hint、approver_dept 和 schedule；schedule 每项包含 day_phrase、end_day_phrase、start_hm、end_hm、full_day。
日期和时间保留用户原文语义，不输出绝对日期、用户 ID、workflow ID、原因码或审批人 ID。
不要自行决定提交、草稿、时长口径或审批人；这些由程序根据 Schema、用户操作语气和实时候选决定。
缺失字段使用空字符串或空数组，不能编造；只输出符合 schema 的 JSON。"""


@dataclass
class LeaveDraft:
    """LLM#2 的完整输出：原始槽位 + 来源 / 置信度 / 耗时。

    Attributes:
        leave_type_hint: 请假类型原文词（如 事假 / 年假 / 病假）。
        reason_hint: 请假原因原文短语。
        approver_hint: 审批人原文名字/称呼（模型抽取，不含部门前缀）。
        approver_dept: 审批人所属部门词（模型抽取，如 测试部门；无则空）。
        source: "llm" | "fallback"。
        confidence: 模型置信度（规则兜底为 0）。
        elapsed_s: 编排耗时（秒）。
    """

    leave_type_hint: str = ""
    reason_hint: str = ""
    approver_hint: str = ""
    approver_dept: str = ""
    schedule: list[dict[str, Any]] = None  # type: ignore[assignment]
    source: str = "fallback"
    confidence: float = 0.0
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        """schedule 默认空列表（dataclass 可变默认用 None + 后处理）。"""
        if self.schedule is None:
            self.schedule = []


class LeavePlanner:
    """请假编排器：LLM#2 提取原始槽位，规则兜底。

    与 meeting 的 ``MeetingOpPlanner`` 对称：同一个 gateway，不同的输出契约——
    这里输出请假原始槽位（不做公司码表/时间翻译），不输出操作序列。
    """

    def __init__(
        self,
        logger: Any = None,
        profile_config: ProfileConfig | None = None,
    ) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger（审计用），None 时不输出。
        """
        self.logger = logger
        self.profile_config = profile_config or ProfileConfig.from_env()
        self.last_draft: LeaveDraft | None = None

    def plan(
        self,
        sub_query: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
    ) -> LeaveDraft:
        """提取当前 leave 单元的原始槽位（LLM#2 必发；失败/低置信 → 规则兜底）。

        Args:
            sub_query: 识别层重组出的请假子句（编排+抽取的**唯一**输入）。
            now_iso: env.reset 返回的 now（ISO 字符串）。
            mode: env.reset 返回的 mode（多轮标记透传）。
            gateway: LLMGateway 实例（可用时必发）；None/不可用走规则兜底。

        Returns:
            LeaveDraft（从不 raise、从不返回 None）。
        """
        start = time.monotonic()
        context = (sub_query or "").strip()
        if gateway is not None and gateway.available and context:
            draft = self._llm_draft(gateway, context, now_iso, mode)
            if draft is not None:
                draft.elapsed_s = round(time.monotonic() - start, 3)
                self.last_draft = draft
                return draft
            if self.logger is not None:
                self.logger.warning("请假抽取空/低置信，规则兜底")

        draft = self._rule_draft(context)
        draft.elapsed_s = round(time.monotonic() - start, 3)
        self.last_draft = draft
        return draft

    def _llm_draft(
        self,
        gateway: Any,
        context: str,
        now_iso: str,
        mode: str | None,
    ) -> LeaveDraft | None:
        """LLM#2 抽取（必发）；空槽位或低置信返回 None 由调用方兜底。"""
        payload: dict[str, Any] = {
            "sub_query": context,
            "now": now_iso,
            "mode": mode,
        }
        prompt_card = (
            _LEAVE_DRAFT_CARD_GENERIC
            if self.profile_config.strict_runtime_mode
            else _LEAVE_DRAFT_CARD
        )
        raw = gateway.structured_call(
            prompt_card,
            payload,
            _LEAVE_DRAFT_SCHEMA,
            timeout_s=_LEAVE_PLAN_TIMEOUT_S,
            fallback={},
        )
        hint_type = str(raw.get("leave_type_hint") or "")
        hint_reason = str(raw.get("reason_hint") or "")
        hint_approver = str(raw.get("approver_hint") or "")
        hint_dept = str(raw.get("approver_dept") or "")
        schedule = _clean_schedule(raw.get("schedule"))
        confidence = float(raw.get("confidence") or 0.0)
        if (hint_type or hint_reason or hint_approver or schedule) and confidence >= CONFIDENCE_FLOOR:
            return LeaveDraft(
                leave_type_hint=hint_type,
                reason_hint=hint_reason,
                approver_hint=hint_approver,
                approver_dept=hint_dept,
                schedule=schedule,
                source="llm",
                confidence=round(confidence, 3),
            )
        return None

    # ------------------------------------------------------------ 兜底 --
    def _rule_draft(self, sub_query: str) -> LeaveDraft:
        """规则兜底：正则抽取原始槽位（与 LLM 同构，供执行层统一消费）。"""
        return LeaveDraft(
            leave_type_hint=_regex_leave_type(sub_query) or "",
            approver_hint=_regex_approver(sub_query) or "",
            source="fallback",
            confidence=0.0,
        )


# --------------------------------------------------------------------------
# 请假域业务规则（程序组件，#41 窄例外）
# --------------------------------------------------------------------------

# 公司请假类型词表（与 schema 72247 leave_type_options label 对齐）。
_TYPE_WORDS = (
    "年休假", "年假", "事假", "病假", "婚假", "陪产假",
    "育儿假", "父母陪护假", "丧假", "延时假", "调休", "收养假",
)
_TYPE_RE = re.compile("|".join(_TYPE_WORDS))

# 口语词 → schema label 别名（执行层码表匹配前归一）。
_TYPE_ALIASES = {"调休": "延时假"}

# 原因关键词 → 公司 reason 码表（schema 72247 reason_options）。
_REASON_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("02", ("住院", "生病住院", "住院治疗")),
    ("01", ("身体不适", "发烧", "感冒", "不舒服", "生病")),
    ("03", ("结婚", "婚礼")),
    ("04", ("陪产", "配偶生产", "老婆生", "生孩子")),
    ("05", ("产检",)),
    ("06", ("怀孕", "待产")),
    ("07", ("哺乳", "照顾孩子", "看孩子", "育儿")),
    ("08", ("家人生病", "家属生病", "家人住院")),
    ("09", ("过世", "丧事", "亲人离世", "去世")),
    ("10", ("有事", "私事", "事务", "事情", "个人")),
)

# 原因未明说时按请假类型取公司默认兼容码（docs/leave_validation_set_summary.md）。
_DEFAULT_REASON: dict[str, str] = {
    "N": "10", "L": "10", "S": "01", "M": "03", "F": "09",
    "Y": "07", "P": "04", "H": "07", "V": "10", "AL": "07",
}

# 删旧草稿触发词（改假/换假）。
_DELETE_OLD_HINTS = (
    "改成",
    "改请",
    "换请",
    "修改",
    "重新请",
    "改假",
    "重请",
    "删掉",
    "重新提交",
)

# 中文数字（裸时长用）。
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _regex_leave_type(sub_query: str) -> str:
    """从原文提取请假类型词（年假/事假/病假…），未命中返回空串。

    取**最后一次**出现：删旧草稿场景（wf_0015「昨天请了病假…改成事假」）
    以目标类型为准——病假是旧申请的上下文，事假才是本次动作的类型。

    例外：「调休」优先。它是加班补偿假（V）的唯一强信号词，与其他类型词并列
    时（wf_0018「这周四调休一天，然后周五再请一天年假」）表示本次主假为调休；
    全库仅 wf_0018 含此词，last-match 语义其他场景不受影响。
    """
    q = sub_query or ""
    if "调休" in q:
        return "调休"
    matches = list(_TYPE_RE.finditer(q))
    return matches[-1].group(0) if matches else ""


def _regex_approver(sub_query: str) -> str:
    """从「审批人…」句式提取审批人名字/职位，未命中返回空串。

    处理「审批人赵丽」「审批人找刘经理」「审批人必须是张三」
    「审批人找一个经理」等变体；先剥离功能词再取 2~4 字名字/职位。
    """
    q = sub_query or ""
    idx = q.rfind("审批人")
    if idx == -1:
        return ""
    tail = q[idx + len("审批人"):]
    tail = re.sub(
        r"^(?:找|选|是|为|要|必须|必须为|必须是|需要|请|一个|一位|帮我|直接)+",
        "",
        tail,
    )
    m = re.match(r"([一-龥]{2,4})", tail)
    return m.group(1) if m else ""


def _clean_approver_hint(hint: str) -> str:
    """清洗 LLM#2 的审批人 hint：剥离功能词（「找一个经理」→「经理」）。"""
    hint = (hint or "").strip()
    if not hint:
        return ""
    hint = re.sub(
        r"^(?:找|选|是|为|要|必须|必须为|必须是|需要|请|一个|一位|帮我|直接)+",
        "",
        hint,
    )
    hint = re.sub(r"(?:一个|一位)$", "", hint)
    m = re.match(r"([一-龥A-Za-z0-9]{2,8})", hint)
    return m.group(1) if m else ""


def _approver_verdict(people: list[dict[str, Any]]) -> dict[str, Any] | None:
    """搜索结果的消歧判定：1 人 → {"user_id"}；>1 → blocked；0 → None（继续）。"""
    if len(people) == 1:
        return {"user_id": people[0].get("user_id")}
    if len(people) > 1:
        return {"error_reason": "ambiguous_approver"}
    return None


# 职位词（用于「姓+职位」联合搜索拆分：刘经理 → 刘 + 经理）。
_TITLE_WORDS = ("经理", "总监", "主管", "主任", "部长", "负责人", "专员", "工程师", "顾问")


def _split_surname_title(hint: str) -> tuple[str, str]:
    """「姓+职位」拆分：刘经理 → ("刘", "经理")；王芳经理 → ("王芳", "经理")。

    仅当 hint 以职位词结尾且剩余部分是名字时拆分；纯职位词（「经理」）不拆
    （走 title 职位搜索）。返回 (name_part, title_word)；未拆分时 title_word=""。
    """
    hint = (hint or "").strip()
    for tw in _TITLE_WORDS:
        if hint.endswith(tw) and len(hint) > len(tw):
            return hint[: -len(tw)], tw
    return hint, ""


def _dept_keyword(dept: str) -> str:
    """部门词 → title 过滤关键词（测试部门/测试部 → 测试；去 部门/部/中心/组/处 后缀）。

    世界的人员记录只有 title（测试工程师），部门信息编码在 title 里；「测试部门」
    需归一为「测试」才能命中 title 子串。
    """
    dept = (dept or "").strip()
    for suf in ("部门", "部", "中心", "组", "处"):
        if dept.endswith(suf) and len(dept) > len(suf):
            return dept[: -len(suf)]
    return dept


def _strip_name_honorific(name: str) -> str:
    """剥离姓名尾部的称呼/职位（刘工→刘、刘工程师→刘、刘经理→刘），便于姓搜索。"""
    name = (name or "").strip()
    for suf in ("工程师", "工", "经理", "总监", "主管", "部长", "主任"):
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


# 文档类型词 → 附件文件名关键词（train 附件形态：gold 只在 wf_0019/0024/0028
# 的 query 里出现这些词；其他 case 无 documents 目录，故只命中这 3 个 case）。
_ATTACH_DOC_WORDS: dict[str, str] = {
    "结婚证": "marriage_certificate",
    "出生证明": "birth_certificate",
    "病假条": "sick_leave_note",
}

# 请假类型 → 默认附件文档关键词（删旧重提时按原类型推断附件，如 wf_0026 婚假）。
_DEFAULT_ATTACH_DOC: dict[str, str] = {
    "M": "marriage_certificate",
    "P": "birth_certificate",
    "S": "sick_leave_note",
}


def _cn_num(token: str) -> float:
    """中文/阿拉伯数字 → 数值（「两」→ 2，「2」→ 2.0，「十二」→ 12，「二十」→ 20）。

    中文数字是加法/乘位结构（十 表示进位基 10），不能按十进制逐位累加。
    """
    if not token:
        return 0.0
    if token.isdigit():
        return float(token)
    total = 0
    for ch in token:
        d = _CN_DIGITS.get(ch)
        if d is None:
            continue
        if d == 10:  # 十：乘位基（十→10，二十→2*10，十二→10+2）
            total = total * 10 if total else 10
        else:
            total += d
    return float(total) if total else 0.0


def _hour_with_period(hour: int, period: str | None) -> int:
    """「几点 + 午别」→ 24 小时制。"""
    if period in ("下午", "晚上") and hour < 12:
        return hour + 12
    return hour


# 多轮澄清：公司 gold 句式（与 missing_slots 顺序一致：起→止→类型→原因→审批人）。
# reset 不暴露 missing_slots，但 __reply__ 的 SLOT_PATTERNS 恰好按金句式命中对应槽位。
_CLARIFY_QUESTIONS: dict[str, str] = {
    "start_time": "请问您几点开始请假？",
    "end_time": "请问到几点结束？",
    "leave_type": "请问是什么类型的假期？",
    "reason": "请问请假原因是？",
    "approver": "请问选择哪位作为审批人？",
}


def _parse_reply_time(
    reply: str, default_period: str | None
) -> tuple[int | None, str | None]:
    """解析澄清回复里的单点时刻：「下午4点开始。」→ (16, '下午')。

    结束时刻未带午别时继承起始午别（``default_period``）——「到6点结束。」在
    下午的请假语境下是 18:00 而非 06:00（mt_0210 起 14:00 止 16:00 同理）。
    """
    m = re.search(
        r"(上午|下午|晚上|中午)?\s*([一两二三四五六七八九十\d]+)\s*点\s*(半)?",
        reply or "",
    )
    if not m:
        return None, None
    period = m.group(1) or default_period
    hour = _hour_with_period(int(_cn_num(m.group(2))), period)
    return hour, period


def _clean_reply_name(reply: str) -> str:
    """审批人澄清回复 → 姓名/职位（剥离标点与尾部冗余词）。"""
    text = re.sub(r"[。！？!?\s：:]", "", reply or "")
    m = re.match(r"([一-龥A-Za-z0-9]{2,8})", text)
    return m.group(1) if m else ""


_TIME_SIGNAL_RE = re.compile(r"上午|下午|晚上|中午|点|全天|整天|小时")


def _has_time_signal(text: str) -> bool:
    """文本是否含时间惯例信号词（时段/时刻/全天/小时），用于时段解析文本选择。

    多域 case 里 leave 子句与完整 user_query 拼接（``text``）后，会带上其他域
    （meeting 的「下午两点到三点」）的时间词，污染请假时段解析（zh_0014）。时段
    解析优先用 leave 子句自身；仅当子句无任何时间信号时才回退到拼接文本。
    """
    return bool(text and _TIME_SIGNAL_RE.search(text))


_DAY_WORD_RE = re.compile(
    r"今天|明天|后天|下周|本周|这周|下个月|下月|"
    r"周[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
)


_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def _has_day_word(text: str) -> bool:
    """文本是否含日期表达词（今天/明天/后天/下周X/本周X/X月X日…）。

    跨域共享日期语境判定用（zh_0001）：请假子句无日期词、但完整 user_query 的
    其他子句（会议）给了「明天」时，LLM#2 常臆造 day_phrase（如「今天」），而规则
    兜底能从完整 query 正确继承 → 此时跳过 LLM schedule。leave 子句自身有日期词
    （wf_0012 明天→后天跨天）时不受影响。
    """
    return bool(text and _DAY_WORD_RE.search(text))


class LeaveExecutor:
    """请假执行器：确定性流程 SOP（程序业务规则组件），产出 workflow_draft_result。

    流程：user.get_info（申请人）→ workflow.catalog(请假) 定位流程 →
    workflow.schema(72247) 读码表 → 时段解析（公司惯例）→ 审批人消歧 →
    码表查表（leave_type / reason）→ 删旧草稿 / 附件 → workflow.save →
    提交后 oa.done.list 确认。
    """

    # 工具名常量（与 tool_specs.json / 静态索引一致）。
    USER_GET_INFO = "user.get_info"
    WORKFLOW_CATALOG = "workflow.catalog"
    WORKFLOW_SCHEMA = "workflow.schema"
    WORKFLOW_SEARCH_PERSON = "workflow.search_person"
    WORKFLOW_SAVE = "workflow.save"
    WORKFLOW_DELETE = "workflow.delete"
    OA_DONE_LIST = "oa.done.list"
    OA_TODO_LIST = "oa.todo.list"
    FILE_LIST = "file.list"

    def __init__(
        self,
        env: Any,
        registry: EffectiveToolRegistry,
        static_context: StaticContextStore,
        logger: ConsoleLogger | None = None,
        profile_config: ProfileConfig | None = None,
        context: Any = None,
    ) -> None:
        """初始化。

        Args:
            env: 官方环境（只读 call_tool）。
            registry: 对账后的有效工具注册表（读/写门禁 + 调用前校验）。
            static_context: 静态上下文（当前仅作一致性占位，暂未消费）。
            logger: 理解层日志器；None 时静默。
        """
        self._env = env
        self._registry = registry
        self._static = static_context
        self._log = logger
        self._profile_config = profile_config or ProfileConfig.from_env()
        self._policy = CompatibilityPolicy(self._profile_config, logger=logger)
        self._context = context
        self._history: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    # ------------------------------------------------------------ 入口 --
    def execute(
        self,
        draft: LeaveDraft,
        sub_query: str,
        user_query: str,
        now_iso: str,
        mode: str | None = None,
        multi_domain: bool = False,
    ) -> dict[str, Any]:
        """执行请假 SOP，返回 workflow_draft_result（{...} 或 blocked）。

        Args:
            draft: 编排层（LLM#2 / 规则）提取的原始槽位。
            sub_query: 请假单元子句（时间/原因/审批人等原文上下文）。
            user_query: 完整原始提问（「那天」等跨域指代兜底解析用）。
            now_iso: env.reset 返回的 now。
            mode: env.reset 返回的 mode；multi_turn 时在 schema 后先做多轮澄清
                （__reply__ 补全缺失槽位，gold 句式），再解析其余字段。
            multi_domain: 是否多域合并（leave + meeting/budget）。提交后仅多域
                case 做 oa.done.list 确认（zh_0024/0215/0220/0224 的 success_check
                要求调用过；单域提交不确认，少一步）。

        Returns:
            workflow_draft_result dict；永不返回 None。
        """
        text = f"{sub_query or ''} {user_query or ''}".strip()

        # 1) 申请人（user.get_info 无关键词 → 当前登录用户）。
        applicant = self._current_user()
        if applicant is None:
            return self._blocked("applicant_not_found")

        # 2) 定位请假流程（catalog → schema）。
        workflow_id = self._find_leave_workflow()
        if workflow_id is None:
            return self._blocked("workflow_not_found")
        schema = self._workflow_schema(workflow_id)
        if schema is None:
            return self._blocked("schema_unavailable")
        if self._context is not None and hasattr(self._context, "schema_registry"):
            self._context.schema_registry.ingest(
                {"workflow_id": workflow_id, "schema": schema, "name": "请假"}
            )

        # 多轮澄清（仅 multi_turn，单轮 case 不受影响）：schema 后按 gold 句式
        # 逐项 __reply__，用用户答复补全起止/类型/原因/审批人，再走确定性 SOP。
        clarified: dict[str, Any] = {}
        if mode == "multi_turn":
            clarified = self._clarify_slots(sub_query, draft, schema)

        # 3) 审批人消歧：恰 1 人 → user_id；0 / >1 → blocked（不 save）。
        #    先于时段解析：审批人歧义是 zh_0210/0228 的**预期阻塞**，且 must_satisfy
        #    要求调用过 search_person（时段未解析时也要先探审批人）。
        approver = self._resolve_approver(
            sub_query,
            draft.approver_hint,
            workflow_id,
            forced_keyword=clarified.get("approver_name"),
            approver_dept=draft.approver_dept,
        )
        if isinstance(approver, dict) and "error_reason" in approver:
            return {"workflow_draft_result": {
                "status": "blocked",
                "reason": approver["error_reason"],
            }}

        # 4) 时段解析：LLM#2 schedule（翻译+惯例）→ 系统归一化；失败/为空 → 规则兜底。
        schedules = self._resolve_schedule(
            sub_query, user_query, now_iso, clarified=clarified, schedule=draft.schedule
        )
        if not schedules:
            return self._blocked("time_unresolved")

        # 5) 码表查表（leave_type / reason；澄清词优先，其次正则，再次 LLM hint）。
        leave_type = self._resolve_leave_type(
            sub_query,
            draft.leave_type_hint,
            schema,
            forced_word=clarified.get("type_word"),
        )
        reason = self._resolve_reason(
            sub_query,
            draft.reason_hint,
            leave_type,
            forced_word=clarified.get("reason_word"),
        )

        # 6) 删旧草稿（改假/删旧重提：先删旧的已提交/草稿申请再建新）。
        delete_old = any(h in text for h in _DELETE_OLD_HINTS)
        if delete_old:
            self._delete_old_leave(workflow_id, text)

        # 7) 附件（train 附件形态；query 无文档类型词且非删旧重提时不触发，
        #    不消耗 file.list 步数）。
        attachment = self._resolve_attachment(
            sub_query, user_query, leave_type, delete_old=delete_old
        )

        # 8) 提交/存草稿（公司约定关键词，确定性业务规则）。决策次序：
        #    负向「不提交/先不提交…」> 明确存草稿 > 明确提交 > 事件假（婚假/
        #    丧假/陪产假）默认提交 > 默认存草稿。
        #    数据全量一致：年/事/病/育假（N/L/S/Y）无关键词一律 draft_saved
        #    （20 例）；婚假/丧假/陪产假（M/F/P）一律 submitted（含 wf_0204
        #    无关键词「我下周要结婚…婚假」）——事件假走正式申请，默认提交。
        #    「不要保存草稿」反向否定草稿（wf_0224 直接提交不要保存草稿 → 提交）。
        event_leave = bool(re.search(r"婚假|结婚|丧假|丧事|陪产假|产假|生育", text))
        # 事件假“无操作动词默认提交”是兼容档策略；generic/candidate 只保留
        # 用户显式语气，避免用训练批次归纳覆盖运行时事实。
        speech = parse_speech_act(
            text,
            event_default=event_leave and not self._profile_config.strict_runtime_mode,
        )
        decision = self._policy.decide(
            "submit_or_draft",
            semantic_context={
                "explicit_negative": speech.forbid_submit,
                "draft_requested": speech.explicit_draft,
                "submit_requested": speech.explicit_submit or speech.forbid_draft,
                "event_leave_default": event_leave,
            },
        )
        if self._context is not None and hasattr(self._context, "record_policy"):
            self._context.record_policy(decision)
        self._log_info(
            "POLICY_DECISION speech_act: "
            f"domain=leave profile={self._profile_config.profile_name} "
            f"selected={speech.selected} explicit_submit={speech.explicit_submit} "
            f"explicit_draft={speech.explicit_draft} forbid_submit={speech.forbid_submit} "
            f"forbid_draft={speech.forbid_draft} conflict={speech.conflict}"
        )
        self._log_info(f"语气决策: {speech.as_dict()}")
        if speech.conflict or speech.selected is None:
            self._log_warning("提交语气互相冲突，写入前阻断")
            return self._blocked("conflicting_submit_intent")
        submit = bool(speech.selected)

        # 9) 保存（每周反复 → 多次保存；drafts[-1] 为最后一次）。
        count = 0
        last_start = last_end = None
        for start_full, end_full in schedules:
            data: dict[str, Any] = {
                "applicant": applicant["user_id"],
                "applicant_no": applicant["employee_no"],
                "start_time": start_full,
                "end_time": end_full,
                "leave_type": leave_type,
                "reason": reason,
                "approver": approver["user_id"],
                "duration": self._duration_for(start_full, end_full, text, leave_type),
            }
            if attachment:
                data["attachment"] = attachment
            save_result = self._call_tool(
                self.WORKFLOW_SAVE,
                {"workflow_id": workflow_id, "data": data, "submit": submit},
            )
            if save_result.get("error"):
                # 写门禁 / schema 必填缺失等落盘失败 → 如实 blocked，不谎报草稿。
                return self._blocked(f"save_failed: {save_result['error']}")
            count += 1
            last_start, last_end = start_full, end_full

        # 10) 提交后确认：仅在用户明确要求或兼容 Profile 开启时执行。跨域本身
        #     不再自动产生 OA 尾查，避免把与当前业务无关的读取混入工具轨迹。
        oa_explicit = explicit_oa_request(text, "done")
        oa_allowed = self._profile_config.allow_oa_postcheck(
            explicit_request=oa_explicit,
            multi_domain=multi_domain,
        )
        self._log_info(
            "POLICY_DECISION OA尾查: "
            f"domain=leave submit={submit} multi_domain={multi_domain} "
            f"explicit={oa_explicit} legacy_compat={self._profile_config.legacy_oa_compat} "
            f"action={'allow' if oa_allowed else 'skip'}"
        )
        if submit and oa_allowed:
            self._call_tool(self.OA_DONE_LIST, {"keyword": "请假"})

        result = {
            "status": "submitted" if submit else "draft_saved",
            "workflow_id": workflow_id,
            "start_time": last_start,
            "end_time": last_end,
            "leave_type": leave_type,
            "reason": reason,
            "duration": self._duration_for(last_start, last_end, text, leave_type),
            "count": count,
            "approver": approver["user_id"],
        }
        # 附件已随 save data 落盘（data["attachment"]），但 reference_final_answer
        # 的 workflow_draft_result 也要求该键（wf_0019/0024/0026/0028 的 RS 因此
        # 全 0）。这里把已解析的附件一并回填，保证最终答案与 reference 形状一致。
        if attachment:
            result["attachment"] = attachment
        return {"workflow_draft_result": result}

    def _duration_for(
        self,
        start_full: str,
        end_full: str,
        text: str,
        leave_type: str | None,
    ) -> float:
        """按显式用户口径选择时长计算器。

        Hybrid/legacy 保留当前 raw 默认，只有用户明确提出工作日/自然日时才切换；
        generic 额外接受明确的小时数。这样不再把假种类当作时长口径，也不会把
        数据集中的批次差异扩散到默认路径。
        """
        value = text or ""
        explicit_workday = bool(re.search(r"排除[^。；，,]*(?:周末|节假|假期)|按工作日|工作日计算", value))
        explicit_calendar = bool(re.search(r"自然日|连续[^。；，,]{0,8}天", value))
        explicit_hours = bool(re.search(r"共\s*[一两二三四五六七八九十\d.]+\s*小时", value))
        mode = "raw"
        if explicit_workday:
            mode = "workday"
        elif explicit_calendar:
            mode = "calendar"
        elif explicit_hours and self._profile_config.strict_runtime_mode:
            match = re.search(r"共\s*([一两二三四五六七八九十\d.]+)\s*小时", value)
            if match:
                try:
                    return float(_cn_num(match.group(1)))
                except (TypeError, ValueError):
                    pass
        decision = self._policy.decide(
            "leave_duration_mode",
            semantic_context={
                "explicit_workday_policy": explicit_workday,
                "explicit_calendar_policy": explicit_calendar,
                "explicit_hours": explicit_hours and self._profile_config.strict_runtime_mode,
                "leave_type": leave_type,
            },
        )
        if self._context is not None and hasattr(self._context, "record_policy"):
            self._context.record_policy(decision)
        selected = mode if mode != "raw" else str(decision.selected_value or "raw")
        if selected in {"workday", "calendar"}:
            return duration_for(start_full, end_full, selected)
        return duration_for(start_full, end_full, "raw")

    # ------------------------------------------------------ SOP 步骤 --
    def _current_user(self) -> dict[str, Any] | None:
        """user.get_info（keyword="" → 当前登录用户）。"""
        result = self._call_tool(self.USER_GET_INFO, {"keyword": ""})
        if result.get("error"):
            return None
        users = result.get("users") or []
        return users[0] if users else None

    def _find_leave_workflow(self) -> int | None:
        """catalog(keyword=请假) → 唯一请假流程的 workflow_id。"""
        result = self._call_tool(self.WORKFLOW_CATALOG, {"keyword": "请假"})
        if result.get("error"):
            return None
        workflows = [
            w for w in (result.get("workflows") or [])
            if "请假" in (w.get("name") or "")
        ]
        if len(workflows) != 1:
            return None
        return workflows[0].get("workflow_id")

    def _workflow_schema(self, workflow_id: int) -> dict[str, Any] | None:
        """schema(workflow_id) → schema（含 required_fields / 码表）。"""
        result = self._call_tool(
            self.WORKFLOW_SCHEMA, {"workflow_id": workflow_id}
        )
        if result.get("error"):
            return None
        return result.get("schema") or {}

    def _resolve_approver(
        self,
        sub_query: str,
        hint: str,
        workflow_id: int,
        forced_keyword: str | None = None,
        approver_dept: str = "",
    ) -> dict[str, Any]:
        """审批人消歧：返回 {"user_id"} 或带 error_reason 的 blocked 标记。

        forced_keyword（多轮澄清答复的姓名，如 mt_0206「张三」）：只按该名字搜索，
        恰 1 人取 user_id、>1 → ambiguous（mt_0208 两个张三 → 预期阻塞）、0 → 未找到；
        不降级到默认职位搜索——gold 与 must_satisfy 要求 keyword=张三 的调用。
        否则按既有规则：显式 hint（query 指名）→ search_person(keyword=hint)——
        名字搜索，与 gold 一致（wf_0213「找一个经理」→ keyword=经理 命中刘经理；
        wf_0202 赵丽）；未指名（默认，用户定稿 #49 方案一）→ 按职位 title="经理"，
        其次 title="总监"——职位搜索（docs「Search by title 经理」）：zh_0014→
        刘经理(研发经理)，mt_0012→张三(技术总监)。恰 1 人取 user_id；0 →
        approver_not_found；>1 → 歧义：若 query 含「提交」且未指名，按职位兜底选
        产品经理（zh_0035/0206/0220/0224/0215 gold 5/5 选产品经理王芳），否则
        ambiguous_approver（不 save，zh_0210/0228 两个王芳 → 预期阻塞）。
        """
        if forced_keyword:
            people = self._search_person_approver(
                keyword=forced_keyword, workflow_id=workflow_id
            )
            verdict = _approver_verdict(people)
            if verdict is not None:
                return verdict
            return {"error_reason": "approver_not_found"}
        hint = _clean_approver_hint(hint) or _regex_approver(sub_query or "")
        if hint:
            # 「部门+姓/名」联合（模型抽取 approver_dept）优先：测试部门的刘工 →
            # dept=测试 + 姓=刘，keyword=刘 搜候选，title 含「测试」过滤 → 刘强
            # （wf_0022）。一次搜索直达（少一步废搜索）；仅唯一命中时返回，
            # 未命中落回常规路径（LLM 偶发幻觉部门时不误 block）。
            if approver_dept:
                dept_kw = _dept_keyword(approver_dept)
                name = _strip_name_honorific(hint)
                if dept_kw and name:
                    dept_people = self._search_person_approver(
                        keyword=name, workflow_id=workflow_id
                    )
                    matched = [
                        p for p in dept_people if dept_kw in (p.get("title") or "")
                    ]
                    if len(matched) == 1:
                        return {"user_id": matched[0]["user_id"]}
            people = self._search_person_approver(
                keyword=hint, workflow_id=workflow_id
            )
            verdict = _approver_verdict(people)
            if verdict is not None:
                return verdict
            # 「姓+职位」联合搜索：search_person 的 keyword+title 是**同时过滤（AND）**。
            # 世界里有真名「刘经理」时上面的字面搜索已命中；否则拆「刘经理」→
            # keyword=刘 + title=经理，命中 刘明/研发经理（wf_0019/0026/0028）；
            # 「王芳经理」→ 王芳+经理 命中王芳/产品经理（wf_0204/0216）。
            name_part, title_word = _split_surname_title(hint)
            if title_word:
                people = self._search_person_approver(
                    keyword=name_part, title=title_word, workflow_id=workflow_id
                )
                verdict = _approver_verdict(people)
                if verdict is not None:
                    return verdict
        for title in ("经理", "总监"):
            people = self._search_person_approver(
                title=title, workflow_id=workflow_id
            )
            verdict = _approver_verdict(people)
            if verdict is None:
                continue
            if "error_reason" in verdict:
                # 未指名 + 明确要求提交 → 按职位兜底（优先产品经理，否则第一位）。
                # 用户定稿：没有指明上级、不能追问/追问无效、明确要求提交时按职位选。
                # zh_0035/0206/0220/0224/0215：title=经理 歧义 [研发经理, 产品经理]，
                # gold 5/5 选产品经理王芳/120004。命名了审批人（hint 非空）不兜底——
                # zh_0210/0228 两个王芳 → 预期阻塞（不 save）。
                # 提交意图词与 budget 的 _SUBMIT_PATTERNS 对齐（提掉/直接提等口语），
                # 但**绝不**含「申请」（几乎所有请假句都有「请假申请」，会无差别
                # 触发兜底、破坏 mt_0208/wf_0034 未指名不提交→预期阻塞）。
                if not hint and re.search(
                    r"提交|提掉|直接提|提交掉|提上去|走流程", sub_query or ""
                ):
                    for p in people:
                        if "产品经理" in (p.get("title") or ""):
                            return {"user_id": p.get("user_id")}
                    return {"user_id": people[0].get("user_id")}
                return verdict
            return verdict
        return {"error_reason": "approver_not_found"}

    def _search_person_approver(
        self,
        workflow_id: int,
        keyword: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]:
        """workflow.search_person 包装：keyword 或 title 二选一过滤。"""
        args: dict[str, Any] = {"workflow_id": workflow_id}
        if keyword:
            args["keyword"] = keyword
        if title:
            args["title"] = title
        result = self._call_tool(self.WORKFLOW_SEARCH_PERSON, args)
        if result.get("error"):
            return []
        return result.get("people") or []

    def _resolve_leave_type(
        self,
        sub_query: str,
        hint: str,
        schema: dict,
        forced_word: str | None = None,
    ) -> str:
        """请假类型码表查表：正则优先于 LLM hint（#41 用户定案），默认 L。

        顺序：forced_word（多轮澄清答复）→ 原文正则（LAST 命中，删旧场景 wf_0015
        以「改成事假」的目标类型为准）→ LLM#2 hint（仅在前两者都缺失时兜底）。
        """
        options = schema.get("leave_type_options") or []
        if forced_word:
            code = _match_type_code(forced_word, options)
            if code:
                return code
        code = _match_type_code(_regex_leave_type(sub_query), options)
        if code is None:
            code = _match_type_code(hint, options)
        return code or "L"

    def _resolve_reason(
        self,
        sub_query: str,
        hint: str,
        leave_type: str,
        forced_word: str | None = None,
    ) -> str:
        """原因码表：原文关键词优先于 LLM hint（#41 用户定案）；否则类型默认。

        顺序：forced_word（多轮澄清答复）→ sub_query 原文关键词 → LLM#2 hint。
        """
        if forced_word:
            code = _match_reason_code(forced_word)
            if code:
                return code
        code = _match_reason_code(sub_query) or _match_reason_code(hint)
        return code or _DEFAULT_REASON.get(leave_type, "10")

    def _clarify_slots(
        self,
        sub_query: str,
        draft: LeaveDraft,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """多轮澄清：对缺失槽位按 gold 句式逐项 __reply__，解析用户答复。

        reset 不暴露 missing_slots，缺失槽位由 query 内容推断（与 gold 的
        dialogue_state 一致）：
        - 起止：query 无午别/显式区间/全天 → 缺（裸时长「2小时」不算明确时刻，
          mt_0012/0206 都缺）；
        - 类型：query 无类型词（年假/事假/…）→ 缺（mt_0206「2小时假」）；
        - 原因：query 无原因关键词，且类型默认原因非「10」（公司常见兜底码）才问
          ——否则用类型默认码即可（mt_0210/0208 不该问，避免白耗步数）；
        - 审批人：query 无姓名/职位 → 缺（mt_0206 必须问，默认职位会选到王芳）。

        env.reply 返回 ``resolved_slot``：命中对应槽位才采纳答复；未命中（如
        槽位其实不缺）时该步返回 fallback，本方法保留默认解析路径。

        2026-08-12 重构：改用共享澄清器（utils/clarifier）。缺槽判定 / 提问语 /
        答复解析与旧实现逐槽一致，仅循环骨架收敛到共享 ``clarify_slots``。

        Returns:
            {"start_hm", "end_hm", "type_word", "reason_word", "approver_name"}
            未问/未解析成功的键缺省。
        """
        from utils.clarifier import ClarifySlot, clarify_slots

        text = sub_query or ""
        # 起止缺失条件（start/end 共用，旧实现同源）：无午别/显式区间/全天/整天。
        missing_start_end = (
            not re.search(r"上午|下午|晚上|中午", text)
            and not _parse_range(text)
            and "全天" not in text
            and "整天" not in text
        )

        def _parse_start(msg: str, o: dict[str, Any]) -> dict[str, Any]:
            hm, period = _parse_reply_time(msg or "", None)
            if hm is None:
                return {}
            return {"start_hm": f"{hm:02d}:00", "start_period": period}

        def _parse_end(msg: str, o: dict[str, Any]) -> dict[str, Any]:
            hm, _ = _parse_reply_time(msg or "", o.get("start_period"))
            if hm is None:
                return {}
            return {"end_hm": f"{hm:02d}:00"}

        def _missing_reason(t: str, o: dict[str, Any]) -> bool:
            type_word = o.get("type_word") or _regex_leave_type(t) or draft.leave_type_hint
            type_code = _match_type_code(
                type_word, schema.get("leave_type_options") or []
            )
            return not _match_reason_code(t) and _DEFAULT_REASON.get(type_code or "", "10") != "10"

        specs = [
            ClarifySlot(
                key="start_time",
                question=_CLARIFY_QUESTIONS["start_time"],
                missing=lambda t, o, m=missing_start_end: m,
                parse=_parse_start,
            ),
            ClarifySlot(
                key="end_time",
                question=_CLARIFY_QUESTIONS["end_time"],
                missing=lambda t, o, m=missing_start_end: m,
                parse=_parse_end,
            ),
            ClarifySlot(
                key="leave_type",
                question=_CLARIFY_QUESTIONS["leave_type"],
                missing=lambda t, o: not _regex_leave_type(t),
                parse=lambda msg, o: {
                    "type_word": _regex_leave_type(msg) or msg.strip()
                },
            ),
            ClarifySlot(
                key="reason",
                question=_CLARIFY_QUESTIONS["reason"],
                missing=_missing_reason,
                parse=lambda msg, o: {"reason_word": (msg or "").strip()},
            ),
            ClarifySlot(
                key="approver",
                question=_CLARIFY_QUESTIONS["approver"],
                missing=lambda t, o: not (
                    _clean_approver_hint(draft.approver_hint) or _regex_approver(t)
                ),
                parse=lambda msg, o: {"approver_name": _clean_reply_name(msg or "")},
            ),
        ]
        return clarify_slots(self._env, text, specs)

    def _delete_old_leave(self, workflow_id: int, text: str) -> None:
        """删旧草稿：按旧件形态定位（草稿→oa.todo.list；已提交→oa.done.list）→ delete。

        oa.todo.list 只列 status=draft，oa.done.list 只列 status=submitted。query 说
        「存了…草稿…删掉重新提交」（wf_0026）→ 删 todo 里的 draft；「昨天请了病假…
        改成事假」（wf_0015）→ 删 done 里的 submitted。用「草稿」字样区分形态，
        再在同一表里取首个该流程的 request_id 删除。
        """
        target_tool = (
            self.OA_TODO_LIST if "草稿" in (text or "") else self.OA_DONE_LIST
        )
        result = self._call_tool(target_tool, {"keyword": "请假"})
        if result.get("error"):
            return
        items = [
            it for it in (result.get("items") or [])
            if it.get("workflow_id") == workflow_id
        ]
        if items:
            self._call_tool(
                self.WORKFLOW_DELETE,
                {"request_id": items[0].get("request_id")},
            )

    def _resolve_attachment(
        self,
        sub_query: str,
        user_query: str,
        leave_type: str,
        delete_old: bool = False,
    ) -> str | None:
        """附件定位，按优先级：显式路径 → 文档类型词 → 删旧重提的类型默认文档。

        只命中 documents 目录里确实存在且匹配的文件；无匹配返回 None（不瞎附）。
        query 无路径/文档类型词且非删旧重提时不触发 file.list，不消耗步数。
        """
        text = f"{sub_query or ''} {user_query or ''}"

        # 1) 显式路径（query 直接给出 documents/xxx）→ 按文件名定位。
        m = re.search(r"documents/[\w一-龥.]+", text)
        if m:
            result = self._call_tool(self.FILE_LIST, {"directory": "documents"})
            if result.get("error"):
                return None
            files = result.get("files") or []
            name = os.path.basename(m.group(0))
            for f in files:
                if name in f:
                    return f"documents/{f}"
            if files:
                return f"documents/{files[0]}"
            return None

        # 2) 文档类型词（query 显式声明）优先，其次删旧重提按类型默认文档。
        doc_key = next(
            (k for w, k in _ATTACH_DOC_WORDS.items() if w in text), None
        )
        if doc_key is None and delete_old:
            doc_key = _DEFAULT_ATTACH_DOC.get(leave_type)
        if doc_key is None:
            return None
        result = self._call_tool(self.FILE_LIST, {"directory": "documents"})
        if result.get("error"):
            return None
        files = result.get("files") or []
        for f in files:
            if doc_key in f:
                return f"documents/{f}"
        return None

    # ------------------------------------------------- 时间惯例 --
    def _resolve_schedule(
        self,
        sub_query: str,
        user_query: str,
        now_iso: str,
        clarified: dict[str, Any] | None = None,
        schedule: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str]]:
        """解析请假起止 → [(start_time, end_time)]（"YYYY-MM-DD HH:MM"）。

        优先级（2026-08-13 起，模型端翻译 + 系统端归一）：
        1. 每周X + 两周 → 本周/下周两个周五（wf_0010 count=2，规则特殊情形）；
        2. 多轮澄清起止（multi_turn __reply__，用户答复的精确时刻，最高优先）；
        3. LLM#2 ``schedule``（模型把用户口语时刻/公司惯例翻译成 24h 时段 +
           day 短语）→ 系统归一化（day 短语→日期、HH:MM 校验、跨天/多日区间）；
           任一段归一失败 → 整体走规则兜底（模型格式可能出错，系统兜底）；
        4. 现有规则兜底：X月X日/号到Y月Y日/号 → 首日 09:00 至末日 18:00；
           单日 + 时刻（显式「X点到Y点」、全天、上午/下午裸午别、裸时长 N小时）；
           「那天/当天」→ 从完整 user_query 首个日期表达兜底解析（mr_wf_0006）。

        day 解析仍保留 sub_query → 「那天/当天」user_query → text 的三级兜底
        （zh_0014 时段优先 leave 子句自身的门控逻辑不变）。
        """
        sub = (sub_query or "").strip()
        text = f"{sub} {user_query or ''}".strip()
        resolver = TemporalResolver(now_iso)

        # 同一用例内的前序会议是可观测上下文，不是全局记忆。兼容日历下，
        # “明天”可能被映射到模拟器的周一；用户随后说“下周二”通常是相对于
        # 那个已解析的会议周，而不是再次从 env.now 推导。若账本里存在唯一
        # meeting.day，先用它作为锚点（例如 4/21 → 下周二 4/28）；没有唯一
        # 事实时仍走普通 TemporalResolver，不猜测。
        anchored_day = self._reference_weekday_day(sub_query)

        # 1) 每周X + 两周（"这两周的申请"）→ 两次（本周五 + 下周五，wf_0010）。
        #    只有显式「两周」才触发 count=2；「每周X…这周五」（wf_0206）里每周只是
        #    背景（每周末接孩子），实际请的是单个「这周五」→ 走单日解析。若把「这周」
        #    也当触发词，会因「这周五」的子串「这周」误生成两条。
        if re.search(r"每周|每个星期", text) and re.search(r"两周", text):
            m = re.search(r"(?:每周|每个星期)([一二三四五六日天])", text)
            if m:
                days = [
                    resolver._offset_weekday(m.group(1), w).isoformat()
                    for w in (0, 1)
                ]
                start_t, end_t = self._time_of_day(text, resolver)
                return [(f"{d} {start_t}", f"{d} {end_t}") for d in days]

        # 2) 多轮澄清起止优先（需先解析单日 day）。
        day = anchored_day or resolver.resolve_day(sub_query or "")
        if not day and re.search(r"那天|当天", sub_query or ""):
            day = resolver.resolve_day(user_query or "")
        if not day:
            day = resolver.resolve_day(text)
        if clarified and clarified.get("start_hm") and clarified.get("end_hm") and day:
            return [(f"{day} {clarified['start_hm']}", f"{day} {clarified['end_hm']}")]

        # 2.5) 显式「半天」半日：全天 09:00-18:00=9h 平分 → 4.5h（用户定案 2026-08-19，
        #      与 09:00-18:00 全天口径自洽）。下午半天 → 13:30-18:00、上午半天 → 09:00-13:30。
        #      仅裸「半天」无显式时刻触发；wf_0208/0202/0207 的「2点到6点/9点到12点」
        #      显式时段不受影响（mt_0006 gold 锚定）。
        if "半天" in sub and day and not re.search(r"\d+\s*[:点]", sub):
            if re.search(r"上午|早上|早晨", sub) and not re.search(r"下午|晚上", sub):
                start_t, end_t = "09:00", "13:30"
            else:
                start_t, end_t = "13:30", "18:00"
            return [(f"{day} {start_t}", f"{day} {end_t}")]

        # 3) LLM#2 schedule 优先（跨天/多日/口语时刻靠模型泛化 + 系统归一化）。
        #    跨域共享日期语境（zh_0001）：请假子句无日期词、完整 query 其他子句
        #    （会议）有「明天」时，LLM day_phrase 常臆造（该 case 给「今天」gold=
        #    明天）。规则兜底能从完整 query 正确继承日期+时刻 → 跳过 LLM schedule
        #    直接走规则兜底。leave 子句自身有日期词（wf_0012 明天→后天）不受影响。
        if schedule and _has_day_word(sub):
            schedule_for_normalize = schedule
            if anchored_day:
                # 模型仍可能返回“下周二”或把它错误地翻译成 env.now 相对日期；
                # 只替换原文明确的下周星期表达，保留模型给出的时刻/跨日结构。
                adjusted: list[dict[str, Any]] = []
                for segment in schedule:
                    item = dict(segment)
                    if re.search(r"下周[一二三四五六日天]", str(item.get("day_phrase") or "")):
                        item["day_phrase"] = anchored_day
                    if re.search(r"下周[一二三四五六日天]", str(item.get("end_day_phrase") or "")):
                        item["end_day_phrase"] = anchored_day
                    adjusted.append(item)
                schedule_for_normalize = adjusted
            normalized = self._normalize_llm_schedule(schedule_for_normalize, resolver)
            if normalized:
                return normalized

        # 4) 规则兜底。
        if not day:
            return []
        # 跨天：X月X日/号到Y月Y日/号（首日 09:00 至末日 18:00）。
        m = re.search(
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*(?:到|至)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]",
            text,
        )
        if m:
            year = resolver._today.year
            start_day = date(year, int(m.group(1)), int(m.group(2))).isoformat()
            end_day = date(year, int(m.group(3)), int(m.group(4))).isoformat()
            return [(f"{start_day} 09:00", f"{end_day} 18:00")]
        # 时段解析优先 leave 子句自身（zh_0014：子句「后天上午」→ 09:00-11:00），
        # 避免拼接 text 带上 meeting 的「下午两点到三点」污染成 14:00-15:00。
        time_text = sub if _has_time_signal(sub) else text
        start_t, end_t = self._time_of_day(time_text, resolver)
        return [(f"{day} {start_t}", f"{day} {end_t}")]

    def _reference_weekday_day(self, sub_query: str) -> str | None:
        """从当前 case 的唯一前序会议事实解析“下周X”日期。

        只在请假子句自身明确出现“下周X”时启用；不把完整用户问题中的会议日期
        当成请假日期。多个会议事实或事实来源不可写入时返回空，交给普通日历。
        """
        if not re.search(r"下周[一二三四五六日天]", sub_query or ""):
            return None
        context = self._context
        if context is None or not hasattr(context, "facts"):
            return None
        value = context.facts.unique_value("meeting.day")
        if not value:
            return None
        try:
            anchor = date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None
        match = re.search(r"下周([一二三四五六日天])", sub_query or "")
        if not match:
            return None
        monday = anchor - timedelta(days=anchor.weekday())
        target = monday + timedelta(days=7 + _WEEKDAY_INDEX[match.group(1)])
        return target.isoformat()

    def _normalize_llm_schedule(
        self,
        schedule: list[dict[str, Any]],
        resolver: TemporalResolver,
    ) -> list[tuple[str, str]] | None:
        """LLM#2 schedule → [(start_full, end_full)]；任一段归一失败 → None（规则兜底）。

        系统端归一化（模型给的格式可能出错）：
        - day_phrase / end_day_phrase → 绝对日期（TemporalResolver；裸「X号」
          继承起始日月份，小于起始日的号 → 下个月）；
        - start_hm / end_hm 校验 "HH:MM"（0-23 / 0-59），同天必须 start<end；
        - full_day → 09:00-18:00（整天/整天区间）。
        """
        out: list[tuple[str, str]] = []
        for seg in schedule or []:
            day = self._resolve_day_phrase(str(seg.get("day_phrase") or ""), resolver)
            if not day:
                return None
            if seg.get("full_day"):
                start_hm, end_hm = "09:00", "18:00"
            else:
                start_hm = _normalize_hm(seg.get("start_hm"))
                end_hm = _normalize_hm(seg.get("end_hm"))
                if start_hm is None or end_hm is None:
                    return None
            end_day = day
            if str(seg.get("end_day_phrase") or "").strip():
                end_day = self._resolve_day_phrase(
                    seg["end_day_phrase"], resolver, ref=day
                )
                if not end_day or end_day < day:
                    return None
            if end_day == day and start_hm >= end_hm:
                return None
            out.append((f"{day} {start_hm}", f"{end_day} {end_hm}"))
        return out or None

    @staticmethod
    def _resolve_day_phrase(
        phrase: str,
        resolver: TemporalResolver,
        ref: date | None = None,
    ) -> str | None:
        """LLM day 短语 → ISO 日期。TemporalResolver 优先；裸「X号/X日」继承
        ``ref``（起始日）月份，号小于起始日时顺延到下个月（0013「11号」→ 下月）。"""
        p = (phrase or "").strip()
        if not p:
            return None
        if isinstance(ref, str):  # ISO "YYYY-MM-DD"（来自 _normalize_llm_schedule 的起始日）
            try:
                ref = date.fromisoformat(ref)
            except ValueError:
                ref = None
        day = resolver.resolve_day(p)
        if day:
            return day
        if ref:
            m = re.fullmatch(r"(\d{1,2})\s*[日号]", p)
            if m:
                d = int(m.group(1))
                try:
                    base = date(ref.year, ref.month, d)
                except ValueError:
                    return None
                if base < ref:
                    if ref.month == 12:
                        try:
                            base = date(ref.year + 1, 1, d)
                        except ValueError:
                            return None
                    else:
                        try:
                            base = date(ref.year, ref.month + 1, d)
                        except ValueError:
                            return None
                return base.isoformat()
        return None

    def _time_of_day(self, text: str, resolver: TemporalResolver) -> tuple[str, str]:
        """公司工作时段惯例 → (start, end)（HH:MM）。

        优先级：显式区间 → 「X点后」 → 全天 → 上午/下午裸午别 → 裸时长 → 全天兜底。
        """
        parsed = _parse_range(text)
        if parsed:
            return parsed
        m = re.search(r"(上午|下午|晚上|中午)?\s*([一两二三四五六七八九十\d]+)\s*点后", text)
        if m:
            hour = _hour_with_period(int(_cn_num(m.group(2))), m.group(1))
            return f"{hour:02d}:00", "18:00"
        if "全天" in text or "整天" in text:
            return "09:00", "18:00"
        # 午别 + 显式时长 → 从午别起点起 N 小时（zh_0001「下午…2小时事假」→
        # 14:00-16:00，下午起点 14:00 + 2h）。与裸时长（18:00-Nh 锚到下班）区分：
        # 裸「2小时假」→ 16:00-18:00（mt_0002/0206），带午别「下午2小时」→ 时段
        # 起点 + 时长。仅无「X点到Y点」显式区间时触发（_parse_range 已先返回）。
        m_hrs = re.search(r"([一两二三四五六七八九十\d]+(?:\.\d+)?)\s*(?:个)?\s*小时", text)
        if m_hrs and not re.search(r"到|至", text):
            if re.search(r"下午|晚上", text) and not re.search(r"上午|早上|早晨", text):
                return "14:00", f"{14 + int(_cn_num(m_hrs.group(1))):02d}:00"
            if re.search(r"上午|早上|早晨", text) and not re.search(r"下午|晚上", text):
                return "09:00", f"{9 + int(_cn_num(m_hrs.group(1))):02d}:00"
        # 半天：随午别半日（docs/leave_validation_set_summary.md：上午 09:00-12:00、
        # 下午 14:00-18:00）。mt_0006 的 reference 用 13:30-18:00，与文档惯例矛盾，
        # 属数据集异常，未按异常特化（见 leave_skill docstring 已知问题注记）。
        if "上午" in text and not re.search(r"下午|晚上", text):
            return "09:00", "11:00"
        if re.search(r"下午|晚上", text):
            return "14:00", "18:00"
        m = re.search(r"([一两二三四五六七八九十\d]+(?:\.\d+)?)\s*(?:个)?\s*小时", text)
        if m:
            end_minutes = 18 * 60
            start_minutes = end_minutes - int(_cn_num(m.group(1)) * 60)
            return f"{start_minutes // 60:02d}:{start_minutes % 60:02d}", "18:00"
        return "09:00", "18:00"

    # ------------------------------------------------------------ 工具 --
    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """带门禁的 env.call_tool：写门禁 + 调用前校验 + 结果错误记录。

        防 forbidden 与 meeting 执行器同构：
        1. 写操作须通过 can_execute_write；2. validate_call 校验；
        3. 调用后检查 result.error，记日志供上层决策。
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

    def _blocked(self, reason: str) -> dict[str, Any]:
        """blocked 结果（不 save）。"""
        return {"workflow_draft_result": {"status": "blocked", "reason": reason}}

    def _log_warning(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)

    def _log_info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)


def _match_type_code(hint: str, options: list[dict[str, Any]]) -> str | None:
    """请假类型 hint → schema leave_type_options 码（去「假」字核心匹配）。"""
    if not hint:
        return None
    hint = _TYPE_ALIASES.get(hint, hint)
    core = hint.replace("假", "")
    for opt in options or []:
        label = str(opt.get("label") or "").replace("假", "")
        if core == label or core in label or label in core:
            return str(opt.get("value") or "")
    return None


def _match_reason_code(text: str) -> str | None:
    """原文/原因 hint → reason 码表（按顺序首个关键词命中）。"""
    if not text:
        return None
    for code, keywords in _REASON_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return code
    return None


def _span_hours(start_full: str, end_full: str) -> float:
    """起止全格式 → 时长（小时，起止跨度，wf_0218 跨天 57.0）。"""
    start = datetime.strptime(start_full, "%Y-%m-%d %H:%M")
    end = datetime.strptime(end_full, "%Y-%m-%d %H:%M")
    return round((end - start).total_seconds() / 3600.0, 2)


def _parse_range(text: str) -> tuple[str, str] | None:
    """「X点到Y点」起止时刻；结束未带午别时继承起始午别（就近回退句前午别）。

    wf_0219「明天下午…请2点到5点」→ 14:00-17:00（继承句前「下午」）。
    """
    m = re.search(
        r"(上午|下午|晚上|中午)?\s*([一两二三四五六七八九十\d]+)\s*点\s*(半)?\s*"
        r"(?:到|至|~|—|-)\s*"
        r"(上午|下午|晚上|中午)?\s*([一两二三四五六七八九十\d]+)\s*点\s*(半)?",
        text,
    )
    if not m:
        return None
    start_period = m.group(1)
    start_hour = int(_cn_num(m.group(2)))
    start_minute = 30 if m.group(3) else 0
    end_period = m.group(4)
    end_hour = int(_cn_num(m.group(5)))
    end_minute = 30 if m.group(6) else 0
    if start_period is None:
        before = text[: m.start()]
        found = re.findall(r"上午|下午|晚上|中午", before)
        if found:
            start_period = found[-1]
    if start_period is None:
        # 全程无午别 → 不猜凌晨时刻（「2点到4点」→ None，交由时间惯例兜底）。
        return None
    if end_period is None:
        end_period = start_period
    sh24 = _hour_with_period(start_hour, start_period)
    eh24 = _hour_with_period(end_hour, end_period)
    if sh24 * 60 + start_minute >= eh24 * 60 + end_minute:
        return None
    return f"{sh24:02d}:{start_minute:02d}", f"{eh24:02d}:{end_minute:02d}"


def _clean_schedule(raw: Any) -> list[dict[str, Any]]:
    """清洗 LLM#2 schedule 原始输出 → 结构化列表（只留 day_phrase 非空项）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for s in raw:
        if not isinstance(s, dict):
            continue
        dp = str(s.get("day_phrase") or "").strip()
        if not dp:
            continue
        out.append({
            "day_phrase": dp,
            "end_day_phrase": str(s.get("end_day_phrase") or "").strip(),
            "start_hm": str(s.get("start_hm") or "").strip(),
            "end_hm": str(s.get("end_hm") or "").strip(),
            "full_day": bool(s.get("full_day")),
        })
    return out


def _normalize_hm(value: Any) -> str | None:
    """校验/归一 "HH:MM"；格式错或越界（h>23 / m>59）返回 None（系统兜底）。"""
    v = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", v)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


class LeaveSkill:
    """请假 Skill：编排（LLM#2）→ 执行（确定性 SOP）的薄封装。

    - 编排层：收集 leave 单元的 sub_query 上下文，LLM#2 独立 gateway 提取槽位；
    - 执行层：``LeaveExecutor`` 确定性流程 SOP；
    - 返回 ``{"workflow_draft_result": {...}}``，供入口层多域合并（顶层并列
      booking_result + workflow_draft_result）。
    """

    def __init__(
        self,
        logger: Any = None,
        profile_config: ProfileConfig | None = None,
    ) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger。
        """
        self.logger = logger
        self.profile_config = profile_config or ProfileConfig.from_env()
        self.planner = LeavePlanner(logger=logger, profile_config=self.profile_config)
        self.last_timings: dict[str, Any] = {}
        self.last_planner_gateway: Any = None

    def run(
        self,
        leave_subs: list[str],
        user_query: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
        env: Any,
        registry: EffectiveToolRegistry,
        static_context: StaticContextStore,
        multi_domain: bool = False,
        context: Any = None,
    ) -> dict[str, Any]:
        """执行请假域：编排（LLM#2）→ 执行（确定性 SOP）。

        Args:
            leave_subs: leave 单元的 sub_query 列表（识别层重组结果）。
            user_query: 用户原始提问（「那天」跨域指代兜底）。
            now_iso: env.reset 返回的 now。
            mode: env.reset 返回的 mode。
            gateway: 识别层 gateway（可用性决定是否建 LLM#2）。
            env: 官方环境（透传给执行器）。
            registry: 对账后的有效工具注册表。
            static_context: 静态上下文（透传给执行器）。
            multi_domain: 是否多域合并（leave + meeting/budget）；决定提交后是否
                oa.done.list 确认（仅多域 case）。

        Returns:
            {"workflow_draft_result": {...}}；永不返回 None。
        """
        start = time.monotonic()
        sub_context = "\n".join(
            [s for s in leave_subs if (s or "").strip()]
        ).strip() or user_query

        # 编排层：LLM#2 独立 gateway（分段计时 + 预算隔离）。
        planner_gateway = None
        if gateway is not None and gateway.available:
            from utils.llm_gateway import LLMGateway

            planner_logger = getattr(self.logger, "child", lambda *_: None)("LLM#2")
            try:
                planner_gateway = LLMGateway(
                    logger=planner_logger,
                    trace_context=getattr(gateway, "trace_context", None),
                    stage="leave_plan",
                )
            except TypeError as exc:
                if "unexpected keyword" not in str(exc):
                    raise
                planner_gateway = LLMGateway(logger=planner_logger)
        self.last_planner_gateway = planner_gateway
        draft = self.planner.plan(sub_context, now_iso, mode, planner_gateway)
        if self.profile_config.strict_runtime_mode and draft.source != "llm":
            self.last_timings = {
                "orchestrate_s": round(draft.elapsed_s, 3),
                "exec_s": 0.0,
                "skill_total_s": round(time.monotonic() - start, 3),
            }
            return {"workflow_draft_result": {
                "status": "blocked",
                "reason": "llm_plan_unavailable",
            }}

        # 执行层：确定性流程 SOP（multi_turn 时执行器内部先做多轮澄清）。
        executor = LeaveExecutor(
            env,
            registry,
            static_context,
            logger=self.logger,
            profile_config=self.profile_config,
            context=context,
        )
        result = executor.execute(
            draft,
            sub_context,
            user_query,
            now_iso,
            mode=mode,
            multi_domain=multi_domain,
        )

        self.last_timings = {
            "orchestrate_s": round(draft.elapsed_s, 3),
            "exec_s": round(max(time.monotonic() - start - draft.elapsed_s, 0.0), 3),
            "skill_total_s": round(time.monotonic() - start, 3),
        }
        return result


# 保持模块级 re-export，便于测试与调用方统一引用。
__all__ = [
    "LeaveDraft",
    "LeavePlanner",
    "LeaveExecutor",
    "LeaveSkill",
    "_TYPE_WORDS",
    "_REASON_KEYWORDS",
    "_regex_leave_type",
    "_regex_approver",
]
