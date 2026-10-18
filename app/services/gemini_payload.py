"""Gemini 请求 payload 的纯函数清洗工具（零 app 依赖）。

从 services/gemini.py 拆出：这些函数只做纯字典/结构变换，不依赖任何
应用模块（刻意只用标准库，保证在任何测试桩加载上下文中均可独立导入）。

- GEMINI_SCHEMA_ALLOWED_KEYS: Gemini function declaration 接受的 JSON
  Schema 字段白名单
- sanitize_gemini_schema:     递归剔除白名单外的字段
- sanitize_gemini_payload:    归一化 OpenAI 兼容客户端直发的 Gemini
  payload（角色映射 / systemInstruction 清洗 / 参数 schema 清洗）
- _guess_mime_type_from_url:  图片 URL 后缀 → MIME 类型
- GEMINI_MAX_THINKING_BUDGET / _clamp_thinking_budget: 思考预算上限与钳制
"""
from typing import Optional
from urllib.parse import urlparse

GEMINI_MAX_THINKING_BUDGET = 24576

GEMINI_SCHEMA_ALLOWED_KEYS = {
    "anyOf",
    "description",
    "enum",
    "format",
    "items",
    "maximum",
    "maxItems",
    "minimum",
    "minItems",
    "nullable",
    "properties",
    "propertyOrdering",
    "required",
    "type",
}


def sanitize_gemini_schema(schema):
    """Keep only Schema fields accepted by Gemini function declarations."""

    def sanitize(value, *, in_properties=False):
        if isinstance(value, dict):
            if in_properties:
                return {key: sanitize(item) for key, item in value.items()}
            sanitized = {}
            for key, item in value.items():
                if key not in GEMINI_SCHEMA_ALLOWED_KEYS:
                    continue
                sanitized[key] = sanitize(item, in_properties=(key == "properties"))
            return sanitized
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(schema)


def sanitize_gemini_payload(data):
    """Normalize pass-through Gemini payloads produced from OpenAI-compatible clients."""
    if not isinstance(data, dict):
        return data

    for content in data.get("contents") or []:
        if not isinstance(content, dict):
            continue
        role = content.get("role")
        if role in {"developer", "system"}:
            content["role"] = "user"
        elif role == "assistant":
            content["role"] = "model"

    system_instruction = data.get("system_instruction") or data.get("systemInstruction")
    if isinstance(system_instruction, dict):
        system_instruction.pop("role", None)

    for tool in data.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        for declaration in (
            tool.get("function_declarations") or tool.get("functionDeclarations") or []
        ):
            if isinstance(declaration, dict) and "parameters" in declaration:
                declaration["parameters"] = sanitize_gemini_schema(
                    declaration["parameters"]
                )

    return data


def _guess_mime_type_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    return None


def _clamp_thinking_budget(value) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(budget, GEMINI_MAX_THINKING_BUDGET))


__all__ = [
    "GEMINI_MAX_THINKING_BUDGET",
    "GEMINI_SCHEMA_ALLOWED_KEYS",
    "sanitize_gemini_schema",
    "sanitize_gemini_payload",
    "_guess_mime_type_from_url",
    "_clamp_thinking_budget",
]
