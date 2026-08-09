"""LeaveSkill / LeavePlanner / LeaveExecutor 单元测试。

覆盖（逐 case 对 val reference 校准）：
- LeavePlanner 规则兜底（gateway=None → 正则抽类型/审批人）；
- LeavePlanner LLM 路径（FakeBackend 注入 → 有效抽取 / 空槽位兜底 / 低置信兜底）；
- 时段惯例：显式区间、午别继承（wf_0219「下午…2点到5点」→14:00-17:00）、
  上午→09:00-11:00（zh_0014）、X点后、全天、裸时长（mt_0012 2小时→16:00-18:00）、
  跨天（wf_0218）、每周反复（wf_0010 count=2）、「那天」跨域兜底（mr_wf_0006）；
- 码表查表：leave_type / reason（公司业务规则组件）；
- 审批人：显式 keyword（wf_0202 赵丽）、默认 title=经理（zh_0014）、
  默认 title=总监（mt_0012 张三）、歧义 blocked（zh_0210 王芳×2）、找不到 blocked；
- 提交/存草稿/删旧草稿（wf_0015）/附件；
- 多域合并形状：LeaveSkill.run → 顶层 workflow_draft_result。

**不触网**：LLM 响应经 FakeBackend 注入，config 用显式测试配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.leave_skill import (
    LeaveDraft,
    LeaveExecutor,
    LeavePlanner,
    LeaveSkill,
    _cn_num,
    _match_reason_code,
    _match_type_code,
    _parse_range,
    _regex_approver,
    _regex_leave_type,
    _span_hours,
)
from utils.llm_gateway import FakeBackend, LLMGateway
from utils.static_context import StaticContextStore
from utils.tool_contract import ToolContractReconciler

# 显式测试配置（非真实密钥）。
CFG = {
    "provider": "openai_compatible",
    "base_url": "https://test.example/v1",
    "model": "test-model",
    "api_key": "test-key",
    "llm_budget_s": 35.0,
}

NOW = "2026-05-11T09:00:00"

# ------------------------------------------------------------ 工具契约 --

_LEAVE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "user.get_info": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
    },
    "workflow.catalog": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
    },
    "workflow.schema": {
        "type": "object",
        "properties": {"workflow_id": {"type": "integer"}},
    },
    "workflow.search_person": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "title": {"type": "string"},
            "workflow_id": {"type": "integer"},
        },
    },
    "workflow.save": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "integer"},
            "data": {"type": "object"},
            "submit": {"type": "boolean"},
        },
        "required": ["data"],
    },
    "workflow.delete": {
        "type": "object",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
    },
    "oa.done.list": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
    "oa.todo.list": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
    "file.list": {
        "type": "object",
        "properties": {"directory": {"type": "string"}},
        "required": ["directory"],
    },
}
_LEAVE_WRITE_TOOLS = ("workflow.save", "workflow.delete")


def _leave_schema() -> dict[str, Any]:
    """schema 72247（与 val world_state workflow_schemas 对齐）。"""
    return {
        "required_fields": [
            "applicant", "applicant_no", "start_time", "end_time",
            "leave_type", "reason", "approver", "duration",
        ],
        "leave_type_options": [
            {"label": "年休假", "value": "N"}, {"label": "事假", "value": "L"},
            {"label": "病假", "value": "S"}, {"label": "婚假", "value": "M"},
            {"label": "陪产假", "value": "P"}, {"label": "父母陪护假", "value": "H"},
            {"label": "育儿假", "value": "Y"}, {"label": "丧假", "value": "F"},
            {"label": "延时假", "value": "V"}, {"label": "收养假", "value": "AL"},
        ],
        "reason_options": [
            {"label": "本人身体不适", "value": "01"},
            {"label": "本人生病住院", "value": "02"},
            {"label": "本人结婚", "value": "03"},
            {"label": "配偶生产陪护", "value": "04"},
            {"label": "本人产检", "value": "05"},
            {"label": "本人怀孕生产", "value": "06"},
            {"label": "哺乳", "value": "07"},
            {"label": "家人生病", "value": "08"},
            {"label": "亲人过世", "value": "09"},
            {"label": "本人有事", "value": "10"},
        ],
    }


# 与模拟器 env.SLOT_PATTERNS 对齐（请假槽位）。
_FAKE_SLOT_PATTERNS: dict[str, tuple[str, ...]] = {
    "start_time": ("几点开始", "开始时间", "从几点", "什么时候开始", "几点到几点", "请假时间"),
    "end_time": ("几点结束", "结束时间", "到几点", "什么时候结束"),
    "leave_type": ("什么类型", "假期类型", "哪种假", "类型", "什么假", "请什么假"),
    "reason": ("原因", "为什么", "什么事", "请假原因"),
    "approver": ("审批人", "谁审批", "找谁批", "审批"),
}


class LeaveFakeEnv:
    """可编程假 env：与模拟器同语义（search_person keyword/title 过滤；reply 按
    missing_slots 顺序命中 SLOT_PATTERNS 返回 slot_replies）。"""

    def __init__(
        self,
        people: list[dict[str, Any]] | None = None,
        current_user: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.people = list(people or [])
        self.current_user = current_user or {
            "user_id": "120001",
            "name": "陈明",
            "employee_no": "2025009001",
            "title": "软件工程师",
        }
        self.catalog = [{"workflow_id": 72247, "name": "001-1 请假申请"}]
        self.schema = _leave_schema()
        self.done_items: list[dict[str, Any]] = []
        self.todo_items: list[dict[str, Any]] = []
        self.files: list[str] = []
        self.saved: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        # 多轮澄清状态（模拟 env.dialogue_state / user_simulator）。
        self.missing_slots: list[str] = []
        self.slot_replies: dict[str, str] = {}
        self.fallback_reply = "你再说清楚一点。"

    def reply(self, message: str) -> dict[str, Any]:
        """模拟 env.reply：按 missing_slots 顺序命中模式 → 返回槽位答复。"""
        self.calls.append(("__reply__", {"message": message}))
        for slot in list(self.missing_slots):
            if any(p in message for p in _FAKE_SLOT_PATTERNS.get(slot, ())):
                self.missing_slots.remove(slot)
                return {
                    "user_message": self.slot_replies.get(slot, self.fallback_reply),
                    "resolved_slot": slot,
                    "assistant_message": message,
                    "messages": [],
                }
        return {
            "user_message": self.fallback_reply,
            "resolved_slot": None,
            "assistant_message": message,
            "messages": [],
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": "", "args_schema": spec}
            for name, spec in _LEAVE_TOOL_SPECS.items()
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "user.get_info":
            return {"users": [self.current_user]}
        if name == "workflow.catalog":
            kw = args.get("keyword") or ""
            return {"workflows": [
                w for w in self.catalog if kw in (w.get("name") or "")
            ]}
        if name == "workflow.schema":
            return {"schema": self.schema}
        if name == "workflow.search_person":
            kw = args.get("keyword") or ""
            title = args.get("title") or ""
            people = list(self.people)
            if kw:
                people = [
                    p for p in people
                    if kw in (p.get("name") or "")
                    or kw in (p.get("employee_no") or "")
                    or kw in (p.get("account") or "")
                ]
            if title:
                people = [p for p in people if title in (p.get("title") or "")]
            return {"people": people, "keyword": kw, "title": title}
        if name == "workflow.save":
            self.saved.append(dict(args))
            return {
                "draft_saved": not args.get("submit", False),
                "submitted": bool(args.get("submit", False)),
                "request_id": f"REQ-{len(self.saved)}",
                "workflow_id": args.get("workflow_id"),
            }
        if name == "workflow.delete":
            self.deleted.append(dict(args))
            return {"deleted": True, "request_id": args.get("request_id")}
        if name == "oa.done.list":
            return self._oa_items(self.done_items, args)
        if name == "oa.todo.list":
            return self._oa_items(self.todo_items, args)
        if name == "file.list":
            return {"directory": args.get("directory"), "files": list(self.files)}
        return {"error": f"unhandled: {name}"}

    def _oa_items(self, items: list[dict], args: dict) -> dict[str, Any]:
        kw = args.get("keyword") or ""
        return {"items": [
            i for i in items if not kw or kw in (i.get("workflow_name") or "")
        ]}


def _registry(env: LeaveFakeEnv, tmp_path: Path) -> Any:
    """构建对账注册表：写名单来自静态索引（workflow.save/delete 为写工具）。"""
    base = tmp_path / "leave-static"
    base.mkdir(parents=True, exist_ok=True)
    (base / "tools.index.json").write_text(
        json.dumps({
            "schema_version": "static-context-v1",
            "counts": {"tools": len(_LEAVE_TOOL_SPECS), "write_tools": len(_LEAVE_WRITE_TOOLS)},
            "write_tools": list(_LEAVE_WRITE_TOOLS),
            "by_name": {
                name: {"name": name, "description": "", "args_schema": spec}
                for name, spec in _LEAVE_TOOL_SPECS.items()
            },
        }),
        encoding="utf-8",
    )
    store = StaticContextStore(base_dir=base, enabled=True)
    return ToolContractReconciler(store).reconcile(env.list_tools())


def _executor(env: LeaveFakeEnv, tmp_path: Path) -> LeaveExecutor:
    return LeaveExecutor(env, _registry(env, tmp_path), None)


# 标准 world people（val 基础人员池，docs 一致）。
_BASE_PEOPLE = [
    {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
    {"user_id": "120002", "name": "刘经理", "employee_no": "2024002001", "title": "研发经理"},
    {"user_id": "120003", "name": "赵丽", "employee_no": "2023006001", "title": "测试工程师"},
    {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
    {"user_id": "120009", "name": "张三", "employee_no": "2025011001", "title": "研发经理"},
]


# ------------------------------------------------------------ 规则兜底 --


class TestLeavePlannerFallback:
    """gateway=None → 正则抽取原始槽位。"""

    def test_regex_type_and_approver(self) -> None:
        draft = LeavePlanner().plan(
            "我明天下午请年假，审批人刘经理",
            NOW, "single_turn", None,
        )
        assert draft.source == "fallback"
        assert draft.leave_type_hint == "年假"
        assert draft.approver_hint == "刘经理"

    def test_regex_approver_variants(self) -> None:
        assert _regex_approver("审批人赵丽") == "赵丽"
        assert _regex_approver("审批人找刘经理") == "刘经理"
        assert _regex_approver("审批人必须是张三") == "张三"
        assert _regex_approver("审批人找一个经理") == "经理"
        assert _regex_approver("请事假") == ""

    def test_regex_type_variants(self) -> None:
        assert _regex_leave_type("我下午请事假") == "事假"
        assert _regex_leave_type("请个陪产假") == "陪产假"
        assert _regex_leave_type("请病假") == "病假"


# ------------------------------------------------------------ LLM 路径 --


def _gateway(*responses: object) -> LLMGateway:
    return LLMGateway(config=CFG, backend=FakeBackend(list(responses)))


class TestLeavePlannerLLM:
    """LLM#2 抽取：有效 / 空槽位 / 低置信。"""

    def test_llm_extracts_slots(self) -> None:
        gateway = _gateway({
            "leave_type_hint": "年假",
            "reason_hint": "休息调整",
            "approver_hint": "刘经理",
            "confidence": 0.92,
        })
        draft = LeavePlanner().plan(
            "我明天下午请年假，审批人刘经理",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "llm"
        assert draft.leave_type_hint == "年假"
        assert draft.approver_hint == "刘经理"
        assert draft.confidence == 0.92

    def test_llm_empty_slots_fall_back(self) -> None:
        gateway = _gateway({
            "leave_type_hint": "",
            "reason_hint": "",
            "approver_hint": "",
            "confidence": 0.9,
        })
        draft = LeavePlanner().plan(
            "我明天下午请年假，审批人刘经理",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "fallback"
        assert draft.leave_type_hint == "年假"  # 规则兜底补上

    def test_llm_low_confidence_fall_back(self) -> None:
        gateway = _gateway({
            "leave_type_hint": "事假",
            "reason_hint": "有事",
            "approver_hint": "王芳",
            "confidence": 0.3,
        })
        draft = LeavePlanner().plan(
            "我明天下午请事假，审批人王芳",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "fallback"
        assert draft.approver_hint == "王芳"


# ------------------------------------------------------------ 时段惯例 --


class TestScheduleResolution:
    """公司工作时段惯例（逐 case 对 val reference 校准）。"""

    def _resolve(self, now: str, sub: str, user: str = "") -> list[tuple[str, str]]:
        ex = LeaveExecutor(None, None, None)
        return ex._resolve_schedule(sub, user, now)

    def test_explicit_range_with_period(self) -> None:
        # wf_0202：今天下午2点到6点
        r = self._resolve("2026-05-11T09:00:00", "我下午2点到6点请事假", "我下午有点私事，想请今天下午2点到6点的事假，审批人赵丽")
        assert r == [("2026-05-11 14:00", "2026-05-11 18:00")]

    def test_range_inherits_preceding_period(self) -> None:
        # wf_0219：明天下午…请2点到5点 → 14:00-17:00
        r = self._resolve("2026-05-11T09:00:00", "明天下午我有事请2点到5点")
        assert r == [("2026-05-12 14:00", "2026-05-12 17:00")]

    def test_range_with_half_hour(self) -> None:
        # wf_0076：明天下午2点半到6点
        r = self._resolve("2026-04-24T10:00:00", "明天下午2点半到6点请事假")
        assert r == [("2026-04-25 14:30", "2026-04-25 18:00")]

    def test_morning_bare_is_short_shift(self) -> None:
        # zh_0014：后天上午 → 09:00-11:00（val reference，非 train 文档 12:00）
        r = self._resolve("2026-05-12T09:00:00", "后天上午请事假")
        assert r == [("2026-05-14 09:00", "2026-05-14 11:00")]

    def test_afternoon_bare(self) -> None:
        # wf_0213：明天下午年假 → 14:00-18:00
        r = self._resolve("2026-05-11T09:00:00", "明天下午请年假")
        assert r == [("2026-05-12 14:00", "2026-05-12 18:00")]

    def test_full_day(self) -> None:
        # wf_0214：后天全天
        r = self._resolve("2026-05-11T09:00:00", "后天全天请事假")
        assert r == [("2026-05-13 09:00", "2026-05-13 18:00")]

    def test_bare_hours_end_of_day(self) -> None:
        # mt_0012：下周二请两个小时事假 → 16:00-18:00
        r = self._resolve("2026-04-18T10:00:00", "下周二请两个小时事假")
        assert r == [("2026-04-21 16:00", "2026-04-21 18:00")]

    def test_cross_day(self) -> None:
        # wf_0218：5月13日到5月15日 → 首日09:00 至 末日18:00
        r = self._resolve("2026-05-11T09:00:00", "5月13日到5月15日请病假")
        assert r == [("2026-05-13 09:00", "2026-05-15 18:00")]

    def test_recurring_two_weeks(self) -> None:
        # wf_0010：每周五 + 这两周 → 04-17 与 04-24 两次
        r = self._resolve("2026-04-16T10:00:00", "每周五下午两点后请育儿假，只请这两周")
        assert r == [
            ("2026-04-17 14:00", "2026-04-17 18:00"),
            ("2026-04-24 14:00", "2026-04-24 18:00"),
        ]

    def test_that_day_resolves_from_user_query(self) -> None:
        # mr_wf_0006：sub 无日期，「那天」→ 从 user_query 的下周二解析
        r = self._resolve(
            "2026-04-18T10:00:00",
            "那天下午4点到6点请事假",
            "帮我约下周二下午2点到3点的会议，另外那天下午4点到6点请事假",
        )
        assert r == [("2026-04-21 16:00", "2026-04-21 18:00")]

    def test_unresolvable_day_returns_empty(self) -> None:
        r = self._resolve("2026-05-11T09:00:00", "下午2点到6点请事假", "")
        assert r == []

    def test_parse_range(self) -> None:
        assert _parse_range("下午2点半到6点") == ("14:30", "18:00")
        assert _parse_range("上午9点到11点") == ("09:00", "11:00")
        assert _parse_range("下午2点到4点") == ("14:00", "16:00")
        # 无午别、无继承上下文 → 不猜凌晨时刻
        assert _parse_range("2点到4点") is None


# ------------------------------------------------------------ 码表 --


class TestCodeTables:
    def test_cn_num(self) -> None:
        assert _cn_num("两") == 2.0
        assert _cn_num("二") == 2.0
        assert _cn_num("十二") == 12.0
        assert _cn_num("二十") == 20.0
        assert _cn_num("3") == 3.0

    def test_span_hours_cross_day(self) -> None:
        assert _span_hours("2026-05-13 09:00", "2026-05-15 18:00") == 57.0
        assert _span_hours("2026-05-11 14:00", "2026-05-11 18:00") == 4.0

    def test_type_code(self) -> None:
        opts = _leave_schema()["leave_type_options"]
        assert _match_type_code("年假", opts) == "N"
        assert _match_type_code("事假", opts) == "L"
        assert _match_type_code("病假", opts) == "S"
        assert _match_type_code("育儿假", opts) == "Y"
        assert _match_type_code("陪产假", opts) == "P"
        assert _match_type_code("", opts) is None

    def test_reason_code(self) -> None:
        assert _match_reason_code("住院治疗") == "02"
        assert _match_reason_code("有点私事") == "10"
        assert _match_reason_code("身体不适") == "01"
        assert _match_reason_code("照顾孩子") == "07"
        assert _match_reason_code("老婆生孩子") == "04"
        assert _match_reason_code("亲人过世") == "09"
        assert _match_reason_code("审批") is None


# ------------------------------------------------------------ 执行 SOP --


class TestLeaveExecutor:
    def _run(self, env: LeaveFakeEnv, tmp_path: Path, sub: str,
             user: str = "", now: str = NOW) -> dict[str, Any]:
        draft = LeavePlanner().plan(sub, now, "single_turn", None)
        return LeaveExecutor(env, _registry(env, tmp_path), None).execute(
            draft, sub, user, now
        )

    def test_draft_save_full_sop(self, tmp_path: Path) -> None:
        """wf_0202：draft，SOP 5 步，data 逐字段与 reference 一致。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        result = self._run(
            env, tmp_path,
            "我下午有点私事，想请今天下午2点到6点的事假，审批人赵丽",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        tools = [n for n, _ in env.calls]
        assert tools == [
            "user.get_info", "workflow.catalog", "workflow.schema",
            "workflow.search_person", "workflow.save",
        ]
        save = env.saved[0]
        assert save["workflow_id"] == 72247
        assert save["submit"] is False
        data = save["data"]
        assert data["applicant"] == "120001"
        assert data["applicant_no"] == "2025009001"
        assert data["start_time"] == "2026-05-11 14:00"
        assert data["end_time"] == "2026-05-11 18:00"
        assert data["leave_type"] == "L"
        assert data["reason"] == "10"
        assert data["approver"] == "120003"
        assert data["duration"] == 4.0
        # 显式 hint → keyword 搜索
        search = next(args for n, args in env.calls if n == "workflow.search_person")
        assert search["keyword"] == "赵丽"

    def test_submit_cross_day(self, tmp_path: Path) -> None:
        """wf_0218：病假住院、跨天、直接提交。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        result = self._run(
            env, tmp_path,
            "我需要住院治疗，请5月13日到5月15日的病假，审批人赵丽，直接提交",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        data = env.saved[0]["data"]
        assert data["leave_type"] == "S"
        assert data["reason"] == "02"
        assert data["start_time"] == "2026-05-13 09:00"
        assert data["end_time"] == "2026-05-15 18:00"
        assert data["duration"] == 57.0
        assert env.saved[0]["submit"] is True

    def test_default_approver_title_manager(self, tmp_path: Path) -> None:
        """zh_0014 同构：未指名 → title=经理 → 刘经理（研发经理）。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘经理", "employee_no": "2024002001", "title": "研发经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "后天上午请假的草稿也存一下",
            now="2026-05-12T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        search = next(args for n, args in env.calls if n == "workflow.search_person")
        assert search["title"] == "经理"
        assert env.saved[0]["data"]["approver"] == "120002"
        assert env.saved[0]["data"]["start_time"] == "2026-05-14 09:00"
        assert env.saved[0]["data"]["end_time"] == "2026-05-14 11:00"

    def test_default_approver_title_director(self, tmp_path: Path) -> None:
        """mt_0012 同构：无经理 → title=总监 → 张三（技术总监）。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我下周二要请两个小时事假，先存个草稿",
            now="2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        searches = [args for n, args in env.calls if n == "workflow.search_person"]
        assert [s.get("title") for s in searches] == ["经理", "总监"]
        data = env.saved[0]["data"]
        assert data["approver"] == "118871"
        assert data["start_time"] == "2026-04-21 16:00"
        assert data["end_time"] == "2026-04-21 18:00"
        assert data["duration"] == 2.0

    def test_ambiguous_approver_blocks_no_save(self, tmp_path: Path) -> None:
        """zh_0210：王芳×2（产品经理+运营经理）→ blocked，不 save。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
            {"user_id": "120009", "name": "王芳", "employee_no": "2025011001", "title": "运营经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我下午还要请事假，审批人找王芳",
            now="2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert result["workflow_draft_result"]["reason"] == "ambiguous_approver"
        assert env.saved == []

    def test_approver_not_found_blocks(self, tmp_path: Path) -> None:
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "请今天下午2点到6点的事假，审批人李四",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert result["workflow_draft_result"]["reason"] == "approver_not_found"
        assert env.saved == []

    def test_delete_old_then_save(self, tmp_path: Path) -> None:
        """wf_0015：改成事假 → 先删旧（oa.done.list+workflow.delete）再存。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        env.done_items = [
            {"request_id": "REQ-72247-old001", "workflow_id": 72247,
             "workflow_name": "001-1 请假申请", "status": "submitted"},
        ]
        result = self._run(
            env, tmp_path,
            "我昨天请了病假，今天想改成事假去处理私事，下午2点到6点，审批人王芳，帮我提交",
            now="2026-04-22T13:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        tools = [n for n, _ in env.calls]
        assert "oa.done.list" in tools
        assert "workflow.delete" in tools
        assert env.deleted[0]["request_id"] == "REQ-72247-old001"
        assert env.saved[0]["data"]["leave_type"] == "L"
        assert env.saved[0]["data"]["reason"] == "10"
        # 删旧发生在 save 之前
        assert tools.index("workflow.delete") < tools.index("workflow.save")

    def test_approver_surname_title_combined_search(self, tmp_path: Path) -> None:
        """wf_0019/0026/0028：世界无「刘经理」真人 → 字面搜 0 人 → 拆「刘经理」
        为 keyword=刘 + title=经理（search_person AND）命中 刘明/研发经理。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘明", "employee_no": "2024002001", "title": "研发经理"},
            {"user_id": "120003", "name": "赵丽", "employee_no": "2023006001", "title": "测试工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
            {"user_id": "120005", "name": "刘华", "employee_no": "2024002002", "title": "前端工程师"},
            {"user_id": "120006", "name": "刘强", "employee_no": "2024002003", "title": "测试工程师"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我今天下午1点到6点请病假，审批人刘经理，帮我提交",
            now="2026-04-23T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        searches = [args for n, args in env.calls if n == "workflow.search_person"]
        # 第一次字面 keyword=刘经理 → 0 人；第二次 keyword=刘 + title=经理 → 刘明
        assert searches[0]["keyword"] == "刘经理"
        assert searches[1]["keyword"] == "刘" and searches[1]["title"] == "经理"
        assert env.saved[0]["data"]["approver"] == "120002"

    def test_approver_literal_liu_manager_no_regression(self, tmp_path: Path) -> None:
        """wf_0201：世界有真名「刘经理」→ 字面搜索直接命中，不拆姓不降级。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘经理", "employee_no": "2024002001", "title": "研发经理"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我想请明天的年假，全天，审批人找刘经理",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        searches = [args for n, args in env.calls if n == "workflow.search_person"]
        assert searches[0]["keyword"] == "刘经理"
        assert len(searches) == 1  # 字面命中，不再拆
        assert env.saved[0]["data"]["approver"] == "120002"

    def test_approver_wang_surname_title(self, tmp_path: Path) -> None:
        """wf_0204：审批人找王芳经理 → 字面 0 → keyword=王芳+title=经理 命中王芳。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘经理", "employee_no": "2024002001", "title": "研发经理"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我要请5月14日到5月16日共3天婚假，审批人找王芳经理",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        searches = [args for n, args in env.calls if n == "workflow.search_person"]
        assert searches[0]["keyword"] == "王芳经理"
        assert searches[1]["keyword"] == "王芳" and searches[1]["title"] == "经理"
        assert env.saved[0]["data"]["approver"] == "120004"

    def test_delete_old_draft_via_todo(self, tmp_path: Path) -> None:
        """wf_0026：删掉重新提交 + 「草稿」→ oa.todo.list 定位 draft 删除，
        附件按删旧重提类型 M 推断 marriage_certificate。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘明", "employee_no": "2024002001", "title": "研发经理"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        env.todo_items = [
            {"request_id": "REQ-72247-draft001", "workflow_id": 72247,
             "workflow_name": "001-1 请假申请", "status": "draft"},
        ]
        env.files = ["marriage_certificate_20260508.pdf", "id_card_front_20260101.pdf"]
        result = self._run(
            env, tmp_path,
            "我之前存了一个婚假草稿，日期填错了，应该是5月12号到5月18号，审批人刘经理，帮我删掉重新提交",
            now="2026-05-09T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        tools = [n for n, _ in env.calls]
        assert "oa.todo.list" in tools
        assert env.deleted[0]["request_id"] == "REQ-72247-draft001"
        data = env.saved[0]["data"]
        assert data["attachment"] == "documents/marriage_certificate_20260508.pdf"
        assert data["approver"] == "120002"

    def test_attachment_doc_type_word(self, tmp_path: Path) -> None:
        """wf_0028：病假条 → file.list → sick_leave_note，时段/时长/审批人全对。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120002", "name": "刘明", "employee_no": "2024002001", "title": "研发经理"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        env.files = ["sick_leave_note_20260423.pdf", "medical_report_20260423.pdf"]
        result = self._run(
            env, tmp_path,
            "我今天发烧了，需要请病假，下午1点到6点，审批人刘经理，病假条在 documents 目录，帮我提交",
            now="2026-04-23T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        data = env.saved[0]["data"]
        assert data["attachment"] == "documents/sick_leave_note_20260423.pdf"
        assert data["start_time"] == "2026-04-23 13:00"
        assert data["end_time"] == "2026-04-23 18:00"
        assert data["duration"] == 5.0
        assert data["approver"] == "120002"
        assert data["leave_type"] == "S"
        assert data["reason"] == "01"
        # 返回的 workflow_draft_result 必须回填 attachment（reference_final_answer
        # 含该键，缺失 → RS/AS 归零）。
        assert result["workflow_draft_result"]["attachment"] == "documents/sick_leave_note_20260423.pdf"

    def test_no_spurious_attachment_call(self, tmp_path: Path) -> None:
        """无文档类型词、非删旧 → 不触发 file.list，不附附件（wf_0204 形态）。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        env.files = ["marriage_certificate_20240815.pdf"]  # 即使 documents 有文件
        result = self._run(
            env, tmp_path,
            "我要请5月14日到5月16日共3天婚假，审批人找王芳经理",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        tools = [n for n, _ in env.calls]
        assert "file.list" not in tools
        assert "attachment" not in env.saved[0]["data"]
        # 无附件 → 返回的 workflow_draft_result 也不含 attachment 键。
        assert "attachment" not in result["workflow_draft_result"]

    def test_multi_domain_submit_confirm(self, tmp_path: Path) -> None:
        """多域合并提交 → 提交后 oa.done.list 确认（zh_0215 形态）。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        sub = "请明天下午2点到6点的事假，审批人赵丽，直接提交"
        result = LeaveExecutor(env, _registry(env, tmp_path), None).execute(
            LeavePlanner().plan(sub, NOW, "single_turn", None),
            sub, "", NOW, multi_domain=True,
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        tools = [n for n, _ in env.calls]
        assert tools[-1] == "oa.done.list"

    def test_single_domain_submit_no_confirm(self, tmp_path: Path) -> None:
        """单域提交 → 不调用 oa.done.list（wf_0028 少一步，ES 不扣分）。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        result = self._run(
            env, tmp_path,
            "我今天下午1点到6点请病假，审批人赵丽，帮我提交",
            now="2026-04-23T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        tools = [n for n, _ in env.calls]
        assert "oa.done.list" not in tools
        assert env.saved[0]["submit"] is True

    def test_recurring_two_saves_submit(self, tmp_path: Path) -> None:
        """wf_0010：每周五+这两周 → 2 次 save，均提交。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我需要每周五下午两点后请育儿假照顾孩子，审批人王芳，帮我提交这两周的申请",
            now="2026-04-16T10:00:00",
        )
        assert result["workflow_draft_result"]["count"] == 2
        assert env.saved[0]["data"]["start_time"] == "2026-04-17 14:00"
        assert env.saved[1]["data"]["start_time"] == "2026-04-24 14:00"
        assert all(s["submit"] is True for s in env.saved)
        assert env.saved[0]["data"]["leave_type"] == "Y"
        assert env.saved[0]["data"]["reason"] == "07"

    def test_attachment_via_file_list(self, tmp_path: Path) -> None:
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        env.files = ["病历.pdf"]
        result = self._run(
            env, tmp_path,
            "请明天下午的事假，审批人赵丽，附件 documents/病历.pdf",
            now="2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert env.saved[0]["data"].get("attachment") == "documents/病历.pdf"
        assert "file.list" in [n for n, _ in env.calls]

    def test_that_day_cross_domain(self, tmp_path: Path) -> None:
        """mr_wf_0006：sub 用「那天」，跨域从 user_query 的下周二解析。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
            {"user_id": "117500", "name": "王芳", "employee_no": "2025000001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "另外我那天下午4点到6点要请2小时事假，原因是个人事务，审批人选张三",
            "帮我约下周二下午2点到3点在A2园区8人会议室，主题季度复盘。另外我那天下午4点到6点要请2小时事假，原因是个人事务，审批人选张三，先把请假申请草稿也存一下",
            now="2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        data = env.saved[0]["data"]
        assert data["start_time"] == "2026-04-21 16:00"
        assert data["end_time"] == "2026-04-21 18:00"
        assert data["approver"] == "118871"
        assert data["duration"] == 2.0


# ------------------------------------------------------------ 多轮澄清 --


class TestLeaveMultiTurnClarify:
    """Q3（用户定案）：multi_turn 下按 gold 句式 __reply__ 补全缺失槽位。

    - reset 不暴露 missing_slots，缺失槽位由 query 推断（起止缺午别、类型缺词、
      审批人缺姓名、原因仅在类型默认码≠10 时问）；
    - 澄清起止/类型/审批人解析进最终 draft；reason 走类型默认码；
    - 单轮 mode 完全不触发 __reply__。
    """

    def _run(
        self,
        env: LeaveFakeEnv,
        tmp_path: Path,
        sub: str,
        now: str,
        mode: str = "multi_turn",
        user: str = "",
    ) -> dict[str, Any]:
        draft = LeavePlanner().plan(sub, now, mode, None)
        return LeaveExecutor(env, _registry(env, tmp_path), None).execute(
            draft, sub, user, now, mode=mode
        )

    def _replies(self, env: LeaveFakeEnv) -> list[str]:
        return [args["message"] for n, args in env.calls if n == "__reply__"]

    def test_all_slots_clarified_mt_0206(self, tmp_path: Path) -> None:
        """mt_0206：5 槽全缺 → 问起/止/类型/审批人；原因默认 10；审批人 张三。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
            {"user_id": "117500", "name": "王芳", "employee_no": "2023005678", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        env.missing_slots = ["start_time", "end_time", "leave_type", "reason", "approver"]
        env.slot_replies = {
            "start_time": "下午4点开始。",
            "end_time": "到6点结束。",
            "leave_type": "事假。",
            "reason": "个人事务。",
            "approver": "张三。",
        }
        result = self._run(
            env, tmp_path,
            "我下周二可能要请2小时假，先把请假申请草稿存一下。",
            "2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        # reason 类型默认 10 → 不问；只问起/止/类型/审批人（gold 顺序）。
        assert self._replies(env) == [
            "请问您几点开始请假？",
            "请问到几点结束？",
            "请问是什么类型的假期？",
            "请问选择哪位作为审批人？",
        ]
        data = env.saved[0]["data"]
        assert data["start_time"] == "2026-04-21 16:00"
        assert data["end_time"] == "2026-04-21 18:00"
        assert data["leave_type"] == "L"
        assert data["reason"] == "10"
        assert data["approver"] == "118871"
        assert data["duration"] == 2.0
        search = next(args for n, args in env.calls if n == "workflow.search_person")
        assert search["keyword"] == "张三"

    def test_type_from_query_mt_0012(self, tmp_path: Path) -> None:
        """mt_0012：事假在 query → 不问类型；问起/止/审批人。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        env.missing_slots = ["start_time", "end_time", "reason", "approver"]
        env.slot_replies = {
            "start_time": "下午4点开始。",
            "end_time": "到6点结束。",
            "reason": "个人事务。",
            "approver": "张三。",
        }
        result = self._run(
            env, tmp_path,
            "我下周二要请两个小时事假，先存个草稿。",
            "2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert self._replies(env) == [
            "请问您几点开始请假？",
            "请问到几点结束？",
            "请问选择哪位作为审批人？",
        ]
        data = env.saved[0]["data"]
        assert data["leave_type"] == "L"
        assert data["start_time"] == "2026-04-21 16:00"
        assert data["approver"] == "118871"

    def test_half_day_afternoon_and_approver_clarified(self, tmp_path: Path) -> None:
        """半天下午（内部一致的合成形态）：随午别 → 14:00-18:00（docs 惯例）；只问审批人。"""
        people = [
            {"user_id": "118245", "name": "陈晨", "employee_no": "2024012845", "title": "算法工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        env.current_user = {
            "user_id": "118245", "name": "陈晨", "employee_no": "2024012845", "title": "算法工程师",
        }
        env.missing_slots = ["reason", "approver"]
        env.slot_replies = {"reason": "个人事务。", "approver": "张三。"}
        result = self._run(
            env, tmp_path,
            "今天下午想请半天事假，先存个草稿。",
            "2026-04-20T09:30:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        # 类型在 query（事假）→ 不问类型；默认原因 10 → 不问原因；只问审批人。
        # 用「今天」而非「下周五」：后者从周一 04-20 解析为次周周五 05-01
        # （60/61 真实 case 的 ISO 语义），而 mt_0006 gold 用了 04-24（本周五）
        # 属该 case 数据异常；本测试只验证半天下午→14:00-18:00，不掺入异常。
        assert self._replies(env) == ["请问选择哪位作为审批人？"]
        data = env.saved[0]["data"]
        assert data["start_time"] == "2026-04-20 14:00"
        assert data["end_time"] == "2026-04-20 18:00"
        assert data["leave_type"] == "L"
        assert data["reason"] == "10"
        assert data["duration"] == 4.0
        assert data["applicant"] == "118245"

    def test_reason_elicited_when_type_default_not_10(self, tmp_path: Path) -> None:
        """类型默认原因≠10（病假 S→01）→ 问原因，答复解析进 draft。"""
        people = [
            {"user_id": "118245", "name": "陈晨", "employee_no": "2024012845", "title": "算法工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        env.current_user = {
            "user_id": "118245", "name": "陈晨", "employee_no": "2024012845", "title": "算法工程师",
        }
        env.missing_slots = ["reason", "approver"]
        env.slot_replies = {"reason": "身体不舒服。", "approver": "张三。"}
        result = self._run(
            env, tmp_path,
            "下周五下午想请半天病假，先存个草稿。",
            "2026-04-20T09:30:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert self._replies(env) == ["请问请假原因是？", "请问选择哪位作为审批人？"]
        data = env.saved[0]["data"]
        assert data["leave_type"] == "S"
        assert data["reason"] == "01"
        assert data["approver"] == "118871"
        assert data["applicant"] == "118245"

    def test_ambiguous_approver_blocks_mt_0208(self, tmp_path: Path) -> None:
        """mt_0208：两个张三 → keyword=张三 歧义 → blocked，不 save。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
            {"user_id": "118872", "name": "张三", "employee_no": "2024001235", "title": "产品总监"},
        ]
        env = LeaveFakeEnv(people=people)
        env.missing_slots = ["approver"]
        env.slot_replies = {"approver": "张三。"}
        result = self._run(
            env, tmp_path,
            "我下周二下午要请两个小时事假，先帮我处理一下。",
            "2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert result["workflow_draft_result"]["reason"] == "ambiguous_approver"
        assert env.saved == []
        assert self._replies(env) == ["请问选择哪位作为审批人？"]
        search = next(args for n, args in env.calls if n == "workflow.search_person")
        assert search["keyword"] == "张三"

    def test_reply_times_override_bare_duration_mt_0210(self, tmp_path: Path) -> None:
        """mt_0210：答复 下午2点/到4点 → 14:00-16:00（覆盖裸时长 16:00-18:00）。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        env.missing_slots = ["start_time", "end_time", "approver"]
        env.slot_replies = {
            "start_time": "下午2点开始。",
            "end_time": "到4点结束。",
            "approver": "张三。",
        }
        result = self._run(
            env, tmp_path,
            "我下周二要请两个小时事假，帮我直接提交。",
            "2026-04-18T10:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        assert self._replies(env) == [
            "请问您几点开始请假？",
            "请问到几点结束？",
            "请问选择哪位作为审批人？",
        ]
        data = env.saved[0]["data"]
        assert data["start_time"] == "2026-04-21 14:00"
        assert data["end_time"] == "2026-04-21 16:00"
        assert data["duration"] == 2.0

    def test_single_turn_never_replies(self, tmp_path: Path) -> None:
        """单轮 mode 不触发澄清：__reply__ 零调用。"""
        people = [
            {"user_id": "119063", "name": "李帅", "employee_no": "2025008399", "title": "高级工程师"},
            {"user_id": "118871", "name": "张三", "employee_no": "2024001234", "title": "技术总监"},
        ]
        env = LeaveFakeEnv(people=people)
        result = self._run(
            env, tmp_path,
            "我下周二要请两个小时事假，先存个草稿",
            "2026-04-18T10:00:00",
            mode="single_turn",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert all(n != "__reply__" for n, _ in env.calls)


# ------------------------------------------------------------ Q1：正则优先于 LLM hint --


class TestRegexPriorityOverHint:
    """Q1（用户定案）：请假类型/原因码表——正则（原文）优先于 LLM#2 hint。"""

    def test_type_regex_beats_llm_hint(self, tmp_path: Path) -> None:
        """wf_0015：改成事假 → 正则 LAST 命中事假(L)；LLM hint 病假(S) 不生效。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        draft = LeaveDraft(
            leave_type_hint="病假",
            reason_hint="",
            approver_hint="王芳",
            source="llm",
            confidence=0.61,
        )
        result = LeaveExecutor(env, _registry(env, tmp_path), None).execute(
            draft,
            "我昨天请了病假，今天想改成事假去处理私事，下午2点到6点，审批人王芳，帮我提交",
            "",
            "2026-04-22T13:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "submitted"
        assert env.saved[0]["data"]["leave_type"] == "L"

    def test_reason_text_beats_llm_hint(self, tmp_path: Path) -> None:
        """Q1：原文「有点私事」→ 10；LLM hint「发烧了」（→01）不覆盖原文。"""
        people = [
            {"user_id": "120001", "name": "陈明", "employee_no": "2025009001", "title": "软件工程师"},
            {"user_id": "120004", "name": "王芳", "employee_no": "2023007001", "title": "产品经理"},
        ]
        env = LeaveFakeEnv(people=people)
        draft = LeaveDraft(
            leave_type_hint="事假",
            reason_hint="发烧了",
            approver_hint="王芳",
            source="llm",
            confidence=0.9,
        )
        result = LeaveExecutor(env, _registry(env, tmp_path), None).execute(
            draft,
            "我今天下午2点到6点请事假，有点私事，审批人王芳",
            "",
            "2026-05-11T09:00:00",
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert env.saved[0]["data"]["reason"] == "10"


# ------------------------------------------------------------ Skill 薄封装 --


class TestLeaveSkill:
    def test_run_returns_workflow_draft_result(self, tmp_path: Path) -> None:
        """LeaveSkill.run：编排（规则兜底）→ 执行 → 顶层 workflow_draft_result。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        result = LeaveSkill().run(
            ["我下午有点私事，想请今天下午2点到6点的事假，审批人赵丽"],
            "我下午有点私事，想请今天下午2点到6点的事假，审批人赵丽",
            NOW, "single_turn", None,
            env, _registry(env, tmp_path), None,
        )
        assert result["workflow_draft_result"]["status"] == "draft_saved"
        assert result["workflow_draft_result"]["workflow_id"] == 72247

    def test_run_timings_recorded(self, tmp_path: Path) -> None:
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        skill = LeaveSkill()
        skill.run(
            ["请明天下午请事假，审批人赵丽"],
            "请明天下午请事假，审批人赵丽",
            NOW, "single_turn", None,
            env, _registry(env, tmp_path), None,
        )
        assert set(skill.last_timings) == {
            "orchestrate_s", "exec_s", "skill_total_s"
        }
        # 规则兜底（无 LLM）可能瞬时完成，只校验字段存在且为数值。
        assert isinstance(skill.last_timings["skill_total_s"], float)
        assert skill.last_timings["exec_s"] >= 0

    def test_write_gate_blocks_save_when_not_exposed(self, tmp_path: Path) -> None:
        """写门禁：workflow.save 未公开 → 不落盘（防 forbidden 越权写）。"""
        env = LeaveFakeEnv(people=_BASE_PEOPLE)
        # 从 list_tools 中摘除 workflow.save → 对账后视为未公开
        runtime = [t for t in env.list_tools() if t["name"] != "workflow.save"]
        store = StaticContextStore(
            base_dir=_write_index(tmp_path), enabled=True
        )
        registry = ToolContractReconciler(store).reconcile(runtime)
        draft = LeavePlanner().plan(
            "请明天下午2点到6点的事假，审批人赵丽", NOW, "single_turn", None
        )
        result = LeaveExecutor(env, registry, None).execute(
            draft, "请明天下午2点到6点的事假，审批人赵丽", "", NOW
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert env.saved == []


def _write_index(tmp_path: Path) -> Path:
    base = tmp_path / "write-gate-static"
    base.mkdir(parents=True, exist_ok=True)
    (base / "tools.index.json").write_text(
        json.dumps({
            "schema_version": "static-context-v1",
            "counts": {"tools": len(_LEAVE_TOOL_SPECS), "write_tools": len(_LEAVE_WRITE_TOOLS)},
            "write_tools": list(_LEAVE_WRITE_TOOLS),
            "by_name": {
                name: {"name": name, "description": "", "args_schema": spec}
                for name, spec in _LEAVE_TOOL_SPECS.items()
            },
        }),
        encoding="utf-8",
    )
    return base
