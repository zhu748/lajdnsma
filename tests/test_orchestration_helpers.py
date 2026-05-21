import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_orchestration_helpers():
    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi.HTTPException = HTTPException

    fake_error_handling = types.ModuleType("app.utils.error_handling")
    fake_error_handling.sanitize_string = lambda text: f"sanitized::{text}"

    sys.modules["fastapi"] = fake_fastapi
    sys.modules["app.utils.error_handling"] = fake_error_handling

    spec = importlib.util.spec_from_file_location(
        "orchestration_helpers", ROOT / "app/api/orchestration_helpers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, HTTPException


class OrchestrationHelpersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_cache_and_active_request_helpers(self):
        module, _ = load_orchestration_helpers()

        async def fake_get_cache(cache_key, is_stream, is_gemini):
            return {"cache_key": cache_key, "is_stream": is_stream, "is_gemini": is_gemini}

        cached = await module.get_cached_response_or_none(
            fake_get_cache, "abc", is_stream=True, is_gemini=False
        )
        self.assertEqual(cached["cache_key"], "abc")

        class Manager:
            pass

        result = await module.reuse_or_wait_active_request(
            public_mode=False,
            active_requests_manager=Manager(),
            pool_key="pool",
            request=object(),
            wait_for_existing_task=lambda manager, pool_key, request: fake_get_cache(pool_key, False, False),
        )
        self.assertEqual(result["cache_key"], "pool")

    async def test_register_remove_and_await_process_task(self):
        module, HTTPException = load_orchestration_helpers()

        class Manager:
            def __init__(self):
                self.added = []
                self.removed = []

            def add(self, key, task):
                self.added.append((key, task))

            def remove(self, key):
                self.removed.append(key)

        manager = Manager()

        async def ok_task():
            return {"ok": True}

        dummy_task = object()
        module.register_active_request_if_needed(
            public_mode=False,
            active_requests_manager=manager,
            pool_key="pool",
            process_task=dummy_task,
        )
        self.assertEqual(manager.added[0][0], "pool")

        result = await module.await_process_task_result(
            process_task=ok_task(),
            public_mode=False,
            active_requests_manager=manager,
            pool_key="pool",
            get_cache_func=lambda *args, **kwargs: None,
            cache_key="cache",
            is_stream=False,
            is_gemini=False,
        )
        self.assertEqual(result, {"ok": True})
        self.assertIn("pool", manager.removed)

        async def bad_task():
            raise RuntimeError("boom")

        async def no_cache(*args, **kwargs):
            return None

        with self.assertRaises(HTTPException) as ctx:
            await module.await_process_task_result(
                process_task=bad_task(),
                public_mode=False,
                active_requests_manager=manager,
                pool_key="pool2",
                get_cache_func=no_cache,
                cache_key="cache",
                is_stream=False,
                is_gemini=False,
            )
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
