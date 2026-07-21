from __future__ import annotations

import json
import unittest
from pathlib import Path

from submission.my_agent import (
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
        self.assertEqual([node["id"] for node in runtime.ready_nodes()], ["approver_search"])
        self.assertEqual(runtime.remaining_cost({"write", "postcheck"}), 2)


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

    def test_workspace_room_search_uses_building_evidence_first(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {"needs_workspace": True, "day": "2026-04-21", "capacity": 10}
        state.meetingroom.evidence["workspace"] = {"office_address": "0552_A1_4F"}
        candidates = self.agent._room_search_candidates(state)
        self.assertEqual(candidates[0], {"office_address": "0552_A1"})


if __name__ == "__main__":
    unittest.main()
