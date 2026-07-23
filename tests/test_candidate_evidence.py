from __future__ import annotations

import unittest
from unittest.mock import patch

from submission.my_agent import MyAgent, ReadTask, ResultProjectionRegistry, RuntimeState


class CandidateEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_project_structure_word_is_last_resort(self) -> None:
        query = "项目是星火质量工程平台，申请测试设备8000元。"
        expense = self.agent._heuristic_expense(query)
        expense["source_text"] = query
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"source_text": query, "expense": expense}
        variants = self.agent._project_search_fanout_args(
            state,
            expense,
            {"project_name": "星火质量工程平台"},
        )
        self.assertEqual(variants[0], {"project_name": "星火质量工程平台"})
        self.assertTrue(all(item["project_name"] not in {"项目", "平台", "工程"} for item in variants[:2]))

    def test_cross_domain_scheduler_keeps_both_domains(self) -> None:
        state = RuntimeState({"step_budget": 10}, set(), 10)
        state.meetingroom.needed = True
        state.workflow.needed = True
        tasks = [
            ReadTask("meeting", "meetingroom.booking.list", {}, "meetingroom"),
            ReadTask("workflow", "workflow.catalog", {}, "workflow"),
        ]
        selected = self.agent._fair_read_batch(state, tasks, 2)
        self.assertEqual({task.domain for task in selected}, {"meetingroom", "workflow"})

    def test_expense_slice_excludes_meeting_time_amount(self) -> None:
        query = (
            "帮我订下周二下午2点到3点小镇A1四楼6人会议室，主题项目复盘。"
            "另外我需要提交品牌广告服务费用，项目还是城市服务大模型发布活动项目："
            "视频制作2条每条1.5万元，活动发布会1场4万元，总预算7万元。"
        )
        expense = self.agent._heuristic_expense(query)
        self.assertEqual(expense["project_name"], "城市服务大模型发布活动项目")
        self.assertEqual({item["name"] for item in expense["items"]}, {"视频制作", "活动发布会"})

    def test_exact_subclass_with_total_is_single_item(self) -> None:
        query = "知识服务项目要买1套测试设备，预算8000元，直接提交。"
        expense = self.agent._heuristic_expense(query)
        expense["source_text"] = query
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.slots = {"source_text": query, "expense": expense, "submit": True}
        options = [
            {"label": "电脑及其配件", "value": "computer"},
            {"label": "测试设备", "value": "test_device"},
        ]
        self.assertEqual(self.agent._expense_request_shape(state, expense, options), "single_item_total")

    def test_attachment_selection_is_bound_to_file_list_candidates(self) -> None:
        query = "请使用我的请假证明作为附件"
        state = RuntimeState({"user_query": query, "step_budget": 8}, set(), 8)
        state.workflow.intent = "leave"
        state.workflow.slots = {"source_text": query, "leave": {"leave_type_label": "事假"}}
        candidates = [
            {"name": "document-a.pdf", "path": "documents/document-a.pdf"},
            {"name": "document-b.pdf", "path": "documents/document-b.pdf"},
        ]
        state.workflow.evidence["file_list"] = {"directory": "documents", "files": candidates}
        with patch.object(
            self.agent,
            "_select_candidate_with_llm",
            return_value=candidates[1],
        ) as ranker:
            selected = self.agent._select_leave_attachment(state, {})
        self.assertEqual(selected, "documents/document-b.pdf")
        self.assertEqual(ranker.call_args.args[3], candidates)
        decision = next(entry for entry in state.ledger.entries if entry.get("event") == "candidate_decision")
        self.assertIn(decision["selected_id"], decision["allowed_candidate_ids"])

    def test_attachment_selection_rejects_path_outside_file_list(self) -> None:
        query = "请使用我的证明作为附件"
        state = RuntimeState({"user_query": query, "step_budget": 8}, set(), 8)
        state.workflow.intent = "leave"
        state.workflow.slots = {"source_text": query, "leave": {"leave_type_label": "事假"}}
        state.workflow.evidence["file_list"] = {
            "directory": "documents",
            "files": [{"name": "document.pdf", "path": "documents/document.pdf"}],
        }
        with patch.object(
            self.agent,
            "_select_candidate_with_llm",
            return_value={"name": "document.pdf", "path": "outside/document.pdf"},
        ):
            self.assertEqual(self.agent._select_leave_attachment(state, {}), "")

    def test_leave_preflight_accepts_exact_file_list_path(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.workflow.evidence["leave_attachment_directory"] = "documents"
        state.workflow.evidence["file_list"] = {
            "directory": "documents",
            "files": ["document.pdf"],
        }
        result = self.agent.preflight_guard.validate_write(
            state,
            "workflow.save",
            {
                "workflow_id": 72247,
                "submit": False,
                "data": {"attachment": "documents/document.pdf"},
            },
        )
        self.assertNotIn("leave_attachment_not_bound_to_file_list", result["errors"])


class ResultAndPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_cross_domain_projection_keeps_done_and_blocked_results(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.status = "done"
        state.meetingroom.result = {"status": "queried", "count": 2}
        state.workflow.needed = True
        state.workflow.status = "blocked"
        state.workflow.blocked_reason = "ambiguous_project"
        answer = ResultProjectionRegistry().project(state)
        self.assertEqual(answer["booking_result"]["count"], 2)
        self.assertEqual(answer["workflow_draft_result"]["status"], "blocked")
        self.assertEqual(answer["workflow_result"], answer["workflow_draft_result"])

    def test_booking_create_preflight_rejects_unlisted_or_busy_room(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.slots = {"capacity": 4}
        state.meetingroom.evidence["room_candidates"] = {
            "rooms": [
                {
                    "room_id": "ROOM-1",
                    "capacity": 6,
                    "bookable": True,
                    "busy_slots": [["14:00", "15:00"]],
                }
            ]
        }
        base = {"day": "2026-04-20", "start": "14:30", "end": "15:30", "title": "review"}
        unlisted = self.agent.preflight_guard.validate_write(
            state,
            "meetingroom.booking.create",
            {**base, "room_id": "ROOM-2", "office_id": "OFFICE-2"},
        )
        busy = self.agent.preflight_guard.validate_write(
            state,
            "meetingroom.booking.create",
            {**base, "room_id": "ROOM-1", "office_id": "OFFICE-1"},
        )
        self.assertIn("room_id_not_in_current_room_candidates", unlisted["errors"])
        self.assertIn("room_time_conflict", busy["errors"])

    def test_booking_create_preflight_requires_room_list_evidence(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.meetingroom.slots = {"capacity": 4}
        result = self.agent.preflight_guard.validate_write(
            state,
            "meetingroom.booking.create",
            {
                "day": "2026-04-20",
                "start": "14:00",
                "end": "15:00",
                "title": "review",
                "room_id": "ROOM-1",
                "office_id": "OFFICE-1",
            },
        )
        self.assertIn("room_candidates_missing", result["errors"])

    def test_failed_create_removes_room_and_allows_next_candidate(self) -> None:
        state = RuntimeState({"step_budget": 8}, set(), 8)
        state.steps_used = 3
        state.meetingroom.slots = {
            "day": "2026-04-20",
            "start": "14:00",
            "end": "15:00",
            "capacity": 4,
        }
        state.meetingroom.evidence["room_candidates"] = {
            "day": "2026-04-20",
            "rooms": [
                {"room_id": "ROOM-1", "capacity": 6, "bookable": True, "busy_slots": []},
                {"room_id": "ROOM-2", "capacity": 8, "bookable": True, "busy_slots": []},
            ],
        }
        self.agent._apply_meeting_tool_result(
            state,
            "meetingroom.booking.create",
            {"room_id": "ROOM-1"},
            {"success": False},
        )
        self.assertEqual(self.agent._select_room_for_booking(state)["room_id"], "ROOM-2")
        self.assertTrue(state.meetingroom.evidence["booking_retry_available"])


if __name__ == "__main__":
    unittest.main()
