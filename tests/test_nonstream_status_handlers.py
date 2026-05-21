import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_status_module():
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.MAX_EMPTY_RESPONSES = 3

    fake_error_handling = types.ModuleType("app.utils.error_handling")
    errors = []
    fake_error_handling.handle_gemini_error = lambda error, api_key: errors.append((error, api_key))

    fake_response = types.ModuleType("app.utils.response")
    fake_response.openAI_from_Gemini = lambda cached_response, stream=False: {
        "converted": cached_response.data,
        "stream": stream,
    }

    fake_loop_helpers = types.ModuleType("app.utils.response_loop_helpers")
    logs = []
    fake_loop_helpers.dump_json_response = lambda response: f"json:{response}"
    fake_loop_helpers.log_empty_response_count = lambda *args, **kwargs: logs.append(("empty", args, kwargs))
    fake_loop_helpers.log_request_success = lambda *args, **kwargs: logs.append(("success", args, kwargs))

    sys.modules.update(
        {
            "app.config.settings": fake_settings,
            "app.config": fake_config_pkg,
            "app.utils.error_handling": fake_error_handling,
            "app.utils.response": fake_response,
            "app.utils.response_loop_helpers": fake_loop_helpers,
        }
    )

    spec = importlib.util.spec_from_file_location(
        "nonstream_status_handlers",
        ROOT / "app/api/nonstream_status_handlers.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._logs = logs
    module._errors = errors
    return module


class NonstreamStatusHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_success_status_returns_converted_response(self):
        module = load_status_module()

        class Task:
            def result(self):
                return "success"

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(data={"ok": True}), True

        request = types.SimpleNamespace(model="m")
        status, response, empty_count = await module.handle_nonstream_task_status(
            task=Task(),
            api_key="apikey123",
            chat_request=request,
            response_cache_manager=Cache(),
            cache_key="cache",
            is_gemini=False,
            empty_response_count=0,
        )
        self.assertEqual(status, "success")
        self.assertEqual(response["converted"], {"ok": True})
        self.assertEqual(empty_count, 0)

    async def test_empty_and_error_statuses(self):
        module = load_status_module()

        class EmptyTask:
            def result(self):
                return "empty"

        class ErrorTask:
            def result(self):
                raise RuntimeError("boom")

        request = types.SimpleNamespace(model="m")
        status, response, empty_count = await module.handle_nonstream_task_status(
            task=EmptyTask(),
            api_key="apikey123",
            chat_request=request,
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=True,
            empty_response_count=1,
        )
        self.assertEqual((status, response, empty_count), ("empty", None, 2))

        status, response, empty_count = await module.handle_nonstream_task_status(
            task=ErrorTask(),
            api_key="apikey123",
            chat_request=request,
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=True,
            empty_response_count=2,
        )
        self.assertEqual((status, response, empty_count), ("error", None, 2))
        self.assertEqual(len(module._errors), 1)


if __name__ == "__main__":
    unittest.main()
