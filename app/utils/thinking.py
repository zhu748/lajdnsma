"""思考强度覆盖策略（最高思考强度 / 按模型指定等级）。

依据（2026-10 官方文档）：
  - ai.google.dev/gemini-api/docs/thinking（Interactions API 版）
  - firebase.google.com/docs/ai-logic/thinking（模型兼容表）

模型能力速查（与本项目模型列表对齐）：

  家族                | 参数风格          | 最高档位
  --------------------+-------------------+--------------------------
  Gemini 2.5 Pro      | thinkingBudget    | 32768（不可关闭，>=128）
  Gemini 2.5 Flash(-Lite) | thinkingBudget | 24576（0=关闭，-1=动态）
  Gemini 3.x          | thinkingLevel     | "high"（budget 仅向后兼容）
  Gemini 4.x (argon)  | thinkingLevel     | "high"（budget 会 400！）
  nano-banana-2/2.1/pro（3.x 图像） | thinkingLevel | "high"
  gemini-*-latest 别名 | thinkingLevel     | "high"（指向新世代）
  robotics-er         | thinkingLevel     | "high"（具身推理，支持等级）
  TTS / Embedding / Lyria / Transcribe / Gemma / Omni /
  deep-research / antigravity / computer-use / aqa / 2.0 及更早
                     | 不支持思考        | 静默跳过（不附加配置）

覆盖优先级（与产品需求一致）：
    按模型指定等级（MAX_THINKING_MODEL_LEVELS）
      > 全局最高档开关（MAX_THINKING）
      > 请求自带参数（原样透传）

返回约定（resolve_thinking_override）：
    None → 无覆盖，按请求原样透传；
    {}   → 生效「不附加任何 thinkingConfig」（走模型默认思考）；
    dict → 强制使用的 thinkingConfig（camelCase，Gemini REST 格式）。

sanitize_thinking_for_model（防呆净化，见函数注释）：
    等级制模型（3.x/4.x）的 payload 中出现 thinkingBudget 时就近
    换算为 thinkingLevel —— 无论 budget 来自代理合成（OpenAI
    reasoning_effort / Claude thinking.budget_tokens 换算）还是
    客户端直发，4.x 携带 budget 都注定 400。

ENABLE_THINKING 是总闸：关闭时本模块一律返回 None（完全不干预，
保持项目原有"不注入"行为），sanitize 亦不生效（完全透明代理）。
"""

import re

import app.config.settings as settings
from app.utils.logging import log

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_VALID_LEVELS = ("minimal", "low", "medium", "high")

# 等级 → 2.5 系列 budget 换算（与 gemini.py 的 reasoning_effort
# 启发式映射保持一致：low≈1024 / medium≈8192 / high=24576）。
_LEVEL_TO_BUDGET = {"low": 1024, "medium": 8192, "high": 24576}

# "关闭思考"的用户写法
_OFF_VALUES = ("off", "none", "disable", "disabled")

# "动态思考"（仅 2.5 系列：thinkingBudget=-1，上限固定 8192）
_DYNAMIC_VALUES = ("dynamic", "auto")

# 等级制模型上数字 budget 的就近换算区间（避免向 4.x 发 budget 造成 400）
_BUDGET_LEVEL_TIERS = ((2048, "low"), (10240, "medium"))

# 明确不支持思考配置的模型关键词（命中即静默跳过，不附加任何 thinkingConfig）：
#   - TTS / Embedding / Lyria / Transcribe：官方明确不支持思考；
#   - Gemma：开源系列不接受 thinkingConfig；
#   - Omni：实时音视频（Live 系），支持情况未明确，保守跳过；
#   - deep-research / antigravity / computer-use：代理类模型，
#     思考过程不受 generateContent 的 thinkingConfig 控制；
#   - imagen / veo：媒体生成模型，不适用。
_NON_THINKING_KEYWORDS = (
    "-tts",
    "embedding",
    "lyria",
    "transcribe",
    "deep-research",
    "antigravity",
    "computer-use",
    "gemma",
    "omni",
    "imagen",
    "veo",
)

# 无效指定的哨兵（区别于 None=透传 与 {}=不附加）
_INVALID = object()


# ---------------------------------------------------------------------------
# 模型家族判定
# ---------------------------------------------------------------------------

def _major_version(model_lower: str):
    """提取 gemini 主版本号（如 gemini-3.8-flash → 3）；无版本返回 None。"""
    m = re.search(r"gemini-(\d+)", model_lower)
    return int(m.group(1)) if m else None


def _model_family(model_lower: str):
    """判定模型所属思考参数家族。

    返回：
      "level"        — 3.x / 4.x / 新世代（等级制，绝不发送 budget）
      "budget-pro"   — 2.5 Pro（budget 上限 32768，不可关闭）
      "budget-flash" — 2.5 Flash / Flash-Lite（budget 上限 24576，0=关闭）
      None           — 不支持思考（或无法识别），静默跳过
    """
    for kw in _NON_THINKING_KEYWORDS:
        if kw in model_lower:
            return None

    # nano-banana 家族（3.x 世代图像模型，支持 thinkingLevel）：
    #   nano-banana-pro* → Gemini 3 Pro Image（Nano Banana Pro）
    #   nano-banana-2*   → Gemini 3 Flash Image（Nano Banana 2 / 2.1）
    #   裸 nano-banana（无编号）→ gemini-2.5-flash-image，2.5 图像不支持思考。
    if "nano-banana" in model_lower:
        if "nano-banana-pro" in model_lower or "nano-banana-2" in model_lower:
            return "level"
        return None

    # robotics-er（具身推理）：官方标注支持 minimal~high 等级
    if "robotics-er" in model_lower:
        return "level"

    major = _major_version(model_lower)

    if major is None:
        # gemini-flash-latest / gemini-pro-latest 等官方别名：指向最新
        # 世代（3.x/4.x），走等级制（绝不发送 budget，防止指向 4.x 时 400）。
        if model_lower.startswith("gemini-") and model_lower.endswith("-latest"):
            return "level"
        return None

    if major >= 3:
        # 3.x / 4.x 及未来世代：等级制。
        # 注意 4.x 对 thinkingBudget 直接 400，3.x 虽向后兼容 budget，
        # 但统一走等级最安全、行为一致。
        return "level"

    if major == 2:
        # 仅 2.5 系列支持 thinkingBudget；2.0 及更早不支持任何思考参数
        if "gemini-2.5" not in model_lower:
            return None
        # 2.5 世代图像模型不支持思考参数（3.x 世代才支持）
        if "-image" in model_lower:
            return None
        if "-pro" in model_lower:
            return "budget-pro"
        return "budget-flash"

    return None


def _max_thinking_config(model_lower: str):
    """按模型家族返回「最高档位」thinkingConfig（camelCase）。

    不支持思考的模型返回 {}（空 dict：不附加任何配置）。
    """
    family = _model_family(model_lower)
    if family == "level":
        return {"thinkingLevel": "high"}
    if family == "budget-pro":
        return {"thinkingBudget": 32768}
    if family == "budget-flash":
        return {"thinkingBudget": 24576}
    return {}


# ---------------------------------------------------------------------------
# 按模型指定的等级值 → thinkingConfig
# ---------------------------------------------------------------------------

def _level_spec_on_budget_family(family: str, level: str):
    """等级制写法应用到 2.5 budget 家族时的换算。"""
    if level == "minimal":
        # minimal ≈ 尽量不思考：Flash 系可关闭（0），Pro 下限 128
        return {"thinkingBudget": 128} if family == "budget-pro" else {"thinkingBudget": 0}
    if level == "high":
        return {"thinkingBudget": 32768} if family == "budget-pro" else {"thinkingBudget": 24576}
    return {"thinkingBudget": _LEVEL_TO_BUDGET[level]}  # low / medium


def _budget_spec_on_level_family(budget: int):
    """数字 budget 应用到等级制（3.x/4.x）家族时的就近换算。

    3.x 虽向后兼容 budget，但 4.x 携带 budget 直接 400 —— 统一换算成
    等级，行为可预期且永不出错。
    """
    for threshold, level in _BUDGET_LEVEL_TIERS:
        if budget <= threshold:
            return {"thinkingLevel": level}
    return {"thinkingLevel": "high"}


def _spec_to_config(model_lower: str, spec):
    """把用户指定的等级值转换为 thinkingConfig。

    返回 _INVALID 表示值无效（记日志，回退到全局开关继续判定）。
    """
    family = _model_family(model_lower)

    if isinstance(spec, bool):
        # bool 是 int 的子类，先防呆拦截（true/false 不是合法等级）
        log("warning", f"思考等级指定值无效（布尔值 {spec!r}）：模型 {model_lower}")
        return _INVALID

    # 数字（或数字字符串）→ budget 语义
    if isinstance(spec, (int, float)) or (
        isinstance(spec, str) and spec.strip().lstrip("-").isdigit()
    ):
        budget = int(spec)
        if family == "budget-pro":
            if budget == -1:
                return {"thinkingBudget": -1}  # 动态思考（上限固定 8192）
            if budget == 0:
                # 2.5 Pro 不可关闭：回退到文档下限 128（最接近"关闭"）
                log(
                    "info",
                    f"模型 {model_lower} 为 2.5 Pro，思考不可关闭，"
                    f"off/0 已按最低 budget=128 处理",
                )
                return {"thinkingBudget": 128}
            return {"thinkingBudget": max(128, min(budget, 32768))}
        if family == "budget-flash":
            if budget == -1:
                return {"thinkingBudget": -1}
            return {"thinkingBudget": max(0, min(budget, 24576))}
        if family == "level":
            if budget == 0 or budget == -1:
                # 等级制模型思考不可关闭（始终动态思考），走模型默认
                log(
                    "info",
                    f"模型 {model_lower} 为等级制（3.x/4.x），思考不可关闭，"
                    f"off/dynamic 已按模型默认处理",
                )
                return {}
            return _budget_spec_on_level_family(budget)
        # family is None：不支持思考的模型
        log("warning", f"模型 {model_lower} 不支持思考参数，指定值 {spec!r} 已忽略")
        return _INVALID

    if isinstance(spec, str):
        s = spec.strip().lower()
        if s in _OFF_VALUES:
            if family == "budget-flash":
                return {"thinkingBudget": 0}
            if family == "budget-pro":
                log(
                    "info",
                    f"模型 {model_lower} 为 2.5 Pro，思考不可关闭，off 已按最低 budget=128 处理",
                )
                return {"thinkingBudget": 128}
            if family == "level":
                log(
                    "info",
                    f"模型 {model_lower} 为等级制（3.x/4.x），思考不可关闭，off 已按模型默认处理",
                )
                return {}
            log("warning", f"模型 {model_lower} 不支持思考参数，off 已忽略")
            return _INVALID
        if s in _DYNAMIC_VALUES:
            if family in ("budget-pro", "budget-flash"):
                return {"thinkingBudget": -1}
            if family == "level":
                return {}  # 3.x/4.x 本来就是动态思考
            return _INVALID
        if s in _VALID_LEVELS:
            if family == "level":
                return {"thinkingLevel": s}
            if family in ("budget-pro", "budget-flash"):
                return _level_spec_on_budget_family(family, s)
            log("warning", f"模型 {model_lower} 不支持思考参数，等级 {s!r} 已忽略")
            return _INVALID
        log(
            "warning",
            f"思考等级指定值无效：{spec!r}（模型 {model_lower}），"
            f"支持 minimal/low/medium/high、off、dynamic 或数字",
        )
        return _INVALID

    log("warning", f"思考等级指定值类型无效：{spec!r}（模型 {model_lower}）")
    return _INVALID


def _lookup_model_spec(levels: dict, model_lower: str):
    """在映射表中查找模型指定：先精确匹配，再最长前缀匹配（大小写不敏感）。"""
    if not isinstance(levels, dict) or not levels:
        return None

    # 规范化键：全小写、去空白
    normalized = {}
    for key, value in levels.items():
        k = str(key).strip().lower()
        if k:
            normalized[k] = value

    if model_lower in normalized:
        return normalized[model_lower]

    best = ""
    for k in normalized:
        if model_lower.startswith(k) and len(k) > len(best):
            best = k
    return normalized.get(best) if best else None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def resolve_thinking_override(model: str):
    """计算某模型最终应强制使用的 thinkingConfig（camelCase）。

    返回 None（透传请求参数）/ {}（不附加思考配置）/ dict（强制配置）。
    """
    if not getattr(settings, "ENABLE_THINKING", True):
        # 总闸关闭：完全不干预思考参数
        return None

    model_lower = (model or "").strip().lower()
    if not model_lower:
        return None

    # 1) 按模型指定（优先级最高）：精确匹配 → 最长前缀匹配
    spec = _lookup_model_spec(
        getattr(settings, "MAX_THINKING_MODEL_LEVELS", None), model_lower
    )
    if spec is not None:
        config = _spec_to_config(model_lower, spec)
        if config is not _INVALID:
            return config
        # 值无效：记过 warning 后回退到全局开关继续判定

    # 2) 全局最高档开关
    if getattr(settings, "MAX_THINKING", False):
        return _max_thinking_config(model_lower)

    # 3) 无覆盖：按请求参数透传
    return None


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def apply_thinking_override(model: str, data: dict) -> None:
    """在最终 Gemini REST payload 上应用思考覆盖（原地修改）。

    适用于 generativelanguage.googleapis.com 的 camelCase JSON：
      data["generationConfig"]["thinkingConfig"] = {...}
    会保留客户端显式要求的思考摘要回显（includeThoughts）。
    """
    if not isinstance(data, dict):
        return

    override = resolve_thinking_override(model)
    if override is None:
        return  # 透传：不做任何修改

    gen_config = data.get("generationConfig")
    if not isinstance(gen_config, dict):
        gen_config = {}

    if not override:
        # 强制「不附加」：移除客户端可能携带的 thinkingConfig
        if "thinkingConfig" in gen_config:
            gen_config.pop("thinkingConfig", None)
            data["generationConfig"] = gen_config
        return

    merged = dict(override)
    existing = gen_config.get("thinkingConfig")
    if isinstance(existing, dict) and (
        existing.get("includeThoughts") or existing.get("include_thoughts")
    ):
        merged["includeThoughts"] = True
    gen_config["thinkingConfig"] = merged
    data["generationConfig"] = gen_config


def sanitize_thinking_for_model(model: str, data: dict) -> None:
    """等级制模型（3.x/4.x）防呆净化：thinkingBudget → thinkingLevel。

    背景：4.x 携带 thinkingBudget 直接 400（官方仅接受等级）；3.x 虽
    向后兼容 budget，但官方已推荐等级制。无论 budget 来自代理合成
    （OpenAI reasoning_effort / thinking_budget 换算、Claude 协议
    thinking.budget_tokens 换算）还是客户端直发，到达等级制模型都是
    错误在等待发生 —— 统一就近换算为等级，把必然的 400 转化为最
    接近的合法语义。

    规则（与 _spec_to_config 的数字 spec 语义一致）：
      - budget <= 0（0=off / -1=dynamic）：等级制模型思考不可关闭，
        丢弃 budget 走模型默认；若 thinkingConfig 因此变空则整体移除；
      - budget > 0：按区间换算（<=2048→low、<=10240→medium、
        更高→high）；payload 已显式携带 thinkingLevel 时仅丢弃 budget
        （显式等级优先，不覆盖）；
      - 其余键（如 includeThoughts）原样保留。

    仅在 ENABLE_THINKING 开启时生效（总闸关闭 = 完全透明代理）。
    2.5 系 budget 家族与不支持思考的模型不受影响。
    """
    if not getattr(settings, "ENABLE_THINKING", True):
        return
    if not isinstance(data, dict):
        return

    family = _model_family((model or "").strip().lower())
    if family != "level":
        return

    gen_config = data.get("generationConfig")
    if not isinstance(gen_config, dict):
        return
    tc = gen_config.get("thinkingConfig")
    if not isinstance(tc, dict) or "thinkingBudget" not in tc:
        return

    budget = tc.pop("thinkingBudget")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        # 非数字脏数据：直接丢弃 budget，其余键原样保留
        log("warning", f"模型 {model} 为等级制，已丢弃非法 thinkingBudget={budget!r}")
    elif budget <= 0:
        log(
            "info",
            f"模型 {model} 为等级制（3.x/4.x），思考不可关闭，"
            f"thinkingBudget={int(budget)} 已按模型默认处理",
        )
    elif "thinkingLevel" in tc:
        # 客户端已显式指定等级：仅去掉废弃的 budget 即可
        log(
            "info",
            f"模型 {model} 已显式指定 thinkingLevel={tc['thinkingLevel']!r}，"
            f"已丢弃兼容性 thinkingBudget={int(budget)}",
        )
    else:
        converted = _budget_spec_on_level_family(int(budget))
        level = converted["thinkingLevel"]
        tc.update(converted)
        log(
            "info",
            f"模型 {model} 为等级制（3.x/4.x），thinkingBudget={int(budget)} "
            f"已就近换算为 thinkingLevel={level}",
        )

    if not tc:
        gen_config.pop("thinkingConfig", None)
    else:
        gen_config["thinkingConfig"] = tc


def apply_thinking_override_genai(model: str, generation_config: dict) -> None:
    """在 Vertex（google-genai SDK）generation_config 上应用思考覆盖。

    SDK 使用 snake_case：generation_config["thinking_config"]。
    语义与 apply_thinking_override 完全一致，仅键名风格不同。
    """
    if not isinstance(generation_config, dict):
        return

    override = resolve_thinking_override(model)
    if override is None:
        return

    if not override:
        if "thinking_config" in generation_config:
            generation_config.pop("thinking_config", None)
        return

    merged = {_camel_to_snake(k): v for k, v in override.items()}
    existing = generation_config.get("thinking_config")
    if isinstance(existing, dict) and existing.get("include_thoughts"):
        merged["include_thoughts"] = True
    generation_config["thinking_config"] = merged
