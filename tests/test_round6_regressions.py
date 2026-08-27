"""Round 6 回归测试。

覆盖本轮五组修复：
1. 防风控：网络类错误（超时/连接失败）触发短冷却（旧逻辑只看 HTTP
   状态码，黑洞 key 在 fill 模式下烧光全部重试）
2. 防风控：fill 模式下粘滞 key 达 RPM 阈值时让出粘滞位（旧逻辑整批
   空手、健康 key 完全没被尝试）
3. 防风控：429 响应体的 retryDelay 被解析并用于冷却时长
4. 性能：缓存键纳入生成参数（temperature/max_tokens 不同不再串答）；
   缓存容量驱逐改为堆式精确 LRU（O(logN) 且日志合并单行）
5. 性能：RPM 窗口在请求发射时计数（record_outbound_attempt），
   在途/失败请求不再漏记
6. 防风控：浏览器 UA 携带 sec-ch-ua/sec-fetch 系列头，SDK UA 不携带
"""
import asyncio
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import httpx


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


def _fake_env():
    """error_handling.py / stats.py / api_key_selection.py 共用的 fake 环境。"""
    fake_logging = types.ModuleType("app.utils.logging")
    fake_logging.log = lambda *a, **k: None
    fake_logging.format_log_message = lambda level, msg: msg
    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.api_call_stats = {"calls": []}
    fake_settings.API_KEY_DAILY_LIMIT = 100
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_utils_pkg = types.ModuleType("app.utils")
    fake_utils_pkg.__path__ = []
    # error_handling.py 顶层导入 app.utils.stealth.full_jitter_backoff
    fake_stealth = types.ModuleType("app.utils.stealth")
    import random as _random

    def _full_jitter(attempt, base=1.0, cap=60.0):
        return _random.uniform(0.0, min(base * (2 ** max(0, attempt)), cap))

    fake_stealth.full_jitter_backoff = _full_jitter
    # api_key.py 顶层导入 build_key_probe_headers；stealth 测试另外
    # 单独加载真实模块（BrowserFingerprintHeadersTestCase）。
    fake_stealth.build_key_probe_headers = lambda *a, **k: {}
    fake_stealth.pick_user_agent = lambda *a, **k: "curl/8.9.1"
    # api_key.py 顶层导入 app.utils.http_client.get_async_client
    fake_http_client = types.ModuleType("app.utils.http_client")

    async def _no_client():
        return None

    fake_http_client.get_async_client = _no_client
    fake_http_client.UPSTREAM_TIMEOUT = None
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.__path__ = []
    return {
        "app.utils.logging": fake_logging,
        "app.config.settings": fake_settings,
        "app.config": fake_config_pkg,
        "app.utils": fake_utils_pkg,
        "app.utils.stealth": fake_stealth,
        "app.utils.http_client": fake_http_client,
        "app": fake_app_pkg,
    }


# ---------------------------------------------------------------------------
# 1. 网络错误冷却 + retryDelay 解析（error_handling.py）
# ---------------------------------------------------------------------------


class NetworkErrorCooldownTestCase(unittest.TestCase):
    def _load(self):
        return load_module(
            "error_handling_r6", "app/utils/error_handling.py", _fake_env()
        )

    def test_timeout_error_is_network_error(self):
        mod = self._load()
        err = httpx.ReadTimeout("read timed out")
        self.assertIsNone(mod._extract_status_code(err))
        self.assertTrue(mod._is_network_error(err))

    def test_connect_error_is_network_error(self):
        mod = self._load()
        err = httpx.ConnectError("connection refused")
        self.assertTrue(mod._is_network_error(err))

    def test_http_status_error_is_not_network_error(self):
        mod = self._load()
        req = httpx.Request("POST", "https://example.test")
        resp = httpx.Response(429, request=req)
        err = httpx.HTTPStatusError("429", request=req, response=resp)
        self.assertFalse(mod._is_network_error(err))
        self.assertEqual(mod._extract_status_code(err), 429)

    def test_schedule_key_cooldown_handles_network_error(self):
        """Round 7 语义更新：网络错误**不**触发冷却（key 亲和重试）。

        Round 6 曾要求网络错误调度 mark_key_failure(status=0, 30s)；
        Round 7 收紧策略为"只有配额耗尽（429）与密钥失效（401/403）
        才冷却换 key"——网络类故障与 key 无关，重试循环改用原 key
        退避重试（preferred_keys 亲和机制，见 test_round7_key_affinity）。
        """
        mod = self._load()
        calls = []

        async def fake_mark(api_key, status_code, cooldown_seconds=None):
            calls.append((api_key, status_code, cooldown_seconds))

        fake_api_key_mod = types.ModuleType("app.utils.api_key")
        fake_api_key_mod.mark_key_failure = fake_mark
        saved = sys.modules.get("app.utils.api_key")
        sys.modules["app.utils.api_key"] = fake_api_key_mod
        try:
            async def run():
                mod.schedule_key_cooldown(
                    httpx.ReadTimeout("read timeout"), "AIzaSytest"
                )
                # fire-and-forget 任务需要一次事件循环调度机会
                await asyncio.sleep(0.01)

            asyncio.run(run())
        finally:
            if saved is None:
                sys.modules.pop("app.utils.api_key", None)
            else:
                sys.modules["app.utils.api_key"] = saved

        self.assertEqual(calls, [])

    def test_extract_retry_delay_parses_seconds(self):
        mod = self._load()

        class FakeResp:
            def json(self):
                return {
                    "error": {
                        "code": 429,
                        "details": [
                            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                             "retryDelay": "26s"}
                        ],
                    }
                }

        self.assertEqual(mod.extract_retry_delay(FakeResp()), 26.0)

    def test_extract_retry_delay_caps_outliers(self):
        mod = self._load()

        class FakeResp:
            def json(self):
                return {"error": {"details": [{"retryDelay": "99999s"}]}}

        self.assertEqual(mod.extract_retry_delay(FakeResp()), 3600.0)

    def test_extract_retry_delay_no_body(self):
        mod = self._load()

        class FakeResp:
            def json(self):
                raise ValueError("no body")

        self.assertIsNone(mod.extract_retry_delay(FakeResp()))
        self.assertIsNone(mod.extract_retry_delay(None))

    def test_summarize_upstream_error_includes_quota_metric(self):
        mod = self._load()

        class FakeResp:
            def json(self):
                return {
                    "error": {
                        "message": "Resource has been exhausted",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {"@type": "...QuotaFailure",
                             "quotaMetric": "GenerateRequestsPerDayPerProject"},
                        ],
                    }
                }

        summary = mod.summarize_upstream_error(FakeResp())
        self.assertIn("Resource has been exhausted", summary)
        self.assertIn("quota=GenerateRequestsPerDayPerProject", summary)
        self.assertIn("status=RESOURCE_EXHAUSTED", summary)


# ---------------------------------------------------------------------------
# 2. mark_key_failure 自定义冷却时长（api_key.py）
# ---------------------------------------------------------------------------


class MarkKeyFailureCooldownSecondsTestCase(unittest.TestCase):
    def _load(self):
        return load_module(
            "api_key_r6", "app/utils/api_key.py", _fake_env()
        )

    def test_429_uses_retry_delay_when_given(self):
        mod = self._load()

        async def run():
            return await mod.mark_key_failure("k", 429, cooldown_seconds=26.0)

        cooldown = asyncio.run(run())
        self.assertEqual(cooldown, 26.0)
        self.assertIn("k", mod._key_cooldowns)
        # 清理，避免跨测试污染
        mod._key_cooldowns.clear()

    def test_429_defaults_to_60(self):
        mod = self._load()

        async def run():
            return await mod.mark_key_failure("k", 429)

        self.assertEqual(asyncio.run(run()), 60.0)
        mod._key_cooldowns.clear()

    def test_network_error_status_zero_short_cooldown(self):
        mod = self._load()

        async def run():
            return await mod.mark_key_failure("k", 0)

        self.assertEqual(asyncio.run(run()), 30.0)
        mod._key_cooldowns.clear()

    def test_unknown_status_without_override_returns_zero(self):
        mod = self._load()

        async def run():
            return await mod.mark_key_failure("k", 418)

        self.assertEqual(asyncio.run(run()), 0.0)


# ---------------------------------------------------------------------------
# 3. fill 模式 RPM 让位（api_key_selection.py + APIKeyManager.advance_sticky_key）
# ---------------------------------------------------------------------------


class _FakeKeyManager:
    """模拟 fill 模式的 key_manager：栈顶粘滞 + advance_sticky_key。"""

    def __init__(self, keys):
        self.api_keys = list(keys)
        self.key_stack = list(reversed(keys))  # 栈顶 = keys[0]
        self.advanced = 0

    async def get_available_key(self):
        # fill 语义：返回栈顶（不 pop）
        if not self.key_stack:
            # 栈空重置（真实实现行为）
            self.key_stack = list(reversed(self.api_keys))
        return self.key_stack[-1] if self.key_stack else None

    async def advance_sticky_key(self):
        self.advanced += 1
        if self.key_stack:
            self.key_stack.pop()


class FillModeRpmSkipTestCase(unittest.TestCase):
    def _load(self, rpm_counts):
        env = _fake_env()
        fake_stats = types.ModuleType("app.utils.stats")
        fake_stats.get_calls_last_minute_for_key = lambda k: rpm_counts.get(k, 0)
        fake_stats.MAX_OUTBOUND_RPM = 15
        fake_stats.OUTBOUND_RPM_BACKOFF_FRACTION = 0.8

        async def usage(_stats, key, model=None):
            return 0

        fake_stats.get_api_key_usage = usage
        env["app.utils.stats"] = fake_stats

        fake_api_key = types.ModuleType("app.utils.api_key")

        async def not_cooled(key):
            return False

        fake_api_key.is_key_cooled_down = not_cooled
        env["app.utils.api_key"] = fake_api_key
        return load_module(
            "api_key_selection_r6", "app/utils/api_key_selection.py", env
        )

    def test_rpm_hot_sticky_key_yields_to_next_key(self):
        """粘滞 key RPM 满时，选择循环必须能拿到下一个健康 key。

        旧逻辑：continue 后 get_available_key 仍返回同一 key →
        checked_keys 去重 break → 返回空列表（健康 key 被无视）。
        """
        mod = self._load({"hot": 20, "healthy": 0})  # threshold = 12
        km = _FakeKeyManager(["hot", "healthy"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="test", model="m"
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["healthy"])
        self.assertEqual(km.advanced, 1)

    def test_all_keys_rpm_hot_returns_empty(self):
        mod = self._load({"a": 20, "b": 20})
        km = _FakeKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="test", model="m"
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, [])

    def test_healthy_sticky_key_no_advance(self):
        mod = self._load({"ok": 3})
        km = _FakeKeyManager(["ok"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="test", model="m"
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["ok"])
        self.assertEqual(km.advanced, 0)


class AdvanceStickyKeyTestCase(unittest.TestCase):
    def _load(self, strategy="fill"):
        env = _fake_env()
        fake_settings = env["app.config.settings"]
        fake_settings.KEY_ROTATION_STRATEGY = strategy
        return load_module("api_key_adv_r6", "app/utils/api_key.py", env)

    def test_advance_pops_top_key(self):
        mod = self._load()
        mgr = mod.APIKeyManager.__new__(mod.APIKeyManager)
        mgr.api_keys = ["a", "b", "c"]
        mgr.key_stack = ["c", "b", "a"]  # 栈顶 a
        mgr.lock = asyncio.Lock()

        async def run():
            await mgr.advance_sticky_key()

        asyncio.run(run())
        self.assertEqual(mgr.key_stack, ["c", "b"])

    def test_advance_noop_in_polling(self):
        mod = self._load(strategy="polling")
        mgr = mod.APIKeyManager.__new__(mod.APIKeyManager)
        mgr.api_keys = ["a", "b"]
        mgr.key_stack = ["b", "a"]
        mgr.lock = asyncio.Lock()

        async def run():
            await mgr.advance_sticky_key()

        asyncio.run(run())
        self.assertEqual(mgr.key_stack, ["b", "a"])


# ---------------------------------------------------------------------------
# 4. RPM 发射计数（stats.record_outbound_attempt）
# ---------------------------------------------------------------------------


class RecordOutboundAttemptTestCase(unittest.TestCase):
    def _load(self):
        return load_module(
            "stats_r6", "app/utils/stats.py", _fake_env()
        )

    def test_attempt_counted_immediately(self):
        mod = self._load()
        mgr = mod.ApiStatsManager(enable_background=False)
        mgr.record_outbound_attempt("k1", "m")
        self.assertEqual(mgr.get_calls_last_minute_for_key("k1"), 1)
        mgr.record_outbound_attempt("k1", "m")
        self.assertEqual(mgr.get_calls_last_minute_for_key("k1"), 2)

    def test_attempt_does_not_touch_counters(self):
        """发射计数只写 RPM 窗口，不虚增调用次数/token 统计。"""
        mod = self._load()
        mgr = mod.ApiStatsManager(enable_background=False)
        mgr.record_outbound_attempt("k1", "m")
        self.assertEqual(mgr.get_calls_last_minute_for_key("k1"), 1)
        # 计数器未被写（完成路径的 update_stats 才写）
        self.assertEqual(mgr.get_calls_last_24h(), 0)

    def test_module_level_helper(self):
        mod = self._load()
        mod.record_outbound_attempt("k2", "m")
        self.assertEqual(mod.get_calls_last_minute_for_key("k2"), 1)


# ---------------------------------------------------------------------------
# 5. 缓存：生成参数进键 + 堆式 LRU 驱逐
# ---------------------------------------------------------------------------


class _FakeChatRequest:
    """最小 OpenAI 格式请求对象。"""

    def __init__(self, model="gemini-2.0-flash", messages=None, **params):
        self.model = model
        self.messages = messages or [
            {"role": "user", "content": "hello"}
        ]
        self.tools = None
        for k, v in params.items():
            setattr(self, k, v)


class CacheGenerationParamsTestCase(unittest.TestCase):
    def _load(self):
        return load_module("cache_r6", "app/utils/cache.py", _fake_env())

    def test_different_temperature_different_keys(self):
        mod = self._load()
        r1 = _FakeChatRequest(temperature=0.7)
        r2 = _FakeChatRequest(temperature=1.5)
        k1 = mod.generate_cache_key(r1)
        k2 = mod.generate_cache_key(r2)
        self.assertNotEqual(k1, k2)

    def test_different_max_tokens_different_keys(self):
        mod = self._load()
        r1 = _FakeChatRequest(max_tokens=100)
        r2 = _FakeChatRequest(max_tokens=4096)
        self.assertNotEqual(
            mod.generate_cache_key(r1), mod.generate_cache_key(r2)
        )

    def test_default_params_key_stable(self):
        mod = self._load()
        r1 = _FakeChatRequest()
        r2 = _FakeChatRequest(temperature=None, max_tokens=None)
        self.assertEqual(mod.generate_cache_key(r1), mod.generate_cache_key(r2))

    def test_stop_sequences_change_key(self):
        mod = self._load()
        r1 = _FakeChatRequest()
        r2 = _FakeChatRequest(stop=["\n\n"])
        self.assertNotEqual(
            mod.generate_cache_key(r1), mod.generate_cache_key(r2)
        )


class CacheHeapEvictionTestCase(unittest.TestCase):
    def _load(self):
        return load_module("cache_evict_r6", "app/utils/cache.py", _fake_env())

    def _mgr(self, mod, max_entries=20):
        # 注：clean_if_needed 的 target_size 有 floor=10 保护（避免小缓存
        # 抖动），max_entries 需 > 10 才能在超限时立即驱逐到 max-10。
        return mod.ResponseCacheManager(
            expiry_time=600, max_entries=max_entries, cache_dict={}
        )

    def test_eviction_removes_oldest_first(self):
        mod = self._load()
        mgr = self._mgr(mod, max_entries=12)

        async def run():
            for i in range(15):
                await mgr.store(f"k{i}", f"v{i}")
                await asyncio.sleep(0.001)

        asyncio.run(run())
        # 15 > 12 → 驱逐到 target=12-10=2 … 实际驱逐 15-2=13 条？
        # 不：store 超限时触发 clean_if_needed，驱逐 cur-target 条。
        # cur=13 时驱逐到 2（一次性），后续 store 继续累积。
        self.assertLessEqual(mgr.cur_cache_num, 12)
        # 最旧的 k0-k2 必已被驱逐（新存的最后几条仍在）
        for key in ("k0", "k1", "k2"):
            _, hit = asyncio.run(mgr.get_and_remove(key))
            self.assertFalse(hit, f"{key} should have been evicted")
        _, hit = asyncio.run(mgr.get_and_remove("k14"))
        self.assertTrue(hit, "newest entry should still be cached")

    def test_stale_heap_nodes_are_skipped(self):
        """get_and_remove 移除的条目在堆里留下惰性节点，驱逐时跳过。"""
        mod = self._load()
        mgr = self._mgr(mod, max_entries=12)

        async def run():
            for i in range(5):
                await mgr.store(f"k{i}", f"v{i}")
                await asyncio.sleep(0.001)
            # 消费掉最旧的 k0（堆里留下惰性节点）
            got, hit = await mgr.get_and_remove("k0")
            self.assertTrue(hit)
            # 存到超限，驱逐时堆顶是 k0 的惰性节点 → 必须跳过它驱逐 k1
            for i in range(5, 15):
                await mgr.store(f"k{i}", f"v{i}")
                await asyncio.sleep(0.001)

        asyncio.run(run())
        _, hit = asyncio.run(mgr.get_and_remove("k1"))
        self.assertFalse(hit, "k1 should have been evicted (k0 already consumed)")
        _, hit = asyncio.run(mgr.get_and_remove("k14"))
        self.assertTrue(hit)

    def test_eviction_count_never_negative(self):
        mod = self._load()
        mgr = self._mgr(mod, max_entries=14)

        async def run():
            for i in range(30):
                await mgr.store(f"k{i}", f"v{i}")

        asyncio.run(run())
        self.assertLessEqual(mgr.cur_cache_num, 14)
        self.assertGreaterEqual(mgr.cur_cache_num, 0)


# ---------------------------------------------------------------------------
# 6. 浏览器 UA 指纹头（stealth.py）
# ---------------------------------------------------------------------------


class BrowserFingerprintHeadersTestCase(unittest.TestCase):
    def _load(self):
        return load_module("stealth_r6", "app/utils/stealth.py", _fake_env())

    def test_browser_ua_gets_sec_headers(self):
        mod = self._load()
        headers = mod.build_gemini_headers("AIzaSytest")
        if headers["User-Agent"].startswith("Mozilla/"):
            self.assertIn("sec-ch-ua", headers)
            self.assertIn("sec-fetch-dest", headers)
            self.assertEqual(headers["sec-fetch-dest"], "empty")
            self.assertEqual(headers["sec-fetch-mode"], "cors")
            self.assertIn("Origin", headers)
            self.assertIn("aistudio.google.com", headers["Origin"])
            # 浏览器 UA 不携带 SDK 头
            self.assertNotIn("x-goog-api-client", headers)
        else:
            # SDK/curl UA：不带浏览器头
            self.assertNotIn("sec-ch-ua", headers)
            self.assertNotIn("sec-fetch-dest", headers)
            self.assertNotIn("Origin", headers)

    def test_sec_ch_ua_version_matches_ua(self):
        mod = self._load()
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
        headers = {}
        mod._apply_browser_fingerprint_headers(headers, ua)
        self.assertIn('v="132"', headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")

    def test_sdk_ua_gets_no_browser_headers(self):
        mod = self._load()
        ua = "google-genai-sdk-python/1.8.2 gl-python/3.12.6"
        headers = {"User-Agent": ua}
        mod._apply_browser_fingerprint_headers(headers, ua)
        self.assertNotIn("sec-ch-ua", headers)
        self.assertNotIn("sec-fetch-dest", headers)
        # SDK UA 反而应携带 x-goog-api-client
        headers = mod._apply_goog_client_header(headers, ua)
        self.assertEqual(headers["x-goog-api-client"], ua)


# ---------------------------------------------------------------------------
# 7. 分层超时 + HTTP/2（http_client.py）
# ---------------------------------------------------------------------------


class LayeredTimeoutTestCase(unittest.TestCase):
    def test_upstream_timeout_is_layered(self):
        from app.utils.http_client import (
            UPSTREAM_TIMEOUT,
            UPSTREAM_CONNECT_TIMEOUT,
            UPSTREAM_READ_TIMEOUT,
        )
        self.assertEqual(UPSTREAM_TIMEOUT.connect, UPSTREAM_CONNECT_TIMEOUT)
        self.assertEqual(UPSTREAM_TIMEOUT.read, UPSTREAM_READ_TIMEOUT)
        # connect 必须远小于旧的 600s 标量（黑洞连接快速失败）
        self.assertLessEqual(UPSTREAM_CONNECT_TIMEOUT, 15)
        # read 必须覆盖 thinking 模型的合法长响应
        self.assertGreaterEqual(UPSTREAM_READ_TIMEOUT, 120)

    def test_http2_enabled_when_h2_available(self):
        try:
            import h2  # noqa: F401

            from app.utils.http_client import _HTTP2_ENABLED

            self.assertTrue(_HTTP2_ENABLED)
        except ImportError:
            pass  # h2 未安装时优雅降级为 http/1.1


if __name__ == "__main__":
    unittest.main()
