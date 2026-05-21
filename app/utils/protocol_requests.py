from typing import Any, Dict, List, Optional

from app.models.schemas import ChatCompletionRequest
from app.utils.protocol_common import _ensure_list


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


def _response_tool_choice_to_openai(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice or "auto"

    choice_type = choice.get("type")
    if choice_type in {"auto", "none", "required"}:
        return "auto" if choice_type == "required" else choice_type
    if choice_type == "function" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}

    return choice


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
        tools=_response_tools_to_openai_tools(payload.get("tools")),
        tool_choice=_response_tool_choice_to_openai(payload.get("tool_choice", "auto")),
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


