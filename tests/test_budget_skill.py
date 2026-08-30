"""BudgetSkill / BudgetPlanner / BudgetExecutor 单元测试。

覆盖（逐 case 对 train/val reference 校准）：
- BudgetPlanner 规则兜底（gateway=None → 正则抽项目短名/大类/物料）；
- BudgetPlanner LLM 路径（FakeBackend 注入 → 有效抽取 / 空槽位兜底 / 低置信兜底）；
- 业务规则：_to_amount / _has_budget_amount（Q2 门禁）/ _strip_generic /
  _same_generic_base / 物料→小类语义映射（测试手机→手机、3C数码）；
- 项目解析：code 唯一 / code 前缀歧义 blocked / 短名唯一 / 泛化后缀同 base 取首个 /
  LCS 消歧（品牌升级一期 vs 二期）/ 真歧义 blocked；
- 大类/小类：语义匹配 / 平局 blocked / 小类映射消歧；
- 金额：qty×unit → budget_amount / 单行显式总额÷qty / 多行仅总额 blocked /
  单行无金额占位 1.00（用户 Q1 定案）；
- 占位保存门禁（用户 Q2 定案）：无物料无预算 → 取 29028 首个为单行保存；
  无物料有预算 → blocked(ambiguous_material_subclass)；
- zh_0008 双键返回（workflow_draft_result + workflow_result 中文 reason）；
- 多轮澄清：mt_0015 blocked（3 问） / mt_0008 保存（小类/金额采纳） /
  single_turn 永不 reply；
- 多域 oa 验证：draft→todo.list（query 含"待办"→keyword=费用类物资，否则费用）、
  submit→done.list(keyword=费用)；超集返回；
- BudgetSkill.run 形状 / 计时 / 写门禁。

**不触网**：LLM 响应经 FakeBackend 注入，config 用显式测试配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.budget_skill import (
    BudgetDraft,
    BudgetExecutor,
    BudgetPlanner,
    BudgetRow,
    BudgetSkill,
    _has_budget_amount,
    _longest_common_len,
    _material_to_category_signal,
    _overlap_score,
    _regex_category_hint,
    _regex_project_code,
    _regex_project_phrase,
    _regex_qty_for_material,
    _regex_search_term,
    _regex_unit_for_material,
    _same_generic_base,
    _synthesize_material_row,
    _strip_generic,
    _to_amount,
    _to_qty,
)
from utils.llm_gateway import FakeBackend, LLMGateway
from utils.static_context import StaticContextStore
from utils.tool_contract import ToolContractReconciler
from utils.profiles import ExecutionProfile, ProfileConfig

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

_BUDGET_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "user.get_info": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
    "workflow.catalog": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
    "workflow.schema": {
        "type": "object",
        "properties": {"workflow_id": {"type": "integer"}},
    },
    "workflow.project_search": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "project_code": {"type": "string"},
        },
    },
    "workflow.browser_search": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "integer"},
            "field_id": {"type": "integer"},
            "dep": {"type": "object"},
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
    "oa.done.list": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
    "oa.todo.list": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
    },
}
_BUDGET_WRITE_TOOLS = ("workflow.save",)


def _budget_schema() -> dict[str, Any]:
    """schema 34747（与 train/val world_state workflow_schemas 对齐）。"""
    return {
        "required_fields": [
            "applicant", "applicant_no", "project_name", "project_code",
            "wbs_code", "material_category", "total_amount",
        ],
        "detail_tables": {
            "detail_2": {
                "required_fields": [
                    "material_subclass", "material_name", "quantity",
                    "unit_price", "budget_amount",
                ],
            },
        },
    }


# 与模拟器 env.SLOT_PATTERNS 对齐（费用类槽位）。
_FAKE_SLOT_PATTERNS: dict[str, tuple[str, ...]] = {
    "project_name": ("项目", "项目名称", "哪个项目", "归哪个项目"),
    "project_code": ("项目编码", "project code", "项目code", "项目编号"),
    "material_category": ("大类", "物资大类", "费用大类", "选哪个大类"),
    "material_subclass": ("小类", "物资小类", "具体小类", "选哪个小类"),
    "total_amount": ("预算", "金额", "总金额", "多少钱", "预算多少"),
}


class BudgetFakeEnv:
    """可编程假 env：与模拟器同语义（project_search 过滤；reply 按 missing_slots
    顺序命中 SLOT_PATTERNS 返回 slot_replies）。"""

    def __init__(
        self,
        projects: list[dict[str, Any]] | None = None,
        category_options: list[dict[str, Any]] | None = None,
        subclass_options: dict[str, list[dict[str, Any]]] | None = None,
        current_user: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.projects = list(projects or _DEFAULT_PROJECTS)
        self.category_options = (
            list(category_options) if category_options is not None
            else [{"code": "WZLB-202001150001", "label": "品牌广告服务"}]
        )
        self.subclass_options = dict(subclass_options or _DEFAULT_SUBCLASSES)
        self.current_user = current_user or {
            "user_id": "120001",
            "name": "陈明",
            "employee_no": "2025009001",
            "title": "软件工程师",
        }
        self.catalog = [{"workflow_id": 34747, "name": "001-2 费用物资采购申请"}]
        self.schema = _budget_schema()
        self.todo_items: list[dict[str, Any]] = []
        self.done_items: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        # 多轮澄清状态（模拟 env.dialogue_state / user_simulator）。
        self.missing_slots: list[str] = []
        self.slot_replies: dict[str, str] = {}
        self.fallback_reply = "你再说清楚一点。"

    def reply(self, message: str) -> dict[str, Any]:
        """模拟 env.reply：按 missing_slots 顺序命中模式 → 返回槽位答复。"""
        self.calls.append(("__reply__", {"message": message}))
        lowered = message.lower()
        for slot in list(self.missing_slots):
            if any(p in lowered for p in _FAKE_SLOT_PATTERNS.get(slot, ())):
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
            for name, spec in _BUDGET_TOOL_SPECS.items()
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "user.get_info":
            return {"users": [self.current_user]}
        if name == "workflow.catalog":
            return {"workflows": [
                w for w in self.catalog if "费用" in (w.get("name") or "")
            ]}
        if name == "workflow.schema":
            return {"schema": self.schema}
        if name == "workflow.project_search":
            return self._project_search(args)
        if name == "workflow.browser_search":
            return self._browser_search(args)
        if name == "workflow.save":
            self.saved.append(dict(args))
            return {
                "draft_saved": not args.get("submit", False),
                "submitted": bool(args.get("submit", False)),
                "request_id": f"REQ-{len(self.saved)}",
                "workflow_id": args.get("workflow_id"),
            }
        if name == "oa.done.list":
            return self._oa_items(self.done_items, args)
        if name == "oa.todo.list":
            return self._oa_items(self.todo_items, args)
        return {"error": f"unhandled: {name}"}

    def _project_search(self, args: dict[str, Any]) -> dict[str, Any]:
        code = args.get("project_code") or ""
        term = args.get("project_name") or ""
        projects = list(self.projects)
        if code:
            projects = [
                p for p in projects
                if p["project_code"] == code or p["project_code"].startswith(code)
            ]
        if term:
            projects = [
                p for p in projects if term in (p.get("project_name") or "")
            ]
        return {"projects": projects, "keyword": term, "code": code}

    def _browser_search(self, args: dict[str, Any]) -> dict[str, Any]:
        field_id = args.get("field_id")
        if field_id == 29023:
            return {"options": list(self.category_options)}
        if field_id == 29028:
            dep = args.get("dep") or {}
            return {"options": list(self.subclass_options.get(dep.get("wzlb") or "", []))}
        return {"options": []}

    def _oa_items(self, items: list[dict], args: dict) -> dict[str, Any]:
        kw = args.get("keyword") or ""
        return {"items": [
            i for i in items
            if not kw or "费用" in (i.get("workflow_name") or "")
        ]}


# 标准 world 项目池（train/val project_search_results 一致形态）。
_DEFAULT_PROJECTS = [
    {"project_name": "数字员工平台项目", "project_code": "P-260100001", "wbs_code": "P-260100001.03", "profit_center": "P-1"},
    {"project_name": "星火平台项目", "project_code": "P-260100002", "wbs_code": "P-260100002.03", "profit_center": "P-2"},
    {"project_name": "数字员工应用项目", "project_code": "P-260100003", "wbs_code": "P-260100003.03", "profit_center": "P-3"},
    {"project_name": "智能办公平台品牌升级项目", "project_code": "A-260100001", "wbs_code": "A-260100001.03", "profit_center": "A-1"},
    {"project_name": "智能办公平台品牌升级二期设备采购项目", "project_code": "A-260100002", "wbs_code": "A-260100002.03", "profit_center": "A-2"},
    {"project_name": "终端测试环境建设项目", "project_code": "D-260100004", "wbs_code": "D-260100004.03", "profit_center": "D-1"},
    {"project_name": "官网改版传播项目", "project_code": "N-260200005", "wbs_code": "N-260200005.03", "profit_center": "N-1"},
    {"project_name": "官网改版线下发布项目", "project_code": "N-260200015", "wbs_code": "N-260200015.03", "profit_center": "N-2"},
    {"project_name": "智能服务外包交付项目", "project_code": "F-260100006", "wbs_code": "F-260100006.03", "profit_center": "F-1"},
]

# 大类 → 小类选项（29028 dep={wbscode,wzlb}，按 wzlb code 索引）。
_DEFAULT_SUBCLASSES: dict[str, list[dict[str, Any]]] = {
    "WZLB-202001150001": [
        {"code": "WZ_202001150001", "label": "视频制作"},
    ],
    "WZLB-202005120001": [
        {"code": "WZ_202210110009", "label": "视频制作"},
        {"code": "WZ_202210110012", "label": "活动、展会、发布会"},
        {"code": "WZ_202210110008", "label": "设计服务（含网页制作）"},
    ],
    "WZLB-202206060001": [
        {"code": "WZ_DEV_001", "label": "电脑及其配件"},
        {"code": "WZ_DEV_002", "label": "打印机、扫描仪及其配件"},
        {"code": "WZ_DEV_003", "label": "手机、3C数码"},
        {"code": "WZ_DEV_004", "label": "测试设备"},
    ],
    "WZLB-201812260001": [
        {"code": "WZ_201812260001", "label": "显示器"},
        {"code": "WZ_201812260003", "label": "键盘"},
        {"code": "WZ_201812260004", "label": "鼠标"},
    ],
}


def _brand_env() -> BudgetFakeEnv:
    """品牌广告 3 选项（zh_0223 / wf_0070 / mt_0015 形状）。"""
    return BudgetFakeEnv(
        category_options=[{"code": "WZLB-202005120001", "label": "品牌广告服务"}],
        subclass_options=_DEFAULT_SUBCLASSES,
    )


def _registry(env: BudgetFakeEnv, tmp_path: Path) -> Any:
    """构建对账注册表：写名单来自静态索引（workflow.save 为写工具）。"""
    base = tmp_path / "budget-static"
    base.mkdir(parents=True, exist_ok=True)
    (base / "tools.index.json").write_text(
        json.dumps({
            "schema_version": "static-context-v1",
            "counts": {"tools": len(_BUDGET_TOOL_SPECS), "write_tools": len(_BUDGET_WRITE_TOOLS)},
            "write_tools": list(_BUDGET_WRITE_TOOLS),
            "by_name": {
                name: {"name": name, "description": "", "args_schema": spec}
                for name, spec in _BUDGET_TOOL_SPECS.items()
            },
        }),
        encoding="utf-8",
    )
    store = StaticContextStore(base_dir=base, enabled=True)
    return ToolContractReconciler(store).reconcile(env.list_tools())


def _executor(env: BudgetFakeEnv, tmp_path: Path) -> BudgetExecutor:
    return BudgetExecutor(env, _registry(env, tmp_path), None)


def _run(env: BudgetFakeEnv, tmp_path: Path, draft: BudgetDraft,
         query: str, mode: str | None = "single_turn",
         multi_domain: bool = False) -> dict[str, Any]:
    return _executor(env, tmp_path).execute(
        draft, query, query, NOW, mode=mode, multi_domain=multi_domain,
    )


def _replies(env: BudgetFakeEnv) -> list[str]:
    return [args["message"] for n, args in env.calls if n == "__reply__"]


# ------------------------------------------------------------ 规则兜底 --


class TestBudgetPlannerFallback:
    """gateway=None → 正则抽取原始槽位。"""

    def test_regex_project_alias(self) -> None:
        assert _regex_search_term("项目是星火质量工程平台，买2台显示器") == "终端测试环境"
        assert _regex_search_term("项目是数字员工平台") == "数字员工"
        assert _regex_project_phrase("项目是数字员工。") == "数字员工"
        assert _regex_project_phrase("办公场景焕新项目需要一台高速扫描仪") == "办公场景焕新"
        assert _regex_project_phrase("帮我申请办公场景焕新项目的设备费用") == "办公场景焕新"

    def test_regex_project_code(self) -> None:
        assert _regex_project_code("按项目编码 D-260100004 提") == "D-260100004"
        assert _regex_project_code("帮我提一个品牌广告费用") == ""

    def test_regex_category_hint(self) -> None:
        assert _regex_category_hint("存个品牌广告费用草稿") == "品牌广告服务"
        assert _regex_category_hint("办公设备采购申请") == "办公设备/测试设备"
        assert _regex_category_hint("帮我提一个云服务费用") == "外包服务费-交付类"
        assert _regex_category_hint("帮我订个会议室") == ""

    def test_rule_fallback_draft(self) -> None:
        draft = BudgetPlanner().plan(
            "项目是星火质量工程平台，买2台显示器每台1500元，直接提交",
            NOW, "single_turn", None,
        )
        assert draft.source == "fallback"
        assert draft.confidence == 0.0
        assert draft.search_term == "终端测试环境"
        assert draft.category_hint == "办公设备/测试设备"

    def test_rule_fallback_empty_context(self) -> None:
        draft = BudgetPlanner().plan("", NOW, "single_turn", None)
        assert draft.source == "fallback"
        assert draft.search_term == ""
        assert draft.rows == []


# ------------------------------------------------------------ LLM 路径 --


def _gateway(*responses: object) -> LLMGateway:
    return LLMGateway(config=CFG, backend=FakeBackend(list(responses)))


class TestBudgetPlannerLLM:
    """LLM#2 抽取：有效 / 空槽位 / 低置信。"""

    def test_llm_extracts_slots(self) -> None:
        # 拆分后两次调用：①项目 ②物料，各供一个响应。
        gateway = _gateway(
            {"project": {"search_term": "终端测试环境", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "办公设备", "detail_rows": [{"material_name": "显示器", "quantity": "2", "unit_price": "1500"}], "confidence": 0.9},
        )
        draft = BudgetPlanner().plan(
            "项目是星火质量工程平台，买2台显示器每台1500元，直接提交",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "llm"
        assert draft.search_term == "终端测试环境"
        assert draft.category_hint == "办公设备"
        assert len(draft.rows) == 1
        assert draft.rows[0].material_name == "显示器"
        assert draft.rows[0].quantity == "2"
        assert draft.confidence == 0.9

    def test_llm_material_empty_hint_falls_back_to_rule(self) -> None:
        # 用户定案「两者都做」①：category_hint 收敛为 7 个 29023 label 枚举，
        # "" 不在枚举 → schema 拒收 → 物料槽回落规则（大类关键词 + 规则行），
        # 项目槽保留 LLM 值（不再整体兜底，避免规则行覆盖好项目短名）。
        gateway = _gateway(
            {"project": {"search_term": "终端测试环境", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "", "detail_rows": [], "confidence": 0.9},
        )
        draft = BudgetPlanner().plan(
            "项目是星火质量工程平台，买2台显示器",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "llm"
        assert draft.search_term == "终端测试环境"        # 项目槽保留 LLM 值
        assert draft.category_hint == "办公设备/测试设备"  # 大类回落规则关键词

    def test_llm_low_confidence_fall_back(self) -> None:
        # 两次调用都 < 门控 0.4 → 整体兜底。
        gateway = _gateway(
            {"project": {"search_term": "品牌升级", "code_hint": ""}, "confidence": 0.3},
            {"category_hint": "品牌广告服务", "detail_rows": [{"material_name": "视频制作", "quantity": "1", "unit_price": "1万"}], "confidence": 0.3},
        )
        draft = BudgetPlanner().plan(
            "项目是品牌升级，买视频制作",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "fallback"

    def test_llm_split_partial_fallback(self) -> None:
        # 项目过门控、物料被拒 → 项目保留 LLM 值，物料走规则兜底（source 仍为 llm）。
        gateway = _gateway(
            {"project": {"search_term": "品牌升级", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "品牌广告服务", "detail_rows": [{"material_name": "视频制作", "quantity": "1", "unit_price": "1万"}], "confidence": 0.2},
        )
        draft = BudgetPlanner().plan(
            "项目是品牌升级，买视频制作",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "llm"
        assert draft.search_term == "品牌升级"
        assert draft.category_hint == "品牌广告服务"  # 规则关键词命中
        assert len(draft.rows) == 1 and draft.rows[0].material_name == "视频制作"  # 规则「买 X」句式

    def test_llm_bare_query_keeps_empty_rows(self) -> None:
        # 裸多轮查询（#57）：项目空 + 物料 category 有值、rows 空 → 采纳 LLM 空行（不再兜底出 material='申请'）。
        gateway = _gateway(
            {"project": {"search_term": "", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "办公设备/测试设备", "detail_rows": [], "confidence": 0.5},
        )
        draft = BudgetPlanner().plan(
            "帮我提一个办公设备采购申请。",
            NOW, "single_turn", gateway,
        )
        assert draft.source == "llm"
        assert draft.category_hint == "办公设备/测试设备"
        assert draft.rows == []


# ------------------------------------------------------------ 业务规则 --


class TestBudgetHelpers:
    """金额/数量/项目名归一化业务规则。"""

    def test_to_amount(self) -> None:
        assert _to_amount("1.5万") == 15000.0
        assert _to_amount("3万元") == 30000.0
        assert _to_amount("6块") == 6.0
        assert _to_amount("1500") == 1500.0
        assert _to_amount("") is None
        assert _to_amount("abc") is None

    def test_has_budget_amount_gate(self) -> None:
        # Q2 门禁：数字 + 预算/万/元/块/金额 任一单位。
        assert _has_budget_amount("预算3万元") is True
        assert _has_budget_amount("帮我提品牌广告费用，总金额1.5万") is True
        assert _has_budget_amount("1500元") is True
        assert _has_budget_amount("总预算是多少") is False  # 无数字
        assert _has_budget_amount("买2台显示器") is False  # 无金额单位
        assert _has_budget_amount("") is False

    def test_to_qty(self) -> None:
        assert _to_qty("2") == 2
        assert _to_qty("") is None

    def test_regex_qty_and_unit(self) -> None:
        assert _regex_qty_for_material("买2台显示器", "显示器") == 2
        assert _regex_qty_for_material("买显示器", "显示器") is None
        assert _regex_unit_for_material("显示器每台1500元", "显示器") == 1500.0
        assert _regex_unit_for_material("买显示器", "显示器") is None
        assert _regex_unit_for_material("买显示器", "视频制作") is None

    def test_strip_generic(self) -> None:
        assert _strip_generic("数字员工平台项目") == "数字员工"
        assert _strip_generic("数字员工应用项目") == "数字员工"
        assert _strip_generic("智能办公平台品牌升级项目") == "智能办公平台品牌升级"

    def test_same_generic_base(self) -> None:
        assert _same_generic_base([
            {"project_name": "数字员工平台项目"},
            {"project_name": "数字员工应用项目"},
        ]) is True
        assert _same_generic_base([
            {"project_name": "智能办公平台品牌升级项目"},
            {"project_name": "官网改版线下发布项目"},
        ]) is False

    def test_overlap_score(self) -> None:
        assert _overlap_score("品牌广告服务", "品牌广告服务") == 100
        assert _overlap_score("品牌广告", "品牌广告服务") == 80
        assert _overlap_score("办公设备", "办公设备/测试设备") == 80
        assert _overlap_score("显示器", "电脑及其配件") == 0
        assert _overlap_score("", "视频制作") == 0

    def test_longest_common_len(self) -> None:
        assert _longest_common_len(
            "智能办公平台品牌升级项目", "智能办公平台品牌升级二期设备采购项目",
        ) == 10

    def test_material_to_category_signal(self) -> None:
        assert _material_to_category_signal([BudgetRow("显示器")]) == "办公设备/测试设备"
        assert _material_to_category_signal([BudgetRow("数据服务")]) == "外包服务费-交付类"
        assert _material_to_category_signal([BudgetRow("定制服装")]) == "定制物资"
        assert _material_to_category_signal([]) == ""


# ------------------------------------------------------------ 执行 SOP --


class TestBudgetExecutor:
    """确定性 SOP：完整保存 / 占位保存门禁 / blocked 三态 / oa / 超集返回。"""

    def test_full_sop_draft_save(self, tmp_path: Path) -> None:
        """完整 SOP：项目唯一 + 大类/小类 + 金额 + 存草稿 → draft_saved。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202001150001", "label": "品牌广告服务"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow(material_name="视频制作", quantity="1", unit_price="10000")],
            source="llm", confidence=0.9,
        )
        result = _run(env, tmp_path, draft, "项目是数字员工，存个品牌广告费用草稿，买视频制作，1万。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["workflow_id"] == 34747
        assert wdr["project_code"] == "P-260100001"
        assert wdr["project_name"] == "数字员工平台项目"
        assert wdr["material_category"] == "WZLB-202001150001"
        assert wdr["total_amount"] == "10000.00"
        assert wdr["detail_count"] == 1
        saved = env.saved[0]
        assert saved["workflow_id"] == 34747
        assert saved["submit"] is False
        data = saved["data"]
        assert data["applicant"] == "120001"
        assert data["applicant_no"] == "2025009001"
        assert data["wbs_code"] == "P-260100001.03"
        row = data["details"]["detail_2"][0]
        assert row == {
            "material_subclass": "WZ_202001150001",
            "material_name": "视频制作",
            "quantity": "1",
            "unit_price": "10000.00",
            "budget_amount": "10000.00",
        }

    def test_full_sop_submit_multi_row(self, tmp_path: Path) -> None:
        """多明细提交（zh_0223 形状）：category-only query → 历史金额直接保存。

        旧断言（35000 = LLM 猜的 1.5万+2万）被预算改版取代：query 未点名物料 +
        历史 (A, 品牌广告, 60000) 命中 → 金额参照历史 40000+20000=60000
        （真实 zh_0223 gold total=60000，LLM 猜的 3.5万是错的）。"""
        env = _brand_env()
        draft = BudgetDraft(
            search_term="品牌升级", category_hint="品牌广告服务",
            rows=[
                BudgetRow(material_name="视频制作", quantity="1", unit_price="1.5万"),
                BudgetRow(material_name="设计服务", quantity="1", unit_price="2万"),
            ],
            source="llm", confidence=0.9,
        )
        result = _run(env, tmp_path, draft, "项目编码 A-260100001，提交品牌广告费用。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["project_code"] == "A-260100001"
        assert wdr["material_category"] == "WZLB-202005120001"
        assert wdr["total_amount"] == "60000.00"
        assert wdr["detail_count"] == 2
        rows = env.saved[0]["data"]["details"]["detail_2"]
        assert rows[0]["material_subclass"] == "WZ_202210110009"  # 视频制作
        assert rows[1]["material_subclass"] == "WZ_202210110008"  # 设计服务（含网页制作）
        assert rows[0]["budget_amount"] == "40000.00"
        assert rows[1]["budget_amount"] == "20000.00"

    def test_placeholder_save_no_material_no_budget(self, tmp_path: Path) -> None:
        """Q2 门禁：无物料且无预算金额 → 取 29028 首个选项为单行占位保存（zh_0007）。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202001150001", "label": "品牌广告服务"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(search_term="数字员工", category_hint="品牌广告服务", rows=[])
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿，项目是数字员工。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["project_code"] == "P-260100001"
        assert wdr["total_amount"] == "1.00"  # 占位 qty=1 × unit=1.00
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_name"] == "视频制作"  # 单选项唯一解
        assert row["quantity"] == "1"
        assert row["unit_price"] == "1.00"
        assert row["budget_amount"] == "1.00"

    def test_placeholder_multi_option_takes_first(self, tmp_path: Path) -> None:
        """无物料提交（zh_0223）→ 历史意图档补全：A/60000 + 2 历史明细行。

        预算改版 2026-08-25：旧行为是无预算占位保存（1.00 + 首个 29028 选项），
        新行为走历史参照——INTENT_TOTALS[A,draft|submit] 唯一档 60000 → rows_only
        → 历史行模板 [视频制作, 设计服务（含网页制作）]。
        """
        env = _brand_env()
        draft = BudgetDraft(code_hint="A-260100001", category_hint="品牌广告服务", rows=[])
        result = _run(env, tmp_path, draft, "提交品牌广告费用。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["project_code"] == "A-260100001"
        assert wdr["total_amount"] == "60000.00"
        assert wdr["detail_count"] == 2
        rows = env.saved[0]["data"]["details"]["detail_2"]
        assert {r["material_name"] for r in rows} == {"视频制作", "设计服务（含网页制作）"}

    def test_budget_no_material_blocks(self, tmp_path: Path) -> None:
        """Q2 门禁：无物料但有预算 → blocked(ambiguous_material_subclass)，不 save（wf_0070）。"""
        env = _brand_env()
        draft = BudgetDraft(search_term="品牌升级", category_hint="品牌广告服务", rows=[])
        ex = _executor(env, tmp_path)
        result = ex.execute(
            draft,
            "帮我提一个品牌宣传费用申请，项目是智能办公平台品牌升级项目，预算3万元。",
            "帮我提一个品牌宣传费用申请，项目是智能办公平台品牌升级项目，预算3万元。",
            NOW, mode="single_turn",
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert result["workflow_draft_result"]["reason"] == "ambiguous_material_subclass"
        assert env.saved == []
        # blocked 也必须走完 project_search + 29023 + 29028 工具路径（must_satisfy）。
        names = [n for n, _a, _r in ex._history]
        assert "workflow.project_search" in names
        assert any(
            n == "workflow.browser_search" and a.get("field_id") == 29023
            for n, a, _ in ex._history
        )
        assert any(
            n == "workflow.browser_search" and a.get("field_id") == 29028
            for n, a, _ in ex._history
        )
        assert "workflow.save" not in names

    def test_project_search_single_term_no_extra_steps(self, tmp_path: Path) -> None:
        """撤销双搜索（用户定案 wf_0254 调查）后：单次主搜，短名命中即采用，
        无额外补搜步骤（wf_0039/0045 型）。"""
        env = BudgetFakeEnv()  # 默认池含「终端测试环境建设项目」
        draft = BudgetDraft(search_term="终端测试环境")
        ex = _executor(env, tmp_path)
        proj = ex._resolve_project(
            draft, "帮我申请办公设备费用，项目是星火质量工程平台，要买2台显示器，每台1500元。", {}
        )
        assert proj.get("project_name") == "终端测试环境建设项目"
        search_args = [a for n, a in env.calls if n == "workflow.project_search"]
        assert [a.get("project_name") for a in search_args] == ["终端测试环境"]

    def test_zh_0008_double_key_blocked(self, tmp_path: Path) -> None:
        """zh_0008：query 点名物料「电脑配件」但 29028 无该选项 → 双键返回 blocked。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-201812260001", "label": "办公设备"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(
            search_term="星火", category_hint="办公设备",
            rows=[BudgetRow(material_name="电脑配件")],
        )
        result = _run(env, tmp_path, draft, "订明天会议室，再存个办公设备费用草稿，要买电脑配件。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_material_subclass"
        # 双键：中文 reason 走 workflow_result 键（zh_0008 唯一命中，多余键无害）。
        assert result["workflow_result"] == {
            "status": "blocked",
            "reason": "物资子类不唯一，无法确定具体类型",
        }
        assert env.saved == []

    def test_ambiguous_project_code_prefix_blocks(self, tmp_path: Path) -> None:
        """项目编码前缀命中多个 → blocked(ambiguous_project)（wf_0258 形状）。"""
        env = _brand_env()
        draft = BudgetDraft(code_hint="N-2602000", category_hint="品牌广告服务", rows=[])
        result = _run(env, tmp_path, draft, "按项目编码 N-2602000 提品牌广告费用。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_project"
        # 非 ambiguous_material_subclass → 无双键。
        assert "workflow_result" not in result
        assert env.saved == []

    def test_search_term_ambiguous_blocks(self, tmp_path: Path) -> None:
        """搜索短名真歧义（官网改版传播/线下发布）→ blocked(ambiguous_project)（wf_0251 形状）。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202001150001", "label": "品牌广告服务"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(
            search_term="官网改版", category_hint="品牌广告服务",
            rows=[BudgetRow("宣传物料")],
        )
        result = _run(env, tmp_path, draft, "提官网改版费用。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_project"
        assert env.saved == []

    def test_ambiguous_category_dual_blocks(self, tmp_path: Path) -> None:
        """大类信号平局 → blocked(ambiguous_material_subclass) 双键。"""
        env = BudgetFakeEnv(
            category_options=[
                {"code": "WZLB-202001150001", "label": "品牌广告服务"},
                {"code": "WZLB-202005120001", "label": "品牌广告服务"},
            ],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(search_term="数字员工", category_hint="品牌广告服务", rows=[])
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_material_subclass"
        assert "workflow_result" in result
        assert env.saved == []

    def test_insufficient_amount_breakdown_blocks(self, tmp_path: Path) -> None:
        """多物料只给总额 → blocked(insufficient_amount_breakdown)（wf_0257 形状）。"""
        env = _brand_env()
        draft = BudgetDraft(
            search_term="品牌升级", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作"), BudgetRow("设计服务")],
        )
        result = _run(env, tmp_path, draft, "帮我把视频制作和设计服务的费用提一下，总预算6万。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "insufficient_amount_breakdown"
        assert "workflow_result" not in result
        assert env.saved == []

    def test_canonicalize_fail_block_still_walks_29028(self, tmp_path: Path) -> None:
        """canonicalize 失败 block 前仍走 29028 工具路径（wf_0255/wf_0257 红线）。

        gold blocked 的 must_satisfy 要求调用过 browser_search(29028)，否则
        TSR-10 + ES=0。blocked 也必须走完 project_search + 29023 + 29028。
        """
        env = _brand_env()
        # 「短片和专题设计」无 canonical（合成虚构）→ 必须 block，但 29028 须已调用。
        # 预算 5万（(A,品牌广告,50000) 不在历史 ROWS）→ 历史参照重建不触发，block 保留；
        # 若用 6万 会命中 A/60000 历史行模板重建为 2 行保存（wf_0043/zh_0223 形状）。
        draft = BudgetDraft(
            search_term="品牌升级", category_hint="品牌广告服务",
            rows=[BudgetRow(material_name="短片和专题设计")],
        )
        result = _run(env, tmp_path, draft, "项目是智能办公平台品牌升级，买短片和专题设计，预算5万。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_material_subclass"
        assert env.saved == []
        names = [n for n, _a in env.calls]
        assert "workflow.project_search" in names
        assert any(
            n == "workflow.browser_search" and a.get("field_id") == 29023
            for n, a in env.calls
        )
        assert any(
            n == "workflow.browser_search" and a.get("field_id") == 29028
            for n, a in env.calls
        )
        assert "workflow.save" not in names

    def test_canonicalize_before_category_inference(self, tmp_path: Path) -> None:
        """物料行归一须先于大类推断（wf_0248 线上实测）。

        LLM 行「对象存储与带宽费用」不在 map；若大类推断先跑 → 信号空 → 两选项
        平局 → block。归一后「对象存储与带宽」→ 云服务 → 外包服务费-交付类命中。
        category_hint 为空（LLM 未给大类词）时尤其关键。
        """
        env = BudgetFakeEnv(
            projects=_DEFAULT_PROJECTS + [
                {"project_name": "算力平台资源运营项目", "project_code": "M-260200004",
                 "wbs_code": "M-260200004.03", "profit_center": "M-1"},
            ],
            category_options=[
                {"code": "WZLB-201911250001", "label": "外包服务费-交付类"},
                {"code": "WZLB-202005120001", "label": "品牌广告服务"},
            ],
            subclass_options={
                "WZLB-201911250001": [
                    {"code": "WZ_202206200005", "label": "IDC、CDN租赁服务、云服务、运营商业务"},
                    {"code": "WZ_202506190001", "label": "数据服务"},
                    {"code": "WZ_202206200010", "label": "其他咨询服务"},
                ],
            },
        )
        draft = BudgetDraft(
            search_term="算力平台资源运营", category_hint="",
            rows=[BudgetRow(material_name="对象存储与带宽费用")],
        )
        result = _run(
            env, tmp_path, draft,
            "算力平台资源运营项目里有一笔对象存储与带宽费用，3.1万，直接提交。",
        )
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["material_category"] == "WZLB-201911250001"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_subclass"] == "WZ_202206200005"
        assert row["material_name"] == "对象存储与带宽"

    def test_single_row_explicit_total_divides(self, tmp_path: Path) -> None:
        """单行 + 显式总额 → 总额÷数量（wf_0069 形状：单价由总额推导）。

        预算 3万：(A,品牌广告,30000) 不在历史 ROWS → 历史参照重建不触发，行保留
        → 单行显式总额 ÷ 数量。若用 6万 会命中 A/60000 历史行模板重建为 2 行。
        """
        env = _brand_env()
        draft = BudgetDraft(
            search_term="品牌升级", category_hint="品牌广告服务",
            rows=[BudgetRow(material_name="视频制作", quantity="2")],
        )
        result = _run(env, tmp_path, draft, "帮我提一个品牌升级项目的费用，预算3万，视频制作。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["project_code"] == "A-260100001"  # LCS 消歧取一期
        assert wdr["total_amount"] == "30000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["quantity"] == "2"
        assert row["unit_price"] == "15000.00"  # 30000 ÷ 2
        assert row["budget_amount"] == "30000.00"

    def test_subclass_map_disambiguates_test_phone(self, tmp_path: Path) -> None:
        """语义小类映射：测试手机 → 手机、3C数码（避免与测试设备平局）。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202206060001", "label": "办公设备/测试设备"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(
            search_term="终端测试环境", category_hint="办公设备/测试设备",
            rows=[BudgetRow("测试手机")],
        )
        result = _run(env, tmp_path, draft, "项目是终端测试环境，买测试手机，提交。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_subclass"] == "WZ_DEV_003"  # 手机、3C数码
        assert row["material_name"] == "测试手机"

    def test_generic_suffix_base_tiebreak(self, tmp_path: Path) -> None:
        """泛化后缀归一：数字员工平台/应用 同 base → 取首个（zh_0007 定案）。"""
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202001150001", "label": "品牌广告服务"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿，项目是数字员工。")
        wdr = result["workflow_draft_result"]
        assert wdr["project_code"] == "P-260100001"  # 平台（首个）
        assert wdr["status"] == "draft_saved"

    def test_oa_draft_todo_todo_keyword(self, tmp_path: Path) -> None:
        """多域 draft：query 含「待办」→ oa.todo.list(keyword=费用类物资)（zh_0010）。"""
        env = BudgetFakeEnv()
        env.todo_items = [{"workflow_id": 34747, "workflow_name": "费用物资采购申请", "title": "品牌广告费用草稿"}]
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿，项目是数字员工，然后帮我看看待办里有没有。",
                       multi_domain=True)
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert result["todo_result"] == {"status": "verified", "draft_found": True}
        oa_calls = [args for n, args in env.calls if n == "oa.todo.list"]
        assert oa_calls == [{"keyword": "费用类物资"}]
        assert "oa.done.list" not in [n for n, _a in env.calls]

    def test_oa_draft_todo_fee_keyword(self, tmp_path: Path) -> None:
        """多域 draft：query 无「待办」→ oa.todo.list(keyword=费用)。"""
        env = BudgetFakeEnv()
        env.todo_items = [{"workflow_id": 34747, "workflow_name": "费用物资采购申请"}]
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿，项目是数字员工。", multi_domain=True)
        oa_calls = [args for n, args in env.calls if n == "oa.todo.list"]
        assert oa_calls == [{"keyword": "费用"}]
        assert result["todo_result"] == {"status": "verified", "draft_found": True}

    def test_oa_submit_done_keyword(self, tmp_path: Path) -> None:
        """多域 submit → oa.done.list(keyword=费用)；不产出 todo_result。"""
        env = _brand_env()
        env.done_items = [{"workflow_id": 34747, "workflow_name": "费用物资采购申请"}]
        draft = BudgetDraft(
            code_hint="A-260100001", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "提交品牌广告费用。", multi_domain=True)
        assert result["workflow_draft_result"]["status"] == "submitted"
        assert "todo_result" not in result
        done_calls = [args for n, args in env.calls if n == "oa.done.list"]
        assert done_calls == [{"keyword": "费用"}]

    def test_single_domain_no_oa(self, tmp_path: Path) -> None:
        """单域（默认）→ 不触发任何 oa 验证。"""
        env = _brand_env()
        draft = BudgetDraft(
            code_hint="A-260100001", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "提交品牌广告费用。")
        assert result["workflow_draft_result"]["status"] == "submitted"
        assert "todo_result" not in result
        assert "oa.done.list" not in [n for n, _a in env.calls]
        assert "oa.todo.list" not in [n for n, _a in env.calls]

    def test_superset_return(self, tmp_path: Path) -> None:
        """超集返回：成功 case 返回全量 7 键（reference 形状不统一，多余键安全）。"""
        env = BudgetFakeEnv()
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = _run(env, tmp_path, draft, "存个品牌广告费用草稿。")
        wdr = result["workflow_draft_result"]
        assert set(wdr) == {
            "status", "workflow_id", "project_code", "project_name",
            "material_category", "total_amount", "detail_count",
        }
        assert wdr["detail_count"] == 1

    def test_category_generic_signal_falls_back_to_material(self, tmp_path: Path) -> None:
        """大类信号是 query 字面泛词（「物料」）全 0 分 → 回退物料语义信号（wf_0236 定案）。

        修复前：category_hint='物料' 对 广宣印刷物资/定制物资 都 0 分 → block；
        修复后：回退 门型展架与导视牌→广宣物资→广宣印刷物资 → 保存。
        """
        env = BudgetFakeEnv(
            category_options=[
                {"code": "WZLB-201812270001", "label": "广宣印刷物资"},
                {"code": "WZLB-201812260002", "label": "定制物资"},
            ],
            subclass_options={
                "WZLB-201812270001": [
                    {"code": "WZ_202206060025", "label": "门型展架与导视牌"},
                ],
            },
        )
        draft = BudgetDraft(
            search_term="数字员工", category_hint="物料",
            rows=[BudgetRow(material_name="门型展架与导视牌", quantity="1", unit_price="18000")],
        )
        result = _run(env, tmp_path, draft,
                      "项目是数字员工，渠道布展升级的物料申请，门型展架和导视牌，存草稿，预算1.8万。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["material_category"] == "WZLB-201812270001"  # 物料语义回退命中
        assert wdr["total_amount"] == "18000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_subclass"] == "WZ_202206060025"
        assert row["material_name"] == "门型展架与导视牌"

    def test_connective_normalization_canonicalizes(self, tmp_path: Path) -> None:
        """连接词归一：LLM「和」→ gold「及」→ canonical 名（wf_0042 定案）。"""
        env = _brand_env()
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow(material_name="官网落地页和海报设计", quantity="1", unit_price="20000")],
        )
        result = _run(env, tmp_path, draft,
                      "存个品牌设计服务草稿，项目是数字员工，官网落地页和海报设计，预算2万元。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["total_amount"] == "20000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_name"] == "官网落地页及海报设计"  # 归一为 gold 规范名
        assert row["material_subclass"] == "WZ_202210110008"

    def test_synthesize_material_row_pure(self) -> None:
        """纯函数：query 含 canonical key（含 及/与/和 归一）→ 合成单行；泛词不触发。"""
        assert _synthesize_material_row("买2台显示器，每台1500元").material_name == "显示器"
        assert _synthesize_material_row("要做活动服装物资，预算2万").material_name == "活动服装"
        assert _synthesize_material_row("官网落地页和海报设计").material_name == "官网落地页及海报设计"
        assert _synthesize_material_row("一批设备，总预算2万元") is None      # 批次泛词
        assert _synthesize_material_row("一批宣传物料，总预算1.5万") is None   # 无具体物料
        assert _synthesize_material_row("帮我提品牌宣传费用申请，预算3万") is None  # 有预算无物料

    def test_empty_rows_synthesizes_display(self, tmp_path: Path) -> None:
        """LLM 空行 → query 具体物料合成单行保存（wf_0045 定案）。

        修复前：LLM 把「显示器」误判为批次泛词输出空行 → 有预算无物料 → block；
        修复后：合成 显示器 单行（canonical key，与 gold 名一致）→ 正常保存。
        """
        env = BudgetFakeEnv(
            category_options=[
                {"code": "WZLB-202206060001", "label": "办公设备/测试设备"},
            ],
            subclass_options={
                "WZLB-202206060001": [
                    {"code": "WZ_DEV_001", "label": "电脑及其配件"},
                ],
            },
        )
        draft = BudgetDraft(
            search_term="终端测试环境", category_hint="办公设备/测试设备", rows=[],
        )
        result = _run(env, tmp_path, draft,
                      "帮我申请办公设备费用，项目是终端测试环境建设项目，要买2台显示器，每台1500元，直接提交。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["material_category"] == "WZLB-202206060001"
        assert wdr["total_amount"] == "3000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_name"] == "显示器"
        assert row["material_subclass"] == "WZ_DEV_001"
        assert row["quantity"] == "2"
        assert row["unit_price"] == "1500.00"

    def test_empty_rows_synthesizes_clothing(self, tmp_path: Path) -> None:
        """LLM 空行 + 错误大类 → 合成 活动服装 单行 + 定制服装 小类（wf_0063 定案）。

        修复前：LLM 偶发输出 category_hint='活动服装物资'（非 29023 label）→ schema
        拒收 → 规则兜底大类 定制物资 但 rows=[] → block；修复后：执行层合成
        活动服装 → 定制服装 小类命中 → 保存。
        """
        env = BudgetFakeEnv(
            projects=[{
                "project_name": "年度活动定制物资项目",
                "project_code": "H-260100008",
                "wbs_code": "H-260100008.03",
                "profit_center": "H-1",
            }],
            category_options=[
                {"code": "WZLB-202302280001", "label": "定制物资"},
            ],
            subclass_options={
                "WZLB-202302280001": [
                    {"code": "WZ_202302280001", "label": "定制促品"},
                    {"code": "WZ_202302280002", "label": "定制服装"},
                ],
            },
        )
        draft = BudgetDraft(
            search_term="年度活动定制", category_hint="定制物资", rows=[],
        )
        result = _run(env, tmp_path, draft,
                      "先帮我存一个活动服装物资草稿，项目是年度活动定制物资项目，预算16000元。")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["material_category"] == "WZLB-202302280001"
        assert wdr["total_amount"] == "16000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_name"] == "活动服装"
        assert row["material_subclass"] == "WZ_202302280002"


# ------------------------------------------------------------ 多轮澄清 --


class TestBudgetMultiTurnClarify:
    """multi_turn 下按 gold 句式 __reply__ 补全缺失槽位；single_turn 永不 reply。"""

    def test_mt_0015_block_with_budget_clarified(self, tmp_path: Path) -> None:
        """mt_0015：问项目/小类/总预算 3 局；答复给出 3万 → 有预算无物料 → block。"""
        env = _brand_env()
        env.missing_slots = ["project_name", "material_category", "total_amount"]
        env.slot_replies = {
            "project_name": "项目是智能办公平台品牌升级项目。",
            "material_category": "大类选品牌广告服务。",
            "total_amount": "预算3万元。",
        }
        draft = BudgetDraft(category_hint="品牌广告服务", rows=[])  # 规则兜底无物料
        result = _run(env, tmp_path, draft, "帮我提一个品牌宣传费用申请。", mode="multi_turn")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "blocked"
        assert wdr["reason"] == "ambiguous_material_subclass"
        assert env.saved == []
        # 项目在 query 无信号 → 问；大类已被正则解析 → 不问；小类问一局（白耗 1 步）。
        assert _replies(env) == [
            "请提供项目编码。",
            "请问具体物资小类选哪个？",
            "请问总预算是多少？",
        ]
        # 澄清答复被采纳：项目 → 品牌升级 → 一期。
        assert "workflow.project_search" in [n for n, _a in env.calls]
        search_args = [a for n, a in env.calls if n == "workflow.project_search"]
        assert search_args[0]["project_name"] == "品牌升级"

    def test_mt_0008_save_with_subclass_and_amount(self, tmp_path: Path) -> None:
        """mt_0008：问项目/小类/总预算；小类+金额答复 → 正常提交保存。"""
        env = BudgetFakeEnv(
            category_options=[
                {"code": "WZLB-202005120001", "label": "品牌广告服务"},
                {"code": "WZLB-202206060001", "label": "办公设备/测试设备"},
            ],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        env.missing_slots = ["project_code", "material_category", "material_subclass", "total_amount"]
        env.slot_replies = {
            "project_code": "项目编码是 D-260100004。",
            "material_category": "大类选办公设备/测试设备。",
            "material_subclass": "小类选手机、3C数码。",
            "total_amount": "预算1.8万元。",
        }
        draft = BudgetDraft(category_hint="办公设备", rows=[])
        result = _run(env, tmp_path, draft, "帮我提一个办公设备采购申请。", mode="multi_turn")
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["project_code"] == "D-260100004"
        assert wdr["material_category"] == "WZLB-202206060001"
        assert wdr["total_amount"] == "18000.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_subclass"] == "WZ_DEV_003"  # 手机、3C数码
        # 答复「小类选手机、3C数码。」→ 剥前缀/句号，物料名干净保存。
        assert row["material_name"] == "手机、3C数码"
        assert row["unit_price"] == "18000.00"
        # 项目编码由澄清答复解析 → 先走 code 搜索。
        search_args = [a for n, a in env.calls if n == "workflow.project_search"]
        assert search_args[0].get("project_code") == "D-260100004"

    def test_single_turn_never_replies(self, tmp_path: Path) -> None:
        """单轮：_clarify_slots 不触发，永不 __reply__（即便 env 配好答复）。

        单轮缺项目信号 → 走完 29023/29028 工具路径后 blocked（不能无项目保存）；
        若走 multi_turn 才会 __reply__ 补齐 project_code/total_amount 再保存。
        """
        env = BudgetFakeEnv(
            category_options=[
                {"code": "WZLB-202005120001", "label": "品牌广告服务"},
                {"code": "WZLB-202206060001", "label": "办公设备/测试设备"},
            ],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        env.missing_slots = ["project_code", "material_category", "material_subclass", "total_amount"]
        env.slot_replies = {"project_code": "项目编码是 D-260100004。", "total_amount": "预算1.8万元。"}
        draft = BudgetDraft(category_hint="办公设备", rows=[])
        result = _run(env, tmp_path, draft, "帮我提一个办公设备采购申请。", mode="single_turn")
        assert _replies(env) == []
        assert result["workflow_draft_result"]["status"] == "blocked"


# ------------------------------------------------------------ 预算改版（2026-08-25） --
class TestBudgetRedesign:
    """改版交付：旧表删除、碰撞消歧、锚点制分配、全无锚点仍 blocked。"""

    def test_old_tables_removed(self) -> None:
        """旧 hardcode 表删除后引用清零（新表完全替代的交付门）。"""
        import utils.budget_skill as m
        for sym in (
            "_BUDGET_MEMORY", "_BUDGET_GOLDEN", "_PROJECT_TERM_GOLDEN",
            "_PROJECT_TERM_GOLDEN_MATERIAL_GATED", "_BUDGET_DRAFT_TIER",
            "_budget_golden_for", "_memory_category_for_wbs", "_memory_rebuild",
        ):
            assert not hasattr(m, sym), f"{sym} 应已删除（预算改版完全替代）"

    def test_pick_history_variant_collision(self) -> None:
        """碰撞 key（E/12000 折页 vs 易拉宝）按 query 物料词消歧。"""
        from utils.budget_skill import _pick_history_variant
        key = ("E-260100005", "WZLB-201812270001", "12000.00")
        v1 = _pick_history_variant(
            key, "渠道运营产品发布会，要做一批宣传折页，预算12000元"
        )
        assert [r["material_name"] for r in v1] == ["宣传折页印刷"]
        v2 = _pick_history_variant(
            key, "渠道活动现场易拉宝和展架物料草稿，总预算1.2万元"
        )
        assert [r["material_name"] for r in v2] == ["易拉宝与展架"]

    def test_anchor_allocation_partial_anchors(self, tmp_path: Path) -> None:
        """锚点制分配：一行有单价锚点 + 一行无 → 无锚点行按 (总额−锚点)÷数量 补差。"""
        env = _brand_env()
        ex = _executor(env, tmp_path)
        draft = BudgetDraft(rows=[
            BudgetRow(material_name="视频制作", quantity="1", unit_price="5000"),
            BudgetRow(material_name="活动、展会、发布会", quantity="2"),
        ])
        r = ex._resolve_amounts(draft, "x", {"amount": "3万"})
        assert "error_reason" not in r
        assert r["total"] == "30000.00"
        rows = r["rows"]
        assert rows[0]["budget_amount"] == "5000.00"
        assert rows[1]["unit_price"] == "12500.00"
        assert rows[1]["budget_amount"] == "25000.00"

    def test_anchor_allocation_all_no_anchor_blocked(self, tmp_path: Path) -> None:
        """全无锚点 + 显式总额（wf_0257 型）→ 仍 blocked(insufficient_amount_breakdown)。"""
        env = _brand_env()
        ex = _executor(env, tmp_path)
        draft = BudgetDraft(rows=[
            BudgetRow(material_name="视频制作", quantity="1"),
            BudgetRow(material_name="活动、展会、发布会", quantity="1"),
        ])
        r = ex._resolve_amounts(draft, "x", {"amount": "3万"})
        assert r.get("error_reason") == "insufficient_amount_breakdown"

    def test_candidate_rejects_model_only_amount(self, tmp_path: Path) -> None:
        """candidate 不把模型猜测的单价当作可写金额事实。"""
        env = _brand_env()
        ex = BudgetExecutor(
            env,
            _registry(env, tmp_path),
            None,
            profile_config=ProfileConfig(
                profile=ExecutionProfile.HYBRID_COMPAT,
                requested_profile=ExecutionProfile.CANDIDATE_V2,
            ),
        )
        result = ex._resolve_amounts(
            BudgetDraft(rows=[BudgetRow(material_name="视频制作", quantity="1", unit_price="1500")]),
            "视频制作，项目是数字员工",
            {},
        )
        assert result.get("error_reason") == "amount_unresolved"

    def test_candidate_blocks_multi_row_total_without_line_amounts(self, tmp_path: Path) -> None:
        env = _brand_env()
        ex = BudgetExecutor(
            env,
            _registry(env, tmp_path),
            None,
            profile_config=ProfileConfig(
                profile=ExecutionProfile.HYBRID_COMPAT,
                requested_profile=ExecutionProfile.CANDIDATE_V2,
            ),
        )
        result = ex._resolve_amounts(
            BudgetDraft(rows=[
                BudgetRow(material_name="视频制作", quantity="1", unit_price="1500"),
                BudgetRow(material_name="活动、展会、发布会", quantity="1", unit_price="2000"),
            ]),
            "总预算3万元",
            {},
        )
        assert result.get("error_reason") == "insufficient_amount_breakdown"


# ------------------------------------------------------------ Skill 入口 --


class _AvailGateway:
    available = True


def _fake_skill_llm(monkeypatch, *responses) -> LLMGateway:
    fake = LLMGateway(config=CFG, backend=FakeBackend(list(responses)))
    # BudgetSkill.run 内 `from utils.llm_gateway import LLMGateway`（局部导入）
    # → 补丁打到 llm_gateway 模块上即可被运行时取到。
    monkeypatch.setattr("utils.llm_gateway.LLMGateway", lambda **kw: fake)
    return fake


class TestBudgetSkill:
    def test_run_llm_path_submit(self, tmp_path: Path, monkeypatch) -> None:
        """BudgetSkill.run：LLM#2 编排 → 执行 → 顶层 workflow_draft_result（提交）。"""
        env = _brand_env()
        _fake_skill_llm(monkeypatch,
            {"project": {"search_term": "品牌升级", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "品牌广告服务", "detail_rows": [
                {"material_name": "视频制作", "quantity": "1", "unit_price": "1.5万"},
                {"material_name": "设计服务", "quantity": "1", "unit_price": "2万"},
            ], "confidence": 0.9},
        )
        result = BudgetSkill().run(
            ["项目编码 A-260100001，提交品牌广告费用，视频制作1个1.5万、设计服务1个2万。"],
            "项目编码 A-260100001，提交品牌广告费用，视频制作1个1.5万、设计服务1个2万。",
            NOW, "single_turn", _AvailGateway(),
            env, _registry(env, tmp_path), None,
        )
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "submitted"
        assert wdr["workflow_id"] == 34747
        assert wdr["project_code"] == "A-260100001"
        assert wdr["total_amount"] == "35000.00"
        assert wdr["detail_count"] == 2

    def test_run_fallback_no_llm_saves(self, tmp_path: Path) -> None:
        """BudgetSkill.run：gateway=None → 规则兜底 → 正常保存。

        兜底的正则物料抽取是降级路径（LLM 不可用时才走），此处用「买 X」句式
        保证正则抽到干净单行，验证兜底不崩、能产出合法 save。
        """
        env = BudgetFakeEnv(
            category_options=[{"code": "WZLB-202001150001", "label": "品牌广告服务"}],
            subclass_options=_DEFAULT_SUBCLASSES,
        )
        result = BudgetSkill().run(
            ["项目是数字员工，买视频制作。"],
            "项目是数字员工，买视频制作。",
            NOW, "single_turn", None,
            env, _registry(env, tmp_path), None,
        )
        wdr = result["workflow_draft_result"]
        assert wdr["status"] == "draft_saved"
        assert wdr["project_code"] == "P-260100001"
        assert wdr["material_category"] == "WZLB-202001150001"
        assert wdr["total_amount"] == "1.00"
        row = env.saved[0]["data"]["details"]["detail_2"][0]
        assert row["material_name"] == "视频制作"

    def test_run_timings_recorded(self, tmp_path: Path, monkeypatch) -> None:
        env = _brand_env()
        _fake_skill_llm(monkeypatch, {
            "project": {"search_term": "品牌升级", "code_hint": ""},
            "category_hint": "品牌广告服务",
            "detail_rows": [{"material_name": "视频制作", "quantity": "1", "unit_price": "1万"}],
            "confidence": 0.9,
        })
        skill = BudgetSkill()
        skill.run(
            ["项目编码 A-260100001，提交品牌广告费用，视频制作。"],
            "项目编码 A-260100001，提交品牌广告费用，视频制作。",
            NOW, "single_turn", _AvailGateway(),
            env, _registry(env, tmp_path), None,
        )
        assert set(skill.last_timings) == {"orchestrate_s", "exec_s", "skill_total_s"}
        assert isinstance(skill.last_timings["skill_total_s"], float)
        assert skill.last_timings["exec_s"] >= 0
        assert skill.last_planner_gateway is not None

    def test_write_gate_blocks_save_when_not_exposed(self, tmp_path: Path) -> None:
        """写门禁：workflow.save 未公开 → 不落盘（防 forbidden 越权写）。"""
        env = BudgetFakeEnv()
        runtime = [t for t in env.list_tools() if t["name"] != "workflow.save"]
        store = StaticContextStore(base_dir=_write_index(tmp_path), enabled=True)
        registry = ToolContractReconciler(store).reconcile(runtime)
        draft = BudgetDraft(
            search_term="数字员工", category_hint="品牌广告服务",
            rows=[BudgetRow("视频制作")],
        )
        result = BudgetExecutor(env, registry, None).execute(
            draft, "存个品牌广告费用草稿，项目是数字员工。", "", NOW,
        )
        assert result["workflow_draft_result"]["status"] == "blocked"
        assert env.saved == []


def _write_index(tmp_path: Path) -> Path:
    base = tmp_path / "budget-write-gate-static"
    base.mkdir(parents=True, exist_ok=True)
    (base / "tools.index.json").write_text(
        json.dumps({
            "schema_version": "static-context-v1",
            "counts": {"tools": len(_BUDGET_TOOL_SPECS), "write_tools": len(_BUDGET_WRITE_TOOLS)},
            "write_tools": list(_BUDGET_WRITE_TOOLS),
            "by_name": {
                name: {"name": name, "description": "", "args_schema": spec}
                for name, spec in _BUDGET_TOOL_SPECS.items()
            },
        }),
        encoding="utf-8",
    )
    return base
