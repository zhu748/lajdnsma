"""保活 yield 期间断开时，真实处理链仍须回收上游任务。"""
from pathlib import Path
import subprocess
import sys


def test_disconnect_during_keepalive_cancels_upstream_batch():
    # 现有测试会替换 app/fastapi 包；用独立进程验证真实处理链及导入，
    # 只模拟外部 API 调用和密钥选择，不执行应用启动或网络请求。
    code = r'''
import asyncio
from unittest.mock import patch
import app.api.nonstream_handlers as handlers
import app.config.settings as settings

async def run():
    pending = []

    async def keys(**kwargs):
        return ["test-key"]

    async def upstream(*args, **kwargs):
        await asyncio.Event().wait()

    async def batch(**kwargs):
        pending.extend(task for _, task in kwargs["tasks"])
        return {"status": "pending", "tasks": kwargs["tasks"], "empty_response_count": 0}

    with (
        patch.object(handlers, "select_valid_api_keys", keys),
        patch.object(handlers, "process_nonstream_request", upstream),
        patch.object(handlers, "run_nonstream_batch_until_success", batch),
        patch.object(handlers, "prepare_request_messages", lambda request: (False, [], None)),
        patch.object(handlers, "effective_max_retries", lambda: 1),
        patch.object(settings, "CONCURRENT_REQUESTS", 1),
        patch.object(settings, "MAX_EMPTY_RESPONSES", 3),
    ):
        from types import SimpleNamespace
        response = await handlers.process_nonstream_with_keepalive_stream(
            SimpleNamespace(model="test-model"), None, None, [], [], "cache", False,
        )
        try:
            assert await anext(response.body_iterator) == "\n"
            assert len(pending) == 1
            await response.body_iterator.aclose()
            await asyncio.sleep(0)
            assert pending[0].cancelled(), "disconnect left the upstream task running"
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

asyncio.run(run())
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
