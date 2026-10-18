"""最高思考强度覆盖策略单元测试（app/utils/thinking.py + 接入点）。

覆盖三层：
1. 模型家族判定与「最高档位」映射 —— 对项目实际拉取到的全部
   Gemini 模型做表驱动断言（2026-10 官方文档口径）；
2. 覆盖优先级与值换算 —— 按模型指定等级 > 全局最高档开关 > 请求透传；
   等级/budget/off/dynamic 在不同家族上的换算与防呆；
3. 接入点 —— apply_thinking_override（Gemini REST，camelCase）、
   apply_thinking_override_genai（Vertex google-genai SDK，snake_case）、
   sanitize_thinking_for_model（等级制模型 budget→level 防呆净化）、
   GeminiClient._convert_request_data（OpenAI 转换路径与原生 Gemini 路径
   的端到端最终 payload）。

工程约束（重要）：本文件不在模块级 import 任何真实 app.* 模块。
原因有二：
1. `import a.b as x` 的绑定走父模块属性链（IMPORT_FROM）——若在
   收集阶段注册真实 app/app.config（及其属性绑定），其他测试文件
   （test_gemini_schema 等）随后注入的假 app.config.settings 会被
   真实父模块的陈旧属性遮蔽，导致其拿到真实 settings 而失败；
2. test_protocol_adapters 等文件在模块级向 sys.modules 注入假
   app.* 桩（如无内容的 app.utils.sse），会作为整个会话的基线存续，
   运行期直接 import 真实 app.services.gemini 会撞上假桩 ImportError。
故所有真实导入都在测试内（setUp）延迟进行，且导入前先清空 app.*
 条目；conftest._isolate_sys_modules 会在每个测试结束后恢复基线。

官方依据：
  - ai.google.dev/gemini-api/docs/thinking（Interactions API 版）
  - firebase.google.com/docs/ai-logic/thinking（模型兼容表）：
    2.5 仅 budget（Pro 128-32768 不可关，Flash 0/1/512-24576），
    3.x 等级制（budget 向后兼容），4.x 仅等级（budget 400），
    TTS/Embedding/Lyria/Transcribe 等不支持思考。
"""

import sys
import unittest


# 首次真实导入后的模块快照（仅 app.* 条目）。跨测试缓存已执行的
# 真实模块对象，避免每个用例重复付出整条依赖链的导入开销；回放时
# 仅操作 sys.modules 映射，模块对象本身无状态（thinking 动态读
# settings，settings 由基类逐用例快照/恢复）。
_REAL_APP_SNAPSHOT = {}


def _fresh_real_modules():
    """确保 sys.modules 中是真实 app.* 链，返回 (settings, thinking)。

    1. 先清空当前 app.* 条目——绕开其他测试文件模块级注入的假桩
       （如无内容的 app.utils.sse）；
    2. 若此前已真实导入过（_REAL_APP_SNAPSHOT），直接回放缓存，
       否则执行首次导入并缓存；
    3. conftest._isolate_sys_modules 会在每个测试结束后恢复基线，
       缓存里持有的模块对象不受影响，可安全复用。
    """
    for name in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
        sys.modules.pop(name, None)

    if _REAL_APP_SNAPSHOT:
        sys.modules.update(_REAL_APP_SNAPSHOT)
    else:
        import app.config.settings as settings_module
        import app.utils.thinking as thinking_module

        _REAL_APP_SNAPSHOT.update(
            {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        )
        return settings_module, thinking_module

    return sys.modules["app.config.settings"], sys.modules["app.utils.thinking"]


class ThinkingOverrideTestBase(unittest.TestCase):
    """统一的「真实模块延迟导入 + settings 属性快照/恢复」。"""

    def setUp(self):
        self.settings, self.thinking = _fresh_real_modules()
        self.resolve = self.thinking.resolve_thinking_override
        self.apply = self.thinking.apply_thinking_override
        self.apply_genai = self.thinking.apply_thinking_override_genai

        self._saved = {
            "ENABLE_THINKING": self.settings.ENABLE_THINKING,
            "MAX_THINKING": self.settings.MAX_THINKING,
            "MAX_THINKING_MODEL_LEVELS": self.settings.MAX_THINKING_MODEL_LEVELS,
        }
        # 每个用例从「默认开启」的干净状态出发
        self.settings.ENABLE_THINKING = True
        self.settings.MAX_THINKING = True
        self.settings.MAX_THINKING_MODEL_LEVELS = {}

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(self.settings, key, value)


class TestMaxThinkingPerModel(ThinkingOverrideTestBase):
    """全局最高档开关开启时，项目模型列表中每个模型的期望行为。"""

    # 模型 → 期望的强制 thinkingConfig（{} 表示不附加任何思考配置）
    EXPECTED = {
        # --- 2.5 系列（budget 制） ---
        "gemini-2.5-flash": {"thinkingBudget": 24576},
        "gemini-2.5-pro": {"thinkingBudget": 32768},
        "gemini-2.5-flash-lite": {"thinkingBudget": 24576},
        "gemini-2.5-flash-preview-tts": {},
        "gemini-2.5-pro-preview-tts": {},
        "gemini-2.5-flash-image": {},  # 2.5 图像模型不支持思考
        # --- Gemma 开源系列：不接受思考配置 ---
        "gemma-4-26b-a4b-it": {},
        "gemma-4-31b-it": {},
        # --- 官方别名（指向新世代）：等级制 ---
        "gemini-flash-latest": {"thinkingLevel": "high"},
        "gemini-flash-lite-latest": {"thinkingLevel": "high"},
        "gemini-pro-latest": {"thinkingLevel": "high"},
        # --- 3.x 系列（等级制） ---
        "gemini-3-flash-preview": {"thinkingLevel": "high"},
        "gemini-3.1-pro-preview": {"thinkingLevel": "high"},
        "gemini-3.1-pro-preview-customtools": {"thinkingLevel": "high"},
        "gemini-3.1-flash-lite-preview": {"thinkingLevel": "high"},
        "gemini-3.1-flash-lite": {"thinkingLevel": "high"},
        "gemini-3.5-flash": {"thinkingLevel": "high"},
        "gemini-3.5-flash-lite": {"thinkingLevel": "high"},
        "gemini-3.6-flash": {"thinkingLevel": "high"},
        "gemini-3.7-flash": {"thinkingLevel": "high"},
        "gemini-3.8-flash": {"thinkingLevel": "high"},
        # --- 3.x 图像（nano banana 家族）：支持等级 ---
        "gemini-3-pro-image-preview": {"thinkingLevel": "high"},
        "gemini-3-pro-image": {"thinkingLevel": "high"},
        "nano-banana-pro-preview": {"thinkingLevel": "high"},
        "gemini-3.1-flash-image-preview": {"thinkingLevel": "high"},
        "gemini-3.1-flash-image": {"thinkingLevel": "high"},
        "gemini-3.1-flash-lite-image": {"thinkingLevel": "high"},
        "gemini-nano-banana-2.1": {"thinkingLevel": "high"},
        # --- 4.x（仅等级，budget 会 400） ---
        "gemini-4-argon": {"thinkingLevel": "high"},
        # --- 实时/转录/音乐/嵌入/代理类：不支持思考 ---
        "gemini-omni-flash-preview": {},
        "gemini-omni-1.1-flash": {},
        "gemini-3.5-transcribe": {},
        "lyria-3-clip-preview": {},
        "lyria-3-pro-preview": {},
        "lyria-3.5": {},
        "gemini-3.1-flash-tts-preview": {},
        "gemini-3.8-flash-tts": {},
        "gemini-3.8-flash-lite-tts": {},
        "gemini-2.5-computer-use-preview-10-2025": {},
        "antigravity-preview-05-2026": {},
        "antigravity-preview-09-2026": {},
        "antigravity-preview-latest": {},
        "deep-research-max-preview-04-2026": {},
        "deep-research-preview-04-2026": {},
        "deep-research-pro-preview-12-2025": {},
        "gemini-embedding-001": {},
        "gemini-embedding-2-preview": {},
        "gemini-embedding-2": {},
        "aqa": {},
        # --- 具身推理：支持等级 ---
        "gemini-robotics-er-2-preview": {"thinkingLevel": "high"},
        # --- 旧世代防呆 ---
        "gemini-2.0-flash": {},  # 2.0 不支持 thinkingBudget
        "gemini-1.5-pro": {},
    }

    def test_max_config_for_all_pulled_models(self):
        for model, expected in self.EXPECTED.items():
            with self.subTest(model=model):
                self.assertEqual(self.resolve(model), expected)

    def test_4x_never_receives_budget(self):
        """4.x 模型绝不携带 thinkingBudget（官方：会返回 400）。"""
        for model in ("gemini-4-argon", "gemini-flash-latest"):
            cfg = self.resolve(model)
            self.assertNotIn("thinkingBudget", cfg)
            self.assertEqual(cfg.get("thinkingLevel"), "high")


class TestPriority(ThinkingOverrideTestBase):
    """优先级：按模型指定等级 > 全局最高档开关 > 请求参数透传。"""

    def test_model_spec_beats_global_max(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "low"}
        self.assertEqual(
            self.resolve("gemini-3.8-flash"),
            {"thinkingLevel": "low"},
        )

    def test_exact_match_beats_prefix(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {
            "gemini-2.5-flash": 4096,
            "gemini-2.5": 1024,
        }
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 4096},
        )
        # 未精确命中的模型落到最长前缀
        self.assertEqual(
            self.resolve("gemini-2.5-flash-preview-09-2026"),
            {"thinkingBudget": 4096},
        )

    def test_longest_prefix_wins(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {
            "gemini-3": "low",
            "gemini-3.8": "medium",
        }
        self.assertEqual(
            self.resolve("gemini-3.8-flash"),
            {"thinkingLevel": "medium"},
        )
        self.assertEqual(
            self.resolve("gemini-3.5-flash"),
            {"thinkingLevel": "low"},
        )

    def test_spec_is_case_insensitive(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"Gemini-3.8-Flash": "HIGH"}
        self.assertEqual(
            self.resolve("GEMINI-3.8-FLASH"),
            {"thinkingLevel": "high"},
        )

    def test_global_off_without_spec_passthrough(self):
        self.settings.MAX_THINKING = False
        self.assertIsNone(self.resolve("gemini-3.8-flash"))
        self.assertIsNone(self.resolve("gemini-2.5-pro"))

    def test_master_gate_disables_everything(self):
        """ENABLE_THINKING 总闸关闭：即使模型指定了等级也完全不干预。"""
        self.settings.ENABLE_THINKING = False
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "low"}
        self.assertIsNone(self.resolve("gemini-3.8-flash"))

    def test_invalid_spec_falls_back_to_global_max(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "bogus-value"}
        self.assertEqual(
            self.resolve("gemini-3.8-flash"),
            {"thinkingLevel": "high"},
        )

    def test_boolean_spec_rejected(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": True}
        # 布尔值非法：回退全局最高档
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 24576},
        )

    def test_non_thinking_model_spec_is_ignored(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash-tts": "high"}
        self.assertEqual(self.resolve("gemini-3.8-flash-tts"), {})


class TestSpecConversion(ThinkingOverrideTestBase):
    """按模型指定的等级值在不同家族上的换算。"""

    def test_level_on_budget_family(self):
        # 2.5 Flash 系：等级换算为 budget
        cases = {
            "gemini-2.5-flash": {
                "low": 1024,
                "medium": 8192,
                "high": 24576,
                "minimal": 0,
            },
            "gemini-2.5-pro": {
                "low": 1024,
                "medium": 8192,
                "high": 32768,
                "minimal": 128,
            },
        }
        for model, mapping in cases.items():
            for level, budget in mapping.items():
                self.settings.MAX_THINKING_MODEL_LEVELS = {model: level}
                with self.subTest(model=model, level=level):
                    self.assertEqual(
                        self.resolve(model),
                        {"thinkingBudget": budget},
                    )

    def test_budget_spec_on_25_family(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": 16384}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 16384},
        )
        # 数字字符串同样接受
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": "16384"}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 16384},
        )

    def test_budget_clamped(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": 999999}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 24576},
        )
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-pro": 1}
        # Pro 下限 128（思考不可关闭）
        self.assertEqual(
            self.resolve("gemini-2.5-pro"),
            {"thinkingBudget": 128},
        )

    def test_budget_spec_translated_to_level_on_modern_models(self):
        """3.x/4.x 上的数字按区间换算为等级，绝不直接发送 budget。"""
        cases = [
            ("gemini-3.8-flash", 1024, "low"),
            ("gemini-3.8-flash", 8192, "medium"),
            ("gemini-3.8-flash", 24576, "high"),
            ("gemini-4-argon", 30000, "high"),  # 4.x 绝不能收到 budget
        ]
        for model, budget, level in cases:
            self.settings.MAX_THINKING_MODEL_LEVELS = {model: budget}
            with self.subTest(model=model, budget=budget):
                self.assertEqual(
                    self.resolve(model),
                    {"thinkingLevel": level},
                )

    def test_off_semantics(self):
        # 2.5 Flash：off → budget 0（真关闭）
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": "off"}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": 0},
        )
        # 2.5 Pro：不可关闭 → 下限 128
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-pro": "off"}
        self.assertEqual(
            self.resolve("gemini-2.5-pro"),
            {"thinkingBudget": 128},
        )
        # 3.x/4.x：不可关闭 → 不附加配置（模型默认动态思考）
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "off"}
        self.assertEqual(self.resolve("gemini-3.8-flash"), {})

    def test_dynamic_semantics(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": "dynamic"}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": -1},
        )
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-2.5-flash": -1}
        self.assertEqual(
            self.resolve("gemini-2.5-flash"),
            {"thinkingBudget": -1},
        )
        # 等级制模型本就动态思考 → 不附加
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.5-flash": "dynamic"}
        self.assertEqual(self.resolve("gemini-3.5-flash"), {})

    def test_zero_on_modern_models_is_default(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": 0}
        self.assertEqual(self.resolve("gemini-3.8-flash"), {})


class TestApplyRestPayload(ThinkingOverrideTestBase):
    """apply_thinking_override：Gemini REST payload（camelCase）。"""

    def test_override_replaces_existing_thinking_config(self):
        data = {
            "contents": [],
            "generationConfig": {
                "temperature": 0.7,
                "thinkingConfig": {"thinkingBudget": 128},
            },
        }
        self.apply("gemini-3.8-flash", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high"},
        )
        # 其余生成参数不受影响
        self.assertEqual(data["generationConfig"]["temperature"], 0.7)

    def test_preserves_include_thoughts(self):
        data = {
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 128, "includeThoughts": True},
            },
        }
        self.apply("gemini-3.8-flash", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )
        # snake_case 写法同样保留
        data2 = {
            "generationConfig": {
                "thinkingConfig": {"include_thoughts": True},
            },
        }
        self.apply("gemini-2.5-flash", data2)
        self.assertEqual(
            data2["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 24576, "includeThoughts": True},
        )

    def test_creates_generation_config_when_missing(self):
        data = {"contents": []}
        self.apply("gemini-2.5-pro", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 32768},
        )

    def test_off_removes_thinking_config(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "off"}
        data = {
            "generationConfig": {
                "temperature": 1.0,
                "thinkingConfig": {"thinkingBudget": 128},
            },
        }
        self.apply("gemini-3.8-flash", data)
        self.assertNotIn("thinkingConfig", data["generationConfig"])
        self.assertEqual(data["generationConfig"]["temperature"], 1.0)

    def test_passthrough_untouched(self):
        self.settings.MAX_THINKING = False
        data = {
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 128}},
        }
        self.apply("gemini-3.8-flash", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 128},
        )

    def test_skip_model_leaves_no_generation_config(self):
        """不支持思考的模型 + 全局开启：不新增空 generationConfig。"""
        data = {"contents": []}
        self.apply("gemini-3.8-flash-tts", data)
        self.assertNotIn("generationConfig", data)


class TestApplyGenaiConfig(ThinkingOverrideTestBase):
    """apply_thinking_override_genai：Vertex google-genai SDK（snake_case）。"""

    def test_snake_case_output(self):
        generation_config = {"temperature": 0.7}
        self.apply_genai("gemini-4-argon", generation_config)
        self.assertEqual(
            generation_config["thinking_config"],
            {"thinking_level": "high"},
        )
        self.assertEqual(generation_config["temperature"], 0.7)

    def test_budget_snake_case(self):
        generation_config = {}
        self.apply_genai("gemini-2.5-pro", generation_config)
        self.assertEqual(
            generation_config["thinking_config"],
            {"thinking_budget": 32768},
        )

    def test_passthrough(self):
        self.settings.MAX_THINKING = False
        generation_config = {}
        self.apply_genai("gemini-2.5-pro", generation_config)
        self.assertNotIn("thinking_config", generation_config)


class TestBudgetSanitize(ThinkingOverrideTestBase):
    """sanitize_thinking_for_model：等级制模型（3.x/4.x）budget → level 防呆。

    场景：MAX_THINKING 关闭（覆盖不干预）时，OpenAI 路径合成的
    thinkingBudget（reasoning_effort / thinking_budget 换算）或客户端
    直发的 budget 到达 4.x 会 400 —— 净化层就近换算为等级。
    """

    def setUp(self):
        super().setUp()
        self.sanitize = self.thinking.sanitize_thinking_for_model
        # 净化层独立于覆盖开关：关闭 MAX_THINKING 才是它真正的用武之地
        self.settings.MAX_THINKING = False

    def _payload(self, thinking_config, model="gemini-4-argon"):
        return {
            "contents": [],
            "generationConfig": {"thinkingConfig": thinking_config}
            if thinking_config is not None
            else {"temperature": 0.5},
        }

    def test_positive_budget_converted_by_tier(self):
        """正数 budget 按区间换算：8192 → medium，24576 → high。"""
        data = self._payload({"thinkingBudget": 8192})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"}
        )
        data = self._payload({"thinkingBudget": 24576})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "high"}
        )

    def test_small_budget_maps_to_low(self):
        data = self._payload({"thinkingBudget": 1})
        self.sanitize("gemini-3.8-flash", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"}
        )

    def test_zero_budget_removed_entirely(self):
        """budget=0（Claude 协议关闭思考的换算产物）：丢弃后配置变空则整体移除。"""
        data = self._payload({"thinkingBudget": 0})
        self.sanitize("gemini-3.8-flash", data)
        self.assertNotIn("thinkingConfig", data["generationConfig"])
        # -1（动态）同理
        data = self._payload({"thinkingBudget": -1})
        self.sanitize("gemini-4-argon", data)
        self.assertNotIn("thinkingConfig", data["generationConfig"])

    def test_explicit_level_wins_over_budget(self):
        """payload 同时携带 level 与 budget：显式等级优先，仅丢弃 budget。"""
        data = self._payload({"thinkingLevel": "low", "thinkingBudget": 5000})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"}
        )

    def test_include_thoughts_preserved(self):
        data = self._payload({"thinkingBudget": 24576, "includeThoughts": True})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )

    def test_other_generation_config_keys_untouched(self):
        data = {
            "contents": [],
            "generationConfig": {
                "temperature": 0.5,
                "thinkingConfig": {"thinkingBudget": 2048},
            },
        }
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(data["generationConfig"]["temperature"], 0.5)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"}
        )

    def test_master_gate_disables_sanitize(self):
        """总闸关闭 = 完全透明：budget 原样保留（即使注定 400）。"""
        self.settings.ENABLE_THINKING = False
        data = self._payload({"thinkingBudget": 8192})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingBudget": 8192}
        )

    def test_budget_family_untouched(self):
        """2.5 系 budget 家族：budget 是合法参数，不做任何净化。"""
        data = self._payload({"thinkingBudget": 100}, model="gemini-2.5-flash")
        self.sanitize("gemini-2.5-flash", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingBudget": 100}
        )

    def test_non_thinking_model_untouched(self):
        """不支持思考的模型：净化层不接管（客户端自负其责）。"""
        data = self._payload({"thinkingBudget": 100}, model="gemini-3.8-flash-tts")
        self.sanitize("gemini-3.8-flash-tts", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingBudget": 100}
        )

    def test_no_budget_noop(self):
        data = self._payload({"thinkingLevel": "medium"})
        self.sanitize("gemini-4-argon", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"}
        )

    def test_missing_generation_config_noop(self):
        data = {"contents": []}
        self.sanitize("gemini-4-argon", data)  # 不应抛异常
        self.assertEqual(data, {"contents": []})

    def test_latest_alias_also_sanitized(self):
        """-latest 别名指向新世代：同样净化。"""
        data = self._payload({"thinkingBudget": 8192})
        self.sanitize("gemini-pro-latest", data)
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"], {"thinkingLevel": "medium"}
        )


class TestGeminiClientIntegration(ThinkingOverrideTestBase):
    """GeminiClient._convert_request_data 端到端：最终发往 Google 的 payload。"""

    def setUp(self):
        super().setUp()
        # 与基类同源的清桩导入，保证 app.services.gemini 拿到真实依赖链
        import app.models.schemas as schemas_module
        import app.services.gemini as gemini_module

        # 合入快照：后续用例经 _fresh_real_modules 回放，免重复导入
        _REAL_APP_SNAPSHOT.update(
            {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
        )

        self.schemas = schemas_module
        self.gemini = gemini_module
        self.client = gemini_module.GeminiClient("fake-key")

    def test_openai_path_overrides_request_budget(self):
        """OpenAI 路径：请求里的 thinking_budget 被最高档覆盖。"""
        request = self.schemas.ChatCompletionRequest(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget=100,
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, model, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 24576},
        )

    def test_openai_path_passthrough_when_disabled(self):
        """关闭最高档后：请求参数原样透传（现有行为不变）。"""
        self.settings.MAX_THINKING = False
        request = self.schemas.ChatCompletionRequest(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget=100,
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 100},
        )

    def test_openai_path_reasoning_effort_passthrough(self):
        """关闭最高档后：reasoning_effort 映射逻辑保持不变。"""
        self.settings.MAX_THINKING = False
        request = self.schemas.ChatCompletionRequest(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="low",
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 1024},
        )

    def test_openai_path_budget_sanitized_on_4x(self):
        """关闭最高档后：4.x 上代理合成的 budget 仍会被净化为等级（防 400）。"""
        self.settings.MAX_THINKING = False
        request = self.schemas.ChatCompletionRequest(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget=8192,
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "medium"},
        )

    def test_openai_path_reasoning_effort_sanitized_on_4x(self):
        """关闭最高档后：reasoning_effort 合成的 budget 在 4.x 上换算为等级。"""
        self.settings.MAX_THINKING = False
        request = self.schemas.ChatCompletionRequest(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="low",
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )

    def test_openai_path_claude_disabled_thinking_on_4x(self):
        """Claude 协议关闭思考（thinking_budget=0）打到 4.x：净化为模型默认。"""
        self.settings.MAX_THINKING = False
        request = self.schemas.ChatCompletionRequest(
            model="gemini-4-argon",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget=0,
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        # 等级制模型思考不可关闭：丢弃 budget，thinkingConfig 变空整体移除
        self.assertNotIn("thinkingConfig", data["generationConfig"])

    def test_openai_path_expose_reasoning_preserved_with_override(self):
        """覆盖时保留请求的思考摘要回显偏好。"""
        request = self.schemas.ChatCompletionRequest(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget=100,
            expose_reasoning=True,
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )

    def test_native_gemini_path_override(self):
        """原生 Gemini 路径：客户端自带 thinkingConfig 被覆盖并保留摘要开关。"""
        request = self.schemas.AIRequest(
            payload=self.schemas.ChatRequestGemini(
                contents=[{"role": "user", "parts": [{"text": "hi"}]}],
                generationConfig={
                    "thinkingConfig": {
                        "thinkingBudget": 128,
                        "includeThoughts": True,
                    },
                    "temperature": 0.5,
                },
            ),
            model="gemini-3.8-flash",
            format_type="gemini",
        )
        api_version, model, data = self.client._convert_request_data(
            request, [], [], None
        )
        self.assertEqual(api_version, "v1beta")
        self.assertEqual(model, "gemini-3.8-flash")
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )
        self.assertEqual(data["generationConfig"]["temperature"], 0.5)

    def test_native_gemini_path_skip_non_thinking_model(self):
        request = self.schemas.AIRequest(
            payload=self.schemas.ChatRequestGemini(
                contents=[{"role": "user", "parts": [{"text": "hi"}]}],
            ),
            model="gemini-3.8-flash-tts",
            format_type="gemini",
        )
        _, _, data = self.client._convert_request_data(request, [], [], None)
        self.assertNotIn("generationConfig", data)

    def test_per_model_spec_through_client(self):
        self.settings.MAX_THINKING_MODEL_LEVELS = {"gemini-3.8-flash": "low"}
        request = self.schemas.ChatCompletionRequest(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hi"}],
        )
        contents = [{"role": "user", "parts": [{"text": "hi"}]}]
        _, _, data = self.client._convert_request_data(
            request, contents, [], None
        )
        self.assertEqual(
            data["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )


if __name__ == "__main__":
    unittest.main()
