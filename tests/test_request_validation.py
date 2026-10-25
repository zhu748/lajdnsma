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
