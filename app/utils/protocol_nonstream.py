import json
from typing import Any, Dict, List

# Cleanup: responses_error_response 导入未使用，已删除。
from app.utils.protocol_common import (
    _ensure_list,
    _extract_openai_usage,
    _now_ts,
    _openai_finish_reason_to_claude_stop_reason,
    _gen_anthropic_thinking_signature,
)
from app.utils.stealth import (
    gen_anthropic_message_id,
    gen_openai_message_id,
    gen_openai_response_id,
)


def openai_chat_to_response_api(
    chat_response: Dict[str, Any], request_payload: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    request_payload = request_payload or {}
    choice = chat_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})
    output_items: List[Dict[str, Any]] = []

    text_content = message.get("content")
    if text_content:
        output_items.append(
            {
                "type": "message",
                "id": gen_openai_message_id(),
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": text_content, "annotations": []}
                ],
            }
        )

    for tool_call in _ensure_list(message.get("tool_calls")):
        if not isinstance(tool_call, dict):
            continue
        function_data = tool_call.get("function", {})
        output_items.append(
            {
                "type": "function_call",
                "id": tool_call.get("id", f"fc_{_now_ts()}"),
                "call_id": tool_call.get("id", f"fc_{_now_ts()}"),
                "name": function_data.get("name"),
                "arguments": function_data.get("arguments", "{}"),
                "status": "completed",
            }
        )

    usage_counts = _extract_openai_usage(usage)
    # Echo `parallel_tool_calls` exactly as the client sent it (None
    # means "client didn't send the field" — don't echo True by
    # default, which previously diverged from real OpenAI Responses
    # behaviour and was a fingerprint).
    parallel_tool_calls_echo = request_payload.get("parallel_tool_calls")
    return {
        # Protocol: 官方 Responses API 的响应 id 形态是 resp_ + 30 位随机
        # 字符；旧实现的 f"resp_{chat_response['id']}" 会产生
        # "resp_chatcmpl-xxx" 双前缀，形态与官方不符且是可检测指纹。
        "id": gen_openai_response_id(),
        "object": "response",
        "created_at": chat_response.get("created", _now_ts()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": request_payload.get("instructions"),
        "metadata": request_payload.get("metadata") or {},
        "model": chat_response.get("model"),
        "output": output_items,
        "parallel_tool_calls": parallel_tool_calls_echo,
        "previous_response_id": request_payload.get("previous_response_id"),
        "reasoning": request_payload.get("reasoning"),
        "store": request_payload.get("store", False),
        "temperature": request_payload.get("temperature"),
        "text": request_payload.get("text") or {"format": {"type": "text"}},
        "tool_choice": request_payload.get("tool_choice", "auto"),
        "tools": request_payload.get("tools") or [],
        "top_p": request_payload.get("top_p"),
        "truncation": request_payload.get("truncation", "disabled"),
        "usage": usage_counts,
    }


def openai_chat_to_claude_response(chat_response: Dict[str, Any]) -> Dict[str, Any]:
    choice = chat_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})
    usage_counts = _extract_openai_usage(usage)
    content: List[Dict[str, Any]] = []

    if message.get("reasoning_content"):
        # Anthropic API requires a non-empty `signature` on every
        # thinking block; an empty string causes the next-turn
        # tool_use round-trip to fail Anthropic's signature validator.
        # If the upstream OpenAI/Gemini response carried a real
        # thoughtSignature, surface it; otherwise emit a strong-random
        # base64-style signature of realistic length (replaces the
        # previous `""` which was both a fingerprint and a
        # functional breakage).
        signature_payload = ""
        extra_content = message.get("extra_content") or {}
        google_extra = extra_content.get("google", {}) if isinstance(extra_content, dict) else {}
        if isinstance(google_extra, dict):
            signature_payload = (
                google_extra.get("thought_signature")
                or google_extra.get("thoughtSignature")
                or ""
            )
        if not signature_payload:
            signature_payload = _gen_anthropic_thinking_signature()
        content.append(
            {
                "type": "thinking",
                "thinking": message["reasoning_content"],
                "signature": signature_payload,
            }
        )

    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})

    for tool_call in _ensure_list(message.get("tool_calls")):
        if not isinstance(tool_call, dict):
            continue
        function_data = tool_call.get("function", {})
        try:
            tool_input = json.loads(function_data.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {"raw_arguments": function_data.get("arguments", "{}")}

        content.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id", f"toolu_{_now_ts()}"),
                "name": function_data.get("name"),
                "input": tool_input,
            }
        )
        extra_content = tool_call.get("extra_content") or {}
        google_extra = extra_content.get("google", {})
        thought_signature = google_extra.get("thought_signature") or google_extra.get(
            "thoughtSignature"
        )
        if thought_signature:
            content[-1]["thought_signature"] = thought_signature

    stop_reason = _openai_finish_reason_to_claude_stop_reason(
        choice.get("finish_reason")
    )

    return {
        # Protocol: 官方 Anthropic 消息 id 形态是 msg_01 + 24 位随机字符；
        # 旧实现的 f"msg_{chat_response['id']}" 会产生 "msg_chatcmpl-xxx"
        # 双前缀，形态与官方不符。
        "id": gen_anthropic_message_id(),
        "type": "message",
        "role": "assistant",
        "model": chat_response.get("model"),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage_counts["input_tokens"],
            "output_tokens": usage_counts["output_tokens"],
        },
    }
