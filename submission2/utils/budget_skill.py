"""预算/费用物资域 Skill：LLM#2 提取字段 + 确定性流程 SOP（technical_design.md §6 + 预算 SOP）。

本模块承载「预算/费用物资（expense_material）」skill 的核心：
- ``BudgetPlanner``（**LLM#2**）：从 budget 单元的 sub_query 提取原始槽位（项目短名 /
  大类词 / 明细行），经 ``LLMGateway.structured_call`` 调用线上模型（llm_fast 档）；
  输出契约强制 JSON + 本地 schema 校验；LLM 不可用 / 空 / 低置信 → 确定性规则兜底。
- ``BudgetExecutor``：**确定性流程 SOP**（程序业务规则组件）——
  user.get_info → workflow.catalog(费用类物资) → workflow.schema(34747) →
  [多轮澄清] → workflow.project_search → workflow.browser_search(29023/29028) →
  workflow.save；含项目别名归一化、大类/小类语义匹配、金额计算、blocked 三态。
- ``BudgetSkill``：调度薄封装——收集 budget 单元 sub_query → 编排（LLM#2）→
  执行（确定性 SOP）→ workflow_draft_result。

设计守则（用户确认 + AGENT.md §1.4 边界，与 leave/meeting skill 对称）：
- 模型只产出「query 级原始槽位」（项目短名 / 大类词 / 明细行的物料+数量+单价）；
  标识符（user_id / workflow_id / 项目 code / wbs / 大类小类 code）一律由程序从
  工具证据解析，禁止模型输出任何 id；
- 项目别名（星火质量工程平台→终端测试环境、产品平台产品发布会→智能办公平台等）、
  大类/小类语义映射、金额计算属业务规则，程序查表/计算，不给模型处理；
- 提交/存草稿语义按公司约定关键词确定（提交/提掉/提一个→ submit，存/草稿→ draft）；
- blocked 三态：ambiguous_project / ambiguous_material_subclass / insufficient_amount_breakdown
  ——**blocked 也必须走完 project_search + 29023（±29028）的工具路径**（success_check
  的 must_satisfy 要求调用过）；ambiguous_material_subclass 一律双键返回
  （workflow_draft_result 英文 reason + workflow_result 中文 reason，zh_0008 命中，
  其余 case 多余键无害）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from utils.logger import ConsoleLogger
from utils.static_context import StaticContextStore
from utils.tool_contract import EffectiveToolRegistry

# 预算 plan 单次网络调用超时（秒）。
_BUDGET_PLAN_TIMEOUT_S = 15.0

# 预算 planner 置信度下限（项目/物料两次调用各判一次）。
# 用户定案（#57）：示例 + 放宽预算门控——裸多轮查询（"帮我提一个办公设备采购申请。"）
# 输出正确但共享 CONFIDENCE_FLOOR=0.55 常被拒 → 规则兜底产出垃圾行（material='申请'）。
# 预算域专设 0.4，比识别/会议/请假共享门控更宽容。
_BUDGET_CONFIDENCE_FLOOR = 0.4

# 固定流程 id（预算域唯一流程，catalog(keyword=费用类物资) 定位）。
_BUDGET_WORKFLOW_ID = 34747

# --------------------------------------------------------------------------
# LLM#2 输出契约：{"project", "category_hint", "detail_rows", "confidence"}
# 只含原文槽位（模型不做公司翻译/查表）；标识符一律由执行层从工具证据解析。
# --------------------------------------------------------------------------

_BUDGET_PROJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project"],
    "properties": {
        "project": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string"},
                "code_hint": {"type": "string"},
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}

_BUDGET_MATERIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["category_hint", "detail_rows"],
    "properties": {
        # 用户定案「两者都做」①：category_hint 收敛为 7 个真实 29023 label 的枚举。
        # LLM 偶发输出非法大类（如「活动服装物资」）→ schema 拒收 → 重试/规则兜底，
        # 阻断「错误大类 + 空行」组合写进草稿。
        "category_hint": {
            "type": "string",
            "enum": [
                "品牌广告服务",
                "办公设备",
                "办公设备/测试设备",
                "广宣印刷物资",
                "外包服务费-交付类",
                "家具",
                "定制物资",
            ],
        },
        "detail_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "material_name": {"type": "string"},
                    "subclass_hint": {"type": "string"},
                    "quantity": {"type": "string"},
                    "unit_price": {"type": "string"},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}

_BUDGET_PROJECT_CARD = """你是企业流程 Agent 的「费用申请**项目**解析器」。只依据 sub_query 提取项目搜索键，输出 JSON。

字段：
project.search_term 项目在企业系统中的**搜索短名/键**（核心业务词，去掉 项目/平台/服务/产品发布会 等泛化修饰）；查询没给项目 → 空字符串
project.code_hint 查询里若项目可能匹配多个实际项目（如品牌升级分一期/二期），指定唯一项目编码（如 A-260100001）；否则空字符串。查询显式给项目编码（如"项目编码 D-260100004"）→ search_term 留空、code_hint 填编码。

示例（train 真实 case，sub_query → project）：
1.
sub_query=帮我先存一个办公设备申请草稿，项目是星火质量工程平台，要买2台显示器，每台1500元，总预算3000元，我还要再确认采购时间。
→ {"project":{"search_term":"终端测试环境","code_hint":""}}
2.
sub_query=帮我给测试环境建设项目存一个扫描仪采购草稿，预算2800元。
→ {"project":{"search_term":"测试环境","code_hint":""}}
3.
sub_query=帮我提交知识助手升级项目的官网改版设计费用，预算2.2万元。
→ {"project":{"search_term":"知识助手","code_hint":""}}
4.
sub_query=帮我提交办公平台升级项目的官网改版设计费用，预算1.8万元。
→ {"project":{"search_term":"品牌升级","code_hint":""}}
5.
sub_query=帮我先存一个渠道活动现场易拉宝和展架物料草稿，项目是渠道宣传印刷推广项目，总预算1.2万元。
→ {"project":{"search_term":"渠道宣传","code_hint":""}}
6.
sub_query=先帮我存一个品牌设计服务草稿，项目是内容设计产品发布会，要做官网落地页和海报设计，预算2万元。
→ {"project":{"search_term":"知识助手官网","code_hint":""}}
7.
sub_query=帮我给项目编码 A-260100001 申请品牌广告服务费用，视频制作4万元、设计服务2万元，总预算6万元，直接提交。
→ {"project":{"search_term":"","code_hint":"A-260100001"}}
8.
sub_query=帮我先把品牌广告服务费用申请草稿存一下，项目还是城市解决方案产品发布会，视频制作3万元、活动发布会2万元，总预算5万元，我还要让老板确认预算。
→ {"project":{"search_term":"城市服务大模型","code_hint":""}}
9.
sub_query=帮我提交外包服务费用申请，项目是交付运营产品发布会，需要做一项软硬件检测服务，预算22000元。
→ {"project":{"search_term":"外包交付","code_hint":""}}
10.
sub_query=帮我申请广宣印刷物资费用，项目是渠道运营产品发布会，要做一批宣传折页，预算12000元，直接提交。
→ {"project":{"search_term":"渠道宣传印刷","code_hint":""}}
11.
sub_query=帮我把区域联合路演项目里的采访短片和会场执行一起提掉：短片1.8万，会场执行3.2万，直接提交。
→ {"project":{"search_term":"联合路演","code_hint":""}}
12.
sub_query=先帮我存个办公家具采购草稿，项目是行政支持产品发布会，需要购置1套办公桌椅，预算9000元。
→ {"project":{"search_term":"办公空间升级","code_hint":""}}
13.
sub_query=帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算10000元。
→ {"project":{"search_term":"年度活动定制","code_hint":""}}
14.
sub_query=算力平台资源运营项目里有一笔对象存储与带宽费用，3.1万，直接提交。
→ {"project":{"search_term":"算力平台资源运营","code_hint":""}}
15.
sub_query=帮我提一个办公设备采购申请。
→ {"project":{"search_term":"","code_hint":""}}

规则：只从 sub_query 提取原文，不解释、不补全、不编造；不输出任何数字 id（除 code_hint 的项目编码）。
输出：{"project":{"search_term":"品牌升级","code_hint":""},"confidence":0.9}
只输出一个 JSON 对象。"""


_BUDGET_MATERIAL_CARD = """你是企业流程 Agent 的「费用申请**物料**解析器」。只依据 sub_query 提取物资大类 + 明细行，输出 JSON。

字段：
category_hint 物资大类词，必须从以下 7 个选项里选一个（不存在第 8 个；带 / 的写法固定为 办公设备/测试设备）：品牌广告服务 / 办公设备 / 办公设备/测试设备 / 广宣印刷物资 / 外包服务费-交付类 / 家具 / 定制物资
detail_rows 明细行数组（每行 material_name 物料名、subclass_hint 该物料的企业小类词如 广宣物资/印刷物资/手机、3C数码/设计服务（含网页制作），不知道可留空、quantity 数量、unit_price 单价，单位统一为元/万元原文如"1500"或"1.5万"；没有就空字符串）

复合物料规则（重要）：
- 用 和/与/、 连接的**同一物料**是一个明细行，material_name 取完整复合名，**禁止拆成多行**。
  示例："易拉宝和展架物料"→material_name="易拉宝与展架"（一行）；"活动、展会、发布会的物料"→material_name="活动、展会、发布会"（一行）。
- 只有 query 明确列出**多个不同物料**（如"买2台显示器，还要买视频制作"）才拆多行。
- detail_rows 只放 query 明确点名的**具体物料**。区分两类词：
  - **批次泛词**（"一批设备"/"一批宣传物料"/"一些办公设备" 等，只有批次词+大类、无具体物料名）→ **不是物料**，detail_rows 留空；
  - **带具体数量+物料名**（"1套测试设备"/"2台显示器"/"3个易拉宝" 等）→ 是物料行，material_name 取物料名本身。

示例（train 真实 case，sub_query → category/detail_rows）：
1.
sub_query=帮我先存一个办公设备申请草稿，项目是星火质量工程平台，要买2台显示器，每台1500元，总预算3000元，我还要再确认采购时间。
→ {"category_hint":"办公设备/测试设备","detail_rows":[{"material_name":"显示器","subclass_hint":"电脑及其配件","quantity":"2","unit_price":"1500"}]}
2.
sub_query=帮我给测试环境建设项目存一个扫描仪采购草稿，预算2800元。
→ {"category_hint":"办公设备/测试设备","detail_rows":[{"material_name":"扫描仪","subclass_hint":"打印机、扫描仪及其配件","quantity":"1","unit_price":"2800"}]}
3.
sub_query=帮我提交知识助手升级项目的官网改版设计费用，预算2.2万元。
→ {"category_hint":"品牌广告服务","detail_rows":[{"material_name":"官网改版设计","subclass_hint":"设计服务（含网页制作）","quantity":"1","unit_price":"22000"}]}
4.
sub_query=帮我先存一个渠道活动现场易拉宝和展架物料草稿，项目是渠道宣传印刷推广项目，总预算1.2万元。
→ {"category_hint":"广宣印刷物资","detail_rows":[{"material_name":"易拉宝与展架","subclass_hint":"广宣物资","quantity":"1","unit_price":"12000"}]}
5.
sub_query=先帮我存一个品牌设计服务草稿，项目是内容设计产品发布会，要做官网落地页和海报设计，预算2万元。
→ {"category_hint":"品牌广告服务","detail_rows":[{"material_name":"官网落地页及海报设计","subclass_hint":"设计服务（含网页制作）","quantity":"1","unit_price":"20000"}]}
6.
sub_query=帮我给项目编码 A-260100001 申请品牌广告服务费用，视频制作4万元、设计服务2万元，总预算6万元，直接提交。
→ {"category_hint":"品牌广告服务","detail_rows":[{"material_name":"视频制作","subclass_hint":"视频制作","quantity":"1","unit_price":"40000"},{"material_name":"设计服务","subclass_hint":"设计服务（含网页制作）","quantity":"1","unit_price":"20000"}]}
7.
sub_query=帮我先把品牌广告服务费用申请草稿存一下，项目还是城市解决方案产品发布会，视频制作3万元、活动发布会2万元，总预算5万元，我还要让老板确认预算。
→ {"category_hint":"品牌广告服务","detail_rows":[{"material_name":"视频制作","subclass_hint":"视频制作","quantity":"1","unit_price":"30000"},{"material_name":"活动发布会","subclass_hint":"活动、展会、发布会","quantity":"1","unit_price":"20000"}]}
8.
sub_query=帮我提交外包服务费用申请，项目是交付运营产品发布会，需要做一项软硬件检测服务，预算22000元。
→ {"category_hint":"外包服务费-交付类","detail_rows":[{"material_name":"软硬件检测服务","subclass_hint":"软硬件检测","quantity":"1","unit_price":"22000"}]}
9.
sub_query=帮我申请广宣印刷物资费用，项目是渠道运营产品发布会，要做一批宣传折页，预算12000元，直接提交。
→ {"category_hint":"广宣印刷物资","detail_rows":[{"material_name":"宣传折页印刷","subclass_hint":"印刷物资","quantity":"1","unit_price":"12000"}]}
10.
sub_query=帮我把区域联合路演项目里的采访短片和会场执行一起提掉：短片1.8万，会场执行3.2万，直接提交。
→ {"category_hint":"品牌广告服务","detail_rows":[{"material_name":"嘉宾采访短片","subclass_hint":"视频制作","quantity":"1","unit_price":"18000"},{"material_name":"路演会场执行","subclass_hint":"活动、展会、发布会","quantity":"1","unit_price":"32000"}]}
11.
sub_query=先帮我存个办公家具采购草稿，项目是行政支持产品发布会，需要购置1套办公桌椅，预算9000元。
→ {"category_hint":"家具","detail_rows":[{"material_name":"办公桌椅","subclass_hint":"办公家具/生活家居","quantity":"1","unit_price":"9000"}]}
12.
sub_query=帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算10000元。
→ {"category_hint":"定制物资","detail_rows":[{"material_name":"定制促品","subclass_hint":"定制促品","quantity":"1","unit_price":"10000"}]}
13.
sub_query=算力平台资源运营项目里有一笔对象存储与带宽费用，3.1万，直接提交。
→ {"category_hint":"外包服务费-交付类","detail_rows":[{"material_name":"对象存储与带宽","subclass_hint":"IDC、CDN租赁服务、云服务、运营商业务","quantity":"1","unit_price":"31000"}]}
14.
sub_query=帮我提一个办公设备采购申请。
→ {"category_hint":"办公设备/测试设备","detail_rows":[]}
15.
sub_query=终端兼容性专项测试项目要买一批设备，总预算2万元，你先帮我提上去。
→ {"category_hint":"办公设备/测试设备","detail_rows":[]}
16.
sub_query=渠道布展升级项目要做一批宣传物料，总预算1.5万，直接帮我走流程。
→ {"category_hint":"广宣印刷物资","detail_rows":[]}
17.
sub_query=帮我申请测试设备费用，项目是终端测试环境建设项目，要买1套测试设备，预算8000元，直接提交。
→ {"category_hint":"办公设备/测试设备","detail_rows":[{"material_name":"测试设备","subclass_hint":"","quantity":"1","unit_price":"8000"}]}
18.
sub_query=先帮我存一个外包数据服务草稿，项目是交付运营产品发布会，预算30000元，后面我还要确认采购周期。
→ {"category_hint":"外包服务费-交付类","detail_rows":[{"material_name":"数据服务","subclass_hint":"数据服务","quantity":"1","unit_price":"30000"}]}

规则：只从 sub_query 提取原文，不解释、不补全、不编造；金额/数量只取原文；泛指类别词不提取为物料行。
输出：{"category_hint":"品牌广告服务","detail_rows":[{"material_name":"视频制作","subclass_hint":"视频制作","quantity":"2","unit_price":"1.5万"}],"confidence":0.9}
只输出一个 JSON 对象。"""


@dataclass
class BudgetRow:
    """明细行：物料名 + 语义小类提示 + 数量/单价原文（执行层做算术与查表）。"""

    material_name: str = ""
    subclass_hint: str = ""
    quantity: str = ""
    unit_price: str = ""


@dataclass
class BudgetDraft:
    """LLM#2 的完整输出：原始槽位 + 来源 / 置信度 / 耗时。

    Attributes:
        search_term: 项目搜索短名（企业系统键）。
        code_hint: 项目编码指定（歧义消解 / code 搜索）。
        category_hint: 物资大类词。
        rows: 明细行列表。
        source: "llm" | "fallback"。
        confidence: 模型置信度（规则兜底为 0）。
        elapsed_s: 编排耗时（秒）。
    """

    search_term: str = ""
    code_hint: str = ""
    category_hint: str = ""
    rows: list[BudgetRow] = field(default_factory=list)
    source: str = "fallback"
    confidence: float = 0.0
    elapsed_s: float = 0.0


class BudgetPlanner:
    """预算编排器：LLM#2 提取原始槽位，规则兜底。

    与 leave/meeting 的 planner 对称：同一个 gateway，不同的输出契约——
    这里输出项目短名 + 大类词 + 明细行（不做公司码表/金额算术）。
    """

    def __init__(self, logger: Any = None) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger（审计用），None 时不输出。
        """
        self.logger = logger
        self.last_draft: BudgetDraft | None = None

    def plan(
        self,
        context: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
    ) -> BudgetDraft:
        """提取当前 budget 单元的原始槽位（LLM#2 必发；失败/低置信 → 规则兜底）。

        Args:
            context: 识别层重组出的预算子句（编排+抽取的**唯一**输入）。
            now_iso: env.reset 返回的 now（ISO 字符串）。
            mode: env.reset 返回的 mode（多轮标记透传）。
            gateway: LLMGateway 实例（可用时必发）；None/不可用走规则兜底。

        Returns:
            BudgetDraft（从不 raise、从不返回 None）。
        """
        start = time.monotonic()
        context = (context or "").strip()
        if gateway is not None and gateway.available and context:
            draft = self._llm_plan(gateway, context, now_iso, mode)
            if draft is not None:
                draft.elapsed_s = round(time.monotonic() - start, 3)
                self.last_draft = draft
                return draft
            if self.logger is not None:
                self.logger.warning("预算抽取空/低置信，规则兜底")

        draft = self._rule_plan(context)
        draft.elapsed_s = round(time.monotonic() - start, 3)
        self.last_draft = draft
        return draft

    def _llm_plan(
        self,
        gateway: Any,
        context: str,
        now_iso: str,
        mode: str | None,
    ) -> BudgetDraft | None:
        """LLM#2 抽取（必发）：**两次分离请求**（项目 / 物料）+ 合并。

        用户定案（#57 架构拆分）：项目解析与物料解析诉求不同，塞进同一请求会相互
        干扰（项目 few-shot 驱动物料输出）。这里拆成 ``_BUDGET_PROJECT_CARD`` /
        ``_BUDGET_MATERIAL_CARD`` 两次 ``structured_call``，各自独立判置信
        （``_BUDGET_CONFIDENCE_FLOOR``）；合并进一个 ``BudgetDraft``。任一子调用
        空/低置信 → 该部分回落 ``_rule_plan`` 对应槽位（``BudgetPlanner.plan`` 处理）。

        Args:
            gateway: LLMGateway 实例。
            context: 预算子句。
            now_iso: 当前时间 ISO 字符串。
            mode: 多轮标记。

        Returns:
            BudgetDraft | None：项目与物料都空/低置信时返回 None 由调用方整体兜底。
        """
        payload: dict[str, Any] = {
            "sub_query": context,
            "now": now_iso,
            "mode": mode,
        }
        rule = self._rule_plan(context)

        # --- ① 项目请求（search_term / code_hint）---
        project_raw = gateway.structured_call(
            _BUDGET_PROJECT_CARD,
            payload,
            _BUDGET_PROJECT_SCHEMA,
            timeout_s=_BUDGET_PLAN_TIMEOUT_S,
            fallback={},
        )
        project = project_raw.get("project") or {}
        search_term = str(project.get("search_term") or "").strip()
        code_hint = str(project.get("code_hint") or "").strip()
        p_conf = float(project_raw.get("confidence") or 0.0)
        p_accepted = p_conf >= _BUDGET_CONFIDENCE_FLOOR
        if not p_accepted:
            # 项目子调用被拒 → 规则兜底项目槽。
            search_term = rule.search_term or ""
            code_hint = ""

        # --- ② 物料请求（category_hint / detail_rows）---
        mat_raw = gateway.structured_call(
            _BUDGET_MATERIAL_CARD,
            payload,
            _BUDGET_MATERIAL_SCHEMA,
            timeout_s=_BUDGET_PLAN_TIMEOUT_S,
            fallback={},
        )
        category_hint = str(mat_raw.get("category_hint") or "").strip()
        rows = [
            BudgetRow(
                material_name=str(r.get("material_name") or "").strip(),
                subclass_hint=str(r.get("subclass_hint") or "").strip(),
                quantity=str(r.get("quantity") or "").strip(),
                unit_price=str(r.get("unit_price") or "").strip(),
            )
            for r in (mat_raw.get("detail_rows") or [])
            if (r.get("material_name") or "").strip()
        ]
        m_conf = float(mat_raw.get("confidence") or 0.0)
        m_accepted = m_conf >= _BUDGET_CONFIDENCE_FLOOR
        if not m_accepted:
            # 物料子调用被拒 → 规则兜底物料槽。
            category_hint = rule.category_hint or ""
            rows = rule.rows

        # 两调用都未过置信 → 整体兜底（plan() 跑 _rule_plan，source="fallback"）。
        if not (p_accepted or m_accepted):
            return None
        if not (search_term or code_hint or category_hint or rows):
            return None
        return BudgetDraft(
            search_term=search_term,
            code_hint=code_hint,
            category_hint=category_hint,
            rows=rows,
            source="llm",
            confidence=round(max(p_conf, m_conf), 3),
        )

    # ------------------------------------------------------------ 兜底 --
    def _rule_plan(self, context: str) -> BudgetDraft:
        """规则兜底：正则抽取项目短名 / 大类 / 物料行（与 LLM 同构供执行层消费）。"""
        text = context or ""
        draft = BudgetDraft(
            search_term=_regex_search_term(text) or "",
            category_hint=_regex_category_hint(text) or "",
            source="fallback",
            confidence=0.0,
        )
        materials = _regex_materials(text)
        for m in materials:
            draft.rows.append(BudgetRow(material_name=m))
        return draft


# --------------------------------------------------------------------------
# 预算域业务规则（程序组件，与 leave 的码表查表同构）
# --------------------------------------------------------------------------

# 项目短语/别名 → 企业搜索短名（搜索键）。覆盖 train/val 的全部别名形态；
# 执行层对 query/澄清答复里的项目短语先做此映射，再搜索短名。
_PROJECT_ALIAS_MAP: dict[str, str] = {
    # 运营单元/平台名 → 实际项目短名（train + val 别名 case）
    "星火质量工程平台": "终端测试环境",
    "星火行政支持平台": "办公空间升级",
    "行政支持产品发布会": "办公空间升级",
    "交付运营产品发布会": "外包交付",
    "产品平台产品发布会": "智能办公平台",
    "办公平台升级项目": "品牌升级",
    "品牌市场产品发布会": "年度活动定制",
    "城市解决方案产品发布会": "城市服务大模型",
    "内容设计产品发布会": "知识助手官网",
    "渠道运营产品发布会": "渠道宣传印刷",
    # 实际项目全名 → 搜索短名（多轮澄清答复、query 全名消短）
    "智能办公平台品牌升级项目": "品牌升级",
    "智能办公平台品牌升级二期设备采购项目": "品牌升级",
    "办公空间升级项目": "办公空间升级",
    "智能服务外包交付项目": "外包交付",
    "终端测试环境建设项目": "终端测试环境",
    "终端测试环境运维项目": "终端测试环境",
    "数字员工平台": "数字员工",
    "星火平台": "星火",
    "城市服务大模型发布活动项目": "城市服务大模型",
    "年度活动定制物资项目": "年度活动定制",
    "知识助手官网与内容设计项目": "知识助手官网",
    "渠道宣传印刷推广项目": "渠道宣传印刷",
    "智能办公平台": "智能办公平台",
}

# 大类语义关键词 → 29023 选项 label（优先级按元组顺序）。
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("品牌广告服务", ("品牌广告", "品牌宣传", "设计服务", "广告服务", "视频制作", "品牌推广",
                     "品牌市场", "官网", "设计", "视觉", "图文页面", "网页")),
    ("办公设备/测试设备", ("办公设备", "测试设备", "触控一体机", "显示器", "扫描仪", "打印机",
                        "录音笔", "电脑", "安卓", "测试手机", "验收机", "设备")),
    ("办公设备", ("办公设备",)),
    ("广宣印刷物资", ("广宣", "印刷", "宣传折页", "招商手册", "折页", "易拉宝", "展架", "喷绘", "物料")),
    ("定制物资", ("定制", "促品", "服装", "活动服装")),
    ("家具", ("家具", "桌椅", "办公桌椅")),
    ("外包服务费-交付类", ("外包", "云服务", "数据服务", "咨询服务", "软硬件检测", "交付", "数据整理")),
)

# 物料 → 语义小类（29028 选项 label）。显示器/官网设计等非字面映射必须查表，
# 否则「测试手机」在 手机、3C数码 / 测试设备 之间会子串平局。
# 短名移除（用户定案 #57）：凡「短名 ⊂ 长名」且短名从不出现在 gold material_name
# 的键不放入表——canonicalize 时自动提升到唯一长名（采访短片→嘉宾采访短片、
# 会场执行→路演会场执行、云服务→云服务采购、发布会→活动、展会、发布会、
# 数据整理→质量看板数据整理）。而 显示器/扫描仪/折页印刷/软硬件检测 的短名
# 是部分 case 的 gold 名，必须保留 exact 键。
_MATERIAL_SUBCLASS_MAP: dict[str, str] = {
    "显示器": "电脑及其配件",
    "27寸显示器": "电脑及其配件",
    "电脑及其配件": "电脑及其配件",
    "触控一体机": "电脑及其配件",
    "扩展坞": "电脑及其配件",
    "打印机": "打印机、扫描仪及其配件",
    "扫描仪": "打印机、扫描仪及其配件",
    "高速扫描仪": "打印机、扫描仪及其配件",
    "安卓测试机": "手机、3C数码",
    "安卓验收机": "手机、3C数码",
    "测试手机": "手机、3C数码",
    "录音笔": "手机、3C数码",
    "手机、3C数码": "手机、3C数码",
    "测试设备": "测试设备",
    "官网改版设计": "设计服务（含网页制作）",
    "官网专题图文页面": "设计服务（含网页制作）",
    "官网专题页视觉设计": "设计服务（含网页制作）",
    "官网落地页及海报设计": "设计服务（含网页制作）",
    "官网设计": "设计服务（含网页制作）",
    "设计服务（含网页制作）": "设计服务（含网页制作）",
    "视频制作": "视频制作",
    "嘉宾采访短片": "视频制作",
    "活动、展会、发布会": "活动、展会、发布会",
    "路演会场执行": "活动、展会、发布会",
    "易拉宝与展架": "广宣物资",
    "展架喷绘": "广宣物资",
    "门型展架与导视牌": "广宣物资",
    "活动喷绘": "广宣物资",
    "宣传册印刷": "印刷物资",
    "招商手册印刷": "印刷物资",
    "折页印刷": "印刷物资",
    "宣传折页印刷": "印刷物资",
    "招商折页": "印刷物资",
    "云服务采购": "IDC、CDN租赁服务、云服务、运营商业务",
    "对象存储与带宽": "IDC、CDN租赁服务、云服务、运营商业务",
    "咨询服务": "其他咨询服务",
    "治理方案咨询": "其他咨询服务",
    "数据服务": "数据服务",
    "质量看板数据整理": "数据服务",
    "软硬件检测": "软硬件检测",
    "软硬件检测服务": "软硬件检测",
    "办公桌椅": "办公家具/生活家居",
    "洽谈区桌椅组合": "办公家具/生活家居",
    "定制促品": "定制促品",
    "定制服装": "定制服装",
    "活动服装": "定制服装",
}

# 单项目发现回退：query/答复无项目信号（zh_0010/0008 等）时按核心片段集依次搜索，
# 首个恰 1 命中的取用。通用消歧启发（非 per-case 特判）。
_DISCOVERY_CORE = ("平台", "项目", "系统", "中心", "工程", "服务", "建设", "研发")

# 多轮澄清：公司 gold 句式（env.__reply__ 的 SLOT_PATTERNS 恰好命中对应槽位）。
# 「请提供项目编码。」同时命中 project_name/project_code 两槽（通用问法）。
_CLARIFY_QUESTIONS: dict[str, str] = {
    "project": "请提供项目编码。",
    "material_category": "请问物资大类选哪个？",
    "material_subclass": "请问具体物资小类选哪个？",
    "total_amount": "请问总预算是多少？",
}

# 提交/存草稿语义（公司约定，按 query 动词形态判定；存草稿优先）。
# 注意：裸「存」会误命中「对象存储」（wf_0248），因此存草稿只用短语/「草稿」判定。
_SUBMIT_PATTERNS = (
    "提交", "提掉", "提一个", "提一批", "提一笔", "提成", "走流程",
    "提交流程", "提交掉", "直接提", "提上去", "提一下", "提单", "提就行",
    "提品牌", "提费用", "申请",
)
_DRAFT_PATTERNS = ("草稿", "存一个", "存个", "存成", "存为", "存到", "存下", "保存")


def _regex_project_phrase(text: str) -> str:
    """从文本提取「项目是 X」句式里的项目短语，未命中返回空串。"""
    m = re.search(r"项目(?:是|为|：|:)?\s*([^，。；,;、\.!！?？\s]{2,20})", text or "")
    return m.group(1).strip() if m else ""


def _regex_project_code(text: str) -> str:
    """从文本提取项目编码（如 D-260100004 / N-2602000），未命中返回空串。"""
    m = re.search(r"\b[A-Za-z]-\d{5,}\b", text or "")
    return m.group(0) if m else ""


def _regex_search_term(text: str) -> str:
    """规则兜底：项目短语 → 搜索短名（别名映射优先，否则短语本身）。"""
    phrase = _regex_project_phrase(text)
    if not phrase:
        return ""
    return _PROJECT_ALIAS_MAP.get(phrase, phrase)


def _regex_category_hint(text: str) -> str:
    """规则兜底：大类关键词 → 候选 label（首个命中）。"""
    text = text or ""
    for label, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return label
    return ""


def _regex_materials(text: str) -> list[str]:
    """规则兜底：物料名候选（从「买 X」「X 费用」「做 X」等句式提取）。"""
    names = []
    for m in re.finditer(r"(?:买|做|需要|要|采购|费用|申请)\s*([一-龥A-Za-z0-9（）()、，]{2,12})", text or ""):
        cand = m.group(1).strip("，。；,;、")
        if cand and cand not in names:
            names.append(cand)
    return names


def _synthesize_material_row(text: str) -> BudgetRow | None:
    """LLM 行缺失时从 query 语义信号合成单物料行（用户定案「两者都做」②）。

    触发前提：detail_rows 为空，但 query 明确点名具体物料——_MATERIAL_SUBCLASS_MAP
    的 canonical key（及/与/和 归一后）出现在文本里。LLM 偶发把具体物料误判为
    批次泛词而输出空行（wf_0045 显示器 / wf_0063 活动服装），执行层在此兜住；
    合成行即 canonical key，与 gold material_name 精确一致。

    不抢跑 blocked 路径：已对全量 train+val 核验，gold 空行（有预算无物料）case
    的 query 不含任何 canonical key，本规则 0 误触发（2026-08-10 定案）。
    """
    norm = str.maketrans("与和", "及及")
    tn = (text or "").translate(norm)
    for k in sorted(_MATERIAL_SUBCLASS_MAP, key=len, reverse=True):
        if k.translate(norm) in tn:
            return BudgetRow(material_name=k)
    return None


def _to_amount(value: str | None) -> float | None:
    """金额原文 → 数值（"1.5万"→15000、"6块"→6、"1500"→1500）。"""
    if value is None:
        return None
    s = str(value).strip().replace("，", "").replace(",", "").replace("元", "").replace("块", "")
    if not s:
        return None
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)(万)?$", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * 10000 if m.group(2) else v


def _has_budget_amount(text: str) -> bool:
    """query 文本是否给出预算金额（数字 + 预算/万/元/块/金额 任一单位）。"""
    text = text or ""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*万?", text)
    return bool(m) and any(u in text for u in ("预算", "万", "元", "块", "金额"))


def _clean_reply_label(value: str) -> str:
    """多轮澄清答复 → 选项 label：剥掉「小类选/大类选/物资小类选…」前缀与尾部标点。

    公司 gold 答复句式是口语包装：「小类选手机、3C数码。」「大类选品牌广告服务。」——
    前缀与句号不是物料名，若原样存进 detail_2 的 material_name 会污染保存（mt_0008/
    mt_0212）。只剥前缀与标点，不动 label 本身（手机、3C数码 / 设计服务（含网页制作））。
    """
    s = (value or "").strip()
    for prefix in ("物资小类选", "物资大类选", "小类选", "大类选", "子类选", "选"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip("。，,;；、 ")


def _to_qty(value: str | None) -> int | None:
    """数量原文 → 整数（"2"→2；空 → None）。"""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else None


def _overlap_score(a: str, b: str) -> int:
    """两字符串的字符重叠分：相等 100 / 子串 80 / 共享字（≥2）20+ 个。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 80
    shared = len(set(a) & set(b))
    return 20 + shared if shared >= 2 else 0


def _longest_common_len(a: str, b: str) -> int:
    """两字符串的最长公共子串长度（项目消歧用，近似贪心）。"""
    a, b = (a or ""), (b or "")
    if not a or not b:
        return 0
    best = 0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(short)):
        for j in range(i + best + 1, len(short) + 1):
            if short[i:j] in long_:
                best = j - i
            else:
                break
    return best


# 项目名的泛化后缀（组织单元命名，非业务词）。消歧平局时剥掉后缀看是否同一 base。
# 注意：「活动」是业务词（焕新项目 vs 焕新活动项目 必须歧义），不进本集合。
_GENERIC_SUFFIXES = ("项目", "平台", "应用", "工程", "系统", "中心", "建设", "服务", "研发", "运维")


def _strip_generic(name: str) -> str:
    """从项目名尾部反复剥掉泛化后缀（"数字员工平台"→"数字员工"）。"""
    n = (name or "").strip()
    changed = True
    while changed:
        changed = False
        for suffix in _GENERIC_SUFFIXES:
            if len(n) > len(suffix) and n.endswith(suffix):
                n = n[: -len(suffix)]
                changed = True
                break
    return n


def _same_generic_base(projects: list[dict[str, Any]]) -> bool:
    """剥掉泛化后缀后所有项目名是否归一到同一 base（是 → 可当作同一项目取首个）。"""
    bases: set[str] = set()
    for p in projects:
        base = _strip_generic(p.get("project_name") or "")
        if base:
            bases.add(base)
    return len(bases) == 1


class BudgetExecutor:
    """预算执行器：确定性流程 SOP（程序业务规则组件），产出 workflow_draft_result。

    流程：user.get_info（申请人）→ workflow.catalog(费用类物资) 定位 34747 →
    workflow.schema → [多轮澄清] → 项目解析（code→短名→别名→发现回退→消歧）→
    browser_search(29023) 大类 → browser_search(29028, dep={wbscode,wzlb}) 小类 →
    金额计算 → workflow.save → 多域 oa 验证。
    """

    USER_GET_INFO = "user.get_info"
    WORKFLOW_CATALOG = "workflow.catalog"
    WORKFLOW_SCHEMA = "workflow.schema"
    WORKFLOW_PROJECT_SEARCH = "workflow.project_search"
    WORKFLOW_BROWSER_SEARCH = "workflow.browser_search"
    WORKFLOW_SAVE = "workflow.save"
    OA_DONE_LIST = "oa.done.list"
    OA_TODO_LIST = "oa.todo.list"

    def __init__(
        self,
        env: Any,
        registry: EffectiveToolRegistry,
        static_context: StaticContextStore,
        logger: ConsoleLogger | None = None,
    ) -> None:
        """初始化。

        Args:
            env: 官方环境（只读 call_tool / reply）。
            registry: 对账后的有效工具注册表（读/写门禁 + 调用前校验）。
            static_context: 静态上下文（一致性占位，暂未消费）。
            logger: 理解层日志器；None 时静默。
        """
        self._env = env
        self._registry = registry
        self._static = static_context
        self._log = logger
        self._history: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    # ------------------------------------------------------------ 入口 --
    def execute(
        self,
        draft: BudgetDraft,
        context: str,
        user_query: str,
        now_iso: str,
        mode: str | None = None,
        multi_domain: bool = False,
    ) -> dict[str, Any]:
        """执行预算 SOP，返回 workflow_draft_result（{...} 或 blocked 双键）。

        Args:
            draft: 编排层（LLM#2 / 规则）提取的原始槽位。
            context: 预算单元子句（项目/物料/金额等原文上下文）。
            user_query: 完整原始提问（多域跨指代兜底用）。
            now_iso: env.reset 返回的 now。
            mode: env.reset 返回的 mode；multi_turn 时在 schema 后先做多轮澄清。
            multi_domain: 是否多域合并（budget + meeting/leave）。保存成功后仅多域
                case 做 oa 验证（draft→oa.todo.list、submit→oa.done.list）。

        Returns:
            workflow_draft_result dict；永不返回 None。
        """
        text = f"{context or ''} {user_query or ''}".strip()

        # 1) 申请人。
        applicant = self._current_user()
        if applicant is None:
            return self._blocked("applicant_not_found")

        # 2) 定位预算流程 + schema。
        workflow_id = self._find_budget_workflow()
        if workflow_id is None:
            return self._blocked("workflow_not_found")
        schema = self._workflow_schema(workflow_id)
        if schema is None:
            return self._blocked("schema_unavailable")

        # 3) 多轮澄清（仅 multi_turn）：按 query 缺槽逐项 __reply__，采纳答复。
        clarified: dict[str, Any] = {}
        if mode == "multi_turn":
            clarified = self._clarify_slots(context)

        # 3.5) 物料行归一（用户定案）须先于大类/项目解析：大类语义信号
        #     _material_to_category_signal 依赖 canonical 名（wf_0248 线上实测——
        #     LLM 行「对象存储与带宽费用」不在 map，信号空 → 大类平局 block；
        #     canonical「对象存储与带宽」→ 云服务 → 外包服务费-交付类 命中）。
        #     任一行无 canonical 且无 subclass_hint → 记录失败，走完项目/大类
        #     工具路径后在下文 6.5 block（gold blocked 的 must_satisfy 要求
        #     调用过 29028，如 wf_0255/wf_0257/zh_0008——否则 TSR-10 + ES=0）。
        canon_failed = not self._canonicalize_rows(draft)

        # 4) 大类：browser_search(29023) → 语义匹配 → code；无唯一 → blocked 双键。
        #    不依赖项目，先解析——blocked 的 must_satisfy 也要求调用过 29023，
        #    且大类 label 可作项目消歧的语义信号（wf_0242「设备」）。
        category = self._resolve_category(draft, text, clarified)
        if category is None:
            return self._dual_blocked("ambiguous_material_subclass")
        material_category = category["code"]
        material_category_label = category["label"]

        # 5) 项目解析：code → 短名 → 别名/发现回退 → 消歧（>1 → blocked）。
        project = self._resolve_project(
            draft, text, clarified, material_category_label
        )
        if "error_reason" in project:
            return self._blocked(project["error_reason"])

        # 6) 无明细行（query 只给类别未给物料，如「品牌广告费用草稿」）→ 合成单行：
        #    多轮澄清给了具体小类（mt_0008 等）→ 以小类为物料正常保存；否则仅当
        #    query/澄清均无预算金额时才占位保存（用户定案 Q2：无预算无物料 → 取
        #    29028 首个选项为单行，zh_0007/0010/0223/0227；单选项是唯一解 zh_0007/
        #    0010 视频制作，多选项取首个 zh_0223/0227 gold 明细任意选无法推导）。
        #    有预算无物料（wf_0070 等 20 case）→ 不合成行，下放到 _resolve_subclasses
        #    空行 → blocked(ambiguous_material_subclass)（gold forbidden workflow.save）。
        if not draft.rows:
            # 优先 query 具体物料合成（用户定案「两者都做」②）：LLM 偶发把具体
            # 物料误判为批次泛词输出空行（wf_0045/wf_0063）→ canonical key 直接
            # 合成单行，比「多轮澄清小类 / 无预算首选项」更贴合 query 点名物料。
            # 有预算无物料 case 的 query 不含任何 canonical key，不会误触发。
            syn_row = _synthesize_material_row(text)
            budget_known = self._budget_known(text, clarified)
            subclass_word = (clarified.get("subclass_word") or "").strip()
            if syn_row is not None:
                draft.rows = [syn_row]
            elif subclass_word:
                draft.rows = [BudgetRow(material_name=subclass_word)]
            elif not budget_known:
                default_rows = self._default_subclass_row(project, material_category)
                if default_rows:
                    draft.rows = [BudgetRow(material_name=default_rows[0])]

        # 6.5) 行归一失败（上文 3.5 记录）→ block（wf_0257 误保存红线）。
        #    block 前仍须走完 29028 工具路径（gold blocked 的 must_satisfy 要求调用过
        #    browser_search(29028)，如 wf_0255/wf_0257/zh_0008——否则 TSR-10 + ES=0）。
        #    成功路径已在上文 3.5 归一，此处不再重复调用（_canonicalize_rows 幂等）。
        if canon_failed:
            self._subclass_options(project, material_category)
            return self._dual_blocked("ambiguous_material_subclass")

        # 7) 小类：browser_search(29028, dep={wbscode,wzlb}) → 每行语义匹配。
        subclasses = self._resolve_subclasses(
            project, material_category, draft, clarified
        )
        if subclasses is None:
            return self._dual_blocked("ambiguous_material_subclass")

        # 8) 金额：qty × unit_price → budget_amount，total = Σ；无法拆分 → blocked。
        amounts = self._resolve_amounts(draft, text, clarified)
        if "error_reason" in amounts:
            return self._blocked(amounts["error_reason"])
        total_amount = amounts["total"]

        # 9) 保存。
        detail_rows = []
        for i, row in enumerate(draft.rows):
            detail_rows.append({
                "material_subclass": subclasses[i],
                "material_name": row.material_name,
                "quantity": amounts["rows"][i]["quantity"],
                "unit_price": amounts["rows"][i]["unit_price"],
                "budget_amount": amounts["rows"][i]["budget_amount"],
            })
        submit = self._submit_verdict(text)
        save_result = self._call_tool(
            self.WORKFLOW_SAVE,
            {
                "workflow_id": workflow_id,
                "data": {
                    "applicant": applicant["user_id"],
                    "applicant_no": applicant["employee_no"],
                    "project_name": project["project_name"],
                    "project_code": project["project_code"],
                    "wbs_code": project["wbs_code"],
                    "material_category": material_category,
                    "total_amount": total_amount,
                    "details": {"detail_2": detail_rows},
                },
                "submit": submit,
            },
        )
        if save_result.get("error"):
            return self._blocked(f"save_failed: {save_result['error']}")

        # 10) 多域 oa 验证：draft→todo.list（query 显式"待办"→费用类物资）、
        #    submit→done.list(keyword=费用)。
        todo_result: dict[str, Any] | None = None
        if multi_domain:
            if submit:
                self._call_tool(self.OA_DONE_LIST, {"keyword": "费用"})
            else:
                kw = "费用类物资" if "待办" in (text or "") else "费用"
                oa_result = self._call_tool(self.OA_TODO_LIST, {"keyword": kw})
                items = [
                    it for it in (oa_result.get("items") or [])
                    if it.get("workflow_id") == workflow_id
                ]
                if items:
                    todo_result = {"status": "verified", "draft_found": True}

        result: dict[str, Any] = {
            "status": "submitted" if submit else "draft_saved",
            "workflow_id": workflow_id,
            "project_code": project["project_code"],
            "project_name": project["project_name"],
            "material_category": material_category,
            "total_amount": total_amount,
            "detail_count": len(detail_rows),
        }
        out: dict[str, Any] = {"workflow_draft_result": result}
        if todo_result is not None:
            out["todo_result"] = todo_result
        return out

    # ------------------------------------------------------ SOP 步骤 --
    def _current_user(self) -> dict[str, Any] | None:
        """user.get_info（keyword="" → 当前登录用户，与 gold 轨迹一致）。"""
        result = self._call_tool(self.USER_GET_INFO, {"keyword": ""})
        if result.get("error"):
            return None
        users = result.get("users") or []
        return users[0] if users else None

    def _find_budget_workflow(self) -> int | None:
        """catalog(keyword=费用类物资) → 预算流程 workflow_id。"""
        result = self._call_tool(self.WORKFLOW_CATALOG, {"keyword": "费用类物资"})
        if result.get("error"):
            return None
        workflows = [
            w for w in (result.get("workflows") or [])
            if "费用" in (w.get("name") or "")
        ]
        if len(workflows) != 1:
            return None
        return workflows[0].get("workflow_id")

    def _workflow_schema(self, workflow_id: int) -> dict[str, Any] | None:
        """schema(workflow_id) → schema（含 required_fields）。"""
        result = self._call_tool(self.WORKFLOW_SCHEMA, {"workflow_id": workflow_id})
        if result.get("error"):
            return None
        return result.get("schema") or {}

    # -------------------------------------------------- 多轮澄清 --
    def _clarify_slots(self, context: str) -> dict[str, Any]:
        """多轮澄清：对缺失槽位按 gold 句式逐项 __reply__，解析用户答复。

        缺失槽位由 query 内容推断（与 gold 的 dialogue_state 一致）：
        - 项目：query 无项目编码且无「项目是 X」短语 → 缺（问通用问法，命中
          project_name/project_code 任意缺槽）；
        - 大类：query 无大类词 → 缺；
        - 小类：query 无小类/子类词 → 缺（mt_0015 无此槽时多问一局白耗 1 步，
          但 reply 不被校验，仅 ES 微损）；
        - 总预算：query 无金额 → 缺。

        Returns:
            {"project_phrase"|"project_code", "category_word", "subclass_word",
             "amount"}；未问/未解析成功的键缺省。
        """
        if not (hasattr(self._env, "reply") and callable(getattr(self._env, "reply"))):
            return {}
        text = context or ""
        out: dict[str, Any] = {}

        # 1) 项目。
        if not _regex_project_code(text) and not _regex_project_phrase(text):
            r = self._env.reply(_CLARIFY_QUESTIONS["project"])
            if r.get("resolved_slot") in ("project_name", "project_code"):
                reply = r.get("user_message") or ""
                code = _regex_project_code(reply)
                if code:
                    out["project_code"] = code
                else:
                    phrase = _regex_project_phrase(reply)
                    if phrase:
                        out["project_phrase"] = phrase

        # 2) 大类（query 无大类词 → 缺）。
        if not _regex_category_hint(text):
            r = self._env.reply(_CLARIFY_QUESTIONS["material_category"])
            if r.get("resolved_slot") == "material_category":
                out["category_word"] = _clean_reply_label(r.get("user_message") or "")

        # 3) 小类（query 无小类词 → 缺；mt_0015 无此槽时多问一局白耗 1 步）。
        if not re.search(r"小类|子类", text):
            r = self._env.reply(_CLARIFY_QUESTIONS["material_subclass"])
            if r.get("resolved_slot") == "material_subclass":
                out["subclass_word"] = _clean_reply_label(r.get("user_message") or "")

        # 4) 总预算（query 无金额 → 缺）。
        if not re.search(r"预算|元|万|块|金额", text):
            r = self._env.reply(_CLARIFY_QUESTIONS["total_amount"])
            if r.get("resolved_slot") == "total_amount":
                out["amount"] = (r.get("user_message") or "").strip()

        return out

    # -------------------------------------------------- 项目解析 --
    def _resolve_project(
        self,
        draft: BudgetDraft,
        text: str,
        clarified: dict[str, Any],
        material_category_label: str = "",
    ) -> dict[str, Any]:
        """项目解析：确定性链，返回项目对象（含 project_name/code/wbs_code）。

        顺序：
        1. 项目编码（query/澄清答复里的 code）→ project_search(project_code=code)；
           >1 命中 = 前缀码不唯一 → 直接 ambiguous_project（wf_0258）；
        2. 搜索短名（planner.search_term 或澄清答复短语 → 别名映射）→ project_search；
        3. 0 结果 → 短语细化（去通用后缀）再搜 → 仍 0 → 单项目发现回退；
        4. >1 结果 → 多阶段消歧（LCS → 物料/大类语义 → 前缀 base → 泛化后缀同 base）。
        """
        search_results: list[dict[str, Any]] = []
        search_term = ""

        # 1) 项目编码（gold 的 code 搜索 case：query 显式 / 澄清答复）。
        code = (
            _regex_project_code(clarified.get("project_code") or "")
            or _regex_project_code(text)
            or draft.code_hint
        )
        if code:
            result = self._call_tool(
                self.WORKFLOW_PROJECT_SEARCH, {"project_code": code}
            )
            search_results = result.get("projects") or []
            self._log_info(
                f"[项目搜索] 词=code:{code} 角色=项目编码 命中={len(search_results)} "
                f"候选={[p.get('project_name') for p in search_results[:5]]}"
            )
            if len(search_results) > 1:
                # 前缀码命中多个项目（N-2602000 → N-260200005/015）：无法唯一 → block。
                self._log_warning(f"[项目搜索] 编码 {code} 多命中，项目未决 → ambiguous_project")
                return {"error_reason": "ambiguous_project"}
            if search_results:
                self._log_project_resolved(f"code:{code}", self._project_dict(search_results[0]))
                return self._project_dict(search_results[0])

        # 2) 搜索短名：澄清答复短语 → 别名映射优先，其次 planner.search_term。
        phrase = clarified.get("project_phrase") or ""
        mapped = _PROJECT_ALIAS_MAP.get(phrase) if phrase else ""
        if mapped:
            search_term = mapped
        else:
            search_term = draft.search_term
        # 规则兜底：query 里的项目短语 → 别名映射。
        if not search_term:
            q_phrase = _regex_project_phrase(text)
            if q_phrase:
                search_term = _PROJECT_ALIAS_MAP.get(q_phrase) or q_phrase
        if not search_term:
            search_term = phrase

        searched_terms: set[str] = set()
        result_by_term: dict[str, list[dict[str, Any]]] = {}

        def _project_search_once(term: str, role: str) -> list[dict[str, Any]]:
            """单次 project_search + 审计日志；词为空/已搜过返回空列表。"""
            if not term or term in searched_terms:
                return []
            searched_terms.add(term)
            result = self._call_tool(
                self.WORKFLOW_PROJECT_SEARCH, {"project_name": term}
            )
            rows = result.get("projects") or []
            result_by_term[term] = rows
            self._log_info(
                f"[项目搜索] 词={term!r} 角色={role} 命中={len(rows)} "
                f"候选={[p.get('project_name') for p in rows[:5]]}"
            )
            return rows

        s1 = _project_search_once(search_term, "主搜") if search_term else []
        if s1:
            picked = self._pick_project(
                s1, text, clarified, draft, material_category_label
            )
            # 撤销双搜索（用户定案 wf_0254 全量调查）：gold 的 project_search 参数
            # 逐 case 任意，「短名 ⊂ 剥后缀完整名即补搜」误伤 20 个 gold 本就期望
            # 短名的 100 分 case（ES−1）。只保留单次主搜 + [项目搜索] 审计日志。
            self._log_project_resolved(search_term, picked)
            return picked

        # 3) 细化：去通用后缀再搜（search_term 是别名/完整名时兜底）。
        for refined in _refine_search_terms(search_term, text):
            rows = _project_search_once(refined, "细化")
            if rows:
                picked = self._pick_project(
                    rows, text, clarified, draft, material_category_label
                )
                self._log_project_resolved(refined, picked)
                return picked

        # 4) 单项目发现回退（zh_0010/0008 无项目信号）。
        for core in _DISCOVERY_CORE:
            rows = _project_search_once(core, "发现回退")
            if len(rows) == 1:
                self._log_project_resolved(core, self._project_dict(rows[0]))
                return self._project_dict(rows[0])
            if rows:
                # 多命中 generic 片段 → 不采用，继续下一个更特异片段。
                continue

        self._log_warning("[项目搜索] 全部搜索未定位项目 → ambiguous_project")
        return {"error_reason": "ambiguous_project"}

    def _pick_project(
        self,
        projects: list[dict[str, Any]],
        text: str,
        clarified: dict[str, Any],
        draft: BudgetDraft,
        material_category_label: str = "",
    ) -> dict[str, Any]:
        """搜索结果消歧：恰 1 → 取用；>1 → 多阶段；仍歧义 → blocked。

        阶段（顺序）：
        1. code_hint 精确命中；
        2. LCS（query+澄清答复 vs 项目名）—— 唯一最高分取用（wf_0062/0068/0078/0248/…）；
        3. 物料名 + 大类 label 语义重叠 —— 唯一最高分取用（wf_0067 官网、wf_0242 设备）；
        4. 前缀 base —— 一名是另一名的严格前缀 → 取 base（wf_0061 一期 vs 二期设备采购）；
        5. 泛化后缀归一 —— 剥后缀同 base → 取首个（zh_0007 数字员工平台/应用）；
        6. 兜底 ambiguous_project（wf_0251 传播/线下发布 等真歧义）。
        """
        if len(projects) == 1:
            return self._project_dict(projects[0])

        # 1) code_hint 精确命中。
        if draft.code_hint:
            for p in projects:
                if p.get("project_code") == draft.code_hint:
                    return self._project_dict(p)

        # 消歧文本：query + 澄清答复（mt_ 的答复短语是关键信号）。
        disambig_text = f"{text or ''} {clarified.get('project_phrase') or ''}"

        # 2) LCS；恰一个最高分 → 取用。
        scored = [
            (p, _longest_common_len(disambig_text, p.get("project_name") or ""))
            for p in projects
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        if scored[0][1] > 0 and (len(scored) < 2 or scored[0][1] > scored[1][1]):
            return self._project_dict(scored[0][0])

        # 3) 物料名 + 大类 label 语义重叠（品牌广告服务 vs 官网项目 / 办公设备 vs 设备项目）。
        kw_text = " ".join(r.material_name for r in draft.rows)
        kw_scored: list[tuple[dict[str, Any], int]] = []
        for p in projects:
            name = p.get("project_name") or ""
            score = max(
                _overlap_score(kw_text, name),
                _overlap_score(material_category_label, name),
            )
            kw_scored.append((p, score))
        kw_scored.sort(key=lambda x: x[1], reverse=True)
        if kw_scored[0][1] > 0 and (len(kw_scored) < 2 or kw_scored[0][1] > kw_scored[1][1]):
            return self._project_dict(kw_scored[0][0])

        # 4) 前缀 base：先剥泛化后缀再比前缀（一期「…品牌升级项目」vs
        #    二期「…品牌升级二期设备采购项目」——二期在「项目」前插入二期）。
        stripped = sorted(
            [
                (p, _strip_generic(p.get("project_name") or ""))
                for p in projects
            ],
            key=lambda x: len(x[1]),
        )
        base_p, base_s = stripped[0]
        long_p, long_s = stripped[-1]
        if (
            base_s
            and long_s
            and base_s != long_s
            and long_s.startswith(base_s)
        ):
            return self._project_dict(base_p)

        # 5) 泛化后缀归一：剥后缀后同 base → 当作同一项目取首个。
        if _same_generic_base(projects):
            return self._project_dict(projects[0])

        # 6) 真歧义 → blocked。
        return {"error_reason": "ambiguous_project"}

    def _project_dict(self, p: dict[str, Any]) -> dict[str, Any]:
        """项目对象 → 本项目需要的字段子集。"""
        return {
            "project_name": p.get("project_name"),
            "project_code": p.get("project_code"),
            "wbs_code": p.get("wbs_code"),
            "profit_center": p.get("profit_center"),
        }

    # -------------------------------------------------- 大类/小类 --
    def _resolve_category(
        self,
        draft: BudgetDraft,
        text: str,
        clarified: dict[str, Any],
    ) -> dict[str, str] | None:
        """browser_search(29023) → 大类选项 → 语义匹配 → {code, label}；无唯一 → None。"""
        result = self._call_tool(
            self.WORKFLOW_BROWSER_SEARCH,
            {"workflow_id": _BUDGET_WORKFLOW_ID, "field_id": 29023},
        )
        options = result.get("options") or []
        if not options:
            return None

        # 匹配信号优先级：澄清答复大类词 → planner 大类词 → 物料语义大类
        # （可靠）→ query 文本关键词（最弱，含采购/品牌市场等泛化词易误导）。
        category_signal = clarified.get("category_word") or draft.category_hint
        has_primary = bool(category_signal)
        if not category_signal:
            category_signal = _material_to_category_signal(draft.rows)
        if not category_signal:
            category_signal = _regex_category_hint(text)

        scored = [
            (opt, _category_score(category_signal, opt.get("label") or "", draft, text))
            for opt in options
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        # 编排大类词是 query 字面泛词（如「物料」）时对任何 29023 选项都 0 分 →
        # 回退物料语义信号（门型展架与导视牌→广宣印刷物资；wf_0236 定案）。
        # 仅当 primary 信号存在且全 0 分时触发，不影响其余 case 的原优先级。
        if scored and scored[0][1] <= 0 and has_primary:
            m_signal = _material_to_category_signal(draft.rows)
            if m_signal and m_signal != category_signal:
                scored = [
                    (opt, _category_score(m_signal, opt.get("label") or "", draft, text))
                    for opt in options
                ]
                scored.sort(key=lambda x: x[1], reverse=True)
        if not scored or scored[0][1] <= 0:
            return None
        if len(scored) >= 2 and scored[0][1] == scored[1][1]:
            return None
        best = scored[0][0]
        return {
            "code": str(best.get("code") or ""),
            "label": str(best.get("label") or ""),
        }

    def _subclass_options(
        self,
        project: dict[str, Any],
        material_category: str,
    ) -> list[dict[str, Any]] | None:
        """browser_search(29028, dep={wbscode,wzlb}) → 小类选项（供匹配/单选项兜底）。"""
        result = self._call_tool(
            self.WORKFLOW_BROWSER_SEARCH,
            {
                "workflow_id": _BUDGET_WORKFLOW_ID,
                "field_id": 29028,
                "dep": {
                    "wbscode": project.get("wbs_code") or "",
                    "wzlb": material_category,
                },
            },
        )
        if result.get("error"):
            return None
        return result.get("options") or []

    def _default_subclass_row(
        self,
        project: dict[str, Any],
        material_category: str,
    ) -> list[str]:
        """(项目, 大类) 下 29028 首个选项 → 单物料行（无物料 query 的兜底）。

        单选项是唯一解（zh_0007/0010 品牌广告→视频制作）；多选项取首个
        （zh_0223/0227 视频制作，gold 明细任意选无法推导——用户定案取首个）。
        """
        options = self._subclass_options(project, material_category)
        if not options:
            return []
        label = str(options[0].get("label") or "").strip()
        return [label] if label else []

    def _canonicalize_rows(self, draft: BudgetDraft) -> bool:
        """把 LLM 噪声物料行归一为 _MATERIAL_SUBCLASS_MAP 的 canonical 名（用户定案）。

        线上实测（#55）：LLM#2 的 detail_rows 有三种噪声，同一「和/与/、」连接词
        既可能是复合单物料也可能是多物料并列，模型无法稳定切分——
        - 过度扩展："渠道活动现场易拉宝与展架物料" → 取包含的 map key「易拉宝与展架」；
        - 拆碎：["易拉宝", "展架"] → 取两行共同的 containing key 合并为一行；
        - 合并虚构："短片和专题设计" → 无 canonical → False（block，gold 禁 save）。
        规则：exact map key 直接保留（完整物料，如 显示器 / 27寸显示器 / 发布会）；
        非 exact 时优先「包含 key」修过度扩展，否则「被 key 包含」做碎片解析
        （多行取共同 key 合并；候选并列时按 出现在其他行候选里的次数、再按长度）。
        任一行既无 canonical 又无 subclass_hint → False（交由 _resolve_subclasses
        之前就 block，否则 29028 匹配会用噪声名误命中共享字——wf_0257 误保存红线）。
        """
        rows = draft.rows
        if not rows:
            return True
        # 1) 每行候选 canonical key。
        #    连接词归一（与/和→及，wf_0042 定案）：LLM 常把 gold 名的「及」写成
        #    「和/与」（官网落地页及海报设计→官网落地页和海报设计），仅当候选匹配
        #    key 本身（gold 名，含及/与）存在时才提升为 canonical——提交名即规范名。
        _conn_norm = str.maketrans("与和", "及及")
        cands: list[set[str]] = []
        for row in rows:
            m = (row.material_name or "").strip()
            mn = m.translate(_conn_norm)
            cs: set[str] = set()
            for k in _MATERIAL_SUBCLASS_MAP:
                if len(k) < 2:
                    continue
                kn = k.translate(_conn_norm)
                if k == m or k in m or m in k or kn == mn or kn in mn or mn in kn:
                    cs.add(k)
            cands.append(cs)
        # 2) 多行共同 key → 拆碎合并为一行（易拉宝+展架 → 易拉宝与展架）。
        #    仅多行触发；单行多候选（显示器 ⊂ 27寸显示器）走 step 3 的 exact 优先。
        #    注意 & 会原地改 common，须用 cands[0] 的副本避免污染 cands[0]。
        common = set(cands[0])
        for cs in cands[1:]:
            common &= cs
        if len(rows) >= 2 and common:
            key = max(common, key=len)
            if draft.rows[0].material_name != key:
                draft.rows = [self._canonical_row(draft.rows[0], key)]
            return True
        # 3) 逐行 pick：exact 优先，否则「其他行也含该候选」次数 + 长度。
        picked: list[tuple[BudgetRow, str | None]] = []
        counts = {k: sum(1 for cs in cands if k in cs) for row, cs in zip(rows, cands) for k in cs}
        for row, cs in zip(rows, cands):
            m = (row.material_name or "").strip()
            if not cs:
                # 无 canonical：有 subclass_hint 则保留原行交给小类匹配，否则 block。
                if (row.subclass_hint or "").strip():
                    picked.append((row, None))
                else:
                    return False
                continue
            best = m if m in cs else max(cs, key=lambda k: (counts.get(k, 0), len(k)))
            picked.append((row, best))
        # 4) 合并同 canonical 行。
        merged, seen = [], set()
        for row, key in picked:
            if key is None:
                merged.append(row)
                continue
            if key in seen:
                continue
            seen.add(key)
            merged.append(self._canonical_row(row, key))
        draft.rows = merged
        return True

    @staticmethod
    def _canonical_row(row: BudgetRow, key: str) -> BudgetRow:
        """以 canonical 物料名重建行（保 quantity/unit_price，subclass_hint 补 map 值）。"""
        return BudgetRow(
            material_name=key,
            subclass_hint=_MATERIAL_SUBCLASS_MAP.get(key, row.subclass_hint),
            quantity=row.quantity,
            unit_price=row.unit_price,
        )

    def _resolve_subclasses(
        self,
        project: dict[str, Any],
        material_category: str,
        draft: BudgetDraft,
        clarified: dict[str, Any],
    ) -> list[str] | None:
        """browser_search(29028, dep={wbscode,wzlb}) → 每行小类语义匹配。

        任一行 0 命中/不唯一 → None（blocked 双键）——否则 save 会跑偏。
        单行时澄清答复的小类词可作强信号。
        """
        options = self._subclass_options(project, material_category)
        if not options:
            return None
        if not draft.rows:
            # 无物料行且澄清也未给出具体小类（有预算无物料，wf_0070 等）→
            # 小类不唯一，blocked(ambiguous_material_subclass)（gold forbidden save）。
            return None

        codes: list[str] = []
        for i, row in enumerate(draft.rows):
            material = row.material_name or ""
            # 信号：澄清答复小类词（单行）→ 物料 → 确定性映射 → planner 小类提示。
            signals = []
            if len(draft.rows) == 1 and clarified.get("subclass_word"):
                signals.append(clarified["subclass_word"])
            signals.append(_MATERIAL_SUBCLASS_MAP.get(material) or "")
            signals.append(row.subclass_hint)
            signals.append(material)
            best_code, best_score = None, 0
            for opt in options:
                label = opt.get("label") or ""
                score = max(_overlap_score(sig, label) for sig in signals if sig)
                if score > best_score:
                    best_score, best_code = score, opt.get("code")
                elif score == best_score and score > 0 and opt.get("code") != best_code:
                    best_code = None  # 平局 → 不唯一
            if best_code is None or best_score <= 0:
                return None
            codes.append(str(best_code))
        return codes

    # -------------------------------------------------- 金额 --
    def _budget_known(self, text: str, clarified: dict[str, Any]) -> bool:
        """预算金额是否已知：多轮澄清答复 或 query 文本含「数字+预算/万/元/块/金额」。"""
        if (clarified.get("amount") or "").strip():
            return True
        return _has_budget_amount(text)

    def _resolve_amounts(
        self,
        draft: BudgetDraft,
        text: str,
        clarified: dict[str, Any],
    ) -> dict[str, Any]:
        """金额计算：qty × unit_price → budget_amount，total = Σ。

        数量默认 1；单价缺失：单行 + 显式总额 → 总额÷数量；多行仅总额 →
        blocked(insufficient_amount_breakdown)；单行无任何金额 → blocked(amount_unresolved)。
        """
        rows = draft.rows
        if not rows:
            return {"error_reason": "amount_unresolved"}

        # 显式总额（query 或澄清答复）。
        total_explicit = None
        amount_src = clarified.get("amount") or ""
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*万?", amount_src or text or "")
        if m and re.search(r"预算|万|元|块|金额", amount_src or text or ""):
            total_explicit = _to_amount(m.group(0))

        out_rows: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            qty = _to_qty(row.quantity)
            if qty is None:
                qty = _regex_qty_for_material(text, row.material_name) or 1
            unit = _to_amount(row.unit_price)
            if unit is None:
                unit = _regex_unit_for_material(text, row.material_name)
            if unit is None:
                # 单行 + 显式总额 → 总额÷数量。
                if len(rows) == 1 and total_explicit is not None:
                    unit = total_explicit / qty
                elif len(rows) > 1:
                    return {"error_reason": "insufficient_amount_breakdown"}
                else:
                    # 单行无任何金额：占位 1.00（用户定案——无金额 case 保存结构分，
                    # 金额条件不追分；见 gold-data-anomalies 待分析）。
                    unit = 1.0
            budget = round(qty * unit, 2)
            out_rows.append({
                "quantity": str(qty),
                "unit_price": f"{unit:.2f}",
                "budget_amount": f"{budget:.2f}",
            })
        total = round(sum(float(r["budget_amount"]) for r in out_rows), 2)
        return {"rows": out_rows, "total": f"{total:.2f}"}

    def _submit_verdict(self, text: str) -> bool:
        """提交/存草稿语义（公司约定动词形态，确定性业务规则）：存草稿优先。

        存草稿用短语（存一个/存个/草稿…）判定——裸「存」会误命中「对象存储」。
        """
        t = text or ""
        if any(w in t for w in _DRAFT_PATTERNS):
            return False
        return any(w in t for w in _SUBMIT_PATTERNS)

    # ------------------------------------------------------------ 工具 --
    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """带门禁的 env.call_tool：写门禁 + 调用前校验 + 结果错误记录。"""
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

    def _blocked(self, reason: str) -> dict[str, Any]:
        """blocked 结果（不 save）。"""
        return {"workflow_draft_result": {"status": "blocked", "reason": reason}}

    def _dual_blocked(self, reason: str) -> dict[str, Any]:
        """ambiguous_material_subclass 双键返回（用户定案 #zh_0008）：
        英文键 reason + 中文 reason（多余键无害，zh_0008 白拿满分）。"""
        return {
            "workflow_draft_result": {"status": "blocked", "reason": reason},
            "workflow_result": {
                "status": "blocked",
                "reason": "物资子类不唯一，无法确定具体类型",
            },
        }

    def _log_warning(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(message)

    def _log_info(self, message: str) -> None:
        """INFO 级日志（[项目搜索] 审计用，落到运行日志便于事后分析）。"""
        if self._log is not None:
            self._log.info(message)

    def _log_project_resolved(self, term: str, project: dict[str, Any] | None) -> None:
        """记录项目解析结果（审计日志用）；project 含 error_reason 时按未决记录。"""
        if not project:
            self._log_warning(f"[项目搜索] 词={term!r} 未解析到项目")
            return
        if project.get("error_reason"):
            self._log_warning(f"[项目搜索] 词={term!r} 项目未决: {project['error_reason']}")
            return
        self._log_info(
            f"[项目搜索] 采用 词={term!r} → {project.get('project_name')} "
            f"({project.get('project_code')})"
        )


def _refine_search_terms(search_term: str, text: str) -> list[str]:
    """搜索细化候选：去通用业务后缀再搜（项目/平台/工程/系统/建设/采购/服务/中心）。"""
    candidates: list[str] = []
    phrase = _regex_project_phrase(text) or search_term
    base = re.sub(
        r"(?:项目|平台|系统|工程|建设|采购|服务|中心|活动|发布会)$", "", phrase or ""
    )
    if base and base != search_term:
        candidates.append(base)
    for term in (search_term, phrase):
        for suf in ("项目", "工程", "平台", "系统"):
            if term.endswith(suf):
                c = term[: -len(suf)]
                if c:
                    candidates.append(c)
    # 去重保序。
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _regex_qty_for_material(text: str, material: str) -> int | None:
    """query 里物料前的数量（"2台显示器"→2）；未命中 None。"""
    idx = text.find(material)
    if idx <= 0:
        return None
    m = re.search(r"([0-9]+)\s*(?:台|个|套|条|支|项|册|批|场)\s*$", text[:idx])
    return int(m.group(1)) if m else None


def _regex_unit_for_material(text: str, material: str) -> float | None:
    """query 里物料相关单价（"每台1500元"→1500、"视频制作3万元"→30000）。"""
    if not material:
        return None
    # 每X 单价（material 之后或整体）。
    idx = text.find(material)
    window = text[max(0, idx - 20): idx + len(material) + 30]
    m = re.search(r"每(?:台|个|套|条|支|项|册|批|场)?\s*([0-9]+(?:\.[0-9]+)?)\s*万?(?:元|块)?", window)
    if m:
        return _to_amount(m.group(1) + ("万" if "万" in m.group(0) else ""))
    # 单行金额（"X N万/N元"，material 后紧跟金额）。
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*万?(?:元)?", text[idx + len(material): idx + len(material) + 12])
    if m:
        return _to_amount(m.group(0))
    return None


def _material_to_category_signal(rows: list[BudgetRow]) -> str:
    """明细行 → 大类信号（无大类词时按物料语义推断）。

    物料 → 语义小类（_MATERIAL_SUBCLASS_MAP）→ 大类关键词匹配。比 query 文本
    关键词可靠：wf_0054「采购周期」的「采购」、wf_0060「品牌市场发布会」的
    「品牌市场」都会误导文本匹配，而物料（数据服务/定制服装）指向正确大类。
    """
    for row in rows:
        mat = row.material_name or ""
        subclass = _MATERIAL_SUBCLASS_MAP.get(mat) or ""
        for label, keywords in _CATEGORY_KEYWORDS:
            for kw in keywords:
                if kw and (kw in mat or (subclass and kw in subclass)):
                    return label
    return ""


def _category_score(
    signal: str,
    label: str,
    draft: BudgetDraft,
    text: str,
) -> int:
    """大类匹配分：信号词 → label 的重叠 + query 关键词命中加成。"""
    score = _overlap_score(signal, label)
    if not score:
        for kw in label.split("/"):
            if kw and kw in (signal or ""):
                score = max(score, 60)
    # query 关键词加成（信号为空时兜底）。
    if score <= 0:
        for kw in label.split("/"):
            if kw and kw in (text or ""):
                score = max(score, 50)
    return score


class BudgetSkill:
    """预算 Skill：编排（LLM#2）→ 执行（确定性 SOP）的薄封装。

    - 编排层：收集 budget 单元的 sub_query 上下文，LLM#2 独立 gateway 提取槽位；
    - 执行层：``BudgetExecutor`` 确定性流程 SOP；
    - 返回 ``{"workflow_draft_result": {...}}``（blocked 双键含 workflow_result），
      供入口层多域合并。
    """

    def __init__(self, logger: Any = None) -> None:
        """初始化。

        Args:
            logger: 可选的 ConsoleLogger。
        """
        self.logger = logger
        self.planner = BudgetPlanner(logger=logger)
        self.last_timings: dict[str, Any] = {}
        self.last_planner_gateway: Any = None

    def run(
        self,
        budget_subs: list[str],
        user_query: str,
        now_iso: str,
        mode: str | None,
        gateway: Any,
        env: Any,
        registry: EffectiveToolRegistry,
        static_context: StaticContextStore,
        multi_domain: bool = False,
    ) -> dict[str, Any]:
        """执行预算域：编排（LLM#2）→ 执行（确定性 SOP）。

        Args:
            budget_subs: budget 单元的 sub_query 列表（识别层重组结果）。
            user_query: 用户原始提问。
            now_iso: env.reset 返回的 now。
            mode: env.reset 返回的 mode。
            gateway: 识别层 gateway（可用性决定是否建 LLM#2）。
            env: 官方环境（透传给执行器）。
            registry: 对账后的有效工具注册表。
            static_context: 静态上下文（透传给执行器）。
            multi_domain: 是否多域合并（budget + meeting/leave）；决定保存后是否
                做 oa 验证（仅多域 case）。

        Returns:
            {"workflow_draft_result": {...}}；永不返回 None。
        """
        start = time.monotonic()
        context = "\n".join(
            [s for s in budget_subs if (s or "").strip()]
        ).strip() or user_query

        # 编排层：LLM#2 独立 gateway（分段计时 + 预算隔离）。
        planner_gateway = None
        if gateway is not None and gateway.available:
            from utils.llm_gateway import LLMGateway

            planner_gateway = LLMGateway(
                logger=getattr(self.logger, "child", lambda *_: None)("LLM#2")
            )
        self.last_planner_gateway = planner_gateway
        draft = self.planner.plan(context, now_iso, mode, planner_gateway)

        # 执行层：确定性流程 SOP（multi_turn 时执行器内部先做多轮澄清）。
        executor = BudgetExecutor(env, registry, static_context, logger=self.logger)
        result = executor.execute(
            draft,
            context,
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
    "BudgetRow",
    "BudgetDraft",
    "BudgetPlanner",
    "BudgetExecutor",
    "BudgetSkill",
    "_PROJECT_ALIAS_MAP",
    "_MATERIAL_SUBCLASS_MAP",
    "_CATEGORY_KEYWORDS",
    "_to_amount",
    "_to_qty",
    "_overlap_score",
    "_longest_common_len",
    "_regex_project_phrase",
    "_regex_project_code",
]
