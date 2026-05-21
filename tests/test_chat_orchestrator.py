import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_chat_orchestrator():
    fake_app = types.ModuleType("app")
    fake_app.__path__ = [str(ROOT / "app")]
    fake_api_pkg = types.ModuleType("app.api")
    fake_api_pkg.__path__ = [str(ROOT / "app/api")]
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []

    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi.HTTPException = HTTPException
    fake_fastapi.status = types.SimpleNamespace(HTTP_400_BAD_REQUEST=400)

    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.MAX_REQUESTS_PER_MINUTE = 10
    fake_settings.MAX_REQUESTS_PER_DAY_PER_IP = 20
    fake_settings.PUBLIC_MODE = False
    fake_settings.PRECISE_CACHE = False
    fake_settings.CALCULATE_CACHE_ENTRIES = 2
    fake_settings.NONSTREAM_KEEPALIVE_ENABLED = False

    fake_utils = types.ModuleType("app.utils")
    protect_calls = []
    log_calls = []

    async def protect_from_abuse(*args):
        protect_calls.append(args)

    fake_utils.protect_from_abuse = protect_from_abuse
    fake_utils.log = lambda *args, **kwargs: log_calls.append((args, kwargs))
    fake_utils.generate_cache_key = lambda *args, **kwargs: "cache-key"

    fake_error_handling = types.ModuleType("app.utils.error_handling")
    fake_error_handling.sanitize_string = lambda text: text

    sys.modules.update(
        {
            "app": fake_app,
            "app.api": fake_api_pkg,
            "fastapi": fake_fastapi,
            "app.config.settings": fake_settings,
            "app.config": fake_config_pkg,
            "app.utils": fake_utils,
            "app.utils.error_handling": fake_error_handling,
        }
    )

    for name in [
        "app.api.request_helpers",
        "app.api.orchestration_helpers",
        "app.api.chat_orchestrator",
    ]:
        sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location(
        "app.api.chat_orchestrator",
        ROOT / "app/api/chat_orchestrator.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._test_state = types.SimpleNamespace(
        settings=fake_settings,
        protect_calls=protect_calls,
        log_calls=log_calls,
    )
    return module


class ChatOrchestratorTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_returns_cached_response_before_creating_task(self):
        module = load_chat_orchestrator()

        async def get_cache(cache_key, *, is_stream, is_gemini):
            return {"cached": cache_key, "stream": is_stream, "gemini": is_gemini}

        runtime = types.SimpleNamespace(get_cache=get_cache)
        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            stream=False,
            format_type=None,
        )

        def should_not_run(*args, **kwargs):
            raise AssertionError("cached path should not create process task")

        result = await module.handle_aistudio_chat_completion(
            request=request,
            http_request="http",
            runtime=runtime,
            available_models=["gemini-2.5-pro"],
            process_stream_request=should_not_run,
            process_nonstream_with_keepalive_stream=should_not_run,
            process_request=should_not_run,
        )

        self.assertEqual(result["cached"], "cache-key")
        self.assertEqual(len(module._test_state.protect_calls), 1)

    async def test_creates_process_task_and_registers_active_request(self):
        module = load_chat_orchestrator()

        async def get_cache(cache_key, *, is_stream, is_gemini):
            return None

        async def process_request(**kwargs):
            return {"processed": kwargs["cache_key"]}

        class ActiveRequests:
            def __init__(self):
                self.items = {}
                self.removed = []

            def get(self, key):
                return self.items.get(key)

            def add(self, key, task):
                self.items[key] = task

            def remove(self, key):
                self.removed.append(key)
                self.items.pop(key, None)

        active_requests = ActiveRequests()
        runtime = types.SimpleNamespace(
            get_cache=get_cache,
            active_requests_manager=active_requests,
            key_manager="key-manager",
            response_cache_manager="cache-manager",
            safety_settings=[],
            safety_settings_g2=[],
        )
        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            stream=False,
            format_type=None,
        )

        result = await module.handle_aistudio_chat_completion(
            request=request,
            http_request="http",
            runtime=runtime,
            available_models=["gemini-2.5-pro"],
            process_stream_request=lambda **kwargs: None,
            process_nonstream_with_keepalive_stream=lambda **kwargs: None,
            process_request=process_request,
        )

        self.assertEqual(result, {"processed": "cache-key"})
        self.assertEqual(active_requests.removed, ["cache-key"])


if __name__ == "__main__":
    unittest.main()
