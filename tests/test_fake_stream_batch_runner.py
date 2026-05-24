import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def done_future(value):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


def load_fake_batch_runner():
    fake_utils = types.ModuleType("app.utils")
    errors = []
    fake_utils.handle_gemini_error = lambda error, api_key: errors.append((error, api_key)) or str(error)
    fake_utils.openAI_from_text = lambda model, content, stream=True: {"openai_text": content, "model": model, "stream": stream}

    fake_response = types.ModuleType("app.utils.response")
    fake_response.ensure_gemini_timing_fields = lambda data: data
    fake_response.gemini_from_text = lambda content, stream=True: {"gemini": content, "stream": stream}
    fake_response.include_reasoning_for_request = (
        lambda request, expose_protocol_thinking=False: expose_protocol_thinking
    )
    fake_response.openAI_from_Gemini = lambda cached_response, stream=True, include_reasoning=True: {
        "openai": cached_response.data,
        "stream": stream,
        "include_reasoning": include_reasoning,
    }

    fake_loop = types.ModuleType("app.utils.response_loop_helpers")
    logs = []
    fake_loop.dump_json_response = lambda data: f"json:{data}"
    fake_loop.log_empty_response_count = lambda *args, **kwargs: logs.append(("empty", args, kwargs))
    fake_loop.log_request_failure = lambda *args, **kwargs: logs.append(("failure", args, kwargs))
    fake_loop.log_request_success = lambda *args, **kwargs: logs.append(("success", args, kwargs))

    fake_retry = types.ModuleType("app.utils.retry_state")
    fake_retry.cancel_pending_tasks = lambda tasks: [task.cancel() for _, task in tasks if not task.done()]
    fake_retry.remove_completed_tasks = lambda tasks: [item for item in tasks if not item[1].done()]

    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.sse_text = lambda data: f"data: {data}\n\n"

    sys.modules.update(
        {
            "app.utils": fake_utils,
            "app.utils.response": fake_response,
            "app.utils.response_loop_helpers": fake_loop,
            "app.utils.retry_state": fake_retry,
            "app.utils.sse": fake_sse,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "fake_stream_batch_runner",
        ROOT / "app/api/fake_stream_batch_runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._logs = logs
    module._errors = errors
    return module


class FakeStreamBatchRunnerTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_success_yields_cached_response_and_summary(self):
        module = load_fake_batch_runner()
        task = done_future("success")

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(data={"ok": True}), True

        settings = types.SimpleNamespace(FAKE_STREAMING_INTERVAL=0.01, MAX_EMPTY_RESPONSES=3)
        request = types.SimpleNamespace(model="m")
        items = []
        async for item in module.run_fake_stream_batch_until_success(
            tasks=[("key123456", task)],
            tasks_map={task: "key123456"},
            chat_request=request,
            response_cache_manager=Cache(),
            cache_key="cache",
            is_gemini=True,
            empty_response_count=0,
            settings=settings,
        ):
            items.append(item)

        self.assertEqual(items[0], ("chunk", {"gemini": "", "stream": True}))
        self.assertEqual(items[1], ("chunk", "data: json:{'ok': True}\n\n"))
        self.assertEqual(items[-1][0], "summary")
        self.assertTrue(items[-1][1]["success"])

    async def test_success_cancels_pending_attempts(self):
        module = load_fake_batch_runner()
        success_task = done_future("success")
        pending_task = asyncio.create_task(asyncio.sleep(60))

        class Cache:
            async def get_and_remove(self, key):
                return types.SimpleNamespace(data={"ok": True}), True

        settings = types.SimpleNamespace(FAKE_STREAMING_INTERVAL=0.01, MAX_EMPTY_RESPONSES=3)
        request = types.SimpleNamespace(model="m")
        async for _ in module.run_fake_stream_batch_until_success(
            tasks=[("key123456", success_task), ("keyabcdef", pending_task)],
            tasks_map={success_task: "key123456", pending_task: "keyabcdef"},
            chat_request=request,
            response_cache_manager=Cache(),
            cache_key="cache",
            is_gemini=False,
            empty_response_count=0,
            settings=settings,
        ):
            pass

        await asyncio.sleep(0)
        self.assertTrue(pending_task.cancelled())

    async def test_empty_exhausts_with_updated_count(self):
        module = load_fake_batch_runner()
        task = done_future("empty")
        settings = types.SimpleNamespace(FAKE_STREAMING_INTERVAL=0.01, MAX_EMPTY_RESPONSES=3)
        request = types.SimpleNamespace(model="m")
        items = []
        async for item in module.run_fake_stream_batch_until_success(
            tasks=[("key123456", task)],
            tasks_map={task: "key123456"},
            chat_request=request,
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=False,
            empty_response_count=1,
            settings=settings,
        ):
            items.append(item)

        self.assertEqual(items[-1][0], "summary")
        self.assertFalse(items[-1][1]["success"])
        self.assertEqual(items[-1][1]["empty_response_count"], 2)

    async def test_timeout_yields_keepalive_then_can_finish(self):
        module = load_fake_batch_runner()
        task = asyncio.create_task(asyncio.sleep(0.02, result="empty"))
        settings = types.SimpleNamespace(FAKE_STREAMING_INTERVAL=0.001, MAX_EMPTY_RESPONSES=3)
        request = types.SimpleNamespace(model="m")
        items = []
        async for item in module.run_fake_stream_batch_until_success(
            tasks=[("key123456", task)],
            tasks_map={task: "key123456"},
            chat_request=request,
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=True,
            empty_response_count=0,
            settings=settings,
        ):
            items.append(item)

        self.assertEqual(items[0][0], "chunk")
        self.assertEqual(items[-1][0], "summary")


if __name__ == "__main__":
    unittest.main()
