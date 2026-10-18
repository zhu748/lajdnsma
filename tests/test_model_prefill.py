"""PREFILL_FIX（model-turn 结尾兼容补丁）单元测试。

覆盖两层：
1. app/utils/model_mapping.py::needs_model_turn_fix —— 对上游真实
   模型名单（含 2026-07-21 规则生效前后的全部世代）做表驱动断言；
2. app/services/gemini.py::GeminiClient.convert_messages —— 验证
   受限模型 + assistant 结尾时自动追加非空 user turn，旧模型与
   普通对话不受影响，以及 RANDOM_STRING / 白名单 / 黑名单 /
   开关 / 自定义追加文本的交互行为。

背景：Google 自 gemini-3.5-flash-lite / 3.6-flash（2026-07-21）起
拒绝以 model turn 结尾的请求（prefill / 前缀续写），返回 400
"Requests ending with a model turn are not supported."。
prefill 是全场景通用技巧（截断续写 / 填表 / 结构化前缀 /
抑制前言），补丁对所有这些场景统一生效。
官方迁移指南：ai.google.dev/gemini-api/docs/generate-content/latest-model
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 与 settings.py 默认值保持一致的通用续写指令（全场景 prefill）。
DEFAULT_PROMPT = (
    "Continue from where you left off, without repeating any content."
)


def load_real_model_mapping():
    """直接从文件加载真实的 model_mapping（仅依赖 re/fnmatch）。"""
    spec = importlib.util.spec_from_file_location(
        "prefill_model_mapping", ROOT / "app/utils/model_mapping.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_real_thinking_module():
    """直接从文件加载真实的 thinking（gemini.py 的思考覆盖依赖）。

    gemini.py 自最高思考强度功能起 `from app.utils.thinking import
    apply_thinking_override`，本 fake 链的 app.utils.__path__ 为空，
    不注册该模块会在加载 gemini.py 时直接 ModuleNotFoundError。
    真实 thinking 对 settings 的读取全部 getattr 防御：在本链的
    fake settings（无 MAX_THINKING 等键）下自然退化为「不覆盖」，
    不影响本文件的 prefill 补丁测试。
    """
    spec = importlib.util.spec_from_file_location(
        "prefill_thinking", ROOT / "app/utils/thinking.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_gemini_module(fake_settings_attrs=None):
    """加载真实 gemini.py，为其搭建完整 fake 依赖链。

    与 test_gemini_schema.py 不同：此处显式注册带 __path__ 的
    "app" / "app.utils" / "app.config" 包对象，避免 import 系统回退
    到磁盘上真实的包 __init__（其内部 import 与 fake 叶子模块冲突）。
    model_mapping 使用真实实现，确保测到补丁本体。
    """
    fake_settings_attrs = fake_settings_attrs or {}

    fake_app = types.ModuleType("app")
    fake_app.__path__ = []
    sys.modules["app"] = fake_app

    fake_utils = types.ModuleType("app.utils")
    fake_utils.__path__ = []
    sys.modules["app.utils"] = fake_utils

    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []
    sys.modules["app.config"] = fake_config_pkg

    fake_models = types.ModuleType("app.models")
    fake_models.__path__ = []
    sys.modules["app.models"] = fake_models
    fake_schemas = types.ModuleType("app.models.schemas")
    fake_schemas.ChatCompletionRequest = object
    sys.modules["app.models.schemas"] = fake_schemas
    fake_models.schemas = fake_schemas

    defaults = {
        "search": {"search_mode": False, "search_prompt": ""},
        "RANDOM_STRING": False,
        "RANDOM_STRING_LENGTH": 0,
        "PREFILL_FIX": True,
        "PREFILL_FIX_PROMPT": DEFAULT_PROMPT,
        "PREFILL_FIX_MODELS": [],
        "PREFILL_FIX_EXCLUDE_MODELS": [],
    }
    defaults.update(fake_settings_attrs)
    fake_settings = types.ModuleType("app.config.settings")
    for key, value in defaults.items():
        setattr(fake_settings, key, value)
    sys.modules["app.config.settings"] = fake_settings
    fake_config_pkg.settings = fake_settings

    fake_http_client = types.ModuleType("app.utils.http_client")
    fake_http_client.get_async_client = lambda: None
    fake_http_client.UPSTREAM_TIMEOUT = None
    sys.modules["app.utils.http_client"] = fake_http_client

    fake_logging = types.ModuleType("app.utils.logging")
    fake_logging.log = lambda *args, **kwargs: None
    sys.modules["app.utils.logging"] = fake_logging

    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.iter_sse_json = lambda *args, **kwargs: iter(())
    sys.modules["app.utils.sse"] = fake_sse

    fake_stealth = types.ModuleType("app.utils.stealth")
    fake_stealth.build_gemini_headers = lambda *a, **k: {}
    fake_stealth.build_key_probe_headers = lambda *a, **k: {}
    fake_stealth.gen_anthropic_thinking_signature = lambda: "x" * 64
    sys.modules["app.utils.stealth"] = fake_stealth

    # 真实的 model_mapping（补丁判断本体）注册进包链。
    real_model_mapping = load_real_model_mapping()
    sys.modules["app.utils.model_mapping"] = real_model_mapping
    fake_utils.model_mapping = real_model_mapping

    # 真实的 thinking（gemini.py 的思考覆盖依赖）注册进包链。
    real_thinking = load_real_thinking_module()
    sys.modules["app.utils.thinking"] = real_thinking
    fake_utils.thinking = real_thinking

    spec = importlib.util.spec_from_file_location(
        "prefill_gemini_service", ROOT / "app/services/gemini.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, real_model_mapping


# ---------------------------------------------------------------------------
# 上游真实模型名单（用户实际可用列表，按受限与否分组）
# ---------------------------------------------------------------------------

# 规则生效世代（2026-07-21 发布的 gemini-3.5-flash-lite / 3.6-flash
# 及此后所有模型；latest 指针指向当前最新世代；gemma-4 沿袭 Gemma
# "必须以 user turn 结尾"的既有约束）。
RESTRICTED_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-transcribe",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
    "lyria-3.5",
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "antigravity-preview-latest",
]

# 规则生效前的旧世代：2.5 系列 / 3.0-preview / 3.1 系列 / embedding /
# 图像 / 专用模型，保持原生 prefill 行为（宁漏勿错）。
UNRESTRICTED_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-image",
    "gemini-2.5-computer-use-preview-10-2025",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3-pro-image-preview",
    "gemini-3-pro-image",
    "gemini-nano-banana-2.1",
    "nano-banana-pro-preview",
    "gemini-omni-flash-preview",
    "gemini-omni-1.1-flash",
    "lyria-3-clip-preview",
    "lyria-3-pro-preview",
    "gemini-robotics-er-2-preview",
    "antigravity-preview-05-2026",
    "antigravity-preview-09-2026",
    "deep-research-max-preview-04-2026",
    "deep-research-preview-04-2026",
    "deep-research-pro-preview-12-2025",
    "gemini-embedding-001",
    "gemini-embedding-2-preview",
    "gemini-embedding-2",
]


class NeedsModelTurnFixTests(unittest.TestCase):
    """needs_model_turn_fix 的世代启发式判断。"""

    def setUp(self):
        self.mm = load_real_model_mapping()

    def test_restricted_models_need_fix(self):
        for model in RESTRICTED_MODELS:
            self.assertTrue(
                self.mm.needs_model_turn_fix(model),
                msg=f"{model} 应被视为受限（需追加 user turn）",
            )

    def test_unrestricted_models_do_not_need_fix(self):
        for model in UNRESTRICTED_MODELS:
            self.assertFalse(
                self.mm.needs_model_turn_fix(model),
                msg=f"{model} 应保持原生 prefill 行为（不追加）",
            )

    def test_decorations_are_stripped(self):
        # models/ 前缀（Gemini 原生命名）与 -search 联网后缀（HAJIMI 注入）。
        self.assertTrue(self.mm.needs_model_turn_fix("models/gemini-3.5-flash"))
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-3.5-flash-search"))
        self.assertFalse(self.mm.needs_model_turn_fix("models/gemini-3.1-flash-lite"))
        self.assertFalse(self.mm.needs_model_turn_fix("gemini-2.5-pro-search"))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(self.mm.needs_model_turn_fix("  Gemini-3.5-Flash-Lite "))
        self.assertFalse(self.mm.needs_model_turn_fix("GEMINI-3.1-FLASH-LITE"))

    def test_empty_and_invalid_inputs(self):
        for bad in ("", None, "   ", "unknown-model", "gpt-4o"):
            self.assertFalse(self.mm.needs_model_turn_fix(bad))

    def test_extra_include_overrides_heuristic(self):
        # 白名单可点名未纳入启发式的专用模型。
        self.assertFalse(self.mm.needs_model_turn_fix("gemini-omni-1.1-flash"))
        self.assertTrue(
            self.mm.needs_model_turn_fix(
                "gemini-omni-1.1-flash", extra_include=["gemini-omni"]
            )
        )
        # 通配符白名单。
        self.assertTrue(
            self.mm.needs_model_turn_fix(
                "deep-research-preview-04-2026", extra_include=["deep-research-*"]
            )
        )

    def test_exclude_overrides_everything(self):
        # 黑名单优先级最高，可豁免受限世代模型。
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-3.5-flash-lite"))
        self.assertFalse(
            self.mm.needs_model_turn_fix(
                "gemini-3.5-flash-lite", exclude=["gemini-3.5"]
            )
        )
        # 黑名单压过白名单。
        self.assertFalse(
            self.mm.needs_model_turn_fix(
                "gemini-3.5-flash-lite",
                extra_include=["gemini-3.5"],
                exclude=["gemini-3.5-flash-lite"],
            )
        )

    def test_future_generations_are_covered(self):
        # 启发式天然覆盖未来命名（3.9 / 5.0 / 6.1 ...）。
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-3.9-flash"))
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-5-flash"))
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-6.1-pro"))
        self.assertFalse(self.mm.needs_model_turn_fix("gemini-3.4-flash"))


class ConvertMessagesPrefillFixTests(unittest.TestCase):
    """convert_messages 的 prefill 兼容补丁行为。"""

    def _convert(self, module, messages, model, **settings_overrides):
        """按需重载模块以应用 settings 覆盖，再执行转换。"""
        if settings_overrides:
            module, _ = load_gemini_module(fake_settings_attrs=settings_overrides)
        return module.GeminiClient.convert_messages(
            module.GeminiClient,
            messages,
            use_system_prompt=True,
            model=model,
        )

    def setUp(self):
        self.module, self.mm = load_gemini_module()

    def test_restricted_model_with_trailing_assistant_appends_user_turn(self):
        # 全场景 prefill：截断续写（回答被截断后继续输出）、填表、
        # 结构化前缀的请求都以 assistant 消息结尾。
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "介绍黑洞"},
            # 回答被截断，前端把已输出的半截回复作为 prefill 续写。
            {"role": "assistant", "content": "黑洞是一种密度极大的天体，其引力强到"},
        ]
        contents, _ = self._convert(
            self.module, messages, "gemini-3.5-flash-lite"
        )
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])
        # prefill 内容本体保持为倒数第二条 model turn（不被破坏）。
        self.assertEqual(contents[-2]["role"], "model")
        self.assertIn("密度极大", contents[-2]["parts"][0]["text"])

    def test_unrestricted_models_keep_native_prefill(self):
        messages = [
            {"role": "user", "content": "填表"},
            {"role": "assistant", "content": "表格开头"},
        ]
        for model in (
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-3.1-flash-lite",
        ):
            contents, _ = self._convert(self.module, messages, model)
            self.assertEqual(
                contents[-1]["role"],
                "model",
                msg=f"{model} 不应被追加 user turn",
            )
            self.assertEqual(len(contents), 2)

    def test_normal_conversation_ending_with_user_is_untouched(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
            {"role": "user", "content": "今天天气如何"},
        ]
        contents, _ = self._convert(self.module, messages, "gemini-3.5-flash-lite")
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": "今天天气如何"}])
        self.assertEqual(len(contents), 3)

    def test_disabled_switch_disables_fix(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        contents, _ = self._convert(
            self.module,
            messages,
            "gemini-3.5-flash-lite",
            PREFILL_FIX=False,
        )
        self.assertEqual(contents[-1]["role"], "model")

    def test_custom_prompt_is_used(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        contents, _ = self._convert(
            self.module,
            messages,
            "gemini-3.5-flash-lite",
            PREFILL_FIX_PROMPT="从你停下的地方继续，不要重复任何内容。",
        )
        self.assertEqual(
            contents[-1]["parts"],
            [{"text": "从你停下的地方继续，不要重复任何内容。"}],
        )

    def test_empty_prompt_falls_back_to_nonempty(self):
        # settings 层已有保底，这里验证 convert_messages 对空文本
        # 也不会发出违反官方 nonempty 要求的 turn（防御性回退）。
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        module, _ = load_gemini_module(
            fake_settings_attrs={"PREFILL_FIX_PROMPT": ""}
        )
        contents, _ = module.GeminiClient.convert_messages(
            module.GeminiClient,
            messages,
            use_system_prompt=True,
            model="gemini-3.5-flash-lite",
        )
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])

    def test_missing_settings_attrs_fall_back_gracefully(self):
        # 旧测试桩 / 旧持久化快照缺失新增配置键时不崩溃（getattr
        # 防御性读取），并按默认值（启用 + 默认文本）工作。
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        module, _ = load_gemini_module(
            fake_settings_attrs={
                "PREFILL_FIX_PROMPT": None,
                "PREFILL_FIX_MODELS": None,
                "PREFILL_FIX_EXCLUDE_MODELS": None,
            }
        )
        # 删掉 PREFILL_FIX 键本身，模拟完全没有该键的旧桩。
        import app.config.settings as fake_settings_mod
        delattr(fake_settings_mod, "PREFILL_FIX")
        contents, _ = module.GeminiClient.convert_messages(
            module.GeminiClient,
            messages,
            use_system_prompt=True,
            model="gemini-3.5-flash-lite",
        )
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])

    def test_include_exclude_settings_flow_through(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        # 白名单点名 omni（启发式默认不覆盖）。
        contents, _ = self._convert(
            self.module,
            messages,
            "gemini-omni-1.1-flash",
            PREFILL_FIX_MODELS=["gemini-omni"],
        )
        self.assertEqual(contents[-1]["role"], "user")

        # 黑名单豁免 3.5。
        contents, _ = self._convert(
            self.module,
            messages,
            "gemini-3.5-flash-lite",
            PREFILL_FIX_EXCLUDE_MODELS=["gemini-3.5"],
        )
        self.assertEqual(contents[-1]["role"], "model")

    def test_search_suffix_model_still_fixed(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        contents, _ = self._convert(
            self.module, messages, "gemini-3.5-flash-search"
        )
        self.assertEqual(contents[-1]["role"], "user")

    def test_random_string_injection_keeps_fix_at_the_end(self):
        # RANDOM_STRING 的第二条注入在 len-1（末尾 model turn 之前），
        # 补丁必须在其后执行，保证追加的 user turn 位于绝对末尾。
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        module, _ = load_gemini_module(
            fake_settings_attrs={"RANDOM_STRING": True, "RANDOM_STRING_LENGTH": 5}
        )
        contents, _ = self._convert(module, messages, "gemini-3.6-flash")
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])
        self.assertEqual(contents[-2]["role"], "model")
        # 随机串注入在 model turn 之前（len-1 处，相对注入时点）。
        self.assertEqual(contents[-3]["role"], "user")

    def test_empty_history_does_not_crash(self):
        contents, _ = self._convert(self.module, [], "gemini-3.5-flash-lite")
        self.assertEqual(contents, [])

    def test_trailing_assistant_tool_calls_also_fixed(self):
        # functionCall parts 的 model turn 结尾同样命中补丁。
        messages = [
            {"role": "user", "content": "查天气"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_abc123def456ghi789jklmno",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Beijing"}',
                        },
                    }
                ],
            },
        ]
        contents, _ = self._convert(self.module, messages, "gemini-3.7-flash")
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])

    def test_no_system_prompt_mode_still_fixed(self):
        module, _ = load_gemini_module()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prefill"},
        ]
        contents, _ = module.GeminiClient.convert_messages(
            module.GeminiClient,
            messages,
            use_system_prompt=False,
            model="gemini-4-argon",
        )
        self.assertEqual(contents[-1]["role"], "user")


class LastNonemptyTurnRoleTests(unittest.TestCase):
    """共享谓词：Google 校验的是「最后一个非空 turn」的角色。"""

    def setUp(self):
        self.mm = load_real_model_mapping()

    def test_regular_roles(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "prefill"}]},
        ]
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "model")

    def test_empty_parts_turn_is_skipped(self):
        # 末尾的空 model turn（parts 缺失/为空）不算最后非空 turn。
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": []},
        ]
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "user")
        contents[-1] = {"role": "model"}
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "user")

    def test_whitespace_text_turn_is_skipped(self):
        contents = [
            {"role": "model", "parts": [{"text": "prefill"}]},
            {"role": "user", "parts": [{"text": "   "}]},
        ]
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "model")

    def test_inline_data_part_counts_as_nonempty(self):
        # 图片 part（无 text）是实际内容，算非空 turn。
        contents = [
            {"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": "x"}}]},
        ]
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "user")

    def test_empty_and_invalid_inputs(self):
        self.assertEqual(self.mm.last_nonempty_turn_role([]), "")
        self.assertEqual(self.mm.last_nonempty_turn_role(None), "")
        self.assertEqual(self.mm.last_nonempty_turn_role("not-a-list"), "")
        self.assertEqual(
            self.mm.last_nonempty_turn_role([{"role": "model", "parts": []}]),
            "",
        )

    def test_object_form_reads_role_attribute(self):
        # google-genai 的 Content 对象走 getattr(role)。
        from types import SimpleNamespace

        contents = [SimpleNamespace(role="model")]
        self.assertEqual(self.mm.last_nonempty_turn_role(contents), "model")

    def test_build_prefill_user_turn_structure(self):
        turn = self.mm.build_prefill_user_turn("继续")
        self.assertEqual(turn["role"], "user")
        self.assertEqual(turn["parts"], [{"text": "继续"}])


class _FakeGeminiPayload:
    """原生 Gemini 请求 payload 的最小替代（只需 model_dump）。"""

    def __init__(self, contents):
        self._contents = contents

    def model_dump(self, exclude_none=True, by_alias=True):
        return {"contents": self._contents}


class _FakeNativeRequest:
    """原生 Gemini 协议请求对象的最小替代。"""

    def __init__(self, model, contents, api_version=None):
        self.model = model
        self.format_type = "gemini"
        self.api_version = api_version
        self.payload = _FakeGeminiPayload(contents)


class NativeGeminiPrefillFixTests(unittest.TestCase):
    """原生 Gemini 协议路径（payload 直接透传）的 prefill 补丁。

    该路径不经 convert_messages，补丁挂在 _convert_request_data 的
    sanitize（assistant→model 归一）之后。"""

    def _convert(self, module, request, **settings_overrides):
        if settings_overrides:
            module, _ = load_gemini_module(fake_settings_attrs=settings_overrides)
        return module.GeminiClient("fake-key")._convert_request_data(
            request, [], [], None
        )

    def setUp(self):
        self.module, self.mm = load_gemini_module()

    def test_native_model_turn_tail_appends_user(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "介绍黑洞"}]},
                {"role": "model", "parts": [{"text": "黑洞是一种"}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        contents = data["contents"]
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])
        self.assertEqual(contents[-2]["role"], "model")

    def test_native_assistant_tail_normalized_then_fixed(self):
        # 客户端直发 role=assistant（非标准但 sanitize 会归一为 model）
        # —— 补丁必须识别归一化后的 model 结尾（验证 sanitize 后执行）。
        request = _FakeNativeRequest(
            "gemini-4-argon",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "assistant", "parts": [{"text": "prefill"}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        contents = data["contents"]
        self.assertEqual(contents[-2]["role"], "model")
        self.assertEqual(contents[-1]["role"], "user")

    def test_native_unrestricted_model_untouched(self):
        request = _FakeNativeRequest(
            "gemini-2.5-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "prefill"}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        self.assertEqual(len(data["contents"]), 2)
        self.assertEqual(data["contents"][-1]["role"], "model")

    def test_native_user_turn_tail_untouched(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "resp"}]},
                {"role": "user", "parts": [{"text": "next"}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        self.assertEqual(len(data["contents"]), 3)
        self.assertEqual(data["contents"][-1]["parts"], [{"text": "next"}])

    def test_native_empty_tail_turn_is_ignored(self):
        # 末尾空 model turn + 倒数第二 user：Google 看到的最后非空
        # turn 是 user，不会 400，不应追加。
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": []},
            ],
        )
        _, _, data = self._convert(self.module, request)
        self.assertEqual(len(data["contents"]), 2)

    def test_native_model_behind_empty_user_tail_still_fixed(self):
        # 末尾空 user turn + 倒数第二 model turn：Google 看到的最后
        # 非空 turn 是 model（会 400），应追加。
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "model", "parts": [{"text": "prefill"}]},
                {"role": "user", "parts": [{"text": "   "}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        contents = data["contents"]
        self.assertEqual(contents[-1]["role"], "user")
        self.assertEqual(contents[-1]["parts"], [{"text": DEFAULT_PROMPT}])

    def test_native_disabled_switch_and_custom_prompt(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p"}]},
            ],
        )
        _, _, data = self._convert(self.module, request, PREFILL_FIX=False)
        self.assertEqual(data["contents"][-1]["role"], "model")

        _, _, data = self._convert(
            self.module, request, PREFILL_FIX_PROMPT="从停下的地方继续"
        )
        self.assertEqual(
            data["contents"][-1]["parts"], [{"text": "从停下的地方继续"}]
        )

    def test_native_exclude_models_flow_through(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p"}]},
            ],
        )
        _, _, data = self._convert(
            self.module, request, PREFILL_FIX_EXCLUDE_MODELS=["gemini-3.8-flash"]
        )
        self.assertEqual(data["contents"][-1]["role"], "model")

    def test_native_search_suffix_still_fixed(self):
        # needs_model_turn_fix 内部自行剥离 -search 装饰，后缀模型
        # 同样命中补丁（联网剥离与否不影响判断）。
        request = _FakeNativeRequest(
            "gemini-3.8-flash-search",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p"}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        self.assertEqual(data["contents"][-1]["role"], "user")
        self.assertEqual(data["contents"][-1]["parts"], [{"text": DEFAULT_PROMPT}])


if __name__ == "__main__":
    unittest.main()
