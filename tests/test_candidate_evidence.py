from __future__ import annotations

import unittest
from unittest.mock import patch

from submission.my_agent import (
    MyAgent,
    ReadTask,
    ResultProjectionRegistry,
    RuntimeState,
    StepAction,
    ToolContractReconciler,
)


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

    def test_project_search_keeps_original_before_suffix_stripped_core(self) -> None:
        expense = {"project_name": "外包交付项目", "project_keywords": [], "raw_text": "外包交付项目"}
        self.assertEqual(self.agent._project_search_candidates(expense)[:2], ["外包交付项目", "外包交付"])

    def test_project_fanout_contains_formal_core_queries(self) -> None:
        for query, expected in [
            ("知识库改造项目要做官网专题图文页面，2.4万，帮我提交。", "知识库改造"),
            ("终端兼容性专项测试项目要买一批设备，总预算2万元，你先帮我提上去。", "终端兼容性专项测试"),
        ]:
            expense = self.agent._heuristic_expense(query)
            state = RuntimeState({"user_query": query, "step_budget": 12}, set(), 12)
            state.workflow.intent = "expense_material"
            state.workflow.slots = {"source_text": query, "expense": expense}
            first = self.agent._next_project_search_args(state, expense, allow_llm=False)
            variants = self.agent._project_search_fanout_args(state, expense, first)
            self.assertEqual(variants[1], {"project_name": expected})

    def test_business_object_and_project_phrase_form_bounded_query(self) -> None:
        query = "先帮我存一个外包数据服务草稿，项目是交付运营产品发布会，预算30000元，后面我还要确认采购周期。"
        expense = self.agent._heuristic_expense(query)
        state = RuntimeState({"user_query": query, "step_budget": 12}, set(), 12)
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"source_text": query, "expense": expense}
        first = self.agent._next_project_search_args(state, expense, allow_llm=False)
        variants = self.agent._project_search_fanout_args(state, expense, first)
        self.assertIn({"project_name": "外包交付"}, variants)
        self.assertEqual(expense["items"][0]["name"], "外包数据服务")

    def test_task_graph_keeps_booking_when_llm_returns_schedule_query(self) -> None:
        query = "帮我先在A1看看订个会议室，不行A2也可以。"
        baseline = {
            "tasks": [
                {
                    "task_id": "meetingroom_1",
                    "domain": "meetingroom",
                    "intent": "book_single",
                    "slots": {"office_candidates": ["A1", "A2"]},
                    "submit_intent": "unknown",
                }
            ]
        }
        normalized = self.agent._normalize_task_graph(
            {"tasks": [{"domain": "meetingroom", "intent": "query_room_schedule", "slots": {}}]},
            baseline,
            query,
        )
        self.assertEqual(normalized["tasks"][0]["intent"], "book_single")

    def test_task_graph_merges_workspace_lookup_into_booking(self) -> None:
        query = "查一下工位，然后在工位附近订会议室"
        baseline = {
            "tasks": [
                {
                    "domain": "meetingroom",
                    "intent": "book_single",
                    "slots": {"needs_workspace": True},
                    "submit_intent": "unknown",
                }
            ]
        }
        normalized = self.agent._normalize_task_graph(
            {
                "tasks": [
                    {"domain": "meetingroom", "intent": "book_single", "slots": {}},
                    {"domain": "meetingroom", "intent": "query_workspace", "slots": {}},
                ]
            },
            baseline,
            query,
        )
        self.assertEqual(len(normalized["tasks"]), 1)
        self.assertEqual(normalized["tasks"][0]["intent"], "book_single")
        self.assertTrue(normalized["tasks"][0]["slots"]["needs_workspace"])

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

    def test_batch_participant_projection_without_explicit_order_uses_list_field(self) -> None:
        state = RuntimeState({"user_query": "把李明、王芳都加到明天的项目会里", "step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "participant_add"
        state.meetingroom.slots = {"order_id": "BK-LOOKED-UP"}
        state.meetingroom.evidence["participant_results"] = [
            {"order_id": "BK-LOOKED-UP", "user_id": "U1", "name": "李明"},
            {"order_id": "BK-LOOKED-UP", "user_id": "U2", "name": "王芳"},
        ]
        answer = ResultProjectionRegistry().project(state)
        self.assertEqual(answer["participants_added"], [{"user_id": "U1", "name": "李明"}, {"user_id": "U2", "name": "王芳"}])
        self.assertNotIn("booking_result", answer)

    def test_explicit_batch_participant_projection_uses_booking_result(self) -> None:
        state = RuntimeState({"user_query": "把张伟和王芳加入订单SEED-100-001", "step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "participant_add"
        state.meetingroom.evidence["participant_results"] = [
            {"order_id": "SEED-100-001", "user_id": "U1", "name": "张伟"},
            {"order_id": "SEED-100-001", "user_id": "U2", "name": "王芳"},
        ]
        answer = ResultProjectionRegistry().project(state)
        self.assertEqual(answer["booking_result"]["status"], "participants_added")
        self.assertEqual(answer["booking_result"]["added_count"], 2)

    def test_single_participant_projection_preserves_tool_status(self) -> None:
        state = RuntimeState({"user_query": "把王芳从需求评审会移除", "step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "participant_remove"
        state.meetingroom.evidence["participant_results"] = [
            {"status": "removed", "order_id": "BK-1", "user_id": "U1", "name": "王芳"}
        ]
        answer = ResultProjectionRegistry().project(state)
        self.assertEqual(answer["participant_result"]["status"], "removed")
        self.assertNotIn("booking_result", answer)

    def test_expense_read_plan_reserves_category_subclass_save_and_postcheck(self) -> None:
        state = RuntimeState({"step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"submit": True, "expense": {"project_name": "外包交付项目"}}
        self.agent._initialize_workflow_skill(state)
        self.assertEqual(self.agent._read_plan_step_reserve(state), 4)

    def test_expense_category_read_waits_for_verified_project(self) -> None:
        state = RuntimeState({"user_query": "项目申请", "step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"expense": {"project_name": "项目申请"}}
        state.workflow.evidence["applicant"] = {"user_id": "U1"}
        state.workflow.evidence["catalog"] = {"workflows": []}
        state.workflow.evidence["schema"] = {
            "schema": {
                "field_descriptions": {"material_category": "field_id=29023"},
                "detail_tables": {"detail_2": {"field_descriptions": {"material_subclass": "field_id=29028"}}},
            }
        }
        tasks: list[ReadTask] = []
        self.agent._append_workflow_read_tasks(state, tasks)
        self.assertFalse(any(task.tool == "workflow.browser_search" for task in tasks))

    def test_room_search_requires_bookable_rooms(self) -> None:
        state = RuntimeState({"user_query": "订明天会议室", "now": "2026-04-20T09:00:00+08:00", "step_budget": 6}, set(), 6)
        state.meetingroom.slots = {"day_text": "明天", "start": "10:00", "end": "11:00", "capacity": 6}
        args = self.agent._room_list_args_for_candidate(state, {"office_id": "A1"})
        self.assertTrue(args["bookable"])

    def test_extension_conflict_plans_single_day_room_occupancy(self) -> None:
        state = RuntimeState({"user_query": "延长半小时，如果冲突就先别动", "step_budget": 8}, set(), 8)
        state.meetingroom.needed = True
        state.meetingroom.intent = "extend_existing"
        state.meetingroom.slots = {"fallback_policy": "keep_if_extend_conflict"}
        state.meetingroom.evidence["selected_booking"] = {
            "order_id": "BK-1",
            "room_id": "ROOM-1",
            "day": "2026-04-21",
            "start": "14:00",
            "end": "15:00",
            "title": "项目复盘",
        }
        tasks: list[ReadTask] = []
        self.agent._append_meetingroom_read_tasks(state, tasks)
        canonical = next(task for task in tasks if task.tool == "meetingroom.booking.list")
        self.assertEqual(canonical.args, {"status": "active", "day": "2026-04-21", "keyword": "项目复盘"})

        state.meetingroom.evidence["tried_booking_lists"] = [{"args": canonical.args, "result": {"bookings": []}}]
        tasks = []
        self.agent._append_meetingroom_read_tasks(state, tasks)
        occupancy = next(task for task in tasks if task.tool == "meetingroom.room.bookings")
        self.assertEqual(occupancy.args, {"day": "2026-04-21", "room_id": "ROOM-1"})

    def test_single_turn_reply_is_blocked_without_calling_env(self) -> None:
        called = []
        self.agent.env.reply = lambda message: called.append(message)
        state = RuntimeState({"mode": "single_turn", "step_budget": 3}, set(), 3)
        state.workflow.needed = True
        self.agent._execute(state, StepAction("reply", message="请补充项目"), {})
        self.assertEqual(called, [])
        self.assertEqual(state.workflow.blocked_reason, "reply_unavailable")


class ToolContractReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_runtime_schema_overrides_static_and_removed_tools_are_disabled(self) -> None:
        runtime_tools = [
            {
                "name": "workflow.save",
                "description": "runtime",
                "args_schema": {
                    "type": "object",
                    "properties": {"workflow_id": {"type": "string"}, "data": {"type": "object"}},
                    "required": ["workflow_id", "data"],
                },
            },
            {
                "name": "runtime.new_tool",
                "args_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            },
        ]
        effective = ToolContractReconciler(self.agent.static_tool_registry).reconcile(runtime_tools)
        self.assertEqual(effective.spec("workflow.save")["properties"]["workflow_id"]["type"], "string")
        self.assertEqual(effective.spec("workflow.save")["required_args"], ["workflow_id", "data"])
        self.assertIn("meetingroom.room.list", effective.disabled_tools)
        self.assertIn("runtime.new_tool", effective.available_unmapped)
        self.assertEqual(effective.risk("runtime.new_tool"), "unknown")
        self.assertFalse(effective.can_execute_write("runtime.new_tool"))

    def test_adapter_uses_runtime_types_and_drops_unknown_args(self) -> None:
        effective = ToolContractReconciler(self.agent.static_tool_registry).reconcile(
            [
                {
                    "name": "meetingroom.room.list",
                    "args_schema": {
                        "type": "object",
                        "properties": {"day": {"type": "string"}, "capacity_gte": {"type": "integer"}},
                        "required": ["day"],
                    },
                }
            ]
        )
        self.agent.tool_adapter.set_tool_registry(effective)
        args = self.agent.tool_adapter.adapt(
            "meetingroom.room.list",
            {"day": "2026-04-21", "capacity_gte": "8", "not_in_runtime_schema": True},
        )
        self.assertEqual(args, {"day": "2026-04-21", "capacity_gte": 8})

    def test_category_selection_does_not_use_unrelated_full_request_words(self) -> None:
        state = RuntimeState({"user_query": "办公场景焕新项目里买一套洽谈区桌椅", "step_budget": 10}, set(), 10)
        state.workflow.slots = {
            "source_text": "办公场景焕新项目里买一套洽谈区桌椅",
            "expense": {"items": [{"name": "洽谈区桌椅"}], "material_category_hint": ""},
        }
        state.workflow.evidence["category_options"] = {
            "options": [
                {"label": "家具", "value": "FURNITURE"},
                {"label": "办公设备/测试设备", "value": "EQUIPMENT"},
            ]
        }
        with patch.object(self.agent, "_can_call_llm", return_value=False):
            self.assertIsNone(self.agent._select_material_category(state))

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
