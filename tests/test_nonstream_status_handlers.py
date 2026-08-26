import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 哨兵：区分「sys.modules 中没有该键」与「值为 None」。
_MISSING = object()


def load_status_module():
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.MAX_EMPTY_RESPONSES = 3

    fake_error_handling = types.ModuleType("app.utils.error_handling")
    errors = []
    fake_error_handling.handle_gemini_error = lambda error, api_key: errors.append((error, api_key))

    fake_response = types.ModuleType("app.utils.response")
    fake_response.ensure_gemini_timing_fields = lambda data: {**data, "timing": True}
    fake_response.include_reasoning_for_request = (
        lambda request: getattr(request, "enable_thinking", True)
    )
    fake_response.openAI_from_Gemini = lambda cached_response, stream=False, include_reasoning=True: {
        "converted": cached_response.data,
        "stream": stream,
        "include_reasoning": include_reasoning,
    }

    fake_loop_helpers = types.ModuleType("app.utils.response_loop_helpers")
    logs = []
    fake_loop_helpers.dump_json_response = lambda response: f"json:{response}"
    fake_loop_helpers.log_empty_response_count = lambda *args, **kwargs: logs.append(("empty", args, kwargs))
    fake_loop_helpers.log_request_success = lambda *args, **kwargs: logs.append(("success", args, kwargs))

    fake_logging = types.ModuleType("app.utils.logging")
    fake_logging.log = lambda *args, **kwargs: logs.append(("log", args, kwargs))

    stubbed = {
        "app.config.settings": fake_settings,
        "app.config": fake_config_pkg,
        "app.utils.error_handling": fake_error_handling,
        "app.utils.logging": fake_logging,
        "app.utils.response": fake_response,
        "app.utils.response_loop_helpers": fake_loop_helpers,
    }
    # 隔离：exec 完成后恢复 sys.modules 原值，避免假桩泄漏到同进程的
    # 其他测试（该文件原有的 update 模式无恢复，靠运气存活）。
    saved = {name: sys.modules.get(name, _MISSING) for name in stubbed}
    sys.modules.update(stubbed)
    try:
        spec = importlib.util.spec_from_file_location(
            "nonstream_status_handlers",
            ROOT / "app/api/nonstream_status_handlers.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
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

    async def test_success_status_adds_gemini_timing_fields(self):
        module = load_status_module()

        class Task:
            def result(self):
                return "success"

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(data={"ok": True}), True

        request = types.SimpleNamespace(model="m")
        status, response, _ = await module.handle_nonstream_task_status(
            task=Task(),
            api_key="apikey123",
            chat_request=request,
            response_cache_manager=Cache(),
            cache_key="cache",
            is_gemini=True,
            empty_response_count=0,
        )
        self.assertEqual(status, "success")
        self.assertTrue(response["timing"])

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


class StolenCacheTestCase(unittest.IsolatedAsyncioTestCase):
    """R2-M3 回归：任务成功但缓存被并发请求消费时不得崩溃。"""

    async def test_success_with_stolen_cache_returns_error_status(self):
        module = load_status_module()

        class SuccessTask:
            def result(self):
                return "success"

        class StolenCacheManager:
            """模拟缓存条目已被另一并发请求消费（get_and_remove 未命中）。"""

            async def get_and_remove(self, cache_key):
                return None, False

        request = types.SimpleNamespace(model="m")
        # 旧实现在此处 cached_response.data 抛 AttributeError（被外层
        # except 记为该 key error）；现在显式返回 "error" 状态。
        status, response, empty_count = await module.handle_nonstream_task_status(
            task=SuccessTask(),
            api_key="apikey123",
            chat_request=request,
            response_cache_manager=StolenCacheManager(),
            cache_key="cache",
            is_gemini=True,
            empty_response_count=0,
        )
        self.assertEqual(status, "error")
        self.assertIsNone(response)
        self.assertEqual(empty_count, 0)


if __name__ == "__main__":
    unittest.main()
