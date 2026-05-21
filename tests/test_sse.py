import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_sse_module():
    spec = importlib.util.spec_from_file_location(
        "sse",
        ROOT / "app/utils/sse.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class SseTestCase(unittest.IsolatedAsyncioTestCase):
    def test_sse_format_helpers(self):
        module = load_sse_module()

        self.assertEqual(module.sse_data({"text": "你好"}), 'data: {"text": "你好"}\n\n')
        self.assertEqual(module.sse_text("[DONE]"), "data: [DONE]\n\n")
        self.assertEqual(module.sse_done(), "data: [DONE]\n\n")
        self.assertEqual(
            module.sse_event("message_stop", {"type": "message_stop"}),
            'event: message_stop\ndata: {"type": "message_stop"}\n\n',
        )

    async def test_iter_sse_json_parses_data_variants(self):
        module = load_sse_module()
        response = FakeResponse(
            [
                "event: message",
                'data: {"a": 1}',
                "",
                'data:{"b": 2}',
                "data: [DONE]",
            ]
        )

        items = []
        async for item in module.iter_sse_json(response):
            items.append(item)

        self.assertEqual(items, [{"a": 1}, {"b": 2}])

    async def test_iter_sse_json_buffers_split_payload(self):
        module = load_sse_module()
        response = FakeResponse(
            [
                'data: {"a":',
                "data: 1}",
                "data: [DONE]",
            ]
        )

        items = []
        async for item in module.iter_sse_json(response):
            items.append(item)

        self.assertEqual(items, [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
