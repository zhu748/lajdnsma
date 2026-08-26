"""第三轮优化的回归测试。

覆盖本轮核心修复：
1. H1: vertex/api_helpers 必须导入 vertex_log（三处流式错误路径曾因
   NameError 裸切断客户端 SSE 流）
2. H2: fill 轮换策略下 key 达日额度必须推进到下一个 key（曾因 log()
   未导入抛 NameError，粘性 key 卡死栈顶导致全站选 key 崩溃）
3. H3: 分钟限流桶必须按 IP 键控（曾为全局 path 桶，单用户可 429 全站）
4. M2: 批次 keyed tasks 在调用方取消时必须被取消（孤儿上游请求烧配额）
5. M7: Responses API reasoning.effort 必须映射到 reasoning_effort
6. L9: choices 为空列表时协议转换不得 IndexError
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_optimization_regressions import load_module  # noqa: E402


class VertexHelpersImportTestCase(unittest.TestCase):
    """H1 回归：api_helpers.vertex_log 必须真实存在且可调用。"""

    def test_vertex_log_resolvable(self):
        import app.vertex.api_helpers as m

        self.assertTrue(
            callable(m.vertex_log),
            "vertex_log 未导入——三处流式 except 块将抛 NameError",
        )

    def test_chat_api_imports_resolve(self):
        """M5/H4 附带保障：chat_api 的新增导入（client pool / to_thread 用到的
        asyncio 已有）不产生循环导入。"""
        import app.vertex.routes.chat_api as m

        self.assertTrue(callable(m._get_client_from_pool))


class FillStrategyDailyLimitTestCase(unittest.IsolatedAsyncioTestCase):
    """H2 回归：fill 策略下日额度耗尽的 key 必须被 pop 并推进。"""

    def _load(self, daily_limit):
        fake_http_client = types.ModuleType("app.utils.http_client")

        async def _noop():
            return None

        fake_http_client.get_async_client = _noop
        fake_logging = types.ModuleType("app.utils.logging")
        fake_logging.format_log_message = lambda level, msg: msg
        fake_logging.log = lambda *a, **k: None
        fake_stealth = types.ModuleType("app.utils.stealth")
        fake_stealth.build_key_probe_headers = lambda key: {}
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.GEMINI_API_KEYS = (
            "AIzaSy" + "a" * 33 + "," + "AIzaSy" + "b" * 33
        )
        fake_settings.KEY_ROTATION_STRATEGY = "fill"
        fake_settings.API_KEY_DAILY_LIMIT = daily_limit
        fake_settings.api_call_stats = object()

        # fill 路径内的延迟导入：app.utils.stats.get_api_key_usage
        fake_stats = types.ModuleType("app.utils.stats")

        async def _usage(stats, key, model=None):
            # 栈顶（粘性）key 已达日额度；其余 key 未达。
            return daily_limit if key.endswith("a" * 33) else 0

        fake_stats.get_api_key_usage = _usage
        fake_config_pkg = types.ModuleType("app.config")
        fake_config_pkg.__path__ = []
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []
        fake_app_pkg = types.ModuleType("app")
        fake_app_pkg.__path__ = []

        mod = load_module(
            "api_key_fill",
            "app/utils/api_key.py",
            {
                "app.utils.http_client": fake_http_client,
                "app.utils.logging": fake_logging,
                "app.utils.stealth": fake_stealth,
                "app.config.settings": fake_settings,
                "app.utils.stats": fake_stats,
                "app.config": fake_config_pkg,
                "app.utils": fake_utils_pkg,
                "app": fake_app_pkg,
            },
        )
        return mod

    async def test_exhausted_key_advances_without_nameerror(self):
        mod = self._load(daily_limit=10)
        mgr = mod.APIKeyManager()
        self.assertEqual(len(mgr.api_keys), 2)

        # 把耗尽的 key 固定在栈顶（模拟粘性行为）
        exhausted = [k for k in mgr.api_keys if k.endswith("a" * 33)][0]
        other = [k for k in mgr.api_keys if not k.endswith("a" * 33)][0]
        mgr.key_stack = [other, exhausted]  # 栈顶是 exhausted

        # fill 路径内部是延迟导入 `from app.utils.stats import
        # get_api_key_usage`，发生在调用时而非加载时——把 fake stats
        # 安装到 sys.modules 中覆盖真实的 stats 模块。
        fake_stats = types.ModuleType("app.utils.stats")

        async def _usage(stats, key, model=None):
            return 10 if key.endswith("a" * 33) else 0

        fake_stats.get_api_key_usage = _usage
        saved = sys.modules.get("app.utils.stats")
        sys.modules["app.utils.stats"] = fake_stats
        try:
            # 修复前：此处抛 NameError(name 'log' is not defined)
            chosen = await mgr.get_available_key()
        finally:
            if saved is None:
                sys.modules.pop("app.utils.stats", None)
            else:
                sys.modules["app.utils.stats"] = saved
        self.assertEqual(chosen, other)
        # 耗尽的 key 必须已被 pop 出栈
        self.assertNotIn(exhausted, mgr.key_stack)

    async def test_all_keys_exhausted_returns_none(self):
        mod = self._load(daily_limit=0)  # 两个 key 都立即超额
        mgr = mod.APIKeyManager()
        fake_stats = types.ModuleType("app.utils.stats")

        async def _usage(stats, key, model=None):
            return 999  # 一律超頌

        fake_stats.get_api_key_usage = _usage
        saved = sys.modules.get("app.utils.stats")
        sys.modules["app.utils.stats"] = fake_stats
        try:
            # 修复前：NameError 阻断循环；修复后：正常返回 None
            self.assertIsNone(await mgr.get_available_key())
        finally:
            if saved is None:
                sys.modules.pop("app.utils.stats", None)
            else:
                sys.modules["app.utils.stats"] = saved


class MinuteBucketPerIpTestCase(unittest.IsolatedAsyncioTestCase):
    """H3 回归：分钟限流桶按 (ip, path, minute) 键控，IP 间互不影响。"""

    def _load(self):
        fake_fastapi = types.ModuleType("fastapi")

        class FakeHTTPException(Exception):
            def __init__(self, status_code=500, detail=None):
                super().__init__(str(detail))
                self.status_code = status_code
                self.detail = detail

        fake_fastapi.HTTPException = FakeHTTPException
        fake_fastapi.Request = object
        return load_module(
            "rate_limiting_r3",
            "app/utils/rate_limiting.py",
            {"fastapi": fake_fastapi},
        )

    @staticmethod
    def _make_request(host, path="/v1/chat/completions"):
        class FakeURL:
            def __init__(self, path):
                self.path = path

        class FakeClient:
            def __init__(self, host):
                self.host = host

        class FakeRequest:
            def __init__(self):
                self.url = FakeURL(path)
                self.client = FakeClient(host)

        return FakeRequest()

    async def test_one_ip_exhausting_does_not_block_other_ip(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0

        # IP A 用完每分钟额度（阈值 3，第 4 次 429）
        for _ in range(3):
            await mod.protect_from_abuse(
                self._make_request("1.1.1.1"), max_requests_per_minute=3
            )
        with self.assertRaises(Exception):
            await mod.protect_from_abuse(
                self._make_request("1.1.1.1"), max_requests_per_minute=3
            )

        # 修复前：分钟桶是全局 path 桶，IP B 的第一条请求就会吃到
        # count=4 → 429（单用户拒绝全站）。修复后 IP B 正常通过。
        await mod.protect_from_abuse(
            self._make_request("2.2.2.2"), max_requests_per_minute=3
        )

    async def test_minute_key_contains_host(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0
        await mod.protect_from_abuse(self._make_request("3.3.3.3"))
        keys = [k for k in mod.rate_limit_data.keys() if ":" in k]
        minute_keys = [
            k for k in keys if mod.rate_limit_data[k][2] == mod._MINUTE_TTL_S
        ]
        self.assertTrue(
            any(k.startswith("3.3.3.3:") for k in minute_keys),
            f"分钟桶键应包含客户端 IP: {minute_keys}",
        )

    async def test_missing_client_info_does_not_crash(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0

        class NoClientRequest:
            url = type("U", (), {"path": "/v1/chat/completions"})()
            client = None

        # request.client 为 None（某些测试传输/ASGI 包装）时不得 AttributeError
        await mod.protect_from_abuse(NoClientRequest())


class BatchRunnerCancelsOnAbortTestCase(unittest.IsolatedAsyncioTestCase):
    """M2 回归：调用方取消时批次内 pending tasks 必须被取消。"""

    def _load(self):
        fake_status_handlers = types.ModuleType("app.api.nonstream_status_handlers")

        async def _status(**kwargs):
            return ("pending", None, kwargs.get("empty_response_count", 0))

        fake_status_handlers.handle_nonstream_task_status = _status
        fake_retry_state = types.ModuleType("app.utils.retry_state")
        self._cancelled_batches = []

        def cancel_pending_tasks(tasks):
            self._cancelled_batches.append(list(tasks))
            # 与真实实现一致：真实取消挂起任务
            for _key, task in tasks:
                task.cancel()

        def remove_completed_tasks(tasks):
            return [t for t in tasks if not t[1].done()]

        fake_retry_state.cancel_pending_tasks = cancel_pending_tasks
        fake_retry_state.remove_completed_tasks = remove_completed_tasks
        return load_module(
            "nonstream_batch_r3",
            "app/api/nonstream_batch_runner.py",
            {
                "app.api.nonstream_status_handlers": fake_status_handlers,
                "app.utils.retry_state": fake_retry_state,
            },
        )

    async def test_cancellation_cancels_pending_tasks(self):
        mod = self._load()

        async def slow():
            await asyncio.sleep(30)

        tasks = [
            ("key1", asyncio.create_task(slow())),
            ("key2", asyncio.create_task(slow())),
        ]
        tasks_map = {t: k for k, t in tasks}

        async def run():
            return await mod.run_nonstream_batch_until_success(
                tasks=tasks,
                tasks_map=tasks_map,
                chat_request=None,
                response_cache_manager=None,
                cache_key="ck",
                is_gemini=False,
                empty_response_count=0,
            )

        runner = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner

        # 修复前：CancelledError 直接穿出，两个 30s 的孤儿任务继续跑。
        self.assertEqual(len(self._cancelled_batches), 1)
        cancelled = self._cancelled_batches[0]
        self.assertEqual(len(cancelled), 2)
        # 让取消送达后再断言
        await asyncio.sleep(0)
        for _key, task in cancelled:
            self.assertTrue(task.cancelled() or task.done())


class ResponsesReasoningEffortTestCase(unittest.TestCase):
    """M7 回归：Responses API reasoning.effort → reasoning_effort。"""

    def test_effort_mapped(self):
        from app.utils.protocol_requests import response_request_to_chat_request

        req = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": "hi",
                "reasoning": {"effort": "high"},
            }
        )
        self.assertEqual(req.reasoning_effort, "high")

    def test_invalid_effort_ignored(self):
        from app.utils.protocol_requests import response_request_to_chat_request

        req = response_request_to_chat_request(
            {
                "model": "gemini-2.5-pro",
                "input": "hi",
                "reasoning": {"effort": "absurd"},
            }
        )
        self.assertIsNone(req.reasoning_effort)

    def test_no_reasoning_field(self):
        from app.utils.protocol_requests import response_request_to_chat_request

        req = response_request_to_chat_request(
            {"model": "gemini-2.5-pro", "input": "hi"}
        )
        self.assertIsNone(req.reasoning_effort)


class EmptyChoicesNoIndexErrorTestCase(unittest.TestCase):
    """L9 回归：choices 为空列表（usage-only chunk）不得 IndexError。"""

    def test_response_api_with_empty_choices(self):
        from app.utils.protocol_nonstream import openai_chat_to_response_api

        result = openai_chat_to_response_api(
            {"choices": [], "usage": {"prompt_tokens": 1}}, None
        )
        self.assertIsInstance(result, dict)

    def test_claude_response_with_empty_choices(self):
        from app.utils.protocol_nonstream import openai_chat_to_claude_response

        result = openai_chat_to_claude_response({"choices": [], "usage": {}})
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
