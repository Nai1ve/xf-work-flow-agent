"""统一语气与请假日期/替换判定的独立回归。"""

from __future__ import annotations

from datetime import date, timedelta

from utils.leave_skill import LeaveDraft, LeaveExecutor
from utils.holiday_calendar import is_workday
from utils.profiles import ProfileConfig
from utils.speech_act import parse_speech_act
from utils.understanding import TemporalResolver


class _Registry:
    def is_write(self, name: str) -> bool:
        return name in {"workflow.save", "workflow.delete"}

    def can_execute_write(self, name: str) -> bool:
        return True

    def validate_call(self, name: str, args: dict) -> dict:
        return {"ok": True, "errors": []}


class _Env:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.done_items = [{"request_id": "old", "workflow_id": 7}]
        self.deleted: list[dict] = []
        self.saved: list[dict] = []

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name == "user.get_info":
            return {"users": [{"user_id": "u", "employee_no": "e"}]}
        if name == "workflow.catalog":
            return {"workflows": [{"workflow_id": 7, "name": "请假"}]}
        if name == "workflow.schema":
            return {"schema": {
                "leave_type_options": [{"label": "事假", "value": "L"}],
                "reason_options": [{"label": "本人有事", "value": "10"}],
            }}
        if name == "workflow.search_person":
            return {"people": [{"user_id": "approver", "name": "赵丽"}]}
        if name == "workflow.save":
            self.saved.append(args)
            return {"submitted": bool(args.get("submit"))}
        if name == "workflow.delete":
            self.deleted.append(args)
            return {"deleted": True}
        if name in {"oa.done.list", "oa.todo.list"}:
            return {"items": self.done_items if name == "oa.done.list" else []}
        return {}


def _speech(text: str, domain: str | None = None):
    return parse_speech_act(text, domain=domain, speech_act_v3=domain == "leave")


def test_submit_actions_and_draft_safety() -> None:
    for action in ("走流程", "发起流程", "发起申请", "走审批", "送审", "直接申请"):
        assert parse_speech_act(action, speech_act_v3=True).selected is True
    assert _speech("暂不提交").selected is False
    assert _speech("申请草稿").selected is False
    assert _speech("不要保存草稿").selected is True
    assert parse_speech_act("不提交但直接申请", speech_act_v3=True).conflict is True
    assert _speech("费用申请").selected is False


def test_v3_submit_vocabulary_is_switchable_and_old_vocabulary_survives() -> None:
    assert parse_speech_act("走流程").selected is False
    assert parse_speech_act("走流程", speech_act_v3=True).selected is True
    assert parse_speech_act("发起审批").selected is True


def test_leave_natural_start_is_submit_but_negation_or_draft_wins() -> None:
    for text in ("我要请明天事假", "想请明天事假", "需要请明天事假", "请明天事假"):
        assert _speech(text, "leave").selected is True
    assert _speech("想请明天事假，暂不提交", "leave").selected is False
    assert _speech("想请明天事假，申请草稿", "leave").selected is False


def test_relative_leave_days_expand_to_contiguous_end_date() -> None:
    now = "2026-05-13T09:00:00"
    config = ProfileConfig(leave_range_v3=True)
    result = LeaveExecutor(None, None, None, profile_config=config)._resolve_schedule(
        "从明天开始请3天", "", now
    )
    start = date.fromisoformat(TemporalResolver(now).resolve_day("明天"))
    assert result == [
        (f"{start} 09:00", f"{start + timedelta(days=2)} 18:00")
    ]


def test_relative_leave_days_use_workday_calendar_when_requested() -> None:
    now = "2026-05-13T09:00:00"
    config = ProfileConfig(leave_range_v3=True)
    result = LeaveExecutor(None, None, None, profile_config=config)._resolve_schedule(
        "从周五开始请3天，排除周末", "", now
    )
    start = date.fromisoformat(TemporalResolver(now).resolve_day("周五"))
    expected = start
    seen = 0
    while seen < 3:
        if is_workday(expected):
            seen += 1
            if seen == 3:
                break
        expected += timedelta(days=1)
    assert result == [(f"{start} 09:00", f"{expected} 18:00")]


def test_relative_leave_days_accept_quantity_before_or_after_date() -> None:
    config = ProfileConfig(leave_range_v3=True)
    executor = LeaveExecutor(None, None, None, profile_config=config)
    now = "2026-05-13T09:00:00"
    start = date.fromisoformat(TemporalResolver(now).resolve_day("明天"))
    for text in ("需要请3天丧假，从明天开始", "从明天开始请父母陪护假3天"):
        assert executor._resolve_schedule(text, "", now) == [
            (f"{start} 09:00", f"{start + timedelta(days=2)} 18:00")
        ]


def test_v3_leave_features_are_off_without_profile_flags() -> None:
    now = "2026-05-13T09:00:00"
    executor = LeaveExecutor(None, None, None)
    # 旧档位仍按单日惯例解析；新连续范围须显式开启 leave_range_v3。
    start = TemporalResolver(now).resolve_day("明天")
    assert executor._resolve_schedule("从明天开始请3天", "", now) == [
        (f"{start} 09:00", f"{start} 18:00")
    ]
    assert parse_speech_act("我要请明天事假", domain="leave").selected is False


def _execute(env: _Env, text: str) -> dict:
    draft = LeaveDraft(leave_type_hint="事假", approver_hint="赵丽")
    config = ProfileConfig(speech_act_v3=True, leave_range_v3=True)
    return LeaveExecutor(env, _Registry(), None, profile_config=config).execute(
        draft, text, text, "2026-05-13T09:00:00"
    )


def test_same_sentence_correction_does_not_query_or_delete_old() -> None:
    env = _Env()
    _execute(env, "哦不对，改成明天下午3点到6点请事假，审批人赵丽，直接申请")
    names = [name for name, _ in env.calls]
    assert "oa.done.list" not in names
    assert "oa.todo.list" not in names
    assert env.deleted == []


def test_historical_replacement_queries_and_deletes_old() -> None:
    env = _Env()
    _execute(env, "昨天已经提交的请假改成明天下午3点到6点请事假，审批人赵丽，直接申请")
    names = [name for name, _ in env.calls]
    assert "oa.done.list" in names
    assert env.deleted == [{"request_id": "old"}]
