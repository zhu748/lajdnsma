"""Round 8 回归测试 —— PVP 模式（指定 key 持续重试）。

用户策略：
  在设置里开启 PVP 模式后，可指定一个 key 持续重试直到输出结果；
  并提供最大重试次数（PVP_MAX_RETRIES）防止无限重试。

实现拆分（本文件逐层覆盖）：
1. sanitize_pvp_selector：完整密钥脱敏为尾片段（不落盘明文），
   序号/哈希/短片段原样保留
2. resolve_pvp_key：四种选择器写法（完整密钥/#序号/纯数字序号/
   key#哈希十进制与十六进制/唯一片段），歧义与未匹配返回 None
3. is_pvp_enabled / get_pvp_max_retries / effective_max_retries：
   开关组合、非法值兜底、PVP 开关下的重试预算切换
4. select_valid_api_keys 的 PVP 分支：钉住 key 无视冷却/RPM/日额度；
   死 key（401/403）提前终止返回空列表；选择器解析失败回落正常轮换
5. retry_state.elevate_pvp_backoff：非 PVP 透传；PVP 下按冷却剩余
   抬高等待且封顶 8s；pvp 缺位时 fail-open
6. APIKeyManager.get_available_key 的 PVP 分支 + peek_key_cooldown_remaining
"""
import asyncio
import sys
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


def _fake_env():
    """pvp.py / api_key_selection.py / api_key.py 共用的最小假环境。"""
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
    fake_stealth.build_key_probe_headers = lambda *a, **k: {}
    fake_stealth.pick_user_agent = lambda *a, **k: "curl/8.9.1"
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


def _register(env):
    """把假环境常驻 sys.modules（conftest 夹具会在测试后恢复快照）。

    用途：api_key_selection / retry_state 在**调用时**才延迟导入
    app.utils.pvp，必须让 `import app.utils.pvp` 在 asyncio.run 期间
    能从 sys.modules 命中已加载的真实 pvp 模块。
    """
    for key, mod in env.items():
        sys.modules[key] = mod


class _StaticKeyManager:
    """固定密钥池桩：resolve/get_available_key 只读 api_keys。"""

    def __init__(self, keys):
        self.api_keys = list(keys)


class _RotatingKeyManager:
    """round7 同款轮换桩：每次调用按序弹出，记录调用次数。"""

    def __init__(self, keys):
        self.api_keys = list(keys)
        self.stack = list(keys)
        self.calls = 0

    async def get_available_key(self):
        self.calls += 1
        if self.stack:
            return self.stack.pop(0)
        return None

    async def advance_sticky_key(self):
        return None


# 测试密钥池：必须是纯不透明 token（>= 20 字符，无 AIzaSy/AQ. 前缀），
# 否则 _parse_api_keys 的正则提取会把池搅乱（AIzaSy 串会被截取）。
POOL = [
    "pvpkey0AAAAAAAAAAAAAAAAAAAA",
    "pvpkey1BBBBBBBBBBBBBBBBBBBB",
    "pvpkey2CCCCCCCCCCCCCCCCCCCC",
    "pvpkey3DDDDDDDDDDDDDDDDDDDD",
]


# ---------------------------------------------------------------------------
# 1. sanitize_pvp_selector（pvp.py）
# ---------------------------------------------------------------------------


class SanitizeSelectorTestCase(unittest.TestCase):
    def _load(self):
        return load_module("pvp_sanitize_r8", "app/utils/pvp.py", _fake_env())

    def test_full_classic_key_masked_to_tail(self):
        mod = self._load()
        raw = "AIzaSy" + "a" * 33  # 39 字符经典格式
        self.assertEqual(mod.sanitize_pvp_selector(raw), raw[-6:])

    def test_long_opaque_token_masked_to_tail(self):
        mod = self._load()
        raw = "x" * 25
        self.assertEqual(mod.sanitize_pvp_selector(raw), raw[-6:])

    def test_short_selector_kept_as_is(self):
        mod = self._load()
        self.assertEqual(mod.sanitize_pvp_selector("#0"), "#0")
        self.assertEqual(mod.sanitize_pvp_selector("abc123"), "abc123")

    def test_hash_selector_kept_as_is(self):
        mod = self._load()
        self.assertEqual(
            mod.sanitize_pvp_selector("key#1a2b3c"), "key#1a2b3c"
        )

    def test_empty(self):
        mod = self._load()
        self.assertEqual(mod.sanitize_pvp_selector(""), "")
        self.assertEqual(mod.sanitize_pvp_selector(None), "")


# ---------------------------------------------------------------------------
# 2. resolve_pvp_key（pvp.py）
# ---------------------------------------------------------------------------


class ResolveSelectorTestCase(unittest.TestCase):
    def _load(self, *, mode=True, selector="#0"):
        env = _fake_env()
        env["app.config.settings"].PVP_MODE = mode
        env["app.config.settings"].PVP_KEY = selector
        return load_module("pvp_resolve_r8", "app/utils/pvp.py", env), env

    def test_disabled_returns_none(self):
        mod, _ = self._load(mode=False, selector="#0")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager(POOL)))

    def test_disabled_with_force_still_resolves(self):
        mod, _ = self._load(mode=False, selector="#1")
        km = _StaticKeyManager(POOL)
        self.assertEqual(mod.resolve_pvp_key(km, force=True), POOL[1])

    def test_exact_full_key(self):
        mod, _ = self._load(selector=POOL[0])
        self.assertEqual(
            mod.resolve_pvp_key(_StaticKeyManager(POOL)), POOL[0]
        )

    def test_index_hash_and_plain_index(self):
        mod, _ = self._load(selector="#2")
        self.assertEqual(mod.resolve_pvp_key(_StaticKeyManager(POOL)), POOL[2])
        mod, _ = self._load(selector="3")
        self.assertEqual(mod.resolve_pvp_key(_StaticKeyManager(POOL)), POOL[3])

    def test_index_out_of_range(self):
        mod, _ = self._load(selector="#99")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager(POOL)))

    def test_hash_decimal_from_dashboard(self):
        # stats 面板展示十进制："key#" + str(hash & 0xFFFFFF)
        km = _StaticKeyManager(POOL)
        target = hash(POOL[1]) & 0xFFFFFF
        mod, _ = self._load(selector=f"key#{target}")
        self.assertEqual(mod.resolve_pvp_key(km), POOL[1])

    def test_hash_hex_from_logs(self):
        # 日志展示十六进制：f"key#{...:06x}"
        km = _StaticKeyManager(POOL)
        target = f"{hash(POOL[2]) & 0xFFFFFF:06x}"
        mod, _ = self._load(selector=f"key#{target}")
        self.assertEqual(mod.resolve_pvp_key(km), POOL[2])

    def test_unique_fragment(self):
        mod, _ = self._load(selector="key3")
        self.assertEqual(mod.resolve_pvp_key(_StaticKeyManager(POOL)), POOL[3])

    def test_ambiguous_fragment_returns_none(self):
        mod, _ = self._load(selector="pvpkey")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager(POOL)))

    def test_unknown_fragment_returns_none(self):
        mod, _ = self._load(selector="zzzz99")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager(POOL)))

    def test_too_short_fragment_returns_none(self):
        mod, _ = self._load(selector="k2")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager(POOL)))

    def test_empty_pool_returns_none(self):
        mod, _ = self._load(selector="#0")
        self.assertIsNone(mod.resolve_pvp_key(_StaticKeyManager([])))

    def test_none_key_manager_returns_none(self):
        mod, _ = self._load(selector="#0")
        self.assertIsNone(mod.resolve_pvp_key(None))


# ---------------------------------------------------------------------------
# 3. 开关与重试预算（pvp.py + api_key_selection.effective_max_retries）
# ---------------------------------------------------------------------------


class PvpEnabledAndBudgetTestCase(unittest.TestCase):
    def _load_pvp(self, **attrs):
        env = _fake_env()
        for key, value in attrs.items():
            setattr(env["app.config.settings"], key, value)
        mod = load_module("pvp_flags_r8", "app/utils/pvp.py", env)
        return mod, env

    def test_enabled_requires_mode_and_key(self):
        mod, _ = self._load_pvp()
        self.assertFalse(mod.is_pvp_enabled())
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="")
        self.assertFalse(mod.is_pvp_enabled())
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="#0")
        self.assertTrue(mod.is_pvp_enabled())

    def test_activation_logged_once_per_pin_change(self):
        """激活日志只在钉住对象变化时打一条（不随批刷屏）。"""
        env = _fake_env()
        settings = env["app.config.settings"]
        settings.PVP_MODE = True
        settings.PVP_KEY = "#0"
        logs = []
        env["app.utils.logging"].log = lambda level, msg, **kw: logs.append(
            (level, msg)
        )
        mod = load_module("pvp_log_r8", "app/utils/pvp.py", env)
        km = _StaticKeyManager(POOL)
        mod.resolve_pvp_key(km)
        mod.resolve_pvp_key(km)  # 同一 key 再解析：不重复打
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][0], "info")
        self.assertIn("PVP 模式已激活", logs[0][1])

        # 切换钉住对象（选择器变更）→ 再打一条
        settings.PVP_KEY = "#1"
        mod.resolve_pvp_key(km)
        self.assertEqual(len(logs), 2)

        # force 预校验（dashboard 保存路径）不打激活日志
        settings.PVP_KEY = "#2"
        mod.resolve_pvp_key(km, force=True)
        self.assertEqual(len(logs), 2)

    def test_max_retries_defaults_and_sanity(self):
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="#0")
        self.assertEqual(mod.get_pvp_max_retries(), 50)  # 缺省
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="#0", PVP_MAX_RETRIES="abc")
        self.assertEqual(mod.get_pvp_max_retries(), 50)  # 非法回退
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="#0", PVP_MAX_RETRIES=-5)
        self.assertEqual(mod.get_pvp_max_retries(), 1)  # 下限钳制
        mod, _ = self._load_pvp(PVP_MODE=True, PVP_KEY="#0", PVP_MAX_RETRIES=7)
        self.assertEqual(mod.get_pvp_max_retries(), 7)

    def test_effective_max_retries_switches_budget(self):
        env = _fake_env()
        settings = env["app.config.settings"]
        settings.MAX_RETRY_NUM = 15
        settings.PVP_MODE = False

        # api_key_selection 顶层依赖 stats / api_key，补齐最小桩
        fake_stats = types.ModuleType("app.utils.stats")
        fake_stats.get_calls_last_minute_for_key = lambda k: 0
        fake_stats.MAX_OUTBOUND_RPM = 15
        fake_stats.OUTBOUND_RPM_BACKOFF_FRACTION = 0.8

        async def get_usage(_stats, key, model=None):
            return 0

        fake_stats.get_api_key_usage = get_usage
        env["app.utils.stats"] = fake_stats

        fake_api_key_mod = types.ModuleType("app.utils.api_key")

        async def is_cooled(key):
            return False

        fake_api_key_mod.is_key_cooled_down = is_cooled
        env["app.utils.api_key"] = fake_api_key_mod

        sel = load_module("aksel_budget_r8", "app/utils/api_key_selection.py", env)
        self.assertEqual(sel.effective_max_retries(), 15)

        # PVP 开启：经 sys.modules 中注册的真实 pvp 模块读取新预算
        settings.PVP_MODE = True
        settings.PVP_KEY = "#0"
        settings.PVP_MAX_RETRIES = 7
        pvp = load_module("pvp_budget_r8", "app/utils/pvp.py", env)
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            self.assertEqual(sel.effective_max_retries(), 7)
        finally:
            sys.modules.pop("app.utils.pvp", None)


# ---------------------------------------------------------------------------
# 4. select_valid_api_keys 的 PVP 分支（api_key_selection.py）
# ---------------------------------------------------------------------------


class SelectionPvpBranchTestCase(unittest.TestCase):
    def _load(self, *, mode=True, selector="#0", cooled=(), rpm_counts=None,
              usage=None, daily_limit=100, failure_kinds=None):
        env = _fake_env()
        settings = env["app.config.settings"]
        settings.PVP_MODE = mode
        settings.PVP_KEY = selector
        settings.API_KEY_DAILY_LIMIT = daily_limit

        fake_stats = types.ModuleType("app.utils.stats")
        fake_stats.get_calls_last_minute_for_key = lambda k: (rpm_counts or {}).get(k, 0)
        fake_stats.MAX_OUTBOUND_RPM = 15
        fake_stats.OUTBOUND_RPM_BACKOFF_FRACTION = 0.8

        async def get_usage(_stats, key, model=None):
            if isinstance(usage, dict):
                return usage.get(key, 0)
            return usage or 0

        fake_stats.get_api_key_usage = get_usage
        env["app.utils.stats"] = fake_stats

        fake_api_key = types.ModuleType("app.utils.api_key")
        cooled_set = set(cooled)

        async def is_cooled(key):
            return key in cooled_set

        fake_api_key.is_key_cooled_down = is_cooled
        env["app.utils.api_key"] = fake_api_key

        # 死 key 判定走 error_handling.get_key_failure_kind
        kinds = dict(failure_kinds or {})
        fake_error_handling = types.ModuleType("app.utils.error_handling")
        fake_error_handling.get_key_failure_kind = lambda k: kinds.get(k)
        env["app.utils.error_handling"] = fake_error_handling

        pvp = load_module("pvp_sel_r8", "app/utils/pvp.py", env)
        sel = load_module("aksel_sel_r8", "app/utils/api_key_selection.py", env)
        return sel, pvp, env, settings

    def _run(self, sel, km, **kwargs):
        _register(self._env)
        sys.modules["app.utils.pvp"] = self._pvp
        try:
            return asyncio.run(
                sel.select_valid_api_keys(km, batch_num=1, request_type="t", model="m", **kwargs)
            )
        finally:
            sys.modules.pop("app.utils.pvp", None)

    def test_pinned_key_bypasses_cooldown_rpm_daily(self):
        """PVP 的核心语义：冷却 / RPM 阈值 / 日额度都不换 key。"""
        sel, pvp, env, _ = self._load(
            selector="#0",
            cooled={POOL[0]},
            rpm_counts={POOL[0]: 99},
            usage={POOL[0]: 999},
        )
        self._env, self._pvp = env, pvp
        km = _RotatingKeyManager(POOL)
        keys = self._run(sel, km)
        self.assertEqual(keys, [POOL[0]])
        self.assertEqual(km.calls, 0)  # 不触碰轮换栈

    def test_pvp_key_001_decimal_hash_selector(self):
        sel, pvp, env, _ = self._load(selector=f"key#{hash(POOL[1]) & 0xFFFFFF}")
        self._env, self._pvp = env, pvp
        km = _StaticKeyManager(POOL)
        self.assertEqual(self._run(sel, km), [POOL[1]])

    def test_dead_key_aborts_immediately(self):
        """401/403 失效的 key 重试不可能成功 → 返回空提前终止。"""
        sel, pvp, env, _ = self._load(
            selector="#0", failure_kinds={POOL[0]: "dead"}
        )
        self._env, self._pvp = env, pvp
        km = _RotatingKeyManager(POOL)
        self.assertEqual(self._run(sel, km), [])
        self.assertEqual(km.calls, 0)

    def test_transient_failure_keeps_pinning(self):
        """500/503 等瞬时失败（transient）不终止 PVP，继续钉住。"""
        sel, pvp, env, _ = self._load(
            selector="#0", failure_kinds={POOL[0]: "transient"}
        )
        self._env, self._pvp = env, pvp
        km = _StaticKeyManager(POOL)
        self.assertEqual(self._run(sel, km), [POOL[0]])

    def test_unresolvable_selector_falls_back_to_rotation(self):
        """选择器解析失败：warn-once 后回落正常轮换，不 fail-closed。"""
        sel, pvp, env, _ = self._load(selector="zzzz99")
        self._env, self._pvp = env, pvp
        km = _RotatingKeyManager(POOL)
        keys = self._run(sel, km)
        self.assertEqual(keys, [POOL[0]])  # 正常轮换的第一个 key
        self.assertEqual(km.calls, 1)

    def test_pvp_off_keeps_round7_behaviour(self):
        """PVP 关闭时行为与 Round 7 完全一致（含 preferred 亲和）。"""
        sel, pvp, env, _ = self._load(mode=False, selector="#0")
        self._env, self._pvp = env, pvp
        km = _RotatingKeyManager(POOL)
        keys = self._run(sel, km, preferred_keys=[POOL[2]])
        self.assertEqual(keys, [POOL[2]])
        self.assertEqual(km.calls, 0)


# ---------------------------------------------------------------------------
# 5. elevate_pvp_backoff（retry_state.py）
# ---------------------------------------------------------------------------


class ElevateBackoffTestCase(unittest.TestCase):
    def _setup(self, *, mode=True, remaining=0.0):
        env = _fake_env()
        settings = env["app.config.settings"]
        settings.PVP_MODE = mode
        settings.PVP_KEY = "#0"

        fake_api_key = types.ModuleType("app.utils.api_key")

        async def peek(key):
            return remaining

        fake_api_key.peek_key_cooldown_remaining = peek
        env["app.utils.api_key"] = fake_api_key

        pvp = load_module("pvp_backoff_r8", "app/utils/pvp.py", env)
        # 先解析一次，让 _last_resolved_key 指向池内 key
        pvp.resolve_pvp_key(_StaticKeyManager(POOL))

        # retry_state 是零依赖纯函数模块，直接按文件加载
        # （不能用 from app.utils import ... —— 会触发真实包 __init__
        # 的重依赖链，测试环境缺 xxhash）。
        retry_state_mod = load_module("retry_state_r8", "app/utils/retry_state.py")

        return retry_state_mod, pvp, env

    def _run_elevate(self, base, env, pvp):
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            return asyncio.run(self._mod.elevate_pvp_backoff(base))
        finally:
            sys.modules.pop("app.utils.pvp", None)

    def test_zero_base_passthrough(self):
        self._mod, pvp, env = self._setup(remaining=20)
        self.assertEqual(self._run_elevate(0.0, env, pvp), 0.0)

    def test_no_cooldown_passthrough(self):
        self._mod, pvp, env = self._setup(remaining=0.0)
        base = 0.4
        self.assertEqual(self._run_elevate(base, env, pvp), base)

    def test_cooldown_elevates_and_caps_at_8s(self):
        self._mod, pvp, env = self._setup(remaining=20.0)
        self.assertEqual(self._run_elevate(0.4, env, pvp), 8.0)
        self._mod2, pvp2, env2 = self._setup(remaining=3.0)
        self.assertEqual(self._run_elevate(0.4, env2, pvp2), 3.0)

    def test_never_below_base(self):
        self._mod, pvp, env = self._setup(remaining=0.5)
        base = 2.0
        self.assertEqual(self._run_elevate(base, env, pvp), 2.0)

    def test_pvp_disabled_passthrough(self):
        self._mod, pvp, env = self._setup(mode=False, remaining=20.0)
        base = 0.4
        self.assertEqual(self._run_elevate(base, env, pvp), base)

    def test_pvp_module_missing_fail_open(self):
        """app.utils.pvp 缺位时恒等于 base（可选增强层契约）。"""
        retry_state_mod = load_module("retry_state_r8b", "app/utils/retry_state.py")

        self._mod = retry_state_mod
        sys.modules.pop("app.utils.pvp", None)
        base = 0.4
        self.assertEqual(asyncio.run(self._mod.elevate_pvp_backoff(base)), base)


# ---------------------------------------------------------------------------
# 6. APIKeyManager PVP 分支 + peek_key_cooldown_remaining（api_key.py）
# ---------------------------------------------------------------------------


class KeyManagerPvpTestCase(unittest.TestCase):
    def _load(self, *, mode=True, selector="#0"):
        env = _fake_env()
        settings = env["app.config.settings"]
        settings.GEMINI_API_KEYS = ",".join(POOL)
        settings.PVP_MODE = mode
        settings.PVP_KEY = selector
        settings.API_KEY_DAILY_LIMIT = 100
        mgr_mod = load_module("api_key_r8", "app/utils/api_key.py", env)
        pvp = load_module("pvp_km_r8", "app/utils/pvp.py", env)
        return mgr_mod, pvp, env

    def test_get_available_key_pins_in_polling_mode(self):
        mgr_mod, pvp, env = self._load(selector="#1")
        env["app.config.settings"].KEY_ROTATION_STRATEGY = "polling"
        mgr = mgr_mod.APIKeyManager()
        self.assertEqual(mgr.api_keys, POOL)
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            self.assertEqual(asyncio.run(mgr.get_available_key()), POOL[1])
        finally:
            sys.modules.pop("app.utils.pvp", None)

    def test_get_available_key_pins_in_fill_mode(self):
        mgr_mod, pvp, env = self._load(selector="#2")
        env["app.config.settings"].KEY_ROTATION_STRATEGY = "fill"
        mgr = mgr_mod.APIKeyManager()
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            self.assertEqual(asyncio.run(mgr.get_available_key()), POOL[2])
        finally:
            sys.modules.pop("app.utils.pvp", None)

    def test_get_available_key_unresolvable_falls_back(self):
        mgr_mod, pvp, env = self._load(selector="zzzz99")
        env["app.config.settings"].KEY_ROTATION_STRATEGY = "polling"
        mgr = mgr_mod.APIKeyManager()
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            got = asyncio.run(mgr.get_available_key())
        finally:
            sys.modules.pop("app.utils.pvp", None)
        self.assertIn(got, POOL)  # 正常轮换返回池内任一 key

    def test_get_available_key_pvp_off_normal(self):
        mgr_mod, pvp, env = self._load(mode=False, selector="#1")
        env["app.config.settings"].KEY_ROTATION_STRATEGY = "polling"
        mgr = mgr_mod.APIKeyManager()
        _register(env)
        sys.modules["app.utils.pvp"] = pvp
        try:
            got = asyncio.run(mgr.get_available_key())
        finally:
            sys.modules.pop("app.utils.pvp", None)
        self.assertIn(got, POOL)

    def test_peek_cooldown_remaining(self):
        mgr_mod, _, env = self._load(mode=False)

        async def run():
            await mgr_mod.mark_key_failure(POOL[0], 429, cooldown_seconds=2.0)
            remaining = await mgr_mod.peek_key_cooldown_remaining(POOL[0])
            clean = await mgr_mod.peek_key_cooldown_remaining(POOL[1])
            return remaining, clean

        remaining, clean = asyncio.run(run())
        self.assertGreater(remaining, 0.0)
        self.assertLessEqual(remaining, 2.0)
        self.assertEqual(clean, 0.0)
        # 清理全局冷却状态，避免污染同进程其他测试
        asyncio.run(mgr_mod.clear_all_cooldowns())


# ---------------------------------------------------------------------------
# 7. settings.py 内联脱敏规则与 pvp.sanitize_pvp_selector 的一致性
# ---------------------------------------------------------------------------


class SettingsInlineSanitizeConsistencyTestCase(unittest.TestCase):
    """settings.py 无法导入 pvp（会循环依赖），故内联了同一条脱敏规则。

    本守卫锁定两处规则不漂移：环境变量里的完整密钥经 settings 加载后，
    必须与权威实现 sanitize_pvp_selector 的输出完全一致，保证
    settings.json 落盘 / 配置接口回显永不含 PVP 明文密钥。
    """

    def _load_settings_with(self, env_value):
        import os

        saved = os.environ.get("PVP_KEY")
        os.environ["PVP_KEY"] = env_value
        try:
            return load_module("settings_r8", "app/config/settings.py")
        finally:
            if saved is None:
                os.environ.pop("PVP_KEY", None)
            else:
                os.environ["PVP_KEY"] = saved

    def test_full_key_env_sanitized_same_as_authority(self):
        pvp = load_module("pvp_consistency_r8", "app/utils/pvp.py", _fake_env())
        full_key = "AIzaSy" + "z" * 33
        settings_mod = self._load_settings_with(full_key)
        self.assertEqual(
            settings_mod.PVP_KEY, pvp.sanitize_pvp_selector(full_key)
        )
        self.assertEqual(settings_mod.PVP_KEY, full_key[-6:])

    def test_selector_forms_pass_through_unmangled(self):
        pvp = load_module("pvp_consistency_r8b", "app/utils/pvp.py", _fake_env())
        for selector in ("#0", "3", "key#1a2b3c", "abc123", ""):
            settings_mod = self._load_settings_with(selector)
            self.assertEqual(
                settings_mod.PVP_KEY, pvp.sanitize_pvp_selector(selector)
            )


if __name__ == "__main__":
    unittest.main()
