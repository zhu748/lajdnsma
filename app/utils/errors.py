"""Protocol-specific error response helpers."""

from __future__ import annotations

from typing import Any, Dict

from app.utils.protocol_common import _now_ts


def openai_error_response(
    message: str,
    status_code: int = 500,
    error_type: str = "server_error",
    code: str | None = None,
) -> Dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code or str(status_code),
        }
    }


def responses_error_response(
    message: str, status_code: int = 500, code: str | None = None
) -> Dict[str, Any]:
    now = _now_ts()
    return {
        "id": f"resp_error_{now}",
        "object": "response",
        "created_at": now,
        "status": "failed",
        "error": {
            "message": message,
            "type": "gateway_error",
            "code": code or str(status_code),
        },
        "incomplete_details": None,
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def anthropic_error_response(
    message: str, error_type: str = "invalid_request_error"
) -> Dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def gemini_error_response(
    message: str, status_code: int = 500, status: str = "INVALID_ARGUMENT"
) -> Dict[str, Any]:
    return {"error": {"code": status_code, "message": message, "status": status}}
