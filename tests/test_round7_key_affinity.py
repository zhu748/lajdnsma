"""Round 7 回归测试 —— key 亲和重试（只有配额耗尽才换 key）。

用户策略：
  只有出现配额耗尽（429 / 日额度 / RPM 阈值）的时候才换 key；
  其他情况（500/503/网络错误/超时/空响应）一律用原 key 继续重试。

实现拆分（本文件逐层覆盖）：
1. 失败分类器 _classify_failure：429→quota，401/403→dead，
   500/503/网络/未知→transient
2. should_retry_same_key：transient/无记录→True（原 key 重试）；
   quota/dead→False（轮换）
3. handle_gemini_error 入口同步记录失败类别（不依赖 fire-and-forget
   冷却任务的调度时序）
4. schedule_key_cooldown 冷却门控收紧：500/503/网络错误不再冷却；
   429 仍按 retryDelay 冷却；401/403 永久拉黑
5. select_valid_api_keys 的 preferred_keys 亲和阶段：
   - 瞬时失败的 key 被优先重用（polling 轮换模式下也不换）
   - 亲和期间 key 被打成 429 / RPM 阈值 / 日额度满 → 正常轮换
6. 重试循环模式：500 失败后下一批仍是同一个 key；429 失败后
   下一批轮换到新 key
"""
import asyncio
import sys
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
    """error_handling.py / api_key_selection.py 共用的 fake 环境。"""
    fake_logging = types.ModuleType("app.utils.logging")
    fake_logging.log = lambda *a, **k: None
    fake_logging.format_log_message = lambda level, msg: msg
    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.api_call_stats = {"calls": []}
    fake_settings.API_KEY_DAILY_LIMIT = 100
    fake_settings.KEY_ROTATION_STRATEGY = "polling"
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_utils_pkg = types.ModuleType("app.utils")
    fake_utils_pkg.__path__ = []
    fake_stealth = types.ModuleType("app.utils.stealth")
    import random as _random

    def _full_jitter(attempt, base=1.0, cap=60.0):
        return _random.uniform(0.0, min(base * (2 ** max(0, attempt)), cap))

    fake_stealth.full_jitter_backoff = _full_jitter
    fake_stealth.build_key_probe_headers = lambda *a, **k: {}
    fake_stealth.pick_user_agent = lambda *a, **k: "curl/8.9.1"
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.__path__ = []
    return {
        "app.utils.logging": fake_logging,
        "app.config.settings": fake_settings,
        "app.config": fake_config_pkg,
        "app.utils": fake_utils_pkg,
        "app.utils.stealth": fake_stealth,
        "app": fake_app_pkg,
    }


def _status_error(status: int, json_body=None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.test")
    kwargs = {"json": json_body} if json_body is not None else {}
    resp = httpx.Response(status, request=req, **kwargs)
    return httpx.HTTPStatusError(str(status), request=req, response=resp)


# ---------------------------------------------------------------------------
# 1. 失败分类器 + should_retry_same_key（error_handling.py）
# ---------------------------------------------------------------------------


class FailureClassificationTestCase(unittest.TestCase):
    def _load(self):
        return load_module(
            "error_handling_r7", "app/utils/error_handling.py", _fake_env()
        )

    def test_429_is_quota(self):
        mod = self._load()
        self.assertEqual(mod._classify_failure(_status_error(429)), "quota")

    def test_401_403_are_dead(self):
        mod = self._load()
        self.assertEqual(mod._classify_failure(_status_error(401)), "dead")
        self.assertEqual(mod._classify_failure(_status_error(403)), "dead")

    def test_500_503_are_transient(self):
        mod = self._load()
        self.assertEqual(mod._classify_failure(_status_error(500)), "transient")
        self.assertEqual(mod._classify_failure(_status_error(503)), "transient")

    def test_network_errors_are_transient(self):
        mod = self._load()
        self.assertEqual(
            mod._classify_failure(httpx.ReadTimeout("t")), "transient"
        )
        self.assertEqual(
            mod._classify_failure(httpx.ConnectError("c")), "transient"
        )

    def test_unknown_exception_is_transient(self):
        mod = self._load()
        self.assertEqual(mod._classify_failure(RuntimeError("boom")), "transient")


class ShouldRetrySameKeyTestCase(unittest.TestCase):
    def _load(self):
        return load_module(
            "error_handling_r7b", "app/utils/error_handling.py", _fake_env()
        )

    def test_no_record_means_same_key(self):
        """空响应等不经过 handle_gemini_error 的失败默认原 key 重试。"""
        mod = self._load()
        mod.reset_key_failure_kinds()
        self.assertTrue(mod.should_retry_same_key("fresh-key"))

    def test_transient_failure_reuses_key(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod._key_failure_kinds["k"] = "transient"
        self.assertTrue(mod.should_retry_same_key("k"))

    def test_quota_failure_switches_key(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod._key_failure_kinds["k"] = "quota"
        self.assertFalse(mod.should_retry_same_key("k"))

    def test_dead_failure_switches_key(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod._key_failure_kinds["k"] = "dead"
        self.assertFalse(mod.should_retry_same_key("k"))

    def test_clear_restores_default(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod._key_failure_kinds["k"] = "quota"
        mod.clear_key_failure_kind("k")
        self.assertTrue(mod.should_retry_same_key("k"))
        self.assertIsNone(mod.get_key_failure_kind("k"))


class HandleGeminiErrorRecordsKindTestCase(unittest.TestCase):
    """handle_gemini_error 必须在返回前**同步**写好失败类别。"""

    def _load(self):
        return load_module(
            "error_handling_r7c", "app/utils/error_handling.py", _fake_env()
        )

    def test_500_error_records_transient_synchronously(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod.handle_gemini_error(_status_error(500), "key-500")
        # 不需要任何事件循环调度机会，读取必须立即生效
        self.assertEqual(mod.get_key_failure_kind("key-500"), "transient")

    def test_429_error_records_quota(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        body = {
            "error": {
                "code": 429,
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                     "retryDelay": "26s"}
                ],
            }
        }
        mod.handle_gemini_error(_status_error(429, body), "key-429")
        self.assertEqual(mod.get_key_failure_kind("key-429"), "quota")

    def test_timeout_error_records_transient(self):
        mod = self._load()
        mod.reset_key_failure_kinds()
        mod.handle_gemini_error(httpx.ReadTimeout("t"), "key-timeout")
        self.assertEqual(mod.get_key_failure_kind("key-timeout"), "transient")


# ---------------------------------------------------------------------------
# 2. 冷却门控收紧（schedule_key_cooldown）
# ---------------------------------------------------------------------------


class CooldownGatingTestCase(unittest.TestCase):
    def _run_with_fake_mark(self, schedule_fn):
        """在 fake app.utils.api_key 保持挂载的状态下执行调度。

        mark_key_failure 是 fire-and-forget 任务里的**延迟导入**，
        必须在事件循环运行期间保持 sys.modules 里的 fake（load_module
        加载完会还原 sys.modules，round-6 同款模式）。
        """
        calls = []

        async def fake_mark(api_key, status_code, cooldown_seconds=None):
            calls.append((api_key, status_code, cooldown_seconds))

        fake_api_key_mod = types.ModuleType("app.utils.api_key")
        fake_api_key_mod.mark_key_failure = fake_mark
        saved = sys.modules.get("app.utils.api_key")
        sys.modules["app.utils.api_key"] = fake_api_key_mod
        try:
            mod = load_module(
                "error_handling_r7d", "app/utils/error_handling.py", _fake_env()
            )

            async def run():
                schedule_fn(mod)
                await asyncio.sleep(0.01)  # 给 fire-and-forget 任务调度机会

            asyncio.run(run())
        finally:
            if saved is None:
                sys.modules.pop("app.utils.api_key", None)
            else:
                sys.modules["app.utils.api_key"] = saved
        return calls

    def test_500_does_not_cooldown(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(_status_error(500), "k")
        )
        self.assertEqual(calls, [])

    def test_503_does_not_cooldown(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(_status_error(503), "k")
        )
        self.assertEqual(calls, [])

    def test_network_error_does_not_cooldown(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(
                httpx.ConnectError("refused"), "k"
            )
        )
        self.assertEqual(calls, [])

    def test_429_still_cooldowns_with_retry_delay(self):
        body = {
            "error": {
                "code": 429,
                "details": [{"retryDelay": "26s"}],
            }
        }
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(
                _status_error(429, body), "k"
            )
        )
        self.assertEqual(calls, [("k", 429, 26.0)])

    def test_429_without_retry_delay_passes_none(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(_status_error(429), "k")
        )
        self.assertEqual(calls, [("k", 429, None)])

    def test_401_still_blocks_permanently(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(_status_error(401), "k")
        )
        self.assertEqual(calls, [("k", 401, None)])

    def test_403_still_blocks_permanently(self):
        calls = self._run_with_fake_mark(
            lambda mod: mod.schedule_key_cooldown(_status_error(403), "k")
        )
        self.assertEqual(calls, [("k", 403, None)])


# ---------------------------------------------------------------------------
# 3. select_valid_api_keys 的 preferred_keys 亲和阶段
# ---------------------------------------------------------------------------


class _RotatingKeyManager:
    """polling 语义的假 key_manager：每次 get_available_key 都 pop 下一个。

    栈初始化为 reversed(keys)，pop() 先返回 keys[0]（与真实
    _reset_key_stack + polling pop 语义一致）。
    """

    def __init__(self, keys):
        self.api_keys = list(keys)
        self.stack = list(reversed(keys))  # pop() 先出 keys[0]
        self.calls = 0

    async def get_available_key(self):
        self.calls += 1
        if not self.stack:
            self.stack = list(reversed(self.api_keys))
        return self.stack.pop() if self.stack else None


class PreferredKeysAffinityTestCase(unittest.TestCase):
    def _load(self, rpm_counts=None, cooled=(), usage=0, daily_limit=100):
        """usage 可传标量（所有 key 同值）或 dict（按 key）。"""
        rpm_counts = rpm_counts or {}

        def _usage_of(key):
            if isinstance(usage, dict):
                return usage.get(key, 0)
            return usage

        env = _fake_env()
        fake_stats = types.ModuleType("app.utils.stats")
        fake_stats.get_calls_last_minute_for_key = lambda k: rpm_counts.get(k, 0)
        fake_stats.MAX_OUTBOUND_RPM = 15
        fake_stats.OUTBOUND_RPM_BACKOFF_FRACTION = 0.8  # threshold = 12

        async def get_usage(_stats, key, model=None):
            return _usage_of(key)

        fake_stats.get_api_key_usage = get_usage
        env["app.utils.stats"] = fake_stats

        fake_api_key = types.ModuleType("app.utils.api_key")
        cooled_set = set(cooled)

        async def is_cooled(key):
            return key in cooled_set

        fake_api_key.is_key_cooled_down = is_cooled
        env["app.utils.api_key"] = fake_api_key

        env["app.config.settings"].API_KEY_DAILY_LIMIT = daily_limit
        return load_module(
            "api_key_selection_r7", "app/utils/api_key_selection.py", env
        )

    def test_preferred_key_reused_without_rotation(self):
        """瞬时失败的 key 作为 preferred 传入时，绝不轮换到其他 key。"""
        mod = self._load()
        km = _RotatingKeyManager(["a", "b", "c"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=["a"],
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])
        # 亲和阶段直接命中，不应触碰轮换栈
        self.assertEqual(km.calls, 0)

    def test_preferred_batch_fills_entirely_from_preferred(self):
        """多个 preferred key 时整批由 preferred 组成，不掺入轮换 key。"""
        mod = self._load()
        km = _RotatingKeyManager(["a", "b", "c", "d"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=3, request_type="t", model="m",
                preferred_keys=["a", "b"],
            )

        keys = asyncio.run(run())
        self.assertEqual(sorted(keys), ["a", "b"])
        self.assertEqual(km.calls, 0)

    def test_preferred_cooled_key_falls_back_to_rotation(self):
        """亲和期间 key 被打成 429（冷却中）→ 正常轮换到下一个 key。"""
        mod = self._load(cooled={"a"})
        km = _RotatingKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=["a"],
            )

        keys = asyncio.run(run())
        # 亲和跳过 a；正常路径 pop 出 a（冷却中，再跳过）→ 拿到 b
        self.assertEqual(keys, ["b"])
        self.assertEqual(km.calls, 2)

    def test_preferred_rpm_hot_key_yields(self):
        """preferred key 的 RPM 达阈值（分钟级配额耗尽）→ 让位轮换。"""
        mod = self._load(rpm_counts={"a": 14})
        km = _RotatingKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=["a"],
            )

        keys = asyncio.run(run())
        # 亲和阶段跳过 a；正常路径 pop 出 a 仍 RPM 满 → 跳过 → 拿到 b
        self.assertEqual(keys, ["b"])
        self.assertEqual(km.calls, 2)

    def test_preferred_daily_limited_key_falls_back(self):
        """preferred key 日额度耗尽（配额耗尽）→ 正常轮换到健康 key。"""
        mod = self._load(usage={"a": 100}, daily_limit=100)
        km = _RotatingKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=["a"],
            )

        keys = asyncio.run(run())
        # a 日额度满被跳过（亲和 + 正常路径都跳过），拿到 b
        self.assertEqual(keys, ["b"])
        self.assertEqual(km.calls, 2)

    def test_no_preferred_keys_keeps_original_behaviour(self):
        """不传 preferred 时行为与旧版完全一致。"""
        mod = self._load()
        km = _RotatingKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m"
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])
        self.assertEqual(km.calls, 1)

    def test_empty_preferred_list_behaves_like_none(self):
        mod = self._load()
        km = _RotatingKeyManager(["a", "b"])

        async def run():
            return await mod.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=[],
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])
        self.assertEqual(km.calls, 1)


# ---------------------------------------------------------------------------
# 4. 重试循环模式（同 key 重试 vs 配额耗尽轮换）
# ---------------------------------------------------------------------------


class RetryLoopPatternTestCase(unittest.TestCase):
    """模拟重试循环里的 preferred_keys 计算模式（4 个循环共用）。"""

    def _load(self):
        env = _fake_env()
        fake_api_key = types.ModuleType("app.utils.api_key")

        async def not_cooled(key):
            return False

        async def fake_mark(api_key, status_code, cooldown_seconds=None):
            pass

        fake_api_key.is_key_cooled_down = not_cooled
        fake_api_key.mark_key_failure = fake_mark
        env["app.utils.api_key"] = fake_api_key

        fake_stats = types.ModuleType("app.utils.stats")
        fake_stats.get_calls_last_minute_for_key = lambda k: 0
        fake_stats.MAX_OUTBOUND_RPM = 15
        fake_stats.OUTBOUND_RPM_BACKOFF_FRACTION = 0.8

        async def get_usage(_stats, key, model=None):
            return 0

        fake_stats.get_api_key_usage = get_usage
        env["app.utils.stats"] = fake_stats

        eh = load_module(
            "error_handling_r7e", "app/utils/error_handling.py", env
        )
        sel = load_module(
            "api_key_selection_r7e", "app/utils/api_key_selection.py", env
        )
        return eh, sel

    def test_transient_failure_retries_same_key(self):
        """500 失败 → handle_gemini_error → 下一批仍选中同一个 key。"""
        eh, sel = self._load()
        eh.reset_key_failure_kinds()

        km = _RotatingKeyManager(["a", "b"])
        valid_keys = ["a"]  # 第一批选中 a

        # 第一批请求 500 失败（走真实 handle_gemini_error 分类路径）
        eh.handle_gemini_error(_status_error(500), "a")

        # 重试循环的 preferred 计算模式
        preferred = [k for k in valid_keys if eh.should_retry_same_key(k)] or None
        self.assertEqual(preferred, ["a"])

        async def run():
            return await sel.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=preferred,
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])
        self.assertEqual(km.calls, 0)  # 没有轮换

    def test_quota_failure_rotates_to_next_key(self):
        """429 配额耗尽 → preferred 为空 → 正常轮换到下一个 key。"""
        eh, sel = self._load()
        eh.reset_key_failure_kinds()

        km = _RotatingKeyManager(["a", "b"])
        valid_keys = ["a"]

        # 第一批请求 429 失败（配额耗尽）
        eh.handle_gemini_error(_status_error(429), "a")

        preferred = [k for k in valid_keys if eh.should_retry_same_key(k)] or None
        self.assertIsNone(preferred)

        async def run():
            return await sel.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=preferred,
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])  # 轮换栈 pop（无冷却 fake 时仍拿 a）
        self.assertEqual(km.calls, 1)  # 触碰了轮换栈 = 发生了轮换语义

    def test_network_failure_retries_same_key(self):
        """网络错误（超时）→ 原 key 重试，不轮换。"""
        eh, sel = self._load()
        eh.reset_key_failure_kinds()

        km = _RotatingKeyManager(["a", "b"])
        valid_keys = ["a"]

        eh.handle_gemini_error(httpx.ReadTimeout("upstream slow"), "a")

        preferred = [k for k in valid_keys if eh.should_retry_same_key(k)] or None
        self.assertEqual(preferred, ["a"])

        async def run():
            return await sel.select_valid_api_keys(
                km, batch_num=1, request_type="t", model="m",
                preferred_keys=preferred,
            )

        keys = asyncio.run(run())
        self.assertEqual(keys, ["a"])
        self.assertEqual(km.calls, 0)


if __name__ == "__main__":
    unittest.main()
