"""Terminal upstream failures must remain protocol errors, never assistant text."""
import json


def build_error_response(is_gemini: bool, model: str, content: str, stream: bool):
    if is_gemini:
        payload = {"error": {"code": 503, "message": content, "status": "UNAVAILABLE"}}
    else:
        payload = {"error": {"message": content, "type": "server_error", "code": "503", "param": None}}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n" if stream else payload
