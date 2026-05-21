import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_vertex_request_adapter():
    fake_vertex_models = types.ModuleType("app.vertex.models")

    class OpenAIMessage:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    class OpenAIRequest:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    fake_vertex_models.OpenAIMessage = OpenAIMessage
    fake_vertex_models.OpenAIRequest = OpenAIRequest
    sys.modules["app.vertex.models"] = fake_vertex_models

    spec = importlib.util.spec_from_file_location(
        "vertex_request_adapter", ROOT / "app/api/vertex_request_adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VertexRequestAdapterTestCase(unittest.TestCase):
    def test_build_vertex_openai_request(self):
        module = load_vertex_request_adapter()
        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            messages=[
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好"},
            ],
            temperature=0.7,
            max_tokens=256,
            top_p=0.9,
            top_k=40,
            stream=True,
            stop=["END"],
            presence_penalty=0.1,
            frequency_penalty=0.2,
            seed=123,
            logprobs=2,
            response_logprobs=True,
            n=1,
        )

        result = module.build_vertex_openai_request(request)
        self.assertEqual(result.model, "gemini-2.5-pro")
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].role, "system")
        self.assertEqual(result.messages[1].content, "你好")
        self.assertTrue(result.stream)
        self.assertEqual(result.seed, 123)


if __name__ == "__main__":
    unittest.main()
