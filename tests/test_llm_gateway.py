"""LLMGateway 单元测试：结构化调用（JSON/schema/重试/预算/无 key/json_object 自适应）。

测试只依赖 submission/utils/llm_gateway.py，用 FakeBackend 注入响应，**不触网**、
不含任何真实密钥。
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from utils.llm_gateway import (
    LLMGateway,
    FakeBackend,
    _parse_json,
    _providers_rejecting_json_object,
    _rejects_json_object,
    _schema_errors,
)

# 显式测试配置（非真实密钥）。
CFG = {
    "provider": "openai_compatible",
    "base_url": "https://test.example/v1",
    "model": "test-model",
    "api_key": "test-key",
    "llm_budget_s": 35.0,
}
SCHEMA = {
    "type": "object",
    "required": ["task_units"],
    "properties": {"task_units": {"type": "array", "items": {"type": "object"}}},
}


@pytest.fixture(autouse=True)
def _reset_provider_latch() -> None:
    """清理供应商级 json_object 记忆，避免测试间串扰。"""
    _providers_rejecting_json_object.clear()
    yield
    _providers_rejecting_json_object.clear()


def _gateway(*responses: object) -> LLMGateway:
    return LLMGateway(config=CFG, backend=FakeBackend(list(responses)))


class TestParseJson:
    """宽容 JSON 解析（含代码块剥离）。"""

    def test_plain_object(self) -> None:
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_empty(self) -> None:
        assert _parse_json("") is None
        assert _parse_json("   ") is None

    def test_invalid(self) -> None:
        assert _parse_json("not json") is None

    def test_non_dict(self) -> None:
        """数组等非 dict 结构不属于结构化输出。"""
        assert _parse_json("[1, 2]") is None


class TestSchemaErrors:
    """本地 schema 校验。"""

    def test_missing_required(self) -> None:
        errors = _schema_errors({"a": 1}, {"type": "object", "required": ["b"]})
        assert any("b" in e for e in errors)

    def test_valid(self) -> None:
        assert _schema_errors({"task_units": []}, SCHEMA) == []


class TestStructuredCallSuccess:
    """成功路径：校验通过即返回。"""

    def test_valid_output(self) -> None:
        g = _gateway({"task_units": [{"unit_type": "meeting"}]})
        out = g.structured_call("card", {}, SCHEMA, 12.0, None)
        assert out["task_units"][0]["unit_type"] == "meeting"

    def test_retry_once_then_success(self) -> None:
        """首次非 JSON → 重试一次成功。"""
        g = _gateway("not json", {"task_units": [{"unit_type": "leave"}]})
        out = g.structured_call("card", {}, SCHEMA, 12.0, None)
        assert out["task_units"][0]["unit_type"] == "leave"

    def test_schema_fail_retry_fallback(self) -> None:
        """两次都不过校验 → 返回兜底 dict。"""
        g = _gateway({"nope": 1}, {"also": 2})
        out = g.structured_call("card", {}, SCHEMA, 12.0, {"task_units": []})
        assert out == {"task_units": []}


class TestStructuredCallFailure:
    """失败路径：全部兜底，永不抛异常。"""

    def test_network_error_fallback(self) -> None:
        class BoomBackend:
            def chat(self, messages, **kwargs):
                raise ConnectionError("network down")

        g = LLMGateway(config=CFG, backend=BoomBackend())
        out = g.structured_call("card", {}, SCHEMA, 12.0, {"task_units": []})
        assert out == {"task_units": []}

    def test_budget_exhausted_skips_backend(self) -> None:
        """预算耗尽后不触达后端，直接兜底。"""
        backend = FakeBackend([{"task_units": []}])
        g = LLMGateway(config=CFG, backend=backend)
        g._spent_s = g.llm_budget_s
        out = g.structured_call("card", {}, SCHEMA, 12.0, {"task_units": []})
        assert out == {"task_units": []}
        assert backend.calls == []

    def test_no_key_unavailable(self) -> None:
        g = LLMGateway(config={"provider": "openai_compatible"})
        assert g.available is False
        out = g.structured_call("card", {}, SCHEMA, 12.0, {"task_units": []})
        assert out == {"task_units": []}


class TestJsonObjectAdaptation:
    """供应商拒绝 response_format 时的自适应降级（进程级记忆）。"""

    def test_rejects_json_object_then_retry_plain(self) -> None:
        class RejectThenOk:
            """第一次带 require_json_object → 400；去掉后成功。"""

            def __init__(self) -> None:
                self.calls: list[bool] = []

            def chat(self, messages, **kwargs):
                self.calls.append(bool(kwargs.get("require_json_object")))
                if kwargs.get("require_json_object"):
                    exc = urllib.error.HTTPError(
                        CFG["base_url"], 400, "Bad Request", {}, None
                    )
                    exc.response_text = '{"error":{"message":"must contain json to use json_object"}}'
                    raise exc
                return json.dumps({"task_units": [{"unit_type": "budget"}]})

        backend = RejectThenOk()
        g = LLMGateway(config=CFG, backend=backend)
        out = g.structured_call("card", {}, SCHEMA, 12.0, None)
        assert out["task_units"][0]["unit_type"] == "budget"
        assert backend.calls == [True, False]  # 第一次带，第二次去掉
        # 进程级记忆：同一供应商后续实例直接跳过 response_format。
        assert CFG["base_url"] in _providers_rejecting_json_object
        assert LLMGateway(config=CFG, backend=FakeBackend([]))._prefer_json_object is False

    def test_rejects_detector(self) -> None:
        exc = urllib.error.HTTPError(CFG["base_url"], 400, "Bad Request", {}, None)
        exc.response_text = '{"error":"json_object"}'
        assert _rejects_json_object(exc) is True
        assert _rejects_json_object(ConnectionError()) is False
