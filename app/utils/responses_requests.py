"""入站请求转换：Responses 请求 → ChatCompletionRequest。"""
import json
from typing import Any, Dict, List, Optional

from app.models.schemas import ChatCompletionRequest
from app.utils.protocol_validation import (
    array_value, object_value, string_value, function_arguments,
    register_call, tool_result_name, tool_metadata,
)


def _response_tools_to_openai_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Convert Responses API function tools to OpenAI chat-completions tools."""
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    openai_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("Only function tools are supported by the Responses adapter")
        function = tool.get("function") if "function" in tool else {
            key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool
        }
        if tool.get("async") or (isinstance(function, dict) and function.get("async")):
            raise ValueError("Async function tools are not supported by this adapter")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ValueError("Function tools require a name")
        if function.get("parameters") is not None and not isinstance(function["parameters"], dict):
            raise ValueError("Function parameters must be a JSON Schema object")
        openai_tools.append({"type": "function", "function": function})
    return openai_tools or None


def _normalize_openai_role(role: Any) -> str:
    """Normalize newer OpenAI roles to chat-completions compatible roles."""
    if not isinstance(role, str):
        raise ValueError("Message role must be a string")
    if role in {"developer", "system"}:
        return "system"
    if role == "assistant":
        return role
    if role == "user":
        return role
    raise ValueError("Unsupported message role")


def _response_tool_choice_to_openai(choice: Any) -> Any:
    if choice is None:
        return "auto"
    if isinstance(choice, str):
        return choice
    object_value(choice, "tool_choice")

    choice_type = string_value(choice.get("type"), "tool_choice.type")
    if choice_type in {"auto", "none"}:
        return choice_type
    if choice_type == "required":
        return "required"
    if choice_type == "function":
        return {"type": "function", "function": {"name": string_value(choice.get("name"), "tool_choice.name", nonempty=True)}}

    raise ValueError("Unsupported Responses tool_choice")


def _response_content_to_openai_content(content: Any) -> Any:
    """Convert Responses message content into Chat Completions content."""
    if isinstance(content, str):
        return content

    parts: List[Dict[str, Any]] = []
    for item in array_value(content, "Message content"):
        object_value(item, "Content block")

        item_type = string_value(item.get("type"), "Content block type")
        if item_type in {"input_text", "text", "output_text"}:
            text = string_value(item.get("text", ""), "Content text")
            if text:
                parts.append({"type": "text", "text": text})
        elif item_type in {"input_image", "image_url"}:
            image_url = item.get("image_url") or item.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                string_value(image_url, "image_url", nonempty=True)
                image = {"url": image_url}
                if item.get("detail"):
                    if item["detail"] not in ("auto", "low", "high"):
                        raise ValueError("Invalid image detail")
                    image["detail"] = item["detail"]
                parts.append({"type": "image_url", "image_url": image})
            else:
                raise ValueError("Responses images require an image_url; file_id is unsupported")
        else:
            raise ValueError(f"Unsupported Responses content type: {item_type}")

    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def _function_call_output_to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def _append_message(messages, role, content):
    # Adjacent assistant items can surround function calls in Responses output.
    # Keep them in one Chat turn so no tool results are required in between.
    if role != "assistant" or not messages or messages[-1]["role"] != "assistant":
        messages.append({"role": role, "content": content})
        return
    previous = messages[-1].get("content")
    if not previous:
        messages[-1]["content"] = content
    elif isinstance(previous, str) and isinstance(content, str):
        messages[-1]["content"] = previous + "\n" + content
    else:
        def parts(value):
            return [{"type": "text", "text": value}] if isinstance(value, str) else value
        messages[-1]["content"] = parts(previous) + parts(content)


def response_request_to_chat_request(payload: Dict[str, Any]) -> ChatCompletionRequest:
    for field in ("background", "store", "conversation"):
        if payload.get(field):
            raise ValueError(f"{field} is not supported by this stateless adapter")
    if payload.get("truncation") not in (None, "disabled"):
        raise ValueError("Automatic truncation is not supported")
    input_value = payload.get("input", [])
    messages: List[Dict[str, Any]] = []
    call_id_to_name: Dict[str, str] = {}

    previous_response_id = payload.get("previous_response_id")
    if previous_response_id:
        raise ValueError("previous_response_id is not supported; send the complete conversation in input")

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    else:
        for item in array_value(input_value, "input"):
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue

            if not isinstance(item, dict):
                raise ValueError("Responses input items must be objects")
            if "type" in item:
                string_value(item["type"], "Input item type")

            if item.get("type") == "message" or ("type" not in item and "role" in item):
                role = _normalize_openai_role(item.get("role", "user"))
                content = _response_content_to_openai_content(item.get("content", []))
                if content and (not isinstance(content, str) or content.strip()):
                    _append_message(messages, role, content)
                continue

            if item.get("type") == "function_call":
                if item.get("async"):
                    raise ValueError("Async function calls are not supported")
                call_id = item.get("call_id") or item.get("id")
                name = item.get("name")
                arguments = function_arguments(item.get("arguments", "{}"))
                register_call(call_id_to_name, call_id, name)
                call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
                if item.get("extra_content") is not None:
                    call["extra_content"] = tool_metadata(item["extra_content"])
                # Responses emits one output item per tool. Chat expects the
                # parallel calls in a single assistant turn before the results.
                if not messages or messages[-1]["role"] != "assistant":
                    messages.append({"role": "assistant", "content": None})
                messages[-1].setdefault("tool_calls", []).append(call)
                continue

            if item.get("type") == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                output = _function_call_output_to_text(item.get("output", ""))
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id or "",
                    "content": output,
                }
                tool_message["name"] = tool_result_name(call_id_to_name, call_id)
                messages.append(tool_message)
                continue

            if item.get("type") in {"input_text", "text", "output_text"}:
                messages.append({"role": "user", "content": string_value(item.get("text", ""), "Input text")})
            elif item.get("type") != "reasoning":
                raise ValueError(f"Unsupported Responses input type: {item.get('type')}")

    instructions = payload.get("instructions")
    if instructions is not None:
        string_value(instructions, "instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})

    # Pass through `parallel_tool_calls` from the request payload.
    # The OpenAI Responses API spec accepts this field; the previous
    # implementation silently dropped it, which caused clients that
    # sent `parallel_tool_calls=false` to still see Gemini emit
    # parallel tool calls (Gemini's default behaviour).
    parallel_tool_calls = payload.get("parallel_tool_calls")

    # Map the Responses API `reasoning: {"effort": "low|medium|high"}`
    # object onto the chat-completions `reasoning_effort` field, which
    # gemini.py translates into a thinkingBudget.  Codex and other
    # Responses clients set high reasoning effort and previously got the
    # default budget because the field was silently dropped here.
    reasoning_effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort.lower() in {"low", "medium", "high"}:
            reasoning_effort = effort.lower()

    return ChatCompletionRequest(
        model=payload.get("model"),
        messages=messages,
        temperature=payload.get("temperature", 0.7),
        top_p=payload.get("top_p"),
        stream=payload.get("stream", False),
        max_tokens=payload.get("max_output_tokens", payload.get("max_completion_tokens")),
        response_format=_responses_text_format(payload.get("text")),
        tools=_response_tools_to_openai_tools(payload.get("tools")),
        tool_choice=_response_tool_choice_to_openai(payload.get("tool_choice", "auto")),
        source_protocol="responses",
        parallel_tool_calls=parallel_tool_calls,
        reasoning_effort=reasoning_effort,
    )


def _responses_text_format(text):
    if text is None:
        return None
    if not isinstance(text, dict):
        raise ValueError("text must be an object")
    fmt = text.get("format")
    if fmt is None:
        return None
    if not isinstance(fmt, dict) or fmt.get("type") not in ("text", "json_object", "json_schema"):
        raise ValueError("Unsupported text.format")
    if fmt["type"] == "json_schema":
        if not isinstance(fmt.get("schema"), dict):
            raise ValueError("text.format.schema must be a JSON Schema object")
        return {"type": "json_schema", "json_schema": {
            key: value for key, value in fmt.items() if key != "type"
        }}
    return fmt
