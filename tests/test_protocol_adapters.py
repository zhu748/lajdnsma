import unittest
import importlib.util
import json
from pathlib import Path
import sys
import types


fake_app = types.ModuleType("app")
fake_models = types.ModuleType("app.models")
fake_schemas = types.ModuleType("app.models.schemas")
fake_utils = types.ModuleType("app.utils")
fake_utils.__path__ = [str(Path(__file__).resolve().parents[1] / "app" / "utils")]
fake_sse = types.ModuleType("app.utils.sse")
fake_sse.sse_data = lambda payload: f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
fake_sse.sse_event = lambda event, payload: f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ChatCompletionRequest:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


fake_schemas.ChatCompletionRequest = ChatCompletionRequest
sys.modules.setdefault("app", fake_app)
sys.modules.setdefault("app.models", fake_models)
sys.modules.setdefault("app.utils", fake_utils)
sys.modules["app.models.schemas"] = fake_schemas
sys.modules["app.utils.sse"] = fake_sse


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "protocol_adapters.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
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


def _event_payloads(events):
    payloads = []
    for event in events:
        for line in event.splitlines():
            if line.startswith("data: "):
                data = line[len("data: ") :]
                if data != "[DONE]":
                    payloads.append(json.loads(data))
    return payloads


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

    def test_response_request_converts_function_call_outputs(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_shell__123_0",
                        "name": "shell",
                        "arguments": {"cmd": "pwd"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_shell__123_0",
                        "output": "D:/hajimi",
                    },
                ],
            }
        )

        self.assertEqual(request.messages[0]["role"], "assistant")
        self.assertEqual(request.messages[0]["tool_calls"][0]["function"]["name"], "shell")
        self.assertEqual(request.messages[1]["role"], "tool")
        self.assertEqual(request.messages[1]["name"], "shell")
        self.assertEqual(request.messages[1]["content"], "D:/hajimi")

    def test_response_request_adds_stateless_previous_response_note(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "previous_response_id": "resp_123",
                "input": "continue",
            }
        )

        self.assertEqual(request.messages[0]["role"], "system")
        self.assertIn("previous_response_id=resp_123", request.messages[0]["content"])
        self.assertEqual(request.messages[1]["content"], "continue")

    def test_response_request_converts_function_tools(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": "查天气",
                "tools": [
                    {
                        "type": "function",
                        "name": "weather",
                        "description": "查询天气",
                        "parameters": {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "function", "name": "weather"},
            }
        )

        self.assertEqual(request.tools[0]["type"], "function")
        self.assertEqual(request.tools[0]["function"]["name"], "weather")
        self.assertEqual(
            request.tool_choice,
            {"type": "function", "function": {"name": "weather"}},
        )

    def test_response_tool_choice_required_forces_any_tool(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": "Run one tool",
                "tools": [
                    {
                        "type": "function",
                        "name": "shell",
                        "description": "Run shell commands",
                        "parameters": {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "required"},
            }
        )

        self.assertEqual(
            request.tool_choice,
            {"type": "function_calling_config", "mode": "ANY"},
        )


    def test_response_request_to_chat_request_skips_empty_message_content(self):
        request = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "   "}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "保留"}]},
                ],
            }
        )

        self.assertEqual(len(request.messages), 1)
        self.assertEqual(request.messages[0]["content"], "保留")

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

    def test_claude_code_fixture_converts_tool_choice_and_system_array(self):
        payload = json.loads((FIXTURES_DIR / "claude_code_tool_use.json").read_text(encoding="utf-8"))
        request = claude_request_to_chat_request(payload)

        self.assertTrue(request.stream)
        self.assertIn("You are Claude Code.", request.messages[0]["content"])
        self.assertIn("Prefer concise tool calls.", request.messages[0]["content"])
        self.assertEqual(
            request.tool_choice,
            {"type": "function", "function": {"name": "Bash"}},
        )
        self.assertEqual(request.messages[3]["role"], "tool")
        self.assertEqual(request.messages[3]["name"], "Bash")

    def test_claude_tool_choice_string_is_safe(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            }
        )

        self.assertEqual(request.tool_choice, "auto")

    def test_claude_request_ignores_cache_control_and_metadata(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "system": [
                    {
                        "type": "text",
                        "text": "system prompt",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "metadata": {"user_id": "claude-code"},
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hello",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            }
        )

        self.assertEqual(request.messages[0]["content"], "system prompt")
        self.assertEqual(request.messages[1]["content"], "hello")

    def test_responses_codex_fixture_converts_function_history(self):
        payload = json.loads((FIXTURES_DIR / "codex_responses_function_call.json").read_text(encoding="utf-8"))
        request = response_request_to_chat_request(payload)

        self.assertTrue(request.stream)
        self.assertEqual(request.messages[0]["role"], "system")
        self.assertEqual(request.messages[2]["tool_calls"][0]["function"]["name"], "shell")
        self.assertEqual(request.messages[3]["role"], "tool")
        self.assertEqual(request.messages[3]["name"], "shell")
        self.assertEqual(
            request.tool_choice,
            {"type": "function", "function": {"name": "shell"}},
        )

    def test_claude_request_to_chat_request_preserves_tool_use_and_result(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [{"type": "text", "text": "/tmp/project"}],
                            }
                        ],
                    },
                ],
            }
        )

        self.assertEqual(request.messages[0]["role"], "assistant")
        self.assertEqual(request.messages[0]["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(request.messages[0]["tool_calls"][0]["function"]["name"], "Bash")
        self.assertEqual(request.messages[1]["role"], "tool")
        self.assertEqual(request.messages[1]["tool_call_id"], "toolu_1")
        self.assertEqual(request.messages[1]["name"], "Bash")
        self.assertEqual(request.messages[1]["content"], "/tmp/project")

    def test_claude_request_keeps_tool_result_before_followup_text(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [{"type": "text", "text": "/tmp/project"}],
                            },
                            {"type": "text", "text": "继续"},
                        ],
                    },
                ],
            }
        )

        self.assertEqual(request.messages[0]["role"], "assistant")
        self.assertEqual(request.messages[1]["role"], "tool")
        self.assertEqual(request.messages[1]["tool_call_id"], "toolu_1")
        self.assertEqual(request.messages[2], {"role": "user", "content": "继续"})

    def test_claude_request_combines_assistant_text_and_tool_use(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "我来查看当前目录。"},
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(len(request.messages), 1)
        self.assertEqual(request.messages[0]["role"], "assistant")
        self.assertEqual(request.messages[0]["content"], "我来查看当前目录。")
        self.assertEqual(request.messages[0]["tool_calls"][0]["id"], "toolu_1")

    def test_claude_request_preserves_tool_use_thought_signature(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                                "thought_signature": "real-signature",
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(
            request.messages[0]["tool_calls"][0]["extra_content"],
            {"google": {"thought_signature": "real-signature"}},
        )

    def test_claude_tool_choice_any_forces_any_tool(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "tool_choice": {"type": "any"},
                "messages": [{"role": "user", "content": "执行一个工具"}],
            }
        )

        self.assertEqual(
            request.tool_choice,
            {"type": "function_calling_config", "mode": "ANY"},
        )

    def test_claude_request_to_chat_request_preserves_images_and_thinking(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "AAAA",
                                },
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(request.thinking_budget, 1024)
        self.assertTrue(request.enable_thinking)
        self.assertEqual(request.messages[0]["content"][0]["text"], "看图")
        self.assertEqual(
            request.messages[0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,AAAA",
        )

    def test_claude_request_defaults_thinking_off(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

        self.assertFalse(request.enable_thinking)
        self.assertEqual(request.thinking_budget, 0)

    def test_claude_request_caps_large_thinking_budget_for_gemini(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "thinking": {"type": "enabled", "budget_tokens": 31999},
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

        self.assertTrue(request.enable_thinking)
        self.assertEqual(request.thinking_budget, 24576)

    def test_claude_tool_result_image_uses_marker_text(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "View",
                                "input": {"path": "screen.png"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "url",
                                            "url": "https://example.com/screen.png",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

        self.assertIn(
            "[tool_result_image:url:https://example.com/screen.png]",
            request.messages[1]["content"],
        )


    def test_claude_request_to_chat_request_skips_blank_string_content(self):
        request = claude_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "messages": [
                    {"role": "user", "content": "   "},
                    {"role": "user", "content": "有效消息"},
                ],
            }
        )

        self.assertEqual(len(request.messages), 1)
        self.assertEqual(request.messages[0]["content"], "有效消息")

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

    def test_openai_chat_to_response_api_includes_compat_fields(self):
        response = openai_chat_to_response_api(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            },
            {
                "instructions": "be helpful",
                "metadata": {"k": "v"},
                "previous_response_id": "resp_old",
                "tool_choice": "auto",
                "tools": [{"type": "function", "name": "x"}],
            },
        )

        self.assertIsNone(response["error"])
        self.assertEqual(response["instructions"], "be helpful")
        self.assertEqual(response["metadata"], {"k": "v"})
        self.assertEqual(response["previous_response_id"], "resp_old")
        self.assertEqual(response["tools"][0]["name"], "x")

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

    def test_openai_chat_to_claude_response_preserves_reasoning(self):
        response = openai_chat_to_claude_response(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "先思考",
                            "content": "答案",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        self.assertEqual(response["content"][0]["type"], "thinking")
        self.assertEqual(response["content"][0]["thinking"], "先思考")
        self.assertEqual(response["content"][1]["text"], "答案")

    def test_openai_chat_to_claude_response_maps_length_stop_reason(self):
        response = openai_chat_to_claude_response(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "partial"},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        self.assertEqual(response["stop_reason"], "max_tokens")

    def test_openai_chat_to_claude_response_maps_safety_stop_reason(self):
        response = openai_chat_to_claude_response(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "SAFETY",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            }
        )

        self.assertEqual(response["stop_reason"], "refusal")

    def test_openai_chat_to_claude_response_preserves_tool_thought_signature(self):
        response = openai_chat_to_claude_response(
            {
                "id": "chatcmpl_1",
                "created": 1,
                "model": "gemini-2.5-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "toolu_1",
                                    "type": "function",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": "{\"command\":\"pwd\"}",
                                    },
                                    "extra_content": {
                                        "google": {
                                            "thought_signature": "real-signature"
                                        }
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        self.assertEqual(response["content"][0]["type"], "tool_use")
        self.assertEqual(response["content"][0]["thought_signature"], "real-signature")

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
        self.assertIn("response.in_progress", joined)
        self.assertIn("response.output_item.added", joined)
        self.assertIn("response.content_part.added", joined)
        self.assertIn("response.output_text.delta", joined)
        self.assertIn("response.content_part.done", joined)
        self.assertIn("response.output_item.done", joined)
        self.assertIn("response.completed", joined)
        self.assertIn('"usage": {"input_tokens": 0, "output_tokens": 2, "total_tokens": 2}', joined)

    async def test_openai_stream_to_responses_stream_tool_call(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_weather","type":"function","function":{"name":"weather","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_responses_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"type": "function_call"', joined)
        self.assertIn('"name": "weather"', joined)
        self.assertIn("response.function_call_arguments.delta", joined)
        self.assertIn("response.function_call_arguments.done", joined)
        self.assertIn("response.completed", joined)

        payloads = _event_payloads(result)
        sequence_numbers = [payload["sequence_number"] for payload in payloads]
        self.assertEqual(sequence_numbers, list(range(1, len(payloads) + 1)))
        done_event = next(
            payload
            for payload in payloads
            if payload["type"] == "response.function_call_arguments.done"
        )
        self.assertEqual(done_event["name"], "weather")


    async def test_openai_stream_to_responses_stream_split_tool_call(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_shell","type":"function","function":{}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"shell"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\\"cmd\\\":\\\"pwd\\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_responses_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        payloads = _event_payloads(result)
        added_events = [
            payload
            for payload in payloads
            if payload["type"] == "response.output_item.added"
            and payload["item"]["type"] == "function_call"
        ]
        self.assertEqual(len(added_events), 1)
        self.assertEqual(added_events[0]["item"]["name"], "shell")
        self.assertNotIn('"name": null', "".join(result))

        done_event = next(
            payload
            for payload in payloads
            if payload["type"] == "response.function_call_arguments.done"
        )
        self.assertEqual(done_event["name"], "shell")
        self.assertEqual(done_event["arguments"], "{\"cmd\":\"pwd\"}")


    async def test_openai_stream_parser_supports_event_and_data_without_space(self):
        chunks = [
            'event: message\ndata:{"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"测"},"finish_reason":null}]}\n\n',
            'data:{"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"total_tokens":3}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"input_tokens": 1', joined)
        self.assertIn('"output_tokens": 2', joined)

    async def test_openai_stream_parser_supports_multiple_events_per_chunk(self):
        chunks = [
            (
                'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
                'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                'data: [DONE]\n\n'
            ),
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn("message_start", joined)
        self.assertIn('"text": "hello"', joined)
        self.assertIn('"stop_reason": "end_turn"', joined)
        self.assertIn("message_stop", joined)

    async def test_openai_stream_to_claude_stream_tool_call(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"toolu_1","type":"function","function":{"name":"Bash","arguments":"{\\\"command\\\":\\\"pwd\\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"type": "tool_use"', joined)
        self.assertIn('"id": "toolu_1"', joined)
        self.assertIn('"name": "Bash"', joined)
        self.assertIn('"type": "input_json_delta"', joined)
        self.assertIn('"stop_reason": "tool_use"', joined)
        self.assertIn('event: message_stop', joined)

    async def test_openai_stream_to_claude_stream_stops_text_before_tool_use(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"I will read it."},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"toolu_1","type":"function","function":{"name":"Read","arguments":"{\\\"file_path\\\":\\\"README.md\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        text_stop_pos = joined.index('event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}')
        tool_start_pos = joined.index('"type": "tool_use"')
        self.assertLess(text_stop_pos, tool_start_pos)

    async def test_openai_stream_to_claude_stream_stops_thinking_before_tool_use(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"reasoning_content":"Need the file."},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"toolu_1","type":"function","function":{"name":"Read","arguments":"{\\\"file_path\\\":\\\"README.md\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        thinking_stop_pos = joined.index('event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}')
        tool_start_pos = joined.index('"type": "tool_use"')
        self.assertLess(thinking_stop_pos, tool_start_pos)

    async def test_openai_stream_to_claude_stream_preserves_tool_thought_signature(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"toolu_1","type":"function","function":{"name":"Read","arguments":"{\\\"file_path\\\":\\\"README.md\\\"}"},"extra_content":{"google":{"thought_signature":"real-signature"}}}]},"finish_reason":"tool_calls"}]}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"thought_signature": "real-signature"', joined)

    async def test_openai_stream_to_claude_stream_delays_split_tool_start_until_name(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"toolu_1","type":"function","function":{}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"Read"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\\"file_path\\\":\\\"README.md\\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"type": "tool_use"', joined)
        self.assertIn('"id": "toolu_1"', joined)
        self.assertIn('"name": "Read"', joined)
        self.assertNotIn('"name": ""', joined)
        self.assertIn('"partial_json": "{\\"file_path\\":\\"README.md\\"}"', joined)

    async def test_openai_stream_to_claude_stream_usage_is_top_level(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        message_delta = "".join(result).split("event: message_delta\n", 1)[1]
        self.assertIn('"delta": {"stop_reason": "end_turn"', message_delta)
        self.assertIn('"usage": {"input_tokens": 10, "output_tokens": 2}', message_delta)

    async def test_openai_stream_to_claude_stream_maps_length_stop_reason(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":"length"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        message_delta = "".join(result).split("event: message_delta\n", 1)[1]
        self.assertIn('"delta": {"stop_reason": "max_tokens"', message_delta)

    async def test_openai_stream_to_claude_stream_maps_safety_stop_reason(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{},"finish_reason":"SAFETY"}],"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        message_delta = "".join(result).split("event: message_delta\n", 1)[1]
        self.assertIn('"delta": {"stop_reason": "refusal"', message_delta)

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

    async def test_openai_stream_to_claude_stream_preserves_reasoning(self):
        chunks = [
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"reasoning_content":"先思考"},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl_1","model":"gemini-2.5-pro","choices":[{"index":0,"delta":{"content":"答案"},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}\n\n',
        ]

        result = []
        async for item in openai_stream_to_claude_stream(
            _iter_chunks(chunks), "gemini-2.5-pro"
        ):
            result.append(item)

        joined = "".join(result)
        self.assertIn('"type": "thinking"', joined)
        self.assertIn('"type": "thinking_delta", "thinking": "先思考"', joined)
        self.assertIn('"index": 1, "delta": {"type": "text_delta", "text": "答案"}', joined)


if __name__ == "__main__":
    unittest.main()
