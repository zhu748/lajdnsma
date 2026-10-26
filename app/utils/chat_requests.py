"""Validate nested Chat input before backend dispatch can lose or misread it."""
from app.utils.protocol_validation import (
    array_value, object_value, string_value, function_arguments,
    register_call, tool_result_name, tool_metadata,
)


def normalize_chat_messages(messages):
    normalized, names = [], {}
    for source in messages:
        message = dict(object_value(source, "Message"))
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise ValueError("Unsupported Chat message role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            for part in array_value(content, "Message content"):
                object_value(part, "Content block")
                kind = part.get("type")
                if kind == "text":
                    string_value(part.get("text"), "Content text")
                elif kind == "image_url" and role != "tool":
                    image = object_value(part.get("image_url"), "image_url")
                    string_value(image.get("url"), "Image URL", nonempty=True)
                else:
                    raise ValueError("Unsupported Chat content type")
            if role == "tool":
                message["content"] = "\n".join(part["text"] for part in content)
        calls = message.get("tool_calls")
        if calls is not None:
            array_value(calls, "tool_calls")
            if calls and role != "assistant":
                raise ValueError("tool_calls must be in assistant messages")
            normalized_calls = []
            for call in calls:
                call = dict(object_value(call, "Tool call"))
                function = dict(object_value(call.get("function"), "Tool function"))
                if call.get("type", "function") != "function":
                    raise ValueError("Only function tool calls are supported")
                register_call(names, call.get("id"), function.get("name"))
                function["arguments"] = function_arguments(function.get("arguments", "{}"))
                call["function"] = function
                if call.get("extra_content") is not None:
                    tool_metadata(call["extra_content"])
                normalized_calls.append(call)
            message["tool_calls"] = normalized_calls
        if role == "tool":
            message["name"] = tool_result_name(names, message.get("tool_call_id"))
            if content is None:
                message["content"] = ""
        normalized.append(message)
    return normalized
