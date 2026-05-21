import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from app.models.schemas import ChatCompletionRequest


def _now_ts() -> int:
    return int(time.time())


def _extract_openai_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(
        usage.get("completion_tokens", usage.get("total_tokens", 0)) or 0
    )
    total = int(usage.get("total_tokens", prompt + completion) or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
    }




def _merge_stream_usage(latest_usage: Dict[str, int], usage: Dict[str, Any]) -> Dict[str, int]:
    if not usage:
        return latest_usage

    merged = latest_usage.copy()
    if "prompt_tokens" in usage and usage.get("prompt_tokens") is not None:
        merged["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    if "completion_tokens" in usage and usage.get("completion_tokens") is not None:
        merged["output_tokens"] = int(usage.get("completion_tokens") or 0)
    elif "total_tokens" in usage and usage.get("total_tokens") is not None:
        merged["output_tokens"] = int(usage.get("total_tokens") or 0)

    if "total_tokens" in usage and usage.get("total_tokens") is not None:
        merged["total_tokens"] = int(usage.get("total_tokens") or 0)

    return merged

def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_text_from_response_input_item(item: Dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type in {"input_text", "text", "output_text"}:
        return item.get("text", "")
    if item_type == "message":
        return "\n".join(
            _extract_text_from_response_input_item(content_item)
            for content_item in _ensure_list(item.get("content"))
        ).strip()
    return ""


def response_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    input_value = payload.get("input", [])
    messages: List[Dict[str, Any]] = []

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    else:
        for item in _ensure_list(input_value):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue

            if not isinstance(item, dict):
                continue

            if item.get("type") == "message":
                role = item.get("role", "user")
                content_items = _ensure_list(item.get("content"))
                text_parts = []
                for content_item in content_items:
                    if isinstance(content_item, dict):
                        text = _extract_text_from_response_input_item(content_item)
                        if text:
                            text_parts.append(text)
                combined_text = "\n".join(text_parts).strip()
                if combined_text:
                    messages.append({"role": role, "content": combined_text})
                continue

            text = _extract_text_from_response_input_item(item)
            if text:
                messages.append({"role": "user", "content": text})

    instructions = payload.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})

    return ChatCompletionRequest(
        model=payload["model"],
        messages=messages,
        temperature=payload.get("temperature", 0.7),
        top_p=payload.get("top_p"),
        stream=payload.get("stream", False),
        max_tokens=payload.get("max_output_tokens")
        or payload.get("max_completion_tokens"),
        tools=payload.get("tools"),
        tool_choice=payload.get("tool_choice", "auto"),
    )


def claude_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    messages: List[Dict[str, Any]] = []

    system_value = payload.get("system")
    if isinstance(system_value, str) and system_value:
        messages.append({"role": "system", "content": system_value})
    elif isinstance(system_value, list):
        system_text = "\n".join(
            item.get("text", "") for item in system_value if isinstance(item, dict)
        ).strip()
        if system_text:
            messages.append({"role": "system", "content": system_text})

    for message in _ensure_list(payload.get("messages")):
        if not isinstance(message, dict):
            continue

        role = message.get("role", "user")
        content = message.get("content", "")

        if isinstance(content, str):
            content = content.strip()
            if content:
                messages.append({"role": role, "content": content})
            continue

        if isinstance(content, list):
            text_parts = []
            tool_result_messages = []
            for item in content:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        text_parts.append(text)
                elif item_type == "tool_result":
                    tool_result_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": item.get("content", ""),
                        }
                    )

            if text_parts:
                combined_text = "\n".join(text_parts).strip()
                if combined_text:
                    messages.append({"role": role, "content": combined_text})
            messages.extend(tool_result_messages)

    tool_choice = payload.get("tool_choice", {}).get("type")
    mapped_tool_choice: Any = "auto"
    if tool_choice == "none":
        mapped_tool_choice = "none"
    elif tool_choice in {"auto", "any"}:
        mapped_tool_choice = "auto"
    elif tool_choice == "tool":
        tool_name = payload.get("tool_choice", {}).get("name")
        if tool_name:
            mapped_tool_choice = {"type": "function", "function": {"name": tool_name}}

    openai_tools = []
    for tool in _ensure_list(payload.get("tools")):
        if not isinstance(tool, dict):
            continue
        if tool.get("name"):
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description"),
                        "parameters": tool.get("input_schema", {}),
                    },
                }
            )

    return ChatCompletionRequest(
        model=payload["model"],
        messages=messages,
        temperature=payload.get("temperature", 0.7),
        top_p=payload.get("top_p"),
        stream=payload.get("stream", False),
        max_tokens=payload.get("max_tokens"),
        stop=payload.get("stop_sequences"),
        tools=openai_tools or None,
        tool_choice=mapped_tool_choice,
    )


def openai_chat_to_response_api(chat_response: Dict[str, Any]) -> Dict[str, Any]:
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
                "content": [{"type": "output_text", "text": text_content, "annotations": []}],
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

    return {
        "id": f"resp_{chat_response.get('id', _now_ts())}",
        "object": "response",
        "created_at": chat_response.get("created", _now_ts()),
        "status": "completed",
        "model": chat_response.get("model"),
        "output": output_items,
        "usage": _extract_openai_usage(usage),
    }


def openai_chat_to_claude_response(chat_response: Dict[str, Any]) -> Dict[str, Any]:
    choice = chat_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = chat_response.get("usage", {})
    usage_counts = _extract_openai_usage(usage)
    content: List[Dict[str, Any]] = []

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


def _parse_sse_json(chunk: str) -> Optional[Dict[str, Any]]:
    """Parse SSE chunk and return JSON payload from one or more data lines."""
    data_lines: List[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    if not data_lines:
        return None

    payload = "\n".join(data_lines).strip()
    if not payload or payload == "[DONE]":
        return None

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def openai_stream_to_responses_stream(
    body_iterator: AsyncIterator[Any],
    model: str,
) -> AsyncIterator[str]:
    created_at = _now_ts()
    response_id = f"resp_{created_at}"
    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'model': model, 'object': 'response', 'status': 'in_progress', 'created_at': created_at}}, ensure_ascii=False)}\n\n"

    output_index = 0
    latest_usage = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        parsed = _parse_sse_json(chunk)
        if not parsed:
            continue

        choice = parsed.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        text = delta.get("content")
        if text:
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': output_index, 'delta': text}, ensure_ascii=False)}\n\n"

        usage = parsed.get('usage', {})
        latest_usage = _merge_stream_usage(latest_usage, usage)

        if choice.get("finish_reason"):
            yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': response_id, 'output_index': output_index}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'model': model, 'object': 'response', 'status': 'completed', 'created_at': created_at}, 'usage': latest_usage}, ensure_ascii=False)}\n\n"


async def openai_stream_to_claude_stream(
    body_iterator: AsyncIterator[Any],
    model: str,
) -> AsyncIterator[str]:
    message_id = f"msg_{_now_ts()}"
    yield "event: message_start\n"
    yield (
        f"data: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"
    )
    yield "event: content_block_start\n"
    yield (
        f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
    )

    output_tokens = 0

    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        parsed = _parse_sse_json(chunk)
        if not parsed:
            continue

        choice = parsed.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        text = delta.get("content")
        if text:
            yield "event: content_block_delta\n"
            yield (
                f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}, ensure_ascii=False)}\n\n"
            )

        usage = parsed.get('usage', {})
        if usage:
            output_tokens = _merge_stream_usage(
                {'input_tokens': 0, 'output_tokens': output_tokens, 'total_tokens': 0},
                usage,
            )['output_tokens']

        if choice.get("finish_reason"):
            stop_reason = "end_turn"
            if choice.get("finish_reason") == "tool_calls":
                stop_reason = "tool_use"

            yield "event: content_block_stop\n"
            yield (
                f"data: {json.dumps({'type': 'content_block_stop', 'index': 0}, ensure_ascii=False)}\n\n"
            )
            yield "event: message_delta\n"
            yield (
                f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None, 'usage': {'output_tokens': output_tokens}}}, ensure_ascii=False)}\n\n"
            )
            yield "event: message_stop\n"
            yield f"data: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"
