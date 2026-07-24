from __future__ import annotations

import json
import unittest
from pathlib import Path

from submission.my_agent import MyAgent, RuntimeState, StaticContextStore


ROOT = Path(__file__).resolve().parents[1]


class RoutingContextRegressionTest(unittest.TestCase):
    def test_compact_router_context_contains_every_registered_capability(self) -> None:
        store = StaticContextStore(ROOT / "submission" / "static_context", max_chars={"intent": 700})
        pack = store.for_intent_router()
        self.assertNotIn("static_context_truncated", pack["content"])
        payload = json.loads(pack["content"])
        capabilities = set(payload["caps"])
        self.assertIn("meeting.participant_list", capabilities)
        self.assertIn("meeting.participant_add", capabilities)
        self.assertIn("meeting.schedule_book", capabilities)
        self.assertIn("workflow.leave_replace_submit", capabilities)
        self.assertEqual(len(capabilities), 18)


class DomainNormalizationRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_chinese_semantic_time_is_normalized_before_runtime(self) -> None:
        slots = self.agent._normalize_task_meeting_slots({"start": "两点", "end": "下午三点"})
        self.assertEqual(slots["start"], "14:00")
        self.assertEqual(slots["end"], "15:00")

    def test_special_leave_types_and_reasons_are_supported(self) -> None:
        workflow_id = self.agent.workflow_registry.workflow_id("leave")
        leave_types = {item["label"]: item["value"] for item in self.agent.workflow_registry.options(workflow_id, "leave_type")}
        reasons = {item["label"]: item["value"] for item in self.agent.workflow_registry.options(workflow_id, "reason")}
        self.assertEqual(leave_types["婚假"], "M")
        self.assertEqual(leave_types["陪产假"], "P")
        self.assertEqual(leave_types["丧假"], "F")
        self.assertEqual(reasons["本人结婚"], "03")
        self.assertEqual(reasons["配偶生产陪护"], "04")
        self.assertEqual(reasons["亲人过世"], "09")

    def test_special_leave_defaults_to_submit_unless_draft_is_explicit(self) -> None:
        submit = self.agent._heuristic_workflow("我需要请5月14日到5月16日婚假，审批人找王芳经理。", {})
        draft = self.agent._heuristic_workflow("先存个5月14日到5月16日婚假草稿，审批人找王芳经理。", {})
        self.assertTrue(submit["submit"])
        self.assertFalse(draft["submit"])

    def test_generic_afternoon_leave_uses_explicit_duration(self) -> None:
        query = "明天上午10点订个会议室开周会，下午我要请2小时事假去办事。"
        state = RuntimeState({"user_query": query, "now": "2026-05-12T09:00:00+08:00", "step_budget": 10}, set(), 10)
        state.workflow.intent = "leave"
        state.workflow.slots = {"source_text": query, "leave": self.agent._heuristic_leave(query)}
        state.workflow.slots["leave"]["reason_label"] = "本人有事"
        plan = self.agent._leave_plan(state)
        self.assertEqual(plan["start_time"], "2026-05-13 14:00")
        self.assertEqual(plan["end_time"], "2026-05-13 16:00")
        self.assertEqual(plan["duration"], 2.0)

    def test_regular_leave_without_submit_word_defaults_to_draft(self) -> None:
        workflow = self.agent._heuristic_workflow("我明天10点半到下午3点45请事假，审批人刘经理。", {})
        self.assertFalse(workflow["submit"])

    def test_explicit_minute_is_not_truncated(self) -> None:
        leave = self.agent._heuristic_leave("我明天10点半到下午3点45请事假")
        self.assertEqual(leave["start"], "10:30")
        self.assertEqual(leave["end"], "15:45")

    def test_leave_date_range_keeps_times_between_dates(self) -> None:
        leave = self.agent._heuristic_leave("我需要请5月12日晚上8点到5月13日早上8点的病假")
        self.assertIn("5月12日", leave["day_text"])
        self.assertIn("5月13日", leave["day_text"])

    def test_weekday_range_and_requested_days_form_leave_plan(self) -> None:
        query = "我下周一到周三要结婚，需要请3天婚假，审批人刘经理。"
        state = RuntimeState({"user_query": query, "now": "2026-04-18T09:00:00+08:00", "step_budget": 9}, set(), 9)
        state.workflow.intent = "leave"
        state.workflow.slots = {"leave": self.agent._heuristic_leave(query)}
        state.workflow.slots["leave"]["reason_label"] = "本人结婚"
        plan = self.agent._leave_plan(state)
        self.assertEqual(plan["start_time"], "2026-04-20 09:00")
        self.assertEqual(plan["end_time"], "2026-04-22 18:00")
        self.assertEqual(plan["duration"], 24.0)

    def test_regular_leave_without_attachment_does_not_request_file_list(self) -> None:
        query = "我明天10点到11点请事假，审批人刘经理。"
        state = RuntimeState({"user_query": query, "step_budget": 8}, set(), 8)
        state.workflow.slots = {"leave": self.agent._heuristic_leave(query)}
        self.assertEqual(self.agent._leave_attachment_directory(state), "")


class ExpenseCandidateRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_literal_item_match_ignores_unrelated_project_context(self) -> None:
        options = [
            {"label": "电脑及其配件", "value": "WZ_202206060014"},
            {"label": "打印机、扫描仪及其配件", "value": "WZ_202206060015"},
            {"label": "测试设备", "value": "WZ_202206060020"},
        ]
        selected = self.agent._select_subclass_option(
            "电脑",
            options,
            set(),
            context_hint="项目名称不应改变候选标签的字面匹配",
        )
        self.assertEqual(selected["value"], "WZ_202206060014")

    def test_cross_domain_expense_slice_keeps_leading_quantity(self) -> None:
        query = "订明天上午会议室，另外帮我提交办公设备申请，要2台显示器每台1500，1个扩展坞800，项目是星火平台。"
        expense = self.agent._heuristic_expense(query)
        self.assertEqual(expense["raw_text"], "帮我提交办公设备申请，要2台显示器每台1500，1个扩展坞800，项目是星火平台。")
        self.assertEqual([(item["name"], item["quantity"]) for item in expense["items"]], [("显示器", "2"), ("扩展坞", "1")])

    def test_purchase_draft_syntax_extracts_specific_item(self) -> None:
        expense = self.agent._heuristic_expense("帮我给测试环境建设项目存一个扫描仪采购草稿，预算2800元。")
        self.assertEqual(expense["items"][0]["name"], "扫描仪")
        self.assertEqual(expense["items"][0]["budget_amount"], "2800.00")

    def test_combined_names_mapping_to_same_subclass_do_not_require_split(self) -> None:
        expense = {
            "total_amount": "20000.00",
            "items": [{"name": "官网落地页及海报设计", "budget_amount": "20000.00"}],
        }
        options = [
            {"label": "设计服务（含网页制作）", "value": "DESIGN"},
            {"label": "视频制作", "value": "VIDEO"},
        ]
        self.assertFalse(self.agent._has_unallocated_multi_material_budget(expense, options))

    def test_verified_singleton_still_runs_user_grounded_refinement(self) -> None:
        query = "帮我提一个品牌宣传费用申请，项目是智能办公平台品牌升级项目，预算3万元。"
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"source_text": query, "expense": self.agent._heuristic_expense(query)}
        state.workflow.evidence["project_search_history"] = [
            {"args": {"project_name": "智能办公平台"}, "result": {"projects": []}}
        ]
        state.workflow.evidence["project_resolution"] = {
            "state": "verified_singleton",
            "query_plan": [],
            "candidate_registry": {},
            "transitions": [],
        }
        args = self.agent._project_refine_search_args(
            state,
            {"project_name": "智能办公平台品牌升级项目", "project_code": "A-260100001", "wbs_code": "A-260100001.03"},
        )
        self.assertEqual(args, {"project_name": "品牌升级"})

    def test_ambiguous_projects_use_unique_user_grounded_refinement(self) -> None:
        query = "帮我给测试环境建设项目存一个扫描仪采购草稿，预算2800元。"
        state = RuntimeState({"user_query": query, "step_budget": 12}, set(), 12)
        state.workflow.intent = "expense_material"
        expense = self.agent._heuristic_expense(query)
        state.workflow.slots = {"source_text": query, "expense": expense}
        projects = [
            {"project_name": "终端测试环境建设项目", "project_code": "D-260100004", "wbs_code": "D-260100004.03"},
            {"project_name": "终端测试环境运维项目", "project_code": "D-260100014", "wbs_code": "D-260100014.03"},
        ]
        args = self.agent._disambiguating_project_search_args(state, expense, projects)
        self.assertEqual(args, {"project_name": "测试环境建设"})
        self.assertEqual(state.workflow.evidence["project_resolution"]["query_plan"][0]["source"], "candidate_disambiguation")

    def test_verified_subclass_label_is_used_when_contained_in_user_item_name(self) -> None:
        result = self.agent._specific_material_name(
            "外包数据服务",
            {"raw_text": "先帮我存一个外包数据服务草稿"},
            {"label": "数据服务", "value": "WZ-1"},
        )
        self.assertEqual(result, "数据服务")

class MeetingResultRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def test_participant_add_phrase_enters_participant_skill(self) -> None:
        semantic = self.agent._heuristic_meetingroom("把李明、王芳都加到明天上午9点的项目启动会里", {})
        self.assertEqual(semantic["intent"], "participant_add")
        self.assertEqual(semantic["participants"], [{"name": "李明"}, {"name": "王芳"}])

    def test_room_shorthand_and_week_context_are_normalized(self) -> None:
        query = "看看A1-349和A1-305下周哪个更空闲，选个空闲的订周三下午2点到4点"
        semantic = self.agent._heuristic_meetingroom(query, {})
        self.assertEqual(semantic["room_ids"], ["A1-3F-349", "A1-3F-305"])
        self.assertEqual(semantic["day_text"], "下周三")
        self.assertEqual(semantic["intent"], "book_by_schedule_analysis")

    def test_small_town_floor_expands_to_both_buildings(self) -> None:
        semantic = self.agent._heuristic_meetingroom("订明天上午小镇一楼10人会议室", {})
        self.assertEqual(semantic["office_address_candidates"][:2], ["0552_A1_1F", "0552_A2_1F"])

    def test_fallback_floor_applies_only_to_the_explicit_building(self) -> None:
        semantic = self.agent._heuristic_meetingroom(
            "帮我订明天下午2点到3点的小镇会议室，优先A1四楼，不行就A2",
            {},
        )
        self.assertEqual(semantic["office_address_candidates"][:2], ["0552_A1_4F", "0552_A2"])

    def test_existing_meeting_tomorrow_uses_shared_workday_policy(self) -> None:
        state = RuntimeState(
            {"user_query": "我明天下午两点的项目复盘会能延长半小时就延", "now": "2026-04-18T10:00:00+08:00", "step_budget": 8},
            set(),
            8,
        )
        state.meetingroom.intent = "extend_existing"
        state.meetingroom.slots = {"day_text": "明天"}
        self.assertEqual(self.agent._meeting_day_candidates(state)[0], "2026-04-20")

    def test_direct_booking_answer_projects_room_to_building(self) -> None:
        state = RuntimeState({"step_budget": 4}, set(), 4)
        state.meetingroom.intent = "book_single"
        state.meetingroom.evidence["room_candidates"] = {
            "rooms": [
                {
                    "room_id": "A3-3F-315",
                    "building": "A3",
                    "officeId": "a33f315000000000000000000000aa25",
                }
            ]
        }
        result = self.agent._booking_result_from_create(
            {
                "day": "2026-05-13",
                "office_id": "a33f315000000000000000000000aa25",
                "room_id": "A3-3F-315",
                "start": "14:00",
                "end": "16:00",
                "title": "产品讨论",
            },
            {},
            state.meetingroom,
        )
        self.assertEqual(result["office_id"], "A3")

    def test_schedule_booking_answer_keeps_verified_office_id(self) -> None:
        state = RuntimeState({"step_budget": 4}, set(), 4)
        state.meetingroom.intent = "book_by_schedule_analysis"
        state.meetingroom.evidence["room_candidates"] = {
            "rooms": [{"room_id": "A3-3F-315", "building": "A3", "officeId": "verified-office"}]
        }
        result = self.agent._booking_result_from_create(
            {"day": "2026-05-13", "office_id": "verified-office", "room_id": "A3-3F-315", "start": "14:00", "end": "16:00", "title": "产品讨论"},
            {},
            state.meetingroom,
        )
        self.assertEqual(result["office_id"], "verified-office")

    def test_rebook_projection_uses_selected_booking_without_order_slot(self) -> None:
        state = RuntimeState({"step_budget": 5}, set(), 5)
        state.meetingroom.intent = "cancel_rebook_existing"
        state.meetingroom.evidence["selected_booking"] = {"order_id": "BK-OLD"}
        state.meetingroom.evidence["cancel_done"] = {"cancelled": True, "order_id": "BK-OLD"}
        result = self.agent._booking_result_from_create(
            {"day": "2026-05-13", "room_id": "ROOM-NEW", "start": "14:00", "end": "16:00", "title": "全组评审"},
            {"success": True, "room_id": "ROOM-NEW"},
            state.meetingroom,
        )
        self.assertEqual(result["status"], "rebooked")
        self.assertEqual(result["cancelled_order_id"], "BK-OLD")

    def test_conditional_extension_conflict_projects_blocked_status(self) -> None:
        state = RuntimeState({"user_query": "延长半小时，如果冲突就先别动", "step_budget": 5}, set(), 5)
        state.meetingroom.intent = "extend_existing"
        state.meetingroom.slots = {"fallback_policy": "keep_if_extend_conflict"}
        action = self.agent._block_meetingroom(state, "conflict_after_requested_extension", order_id="BK-1")
        self.assertNotIn("result", action.args)

    def test_cross_domain_meeting_span_owns_its_date(self) -> None:
        query = "帮我约下周二下午2点到3点在A2园区8人会议室，主题季度复盘。另外我4月19日下午2点到6点请事假。"
        source = self.agent._domain_source_text(query, "meetingroom", "book_single")
        self.assertIn("下周二", source)
        self.assertNotIn("4月19日", source)
        state = RuntimeState({"user_query": query, "now": "2026-04-18T10:00:00+08:00", "step_budget": 9}, set(), 9)
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {"source_text": source, **self.agent._heuristic_meetingroom(source, state.obs)}
        self.assertEqual(self.agent._meeting_day(state), "2026-04-21")

    def test_workspace_evidence_reselects_same_floor_room(self) -> None:
        state = RuntimeState({"user_query": "订离工位最近的会议室", "step_budget": 5}, set(), 5)
        state.meetingroom.intent = "book_single"
        state.meetingroom.slots = {"needs_workspace": True, "day": "2026-04-21", "start": "14:00", "end": "15:00", "capacity": 10}
        state.meetingroom.evidence["room_candidates"] = {
            "day": "2026-04-21",
            "rooms": [
                {"room_id": "A4-3F-307", "building": "A4", "floor": "3F", "campus": "合肥", "capacity": 10, "bookable": True, "busy_slots": []},
                {"room_id": "A4-4F-004", "building": "A4", "floor": "4F", "campus": "合肥", "capacity": 11, "bookable": True, "busy_slots": []},
            ],
        }
        state.meetingroom.evidence["pending_selected_room"] = state.meetingroom.evidence["room_candidates"]["rooms"][0]
        self.agent._apply_meeting_tool_result(state, "user.get_workspace", {}, {"office_address": "0551_A4_4F"})
        self.assertEqual(self.agent._select_room_for_booking(state)["room_id"], "A4-4F-004")

    def test_rebook_selects_verified_replacement_before_cancel(self) -> None:
        state = RuntimeState(
            {"user_query": "把明天的会议换个更大的会议室", "now": "2026-04-20T10:00:00+08:00", "step_budget": 5},
            set(),
            5,
        )
        state.steps_used = 3
        state.meetingroom.needed = True
        state.meetingroom.intent = "rebook_larger_existing"
        state.meetingroom.slots = {
            "day": "2026-04-21",
            "start": "14:00",
            "end": "15:00",
            "capacity": 8,
            "title": "项目复盘",
        }
        state.meetingroom.evidence["selected_booking"] = {
            "order_id": "BK-1",
            "room_id": "A1-3F-301",
            "day": "2026-04-21",
            "start": "14:00",
            "end": "15:00",
            "attendees": 6,
            "title": "项目复盘",
        }
        state.meetingroom.evidence["room_candidates"] = {
            "day": "2026-04-21",
            "rooms": [
                {
                    "room_id": "A1-3F-302",
                    "officeId": "office-302",
                    "capacity": 10,
                    "bookable": True,
                    "busy_slots": [],
                }
            ],
        }
        action = self.agent._plan_existing_booking_skill_action(state)
        self.assertEqual(action.tool, "meetingroom.booking.cancel")
        self.assertEqual(state.meetingroom.evidence["pending_selected_room"]["room_id"], "A1-3F-302")
        self.assertTrue(
            self.agent.preflight_guard.validate_write(state, action.tool, action.args)["passed"]
        )

    def test_rebook_never_cancels_without_replacement_or_two_steps(self) -> None:
        state = RuntimeState(
            {"user_query": "把明天的会议换个更大的会议室", "now": "2026-04-20T10:00:00+08:00", "step_budget": 5},
            set(),
            5,
        )
        state.steps_used = 4
        state.meetingroom.needed = True
        state.meetingroom.intent = "rebook_larger_existing"
        state.meetingroom.slots = {
            "day": "2026-04-21",
            "start": "14:00",
            "end": "15:00",
            "capacity": 8,
            "title": "项目复盘",
        }
        state.meetingroom.evidence["selected_booking"] = {
            "order_id": "BK-1",
            "room_id": "A1-3F-301",
            "day": "2026-04-21",
            "start": "14:00",
            "end": "15:00",
            "attendees": 6,
        }
        replacement = {
            "room_id": "A1-3F-302",
            "officeId": "office-302",
            "capacity": 10,
            "bookable": True,
            "busy_slots": [],
        }
        state.meetingroom.evidence["room_candidates"] = {"day": "2026-04-21", "rooms": [replacement]}
        action = self.agent._plan_existing_booking_skill_action(state)
        self.assertEqual(action.kind, "block_meetingroom")
        self.assertEqual(action.args["reason"], "insufficient_step_budget")

        state.steps_used = 3
        state.meetingroom.evidence.pop("pending_selected_room", None)
        preflight = self.agent.preflight_guard.validate_write(
            state,
            "meetingroom.booking.cancel",
            {"order_id": "BK-1"},
        )
        self.assertFalse(preflight["passed"])
        self.assertIn("rebook_replacement_not_verified", preflight["errors"])


if __name__ == "__main__":
    unittest.main()
