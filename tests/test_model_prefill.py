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
抑制前言），补丁对所有这些场景统一生效；更早发布的
gemini-3.5-flash 原生支持 prefill，不受影响。
官方迁移指南：ai.google.dev/gemini-api/docs/generate-content/latest-model
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 与 model_mapping.DEFAULT_PREFILL_FIX_PROMPT / settings.PREFILL_FIX_PROMPT
# 默认值保持一致的通用续写指令（另有专门测试强制三处不漂移）。
DEFAULT_PROMPT = (
    "Continue the immediately preceding assistant response. "
    "Output only its continuation; do not repeat, explain, or restart it."
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


def load_real_prefill_echo_module():
    """直接从文件加载真实的 prefill_echo（gemini.py 的新依赖，仅依赖
    标准库 contextvars/typing，无 app.* 依赖链问题）。"""
    spec = importlib.util.spec_from_file_location(
        "prefill_prefill_echo", ROOT / "app/utils/prefill_echo.py"
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
        "PREFILL_FIX_STRIP_ECHO": True,
        "ENABLE_THINKING": False,
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

    # 真实的 prefill_echo（gemini.py 的回显剥离依赖）注册进包链。
    real_prefill_echo = load_real_prefill_echo_module()
    sys.modules["app.utils.prefill_echo"] = real_prefill_echo
    fake_utils.prefill_echo = real_prefill_echo

    # 拆分后的 gemini.py 从 app.services.gemini_payload 导入纯函数工具。
    # 注册带 __path__ 的假 "app.services" 包（跳过真实 __init__，避免
    # gemini.py 双重加载）；gemini_payload 零 app 依赖，可独立导入。
    fake_services = types.ModuleType("app.services")
    fake_services.__path__ = [str(ROOT / "app/services")]
    sys.modules["app.services"] = fake_services
    fake_app.services = fake_services

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

# 规则生效世代（2026-07-21 与 3.6 同期发布的 gemini-3.5-flash-lite
# 及此后所有模型；latest 指针指向当前最新世代；gemma-4 沿袭 Gemma
# "必须以 user turn 结尾"的既有约束）。注：同为 (3,5) 世代的
# gemini-3.5-flash / 3.5-transcribe / lyria-3.5 发布于规则生效前，
# 归入下方不受限名单。
RESTRICTED_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "antigravity-preview-latest",
]

# 规则生效前的旧世代：2.5 系列 / 3.0-preview / 3.1 系列 / embedding /
# 图像 / 专用模型，以及发布于规则生效前的 (3,5) 世代非 flash-lite
# 变体（gemini-3.5-flash 原生支持 prefill；3.5-transcribe / lyria-3.5
# 是转写 / 音乐专用模型，不参与 chat prefill），保持原生 prefill 行为
# （宁漏勿错）。
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
    "gemini-3.5-flash",
    "gemini-3.5-transcribe",
    "lyria-3.5",
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
        self.assertTrue(self.mm.needs_model_turn_fix("models/gemini-3.6-flash"))
        self.assertTrue(self.mm.needs_model_turn_fix("gemini-3.6-flash-search"))
        self.assertFalse(self.mm.needs_model_turn_fix("models/gemini-3.1-flash-lite"))
        self.assertFalse(self.mm.needs_model_turn_fix("gemini-2.5-pro-search"))
        # 3.5 世代分水岭：同世代的 3.5-flash 不受限（规则前发布）。
        self.assertFalse(self.mm.needs_model_turn_fix("models/gemini-3.5-flash"))
        self.assertFalse(self.mm.needs_model_turn_fix("gemini-3.5-flash-search"))

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
            self.module, messages, "gemini-3.6-flash-search"
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

    def test_trailing_assistant_tool_calls_left_as_is(self):
        # functionCall part 的 model turn 结尾是真实历史（待回复的
        # 工具调用），不是文本 prefill：保持原样让上游按官方语义
        # 校验，而非猜测改写（升级后的语义，对齐 vertex-master）。
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
        self.assertEqual(len(contents), 2)
        self.assertEqual(contents[-1]["role"], "model")
        self.assertIn("functionCall", contents[-1]["parts"][0])
        # 未命中 prefill 补丁 → ContextVar 应显式置空。
        self.assertIsNone(self.module.get_current_prefill_echo())

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

    def test_native_model_behind_empty_user_tail_uses_fallback(self):
        # 末尾空 user turn + 倒数第二 model turn：Google 看到的最后非空
        # turn 是 model（会 400）。prefill turn 之前没有 turn → 兑底：
        # 移除 model 轮，指令并入既有（空）user 轮，结尾合法。
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "model", "parts": [{"text": "prefill"}]},
                {"role": "user", "parts": [{"text": "   "}]},
            ],
        )
        _, _, data = self._convert(self.module, request)
        contents = data["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["role"], "user")
        self.assertEqual(contents[0]["parts"][0]["text"], "   ")
        self.assertTrue(
            contents[0]["parts"][1]["text"].startswith(
                self.mm.PREFILL_FALLBACK_INSTRUCTION_PREFIX
            )
        )

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


class ExtractTrailingModelPrefillTests(unittest.TestCase):
    """extract_trailing_model_prefill：末尾纯文本 model turn 识别。"""

    def setUp(self):
        self.mm = load_real_model_mapping()

    def test_plain_text_prefill_after_user(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "prefill"}]},
        ]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("prefill", "user", 1),
        )

    def test_multi_text_parts_concatenated(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "A"}, {"text": "B"}]},
        ]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("AB", "user", 1),
        )

    def test_model_only_history(self):
        contents = [{"role": "model", "parts": [{"text": "prefill"}]}]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("prefill", "", 0),
        )

    def test_consecutive_model_turns(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "resp"}]},
            {"role": "model", "parts": [{"text": "prefill"}]},
        ]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("prefill", "model", 2),
        )

    def test_trailing_empty_turn_is_skipped_for_role_detection(self):
        # 末尾空 user turn 之后藏着 model turn：最后一个非空 turn 是 model
        # （命中补丁）；prefill turn 之前没有 turn → 兑底路径（结构上
        # 必然合法：移除 model 轮后把指令并入既有 user 轮）。
        contents = [
            {"role": "model", "parts": [{"text": "prefill"}]},
            {"role": "user", "parts": [{"text": "   "}]},
        ]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("prefill", "", 0),
        )

    def test_explicit_thought_false_is_tolerated(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "p", "thought": False}]},
        ]
        self.assertEqual(
            self.mm.extract_trailing_model_prefill(contents),
            ("p", "user", 1),
        )

    def test_function_call_part_is_not_prefill(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {}}}]},
        ]
        self.assertIsNone(self.mm.extract_trailing_model_prefill(contents))

    def test_thought_part_is_not_prefill(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "t", "thought": True}]},
        ]
        self.assertIsNone(self.mm.extract_trailing_model_prefill(contents))

    def test_media_part_is_not_prefill(self):
        contents = [
            {"role": "user", "parts": [{"text": "hi"}]},
            {
                "role": "model",
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "x"}}],
            },
        ]
        self.assertIsNone(self.mm.extract_trailing_model_prefill(contents))

    def test_user_tail_is_not_prefill(self):
        contents = [
            {"role": "model", "parts": [{"text": "resp"}]},
            {"role": "user", "parts": [{"text": "next"}]},
        ]
        self.assertIsNone(self.mm.extract_trailing_model_prefill(contents))

    def test_empty_text_parts_is_none(self):
        contents = [{"role": "model", "parts": [{"text": ""}]}]
        self.assertIsNone(self.mm.extract_trailing_model_prefill(contents))

    def test_invalid_inputs(self):
        self.assertIsNone(self.mm.extract_trailing_model_prefill(None))
        self.assertIsNone(self.mm.extract_trailing_model_prefill([]))
        self.assertIsNone(self.mm.extract_trailing_model_prefill("no"))


class DegenerateHistoryFallbackTests(unittest.TestCase):
    """畸形历史（model-only / 连续 model）的 JSON 兜底改写。"""

    def setUp(self):
        self.module, self.mm = load_gemini_module()

    def _convert(self, module, messages, model):
        return module.GeminiClient.convert_messages(
            module.GeminiClient,
            messages,
            use_system_prompt=True,
            model=model,
        )

    def test_assistant_only_history_uses_json_fallback(self):
        # 只有 assistant 一条消息（model-only）：移除 model turn，prefill
        # 原文以 JSON 数据形式内嵌进新 user 指令。
        contents, _ = self._convert(
            self.module,
            [{"role": "assistant", "content": "黑洞是一种"}],
            "gemini-3.7-flash",
        )
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["role"], "user")
        text = contents[0]["parts"][0]["text"]
        self.assertTrue(text.startswith(self.mm.PREFILL_FALLBACK_INSTRUCTION_PREFIX))
        self.assertIn('"黑洞是一种"', text)  # json.dumps(ensure_ascii=False)
        # 兜底命中同样记录 prefill 原文（供响应侧回显剥离）。
        self.assertEqual(self.module.get_current_prefill_echo(), "黑洞是一种")

    def test_native_model_only_uses_json_fallback(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [{"role": "model", "parts": [{"text": "prefill"}]}],
        )
        _, _, data = self.module.GeminiClient("k")._convert_request_data(
            request, [], [], None
        )
        contents = data["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["role"], "user")
        self.assertTrue(
            contents[0]["parts"][0]["text"].startswith(
                self.mm.PREFILL_FALLBACK_INSTRUCTION_PREFIX
            )
        )

    def test_native_consecutive_model_appends_new_user_turn(self):
        # [user, model, model]：移除末尾 prefill 后结尾仍是 model →
        # 新建 user 指令轮（不能并入 model 轮）。
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "resp"}]},
                {"role": "model", "parts": [{"text": "prefill"}]},
            ],
        )
        _, _, data = self.module.GeminiClient("k")._convert_request_data(
            request, [], [], None
        )
        contents = data["contents"]
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertTrue(
            contents[-1]["parts"][0]["text"].startswith(
                self.mm.PREFILL_FALLBACK_INSTRUCTION_PREFIX
            )
        )

    def test_native_tool_call_tail_is_left_as_is(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "f", "args": {}}}],
                },
            ],
        )
        _, _, data = self.module.GeminiClient("k")._convert_request_data(
            request, [], [], None
        )
        self.assertEqual(len(data["contents"]), 2)
        self.assertEqual(data["contents"][-1]["role"], "model")
        self.assertIsNone(self.module.get_current_prefill_echo())

    def test_native_thought_part_tail_is_left_as_is(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "t", "thought": True}]},
            ],
        )
        _, _, data = self.module.GeminiClient("k")._convert_request_data(
            request, [], [], None
        )
        self.assertEqual(len(data["contents"]), 2)
        self.assertEqual(data["contents"][-1]["role"], "model")

    def test_native_explicit_thought_false_still_fixed(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p", "thought": False}]},
            ],
        )
        _, _, data = self.module.GeminiClient("k")._convert_request_data(
            request, [], [], None
        )
        self.assertEqual(data["contents"][-1]["role"], "user")
        self.assertEqual(data["contents"][-1]["parts"], [{"text": DEFAULT_PROMPT}])
        self.assertEqual(self.module.get_current_prefill_echo(), "p")


class PrefillEchoContextVarTests(unittest.TestCase):
    """请求侧 prefill 原文经 ContextVar 透传给响应侧。"""

    def setUp(self):
        self.module, self.mm = load_gemini_module()

    def test_fixed_request_records_prefill_text(self):
        self.module.GeminiClient.convert_messages(
            self.module.GeminiClient,
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "prefill"},
            ],
            use_system_prompt=True,
            model="gemini-3.6-flash",
        )
        self.assertEqual(self.module.get_current_prefill_echo(), "prefill")

    def test_unfixed_request_resets_to_none(self):
        # 未命中（旧模型）→ 显式置空；先命中再未命中也不残留。
        self.module.GeminiClient.convert_messages(
            self.module.GeminiClient,
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "prefill"},
            ],
            use_system_prompt=True,
            model="gemini-3.6-flash",
        )
        self.assertEqual(self.module.get_current_prefill_echo(), "prefill")
        self.module.GeminiClient.convert_messages(
            self.module.GeminiClient,
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "prefill"},
            ],
            use_system_prompt=True,
            model="gemini-2.5-flash",
        )
        self.assertIsNone(self.module.get_current_prefill_echo())

    def test_native_path_records_and_resets(self):
        request = _FakeNativeRequest(
            "gemini-3.8-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p"}]},
            ],
        )
        self.module.GeminiClient("k")._convert_request_data(request, [], [], None)
        self.assertEqual(self.module.get_current_prefill_echo(), "p")

        plain = _FakeNativeRequest(
            "gemini-2.5-flash",
            [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"text": "p"}]},
            ],
        )
        self.module.GeminiClient("k")._convert_request_data(plain, [], [], None)
        self.assertIsNone(self.module.get_current_prefill_echo())


class DefaultPromptConsistencyTests(unittest.TestCase):
    """三处默认续写指令不漂移（model_mapping / settings / 测试常量）。"""

    def test_defaults_are_consistent(self):
        mm = load_real_model_mapping()
        self.assertEqual(mm.DEFAULT_PREFILL_FIX_PROMPT, DEFAULT_PROMPT)
        settings_source = (ROOT / "app/config/settings.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Continue the immediately preceding assistant response", settings_source)
        self.assertIn("do not repeat, explain, or restart it", settings_source)


class _FakeStreamResponse:
    status_code = 200
    is_closed = True

    async def aread(self):
        return b""


class _FakeStreamContext:
    async def __aenter__(self):
        return _FakeStreamResponse()

    async def __aexit__(self, *exc):
        return False


class _FakeStreamingHttpClient:
    def stream(self, *args, **kwargs):
        return _FakeStreamContext()


class _FakePostResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakePostingHttpClient:
    def __init__(self, payload):
        response = _FakePostResponse()
        response.payload = payload
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response


class GeminiClientEchoStripTests(unittest.TestCase):
    """stream_chat / complete_chat 的响应侧回显剥离（端到端桩测试）。"""

    def setUp(self):
        self.module, self.mm = load_gemini_module()

    @staticmethod
    def _request(model="gemini-3.7-flash"):
        from types import SimpleNamespace

        return SimpleNamespace(
            model=model,
            format_type=None,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            stop=None,
            n=None,
            thinking_budget=None,
            reasoning_effort=None,
            tools=None,
            tool_choice=None,
        )

    def _run_stream(self, chunks, prefill, **settings_overrides):
        module = self.module
        if settings_overrides:
            module, _ = load_gemini_module(fake_settings_attrs=settings_overrides)

        async def _client_factory():
            return _FakeStreamingHttpClient()

        def _iter_sse(response):
            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

        module.get_async_client = _client_factory
        module.iter_sse_json = _iter_sse
        module.set_current_prefill_echo(prefill)

        client = module.GeminiClient("fake-key")

        async def _collect():
            return [w async for w in client.stream_chat(
                self._request(), [], [], None
            )]

        import asyncio

        return asyncio.run(_collect())

    def test_stream_full_echo_stripped(self):
        # 回显跨多个 chunk 完整出现后续写：剥离全部回显，仅留新内容。
        chunks = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "AB"}]}, "index": 0}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "CDEF"}]}, "index": 0}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "XYZ"}]}, "index": 0, "finishReason": "STOP"}]},
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")
        text = "".join(w.text for w in wrappers)
        self.assertEqual(text, "XYZ")

    def test_stream_no_echo_passes_through(self):
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "Q"}]}, "index": 0}]},
            {"candidates": [{"content": {"parts": [{"text": "R"}]}, "index": 0, "finishReason": "STOP"}]},
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")
        self.assertEqual("".join(w.text for w in wrappers), "QR")

    def test_stream_partial_echo_released_on_finish_frame(self):
        # 输出只匹配到前缀一半就结束（maxTokens 截断等）：按真实内容
        # 原样释放，绝不吞输出。
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "AB"}]}, "index": 0}]},
            {"candidates": [{"index": 0, "finishReason": "MAX_TOKENS"}]},
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")
        self.assertEqual("".join(w.text for w in wrappers), "AB")

    def test_stream_cut_without_finish_frame_releases_tail_chunk(self):
        # 流异常关闭（无 finishReason）：finalize 合成补发 chunk。
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "AB"}]}, "index": 0}]},
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")
        self.assertEqual("".join(w.text for w in wrappers), "AB")
        # 补发 chunk 是普通文本增量形态（无 finishReason）。
        self.assertEqual(wrappers[-1].data["candidates"][0].get("finishReason"), None)

    def test_stream_thought_parts_untouched(self):
        chunks = [
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "AB", "thought": True},
                                {"text": "AB"},
                            ],
                        },
                        "index": 0,
                    }
                ]
            },
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")
        parts = wrappers[0].data["candidates"][0]["content"]["parts"]
        self.assertEqual(parts[0]["text"], "AB")  # thought 原样保留
        self.assertEqual(parts[1]["text"], "")  # 普通文本被暂存
        self.assertEqual(wrappers[0].thoughts, "AB")

    def test_stream_strip_echo_disabled(self):
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "ABCDEF"}]}, "index": 0, "finishReason": "STOP"}]},
        ]
        wrappers = self._run_stream(chunks, "ABCDEF", PREFILL_FIX_STRIP_ECHO=False)
        self.assertEqual("".join(w.text for w in wrappers), "ABCDEF")

    def test_stream_multi_candidate_independent(self):
        # 双候选各自独立剥离（candidateCount>1），回显切分方式不同。
        chunks = [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "AB"}]}, "index": 0},
                    {"content": {"parts": [{"text": "ABCD"}]}, "index": 1},
                ]
            },
            {
                "candidates": [
                    {"content": {"parts": [{"text": "CDEF!"}]}, "index": 0, "finishReason": "STOP"},
                    {"content": {"parts": [{"text": "EF!"}]}, "index": 1, "finishReason": "STOP"},
                ]
            },
        ]
        wrappers = self._run_stream(chunks, "ABCDEF")

        def _text(data, index):
            parts = data["candidates"][index]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)

        # 两候选的回显在不同 chunk 边界完成，均被完整剥离。
        self.assertEqual(_text(wrappers[0].data, 0), "")
        self.assertEqual(_text(wrappers[0].data, 1), "")
        self.assertEqual(_text(wrappers[1].data, 0), "!")
        self.assertEqual(_text(wrappers[1].data, 1), "!")

    def test_complete_chat_strips_echo(self):
        import asyncio

        payload = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ABCDEF tail"}]},
                    "index": 0,
                    "finishReason": "STOP",
                }
            ]
        }

        async def _client_factory():
            return _FakePostingHttpClient(payload)

        self.module.get_async_client = _client_factory
        self.module.set_current_prefill_echo("ABCDEF")
        wrapper = asyncio.run(
            self.module.GeminiClient("k").complete_chat(
                self._request(), [], [], None
            )
        )
        self.assertEqual(wrapper.text, " tail")

    def test_complete_chat_partial_echo_kept(self):
        import asyncio

        payload = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "AB"}]},
                    "index": 0,
                    "finishReason": "STOP",
                }
            ]
        }

        async def _client_factory():
            return _FakePostingHttpClient(payload)

        self.module.get_async_client = _client_factory
        self.module.set_current_prefill_echo("ABCDEF")
        wrapper = asyncio.run(
            self.module.GeminiClient("k").complete_chat(
                self._request(), [], [], None
            )
        )
        # 只匹配一半的输出是真实内容：原样释放（追加为普通 part）。
        self.assertEqual(wrapper.text, "AB")


if __name__ == "__main__":
    unittest.main()
