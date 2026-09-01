from __future__ import annotations

from my_agent import MyAgent, _RecordingEnv
from utils.context import CaseContext


class _Env:
    def call_tool(self, name: str, args: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "tool": name, "args_seen": args}


def test_recording_env_keeps_call_and_result_events() -> None:
    trace: list[dict[str, object]] = []
    wrapped = _RecordingEnv(_Env(), trace)

    result = wrapped.call_tool("demo.read", {"value": 1})

    assert result["ok"] is True
    assert [event["event"] for event in trace] == ["TOOL_CALL", "TOOL_RESULT"]
    assert trace[0]["tool"] == "demo.read"
    assert trace[0]["args"] == '{"value": 1}'


def test_context_events_include_internal_facts_and_policy_once() -> None:
    context = CaseContext(
        case_id="case-1",
        user_query="提交费用",
        now_iso="2026-09-01T09:00:00",
        mode="test",
    )
    context.upsert_fact(
        "expense.project_code",
        "P-1",
        source="RUNTIME_TOOL",
        task_id="task-0",
    )
    context.record_policy({"policy_id": "speech_act", "selected": "submit"})
    trace: list[dict[str, object]] = []

    MyAgent._append_context_events(trace, context)
    MyAgent._append_context_events(trace, context)

    assert [event["event"] for event in trace].count("FACT_UPSERT") == 1
    assert [event["event"] for event in trace].count("POLICY_DECISION") == 1
    fact_event = next(event for event in trace if event["event"] == "FACT_UPSERT")
    assert fact_event["source"] == "RUNTIME_TOOL"
    assert fact_event["task_id"] == "task-0"
