import json
from typing import Any, Dict, List

from app.utils.errors import responses_error_response
from app.utils.protocol_common import _ensure_list, _extract_openai_usage, _now_ts


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
                "id": f"msg_{chat_response.get('id', 'response')}",
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
    return {
        "id": f"resp_{chat_response.get('id', _now_ts())}",
        "object": "response",
        "created_at": chat_response.get("created", _now_ts()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": request_payload.get("instructions"),
        "metadata": request_payload.get("metadata") or {},
        "model": chat_response.get("model"),
        "output": output_items,
        "parallel_tool_calls": request_payload.get("parallel_tool_calls", True),
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
        content.append(
            {
                "type": "thinking",
                "thinking": message["reasoning_content"],
                "signature": "",
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

    finish_reason = choice.get("finish_reason")
    stop_reason = "end_turn"
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"

    return {
        "id": f"msg_{chat_response.get('id', _now_ts())}",
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
