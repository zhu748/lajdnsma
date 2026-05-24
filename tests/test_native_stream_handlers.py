import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_native_stream_module(chunks=None, raise_error=False):
    fake_services = types.ModuleType("app.services")

    class GeminiClient:
        def __init__(self, api_key):
            self.api_key = api_key

        async def stream_chat(self, *args, **kwargs):
            if raise_error:
                raise RuntimeError("boom")
            for chunk in chunks or []:
                yield chunk

    fake_services.GeminiClient = GeminiClient

    fake_utils = types.ModuleType("app.utils")
    stat_calls = []
    errors = []

    async def update_api_call_stats(stats, endpoint, model, token):
        stat_calls.append((stats, endpoint, model, token))

    def handle_gemini_error(error, api_key):
        errors.append((error, api_key))
        return str(error)

    fake_utils.update_api_call_stats = update_api_call_stats
    fake_utils.handle_gemini_error = handle_gemini_error

    fake_processing = types.ModuleType("app.utils.gemini_response_processing")
    fake_processing.select_safety_settings = lambda model, s1, s2: s2 if "2.5" in model else s1

    fake_response = types.ModuleType("app.utils.response")
    fake_response.ensure_gemini_timing_fields = lambda data: {**data, "timing": True}
    fake_response.openAI_from_Gemini = lambda chunk, stream=True, include_reasoning=True: {
        "chunk": chunk.data,
        "stream": stream,
        "include_reasoning": include_reasoning,
    }

    fake_loop = types.ModuleType("app.utils.response_loop_helpers")
    logs = []
    fake_loop.dump_json_response = lambda data: f"json:{data}"
    fake_loop.log_empty_response_count = lambda *args, **kwargs: logs.append(("empty", args, kwargs))
    fake_loop.log_request_failure = lambda *args, **kwargs: logs.append(("failure", args, kwargs))

    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.sse_text = lambda data: f"data: {data}\n\n"

    sys.modules.update(
        {
            "app.services": fake_services,
            "app.utils": fake_utils,
            "app.utils.gemini_response_processing": fake_processing,
            "app.utils.response": fake_response,
            "app.utils.response_loop_helpers": fake_loop,
            "app.utils.sse": fake_sse,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "native_stream_handlers",
        ROOT / "app/api/native_stream_handlers.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._stat_calls = stat_calls
    module._errors = errors
    module._logs = logs
    return module


class NativeStreamHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_yields_chunks_and_summary(self):
        chunk = types.SimpleNamespace(data={"ok": True}, total_token_count=7)
        module = load_native_stream_module([chunk])
        request = types.SimpleNamespace(model="gemini-2.5-pro")
        settings = types.SimpleNamespace(MAX_EMPTY_RESPONSES=3, api_call_stats={})

        items = []
        async for item in module.generate_native_stream_chunks(
            api_key="apikey123",
            chat_request=request,
            contents=[],
            system_instruction=None,
            safety_settings=[],
            safety_settings_g2=[],
            is_gemini=True,
            settings=settings,
        ):
            items.append(item)

        self.assertEqual(items[0], ("chunk", "data: json:{'ok': True, 'timing': True}\n\n"))
        self.assertEqual(items[-1][0], "summary")
        self.assertTrue(items[-1][1]["success"])
        self.assertEqual(module._stat_calls[0][3], 7)

    async def test_empty_chunk_marks_empty(self):
        module = load_native_stream_module([None])
        request = types.SimpleNamespace(model="m")
        settings = types.SimpleNamespace(MAX_EMPTY_RESPONSES=3, api_call_stats={})

        items = []
        async for item in module.generate_native_stream_chunks(
            api_key="apikey123",
            chat_request=request,
            contents=[],
            system_instruction=None,
            safety_settings=[],
            safety_settings_g2=[],
            is_gemini=False,
            settings=settings,
        ):
            items.append(item)

        self.assertEqual(items[-1][1]["empty"], True)
        self.assertFalse(items[-1][1]["success"])
        self.assertEqual(len(module._logs), 1)


if __name__ == "__main__":
    unittest.main()
