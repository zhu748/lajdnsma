"""真实 Pydantic / FastAPI 回归：required 入站兼容和路由前的 422 日志。"""
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api():
    schemas = load_file("validation_schemas", "app/models/schemas.py")
    logs = load_file("validation_logging", "app/utils/logging.py")
    try:
        with patch.dict(sys.modules, {"app.utils.logging": logs}):
            handlers = load_file("validation_handlers", "app/api/exception_handlers.py")
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, handlers.handle_request_validation_error)

        @app.post("/v1/chat/completions")
        async def chat(request: schemas.ChatCompletionRequest):
            return request.model_dump()

        # 跳过 app.utils.__init__ 的服务初始化，仍使用真实协议转换函数。
        utils = types.ModuleType("app.utils")
        utils.__path__ = [str(ROOT / "app/utils")]
        with patch.dict(sys.modules, {"app.models.schemas": schemas, "app.utils": utils}):
            # Legacy unit suites install fake schema classes at collection time.
            # Reload both implementations under the real schema, not just the facade.
            requests = {
                f"app.utils.{name}": load_file(f"validation_{name}", f"app/utils/{name}.py")
                for name in ("responses_requests", "anthropic_requests")
            }
            with patch.dict(sys.modules, requests):
                adapters = load_file("validation_protocols", "app/utils/protocol_requests.py")
                with patch.dict(sys.modules, {"app.utils.protocol_requests": adapters}):
                    protocols = load_file("validation_protocol_handlers", "app/api/protocol_handlers.py")

        @app.post("/v1/responses")
        async def responses(payload: dict):
            return protocols._normalize_protocol_request(payload, adapters.response_request_to_chat_request).model_dump()

        @app.post("/v1/messages")
        async def messages(payload: dict):
            return protocols._normalize_protocol_request(payload, adapters.claude_request_to_chat_request).model_dump()

        @app.post("/gemini-payload")
        async def native(request: schemas.ChatRequestGemini):
            return request.model_dump(by_alias=True, exclude_none=True)

        with TestClient(app) as client:
            yield client, logs.log_manager
    finally:
        logs.logger.removeHandler(logs.console_handler)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("choice", ["required", "auto", "none", None, {"type": "function", "function": {"name": "weather"}}])
def test_chat_accepts_standard_tool_choices(api, stream, choice):
    client, logs = api
    response = client.post("/v1/chat/completions", json={
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": "查天气"}],
        "stream": stream,
        "tools": [{"type": "function", "function": {"name": "weather"}}],
        "tool_choice": choice,
    })
    assert response.status_code == 200, response.text
    assert response.json()["tool_choice"] == choice
    assert logs.get_recent_logs() == []


@pytest.mark.parametrize("choice", ["required", {"type": "required"}])
def test_responses_required_passes_real_schema(api, choice):
    client, _ = api
    response = client.post("/v1/responses", json={
        "model": "gemini-2.5-pro",
        "input": "查天气",
        "tools": [{"type": "function", "name": "weather"}],
        "tool_choice": choice,
    })
    assert response.status_code == 200, response.text
    assert response.json()["tool_choice"] in (
        "required", {"type": "function_calling_config", "mode": "ANY"},
    )


def test_invalid_choice_still_returns_422_and_logs_without_request_secrets(api):
    client, logs = api
    response = client.post(
        "/v1/chat/completions?key=secret-query",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "model": "gemini-2.5-pro",
            "messages": [{"role": "user", "content": "secret-message"}],
            "stream": True,
            "tool_choice": "secret-invalid-choice",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "tool_choice"]
    entry, = logs.get_recent_logs()
    assert entry["status_code"] == 422
    assert entry["level"] == "WARNING"
    assert entry["model"] == "gemini-2.5-pro"
    assert entry["request_type"] == "stream"
    assert "POST /v1/chat/completions" in entry["message"]
    assert "body.tool_choice" in entry["error_message"]
    assert "secret-" not in json.dumps(entry)


@pytest.mark.parametrize("body", ["{", "[]", '{"messages": []}', '{"model": {}, "messages": 1}'])
def test_other_validation_failures_keep_422_and_are_logged(api, body):
    client, logs = api
    response = client.post(
        "/v1/chat/completions", content=body, headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "detail" in response.json()
    entry, = logs.get_recent_logs()
    assert entry["status_code"] == 422
    assert entry["error_message"]


@pytest.mark.parametrize("route", ["/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("payload,field", [
    ({}, "model"),
    ({"model": {}}, "model"),
    ({"model": "gemini-2.5-pro", "stream": "invalid"}, "stream"),
])
def test_protocol_adapter_validation_errors_are_logged_as_422(api, route, payload, field):
    client, logs = api
    response = client.post(route, json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", field]
    entry, = logs.get_recent_logs()
    assert entry["status_code"] == 422
    assert f"body.{field}" in entry["error_message"]


@pytest.mark.parametrize("field", ["toolConfig", "tool_config"])
def test_native_tool_config_is_preserved(api, field):
    client, _ = api
    config = {"functionCallingConfig": {"mode": "ANY"}}
    response = client.post("/gemini-payload", json={
        "contents": [{"role": "user", "parts": [{"text": "查天气"}]}], field: config,
    })
    assert response.status_code == 200, response.text
    assert response.json()["toolConfig"] == config


@pytest.mark.parametrize("option", [
    {"input": {"role": "user", "content": "bad"}},
    {"input": [{"role": "user", "content": [7]}]},
    {"input": [{"role": "user", "content": [{"type": [], "text": "bad"}]}]},
    {"input": [{"role": "user", "content": [{"type": "input_text", "text": 7}]}]},
    {"input": [{"type": "function_call", "call_id": [], "name": "f"}]},
    {"input": [{"type": "function_call", "call_id": "a", "name": "f", "arguments": "[1]"}]},
    {"input": [{"type": "function_call", "call_id": "a", "name": "f", "arguments": "{bad}"}]},
    {"input": [{"type": "function_call", "call_id": "a", "name": "f", "arguments": '{"x":NaN}'}]},
    {"input": [{"type": "function_call_output", "call_id": "missing", "output": "ok"}]},
    {"input": [{"type": "function_call", "call_id": "a", "name": "f", "extra_content": {"google": []}}]},
    {"input": [{"type": "function_call", "call_id": "a", "name": "f"}] * 2},
    {"tool_choice": {"type": []}},
    {"tool_choice": False},
    {"tools": [{"type": "function", "name": "f", "async": True}]},
    {"instructions": ["not a string"]},
])
def test_responses_rejects_malformed_nested_input_before_dispatch(api, option):
    client, _ = api
    response = client.post('/v1/responses', json={"model": "test", "input": "hi", **option})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("option", [
    {"messages": [7]},
    {"messages": [{"role": [], "content": "bad"}]},
    {"messages": [{"role": "user", "content": 7}]},
    {"messages": [{"role": "user", "content": [7]}]},
    {"messages": [{"role": "user", "content": [{"type": [], "text": "bad"}]}]},
    {"messages": [{"role": "user", "content": [{"type": "text", "text": []}]}]},
    {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "file", "id": "image"}}]}]},
    {"messages": [{"role": "user", "content": [{"type": "document"}]}]},
    {"messages": [{"role": "user", "content": [{"type": "tool_use", "id": "a", "name": "f", "input": {}}]}]},
    {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "f", "input": []}]}]},
    {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "missing"}]}]},
    {"tool_choice": {"type": "invalid"}},
    {"tool_choice": {"type": "auto", "disable_parallel_tool_use": "false"}},
    {"tools": [None]},
    {"tools": [{"name": "f", "input_schema": []}]},
    {"thinking": {"type": "enabled", "budget_tokens": "bad"}},
    {"system": [{"type": "text", "text": 7}]},
])
def test_messages_rejects_malformed_nested_input_before_dispatch(api, option):
    client, _ = api
    response = client.post('/v1/messages', json={"model": "test", "messages": [{"role": "user", "content": "hi"}], **option})
    assert response.status_code == 400, response.text


def test_responses_parallel_tools_remain_in_one_assistant_turn(api):
    client, _ = api
    response = client.post('/v1/responses', json={"model": "test", "input": [
        {"role": "assistant", "content": "Checking"},
        {"type": "function_call", "call_id": "a", "name": "lookup", "arguments": '{"q":1}'},
        {"role": "assistant", "content": "both"},
        {"type": "function_call", "call_id": "b", "name": "lookup", "arguments": '{"q":2}'},
        {"type": "function_call_output", "call_id": "b", "output": "second"},
        {"type": "function_call_output", "call_id": "a", "output": "first"},
    ]})
    assert response.status_code == 200, response.text
    messages = response.json()['messages']
    assert [m['role'] for m in messages] == ['assistant', 'tool', 'tool']
    assert messages[0]['content'] == 'Checking\nboth'
    assert [c['id'] for c in messages[0]['tool_calls']] == ['a', 'b']
    assert [m['tool_call_id'] for m in messages[1:]] == ['b', 'a']


def test_messages_preserves_system_whitespace_and_parallel_results(api):
    client, _ = api
    response = client.post('/v1/messages', json={"model": "test", "system": "  rules\n", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": key, "name": "f", "input": {}} for key in ['a', 'b']]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": key, "content": "ok"} for key in ['b', 'a']] + [{"type": "text", "text": "continue"}]},
    ]})
    assert response.status_code == 200, response.text
    messages = response.json()['messages']
    assert messages[0]['content'] == '  rules\n'
    assert [m['role'] for m in messages] == ['system', 'assistant', 'tool', 'tool', 'user']
    assert [m['name'] for m in messages[2:4]] == ['f', 'f']
