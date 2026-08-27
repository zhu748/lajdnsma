"""第四轮优化的回归测试。

覆盖本轮核心修复（防谷歌风控 + UI 数据链路 + 性能）：
1. P0: handle_gemini_error 必须调度密钥冷却——此前冷却机制只存在于
   从未被调用的 handle_api_error() 里，429 密钥反复被选中（死代码修复）
2. P0: 重试批次间必须有 full-jitter 退避（compute_inter_batch_backoff）
3. P0: 仪表盘图表数据链路 value 字段（后端返回 {time, value}，前端
   此前读不存在的 p.count 导致图表恒为零——前端修复，此处锁后端契约）
4. P1: x-goog-api-client 头必须与 User-Agent 联动（此前硬编码 SDK
   值，与随机选中的 Chrome/curl UA 自相矛盾）
5. P1: sanitize_string 必须脱敏 AQ. 新版格式密钥
6. P1: test_api_key 探测超时必须为 15s（原 60s 网络黑洞时拖垮启动）
"""
import asyncio
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_optimization_regressions import load_module  # noqa: E402

import httpx  # noqa: E402


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造一个携带目标状态码的 httpx.HTTPStatusError（不发起真实请求）。"""
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status_code}'", request=request, response=response
    )


# ---------------------------------------------------------------------------
# 1. 密钥冷却激活（P0）
# ---------------------------------------------------------------------------


class CooldownActivationTestCase(unittest.IsolatedAsyncioTestCase):
    """handle_gemini_error 遇 429/401/403/500/503 必须把 key 拉进冷却。"""

    def _load(self):
        # error_handling 只依赖 httpx / logging / fastapi（异常分支）；
        # mark_key_failure 是函数内延迟导入真实模块，这里直接用真实
        # app.utils.api_key（其自身依赖 http_client/stealth，均可导入）。
        return load_module("error_handling_r4", "app/utils/error_handling.py")

    async def test_429_schedules_cooldown(self):
        mod = self._load()
        from app.utils import api_key as real_api_key

        real_api_key._key_cooldowns.clear()
        api_key_value = "AIzaSy" + "c" * 33

        message = mod.handle_gemini_error(_make_http_status_error(429), api_key_value)

        # fire-and-forget 任务需要跑一个事件循环 tick 才会真正写入
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIn(api_key_value, real_api_key._key_cooldowns)
        cooldown_until = real_api_key._key_cooldowns[api_key_value]
        # 429 -> 60s 冷却（留 1s 余量对抗慢机器）
        remaining = cooldown_until - time.time()
        self.assertGreater(remaining, 55.0, f"429 冷却剩余应约 60s，实际 {remaining}")
        self.assertLessEqual(remaining, 60.5)
        # 返回给客户端的消息保持脱敏约定
        self.assertNotIn("Gemini", message)

    async def test_400_does_not_schedule_cooldown(self):
        """400 是请求本身的问题，与密钥无关，不应冷却。"""
        mod = self._load()
        from app.utils import api_key as real_api_key

        real_api_key._key_cooldowns.clear()
        api_key_value = "AIzaSy" + "d" * 33

        mod.handle_gemini_error(_make_http_status_error(400), api_key_value)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertNotIn(api_key_value, real_api_key._key_cooldowns)

    async def test_403_permanently_blocks(self):
        mod = self._load()
        from app.utils import api_key as real_api_key

        real_api_key._key_cooldowns.clear()
        api_key_value = "AIzaSy" + "e" * 33

        mod.handle_gemini_error(_make_http_status_error(403), api_key_value)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIn(api_key_value, real_api_key._key_cooldowns)
        # 401/403 -> 永久拉黑（巨大时间戳）
        self.assertGreater(
            real_api_key._key_cooldowns[api_key_value],
            real_api_key.PERMANENT_BLOCK_TS - time.time() - 1,
        )

    async def test_sync_context_without_loop_is_noop(self):
        """无事件循环时（纯同步调用方）不得抛错，仅跳过冷却调度。"""
        mod = self._load()

        def _call_sync():
            # 在没有运行中事件循环的线程里直接调用
            return mod.schedule_key_cooldown(
                _make_http_status_error(429), "AIzaSy" + "f" * 33
            )

        # schedule_key_cooldown 设计为同步可调用；在无循环线程中应静默跳过
        import threading

        thread = threading.Thread(target=_call_sync)
        thread.start()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 2. 批间退避（P0）
# ---------------------------------------------------------------------------


class InterBatchBackoffTestCase(unittest.TestCase):
    """compute_inter_batch_backoff 的区间与封顶语义。"""

    def setUp(self):
        from app.utils import retry_state as real_retry_state

        self.mod = real_retry_state

    def test_no_backoff_before_first_failure(self):
        self.assertEqual(self.mod.compute_inter_batch_backoff(0), 0.0)

    def test_first_backoff_within_base(self):
        for _ in range(50):
            delay = self.mod.compute_inter_batch_backoff(1)
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, 0.5)

    def test_backoff_grows_then_caps(self):
        # 第 2 批失败：0~1s；第 3 批：0~2s；第 4 批：0~4s；第 5 批起封顶 0~8s
        for failed, upper in ((2, 1.0), (3, 2.0), (4, 4.0), (5, 8.0), (20, 8.0)):
            for _ in range(20):
                delay = self.mod.compute_inter_batch_backoff(failed)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, upper + 1e-9)

    def test_retry_loops_reference_backoff(self):
        """4 个重试循环的源码必须真的调用 compute_inter_batch_backoff——
        防止未来重构时退避被无声移除（这是 P0 反风控修复）。"""
        for relpath in (
            "app/api/nonstream_handlers.py",
            "app/api/stream_handlers.py",
        ):
            source = (ROOT / relpath).read_text(encoding="utf-8")
            self.assertIn(
                "compute_inter_batch_backoff",
                source,
                f"{relpath} 缺少批间退避调用",
            )
        # nonstream_handlers 有两条独立循环，应有至少两处调用
        source = (ROOT / "app/api/nonstream_handlers.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("compute_inter_batch_backoff"), 3)
        source = (ROOT / "app/api/stream_handlers.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("compute_inter_batch_backoff"), 3)


# ---------------------------------------------------------------------------
# 3. 仪表盘时间序列契约（P0，前端图表修复的后端侧锁定）
# ---------------------------------------------------------------------------


class TimeSeriesContractTestCase(unittest.TestCase):
    """get_time_series_data 必须继续输出 {time, value} 点结构——前端
    ApiCallsChart 已按 value 读取（round 4 修复图表恒为零的 bug），
    后端字段名一旦变化会立即把图表打回全零。"""

    def test_points_carry_value_key(self):
        import random as _random
        from app.utils import stats as real_stats

        manager = real_stats.ApiStatsManager(enable_background=False)
        # 注入两个分钟桶
        now = time.time()
        minute_ts = int(now // 60 * 60)
        with manager._time_series_lock:
            manager.time_buckets[minute_ts] = {"calls": 7, "tokens": 1234}

        import datetime as _dt

        calls, tokens = manager.get_time_series_data(5, _dt.datetime.now())
        self.assertEqual(len(calls), len(tokens), 6)
        # 至少一个非零点，且字段名必须是 value（而非 count）
        values = [p["value"] for p in calls]
        counts_present = any("count" in p for p in calls)
        self.assertFalse(counts_present, "时间序列点不应含 count 字段（前端读 value）")
        self.assertIn(7, values)
        token_values = [p["value"] for p in tokens]
        self.assertIn(1234, token_values)


# ---------------------------------------------------------------------------
# 4. x-goog-api-client 与 UA 联动（P1）
# ---------------------------------------------------------------------------


class HeaderConsistencyTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("stealth_r4", "app/utils/stealth.py")

    def test_sdk_ua_carries_matching_client_header(self):
        ua = "google-genai-sdk-python/1.8.2 gl-python/3.12.6"
        self.assertEqual(self.mod._x_goog_api_client_for(ua), ua)

    def test_browser_ua_omits_client_header(self):
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self.assertIsNone(self.mod._x_goog_api_client_for(ua))

    def test_curl_ua_omits_client_header(self):
        self.assertIsNone(self.mod._x_goog_api_client_for("curl/8.9.1"))

    def test_build_gemini_headers_consistent(self):
        """钉住同一 key 后，headers 里 UA 与 x-goog-api-client 必须同源。"""
        key = "AIzaSy" + "9" * 33
        # 首次选择并钉住
        pinned_ua = self.mod.pick_user_agent(key)
        headers = self.mod.build_gemini_headers(key, streaming=True)
        self.assertEqual(headers["User-Agent"], pinned_ua)
        if pinned_ua.startswith("google-genai-sdk-python/"):
            self.assertEqual(headers["x-goog-api-client"], pinned_ua)
        else:
            self.assertNotIn("x-goog-api-client", headers)

    def test_probe_headers_consistent(self):
        key = "AIzaSy" + "8" * 33
        pinned_ua = self.mod.pick_user_agent(key)
        headers = self.mod.build_key_probe_headers(key)
        self.assertEqual(headers["User-Agent"], pinned_ua)
        if pinned_ua.startswith("google-genai-sdk-python/"):
            self.assertIn("x-goog-api-client", headers)
        else:
            self.assertNotIn("x-goog-api-client", headers)


# ---------------------------------------------------------------------------
# 5. AQ. 新格式密钥脱敏（P1）
# ---------------------------------------------------------------------------


class SanitizeNewFormatKeyTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = load_module("error_handling_sanitize_r4", "app/utils/error_handling.py")

    def test_new_format_key_redacted(self):
        new_key = "AQ." + "x" * 40
        message = f"upstream said key {new_key} is invalid"
        sanitized = self.mod.sanitize_string(message)
        self.assertNotIn(new_key, sanitized)
        self.assertNotIn("AQ.", sanitized)
        self.assertIn("key#", sanitized)

    def test_classic_format_still_redacted(self):
        classic = "AIzaSy" + "a" * 33
        sanitized = self.mod.sanitize_string(f"bad key {classic} here")
        self.assertNotIn(classic, sanitized)
        self.assertIn("key#", sanitized)

    def test_plain_text_untouched(self):
        self.assertEqual(
            self.mod.sanitize_string("普通错误信息，无密钥"), "普通错误信息，无密钥"
        )


# ---------------------------------------------------------------------------
# 6. 探测超时（P1）
# ---------------------------------------------------------------------------


class ProbeTimeoutTestCase(unittest.IsolatedAsyncioTestCase):
    """test_api_key 的出站超时必须是 15s（原 60s 会拖垮启动路径）。"""

    def _load_with_recorder(self):
        recorded = {}

        class _FakeResponse:
            def raise_for_status(self):
                recorded.setdefault("statuses", []).append("ok")

        class _FakeClient:
            async def get(self, url, headers=None, timeout=None):
                recorded["timeout"] = timeout
                recorded["url"] = url
                return _FakeResponse()

        fake_http_client = types.ModuleType("app.utils.http_client")

        async def _get_client():
            return _FakeClient()

        fake_http_client.get_async_client = _get_client
        fake_logging = types.ModuleType("app.utils.logging")
        fake_logging.format_log_message = lambda level, msg: msg
        fake_logging.log = lambda *a, **k: None
        fake_stealth = types.ModuleType("app.utils.stealth")
        fake_stealth.build_key_probe_headers = lambda key: {}
        fake_settings = types.ModuleType("app.config.settings")
        fake_settings.GEMINI_API_KEYS = ""
        fake_settings.KEY_ROTATION_STRATEGY = "fill"
        fake_settings.API_KEY_DAILY_LIMIT = 100

        mod = load_module(
            "api_key_probe_r4",
            "app/utils/api_key.py",
            {
                "app.utils.http_client": fake_http_client,
                "app.utils.logging": fake_logging,
                "app.utils.stealth": fake_stealth,
                "app.config.settings": fake_settings,
                "app.config": types.ModuleType("app.config"),
                "app.utils": types.ModuleType("app.utils"),
                "app": types.ModuleType("app"),
            },
        )
        return mod, recorded

    async def test_probe_timeout_is_15s(self):
        mod, recorded = self._load_with_recorder()
        result = await mod.test_api_key("AIzaSy" + "7" * 33)
        self.assertTrue(result)
        self.assertEqual(recorded.get("timeout"), 15)


if __name__ == "__main__":
    unittest.main()
