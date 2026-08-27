"""Round 5 回归测试。

覆盖四项修复：
1. stats: per-key RPM 滑动窗口在全局高负载下仍精确（旧实现靠全局
   recent_calls deque(maxlen=100)，总调用量 > 100/min 时 per-key 计数
   恒为 0，RPM 退避失效）
2. stats: update_stats 不再逐笔写 "API调用已记录" info 日志（旧实现
   几秒内刷满 LogManager 有界缓冲，淹没真正的 warning/error）
3. logging: LogManager 缓冲扩容到 200 条
4. dashboard: run_api_key_test 完成后必须清理全部冷却状态（旧实现
   让重新验证有效的 key 一直被陈旧的永久拉黑条目跳过）
"""
import asyncio
import sys
import time
import types
import unittest
from collections import deque
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


def _fake_env(log_calls=None):
    """stats.py / api_key.py 共用的最小 fake 环境。"""
    fake_logging = types.ModuleType("app.utils.logging")
    if log_calls is None:
        fake_logging.log = lambda *a, **k: None
        fake_logging.format_log_message = lambda level, msg: msg
    else:
        fake_logging.log = lambda *a, **k: log_calls.append((a, k))
        fake_logging.format_log_message = lambda level, msg: msg
    fake_settings = types.ModuleType("app.config.settings")
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_utils_pkg = types.ModuleType("app.utils")
    fake_utils_pkg.__path__ = []
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.__path__ = []
    return {
        "app.utils.logging": fake_logging,
        "app.config.settings": fake_settings,
        "app.config": fake_config_pkg,
        "app.utils": fake_utils_pkg,
        "app": fake_app_pkg,
    }


def _load_stats(name="stats_r5", log_calls=None):
    return load_module(name, "app/utils/stats.py", _fake_env(log_calls))


class RpmSlidingWindowTestCase(unittest.TestCase):
    """Round 5 P0：per-key RPM 窗口必须不受全局调用量影响。"""

    def test_rpm_window_survives_global_flood(self):
        """全局 200 次/min（超过旧实现 100 条全局缓冲）时 per-key 计数仍精确。"""
        mod = _load_stats("stats_r5_flood")
        mgr = mod.ApiStatsManager(enable_background=False)

        async def fill():
            # 交替打两个 key，总计 200 次 —— 旧实现只保留最近 100 条全局记录
            for i in range(100):
                await mgr.update_stats("k1", "m", 1)
                await mgr.update_stats("k2", "m", 1)

        asyncio.run(fill())
        self.assertEqual(mgr.get_calls_last_minute_for_key("k1"), 100)
        self.assertEqual(mgr.get_calls_last_minute_for_key("k2"), 100)

    def test_rpm_window_prunes_expired_timestamps(self):
        """读取时裁剪 60s 前的过期时间戳，之后的 len() 即窗口内计数。"""
        mod = _load_stats("stats_r5_prune")
        mgr = mod.ApiStatsManager(enable_background=False)

        now = time.time()
        with mgr._rpm_lock:
            window = mgr._key_rpm_windows["k1"]
            window.append(now - 120)  # 2 分钟前：过期
            window.append(now - 90)   # 过期
            window.append(now - 30)   # 窗口内
            window.append(now - 5)    # 窗口内

        self.assertEqual(mgr.get_calls_last_minute_for_key("k1"), 2)
        # 裁剪是持久的：再次读取窗口只剩 2 条
        with mgr._rpm_lock:
            self.assertEqual(len(mgr._key_rpm_windows["k1"]), 2)

    def test_rpm_window_read_has_no_side_effect(self):
        """读不存在的 key 返回 0 且不创建窗口条目。"""
        mod = _load_stats("stats_r5_read")
        mgr = mod.ApiStatsManager(enable_background=False)

        self.assertEqual(mgr.get_calls_last_minute_for_key("ghost"), 0)
        self.assertNotIn("ghost", mgr._key_rpm_windows)

    def test_reset_clears_rpm_windows(self):
        mod = _load_stats("stats_r5_reset")
        mgr = mod.ApiStatsManager(enable_background=False)

        async def fill():
            await mgr.update_stats("k1", "m", 1)

        asyncio.run(fill())
        self.assertIn("k1", mgr._key_rpm_windows)
        asyncio.run(mgr.reset())
        self.assertNotIn("k1", mgr._key_rpm_windows)


class PerCallLogRemovedTestCase(unittest.TestCase):
    """Round 5 P0：update_stats 不再逐笔写日志。"""

    def test_update_stats_writes_no_log_entry(self):
        log_calls = []
        mod = _load_stats("stats_r5_log", log_calls)

        mgr = mod.ApiStatsManager(enable_background=False)

        async def one():
            await mgr.update_stats("k1", "model-x", 42)

        asyncio.run(one())
        # 旧实现这里会有一条 log("info", "API调用已记录: ...")
        self.assertEqual(
            len(log_calls),
            0,
            "update_stats 不应再产生逐笔日志（会刷满日志缓冲）",
        )

    def test_stats_module_no_recent_calls_attribute(self):
        mod = _load_stats("stats_r5_dead")
        mgr = mod.ApiStatsManager(enable_background=False)
        self.assertFalse(
            hasattr(mgr, "recent_calls"),
            "recent_calls 已被 per-key RPM 窗口取代，不应残留",
        )


class LogManagerCapacityTestCase(unittest.TestCase):
    """Round 5：LogManager 缓冲扩容到 200 条。"""

    def test_default_capacity_is_200(self):
        mod = load_module("logging_r5", "app/utils/logging.py")
        mgr = mod.LogManager()
        self.assertEqual(mgr.logs.maxlen, 200)

        for i in range(250):
            mgr.add_log({"i": i})
        self.assertEqual(len(mgr.get_recent_logs(500)), 200)
        # 最新一条是第 250 次（最旧的 50 条被淘汰）
        self.assertEqual(mgr.get_recent_logs(1)[0]["i"], 249)


class CooldownClearAfterKeyTestTestCase(unittest.IsolatedAsyncioTestCase):
    """Round 5 逻辑 bug：密钥检测完成后必须清空冷却状态。"""

    def _load_api_key(self):
        fake_http_client = types.ModuleType("app.utils.http_client")

        async def _noop():
            return None

        fake_http_client.get_async_client = _noop
        fake_stealth = types.ModuleType("app.utils.stealth")
        fake_stealth.build_key_probe_headers = lambda key: {}
        fakes = _fake_env()
        fakes["app.utils.http_client"] = fake_http_client
        fakes["app.utils.stealth"] = fake_stealth
        fakes["app.config.settings"].GEMINI_API_KEYS = "AIzaSy" + "a" * 33
        fakes["app.config.settings"].KEY_ROTATION_STRATEGY = "fill"
        return load_module("api_key_r5", "app/utils/api_key.py", fakes)

    async def test_clear_all_cooldowns_resets_permanent_block(self):
        mod = self._load_api_key()
        key = "AIzaSy" + "b" * 33

        # 403 → 永久拉黑
        await mod.mark_key_failure(key, 403)
        self.assertTrue(await mod.is_key_cooled_down(key))

        # 检测完成后清理 → 解除
        await mod.clear_all_cooldowns()
        self.assertFalse(await mod.is_key_cooled_down(key))

    def test_run_api_key_test_calls_clear_all_cooldowns(self):
        """源码契约：run_api_key_test 的 finally 块必须调用 clear_all_cooldowns。"""
        source = (ROOT / "app" / "api" / "dashboard.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def run_api_key_test")
        end = source.index("@dashboard_router", start)
        body = source[start:end]
        self.assertIn("finally:", body)
        self.assertIn(
            "clear_all_cooldowns()",
            body,
            "run_api_key_test 完成后必须清理冷却状态，否则重新验证"
            "有效的 key 会被陈旧的永久拉黑条目永久跳过",
        )


class ApiKeySelectionSemanticsDocTestCase(unittest.TestCase):
    """Round 5：fill 模式批次=1 的语义已在 select_valid_api_keys 文档化。"""

    def test_docstring_documents_fill_mode_semantics(self):
        source = (ROOT / "app" / "utils" / "api_key_selection.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("fill", source)
        self.assertIn("语义说明", source)


if __name__ == "__main__":
    unittest.main()
