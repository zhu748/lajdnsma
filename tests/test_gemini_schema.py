import unittest
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_gemini_module():
    fake_models = types.ModuleType("app.models")
    sys.modules["app.models"] = fake_models
    fake_schemas = types.ModuleType("app.models.schemas")
    fake_schemas.ChatCompletionRequest = object
    sys.modules["app.models.schemas"] = fake_schemas
    fake_models.schemas = fake_schemas

    fake_config = types.ModuleType("app.config")
    sys.modules["app.config"] = fake_config
    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.search = {"search_mode": False}
    fake_settings.RANDOM_STRING = False
    fake_settings.RANDOM_STRING_LENGTH = 0
    sys.modules["app.config.settings"] = fake_settings
    fake_config.settings = fake_settings

    fake_http_client = types.ModuleType("app.utils.http_client")
    fake_http_client.get_async_client = lambda: None
    sys.modules["app.utils.http_client"] = fake_http_client

    fake_logging = types.ModuleType("app.utils.logging")
    fake_logging.log = lambda *args, **kwargs: None
    sys.modules["app.utils.logging"] = fake_logging

    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.iter_sse_json = lambda *args, **kwargs: iter(())
    sys.modules["app.utils.sse"] = fake_sse

    spec = importlib.util.spec_from_file_location(
        "gemini_service",
        ROOT / "app/services/gemini.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GeminiSchemaTestCase(unittest.TestCase):
    def test_sanitize_gemini_schema_removes_unsupported_json_schema_keywords(self):
        module = load_gemini_module()
        schema = {
            "type": "object",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "propertyNames": {"pattern": "^[a-z]+$"},
            "properties": {
                "filters": {
                    "type": "object",
                    "propertyNames": {"enum": ["status"]},
                    "additionalProperties": {"type": "string"},
                },
                "count": {
                    "type": "integer",
                    "minimum": 0,
                    "exclusiveMinimum": True,
                    "description": "Number of rows",
                },
            },
            "required": ["count"],
        }

        sanitized = module.sanitize_gemini_schema(schema)

        self.assertEqual(
            sanitized,
            {
                "type": "object",
                "properties": {
                    "filters": {"type": "object"},
                    "count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Number of rows",
                    },
                },
                "required": ["count"],
            },
        )

    def test_sanitize_gemini_payload_sanitizes_function_declarations(self):
        module = load_gemini_module()
        payload = {
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "search",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "propertyNames": {"type": "string"},
                                    }
                                },
                            },
                        }
                    ]
                }
            ]
        }

        module.sanitize_gemini_payload(payload)

        self.assertNotIn(
            "propertyNames",
            payload["tools"][0]["function_declarations"][0]["parameters"][
                "properties"
            ]["query"],
        )

    def test_convert_messages_keeps_assistant_text_before_tool_call(self):
        module = load_gemini_module()
        client = module.GeminiClient("test-key")

        history, _ = client.convert_messages(
            [
                {
                    "role": "assistant",
                    "content": "我来查看当前目录。",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": "{\"command\":\"pwd\"}",
                            },
                        }
                    ],
                }
            ]
        )

        self.assertEqual(history[0]["role"], "model")
        self.assertEqual(history[0]["parts"][0], {"text": "我来查看当前目录。"})
        self.assertEqual(
            history[0]["parts"][1],
            {
                "functionCall": {"name": "Bash", "args": {"command": "pwd"}},
                "thoughtSignature": "skip_thought_signature_validator",
            },
        )

    def test_convert_messages_promotes_leading_system_instruction(self):
        module = load_gemini_module()
        client = module.GeminiClient("test-key")

        history, system_instruction = client.convert_messages(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
            ],
            use_system_prompt=True,
        )

        self.assertEqual(system_instruction, {"parts": [{"text": "system prompt"}]})
        self.assertEqual(history, [{"role": "user", "parts": [{"text": "hello"}]}])

    def test_convert_messages_can_skip_random_string_for_protocol_clients(self):
        module = load_gemini_module()
        module.settings.RANDOM_STRING = True
        module.settings.RANDOM_STRING_LENGTH = 8
        client = module.GeminiClient("test-key")

        history, _ = client.convert_messages(
            [{"role": "user", "content": "hello"}],
            skip_random_string=True,
        )

        self.assertEqual(history, [{"role": "user", "parts": [{"text": "hello"}]}])

    def test_convert_messages_keeps_remote_image_url_as_file_data(self):
        module = load_gemini_module()
        client = module.GeminiClient("test-key")

        history, _ = client.convert_messages(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/image.png",
                            },
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            history[0]["parts"][0],
            {
                "file_data": {
                    "file_uri": "https://example.com/image.png",
                    "mime_type": "image/png",
                }
            },
        )

    def test_convert_openai_request_clamps_thinking_budget(self):
        module = load_gemini_module()
        module.settings.ENABLE_THINKING = True
        client = module.GeminiClient("test-key")
        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            temperature=0.7,
            max_tokens=None,
            top_p=None,
            top_k=None,
            stop=None,
            n=1,
            thinking_budget=31999,
            enable_thinking=True,
            expose_reasoning=True,
            tools=None,
            tool_choice="auto",
        )

        _, data = client._convert_openAI_request(request, [], None, None)

        self.assertEqual(
            data["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            24576,
        )
        self.assertTrue(
            data["generationConfig"]["thinkingConfig"]["include_thoughts"]
        )

    def test_convert_openai_request_can_hide_thoughts_but_keep_budget(self):
        module = load_gemini_module()
        module.settings.ENABLE_THINKING = True
        client = module.GeminiClient("test-key")
        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            temperature=0.7,
            max_tokens=None,
            top_p=None,
            top_k=None,
            stop=None,
            n=1,
            thinking_budget=1024,
            enable_thinking=True,
            expose_reasoning=False,
            tools=None,
            tool_choice="auto",
        )

        _, data = client._convert_openAI_request(request, [], None, None)

        thinking_config = data["generationConfig"]["thinkingConfig"]
        self.assertEqual(thinking_config["thinkingBudget"], 1024)
        self.assertNotIn("include_thoughts", thinking_config)

    def test_convert_messages_preserves_tool_call_thought_signature(self):
        module = load_gemini_module()
        client = module.GeminiClient("test-key")

        history, _ = client.convert_messages(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "Bash",
                                "arguments": "{\"command\":\"pwd\"}",
                            },
                            "extra_content": {
                                "google": {"thought_signature": "real-signature"}
                            },
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            history[0]["parts"][0],
            {
                "functionCall": {"name": "Bash", "args": {"command": "pwd"}},
                "thoughtSignature": "real-signature",
            },
        )

    def test_gemini_response_extracts_function_call_thought_signature(self):
        module = load_gemini_module()

        response = module.GeminiResponseWrapper(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "Bash",
                                        "args": {"command": "pwd"},
                                    },
                                    "thoughtSignature": "real-signature",
                                }
                            ]
                        }
                    }
                ]
            }
        )

        self.assertEqual(
            response.function_call,
            [
                {
                    "name": "Bash",
                    "args": {"command": "pwd"},
                    "extra_content": {
                        "google": {"thought_signature": "real-signature"}
                    },
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
