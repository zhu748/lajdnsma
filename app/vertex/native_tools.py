"""Lossless tool messages and configuration for the native Gen AI SDK."""
import base64
import binascii
import json
import uuid

from google.genai import types


class NativeRequestError(ValueError):
    """Invalid client input, safe to report as HTTP 400."""


def _object(value, name):
    if not isinstance(value, dict):
        raise NativeRequestError(f"{name} must be an object")
    return value


def function_call_parts(calls):
    parts = []
    for call in calls or []:
        call = _object(call, "tool_call")
        function = _object(call.get("function"), "tool_call.function")
        if call.get("type", "function") != "function" or not function.get("name"):
            raise NativeRequestError("Only named function tool calls are supported")
        args = function.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError as exc:
                raise NativeRequestError("Tool arguments must be valid JSON") from exc
        _object(args, "tool arguments")
        extra = _object(call.get("extra_content") or {}, "extra_content")
        google = _object(extra.get("google") or {}, "extra_content.google")
        signature = google.get("thought_signature") or google.get("thoughtSignature")
        signature_bytes = None
        if signature:
            try:
                signature_bytes = base64.b64decode(signature, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise NativeRequestError("thought_signature must be base64 encoded") from exc
        # Foreign history has no provider signature. The documented sentinel is
        # accepted as bytes by the SDK; never replace an actual signature.
        elif not parts:
            signature_bytes = b"skip_thought_signature_validator"
        parts.append(types.Part(
            function_call=types.FunctionCall(id=call.get("id"), name=function["name"], args=args),
            thought_signature=signature_bytes,
        ))
    return parts


def function_response_part(message, call_names):
    call_id = getattr(message, "tool_call_id", None)
    name = call_names.get(call_id) or getattr(message, "name", None)
    if not name:
        raise NativeRequestError("Tool result must reference a named function call")
    content = message.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
    response = content if isinstance(content, dict) else {"result": content}
    return types.Part(function_response=types.FunctionResponse(id=call_id, name=name, response=response))


def candidate_tool_calls(candidate, *, index_offset=None):
    calls = []
    for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
        function = getattr(part, "function_call", None)
        if function is None:
            continue
        call = {
            "id": getattr(function, "id", None) or "call_" + uuid.uuid4().hex,
            "type": "function",
            "function": {"name": function.name, "arguments": json.dumps(function.args or {}, ensure_ascii=False)},
        }
        signature = getattr(part, "thought_signature", None)
        if signature:
            encoded = base64.b64encode(signature).decode("ascii") if isinstance(signature, bytes) else signature
            call["extra_content"] = {"google": {"thought_signature": encoded}}
        if index_offset is not None:
            call["index"] = index_offset + len(calls)
        calls.append(call)
    return calls


def native_request_options(request):
    """Translate options without allowing SDK automatic tool execution."""
    config = {}
    declarations = []
    for tool in getattr(request, "tools", None) or []:
        tool = _object(tool, "tool")
        function = _object(tool.get("function"), "tool.function")
        if tool.get("type") != "function" or not function.get("name"):
            raise NativeRequestError("Native Vertex supports named function tools only")
        declarations.append(types.FunctionDeclaration(
            name=function["name"], description=function.get("description"),
            parameters_json_schema=function.get("parameters"),
        ))
    if declarations:
        config["tools"] = [types.Tool(function_declarations=declarations)]
        config["automatic_function_calling"] = {"disable": True}
    choice = getattr(request, "tool_choice", None)
    if choice is not None:
        allowed = None
        if isinstance(choice, str) and choice in {"auto", "none", "required"}:
            mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}[choice]
        elif isinstance(choice, dict) and choice.get("type") == "function":
            name = _object(choice.get("function"), "tool_choice.function").get("name")
            if name not in {d.name for d in declarations}:
                raise NativeRequestError("tool_choice must reference a declared function")
            mode, allowed = "ANY", [name]
        else:
            raise NativeRequestError("Unsupported tool_choice")
        if declarations:
            config["tool_config"] = {"function_calling_config": {"mode": mode, "allowed_function_names": allowed}}
        elif choice not in ("none", "auto"):
            raise NativeRequestError("tool_choice requires tools")
    response_format = getattr(request, "response_format", None) or {}
    kind = response_format.get("type")
    if kind in {"json_object", "json_schema"}:
        config["response_mime_type"] = "application/json"
        if kind == "json_schema":
            config["response_json_schema"] = _object(
                _object(response_format.get("json_schema"), "json_schema").get("schema"), "json_schema.schema"
            )
    elif kind not in (None, "text"):
        raise NativeRequestError("Unsupported response_format")
    thinking = {}
    budget = getattr(request, "thinking_budget", None)
    effort = getattr(request, "reasoning_effort", None)
    if budget is not None:
        thinking["thinking_budget"] = budget
    elif effort in {"low", "medium", "high"}:
        thinking["thinking_budget"] = {"low": 1024, "medium": 8192, "high": 24576}[effort]
    if getattr(request, "expose_reasoning", False):
        thinking["include_thoughts"] = True
    if thinking:
        config["thinking_config"] = thinking
    return config
