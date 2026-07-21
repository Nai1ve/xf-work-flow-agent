from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.build_static_context import build_expense_examples_index, validate_expense_template
from submission.my_agent import MyAgent, ReadTask, RuntimeState


ROOT = Path(__file__).resolve().parents[1]


class ExpenseMemoryBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = build_expense_examples_index(ROOT / "contest" / "train" / "cases")

    def test_index_has_train_only_provenance_and_no_case_ids(self) -> None:
        self.assertEqual(self.index["provenance"]["split"], "train")
        encoded = json.dumps(self.index, ensure_ascii=False)
        self.assertNotIn("case_id", encoded)
        self.assertNotIn("beta_", encoded)

    def test_brand_package_memory_is_generic_and_amount_closed(self) -> None:
        matches = [
            entry
            for entry in self.index["entries"]
            if entry["request_shape"] == "generic_package"
            and entry["project"]["project_code"] == "A-260100001"
            and entry["total_amount"] == "60000.00"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            [row["budget_amount"] for row in matches[0]["rows"]],
            ["40000.00", "20000.00"],
        )

    def test_invalid_amount_chain_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_expense_template(
                "60000.00",
                [
                    {
                        "material_subclass": "WZ_A",
                        "material_name": "示例明细",
                        "quantity": "1",
                        "unit_price": "40000.00",
                        "budget_amount": "40000.00",
                    }
                ],
            )

    def test_project_alias_memory_prefers_canonical_train_query(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        query = "项目是产品平台产品发布会，包括视频制作和活动发布会，总预算8万元。"
        expense = agent._heuristic_expense(query)
        expense["source_text"] = query
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"source_text": query, "expense": expense}
        self.assertEqual(agent._expense_memory_project_search_args(state, expense), {"project_name": "智能办公平台"})

    def test_project_structure_word_is_last_resort(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        query = "项目是星火质量工程平台，申请测试设备8000元。"
        expense = agent._heuristic_expense(query)
        expense["source_text"] = query
        state = RuntimeState({"user_query": query, "step_budget": 10}, set(), 10)
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        state.workflow.slots = {"source_text": query, "expense": expense}
        variants = agent._project_search_fanout_args(state, expense, {"project_name": "星火质量工程平台"})
        self.assertEqual(variants[0], {"project_name": "星火质量工程平台"})
        self.assertTrue(all(item["project_name"] not in {"项目", "平台", "工程"} for item in variants[:2]))

    def test_cross_domain_scheduler_does_not_mutate_sort_lookup(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        state = RuntimeState({"step_budget": 10}, set(), 10)
        state.meetingroom.needed = True
        state.workflow.needed = True
        tasks = [
            ReadTask("meeting", "meetingroom.booking.list", {}, "meetingroom"),
            ReadTask("workflow", "workflow.catalog", {}, "workflow"),
        ]
        selected = agent._fair_read_batch(state, tasks, 2)
        self.assertEqual({task.domain for task in selected}, {"meetingroom", "workflow"})

    def test_expense_slice_excludes_meeting_time_amount(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        query = (
            "帮我订下周二下午2点到3点小镇A1四楼6人会议室，主题项目复盘。"
            "另外我需要提交品牌广告服务费用，项目还是城市服务大模型发布活动项目："
            "视频制作2条每条1.5万元，活动发布会1场4万元，总预算7万元。"
        )
        expense = agent._heuristic_expense(query)
        self.assertEqual(expense["project_name"], "城市服务大模型发布活动项目")
        self.assertEqual({item["name"] for item in expense["items"]}, {"视频制作", "活动发布会"})

    def test_multi_turn_subclass_and_total_is_single_item(self) -> None:
        agent = MyAgent(type("Env", (), {})())
        state = RuntimeState({"step_budget": 14}, set(), 14)
        expense = {
            "items": [],
            "material_subclass_hint": "电脑及其配件",
            "total_amount": "20000.00",
        }
        self.assertEqual(agent._expense_request_shape(state, expense, []), "single_item_total")


if __name__ == "__main__":
    unittest.main()
