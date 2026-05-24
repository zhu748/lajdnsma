import json
from typing import Any, Dict, List, Optional

from app.models.schemas import ChatCompletionRequest
from app.utils.protocol_common import _ensure_list


GEMINI_MAX_THINKING_BUDGET = 24576


def _clamp_gemini_thinking_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(budget, GEMINI_MAX_THINKING_BUDGET))


def _response_tools_to_openai_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Convert Responses API function tools to OpenAI chat-completions tools."""
    openai_tools = []
    for tool in _ensure_list(tools):
        if not isinstance(tool, dict):
            continue

        if tool.get("type") == "function" and tool.get("function"):
            openai_tools.append(tool)
            continue

        if tool.get("type") == "function" and tool.get("name"):
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters", {}),
                    },
                }
            )

    return openai_tools or None


def _normalize_openai_role(role: Any) -> str:
    """Normalize newer OpenAI roles to chat-completions compatible roles."""
    if role in {"developer", "system"}:
        return "system"
    if role in {"assistant", "tool"}:
        return role
    return "user"


def _response_tool_choice_to_openai(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice or "auto"

    choice_type = choice.get("type")
    if choice_type in {"auto", "none"}:
        return choice_type
    if choice_type == "required":
        return {"type": "function_calling_config", "mode": "ANY"}
    if choice_type == "function" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}

    return choice


def _extract_text_from_response_input_item(item: Dict[str, Any]) -> str:
    item_type = item.get("type")
    if item_type in {"input_text", "text", "output_text"}:
        return item.get("text", "")
    if item_type == "function_call_output":
        output = item.get("output", "")
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if item_type == "message":
        content = item.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(
            _extract_text_from_response_input_item(content_item)
            for content_item in _ensure_list(content)
            if isinstance(content_item, dict)
        ).strip()
    return ""


def _response_content_to_openai_content(content: Any) -> Any:
    """Convert Responses message content into Chat Completions content."""
    if isinstance(content, str):
        return content

    parts: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for item in _ensure_list(content):
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type in {"input_text", "text", "output_text"}:
            text = item.get("text", "")
            if text:
                text_parts.append(text)
        elif item_type in {"input_image", "image_url"}:
            image_url = item.get("image_url") or item.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                parts.append({"type": "image_url", "image_url": {"url": image_url}})

    if parts:
        return [{"type": "text", "text": "\n".join(text_parts)}] + parts if text_parts else parts
    return "\n".join(text_parts).strip()


def _function_call_output_to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def _function_name_from_call_id(call_id: Any) -> Optional[str]:
    if not isinstance(call_id, str) or not call_id.startswith("call_"):
        return None
    return call_id[len("call_") :].split("__", 1)[0]


def _claude_image_to_openai_image(item: Dict[str, Any]) -> Dict[str, Any] | None:
    source = item.get("source", {})
    if not isinstance(source, dict):
        return None

    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")
        if data:
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }

    if source_type == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}

    return None


def response_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    input_value = payload.get("input", [])
    messages: List[Dict[str, Any]] = []
    call_id_to_name: Dict[str, str] = {}

    previous_response_id = payload.get("previous_response_id")
    if previous_response_id:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Compatibility note: previous_response_id="
                    f"{previous_response_id} was supplied, but this gateway is stateless. "
                    "The client must include the required conversation and tool history in input."
                ),
            }
        )

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
                role = _normalize_openai_role(item.get("role", "user"))
                content = _response_content_to_openai_content(item.get("content"))
                if content:
                    messages.append({"role": role, "content": content})
                continue

            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                arguments = item.get("arguments", "{}")
                if isinstance(arguments, (dict, list)):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                if call_id and name:
                    call_id_to_name[call_id] = name
                if name:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id or f"call_{name}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": arguments or "{}",
                                    },
                                }
                            ],
                        }
                    )
                continue

            if item.get("type") == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                output = _function_call_output_to_text(item.get("output", ""))
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id or "",
                    "content": output,
                }
                function_name = call_id_to_name.get(call_id) or _function_name_from_call_id(call_id)
                if function_name:
                    tool_message["name"] = function_name
                messages.append(tool_message)
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
        tools=_response_tools_to_openai_tools(payload.get("tools")),
        tool_choice=_response_tool_choice_to_openai(payload.get("tool_choice", "auto")),
        source_protocol="responses",
    )


def _claude_tool_result_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    text_parts = []
    for item in _ensure_list(content):
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict):
            if item.get("type") == "text" and item.get("text"):
                text_parts.append(item["text"])
            elif item.get("type") == "image":
                source = item.get("source") or {}
                source_type = source.get("type") if isinstance(source, dict) else None
                if source_type == "url" and source.get("url"):
                    text_parts.append(f"[tool_result_image:url:{source['url']}]")
                elif source_type == "base64":
                    media_type = source.get("media_type", "image/png")
                    text_parts.append(f"[tool_result_image:{media_type}:base64]")
                else:
                    text_parts.append(json.dumps(item, ensure_ascii=False))
            elif item.get("type") == "document":
                text_parts.append(
                    "[tool_result_document]\n"
                    + json.dumps(item, ensure_ascii=False)
                )
            else:
                text_parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(part for part in text_parts if part)


def _claude_system_to_text(system_value: Any) -> str:
    if isinstance(system_value, str):
        return system_value.strip()

    text_parts = []
    for item in _ensure_list(system_value):
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {None, "text"} and item.get("text"):
                text_parts.append(item["text"])
            elif item.get("content"):
                text_parts.append(str(item["content"]))

    return "\n".join(part.strip() for part in text_parts if part and part.strip())


def _claude_tool_choice_to_openai(choice: Any) -> Any:
    if isinstance(choice, str):
        return "none" if choice == "none" else "auto"

    if not isinstance(choice, dict):
        return "auto"

    choice_type = choice.get("type")
    if choice_type == "none":
        return "none"
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return {"type": "function_calling_config", "mode": "ANY"}
    if choice_type == "tool":
        tool_name = choice.get("name")
        if tool_name:
            return {"type": "function", "function": {"name": tool_name}}

    return "auto"


def claude_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    messages: List[Dict[str, Any]] = []
    tool_use_id_to_name: Dict[str, str] = {}

    system_text = _claude_system_to_text(payload.get("system"))
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
            content_parts = []
            tool_calls = []

            def append_content_message():
                nonlocal content_parts
                if not content_parts:
                    return
                if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                    messages.append({"role": role, "content": content_parts[0]["text"]})
                else:
                    messages.append({"role": role, "content": content_parts})
                content_parts = []

            def build_tool_result_message(item):
                tool_use_id = item.get("tool_use_id", "")
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": _claude_tool_result_content_to_text(
                        item.get("content", "")
                    ),
                }
                if item.get("is_error"):
                    tool_message["content"] = (
                        "[tool_result_error]\n" + tool_message["content"]
                    ).rstrip()
                if tool_use_id in tool_use_id_to_name:
                    tool_message["name"] = tool_use_id_to_name[tool_use_id]
                return tool_message

            for item in content:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type")
                if item_type == "text":
                    text = item.get("text", "")
                    if text:
                        content_parts.append({"type": "text", "text": text})
                elif item_type == "thinking":
                    thinking = item.get("thinking", "")
                    if thinking:
                        content_parts.append({"type": "text", "text": thinking})
                elif item_type == "redacted_thinking":
                    redacted = item.get("data") or item.get("text") or ""
                    if redacted:
                        content_parts.append(
                            {"type": "text", "text": f"[redacted_thinking:{redacted}]"}
                        )
                elif item_type == "image":
                    image_part = _claude_image_to_openai_image(item)
                    if image_part:
                        content_parts.append(image_part)
                elif item_type == "tool_use":
                    tool_id = item.get("id") or f"call_{item.get('name', 'tool')}"
                    tool_name = item.get("name")
                    tool_input = item.get("input", {})
                    if tool_name:
                        tool_use_id_to_name[tool_id] = tool_name
                        tool_call = {
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_input, ensure_ascii=False),
                            },
                        }
                        thought_signature = (
                            item.get("thought_signature")
                            or item.get("thoughtSignature")
                        )
                        if thought_signature:
                            tool_call["extra_content"] = {
                                "google": {"thought_signature": thought_signature}
                            }
                        tool_calls.append(tool_call)
                elif item_type == "tool_result":
                    append_content_message()
                    messages.append(build_tool_result_message(item))

            if role != "assistant":
                append_content_message()
            if tool_calls:
                assistant_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                }
                if role == "assistant" and content_parts:
                    if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                        assistant_message["content"] = content_parts[0]["text"]
                    else:
                        assistant_message["content"] = content_parts
                messages.append(assistant_message)
            elif role == "assistant":
                append_content_message()

    mapped_tool_choice = _claude_tool_choice_to_openai(payload.get("tool_choice"))

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


    thinking_config = payload.get("thinking")
    enable_thinking = False
    thinking_budget = 0
    if isinstance(thinking_config, dict):
        thinking_type = thinking_config.get("type")
        if thinking_type == "disabled":
            enable_thinking = False
            thinking_budget = 0
        elif thinking_type == "enabled":
            enable_thinking = True
            thinking_budget = _clamp_gemini_thinking_budget(
                thinking_config.get("budget_tokens", 0)
            )

    return ChatCompletionRequest(
        model=payload["model"],
        messages=messages,
        temperature=payload.get("temperature", 0.7),
        top_p=payload.get("top_p"),
        stream=payload.get("stream", False),
        max_tokens=payload.get("max_tokens"),
        stop=payload.get("stop_sequences"),
        thinking_budget=thinking_budget,
        enable_thinking=enable_thinking,
        tools=openai_tools or None,
        tool_choice=mapped_tool_choice,
        source_protocol="claude",
    )
