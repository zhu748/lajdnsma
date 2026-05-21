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
    fake_fastapi_responses = types.ModuleType("fastapi.responses")
    fake_fastapi_responses.StreamingResponse = FakeStreamingResponse

    fake_protocol_adapters = types.ModuleType("app.utils.protocol_adapters")
    fake_protocol_adapters.response_request_to_chat_request = (
        lambda payload: types.SimpleNamespace(model=payload["model"], payload=payload)
    )
    fake_protocol_adapters.claude_request_to_chat_request = (
        lambda payload: types.SimpleNamespace(model=payload["model"], payload=payload)
    )
    fake_protocol_adapters.openai_chat_to_response_api = (
        lambda response: {"kind": "responses", "response": response}
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


if __name__ == "__main__":
    unittest.main()
