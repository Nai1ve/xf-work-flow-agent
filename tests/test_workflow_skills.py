from __future__ import annotations

import json
import unittest
from pathlib import Path

from submission.my_agent import (
    CaseCalendar,
    MyAgent,
    OutcomePolicyMemory,
    RuntimeState,
    WorkflowSkillRegistry,
    WorkflowSkillRuntime,
)


ROOT = Path(__file__).resolve().parents[1]


def load_index(name: str) -> dict:
    return json.loads((ROOT / "submission" / "static_context" / name).read_text(encoding="utf-8"))


class WorkflowSkillRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = WorkflowSkillRegistry(load_index("workflow_skills.index.json"))

    def test_submit_skill_replaces_only_postcheck_node(self) -> None:
        skill_id, definition = self.registry.select("expense_material", True)
        self.assertEqual(skill_id, "workflow.expense.submit")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["verify"]["tool"], "oa.done.list")
        self.assertEqual(by_id["project"]["depends_on"], ["applicant", "catalog", "schema"])

    def test_replace_leave_skill_has_delete_barrier(self) -> None:
        skill_id, definition = self.registry.select("leave", True, replace=True)
        self.assertEqual(skill_id, "workflow.leave.replace_submit")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["delete_source"]["depends_on"], ["source_lookup"])
        self.assertIn("delete_source", by_id["catalog"]["depends_on"])

    def test_runtime_exposes_only_dependency_ready_nodes(self) -> None:
        _, definition = self.registry.select("leave", False)
        runtime = WorkflowSkillRuntime("workflow.leave.draft", definition)
        self.assertEqual(
            {node["id"] for node in runtime.ready_nodes()},
            {"applicant", "catalog", "schema"},
        )
        runtime.sync_completed({"applicant", "catalog", "schema", "leave_form"})
        self.assertEqual([node["id"] for node in runtime.ready_nodes()], ["attachment"])
        runtime.sync_completed({"attachment"})
        self.assertEqual([node["id"] for node in runtime.ready_nodes()], ["approver_search"])
        self.assertEqual(runtime.remaining_cost({"write", "postcheck"}), 2)

    def test_all_skill_contracts_are_valid(self) -> None:
        self.assertEqual(len(self.registry.skills), 15)
        self.assertEqual(self.registry.validate_contracts(), [])
        self.assertEqual(
            self.registry.validate_capability_coverage(load_index("capabilities.index.json")),
            [],
        )

    def test_schedule_query_does_not_select_booking_skill(self) -> None:
        skill_id, definition = self.registry.select_meetingroom("query_room_schedule")
        self.assertEqual(skill_id, "meetingroom.schedule_query")
        self.assertEqual([node["id"] for node in definition["nodes"]], ["query_schedule"])

    def test_rebook_skill_selects_replacement_before_cancel(self) -> None:
        skill_id, definition = self.registry.select_meetingroom("cancel_rebook_existing")
        self.assertEqual(skill_id, "meetingroom.rebook")
        by_id = {node["id"]: node for node in definition["nodes"]}
        self.assertEqual(by_id["query_rooms"]["depends_on"], ["locate"])
        self.assertEqual(by_id["select_room"]["depends_on"], ["query_rooms"])
        self.assertEqual(by_id["cancel"]["depends_on"], ["select_room"])
        self.assertEqual(by_id["create"]["depends_on"], ["cancel"])
        runtime = WorkflowSkillRuntime(skill_id, definition)
        self.assertEqual(runtime.remaining_cost({"write", "postcheck"}), 2)

    def test_participant_update_accepts_add_and_remove_tools(self) -> None:
        _, definition = self.registry.select_meetingroom("participant_remove")
        update = next(node for node in definition["nodes"] if node["id"] == "update")
        self.assertEqual(
            update["tools"],
            ["meetingroom.booking.participant.add", "meetingroom.booking.participant.remove"],
        )

    def test_validation_waits_for_dependencies(self) -> None:
        definition = {
            "nodes": [
                {"id": "read", "depends_on": []},
                {"id": "write", "depends_on": ["read"]},
            ]
        }
        runtime = WorkflowSkillRuntime("test", definition)
        result = runtime.apply_validation("write", {"status": "failed", "reason": "bad", "fingerprint": "bad-1"})
        self.assertEqual(result["validation"], "failed_waiting_dependencies")
        self.assertEqual(runtime.statuses["write"], "pending")
        self.assertFalse(runtime.blocked_reason)

    def test_failure_retry_is_deduplicated_and_exhausts(self) -> None:
        definition = {
            "nodes": [
                {
                    "id": "query",
                    "depends_on": [],
                    "failure_edges": {
                        "empty": {
                            "action": "retry",
                            "target": "query",
                            "max_attempts": 1,
                            "exhausted_reason": "not_found",
                        }
                    },
                },
                {"id": "write", "depends_on": ["query"], "failure_edges": {"*": {"action": "block"}}},
            ]
        }
        runtime = WorkflowSkillRuntime("test", definition)
        runtime.mark_running("query")
        self.assertEqual(runtime.resolve_failure("query", "empty", fingerprint="same")["status"], "retry")
        self.assertEqual(runtime.resolve_failure("query", "empty", fingerprint="same")["status"], "duplicate")
        self.assertEqual(runtime.retry_counts["query"], 1)
        runtime.mark_running("query")
        result = runtime.resolve_failure("query", "empty", fingerprint="different")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "not_found")

    def test_subclass_failure_reopens_category_and_downstream_nodes(self) -> None:
        _, definition = self.registry.select("expense_material", False)
        runtime = WorkflowSkillRuntime("workflow.expense.draft", definition)
        for node_id in ["applicant", "catalog", "schema", "project", "category", "subclass", "draft_ir"]:
            runtime.statuses[node_id] = "completed"
        result = runtime.resolve_failure("subclass", "empty_subclass_result", fingerprint="empty-1")
        self.assertEqual(result["status"], "retry")
        self.assertEqual(result["target"], "category")
        self.assertEqual(runtime.statuses["category"], "pending")
        self.assertEqual(runtime.statuses["subclass"], "pending")
        self.assertEqual(runtime.statuses["draft_ir"], "pending")


class OutcomePolicyMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = OutcomePolicyMemory(load_index("outcome_policies.index.json"))

    def test_missing_approver_lookup_is_next_action_not_terminal(self) -> None:
        decision = self.memory.match(
            "workflow.leave",
            {
                "approver_hint_present": True,
                "approver_search_completed": False,
                "approver_candidate_count": 0,
            },
        )
        self.assertEqual(decision["decision"], "next_action")
        self.assertEqual(decision["action"], "workflow.search_person")

    def test_explicit_multi_amount_reason_has_priority(self) -> None:
        decision = self.memory.match(
            "workflow.expense_material",
            {
                "subclass_query_completed": True,
                "specific_material_evidence": False,
                "save_completed": False,
                "explicit_multi_item_total_only": True,
            },
        )
        self.assertEqual(decision["reason"], "insufficient_amount_breakdown")

    def test_no_bookable_room_is_canonical(self) -> None:
        decision = self.memory.match(
            "meetingroom",
            {"room_query_completed": True, "bookable_room_count": 0, "booking_completed": False},
        )
        self.assertEqual(decision["reason"], "no_bookable_room")


class WorkflowSkillAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_approver_planning_does_not_consume_attempt(self) -> None:
        state = RuntimeState({"step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "leave"
        state.workflow.slots = {"leave": {"approver_keyword": "赵丽"}}
        plan = {"approver_keyword": "赵丽", "approver_title": "", "approver_employee_no": ""}
        first = self.agent._next_approver_search_args(state, plan)
        second = self.agent._next_approver_search_args(state, plan)
        self.assertEqual(first, second)
        self.assertEqual(state.workflow.evidence["approver_search_tried"], [])

    def test_replace_request_selects_target_leave_type_and_skill(self) -> None:
        query = "我昨天请了病假，想把今天下午改成事假，审批人王芳，帮我提交。"
        workflow = self.agent._heuristic_workflow(query, {})
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "leave"
        state.workflow.slots = workflow
        self.agent._normalize_workflow_slots(state)
        self.agent._initialize_workflow_skill(state)
        self.assertEqual(self.agent._leave_slots(state)["leave_type_label"], "事假")
        self.assertEqual(state.workflow_skill.skill_id, "workflow.leave.replace_submit")

    def test_expense_phrase_with_intervening_business_name_is_submit(self) -> None:
        query = "另外我需要提品牌广告服务费用，项目是城市服务大模型发布活动项目。"
        self.assertTrue(self.agent._submit_intent(query))

    def test_project_review_meeting_title_is_canonical(self) -> None:
        self.assertEqual(self.agent._normalize_meeting_title("项目复盘会"), "项目复盘")

    def test_workspace_room_search_uses_floor_evidence_first(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {"needs_workspace": True, "day": "2026-04-21", "capacity": 10}
        state.meetingroom.evidence["workspace"] = {"office_address": "0552_A1_4F"}
        candidates = self.agent._room_search_candidates(state)
        self.assertEqual(candidates[0], {"office_address": "0552_A1_4F"})

    def test_workspace_room_search_expands_floor_building_then_campus(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {
            "needs_workspace": True,
            "day": "2026-04-21",
            "capacity": 10,
            "location_constraint": "preference",
            "search_scopes": ["exact_floor", "same_building", "same_campus"],
        }
        state.meetingroom.evidence["workspace"] = {"office_address": "0552_A2_1F"}
        self.assertEqual(
            self.agent._room_search_candidates(state)[:3],
            [
                {"office_address": "0552_A2_1F"},
                {"office_address": "0552_A2"},
                {"office_address": "0552"},
            ],
        )

    def test_hard_floor_room_search_does_not_broaden(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {
            "office_candidates": ["A1"],
            "office_address_candidates": ["0552_A1_4F"],
            "location_constraint": "hard",
            "search_scopes": ["exact_floor"],
        }
        self.assertEqual(
            self.agent._room_search_candidates(state),
            [{"office_address": "0552_A1_4F"}],
        )

    def test_equivalent_building_queries_are_deduplicated(self) -> None:
        query = "优先小镇A1四楼，不行就A2，订明天下午2点到3点会议室"
        state = RuntimeState(
            {"user_query": query, "now": "2026-04-20T10:00:00+08:00", "step_budget": 8},
            set(),
            8,
        )
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = self.agent._heuristic_meetingroom(query, state.obs)
        self.agent._normalize_meeting_slots(state)
        candidates = self.agent._room_search_candidates(state)
        self.assertEqual(
            candidates[:3],
            [
                {"office_address": "0552_A1_4F"},
                {"office_address": "0552_A2"},
                {"office_address": "0552"},
            ],
        )
        self.assertNotIn({"office_id": "A1"}, candidates)
        self.assertNotIn({"office_id": "A2"}, candidates)

    def test_rebook_read_plan_reserves_both_writes(self) -> None:
        state = RuntimeState({"step_budget": 5}, set(), 5)
        state.meetingroom.needed = True
        state.meetingroom.intent = "cancel_rebook_existing"
        self.agent._initialize_meetingroom_skill(state)
        self.assertEqual(self.agent._read_plan_step_reserve(state), 2)
        self.assertEqual(state.step_budget - state.steps_used - self.agent._read_plan_step_reserve(state), 3)

    def test_case_calendar_has_no_machine_date_fallback(self) -> None:
        calendar = CaseCalendar(None)
        self.assertEqual(calendar.resolve_day("明天"), "")
        self.assertEqual(calendar.month_day(20), "")
        self.assertEqual(calendar.next_meeting_business_day(), "")
        self.assertIsNone(calendar.week_start())

    def test_case_calendar_resolves_relative_dates_from_observation(self) -> None:
        calendar = CaseCalendar("2026-04-18T10:00:00+08:00")
        self.assertEqual(calendar.resolve_day("明天"), "2026-04-19")
        self.assertEqual(calendar.resolve_day("明天", prefer_workday=True), "2026-04-20")
        self.assertEqual(calendar.resolve_day("下周二"), "2026-04-21")

    def test_explicit_order_id_satisfies_booking_location_validator(self) -> None:
        state = RuntimeState({"user_query": "查看订单 BK-100 的参会人", "step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "participant_list"
        state.meetingroom.slots = {"order_id": "BK-100"}
        self.agent._initialize_meetingroom_skill(state)
        self.assertEqual(state.meetingroom_skill.statuses["locate"], "completed")
        self.assertEqual(state.meetingroom_skill.next_ready_id(), "read_participants")

    def test_schedule_query_finishes_without_booking_write(self) -> None:
        state = RuntimeState({"user_query": "查询A1-3F-349本周日程", "step_budget": 3}, set(), 3)
        state.meetingroom.needed = True
        state.meetingroom.intent = "query_room_schedule"
        state.meetingroom.slots = {"room_ids": ["A1-3F-349"]}
        first = self.agent._next_schedule_book_action(state)
        self.assertEqual(first.tool, "meetingroom.room.schedule")
        state.meetingroom.evidence["schedules"] = {
            "A1-3F-349": {"room_id": "A1-3F-349", "busy_slots": []}
        }
        second = self.agent._next_schedule_book_action(state)
        self.assertIsNone(second)
        self.assertEqual(state.meetingroom.status, "done")
        self.assertEqual(state.meetingroom.result["status"], "queried")

    def test_room_schedule_query_and_comparison_are_distinct_intents(self) -> None:
        query = self.agent._heuristic_meetingroom(
            "查询A1-3F-349会议室5月11日到5月15日的预订情况",
            {},
        )
        compare = self.agent._heuristic_meetingroom(
            "看看A3-3F-311和A3-3F-312哪个更空闲，选空闲的订周四下午2点到4点",
            {},
        )
        self.assertEqual(query["intent"], "query_room_schedule")
        self.assertEqual(compare["intent"], "book_by_schedule_analysis")

    def test_schedule_range_preserves_explicit_date_interval(self) -> None:
        state = RuntimeState(
            {"user_query": "查询会议室5月11日到5月15日的日程", "now": "2026-05-11T09:00:00+08:00", "step_budget": 3},
            set(),
            3,
        )
        state.meetingroom.slots = {"day_text": "5月11日到5月15日"}
        self.assertEqual(self.agent._schedule_range(state), ("2026-05-11", "2026-05-15"))

    def test_unsuccessful_write_result_uses_failure_edge(self) -> None:
        state = RuntimeState({"step_budget": 3}, set(), 3)
        state.meetingroom.needed = True
        state.meetingroom.intent = "book_single"
        _, definition = self.agent.workflow_skill_registry.select_meetingroom("book_single")
        state.meetingroom_skill = WorkflowSkillRuntime("meetingroom.book", definition)
        state.meetingroom_skill.statuses["query_rooms"] = "completed"
        state.meetingroom_skill.statuses["select_room"] = "completed"
        state.meetingroom_skill.mark_running("create")
        self.agent._record_skill_tool_outcome(
            state,
            "meetingroom.booking.create",
            {"office_id": "ROOM-1"},
            {"success": False},
        )
        self.assertEqual(state.meetingroom_skill.statuses["create"], "blocked")
        self.assertEqual(state.meetingroom_skill.blocked_reason, "booking_create_failed")


if __name__ == "__main__":
    unittest.main()
