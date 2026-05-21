import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_batch_runner(status_sequence):
    fake_status = types.ModuleType("app.api.nonstream_status_handlers")
    calls = []
    sequence = list(status_sequence)

    async def handle_nonstream_task_status(**kwargs):
        calls.append(kwargs)
        status, response, empty_count = sequence.pop(0)
        return status, response, empty_count

    fake_status.handle_nonstream_task_status = handle_nonstream_task_status

    fake_retry = types.ModuleType("app.utils.retry_state")
    fake_retry.remove_completed_tasks = lambda tasks: [item for item in tasks if not item[1].done()]

    sys.modules.update(
        {
            "app.api.nonstream_status_handlers": fake_status,
            "app.utils.retry_state": fake_retry,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "nonstream_batch_runner",
        ROOT / "app/api/nonstream_batch_runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._calls = calls
    return module


def done_future():
    fut = asyncio.get_running_loop().create_future()
    fut.set_result("stub")
    return fut


class NonstreamBatchRunnerTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_success_result_short_circuits_batch(self):
        module = load_batch_runner([("success", {"ok": True}, 0)])
        task = done_future()
        result = await module.run_nonstream_batch_until_success(
            tasks=[("key", task)],
            tasks_map={task: "key"},
            chat_request=types.SimpleNamespace(model="m"),
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=False,
            empty_response_count=0,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["response"], {"ok": True})

    async def test_empty_exhausts_batch_with_updated_count(self):
        module = load_batch_runner([("empty", None, 2)])
        task = done_future()
        result = await module.run_nonstream_batch_until_success(
            tasks=[("key", task)],
            tasks_map={task: "key"},
            chat_request=types.SimpleNamespace(model="m"),
            response_cache_manager=None,
            cache_key="cache",
            is_gemini=True,
            empty_response_count=1,
        )
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["empty_response_count"], 2)
        self.assertEqual(result["tasks"], [])

    async def test_pending_when_timeout_has_no_done_tasks(self):
        module = load_batch_runner([])
        task = asyncio.create_task(asyncio.sleep(0.05))
        try:
            result = await module.run_nonstream_batch_until_success(
                tasks=[("key", task)],
                tasks_map={task: "key"},
                chat_request=types.SimpleNamespace(model="m"),
                response_cache_manager=None,
                cache_key="cache",
                is_gemini=True,
                empty_response_count=0,
                wait_timeout=0.001,
            )
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["tasks"], [("key", task)])
        finally:
            task.cancel()


if __name__ == "__main__":
    unittest.main()
