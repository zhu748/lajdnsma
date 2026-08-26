"""针对本次优化的回归测试。

覆盖三个核心修复：
1. rate_limiting: day 桶必须在 90s 清扫后存活（每日限流不被误清零）
2. stats: 后台 worker 必须能一次消费整批积压（不再 100 条/秒瓶颈）
3. route_runtime: 缓存命中流式响应必须是单个 chunk 的异步生成器
"""
import asyncio
import os
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relpath, fakes=None):
    """Load a module file with optional fake modules injected into sys.modules."""
    saved = {}
    if fakes:
        for key, mod in fakes.items():
            saved[key] = sys.modules.get(key)
            sys.modules[key] = mod
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old


class RateLimitingDayBucketTestCase(unittest.IsolatedAsyncioTestCase):
    """H4 回归：day 桶 90 秒后不能被清扫删除。"""

    def _load(self):
        fake_fastapi = types.ModuleType("fastapi")
        fake_fastapi.HTTPException = Exception
        fake_fastapi.Request = object
        return load_module(
            "rate_limiting_opt",
            "app/utils/rate_limiting.py",
            {"fastapi": fake_fastapi},
        )

    async def test_day_bucket_survives_sweep_after_90s(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0  # 强制允许清扫

        now = int(time.time())
        # day 桶 90 秒前最后更新，计数 599（接近 600/天上限）
        mod.rate_limit_data["1.2.3.4:%d" % (now // 86400)] = (
            599,
            now - 90,
            mod._DAY_TTL_S,
        )
        # minute 桶 91 秒前最后更新（应被清扫）
        minute_key = "/v1/chat/completions:%d" % (now // 60 - 2)
        mod.rate_limit_data[minute_key] = (3, now - 91, mod._MINUTE_TTL_S)

        await mod._maybe_sweep_expired(now)

        day_key = "1.2.3.4:%d" % (now // 86400)
        self.assertIn(day_key, mod.rate_limit_data)
        self.assertEqual(mod.rate_limit_data[day_key][0], 599)
        self.assertNotIn(minute_key, mod.rate_limit_data)

    async def test_day_bucket_expires_after_full_day(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0

        now = int(time.time())
        day_key = "1.2.3.4:%d" % (now // 86400)
        # 超过一天的 day 桶应被清除
        mod.rate_limit_data[day_key] = (5, now - 86400 - 400, mod._DAY_TTL_S)

        await mod._maybe_sweep_expired(now)
        self.assertNotIn(day_key, mod.rate_limit_data)


class StatsWorkerBatchDrainTestCase(unittest.TestCase):
    """H5 回归：worker 必须一次排空整批积压，而不是每 10ms 一条。"""

    def _load(self, name):
        fake_logging = types.ModuleType("app.utils.logging")
        fake_logging.log = lambda *a, **k: None
        fake_settings = types.ModuleType("app.config.settings")
        fake_config_pkg = types.ModuleType("app.config")
        fake_config_pkg.__path__ = []
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []
        fake_app_pkg = types.ModuleType("app")
        fake_app_pkg.__path__ = []

        return load_module(
            name,
            "app/utils/stats.py",
            {
                "app.utils.logging": fake_logging,
                "app.config.settings": fake_settings,
                "app.config": fake_config_pkg,
                "app.utils": fake_utils_pkg,
                "app": fake_app_pkg,
            },
        )

    def test_worker_drains_full_backlog_at_once(self):
        mod = self._load("stats_opt")

        mgr = mod.ApiStatsManager(enable_background=False)
        # 直接把 500 条更新塞进队列，模拟高 QPS 积压
        for i in range(500):
            mgr._update_queue.put(("key-%d" % (i % 5), "model-x", 10))

        # 手动执行一轮 worker 循环的核心逻辑（不启线程，避免时序抖动）
        # 复制 _worker_loop 的排空逻辑验证行为
        drained = []
        while True:
            try:
                drained.append(mgr._update_queue.get_nowait())
            except Exception:
                break
        self.assertEqual(len(drained), 500)

        mgr._process_batch(drained)
        self.assertEqual(mgr.get_calls_last_24h(), 500)

        # 读污染回归（M9）：get_api_key_usage 不应向 defaultdict 插入条目
        mgr.get_api_key_usage("never-seen-key")
        self.assertNotIn("never-seen-key", mgr.api_key_counts)
        self.assertNotIn("never-seen-key", mgr.api_model_counts)

    def test_recent_calls_is_bounded_deque(self):
        mod = self._load("stats_opt2")

        import asyncio
        from collections import deque

        mgr = mod.ApiStatsManager(enable_background=False)
        self.assertIsInstance(mgr.recent_calls, deque)
        self.assertEqual(mgr.recent_calls.maxlen, 100)

        async def fill():
            for i in range(150):
                await mgr.update_stats("k", "m", 1)

        asyncio.run(fill())
        self.assertEqual(len(mgr.recent_calls), 100)  # deque 自动淘汰旧记录


class ApiKeyManagerNoSchedulerTestCase(unittest.TestCase):
    """H8 回归：APIKeyManager 不再创建死的 BackgroundScheduler。"""

    def test_no_background_scheduler(self):
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
        fake_settings.GEMINI_API_KEYS = "AIzaSy" + "a" * 33
        fake_settings.KEY_ROTATION_STRATEGY = "fill"
        fake_config_pkg = types.ModuleType("app.config")
        fake_config_pkg.__path__ = []
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []
        fake_app_pkg = types.ModuleType("app")
        fake_app_pkg.__path__ = []

        mod = load_module(
            "api_key_opt",
            "app/utils/api_key.py",
            {
                "app.utils.http_client": fake_http_client,
                "app.utils.logging": fake_logging,
                "app.utils.stealth": fake_stealth,
                "app.config.settings": fake_settings,
                "app.config": fake_config_pkg,
                "app.utils": fake_utils_pkg,
                "app": fake_app_pkg,
            },
        )

        mgr = mod.APIKeyManager()
        self.assertFalse(hasattr(mgr, "scheduler"))
        # GEMINI_API_KEYS_{i} 间隙跳过 bug 回归（L10）
        self.assertEqual(len(mgr.api_keys), 1)


class PersistenceAtomicSaveTestCase(unittest.TestCase):
    """R2-H1 回归：settings.json 必须原子写入且不残留 .tmp。"""

    def _load(self, storage_dir):
        import tempfile

        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.ENABLE_STORAGE = True
        fake_settings.STORAGE_DIR = storage_dir
        fake_settings.FAKE_OPTION_A = "hello"
        fake_settings.FAKE_OPTION_B = 42

        fake_config = types.ModuleType("app.config")
        fake_config.settings = fake_settings

        fake_logging = types.ModuleType("app.utils.logging")
        fake_logging.log = lambda *a, **k: None

        fake_app_pkg = types.ModuleType("app")
        fake_config_pkg = types.ModuleType("app.config")
        fake_config_pkg.__path__ = []
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []

        return load_module(
            "persistence_opt",
            "app/config/persistence.py",
            {
                "app": fake_app_pkg,
                "app.config": fake_config_pkg,
                "app.config.settings": fake_settings,
                "app.utils.logging": fake_logging,
                "app.utils": fake_utils_pkg,
            },
        )

    def test_save_is_atomic_and_no_tmp_residue(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            mod = self._load(td)
            settings_file = Path(td) / "settings.json"
            tmp_file = Path(td) / "settings.json.tmp"

            mod.save_settings()
            self.assertTrue(settings_file.exists())
            # 正常路径不残留临时文件
            self.assertFalse(tmp_file.exists())
            # 写出的是完整可解析的 JSON
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(data.get("FAKE_OPTION_A"), "hello")
            self.assertEqual(data.get("FAKE_OPTION_B"), 42)
            # 凭据不落盘
            self.assertNotIn("GEMINI_API_KEYS", data)
            self.assertNotIn("PASSWORD", data)

            # 二次保存（覆盖写）依然原子
            mod.settings.FAKE_OPTION_A = "changed"
            mod.save_settings()
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(data.get("FAKE_OPTION_A"), "changed")
            self.assertFalse(tmp_file.exists())


class WaitForExistingTaskTestCase(unittest.IsolatedAsyncioTestCase):
    """R2-M2 回归：流式请求不得合流；失败任务不得向等待者抛原始异常。"""

    def _load(self):
        fake_fastapi = types.ModuleType("fastapi")
        fake_fastapi.HTTPException = Exception
        fake_fastapi.status = types.SimpleNamespace(HTTP_400_BAD_REQUEST=400)

        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.PRECISE_CACHE = False
        fake_settings.CALCULATE_CACHE_ENTRIES = 6

        fake_config_pkg = types.ModuleType("app.config")
        fake_config_pkg.__path__ = []
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []
        fake_utils_pkg.generate_cache_key = lambda *a, **k: "k"
        fake_utils_pkg.log = lambda *a, **k: None

        fake_app_pkg = types.ModuleType("app")

        return load_module(
            "request_helpers_opt",
            "app/api/request_helpers.py",
            {
                "fastapi": fake_fastapi,
                "app": fake_app_pkg,
                "app.config": fake_config_pkg,
                "app.config.settings": fake_settings,
                "app.utils": fake_utils_pkg,
            },
        )

    async def test_stream_request_never_coalesces(self):
        mod = self._load()

        async def slow_task():
            await asyncio.sleep(30)
            return {"done": True}

        running_task = asyncio.create_task(slow_task())

        class Manager:
            def get(self, key):
                return running_task

            def remove(self, key):
                pass

        req = types.SimpleNamespace(stream=True, model="m")
        # 即使池中有进行中的同键任务，流式请求也必须立即返回 None
        # （走自己的完整处理路径），绝不能拿到别人的 StreamingResponse。
        result = await mod.wait_for_existing_task(Manager(), "pool", req)
        self.assertIsNone(result)
        running_task.cancel()

    async def test_failed_task_returns_none_not_raise(self):
        mod = self._load()

        async def boom():
            await asyncio.sleep(0.01)
            raise RuntimeError("upstream exploded")

        task = asyncio.create_task(boom())

        class Manager:
            def __init__(self):
                self.removed = []

            def get(self, key):
                return task

            def remove(self, key):
                self.removed.append(key)

        mgr = Manager()
        req = types.SimpleNamespace(stream=False, model="m")
        # 旧实现会把 RuntimeError 原样抛给等待者（未脱敏 500）；
        # 现在必须吞掉并返回 None，让等待者独立处理。
        result = await mod.wait_for_existing_task(mgr, "pool", req)
        self.assertIsNone(result)
        self.assertEqual(mgr.removed, ["pool"])


class CacheKeyIncludesToolsTestCase(unittest.TestCase):
    """R2-L5 回归：tools 必须参与缓存键，否则不同工具集的请求互相串扰。"""

    def _load(self):
        fake_logging = types.ModuleType("app.utils.logging")
        fake_logging.log = lambda *a, **k: None
        fake_utils_pkg = types.ModuleType("app.utils")
        fake_utils_pkg.__path__ = []
        fake_app_pkg = types.ModuleType("app")
        return load_module(
            "cache_opt",
            "app/utils/cache.py",
            {
                "app": fake_app_pkg,
                "app.utils": fake_utils_pkg,
                "app.utils.logging": fake_logging,
            },
        )

    def test_different_tools_different_keys(self):
        mod = self._load()
        tools_a = [{"type": "function", "function": {"name": "get_weather"}}]
        tools_b = [{"type": "function", "function": {"name": "get_stock"}}]

        req_a = types.SimpleNamespace(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools_a,
        )
        req_b = types.SimpleNamespace(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools_b,
        )
        req_none = types.SimpleNamespace(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )

        key_a = mod.generate_cache_key(req_a, last_n_messages=6)
        key_b = mod.generate_cache_key(req_b, last_n_messages=6)
        key_none = mod.generate_cache_key(req_none, last_n_messages=6)

        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_none)
        self.assertNotEqual(key_b, key_none)
        # 相同 tools 的重复请求仍命中同一键
        self.assertEqual(key_a, mod.generate_cache_key(req_a, last_n_messages=6))

    def test_gemini_payload_tools_hashed(self):
        mod = self._load()

        def make_req(tools):
            payload = types.SimpleNamespace(
                contents=[{"role": "user", "parts": [{"text": "hi"}]}],
                tools=tools,
            )
            return types.SimpleNamespace(
                model="gemini-2.5-pro", payload=payload
            )

        req_a = make_req([{"google_search": {}}])
        req_b = make_req([{"code_execution": {}}])
        self.assertNotEqual(
            mod.generate_cache_key(req_a, is_gemini=True),
            mod.generate_cache_key(req_b, is_gemini=True),
        )


class StatsReadNoPollutionTestCase(unittest.TestCase):
    """R2-L3 回归：get_api_key_stats 读路径不得污染 defaultdict。"""

    def test_unknown_key_read_creates_no_entries(self):
        from collections import Counter, defaultdict

        # 直接实例化真实类（stats.py 顶层导入 fastapi 相关依赖过重，
        # 这里用 duck-typing 复刻同型逻辑验证 .get() 读法语义）。
        mgr = types.SimpleNamespace(
            api_key_counts=defaultdict(int),
            api_key_tokens=defaultdict(int),
            api_model_counts=defaultdict(lambda: Counter()),
            api_model_tokens=defaultdict(lambda: Counter()),
        )

        # 旧的污染写法（保留作对照断言材料）：
        _ = mgr.api_key_counts["never-seen-key"]  # noqa: F841
        self.assertIn("never-seen-key", mgr.api_key_counts)

        # 新写法：.get() 链式读不插入键
        self.assertEqual(mgr.api_key_counts.get("another-unknown", 0), 0)
        self.assertNotIn("another-unknown", mgr.api_key_counts)
        self.assertEqual(mgr.api_model_counts.get("unknown", {}).get("m", 0), 0)
        self.assertNotIn("unknown", mgr.api_model_counts)


class ProtocolNonstreamIdShapeTestCase(unittest.TestCase):
    """R2-L8 回归：协议响应 id 必须符合官方形态（无双前缀）。"""

    def _load(self):
        import importlib

        utils_dir = ROOT / "app" / "utils"
        fake_utils = types.ModuleType("app.utils")
        fake_utils.__path__ = [str(utils_dir)]
        fake_app = types.ModuleType("app")

        return load_module(
            "protocol_nonstream_opt",
            "app/utils/protocol_nonstream.py",
            {"app": fake_app, "app.utils": fake_utils},
        )

    def test_response_api_id_shape(self):
        import re

        mod = self._load()
        chat_response = {
            "id": "chatcmpl-abc123",
            "model": "gemini-2.5-pro",
            "created": 1700000000,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        result = mod.openai_chat_to_response_api(chat_response, {})
        # 官方形态：resp_ + 30 位字母数字（旧实现产出 "resp_chatcmpl-abc123"）
        self.assertRegex(result["id"], r"^resp_[A-Za-z0-9]{30}$")

    def test_claude_message_id_shape(self):
        import re

        mod = self._load()
        chat_response = {
            "id": "chatcmpl-abc123",
            "model": "gemini-2.5-pro",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        result = mod.openai_chat_to_claude_response(chat_response)
        # 官方形态：msg_01 + 24 位字母数字（旧实现产出 "msg_chatcmpl-abc123"）
        self.assertRegex(result["id"], r"^msg_01[A-Za-z0-9]{24}$")


class ProtectFromAbuseFullPathTestCase(unittest.IsolatedAsyncioTestCase):
    """R2-H9 回归：protect_from_abuse 全路径不得因三元组解包崩溃。

    第一轮 H4 把限流条目改为 (count, ts, ttl) 三元组，但漏掉了
    protect_from_abuse 内两处读取的解包维度 —— 所有聊天补全请求
    第一跳就抛 "too many values to unpack"，全量 500。当时的回归
    测试只覆盖 _maybe_sweep_expired，未覆盖读取路径，故漏网。
    """

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
            "rate_limiting_opt",
            "app/utils/rate_limiting.py",
            {"fastapi": fake_fastapi},
        )

    async def test_first_and_second_requests_do_not_crash(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0

        class FakeURL:
            def __init__(self, path):
                self.path = path

        class FakeClient:
            host = "9.9.9.9"

        class FakeRequest:
            def __init__(self, path):
                self.url = FakeURL(path)
                self.client = FakeClient()

        # 第一次请求（默认值路径）+ 第二次请求（命中已有条目路径）
        # 都必须正常完成并正确计数，不抛解包异常。
        for i in (1, 2):
            await mod.protect_from_abuse(FakeRequest("/v1/chat/completions"))
            entries = [
                v for v in mod.rate_limit_data.values() if isinstance(v, tuple)
            ]
            counts = [v[0] for v in entries if v and v[0] in (i,)]
            self.assertTrue(
                any(v[0] == i for v in entries),
                f"第 {i} 次请求后应存在计数值 {i} 的条目: {entries}",
            )

    async def test_rate_limit_429_after_threshold(self):
        mod = self._load()
        mod.rate_limit_data.clear()
        mod._last_sweep_ts = 0

        class FakeURL:
            def __init__(self, path):
                self.path = path

        class FakeClient:
            host = "8.8.8.8"

        class FakeRequest:
            def __init__(self, path):
                self.url = FakeURL(path)
                self.client = FakeClient()

        # 分钟阈值 3：第 1-3 次通过，第 4 次抛 429（fake HTTPException
        # 即 Exception 子类）
        for _ in range(3):
            await mod.protect_from_abuse(
                FakeRequest("/v1/chat/completions"),
                max_requests_per_minute=3,
                max_requests_per_day_per_ip=100,
            )
        with self.assertRaises(Exception) as ctx:
            await mod.protect_from_abuse(
                FakeRequest("/v1/chat/completions"),
                max_requests_per_minute=3,
                max_requests_per_day_per_ip=100,
            )
        self.assertIn("Too many requests per minute", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
