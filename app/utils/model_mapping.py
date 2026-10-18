"""Model alias selection helpers shared by protocol handlers."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any


def select_alias_model(model: str, aliases: Any) -> str:
    """Return exact alias target first, then first matching wildcard target."""
    if not isinstance(aliases, dict):
        return ""

    exact_match = aliases.get(model, "")
    if exact_match:
        return exact_match

    for alias_pattern, target_model in aliases.items():
        if "*" in alias_pattern and fnmatchcase(model, alias_pattern):
            return target_model

    return ""


# ---------------------------------------------------------------------------
# Prefill / model-turn 兼容性判断
# ---------------------------------------------------------------------------
# Google 自 2026-07-21 发布 gemini-3.5-flash-lite / gemini-3.6-flash 起，
# 新世代模型强制拒绝"最后一个非空 turn 为 model 角色"的请求（即 OpenAI
# 生态常说的 prefill / 前缀续写，酒馆填表/续写类前端的核心技巧），
# 官方迁移指南原文：
#     "If the last non-empty turn in your request has the role `model`,
#      the API returns 400."
# 并明示该规则适用于这两个模型及此后发布的所有模型；此前的 2.5 系列、
# 3.0-preview、3.1 系列等不受影响，保持原生 prefill 行为。
#
# 判断策略为"宁漏勿错"：漏掉的模型只是维持现状（新模型上原本就 400），
# 而错杀的旧模型会被追加的 user turn 改变续写行为（模型转而回应追加
# 消息而非续写 prefill）。默认按模型世代圈定：
#   * gemini-3.5 / 3.6 / 3.7 / 3.8 ...（主次版本 >= 3.5）→ 需要修复
#   * gemini-4-argon 及未来 gemini-4+                        → 需要修复
#   * gemma-4+（Gemma 历来即要求以 user turn 结尾）          → 需要修复
#   * *-latest 指针别名（指向最新世代，大概率受限）           → 需要修复
#   * 2.5 / 3.0-preview / 3.1 / embedding / 图像类旧模型     → 不修复
# 并支持环境变量白名单 / 黑名单微调（见 settings.PREFILL_FIX_MODELS /
# PREFILL_FIX_EXCLUDE_MODELS）。

# 形如 "gemini-3.5-flash" / "gemini-2.5-pro" / "lyria-3.5" 的主次版本号。
_MAJOR_MINOR_RE = re.compile(r"(?:^|[-_])(\d+)\.(\d+)(?=[-_]|$)")
# 形如 "gemini-4-argon" / "gemma-4-31b-it" / "gemini-3-flash-preview"
# 的主版本号（无小数位，用于抬升到下一代的无小数模型名）。
_MAJOR_ONLY_RE = re.compile(r"^(?:gemini|gemma)-(\d+)(?=[-_]|$)")

# "最后一次受控释放"的世代坐标：规则自 (3, 5) 起生效。
_FIRST_RESTRICTED_GENERATION = (3, 5)
# 无小数位的模型名自 major 4 起视为受限（gemini-4-argon / gemma-4-*）。
_FIRST_RESTRICTED_MAJOR_ONLY = 4


def _strip_model_decorations(model: str) -> str:
    """去掉 models/ 前缀与 -search 联网后缀，得到裸模型名。"""
    m = model.strip().lower()
    if m.startswith("models/"):
        m = m[len("models/"):]
    if m.endswith("-search"):
        m = m[: -len("-search")]
    return m


def needs_model_turn_fix(model: str, *, extra_include=(), exclude=()) -> bool:
    """判断 `model` 是否拒绝以 model turn 结尾的请求（prefill）。

    参数 `extra_include` / `exclude` 接收字符串前缀列表（来自
    settings 的 PREFILL_FIX_MODELS / PREFILL_FIX_EXCLUDE_MODELS，
    逗号分隔），用于覆盖默认启发式。返回 True 表示调用方应在
    contents 末尾补一条非空文本的 user turn。
    """
    m = _strip_model_decorations(str(model or ""))
    if not m:
        return False

    # 黑名单优先级最高：用户显式声明"这个模型不用修"。
    for pattern in exclude:
        pattern = _strip_model_decorations(str(pattern))
        if pattern and (m == pattern or m.startswith(pattern) or fnmatchcase(m, pattern)):
            return False

    # 白名单次之：用户显式声明"这个模型要修"（可覆盖世代启发式）。
    for pattern in extra_include:
        pattern = _strip_model_decorations(str(pattern))
        if pattern and (m == pattern or m.startswith(pattern) or fnmatchcase(m, pattern)):
            return True

    # latest 指针别名指向当前最新世代模型，默认视为受限。
    if m.endswith("-latest"):
        return True

    # 主次版本判断：gemini-3.5 / 3.6 / ... / lyria-3.5 等。
    mm = _MAJOR_MINOR_RE.search(m)
    if mm:
        generation = (int(mm.group(1)), int(mm.group(2)))
        return generation >= _FIRST_RESTRICTED_GENERATION

    # 无小数位的主系列判断：gemini-4-argon / gemma-4-* 受限，
    # gemini-3-flash-preview / gemini-2-* 不受限。
    mo = _MAJOR_ONLY_RE.match(m)
    if mo:
        return int(mo.group(1)) >= _FIRST_RESTRICTED_MAJOR_ONLY

    # 其余（embedding / robotics / deep-research / antigravity 等专用
    # 模型，以及 nano-banana 等图像模型）不参与 chat prefill 场景，
    # 默认不修，需要时用 PREFILL_FIX_MODELS 白名单点名。
    return False


# ---------------------------------------------------------------------------
# Prefill 补丁的共享谓词（纯函数，不依赖 settings —— 各接入路径自行
# 传入配置，供 OpenAI 协议转换、原生 Gemini 透传、Vertex 三条链路复用）
# ---------------------------------------------------------------------------

def _turn_is_nonempty(content) -> bool:
    """判断一条 Gemini turn（dict 形式）是否为非空（Google 校验口径）。

    空 turn = parts 缺失/为空，或所有 part 都没有实际内容（text 为
    空白，且不含 inlineData / fileData / functionCall 等其他键）。
    非 dict 对象（如 google-genai 的 Content）一律视为非空，角色
    判断交给调用方。
    """
    if not isinstance(content, dict):
        return bool(content)
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return False
    for part in parts:
        if not isinstance(part, dict):
            return True
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return True
        if any(key != "text" for key in part):
            return True
    return False


def last_nonempty_turn_role(contents) -> str:
    """返回 contents 中最后一个非空 turn 的角色（Google 校验的就是它）。

    空列表 / 无非空 turn / 非列表输入返回 ""。dict 形式读 "role"，
    对象形式（genai Content）读 role 属性。
    """
    if not isinstance(contents, list) or not contents:
        return ""
    for content in reversed(contents):
        if _turn_is_nonempty(content):
            if isinstance(content, dict):
                role = content.get("role")
            else:
                role = getattr(content, "role", None)
            return role if isinstance(role, str) else ""
    return ""


def build_prefill_user_turn(prompt_text: str) -> dict:
    """构造补丁用的非空 user turn（Gemini REST / camelCase dict 形式）。"""
    return {"role": "user", "parts": [{"text": prompt_text}]}
