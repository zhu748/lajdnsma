import unittest
import importlib.util
from pathlib import Path
import sys
import types


fake_app = types.ModuleType("app")
fake_models = types.ModuleType("app.models")
fake_schemas = types.ModuleType("app.models.schemas")


class ChatCompletionRequest:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


fake_schemas.ChatCompletionRequest = ChatCompletionRequest
sys.modules.setdefault("app", fake_app)
sys.modules.setdefault("app.models", fake_models)
sys.modules["app.models.schemas"] = fake_schemas


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "protocol_adapters.py"
SPEC = importlib.util.spec_from_file_location("protocol_adapters", MODULE_PATH)
protocol_adapters = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(protocol_adapters)

claude_request_to_chat_request = protocol_adapters.claude_request_to_chat_request
openai_chat_to_claude_response = protocol_adapters.openai_chat_to_claude_response
openai_chat_to_response_api = protocol_adapters.openai_chat_to_response_api
openai_stream_to_claude_stream = protocol_adapters.openai_stream_to_claude_stream
openai_stream_to_responses_stream = protocol_adapters.openai_stream_to_responses_stream
response_request_to_chat_request = protocol_adapters.response_request_to_chat_request


async def _iter_chunks(chunks):
    for chunk in chunks:
        yield chunk


class ProtocolAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def test_response_request_to_chat_request(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "instructions": "你是助手",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "你好"}],
                    }
                ],
                "stream": True,
                "max_output_tokens": 256,
            }
        )

        self.assertEqual(request.model, "gemini-2.5-pro")
        self.assertTrue(request.stream)
        self.assertEqual(request.max_tokens, 256)
        self.assertEqual(request.messages[0]["role"], "system")
        self.assertEqual(request.messages[1]["content"], "你好")

    def test_claude_request_to_chat_request(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "system": "你是 Claude 风格代理",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "讲个笑话"}],
                    }
                ],
                "max_tokens": 128,
                "tools": [
                    {
                        "name": "weather",
                        "description": "查询天气",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        )

        self.assertEqual(request.messages[0]["role"], "system")
        self.assertEqual(request.messages[1]["content"], "讲个笑话")
        self.assertEqual(request.max_tokens, 128)
        self.assertEqual(request.tools[0]["function"]["name"], "weather")

    def test_openai_chat_to_response_api(self):
        response = openai_chat_to_response_api(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "你好"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        )

        self.assertEqual(response["object"], "response")
        self.assertEqual(response["output"][0]["content"][0]["text"], "你好")
        self.assertEqual(response["usage"]["total_tokens"], 15)

    def test_openai_chat_to_claude_response(self):
        response = openai_chat_to_claude_response(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "你好"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        self.assertEqual(response["type"], "message")
        self.assertEqual(response["content"][0]["text"], "你好")
        self.assertEqual(response["usage"]["output_tokens"], 5)

    async def test_openai_stream_to_responses_stream(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_responses_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn("response.created", joined)
        self.assertIn("response.output_text.delta", joined)
        self.assertIn("response.completed", joined)


    async def test_openai_stream_parser_supports_event_and_data_without_space(self):
        chunks = [
            'event: message\ndata:{"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"测"},"finish_reason":null}]}\n\n',
            'data:{"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":3}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"output_tokens": 3', joined)

    async def test_openai_stream_to_claude_stream(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn("message_start", joined)
        self.assertIn("content_block_delta", joined)
        self.assertIn("message_stop", joined)


if __name__ == "__main__":
    unittest.main()
