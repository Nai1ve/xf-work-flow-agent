from __future__ import annotations

import json
import unittest
from pathlib import Path

from submission.my_agent import (
    BusinessSkillRegistry,
    MyAgent,
    ReadTask,
    ResultProjectionRegistry,
    RuntimeState,
    TaskRuntime,
    ToolExecutionContext,
    ToolRegistry,
)
from submission.utils.skill_runtime import NodeDirective, SkillRun


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

    def test_semantic_unavailable_is_terminal_without_scheduler_stall(self) -> None:
        state = RuntimeState({"step_budget": 4, "mode": "single_turn"}, set(), 4)
        state.llm_semantic = {"semantic_error": "timed out", "task_graph": {"tasks": []}}
        self.assertTrue(self.agent._all_done(state))
        self.assertEqual(state.scheduler_stalls, [])

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

    def test_every_read_node_has_a_specific_registered_handler(self) -> None:
        missing = []
        for skill_id in self.agent.business_skill_registry.skills:
            for node in self.agent.business_skill_registry.definition(skill_id)["nodes"]:
                if node["operation"] not in {"read", "postcheck"}:
                    continue
                handler = str(node.get("args_handler") or "")
                if handler == "default_tool_args" or not self.agent.node_args_handlers.get(handler):
                    missing.append((skill_id, node["id"], handler))
        self.assertEqual(missing, [])

    def test_compute_transition_returns_to_read_planning_fixed_point(self) -> None:
        state = RuntimeState({"step_budget": 4, "mode": "single_turn"}, set(), 4)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "test.fixed_point", "intent": "test", "slots": {}}
        )
        runtime.skill_run = SkillRun(
            "test.fixed_point",
            {
                "nodes": [
                    {"id": "decide", "operation": "compute", "phase": "compute", "depends_on": [], "decision_handler": "test_fixed_point"},
                    {
                        "id": "lookup",
                        "operation": "read",
                        "phase": "read",
                        "depends_on": ["decide"],
                        "tool": "workflow.catalog",
                        "validator": "workflow.catalog_verified",
                    },
                ]
            },
            task_id="t1",
            capability="test.fixed_point",
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        self.agent.node_decision_handlers.register("test_fixed_point", lambda *_args: NodeDirective("passed"))

        decision = self.agent.skill_scheduler.next_action(state, {})

        self.assertIsNone(decision.action)
        self.assertTrue(decision.progressed)
        self.assertTrue(runtime.skill_run.is_ready("lookup"))

    def test_exhausted_budget_skips_only_ready_postcheck_after_write(self) -> None:
        state = RuntimeState({"step_budget": 1, "mode": "single_turn"}, set(), 1)
        state.steps_used = 1
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "workflow.leave_draft", "intent": "leave", "slots": {}}
        )
        runtime.skill_run = SkillRun(
            "test.postcheck",
            {
                "nodes": [
                    {"id": "save", "operation": "write", "phase": "write", "depends_on": []},
                    {"id": "verify", "operation": "postcheck", "phase": "postcheck", "depends_on": ["save"]},
                ]
            },
            task_id="t1",
            capability=runtime.capability,
        )
        runtime.skill_run.mark_completed("save", source="test")
        state.task_runtimes = [runtime]
        state.skill_runs = {runtime.task_id: runtime.skill_run}
        state.active_task_ids = {"workflow": runtime.task_id}

        progressed = self.agent.skill_scheduler.settle_exhausted_postchecks(state, {})

        self.assertTrue(progressed)
        self.assertEqual(runtime.skill_run.statuses["verify"], "skipped")
        self.assertEqual(runtime.status, "completed")

    def test_compute_progresses_after_tool_budget_is_exhausted(self) -> None:
        state = RuntimeState({"step_budget": 1, "mode": "single_turn"}, set(), 1)
        state.steps_used = 1
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "test.compute", "intent": "test", "slots": {}}
        )
        runtime.skill_run = SkillRun(
            "test.compute",
            {
                "nodes": [
                    {"id": "read", "operation": "read", "phase": "read", "depends_on": []},
                    {
                        "id": "compute",
                        "operation": "compute",
                        "phase": "compute",
                        "depends_on": ["read"],
                        "decision_handler": "test_budget_compute",
                    },
                ]
            },
            task_id="t1",
            capability="test.compute",
        )
        runtime.skill_run.mark_completed("read", source="test")
        state.task_runtimes = [runtime]
        state.skill_runs = {"t1": runtime.skill_run}
        state.active_task_ids = {"workflow": "t1"}
        self.agent.node_decision_handlers.register("test_budget_compute", lambda *_args: NodeDirective("passed"))

        decision = self.agent.skill_scheduler.next_action(state, {})

        self.assertIsNone(decision.action)
        self.assertTrue(decision.progressed)
        self.assertEqual(runtime.skill_run.statuses["compute"], "completed")

    def test_read_failure_retries_and_only_success_completes_owner(self) -> None:
        responses = [{"error": "temporary tool failure"}, {"projects": [{"project_code": "P1", "wbs_code": "W1"}]}]
        env = type("Env", (), {"call_tool": lambda _self, _tool, _args: responses.pop(0)})()
        agent = MyAgent(env)
        state = RuntimeState({"step_budget": 3}, {"workflow.project_search"}, 3)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "workflow.expense_submit", "intent": "expense_material", "slots": {}}
        )
        runtime.skill_run = SkillRun(
            "test.read_retry",
            {
                "nodes": [
                    {
                        "id": "project",
                        "operation": "read",
                        "phase": "read",
                        "tool": "workflow.project_search",
                        "depends_on": [],
                        "failure_edges": {
                            "project_search_error": {"action": "retry", "target": "project", "max_attempts": 1}
                        },
                    }
                ]
            },
            task_id="t1",
            capability=runtime.capability,
        )
        state.task_runtimes = [runtime]
        state.skill_runs = {"t1": runtime.skill_run}
        state.active_task_ids = {"workflow": "t1"}
        task = ReadTask(
            agent._read_task_key("workflow.project_search", {"project_name": "alpha"}),
            "workflow.project_search",
            {"project_name": "alpha"},
            "workflow",
            owner_task_id="t1",
            owner_node_id="project",
        )
        logs = []
        agent._debug_log = lambda _config, payload: logs.append(payload)

        agent._execute_read_task(state, task, {})
        self.assertNotIn(task.task_key, state.read_task_keys_completed)
        self.assertEqual(runtime.skill_run.statuses["project"], "pending")
        self.assertFalse(logs[-1]["success"])

        agent._execute_read_task(state, task, {})
        self.assertIn(task.task_key, state.read_task_keys_completed)
        self.assertEqual(state.read_tasks_failed, 1)
        self.assertEqual(state.read_tasks_retried, 1)

    def test_empty_read_is_completed_once_without_identical_retry(self) -> None:
        calls = []
        env = type("Env", (), {"call_tool": lambda _self, tool, args: calls.append((tool, args)) or {"projects": []}})()
        agent = MyAgent(env)
        state = RuntimeState({"step_budget": 3}, {"workflow.project_search"}, 3)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "workflow.expense_submit", "intent": "expense_material", "slots": {}}
        )
        runtime.skill_run = SkillRun(
            "test.empty",
            {
                "nodes": [
                    {
                        "id": "project",
                        "operation": "read",
                        "phase": "read",
                        "tool": "workflow.project_search",
                        "depends_on": [],
                        "failure_edges": {"empty_project_result": {"action": "retry", "target": "project", "max_attempts": 2}},
                    }
                ]
            },
            task_id="t1",
            capability=runtime.capability,
        )
        state.task_runtimes = [runtime]
        state.skill_runs = {"t1": runtime.skill_run}
        state.active_task_ids = {"workflow": "t1"}
        task = ReadTask(
            agent._read_task_key("workflow.project_search", {"project_name": "missing"}),
            "workflow.project_search",
            {"project_name": "missing"},
            "workflow",
            owner_task_id="t1",
            owner_node_id="project",
        )

        agent._execute_read_task(state, task, {})

        self.assertEqual(len(calls), 1)
        self.assertIn(task.task_key, state.read_task_keys_completed)
        self.assertFalse(agent._read_task_allowed(state, task))

    def test_deduplicated_read_fans_out_to_each_owner(self) -> None:
        env = type(
            "Env",
            (),
            {"call_tool": lambda _self, _tool, _args: {"users": [{"user_id": "U1", "employee_no": "E1"}]}},
        )()
        agent = MyAgent(env)
        state = RuntimeState({"step_budget": 3}, {"user.get_info"}, 3)
        runtimes = []
        for task_id in ("t1", "t2"):
            runtime = TaskRuntime(
                {"task_id": task_id, "domain": "workflow", "capability": "workflow.leave_submit", "intent": "leave", "slots": {}}
            )
            runtime.skill_run = SkillRun(
                "test.owner",
                {"nodes": [{"id": "applicant", "operation": "read", "phase": "read", "tool": "user.get_info", "depends_on": []}]},
                task_id=task_id,
                capability=runtime.capability,
            )
            runtimes.append(runtime)
        state.task_runtimes = runtimes
        state.skill_runs = {runtime.task_id: runtime.skill_run for runtime in runtimes}
        state.active_task_ids = {"workflow": "t1"}
        task = ReadTask(
            agent._read_task_key("user.get_info", {}),
            "user.get_info",
            {},
            "workflow",
            owners=[
                {"task_id": "t1", "node_id": "applicant", "domain": "workflow"},
                {"task_id": "t2", "node_id": "applicant", "domain": "workflow"},
            ],
        )

        agent._execute_read_task(state, task, {})

        self.assertEqual(runtimes[0].local_evidence["applicant"]["user_id"], "U1")
        self.assertEqual(runtimes[1].local_evidence["applicant"]["user_id"], "U1")
        self.assertEqual(len(state.read_task_owner_keys_completed), 2)
        self.assertEqual(state.ledger.summary()["reads"], 1)

    def test_user_lookup_evidence_is_isolated_by_owner_domain(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        state = RuntimeState({"step_budget": 3}, set(), 3)
        workflow = TaskRuntime(
            {"task_id": "wf", "domain": "workflow", "capability": "workflow.leave_submit", "intent": "leave", "slots": {}}
        )
        meeting = TaskRuntime(
            {
                "task_id": "mr",
                "domain": "meetingroom",
                "capability": "meeting.participant_add",
                "intent": "participant_add",
                "slots": {"participants": [{"name": "Alice"}]},
            }
        )
        state.task_runtimes = [workflow, meeting]
        state.active_task_ids = {"workflow": "wf", "meetingroom": "mr"}

        agent._apply_tool_result(
            state,
            "user.get_info",
            {},
            {"users": [{"user_id": "APPLICANT", "employee_no": "E1"}]},
            ToolExecutionContext(owner_task_id="wf", owner_node_id="applicant", domain="workflow"),
        )
        agent._apply_tool_result(
            state,
            "user.get_info",
            {"keyword": "Alice"},
            {"users": [{"user_id": "PARTICIPANT", "name": "Alice"}]},
            ToolExecutionContext(
                owner_task_id="mr",
                owner_node_id="resolve_people",
                domain="meetingroom",
                metadata={"participant_index": 0},
            ),
        )

        self.assertEqual(workflow.local_evidence["applicant"]["user_id"], "APPLICANT")
        self.assertNotIn("participant_user_0", workflow.local_evidence)
        self.assertEqual(meeting.local_evidence["participant_user_0"]["users"][0]["user_id"], "PARTICIPANT")
        self.assertNotIn("applicant", meeting.local_evidence)

    def test_task_terminal_projection_prefers_local_evidence(self) -> None:
        state = RuntimeState({"step_budget": 3}, set(), 3)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "meetingroom", "capability": "meeting.query_booking", "intent": "query_booking", "slots": {"day": "2026-07-28"}}
        )
        runtime.status = "completed"
        runtime.local_evidence = {"booking_query": {"bookings": []}}
        state.meetingroom.evidence = {"booking_query": {"bookings": [{"order_id": "OTHER"}]}}

        result = self.agent._task_terminal_result(state, runtime)

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["bookings"], [])

    def test_validator_does_not_call_llm_tool_or_mutate_evidence(self) -> None:
        state = self._state_with_tasks(
            [{"id": "t1", "domain": "workflow", "capability": "workflow.leave_submit", "intent": "leave", "slots": {}}]
        )
        runtime = state.task_runtimes[0]
        node = next(item for item in runtime.skill_run.nodes if item["id"] == "leave_form")
        state.workflow.evidence["leave_plan"] = {
            "start_time": "2026-07-25 09:00",
            "end_time": "2026-07-25 18:00",
            "leave_type": "annual",
            "reason": "personal",
            "duration": 8,
        }
        before = json.dumps(state.workflow.evidence, ensure_ascii=False, sort_keys=True)
        self.agent._chat_completion = lambda *_args, **_kwargs: self.fail("validator called LLM")
        self.agent.env.call_tool = lambda *_args, **_kwargs: self.fail("validator called tool")

        result = self.agent._validate_registered_skill_node(state, runtime, node)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(json.dumps(state.workflow.evidence, ensure_ascii=False, sort_keys=True), before)

    def test_compute_decision_is_cached_by_stable_input_hash(self) -> None:
        state = RuntimeState({"step_budget": 4, "mode": "single_turn"}, set(), 4)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "workflow.leave_submit", "intent": "leave", "slots": {}}
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        calls = []
        self.agent._compute_skill_node = lambda *_args: calls.append("compute") or ""
        self.agent._validate_registered_skill_node = lambda *_args: self.agent._skill_validation("passed", evidence_refs=["cached"])
        node = {"id": "leave_form", "decision_handler": "default_compute"}

        first = self.agent._default_skill_compute(state, runtime, node)
        second = self.agent._default_skill_compute(state, runtime, node)

        self.assertEqual((first.status, second.status), ("passed", "passed"))
        self.assertEqual(calls, ["compute"])

    def test_slot_resolver_handles_ranges_all_day_and_duration(self) -> None:
        cases = [
            ("明天全天请病假，审批人李经理", "2026-05-19", "2026-05-19", "09:00", "18:00"),
            ("5月19日到5月23日请病假，审批人李经理", "2026-05-19", "2026-05-23", "09:00", "18:00"),
            ("下个月6号到11号请病假，审批人李经理", "2026-06-06", "2026-06-11", "09:00", "18:00"),
            ("明天下午请两个小时病假，审批人李经理", "2026-05-19", "2026-05-19", "14:00", "16:00"),
        ]
        for query, day, end_day, start, end in cases:
            with self.subTest(query=query):
                state = RuntimeState(
                    {"now": "2026-05-18T09:00:00+08:00", "user_query": query, "mode": "single_turn", "step_budget": 8},
                    set(),
                    8,
                )
                runtime = TaskRuntime(
                    {
                        "task_id": "t1",
                        "domain": "workflow",
                        "capability": "workflow.leave_submit",
                        "intent": "leave",
                        "source_text": query,
                        "slots": {"leave": {}},
                    }
                )
                state.task_runtimes = [runtime]
                state.active_task_ids = {"workflow": "t1"}

                self.agent.slot_resolver.resolve(state, runtime)

                leave = runtime.slots["leave"]
                self.assertEqual((leave["day"], leave["end_day"]), (day, end_day))
                self.assertEqual((leave["start"], leave["end"]), (start, end))

    def test_leave_collect_input_defers_business_enums_to_form_compute(self) -> None:
        query = "明天下午2点到6点请事假，审批人王芳，直接提交"
        state = RuntimeState(
            {"now": "2026-05-18T09:00:00+08:00", "user_query": query, "mode": "single_turn", "step_budget": 8},
            set(),
            8,
        )
        runtime = TaskRuntime(
            {
                "task_id": "t1",
                "domain": "workflow",
                "capability": "workflow.leave_submit",
                "intent": "leave",
                "source_text": query,
                "slots": {"leave": {"approver_keyword": "王芳"}},
            }
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}

        self.agent.slot_resolver.resolve(state, runtime)

        self.assertEqual(self.agent._missing_task_slots(state, runtime), [])
        self.assertNotIn("leave_type_label", runtime.slots["leave"])

    def test_leave_form_maps_literal_type_and_schema_singleton_reason(self) -> None:
        query = "明天下午2点到6点请事假，审批人王芳，直接提交"
        state = RuntimeState(
            {"now": "2026-05-18T09:00:00+08:00", "user_query": query, "mode": "single_turn", "step_budget": 8},
            set(),
            8,
        )
        runtime = TaskRuntime(
            {
                "task_id": "t1",
                "domain": "workflow",
                "capability": "workflow.leave_submit",
                "intent": "leave",
                "source_text": query,
                "slots": {"leave": {"approver_keyword": "王芳"}},
            }
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        self.agent.slot_resolver.resolve(state, runtime)
        self.agent._chat_completion = lambda *_args, **_kwargs: self.fail("schema-grounded singleton used LLM")

        plan = self.agent._leave_plan(state)

        self.assertEqual(plan["leave_type"], "L")
        self.assertEqual(plan["reason"], "10")

    def test_single_turn_expense_defers_material_enum_to_schema_nodes(self) -> None:
        state = RuntimeState(
            {"user_query": "为办公空间升级项目申请办公设备采购，总预算3万元", "mode": "single_turn", "step_budget": 10},
            set(),
            10,
        )
        runtime = TaskRuntime(
            {
                "task_id": "t1",
                "domain": "workflow",
                "capability": "workflow.expense_submit",
                "intent": "expense_material",
                "slots": {"expense": {"project_name": "办公空间升级项目", "total_amount": "30000.00"}},
            }
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        state.workflow.slots = runtime.slots

        self.assertEqual(self.agent._missing_task_slots(state, runtime), [])

    def test_multi_turn_expense_collects_each_missing_business_input(self) -> None:
        state = RuntimeState({"mode": "multi_turn", "step_budget": 14}, set(), 14)
        runtime = TaskRuntime(
            {
                "task_id": "t1",
                "domain": "workflow",
                "capability": "workflow.expense_draft",
                "intent": "expense_material",
                "slots": {"expense": {"project_code": "D-260100004", "material_category_hint": "办公设备"}},
            }
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        state.workflow.slots = runtime.slots

        self.assertEqual(self.agent._missing_task_slots(state, runtime), ["material_subclass"])

    def test_reply_availability_uses_only_public_multi_turn_contract(self) -> None:
        env = type("Env", (), {"reply": lambda _self, _message: {"resolved_slot": None, "user_message": "不知道"}})()
        agent = MyAgent(env)
        state = RuntimeState({"mode": "multi_turn", "step_budget": 3}, set(), 3)
        runtime = TaskRuntime(
            {"task_id": "t1", "domain": "workflow", "capability": "workflow.leave_submit", "intent": "leave", "slots": {}}
        )
        state.task_runtimes = [runtime]
        state.active_task_ids = {"workflow": "t1"}
        state.asked_slots.add("approver")
        self.assertTrue(agent._reply_available(state, "请提供审批人"))

        agent._apply_reply_result(state, "请提供审批人", env.reply("请提供审批人"))

        self.assertEqual(runtime.status, "blocked")
        self.assertEqual(runtime.blocked_reason, "unresolved_user_input")
        self.assertFalse(agent._reply_available(state, "请提供审批人"))
        state.obs["mode"] = "single_turn"
        self.assertFalse(agent._reply_available(state, "另一条消息"))

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
        state.meetingroom.evidence["pending_selected_room"] = state.meetingroom.evidence["room_candidates"]["rooms"][0]
        calls = []
        for index in range(3):
            action = self.agent.skill_scheduler.next_action(state, {}).action
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
                    action = self.agent.skill_scheduler.next_action(state, {}).action
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
            {"start_time": "2026-07-25 09:00", "end_time": "2026-07-25 10:00", "leave_type": "annual", "reason": "personal", "duration": 1},
            {"start_time": "2026-07-26 09:00", "end_time": "2026-07-26 10:00", "leave_type": "annual", "reason": "personal", "duration": 1},
            {"start_time": "2026-07-27 09:00", "end_time": "2026-07-27 10:00", "leave_type": "annual", "reason": "personal", "duration": 1},
        ]
        workflow_id = self.agent.workflow_registry.workflow_id("leave")
        state.workflow.evidence["applicant"] = {"user_id": "applicant", "employee_no": "E1"}
        state.workflow.evidence["selected_approver"] = {"user_id": "u1"}
        state.workflow.evidence["workflow_skill_draft_ir"] = {
            "workflow_id": workflow_id,
            "submit": True,
            "save_args": self.agent._leave_save_args(
                state,
                state.workflow.evidence["leave_plans"][0],
                state.workflow.evidence["selected_approver"],
            ),
        }
        starts = []
        for index in range(3):
            action = self.agent.skill_scheduler.next_action(state, {}).action
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
