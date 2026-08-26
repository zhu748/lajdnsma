"""密钥解析回归测试。

背景 bug：旧版 APIKeyManager 仅用 `AIzaSy[a-zA-Z0-9_-]{33}` 正则从
GEMINI_API_KEYS 中提取密钥，而 Google 2025 起签发的新版密钥以 "AQ."
开头（约 90 字符）。结果：
  - 环境变量里配置 AQ. 密钥 -> 被静默丢弃，加载 0 个密钥
  - 面板"添加密钥"入口按逗号分割、不校验格式 -> AQ. 密钥可以加进去
两条路径行为不一致，用户侧表现为"环境变量加不进去、只有面板能加"。

修复后的契约：
  1. 环境变量解析接受任意"纯不透明 token"（字母数字 . _ -，长度 >= 20）
  2. 同时保留两种已知格式（AIzaSy 经典 / AQ. 新版）的正则提取，
     兼容把整段 JSON / 自由文本粘进环境变量的旧用法
  3. 保序去重；纯噪声 / 过短 token 丢弃
"""
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_optimization_regressions import load_module  # noqa: E402

# 合成测试密钥（与真实格式同构，非真实凭据）
CLASSIC_1 = "AIzaSy" + "a" * 33
CLASSIC_2 = "AIzaSy" + "b" * 33
NEW_1 = "AQ.Ab8RN8" + "X" * 60 + "-6LVncYSswDklm"
NEW_2 = "AQ.Ab8RN8" + "Y" * 60 + "-6LVncYSswDkmw"


def _load_api_key_module(gemini_api_keys=""):
    """以隔离的假依赖加载 app/utils/api_key.py。"""
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
    fake_settings.GEMINI_API_KEYS = gemini_api_keys
    fake_settings.KEY_ROTATION_STRATEGY = "fill"
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    fake_utils_pkg = types.ModuleType("app.utils")
    fake_utils_pkg.__path__ = []
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.__path__ = []

    return load_module(
        "api_key_parsing_under_test",
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


class ParseApiKeysTestCase(unittest.TestCase):
    """_parse_api_keys 的纯函数行为。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_api_key_module()

    def test_new_format_keys_comma_separated(self):
        """用户报告的原始场景：AQ. 新格式密钥逗号分隔必须全部解析。"""
        parsed = self.mod._parse_api_keys(f"{NEW_1},{NEW_2}")
        self.assertEqual(parsed, [NEW_1, NEW_2])

    def test_new_format_keys_newline_separated(self):
        parsed = self.mod._parse_api_keys(f"{NEW_1}\n{NEW_2}")
        self.assertEqual(sorted(parsed), sorted([NEW_1, NEW_2]))

    def test_mixed_formats(self):
        parsed = self.mod._parse_api_keys(f"{CLASSIC_1},{NEW_1},{CLASSIC_2}")
        self.assertEqual(sorted(parsed), sorted([CLASSIC_1, CLASSIC_2, NEW_1]))

    def test_classic_only_backward_compatible(self):
        """旧行为兼容：仅经典格式、逗号分隔。"""
        parsed = self.mod._parse_api_keys(f"{CLASSIC_1},{CLASSIC_2}")
        self.assertEqual(sorted(parsed), sorted([CLASSIC_1, CLASSIC_2]))

    def test_json_blob_paste(self):
        """整段 JSON 粘贴：分隔符切分会产生带标点的碎片，必须被 Pass 1
        拒绝、由 Pass 2 正则提取兜底。"""
        raw = '{"keys": ["%s", "%s"]}' % (CLASSIC_1, NEW_1)
        parsed = self.mod._parse_api_keys(raw)
        self.assertEqual(sorted(parsed), sorted([CLASSIC_1, NEW_1]))

    def test_quoted_keys(self):
        """密钥被引号包裹（平台环境变量注入常见）时必须剥掉引号。"""
        parsed = self.mod._parse_api_keys(f'"{NEW_1}","{NEW_2}"')
        self.assertEqual(sorted(parsed), sorted([NEW_1, NEW_2]))

    def test_duplicates_deduped_order_preserved(self):
        parsed = self.mod._parse_api_keys(f"{NEW_1},{CLASSIC_1},{NEW_1},{NEW_1}")
        self.assertEqual(parsed, [NEW_1, CLASSIC_1])

    def test_noise_and_short_tokens_dropped(self):
        parsed = self.mod._parse_api_keys("hello world 123, AQ.short, x, ")
        self.assertEqual(parsed, [])

    def test_empty_and_blank(self):
        self.assertEqual(self.mod._parse_api_keys(""), [])
        self.assertEqual(self.mod._parse_api_keys("   \n  "), [])


class ManagerEnvLoadingTestCase(unittest.TestCase):
    """APIKeyManager 从 settings + GEMINI_API_KEYS_N 加载密钥。"""

    def test_new_format_keys_loaded_from_settings(self):
        """回归核心：环境变量（settings.GEMINI_API_KEYS）里的 AQ. 密钥
        必须进入密钥池——旧实现在此场景加载 0 个密钥。"""
        mod = _load_api_key_module(gemini_api_keys=f"{NEW_1},{NEW_2}")
        mgr = mod.APIKeyManager()
        self.assertEqual(sorted(mgr.api_keys), sorted([NEW_1, NEW_2]))
        # 密钥栈也必须包含全部密钥（fill/polling 轮换的原料）
        self.assertEqual(sorted(mgr.key_stack), sorted([NEW_1, NEW_2]))

    def test_numbered_env_vars_new_format(self):
        """GEMINI_API_KEYS_N 编号变量里的 AQ. 密钥也必须被加载。"""
        mod = _load_api_key_module(gemini_api_keys=CLASSIC_1)
        env = {
            "GEMINI_API_KEYS_1": NEW_1,
            "GEMINI_API_KEYS_2": NEW_2,
        }
        saved = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            mgr = mod.APIKeyManager()
            self.assertEqual(
                sorted(mgr.api_keys), sorted([CLASSIC_1, NEW_1, NEW_2])
            )
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_classic_only_unchanged(self):
        """旧用法（仅经典格式）不受影响。"""
        mod = _load_api_key_module(
            gemini_api_keys=f"{CLASSIC_1},{CLASSIC_2}"
        )
        mgr = mod.APIKeyManager()
        self.assertEqual(len(mgr.api_keys), 2)


if __name__ == "__main__":
    unittest.main()
