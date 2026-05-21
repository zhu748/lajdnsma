import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str, extra_modules: dict | None = None):
    if extra_modules:
        sys.modules.update(extra_modules)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RuntimeHelpersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_route_runtime_cache_stream_openai_includes_done(self):
        fake_fastapi = types.ModuleType("fastapi")
        fake_fastapi.HTTPException = Exception
        fake_fastapi.Request = object
        fake_fastapi.status = types.SimpleNamespace(HTTP_403_FORBIDDEN=403)

        fake_responses = types.ModuleType("fastapi.responses")

        class StreamingResponse:
            def __init__(self, body_iterator=None, media_type=None):
                self.body_iterator = body_iterator
                self.media_type = media_type

        fake_responses.StreamingResponse = StreamingResponse

        fake_config_pkg = types.ModuleType("app.config")
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.WHITELIST_USER_AGENT = []

        fake_utils = types.ModuleType("app.utils")
        fake_utils.log = lambda *args, **kwargs: None

        fake_response = types.ModuleType("app.utils.response")
        fake_response.ensure_gemini_timing_fields = lambda data: data
        fake_response.openAI_from_Gemini = (
            lambda cached_response, stream=True: "data: openai\n\n"
        )

        fake_sse = types.ModuleType("app.utils.sse")
        fake_sse.sse_data = lambda payload: f"data: {payload}\n\n"
        fake_sse.sse_done = lambda: "data: [DONE]\n\n"

        module = load_module(
            "route_runtime",
            "app/api/route_runtime.py",
            {
                "fastapi": fake_fastapi,
                "fastapi.responses": fake_responses,
                "app.config": fake_config_pkg,
                "app.config.settings": fake_settings,
                "app.utils": fake_utils,
                "app.utils.response": fake_response,
                "app.utils.sse": fake_sse,
            },
        )

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(model="m", data={"ok": True}), True

        module.response_cache_manager = Cache()

        response = await module.get_cache("cache-key", is_stream=True, is_gemini=False)

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.body_iterator, "data: openai\n\ndata: [DONE]\n\n")

    async def test_route_runtime_cache_gemini_adds_timing(self):
        fake_fastapi = types.ModuleType("fastapi")
        fake_fastapi.HTTPException = Exception
        fake_fastapi.Request = object
        fake_fastapi.status = types.SimpleNamespace(HTTP_403_FORBIDDEN=403)

        fake_responses = types.ModuleType("fastapi.responses")
        fake_responses.StreamingResponse = object

        fake_config_pkg = types.ModuleType("app.config")
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.WHITELIST_USER_AGENT = []

        fake_utils = types.ModuleType("app.utils")
        fake_utils.log = lambda *args, **kwargs: None

        fake_response = types.ModuleType("app.utils.response")
        fake_response.ensure_gemini_timing_fields = (
            lambda data: {**data, "createTime": "now"}
        )
        fake_response.openAI_from_Gemini = lambda *args, **kwargs: None

        fake_sse = types.ModuleType("app.utils.sse")
        fake_sse.sse_data = lambda payload: f"data: {payload}\n\n"
        fake_sse.sse_done = lambda: "data: [DONE]\n\n"

        module = load_module(
            "route_runtime",
            "app/api/route_runtime.py",
            {
                "fastapi": fake_fastapi,
                "fastapi.responses": fake_responses,
                "app.config": fake_config_pkg,
                "app.config.settings": fake_settings,
                "app.utils": fake_utils,
                "app.utils.response": fake_response,
                "app.utils.sse": fake_sse,
            },
        )

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(model="m", data={"ok": True}), True

        module.response_cache_manager = Cache()

        response = await module.get_cache("cache-key", is_stream=False, is_gemini=True)

        self.assertEqual(response, {"ok": True, "createTime": "now"})

    async def test_route_runtime_cache_gemini_stream_adds_timing(self):
        fake_fastapi = types.ModuleType("fastapi")
        fake_fastapi.HTTPException = Exception
        fake_fastapi.Request = object
        fake_fastapi.status = types.SimpleNamespace(HTTP_403_FORBIDDEN=403)

        fake_responses = types.ModuleType("fastapi.responses")

        class StreamingResponse:
            def __init__(self, body_iterator=None, media_type=None):
                self.body_iterator = body_iterator
                self.media_type = media_type

        fake_responses.StreamingResponse = StreamingResponse

        fake_config_pkg = types.ModuleType("app.config")
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.WHITELIST_USER_AGENT = []

        fake_utils = types.ModuleType("app.utils")
        fake_utils.log = lambda *args, **kwargs: None

        fake_response = types.ModuleType("app.utils.response")
        fake_response.ensure_gemini_timing_fields = (
            lambda data: {**data, "createTime": "now"}
        )
        fake_response.openAI_from_Gemini = lambda *args, **kwargs: None

        fake_sse = types.ModuleType("app.utils.sse")
        fake_sse.sse_data = lambda payload: f"data: {payload}\n\n"
        fake_sse.sse_done = lambda: "data: [DONE]\n\n"

        module = load_module(
            "route_runtime",
            "app/api/route_runtime.py",
            {
                "fastapi": fake_fastapi,
                "fastapi.responses": fake_responses,
                "app.config": fake_config_pkg,
                "app.config.settings": fake_settings,
                "app.utils": fake_utils,
                "app.utils.response": fake_response,
                "app.utils.sse": fake_sse,
            },
        )

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(model="m", data={"ok": True}), True

        module.response_cache_manager = Cache()

        response = await module.get_cache("cache-key", is_stream=True, is_gemini=True)

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.body_iterator, "data: {'ok': True, 'createTime': 'now'}\n\n")

    async def test_empty_response_helpers(self):
        fake_error_response = types.ModuleType("app.utils.error_response")
        fake_logging = types.ModuleType("app.utils.logging")

        log_calls = []

        def build_error_response(**kwargs):
            return kwargs

        def log(level, message, extra=None):
            log_calls.append((level, message, extra))

        fake_error_response.build_error_response = build_error_response
        fake_logging.log = log

        module = load_module(
            "empty_response",
            "app/utils/empty_response.py",
            {
                "app.utils.error_response": fake_error_response,
                "app.utils.logging": fake_logging,
            },
        )

        class Resp:
            text = ""
            function_call = None

        self.assertTrue(module.is_empty_gemini_response(None))
        self.assertTrue(module.is_empty_gemini_response(Resp()))
        response = module.build_empty_limit_response(True, "gemini-2.5-pro", False)
        self.assertEqual(response["content"], module.EMPTY_RESPONSE_LIMIT_MESSAGE)
        module.log_empty_response_limit(2, 5, "stream", "gemini-2.5-pro")
        self.assertEqual(log_calls[0][0], "warning")

    async def test_finalize_gemini_response(self):
        fake_app = types.ModuleType("app")
        fake_config = types.ModuleType("app.config")
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.api_call_stats = {}

        fake_utils = types.ModuleType("app.utils")
        update_calls = []

        async def update_api_call_stats(stats, endpoint, model, token):
            update_calls.append((stats, endpoint, model, token))

        fake_utils.update_api_call_stats = update_api_call_stats

        fake_empty_response = types.ModuleType("app.utils.empty_response")
        fake_logging = types.ModuleType("app.utils.logging")
        log_calls = []

        def is_empty_gemini_response(response_content):
            return not response_content.text and not response_content.function_call

        def log(level, message, extra=None):
            log_calls.append((level, message, extra))

        fake_empty_response.is_empty_gemini_response = is_empty_gemini_response
        fake_logging.log = log

        module = load_module(
            "gemini_response_processing",
            "app/utils/gemini_response_processing.py",
            {
                "app": fake_app,
                "app.config": fake_config,
                "app.config.settings": fake_settings,
                "app.utils": fake_utils,
                "app.utils.empty_response": fake_empty_response,
                "app.utils.logging": fake_logging,
            },
        )

        class FakeCache:
            def __init__(self):
                self.items = []

            async def store(self, key, response):
                self.items.append((key, response))

        class FakeResponse:
            def __init__(self, text="ok", function_call=None, total=7):
                self.text = text
                self.function_call = function_call
                self.total_token_count = total
                self.model = None

            def set_model(self, model):
                self.model = model

        cache = FakeCache()
        response = FakeResponse()
        result = await module.finalize_gemini_response(
            response,
            api_key="AIza-test",
            request_type="non-stream",
            model="gemini-2.5-pro",
            response_cache_manager=cache,
            cache_key="abc",
        )
        self.assertEqual(result, "success")
        self.assertEqual(response.model, "gemini-2.5-pro")
        self.assertEqual(len(cache.items), 1)
        self.assertEqual(len(update_calls), 1)

        empty_result = await module.finalize_gemini_response(
            FakeResponse(text="", function_call=None),
            api_key="AIza-test",
            request_type="fake-stream",
            model="gemini-2.5-pro",
            response_cache_manager=cache,
            cache_key="empty",
            update_stats=False,
        )
        self.assertEqual(empty_result, "empty")
        self.assertTrue(log_calls)


if __name__ == "__main__":
    unittest.main()
