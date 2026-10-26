"""入站请求转换：Anthropic Messages 请求 → ChatCompletionRequest。"""
import json
from typing import Any, Dict, List

from app.models.schemas import ChatCompletionRequest
from app.utils.protocol_validation import (
    array_value, object_value, string_value, function_arguments, register_call, tool_result_name,
)


GEMINI_MAX_THINKING_BUDGET = 24576


def _clamp_gemini_thinking_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(budget, GEMINI_MAX_THINKING_BUDGET))


def _claude_image_to_openai_image(item: Dict[str, Any]) -> Dict[str, Any] | None:
    source = object_value(item.get("source"), "Image source")

    source_type = source.get("type")
    if source_type == "base64":
        media_type = string_value(source.get("media_type", "image/png"), "Image media_type", nonempty=True)
        data = string_value(source.get("data", ""), "Image data", nonempty=True)
        if data:
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }

    if source_type == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": string_value(source["url"], "Image URL", nonempty=True)}}

    raise ValueError("Unsupported image source; provide base64 data or a URL")


def _claude_tool_result_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    text_parts = []
    for item in array_value(content, "tool_result.content"):
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict):
            if item.get("type") == "text" and item.get("text"):
                text_parts.append(string_value(item["text"], "Tool result text"))
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
        else:
            raise ValueError("Tool result blocks must be objects")
    return "\n".join(part for part in text_parts if part)


def _claude_system_to_text(system_value: Any) -> str:
    if isinstance(system_value, str):
        return system_value
    if system_value is None:
        return ""

    text_parts = []
    for item in array_value(system_value, "system"):
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict):
            if item.get("type") not in (None, "text"):
                raise ValueError("Only text system blocks are supported")
            text_parts.append(string_value(item.get("text", ""), "System text"))
        else:
            raise ValueError("System blocks must be text objects")

    return "\n".join(text_parts)


def _claude_tool_choice_to_openai(choice: Any) -> Any:
    if choice is None:
        return "auto"
    if isinstance(choice, str):
        if choice not in ("auto", "none"):
            raise ValueError("Unsupported tool_choice")
        return choice

    object_value(choice, "tool_choice")
    choice_type = string_value(choice.get("type"), "tool_choice.type")
    if "disable_parallel_tool_use" in choice and not isinstance(choice["disable_parallel_tool_use"], bool):
        raise ValueError("disable_parallel_tool_use must be a boolean")
    if choice_type == "none":
        return "none"
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        tool_name = string_value(choice.get("name"), "tool_choice.name", nonempty=True)
        return {"type": "function", "function": {"name": tool_name}}

    raise ValueError("Unsupported tool_choice")


def claude_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    messages: List[Dict[str, Any]] = []
    tool_use_id_to_name: Dict[str, str] = {}

    system_text = _claude_system_to_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message in array_value(payload.get("messages", []), "messages"):
        object_value(message, "Message")

        role = message.get("role", "user")
        if role not in ("user", "assistant"):
            raise ValueError("Messages roles must be user or assistant")
        content = message.get("content", "")

        if isinstance(content, str):
            if content.strip():
                messages.append({"role": role, "content": content})
            continue

        array_value(content, "Message content")
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
            tool_use_id = item.get("tool_use_id")
            name = tool_result_name(tool_use_id_to_name, tool_use_id)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": _claude_tool_result_content_to_text(
                    item.get("content", "")
                ),
            }
            if "is_error" in item and not isinstance(item["is_error"], bool):
                raise ValueError("tool_result.is_error must be a boolean")
            if item.get("is_error"):
                tool_message["content"] = (
                    "[tool_result_error]\n" + tool_message["content"]
                ).rstrip()
            tool_message["name"] = name
            return tool_message

        for item in content:
            object_value(item, "Content block")

            item_type = string_value(item.get("type"), "Content block type")
            if item_type == "text":
                text = string_value(item.get("text", ""), "Content text")
                if text:
                    content_parts.append({"type": "text", "text": text})
            elif item_type in {"thinking", "redacted_thinking"}:
                # Opaque reasoning history is not ordinary assistant text.
                continue
            elif item_type == "image":
                image_part = _claude_image_to_openai_image(item)
                if image_part:
                    content_parts.append(image_part)
            elif item_type == "tool_use":
                if role != "assistant":
                    raise ValueError("tool_use blocks must be in assistant messages")
                tool_id = item.get("id")
                tool_name = item.get("name")
                tool_input = item.get("input", {})
                register_call(tool_use_id_to_name, tool_id, tool_name)
                tool_call = {
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": function_arguments(object_value(tool_input, "tool_use.input")),
                    },
                }
                thought_signature = (
                    item.get("thought_signature")
                    or item.get("thoughtSignature")
                )
                if thought_signature:
                    string_value(thought_signature, "thought_signature")
                    tool_call["extra_content"] = {
                        "google": {"thought_signature": thought_signature}
                    }
                tool_calls.append(tool_call)
            elif item_type == "tool_result":
                if role != "user" or content_parts:
                    raise ValueError("tool_result blocks must precede other content in user messages")
                messages.append(build_tool_result_message(item))
            else:
                raise ValueError(f"Unsupported Messages content type: {item_type}")

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
    for tool in array_value(payload.get("tools") if payload.get("tools") is not None else [], "tools"):
        object_value(tool, "Tool")
        string_value(tool.get("name"), "Tool name", nonempty=True)
        object_value(tool.get("input_schema", {}), "Tool input_schema")
        if tool.get("type") not in (None, "custom"):
            raise ValueError("Only custom function tools are supported")
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


    enable_thinking = False
    thinking_budget = 0
    expose_reasoning = False
    thinking_config = payload.get("thinking")
    if thinking_config is not None:
        object_value(thinking_config, "thinking")
        thinking_type = thinking_config.get("type")
        if thinking_type not in ("enabled", "disabled"):
            raise ValueError("Only enabled/disabled thinking is supported")
        if thinking_type == "enabled":
            budget = thinking_config.get("budget_tokens")
            if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
                raise ValueError("thinking.budget_tokens must be a positive integer")
            enable_thinking = True
            thinking_budget = _clamp_gemini_thinking_budget(
                thinking_config.get("budget_tokens", 0)
            )

    return ChatCompletionRequest(
        model=payload.get("model"),
        messages=messages,
        temperature=payload.get("temperature", 0.7),
        top_p=payload.get("top_p"),
        stream=payload.get("stream", False),
        max_tokens=payload.get("max_tokens"),
        stop=payload.get("stop_sequences"),
        thinking_budget=thinking_budget,
        enable_thinking=enable_thinking,
        expose_reasoning=expose_reasoning,
        tools=openai_tools or None,
        tool_choice=mapped_tool_choice,
        parallel_tool_calls=(
            not payload["tool_choice"]["disable_parallel_tool_use"]
            if isinstance(payload.get("tool_choice"), dict)
            and "disable_parallel_tool_use" in payload["tool_choice"] else None
        ),
        source_protocol="claude",
    )
