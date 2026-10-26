"""非流式协议转换：chat 响应 → Responses / Claude 响应。"""
import json
from uuid import uuid4
from typing import Any, Dict, List

from app.utils.protocol_common import (
    _ensure_list,
    _extract_openai_usage,
    _now_ts,
    _openai_finish_reason_to_claude_stop_reason,
    _responses_completion_status,
)
from app.utils.stealth import (
    gen_anthropic_message_id,
    gen_openai_message_id,
    gen_openai_response_id,
    gen_responses_function_call_id,
)


def _error_status(error):
    try:
        return int(error.get("code", 502))
    except (TypeError, ValueError):
        return 429 if "rate_limit" in str(error.get("type", "")) else 502


def openai_chat_to_response_api(
    chat_response: Dict[str, Any], request_payload: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    request_payload = request_payload or {}
    if chat_response.get("error"):
        from app.utils.errors import responses_error_response
        error = chat_response["error"]
        return responses_error_response(
            error.get("message", "Upstream service error"), _error_status(error),
            code=str(error.get("code") or "upstream_error"),
        )
    choice = (chat_response.get("choices") or [{}])[0]
    status, incomplete_details = _responses_completion_status(choice.get("finish_reason"))
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})
    output_items: List[Dict[str, Any]] = []

    if message.get("reasoning_content"):
        output_items.append({
            "id": "rs_" + uuid4().hex, "type": "reasoning", "status": status,
            "summary": [{"type": "summary_text", "text": message["reasoning_content"]}],
        })

    text_content = message.get("content")
    if text_content:
        output_items.append(
            {
                "type": "message",
                "id": gen_openai_message_id(),
                "status": status,
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
        call_id = tool_call.get("id") or gen_responses_function_call_id()
        output_items.append(
            {
                "type": "function_call",
                "id": gen_responses_function_call_id(),
                "call_id": call_id,
                "name": function_data.get("name"),
                "arguments": function_data.get("arguments", "{}"),
                "status": status,
                **({"extra_content": tool_call["extra_content"]} if tool_call.get("extra_content") else {}),
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
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
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
    if chat_response.get("error"):
        from app.utils.errors import anthropic_error_response
        error = chat_response["error"]
        return anthropic_error_response(error.get("message", "Upstream service error"), status_code=_error_status(error))
    choice = (chat_response.get("choices") or [{}])[0]
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})
    usage_counts = _extract_openai_usage(usage)
    content: List[Dict[str, Any]] = []

    if message.get("reasoning_content"):
        # Preserve available provider signatures. Never manufacture signatures.
        signature_payload = ""
        extra_content = message.get("extra_content") or {}
        google_extra = extra_content.get("google", {}) if isinstance(extra_content, dict) else {}
        if isinstance(google_extra, dict):
            signature_payload = (
                google_extra.get("thought_signature")
                or google_extra.get("thoughtSignature")
                or ""
            )
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
