from __future__ import annotations

import json
import unittest
from pathlib import Path

from submission.my_agent import (
    BusinessSkillRegistry,
    MyAgent,
    ResultProjectionRegistry,
    RuntimeState,
    StepAction,
    TaskRuntime,
    ToolRegistry,
)
from submission.utils.skill_runtime import SkillRun


ROOT = Path(__file__).resolve().parents[1]


def load_index(name: str) -> dict:
    return json.loads((ROOT / "submission" / "static_context" / name).read_text(encoding="utf-8"))


class BusinessSkillContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = BusinessSkillRegistry(load_index("workflow_skills.index.json"))

    def test_eighteen_capabilities_map_to_fifteen_skills(self) -> None:
        capabilities = load_index("capabilities.index.json")
        self.assertEqual(len(capabilities["capabilities"]), 18)
        self.assertEqual(len(self.registry.skills), 15)
        self.assertEqual(len(self.registry.capability_map), 18)
        self.assertEqual(self.registry.validate_capability_coverage(capabilities), [])

    def test_all_skill_contracts_and_tools_are_valid(self) -> None:
        capabilities = load_index("capabilities.index.json")
        tools = ToolRegistry(load_index("tools.index.json"))
        self.assertEqual(self.registry.validate_contracts(), [])
        self.assertEqual(self.registry.validate_tool_coverage(capabilities, tools), [])

    def test_every_skill_has_collect_input_and_writes_have_confirmation(self) -> None:
        for skill_id in self.registry.skills:
            definition = self.registry.definition(skill_id)
            by_id = {node["id"]: node for node in definition["nodes"]}
            self.assertIn("collect_input", by_id, skill_id)
            writes = [node for node in definition["nodes"] if node["operation"] == "write"]
            if writes:
                self.assertIn("confirm_write", by_id, skill_id)
                self.assertFalse(any(node["cardinality"] == "foreach" for node in writes), skill_id)

    def test_rebook_selects_replacement_before_cancel(self) -> None:
        _, definition = self.registry.select_capability("meeting.cancel_rebook")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["query_rooms"]["depends_on"], ["locate"])
        self.assertEqual(by_id["select_room"]["depends_on"], ["query_rooms"])
        self.assertEqual(by_id["confirm_write"]["depends_on"], ["select_room"])
        self.assertEqual(by_id["cancel"]["depends_on"], ["confirm_write"])
        self.assertEqual(by_id["create"]["depends_on"], ["cancel"])

    def test_extend_has_explicit_occupancy_evidence(self) -> None:
        _, definition = self.registry.select_capability("meeting.extend")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["occupancy"]["tool"], "meetingroom.room.bookings")
        self.assertEqual(by_id["occupancy"]["depends_on"], ["locate"])
        self.assertEqual(by_id["confirm_write"]["depends_on"], ["occupancy"])

    def test_leave_replace_has_delete_barrier(self) -> None:
        _, definition = self.registry.select_capability("workflow.leave_replace_submit")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["confirm_write"]["depends_on"], ["source_lookup"])
        self.assertEqual(by_id["delete_source"]["depends_on"], ["confirm_write"])
        self.assertIn("delete_source", by_id["catalog"]["depends_on"])


class SkillRunTest(unittest.TestCase):
    def test_ready_nodes_and_repeat_invocations_are_task_scoped(self) -> None:
        definition = {
            "nodes": [
                {"id": "read", "phase": "read", "operation": "read", "depends_on": []},
                {"id": "write", "phase": "write", "operation": "write", "depends_on": ["read"]},
            ]
        }
        run = SkillRun("test", definition, task_id="t1", capability="meeting.book")
        self.assertEqual([node["id"] for node in run.ready_nodes()], ["read"])
        run.mark_completed("read")
        self.assertEqual([node["id"] for node in run.ready_nodes()], ["write"])
        run.mark_invocation("write", "segment-1", "completed")
        run.mark_invocation("write", "segment-2", "completed")
        self.assertEqual(run.invocation_status("write", "segment-2"), "completed")

    def test_repeat_until_returns_to_pending_after_each_finished_invocation(self) -> None:
        definition = {
            "nodes": [
                {
                    "id": "write",
                    "phase": "write",
                    "operation": "write",
                    "depends_on": [],
                    "cardinality": "repeat_until",
                }
            ]
        }
        run = SkillRun("test", definition)
        for index in range(3):
            run.mark_running("write")
            run.mark_invocation("write", f"item-{index}", "completed")
            if index < 2:
                self.assertEqual(run.apply_validation("write", {"status": "pending"})["status"], "repeat")
                self.assertTrue(run.is_ready("write"))
            else:
                self.assertEqual(run.apply_validation("write", {"status": "passed"})["status"], "completed")
        self.assertEqual(run.status, "completed")

    def test_single_node_does_not_repeat_while_validation_is_pending(self) -> None:
        definition = {
            "nodes": [
                {
                    "id": "write",
                    "phase": "write",
                    "operation": "write",
                    "depends_on": [],
                    "cardinality": "single",
                }
            ]
        }
        run = SkillRun("test", definition)
        run.mark_running("write")
        run.mark_invocation("write", "same", "completed")
        self.assertEqual(run.apply_validation("write", {"status": "pending"})["status"], "pending")
        self.assertEqual(run.statuses["write"], "running")

    def test_failure_retry_is_deduplicated_and_exhausts(self) -> None:
        definition = {
            "nodes": [
                {
                    "id": "query",
                    "phase": "read",
                    "depends_on": [],
                    "failure_edges": {
                        "empty": {
                            "action": "retry",
                            "target": "query",
                            "max_attempts": 1,
                            "exhausted_reason": "not_found",
                        }
                    },
                }
            ]
        }
        run = SkillRun("test", definition)
        self.assertEqual(run.resolve_failure("query", "empty", fingerprint="same")["status"], "retry")
        self.assertEqual(run.resolve_failure("query", "empty", fingerprint="same")["status"], "duplicate")
        result = run.resolve_failure("query", "empty", fingerprint="different")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "not_found")


class SkillSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def _state_with_tasks(self, tasks: list[dict]) -> RuntimeState:
        state = RuntimeState({"step_budget": 12, "mode": "single_turn"}, set(), 12)
        state.task_graph = {"tasks": tasks}
        self.agent._initialize_task_runtimes(state)
        self.agent.skill_scheduler.initialize(state, {})
        return state

    def _ready_only(self, state: RuntimeState, node_id: str) -> TaskRuntime:
        runtime = state.task_runtimes[0]
        for node in runtime.skill_run.nodes:
            if node["id"] != node_id:
                runtime.skill_run.mark_completed(node["id"], source="test")
        self.agent._activate_task_runtime_view(state, runtime)
        return runtime

    def test_same_domain_reads_are_runnable_before_write_barrier(self) -> None:
        state = self._state_with_tasks(
            [
                {"task_id": "t1", "domain": "meetingroom", "capability": "meeting.extend", "intent": "extend_existing", "slots": {}, "write_after": []},
                {"task_id": "t2", "domain": "meetingroom", "capability": "meeting.participant_add", "intent": "participant_add", "slots": {}, "write_after": ["t1"]},
            ]
        )
        self.assertTrue(self.agent._task_dependencies_completed(state, state.task_runtimes[1], writes_only=False))
        self.assertFalse(self.agent._task_dependencies_completed(state, state.task_runtimes[1], writes_only=True))

    def test_unknown_capability_is_blocked_without_fallback(self) -> None:
        state = self._state_with_tasks(
            [{"task_id": "t1", "domain": "unknown", "capability": "unknown.write", "intent": "unknown", "slots": {}}]
        )
        runtime = state.task_runtimes[0]
        self.assertEqual(runtime.status, "blocked")
        self.assertEqual(runtime.blocked_reason, "unsupported_capability")

    def test_handler_registry_covers_every_declared_node(self) -> None:
        missing = []
        for skill_id in self.agent.business_skill_registry.skills:
            for node in self.agent.business_skill_registry.definition(skill_id)["nodes"]:
                operation = node["operation"]
                if operation in {"read", "write", "postcheck"} and not self.agent.node_args_handlers.get(node["args_handler"]):
                    missing.append((skill_id, node["id"], "args"))
                if operation in {"compute", "reply"} and not self.agent.node_decision_handlers.get(node["decision_handler"]):
                    missing.append((skill_id, node["id"], "decision"))
                if not self.agent.node_validators.get(node["validator"]):
                    missing.append((skill_id, node["id"], "validator"))
        self.assertEqual(missing, [])

    def test_multi_segment_booking_emits_three_distinct_creates(self) -> None:
        state = self._state_with_tasks(
            [
                {
                    "id": "t1",
                    "domain": "meetingroom",
                    "capability": "meeting.book_multi_segments",
                    "intent": "book",
                    "slots": {
                        "day": "2026-07-25",
                        "day_text": "2026-07-25",
                        "capacity": 2,
                        "multi_segments": [
                            {"day": "2026-07-25", "start": "09:00", "end": "09:30", "title": "A"},
                            {"day": "2026-07-25", "start": "10:00", "end": "10:30", "title": "B"},
                            {"day": "2026-07-25", "start": "11:00", "end": "11:30", "title": "C"},
                        ],
                    },
                }
            ]
        )
        runtime = self._ready_only(state, "create")
        state.meetingroom.evidence["room_candidates"] = {
            "day": "2026-07-25",
            "rooms": [{"room_id": "R-1", "officeId": "O-1", "capacity": 8, "bookable": True, "busy_slots": []}],
        }
        calls = []
        for index in range(3):
            action = self.agent.skill_scheduler.next_action(state, {})
            self.assertIsNotNone(action)
            self.assertEqual(action.tool, "meetingroom.booking.create")
            calls.append(dict(action.args))
            self.agent._apply_tool_result(
                state,
                action.tool,
                action.args,
                {"success": True, "order_id": f"BK-{index + 1}"},
            )
        self.assertEqual([(item["start"], item["end"]) for item in calls], [("09:00", "09:30"), ("10:00", "10:30"), ("11:00", "11:30")])
        self.assertEqual(runtime.skill_run.statuses["create"], "completed")
        self.assertEqual(len(runtime.skill_run.invocations["create"]), 3)

    def test_participant_updates_repeat_for_add_and_remove(self) -> None:
        for capability, intent, tool in (
            ("meeting.participant_add", "participant_add", "meetingroom.booking.participant.add"),
            ("meeting.participant_remove", "participant_remove", "meetingroom.booking.participant.remove"),
        ):
            with self.subTest(capability=capability):
                state = self._state_with_tasks(
                    [
                        {
                            "id": "t1",
                            "domain": "meetingroom",
                            "capability": capability,
                            "intent": intent,
                            "slots": {
                                "order_id": "BK-1",
                                "participants": [
                                    {"user_id": "u1", "name": "A"},
                                    {"user_id": "u2", "name": "B"},
                                    {"user_id": "u3", "name": "C"},
                                ],
                            },
                        }
                    ]
                )
                runtime = self._ready_only(state, "update")
                user_ids = []
                for _ in range(3):
                    action = self.agent.skill_scheduler.next_action(state, {})
                    self.assertIsNotNone(action)
                    self.assertEqual(action.tool, tool)
                    user_ids.append(action.args["user_id"])
                    self.agent._apply_tool_result(
                        state,
                        action.tool,
                        action.args,
                        {"success": True, "order_id": "BK-1", "user_id": action.args["user_id"]},
                    )
                self.assertEqual(user_ids, ["u1", "u2", "u3"])
                self.assertEqual(runtime.skill_run.statuses["update"], "completed")
                self.assertEqual(len(runtime.skill_run.invocations["update"]), 3)

    def test_recurring_leave_save_waits_for_all_plans(self) -> None:
        state = self._state_with_tasks(
            [
                {
                    "id": "t1",
                    "domain": "workflow",
                    "capability": "workflow.leave_submit",
                    "intent": "leave",
                    "slots": {"submit": True},
                }
            ]
        )
        runtime = self._ready_only(state, "save")
        state.workflow.evidence["leave_plans"] = [
            {"day": "2026-07-25"},
            {"day": "2026-07-26"},
            {"day": "2026-07-27"},
        ]
        workflow_id = self.agent.workflow_registry.workflow_id("leave")

        def planner(current: RuntimeState) -> StepAction:
            index = len(current.workflow.evidence.get("saved_leave_requests") or [])
            return StepAction(
                "tool",
                "workflow.save",
                {
                    "workflow_id": workflow_id,
                    "submit": True,
                    "data": {
                        "start_time": f"2026-07-{25 + index} 09:00",
                        "end_time": f"2026-07-{25 + index} 10:00",
                        "leave_type": "annual",
                        "reason": "personal",
                        "duration": 1,
                    },
                    "approvers": ["u1"],
                },
            )

        self.agent.skill_capability_planners["workflow.leave_submit"] = planner
        starts = []
        for index in range(3):
            action = self.agent.skill_scheduler.next_action(state, {})
            self.assertIsNotNone(action)
            self.assertEqual(action.tool, "workflow.save")
            args = self.agent.tool_adapter.adapt(action.tool, self.agent._clean_args(action.args))
            starts.append(args["data"]["start_time"])
            self.agent._apply_tool_result(
                state,
                action.tool,
                args,
                {"draft_saved": True, "request_id": f"WF-{index + 1}"},
            )
        self.assertEqual(starts, ["2026-07-25 09:00", "2026-07-26 09:00", "2026-07-27 09:00"])
        self.assertEqual(runtime.skill_run.statuses["save"], "completed")
        self.assertEqual(len(runtime.skill_run.invocations["save"]), 3)

    def test_partial_success_projection_keeps_successful_task(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.task_results = [
            {"task_id": "t1", "domain": "meetingroom", "capability": "meeting.extend", "status": "completed", "result": {"status": "extended", "order_id": "BK-1"}},
            {"task_id": "t2", "domain": "workflow", "capability": "workflow.expense_submit", "status": "blocked", "result": {"status": "blocked", "reason": "ambiguous_project"}},
        ]
        answer = ResultProjectionRegistry().project(state)
        self.assertEqual(answer["booking_result"]["status"], "extended")
        self.assertEqual(answer["workflow_result"]["reason"], "ambiguous_project")


if __name__ == "__main__":
    unittest.main()
