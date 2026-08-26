"""Protocol-specific error response helpers."""

from __future__ import annotations

from typing import Any, Dict

from app.utils.protocol_common import _now_ts
from app.utils.stealth import gen_openai_response_id


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
    """Build a Responses API error payload.

    Hardening notes:
    * `id` previously used `resp_error_{int(time.time())}` — non-standard
      prefix and a second-level timestamp fingerprint.  We now reuse the
      strong-random `gen_openai_response_id()` (resp_ + 30 alphanumerics)
      that real OpenAI Responses payloads use.
    * `error.type` previously was the literal `"gateway_error"` which
      directly disclosed that this is a gateway.  We now use OpenAI's
      canonical `server_error` (for 5xx) / `invalid_request_error` (4xx)
      / `rate_limit_exceeded` (429) types, which is exactly what the
      real OpenAI Responses API returns.
    """
    if status_code == 429:
        err_type = "rate_limit_exceeded"
    elif 400 <= status_code < 500:
        err_type = "invalid_request_error"
    else:
        err_type = "server_error"
    now = _now_ts()
    return {
        "id": gen_openai_response_id(),
        "object": "response",
        "created_at": now,
        "status": "failed",
        "error": {
            "message": message,
            "type": err_type,
            "code": code or str(status_code),
        },
        "incomplete_details": None,
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _anthropic_error_type(status_code: int | None) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code and status_code >= 500:
        return "api_error"
    return "invalid_request_error"


def anthropic_error_response(
    message: str,
    error_type: str | None = None,
    status_code: int | None = None,
) -> Dict[str, Any]:
    error_type = error_type or _anthropic_error_type(status_code)
    return {"type": "error", "error": {"type": error_type, "message": message}}


def gemini_error_response(
    message: str, status_code: int = 500, status: str = "INVALID_ARGUMENT"
) -> Dict[str, Any]:
    return {"error": {"code": status_code, "message": message, "status": status}}
