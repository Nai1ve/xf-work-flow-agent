"""费用实时绑定与恢复的运行时回归（不依赖训练样本或固定答案）。"""

from __future__ import annotations

from typing import Any

from utils.budget_skill import (
    BudgetDraft,
    BudgetExecutor,
    BudgetPlanner,
    BudgetRow,
    BudgetSkill,
    _refine_search_terms,
    _normalize_project_search_term,
)
from utils.llm_gateway import FakeBackend, LLMGateway
from utils.profiles import ExecutionProfile, ProfileConfig


class _Registry:
    def is_write(self, name: str) -> bool:
        return name == "workflow.save"

    def can_execute_write(self, name: str) -> bool:
        return True

    def validate_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "errors": []}


class _Env:
    def __init__(self, projects: list[dict[str, Any]], *, schema_flip: bool = False):
        self.projects = projects
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.saved: list[dict[str, Any]] = []
        self.schema_calls = 0
        self.schema_flip = schema_flip

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "user.get_info":
            return {"users": [{"user_id": "u1", "employee_no": "e1"}]}
        if name == "workflow.catalog":
            return {"workflows": [{"workflow_id": 34747, "name": "费用物资"}]}
        if name == "workflow.schema":
            self.schema_calls += 1
            if self.schema_flip and self.schema_calls > 1:
                return {"schema": {"fields": [
                    {"field_id": 901, "name": "物资大类"},
                    {"field_id": 902, "name": "物资小类", "depends_on": "901"},
                ]}}
            return {"schema": {"required_fields": ["material_category"]}}
        if name == "workflow.project_search":
            term = args.get("project_name", "")
            return {"projects": [p for p in self.projects if term in p["project_name"]]}
        if name == "workflow.browser_search":
            field = args.get("field_id")
            if self.schema_flip and field == 29023:
                return {"error": "Browser options not found"}
            if field == 29023 or field == 901:
                return {"options": [{"code": "cat-1", "label": "定制物资"}]}
            if field == 29028 or field == 902:
                return {"options": [
                    {"code": "sub-1", "label": "定制促品"},
                    {"code": "sub-2", "label": "定制服装"},
                ]}
            return {"options": []}
        if name == "workflow.save":
            self.saved.append(args)
            return {"draft_saved": True}
        return {"items": []}


def _executor(env: _Env, selector: Any = None, *, v3: bool = True) -> BudgetExecutor:
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        requested_profile=ExecutionProfile.CANDIDATE_V2 if v3 else None,
        legacy_budget_templates=not v3,
        budget_runtime_v3=v3,
    )
    return BudgetExecutor(env, _Registry(), None, profile_config=config,
                          material_selector=selector)


def test_runtime_v3_toggle_keeps_legacy_refinement_budget() -> None:
    assert len(_refine_search_terms("官网传播", "官网传播那边", runtime_v3=True)) == 1
    assert _refine_search_terms("官网传播项目", "官网传播项目", runtime_v3=False) == ["官网传播"]

    # The selector is ignored when the opt-in budget flag is off; the legacy
    # deterministic mapping remains authoritative.
    env = _Env([_project()])
    result = _executor(
        env, lambda rows, options: [{"row_index": 0, "candidate_index": 0, "confidence": 0.99}],
        v3=False,
    ).execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("定制服装", unit_price="300")]),
        "官网改版传播项目，定制服装300元，存草稿",
        "官网改版传播项目，定制服装300元，存草稿", "now",
    )
    assert result["workflow_draft_result"]["status"] == "draft_saved"
    assert env.saved[0]["data"]["details"]["detail_2"][0]["material_subclass"] == "sub-2"


def test_hybrid_with_budget_flag_enables_runtime_selector() -> None:
    env = _Env([_project()])
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        legacy_budget_templates=True,
        budget_runtime_v3=True,
    )
    ex = BudgetExecutor(env, _Registry(), None, profile_config=config,
                        material_selector=lambda rows, options: [
                            {"row_index": 0, "candidate_index": 0, "confidence": 0.9}
                        ])
    assert ex._runtime_v3_enabled() is True


def test_project_refinement_can_be_enabled_without_strict_budget_guards() -> None:
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        legacy_budget_templates=True,
        budget_runtime_v3=False,
        project_search_refinement_v3=True,
    )
    ex = BudgetExecutor(_Env([]), _Registry(), None, profile_config=config)
    assert ex._runtime_v3_enabled() is False
    assert ex._project_refinement_enabled() is True


def test_field_descriptions_ids_do_not_cross_roles() -> None:
    schema = {"field_descriptions": {
        "material_category": "物资大类，通过 browser_search field_id=29023 查询",
        "material_subclass": "物资小类，通过 browser_search field_id=29028 查询，依赖 wzlb",
    }}
    assert BudgetExecutor._schema_field_id(schema, "category") == 29023
    assert BudgetExecutor._schema_field_id(schema, "subclass") == 29028


def test_planner_gateway_selector_is_one_index_only_call() -> None:
    gateway = LLMGateway(
        config={"provider": "openai_compatible", "base_url": "https://test.example/v1",
                "model": "test", "api_key": "test", "llm_budget_s": 35},
        backend=FakeBackend([{"selections": [
            {"row_index": 0, "candidate_index": 1, "confidence": 0.93}
        ]}]),
    )
    selector = BudgetSkill._build_material_selector(gateway)
    result = selector([{"row_index": 0, "material_name": "文化服装"}], [
        {"candidate_index": 0, "label": "定制促品"},
        {"candidate_index": 1, "label": "定制服装"},
    ])
    assert result[0]["candidate_index"] == 1
    assert len(gateway._backend.calls) == 1


def test_budget_v3_uses_short_generic_prompt_cards() -> None:
    gateway = LLMGateway(
        config={"provider": "openai_compatible", "base_url": "https://test.example/v1",
                "model": "test", "api_key": "test", "llm_budget_s": 35},
        backend=FakeBackend([
            {"project": {"search_term": "官网传播", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "定制物资", "detail_rows": [
                {"material_name": "定制促品", "quantity": "1", "unit_price": "300"}
            ], "confidence": 0.9},
        ]),
    )
    config = ProfileConfig(profile=ExecutionProfile.HYBRID_COMPAT, budget_runtime_v3=True)
    draft = BudgetPlanner(profile_config=config).plan(
        "官网传播项目定制促品300元", "now", "single_turn", gateway
    )
    assert draft.source == "llm"
    prompts = [call[0]["content"] for call in gateway._backend.calls]
    assert all("只根据 sub_query" in prompt for prompt in prompts)
    assert all("示例（train" not in prompt for prompt in prompts)


def test_project_refinement_flag_also_uses_short_extraction_prompts() -> None:
    gateway = LLMGateway(
        config={"provider": "openai_compatible", "base_url": "https://test.example/v1",
                "model": "test", "api_key": "test", "llm_budget_s": 35},
        backend=FakeBackend([
            {"project": {"search_term": "知识助手升级", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "品牌广告服务", "detail_rows": [], "confidence": 0.9},
        ]),
    )
    config = ProfileConfig(
        profile=ExecutionProfile.HYBRID_COMPAT,
        project_search_refinement_v3=True,
    )
    draft = BudgetPlanner(profile_config=config).plan(
        "知识助手升级项目的设计费用", "now", "single_turn", gateway
    )
    assert draft.source == "llm"
    prompts = [call[0]["content"] for call in gateway._backend.calls]
    assert "示例（train" not in prompts[0]
    assert "示例（train" in prompts[1]


def test_budget_skill_uses_same_planner_gateway_for_runtime_selector(monkeypatch) -> None:
    env = _Env([_project()])
    fake = LLMGateway(
        config={"provider": "openai_compatible", "base_url": "https://test.example/v1",
                "model": "test", "api_key": "test", "llm_budget_s": 35},
        backend=FakeBackend([
            {"project": {"search_term": "官网改版传播", "code_hint": ""}, "confidence": 0.9},
            {"category_hint": "定制物资", "detail_rows": [
                {"material_name": "纪念徽章礼盒", "quantity": "1", "unit_price": "300"}
            ], "confidence": 0.9},
            {"selections": [{"row_index": 0, "candidate_index": 0, "confidence": 0.9}]},
        ]),
    )
    monkeypatch.setattr("utils.llm_gateway.LLMGateway", lambda **_kwargs: fake)
    config = ProfileConfig(profile=ExecutionProfile.HYBRID_COMPAT,
                           budget_runtime_v3=True)
    class _Available:
        available = True
    result = BudgetSkill(profile_config=config).run(
        ["官网改版传播项目，纪念徽章礼盒300元，存草稿"],
        "官网改版传播项目，纪念徽章礼盒300元，存草稿", "now", "single_turn",
        _Available(), env, _Registry(), None,
    )
    assert result["workflow_draft_result"]["status"] == "draft_saved"
    assert len(fake._backend.calls) == 3
    assert env.saved[0]["data"]["details"]["detail_2"][0]["material_subclass"] == "sub-1"


def _project(name: str = "官网改版传播项目") -> dict[str, str]:
    return {"project_name": name, "project_code": "N-1", "wbs_code": "N-1.03"}


def test_zero_hit_project_gets_one_semantic_refinement() -> None:
    env = _Env([_project()])
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(search_term="官网传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品", unit_price="21000")]),
        "官网传播那边有一笔定制促品2.1万元，直接提交",
        "官网传播那边有一笔定制促品2.1万元，直接提交", "now",
    )
    assert result["workflow_draft_result"]["status"] == "submitted"
    searches = [a.get("project_name") for n, a in env.calls
                if n == "workflow.project_search"]
    assert searches == ["官网传播", "传播"]


def test_refinement_can_use_a_distinctive_fragment_from_the_original_query() -> None:
    """主项目短语与运行时名称不同，细化只能取原文语义片段。"""
    env = _Env([_project("年度活动定制物资项目")])
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(
            search_term="品牌市场产品发布会",
            category_hint="定制物资",
            rows=[BudgetRow("定制促品", unit_price="10000")],
        ),
        "帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算10000元。",
        "帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算10000元。",
        "now",
    )
    assert result["workflow_draft_result"]["status"] == "submitted"
    searches = [
        a.get("project_name")
        for n, a in env.calls
        if n == "workflow.project_search"
    ]
    assert searches == ["品牌市场产品发布会", "定制"]


def test_refinement_removes_generic_upgrade_suffix_before_search() -> None:
    env = _Env([
        _project("知识助手官网与内容设计项目"),
        _project("知识助手线下推广活动项目"),
    ])
    ex = _executor(env)
    text = "帮我提交知识助手升级项目的官网改版设计费用，预算22000元。"
    project = ex._resolve_project(
        BudgetDraft(search_term="知识助手升级", rows=[BudgetRow("官网改版设计")]),
        text,
        {},
        "品牌广告服务",
    )
    assert project["project_name"] == "知识助手官网与内容设计项目"
    searches = [
        a.get("project_name")
        for n, a in env.calls
        if n == "workflow.project_search"
    ]
    assert searches == ["知识助手"]


def test_primary_project_normalization_keeps_non_generic_phrase() -> None:
    assert _normalize_project_search_term("知识助手升级项目") == "知识助手"
    assert _normalize_project_search_term("办公平台升级项目") == "办公平台升级"


def test_semantic_refinement_still_blocks_ambiguous_candidates() -> None:
    env = _Env([
        _project("年度活动定制甲项目"),
        _project("年度活动定制乙项目"),
    ])
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(
            search_term="品牌市场产品发布会",
            category_hint="定制物资",
            rows=[BudgetRow("定制促品", unit_price="100")],
        ),
        "帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算100元。",
        "帮我提交定制物资申请，项目是品牌市场产品发布会，要做一批定制促品，预算100元。",
        "now",
    )
    assert result["workflow_draft_result"]["reason"] == "ambiguous_project"
    assert not any(n == "workflow.save" for n, _ in env.calls)


def test_refinement_with_multiple_candidates_blocks() -> None:
    env = _Env([_project("官网改版传播项目"), _project("官网线下传播项目")])
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(search_term="官网传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品", unit_price="100")]),
        "官网传播那边有一笔定制促品100元，直接提交",
        "官网传播那边有一笔定制促品100元，直接提交", "now",
    )
    assert result["workflow_draft_result"]["reason"] == "ambiguous_project"
    assert len([n for n, _ in env.calls if n == "workflow.project_search"]) == 2


def test_generic_project_discovery_is_not_used() -> None:
    env = _Env([_project()])
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(search_term="项目", category_hint="定制物资",
                    rows=[BudgetRow("定制促品", unit_price="100")]),
        "申请定制促品100元",
        "申请定制促品100元", "now",
    )
    assert result["workflow_draft_result"]["reason"] == "ambiguous_project"
    assert not any(a.get("project_name") == "项目" for n, a in env.calls
                   if n == "workflow.project_search")


def test_subclasses_bind_from_selector_indices_and_codes_come_from_options() -> None:
    env = _Env([_project("官网改版传播项目")])

    def selector(rows: list[dict[str, Any]], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert [o["label"] for o in options] == ["定制促品", "定制服装"]
        return [
            {"row_index": 0, "candidate_index": 0, "confidence": 0.95},
            {"row_index": 1, "candidate_index": 1, "confidence": 0.91},
        ]

    ex = _executor(env, selector)
    result = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资", rows=[
            BudgetRow("纪念徽章礼盒", unit_price="300"),
            BudgetRow("文化服装", unit_price="500"),
        ]),
        "官网改版传播项目，纪念徽章礼盒300元、文化服装500元，存草稿",
        "官网改版传播项目，纪念徽章礼盒300元、文化服装500元，存草稿", "now",
    )
    assert result["workflow_draft_result"]["status"] == "draft_saved"
    rows = env.saved[0]["data"]["details"]["detail_2"]
    assert [r["material_subclass"] for r in rows] == ["sub-1", "sub-2"]


def test_low_confidence_selector_blocks() -> None:
    env = _Env([_project()])
    ex = _executor(env, lambda rows, options: [
        {"row_index": 0, "candidate_index": 0, "confidence": 0.2}
    ])
    result = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("纪念徽章礼盒", unit_price="300")]),
        "官网改版传播项目，纪念徽章礼盒300元，存草稿",
        "官网改版传播项目，纪念徽章礼盒300元，存草稿", "now",
    )
    assert result["workflow_draft_result"]["status"] == "blocked"
    assert env.saved == []


def test_browser_error_refreshes_schema_and_retries_once() -> None:
    env = _Env([_project()], schema_flip=True)
    ex = _executor(env)
    result = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品", unit_price="300")]),
        "官网改版传播项目，定制促品300元，存草稿",
        "官网改版传播项目，定制促品300元，存草稿", "now",
    )
    assert result["workflow_draft_result"]["status"] == "draft_saved"
    assert env.schema_calls == 2
    assert len([1 for n, a in env.calls if n == "workflow.browser_search"
                and a["field_id"] in (29023, 901)]) == 2


def test_amount_gate_and_conservation() -> None:
    env = _Env([_project()])
    ex = _executor(env)
    blocked = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品")]),
        "官网改版传播项目，定制促品，存草稿",
        "官网改版传播项目，定制促品，存草稿", "now",
    )
    assert blocked["workflow_draft_result"]["status"] == "blocked"
    assert env.saved == []

    single = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品")]),
        "官网改版传播项目，定制促品，总预算800元，存草稿",
        "官网改版传播项目，定制促品，总预算800元，存草稿", "now",
    )
    assert single["workflow_draft_result"]["total_amount"] == "800.00"

    multi = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资",
                    rows=[BudgetRow("定制促品"), BudgetRow("定制服装")]),
        "官网改版传播项目，定制促品和定制服装，总预算800元，存草稿",
        "官网改版传播项目，定制促品和定制服装，总预算800元，存草稿", "now",
    )
    assert multi["workflow_draft_result"]["status"] == "blocked"

    conserved = ex.execute(
        BudgetDraft(search_term="官网改版传播", category_hint="定制物资", rows=[
            BudgetRow("定制促品", unit_price="300"),
            BudgetRow("定制服装", unit_price="500"),
        ]),
        "官网改版传播项目，定制促品300元、定制服装500元，存草稿",
        "官网改版传播项目，定制促品300元、定制服装500元，存草稿", "now",
    )
    assert conserved["workflow_draft_result"]["total_amount"] == "800.00"
