import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_response_module():
    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.sse_data = lambda payload: f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    sys.modules["app.utils.sse"] = fake_sse

    spec = importlib.util.spec_from_file_location(
        "response",
        ROOT / "app/utils/response.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ResponseTestCase(unittest.TestCase):
    def test_gemini_empty_stream_chunk_has_timing_and_empty_text(self):
        module = load_response_module()

        chunk = module.gemini_from_text(content="", stream=True)

        self.assertTrue(chunk.startswith("data: "))
        payload = json.loads(chunk.removeprefix("data: ").strip())
        self.assertIn("responseId", payload)
        self.assertIn("createTime", payload)
        self.assertEqual(
            payload["candidates"][0]["content"]["parts"],
            [{"text": ""}],
        )


if __name__ == "__main__":
    unittest.main()
