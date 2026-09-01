"""第一批请假审批人消歧回归。

这些测试只覆盖运行时证据驱动的 v3 分支，不依赖任何 Case/Gold 映射。
"""

from __future__ import annotations

from typing import Any

from utils.leave_skill import LeaveExecutor
from utils.leave_skill import LeaveDraft
from utils.profiles import ProfileConfig


class _PersonSearchExecutor(LeaveExecutor):
    def __init__(self, people: list[dict[str, Any]]) -> None:
        self._people = people
        self._profile_config = ProfileConfig(approver_resolution_v3=True)
        self.searches: list[dict[str, Any]] = []

    def _search_person_approver(
        self,
        workflow_id: int,
        keyword: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]:
        self.searches.append({"workflow_id": workflow_id, "keyword": keyword, "title": title})
        people = list(self._people)
        if keyword:
            people = [p for p in people if keyword in str(p.get("name") or "")]
        if title:
            people = [p for p in people if title in str(p.get("title") or "")]
        return people


def test_explicit_unique_approver_is_used_directly() -> None:
    executor = _PersonSearchExecutor([
        {"user_id": "u-1", "name": "王芳", "title": "产品经理"},
        {"user_id": "u-2", "name": "赵丽", "title": "测试工程师"},
    ])

    result = executor._resolve_approver(
        "审批人王芳",
        "王芳",
        72247,
        applicant={"user_id": "me", "department": "研发部"},
    )

    assert result == {"user_id": "u-1"}
    assert executor.searches == [{"workflow_id": 72247, "keyword": "王芳", "title": None}]


def test_duplicate_name_is_filtered_by_applicant_department() -> None:
    executor = _PersonSearchExecutor([
        {"user_id": "u-1", "name": "王芳", "department": "产品部", "title": "产品经理"},
        {"user_id": "u-2", "name": "王芳", "department": "运营部", "title": "运营经理"},
    ])

    result = executor._resolve_approver(
        "审批人王芳",
        "王芳",
        72247,
        applicant={"user_id": "me", "department": "产品部门"},
    )

    assert result == {"user_id": "u-1"}


def test_duplicate_name_can_use_department_encoded_in_candidate_title() -> None:
    """兼容只返回 title 的运行时人员记录（职位前缀携带部门）。"""
    executor = _PersonSearchExecutor([
        {"user_id": "u-1", "name": "刘", "title": "研发经理"},
        {"user_id": "u-2", "name": "刘", "title": "运营经理"},
    ])

    result = executor._resolve_approver(
        "审批人刘",
        "刘",
        72247,
        applicant={"user_id": "me", "department": "研发部"},
    )

    assert result == {"user_id": "u-1"}


def test_duplicate_name_without_unique_runtime_evidence_blocks() -> None:
    executor = _PersonSearchExecutor([
        {"user_id": "u-1", "name": "王芳", "department": "产品部", "title": "产品经理"},
        {"user_id": "u-2", "name": "王芳", "department": "运营部", "title": "运营经理"},
    ])

    result = executor._resolve_approver(
        "审批人王芳，直接提交",
        "王芳",
        72247,
        applicant={"user_id": "me"},
    )

    assert result == {"error_reason": "ambiguous_approver"}


def test_explicit_title_can_disambiguate_same_name() -> None:
    executor = _PersonSearchExecutor([
        {"user_id": "u-1", "name": "王芳", "title": "产品经理"},
        {"user_id": "u-2", "name": "王芳", "title": "运营主管"},
    ])

    result = executor._resolve_approver(
        "审批人王芳经理",
        "王芳经理",
        72247,
        applicant={"user_id": "me"},
    )

    assert result == {"user_id": "u-1"}


def test_ambiguous_approver_never_reaches_workflow_save(tmp_path) -> None:
    """集成门禁：v3 无法唯一消歧时必须在写入前阻断。"""
    from test_leave_skill import LeaveFakeEnv, _registry

    env = LeaveFakeEnv([
        {"user_id": "u-1", "name": "王芳", "department": "产品部", "title": "产品经理"},
        {"user_id": "u-2", "name": "王芳", "department": "运营部", "title": "运营经理"},
    ])
    executor = LeaveExecutor(
        env,
        _registry(env, tmp_path),
        None,
        profile_config=ProfileConfig(approver_resolution_v3=True),
    )
    draft = LeaveDraft(
        leave_type_hint="事假",
        approver_hint="王芳",
        schedule=[{
            "day_phrase": "明天",
            "start_hm": "14:00",
            "end_hm": "18:00",
            "full_day": False,
        }],
    )

    result = executor.execute(draft, "明天下午请事假，审批人王芳", "", "2026-05-11T09:00:00")

    assert result["workflow_draft_result"] == {
        "status": "blocked",
        "reason": "ambiguous_approver",
    }
    assert env.saved == []
