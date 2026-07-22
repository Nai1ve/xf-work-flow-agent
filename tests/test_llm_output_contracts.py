from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submission.my_agent import MyAgent, RuntimeState


class LLMOutputContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = MyAgent(type("Env", (), {})())

    def _state(self, query: str = "") -> RuntimeState:
        return RuntimeState({"user_query": query, "step_budget": 12}, set(), 12)

    def test_sparse_semantic_output_is_expanded_and_contains_no_model_confidence(self) -> None:
        query = "帮我订明天下午2点到3点的会议室，另外提交事假申请。"
        state = self._state(query)
        captured: dict = {}
        self.agent._llm_config = lambda profile="strong": {
            "api_key": "test",
            "timeout": 1,
            "max_calls": 2,
            "max_tokens": 256,
            "profile": profile,
        }

        def fake_chat(_config, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return json.dumps(
                {
                    "tasks": [
                        {
                            "domain": "meetingroom",
                            "intent": "book_single",
                            "submit": False,
                            "slots": {"day_text": "明天", "start": "14:00", "end": "15:00"},
                        },
                        {
                            "domain": "workflow",
                            "intent": "leave",
                            "submit": True,
                            "slots": {"leave_type_label": "事假"},
                        },
                    ]
                },
                ensure_ascii=False,
            )

        self.agent._chat_completion = fake_chat
        semantic = self.agent._extract_semantics(state, state.obs, [])
        self.assertEqual([task["domain"] for task in semantic["task_graph"]["tasks"]], ["meetingroom", "workflow"])
        self.assertTrue(semantic["workflow"]["submit"])
        self.assertNotIn('"confidence":', captured["messages"][0]["content"])
        self.assertEqual(captured["kwargs"]["max_output_tokens"], 192)

    def test_candidate_model_returns_only_verified_candidate_id(self) -> None:
        state = self._state("项目选终端测试环境建设项目")
        state.task_graph = {"tasks": []}
        captured: dict = {}
        self.agent._llm_config = lambda profile="strong": {
            "api_key": "test",
            "timeout": 1,
            "max_calls": 2,
            "max_tokens": 1200,
            "profile": profile,
        }

        def fake_chat(_config, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return '{"selected_id":"P2"}'

        self.agent._chat_completion = fake_chat
        result = self.agent._rank_candidates_with_llm(
            state,
            "选择费用申请项目",
            state.obs["user_query"],
            [{"project_code": "P1", "project_name": "其他项目"}, {"project_code": "P2", "project_name": "终端测试环境建设项目"}],
            ["project_code", "project_name"],
        )
        self.assertEqual(result, {"decision": "select", "selected_id": "P2"})
        payload = json.loads(captured["messages"][1]["content"].split("\n", 1)[1])
        self.assertEqual(payload["output_schema"], {"selected_id": "existing candidate id or null"})
        self.assertEqual(captured["kwargs"]["max_output_tokens"], 64)

    def test_unknown_candidate_id_is_rejected(self) -> None:
        state = self._state("选择项目")
        state.task_graph = {"tasks": []}
        self.agent._llm_config = lambda profile="strong": {
            "api_key": "test",
            "timeout": 1,
            "max_calls": 2,
            "max_tokens": 1200,
            "profile": profile,
        }
        self.agent._chat_completion = lambda *_args, **_kwargs: '{"selected_id":"INVENTED"}'
        result = self.agent._rank_candidates_with_llm(
            state,
            "选择费用申请项目",
            state.obs["user_query"],
            [{"project_code": "P1"}, {"project_code": "P2"}],
            ["project_code"],
        )
        self.assertEqual(result, {"decision": "need_more_info", "selected_id": ""})

    def test_project_mapping_uses_ordered_queries_without_model_scores(self) -> None:
        state = self._state("项目是星火质量工程平台")
        state.workflow.needed = True
        state.workflow.intent = "expense_material"
        expense = {"project_name": "星火质量工程平台", "items": []}
        state.workflow.slots = {"expense": expense, "source_text": state.obs["user_query"]}
        state.workflow.evidence["project_search_history"] = [{"args": {"project_name": "星火质量工程平台"}, "result": {"projects": []}}]
        captured: dict = {}
        self.agent._llm_config = lambda profile="strong": {
            "api_key": "test",
            "timeout": 1,
            "max_calls": 2,
            "max_tokens": 1200,
            "profile": profile,
        }

        def fake_chat(_config, messages, **kwargs):
            captured["messages"] = messages
            return '{"queries":["终端测试环境建设","质量工程平台"]}'

        self.agent._chat_completion = fake_chat
        variants = self.agent._llm_project_search_mapping_variants(state, expense)
        self.assertEqual([item["project_name"] for item in variants], ["终端测试环境建设", "质量工程平台"])
        payload = json.loads(captured["messages"][1]["content"].split("\n", 1)[1])
        self.assertEqual(payload["output_schema"], {"queries": ["substring query"]})

    def test_expense_detail_contract_derives_internal_decision(self) -> None:
        state = self._state("视频制作5万元，总预算5万元")
        state.workflow.slots = {
            "source_text": state.obs["user_query"],
            "expense": {
                "source_text": state.obs["user_query"],
                "items": [{"name": "视频制作", "budget_amount": "50000"}],
            },
        }
        state.workflow.evidence["schema"] = {"schema": {"detail_tables": {"detail_2": {"required_fields": []}}}}
        state.workflow.evidence["subclass_options"] = {"options": [{"value": "VIDEO", "label": "视频制作"}]}
        self.agent._chat_completion = lambda *_args, **_kwargs: json.dumps(
            {
                "details": [
                    {
                        "source_item_index": 0,
                        "material_subclass": "VIDEO",
                        "material_name": "视频制作",
                        "quantity": "1",
                        "unit_price": "50000",
                        "budget_amount": "50000",
                    }
                ]
            },
            ensure_ascii=False,
        )
        draft = self.agent._llm_expense_draft_ir(
            state,
            {"api_key": "test"},
            {"project_name": "项目", "project_code": "P1", "wbs_code": "W1"},
            {"value": "CAT", "label": "服务"},
            "50000.00",
        )
        self.assertEqual(draft["decision"], "draft")

    def test_form_draft_validation_does_not_require_confidence(self) -> None:
        state = self._state("创建测试设备费用草稿")
        state.workflow.slots = {"submit": False, "expense": {"total_amount": "8000"}}
        state.workflow.evidence["applicant"] = {"user_id": "U1", "employee_no": "E1"}
        state.workflow.evidence["subclass_options"] = {"options": [{"value": "TEST", "label": "测试设备"}]}
        args = self.agent._validate_expense_form_draft(
            state,
            {"project_name": "项目", "project_code": "P1", "wbs_code": "W1"},
            {"value": "CAT"},
            {
                "total_amount": "8000",
                "details": [
                    {
                        "material_subclass": "TEST",
                        "material_name": "测试设备",
                        "quantity": "1",
                        "unit_price": "8000",
                        "budget_amount": "8000",
                    }
                ],
            },
        )
        self.assertIsInstance(args, dict)

    def test_llm_call_log_has_start_finish_correlation_and_usage(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "choices": [{"message": {"content": '{"selected_id":"P1"}'}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
                    }
                ).encode()

        state = self._state("选择项目")
        captured_request: dict = {}

        def fake_urlopen(request, timeout):
            captured_request.update(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "calls.jsonl"
            config = {
                "provider": "openai_compatible",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "api_key": "secret-key",
                "timeout": 2,
                "temperature": 0,
                "max_calls": 2,
                "max_tokens": 64,
                "debug_log_path": str(log_path),
            }
            with patch("submission.my_agent.urllib.request.urlopen", side_effect=fake_urlopen):
                output = self.agent._chat_completion(
                    config,
                    [
                        {"role": "system", "content": "Return valid json only."},
                        {"role": "user", "content": "select"},
                    ],
                    state=state,
                    profile="strong",
                    call_purpose="candidate_ranking",
                )
            self.assertEqual(output, '{"selected_id":"P1"}')
            events = [json.loads(line) for line in log_path.read_text().splitlines()]
            start = next(item for item in events if item["event"] == "llm_call_start")
            finish = next(item for item in events if item["event"] == "llm_call")
            self.assertEqual(start["call_id"], finish["call_id"])
            self.assertEqual(finish["purpose"], "candidate_ranking")
            self.assertEqual(finish["usage"]["total_tokens"], 17)
            self.assertTrue(finish["response_json_valid"])
            self.assertNotIn("secret-key", log_path.read_text())
            user_messages = [item["content"] for item in captured_request["messages"] if item["role"] == "user"]
            self.assertTrue(any("json" in content.lower() for content in user_messages))


if __name__ == "__main__":
    unittest.main()
