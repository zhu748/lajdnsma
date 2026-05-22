import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FakeStreamingResponse:
    def __init__(self, body_iterator, media_type=None):
        self.body_iterator = body_iterator
        self.media_type = media_type


async def _iter_chunks(chunks):
    for chunk in chunks:
        yield chunk


def load_protocol_handlers():
    fake_fastapi = types.ModuleType("fastapi")

    class FakeHTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            self.status_code = status_code
            self.detail = detail

    class FakeJSONResponse:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code

    fake_fastapi.HTTPException = FakeHTTPException
    fake_fastapi_responses = types.ModuleType("fastapi.responses")
    fake_fastapi_responses.StreamingResponse = FakeStreamingResponse
    fake_fastapi_responses.JSONResponse = FakeJSONResponse

    fake_protocol_adapters = types.ModuleType("app.utils.protocol_adapters")
    fake_protocol_adapters.response_request_to_chat_request = (
        lambda payload: types.SimpleNamespace(model=payload["model"], payload=payload)
    )
    fake_protocol_adapters.claude_request_to_chat_request = (
        lambda payload: types.SimpleNamespace(model=payload["model"], payload=payload)
    )
    fake_protocol_adapters.openai_chat_to_response_api = (
        lambda response, payload=None: {"kind": "responses", "response": response, "payload": payload}
    )
    fake_protocol_adapters.responses_error_response = (
        lambda message, status_code=500, code=None: {"status": "failed", "error": {"message": message, "code": code or str(status_code)}}
    )
    fake_protocol_adapters.openai_chat_to_claude_response = (
        lambda response: {"kind": "claude", "response": response}
    )

    async def openai_stream_to_responses_stream(body_iterator, model):
        async for chunk in body_iterator:
            yield f"responses::{model}::{chunk}"

    async def openai_stream_to_claude_stream(body_iterator, model):
        async for chunk in body_iterator:
            yield f"claude::{model}::{chunk}"

    fake_protocol_adapters.openai_stream_to_responses_stream = (
        openai_stream_to_responses_stream
    )
    fake_protocol_adapters.openai_stream_to_claude_stream = (
        openai_stream_to_claude_stream
    )

    sys.modules["fastapi"] = fake_fastapi
    sys.modules["fastapi.responses"] = fake_fastapi_responses
    sys.modules["app.utils.protocol_adapters"] = fake_protocol_adapters

    spec = importlib.util.spec_from_file_location(
        "protocol_handlers", ROOT / "app/api/protocol_handlers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProtocolHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_handle_responses_request_non_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return {"id": "chatcmpl_1"}

        result = await module.handle_responses_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )
        self.assertEqual(result["kind"], "responses")

    async def test_handle_responses_request_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return FakeStreamingResponse(_iter_chunks(["chunk-a", "chunk-b"]))

        response = await module.handle_responses_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )
        self.assertEqual(response.media_type, "text/event-stream")
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        self.assertIn("responses::gemini-2.5-pro::chunk-a", chunks[0])
        self.assertIn("responses::gemini-2.5-pro::chunk-b", chunks[1])

    async def test_handle_responses_request_wraps_cached_openai_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return FakeStreamingResponse(
                _iter_chunks(["data: cached\n\n", "data: [DONE]\n\n"]),
                media_type="text/event-stream",
            )

        response = await module.handle_responses_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        self.assertEqual(
            chunks,
            [
                "responses::gemini-2.5-pro::data: cached\n\n",
                "responses::gemini-2.5-pro::data: [DONE]\n\n",
            ],
        )

    async def test_handle_responses_request_passes_payload_to_converter(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return {"id": "chatcmpl_1"}

        result = await module.handle_responses_request(
            {"model": "gemini-2.5-pro", "metadata": {"a": "b"}},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )

        self.assertEqual(result["payload"]["metadata"], {"a": "b"})

    def test_responses_model_alias_supports_wildcards(self):
        module = load_protocol_handlers()
        fake_settings = types.SimpleNamespace(
            RESPONSES_MODEL_ALIASES={"gpt-*": "gemini-2.5-flash"},
            RESPONSES_DEFAULT_MODEL="gemini-2.5-pro",
            WHITELIST_MODELS=set(),
        )

        class FakeGeminiClient:
            AVAILABLE_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

        fake_config = types.ModuleType("app.config")
        fake_services = types.ModuleType("app.services")
        fake_logging = types.ModuleType("app.utils.logging")
        fake_services.GeminiClient = FakeGeminiClient
        fake_logging.log = lambda *args, **kwargs: None
        sys.modules["app.config"] = fake_config
        sys.modules["app.config.settings"] = fake_settings
        sys.modules["app.services"] = fake_services
        sys.modules["app.utils.logging"] = fake_logging

        request = types.SimpleNamespace(model="gpt-5")
        module._resolve_responses_model_alias(request)
        self.assertEqual(request.model, "gemini-2.5-flash")

    def test_responses_model_alias_uses_exact_before_wildcard(self):
        module = load_protocol_handlers()
        fake_settings = types.SimpleNamespace(
            RESPONSES_MODEL_ALIASES={"gpt-*": "gemini-2.5-flash", "gpt-5": "gemini-2.5-pro"},
            RESPONSES_DEFAULT_MODEL="gemini-2.5-flash",
            WHITELIST_MODELS=set(),
        )

        class FakeGeminiClient:
            AVAILABLE_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

        fake_config = types.ModuleType("app.config")
        fake_services = types.ModuleType("app.services")
        fake_logging = types.ModuleType("app.utils.logging")
        fake_services.GeminiClient = FakeGeminiClient
        fake_logging.log = lambda *args, **kwargs: None
        sys.modules["app.config"] = fake_config
        sys.modules["app.config.settings"] = fake_settings
        sys.modules["app.services"] = fake_services
        sys.modules["app.utils.logging"] = fake_logging

        request = types.SimpleNamespace(model="gpt-5")
        module._resolve_responses_model_alias(request)
        self.assertEqual(request.model, "gemini-2.5-pro")

    def test_claude_model_alias_supports_wildcards(self):
        module = load_protocol_handlers()
        fake_settings = types.SimpleNamespace(
            CLAUDE_MODEL_ALIASES={"claude-sonnet-*": "gemini-2.5-flash"},
            CLAUDE_DEFAULT_MODEL="gemini-2.5-pro",
            WHITELIST_MODELS=set(),
        )

        class FakeGeminiClient:
            AVAILABLE_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

        fake_config = types.ModuleType("app.config")
        fake_services = types.ModuleType("app.services")
        fake_logging = types.ModuleType("app.utils.logging")
        fake_services.GeminiClient = FakeGeminiClient
        fake_logging.log = lambda *args, **kwargs: None
        sys.modules["app.config"] = fake_config
        sys.modules["app.config.settings"] = fake_settings
        sys.modules["app.services"] = fake_services
        sys.modules["app.utils.logging"] = fake_logging

        request = types.SimpleNamespace(model="claude-sonnet-4-20250514")
        module._resolve_claude_model_alias(request)
        self.assertEqual(request.model, "gemini-2.5-flash")

    def test_claude_model_alias_uses_exact_before_wildcard(self):
        module = load_protocol_handlers()
        fake_settings = types.SimpleNamespace(
            CLAUDE_MODEL_ALIASES={
                "claude-*": "gemini-2.5-flash",
                "claude-opus-4-20250514": "gemini-2.5-pro",
            },
            CLAUDE_DEFAULT_MODEL="gemini-2.5-flash",
            WHITELIST_MODELS=set(),
        )

        class FakeGeminiClient:
            AVAILABLE_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

        fake_config = types.ModuleType("app.config")
        fake_services = types.ModuleType("app.services")
        fake_logging = types.ModuleType("app.utils.logging")
        fake_services.GeminiClient = FakeGeminiClient
        fake_logging.log = lambda *args, **kwargs: None
        sys.modules["app.config"] = fake_config
        sys.modules["app.config.settings"] = fake_settings
        sys.modules["app.services"] = fake_services
        sys.modules["app.utils.logging"] = fake_logging

        request = types.SimpleNamespace(model="claude-opus-4-20250514")
        module._resolve_claude_model_alias(request)
        self.assertEqual(request.model, "gemini-2.5-pro")

    async def test_handle_responses_request_wraps_http_exception(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            raise module.HTTPException(status_code=400, detail="bad model")

        response = await module.handle_responses_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content["status"], "failed")
        self.assertIn("bad model", response.content["error"]["message"])

    async def test_handle_claude_request_wraps_http_exception_as_anthropic_error(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            raise module.HTTPException(status_code=400, detail="bad model")

        response = await module.handle_claude_messages_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content["type"], "error")
        self.assertEqual(response.content["error"]["type"], "invalid_request_error")
        self.assertIn("bad model", response.content["error"]["message"])

    async def test_handle_claude_messages_request_non_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return {"id": "chatcmpl_1"}

        result = await module.handle_claude_messages_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )
        self.assertEqual(result["kind"], "claude")

    async def test_handle_claude_messages_request_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return FakeStreamingResponse(_iter_chunks(["chunk-a"]))

        response = await module.handle_claude_messages_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )
        self.assertEqual(response.media_type, "text/event-stream")
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        self.assertIn("claude::gemini-2.5-pro::chunk-a", chunks[0])

    async def test_handle_claude_request_wraps_cached_openai_stream(self):
        module = load_protocol_handlers()

        async def fake_chat_handler(request, http_request, auth_dep, user_agent_dep):
            return FakeStreamingResponse(
                _iter_chunks(["data: cached\n\n", "data: [DONE]\n\n"]),
                media_type="text/event-stream",
            )

        response = await module.handle_claude_messages_request(
            {"model": "gemini-2.5-pro"},
            http_request=None,
            auth_dep=None,
            user_agent_dep=None,
            chat_handler=fake_chat_handler,
        )

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        self.assertEqual(
            chunks,
            [
                "claude::gemini-2.5-pro::data: cached\n\n",
                "claude::gemini-2.5-pro::data: [DONE]\n\n",
            ],
        )


if __name__ == "__main__":
    unittest.main()
