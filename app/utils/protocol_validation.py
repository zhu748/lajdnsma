"""Small input guards shared by protocol adapters; failures become HTTP 400."""
import json


def object_value(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def array_value(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def string_value(value, field, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{field} must be {'a non-empty string' if nonempty else 'a string'}")
    return value


def function_arguments(value):
    """Accept JSON objects or their serialized form without changing argument types."""
    if isinstance(value, str):
        try:
            def invalid_constant(_):
                raise ValueError("Non-finite JSON number")
            parsed = json.loads(value, parse_constant=invalid_constant)
        except ValueError as exc:
            raise ValueError("Function arguments must be valid JSON") from exc
        object_value(parsed, "Function arguments")
        return value
    return json.dumps(object_value(value, "Function arguments"), ensure_ascii=False, allow_nan=False)


def tool_metadata(value):
    extra = object_value(value, "extra_content")
    google = object_value(extra.get("google", {}), "extra_content.google")
    for key in ("thought_signature", "thoughtSignature"):
        if key in google:
            string_value(google[key], key)
    return extra


def register_call(names, call_id, name):
    string_value(call_id, "Tool call ID", nonempty=True)
    string_value(name, "Function name", nonempty=True)
    if call_id in names:
        raise ValueError("Tool call IDs must be unique within the conversation")
    names[call_id] = name


def tool_result_name(names, call_id):
    string_value(call_id, "Tool result call ID", nonempty=True)
    if call_id not in names:
        raise ValueError("Tool result must reference a preceding function call; send the complete history")
    return names[call_id]
