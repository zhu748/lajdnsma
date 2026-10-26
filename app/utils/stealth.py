"""Stable Google REST request headers, response IDs and retry jitter.

Headers follow the installed google-genai version's convention. They provide
HTTP-level consistency, not browser TLS/HTTP2 impersonation. Credentials remain
request-scoped and are never retained in a User-Agent cache.
"""
from __future__ import annotations

from importlib.metadata import version
import random
import secrets
import string
import sys
from typing import Dict, Optional


# Match google.genai._api_client.append_library_version_headers without
# importing SDK internals or maintaining a second, stale version catalogue.
_GOOGLE_CLIENT_ID = f"google-genai-sdk/{version('google-genai')} gl-python/{sys.version.split()[0]}"


def pick_user_agent(api_key: Optional[str] = None) -> str:
    """Return a stable SDK-compatible identity; api_key is kept for compatibility.

    Key rotation does not change the software/runtime making the request.
    There is deliberately no per-key state or random browser/curl identity.
    """
    return _GOOGLE_CLIENT_ID


def _build_request_headers(
    api_key: Optional[str],
    *,
    accept: str,
    content_type: Optional[str] = "application/json",
) -> Dict[str, str]:
    ua = pick_user_agent(api_key)
    headers = {
        "User-Agent": ua,
        "x-goog-api-client": ua,
        "Accept": accept,
        # These decoders are always available in the installed HTTP transport.
        "Accept-Encoding": "gzip, deflate",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def build_gemini_headers(
    api_key: Optional[str] = None, *, streaming: bool = False,
) -> Dict[str, str]:
    """Google REST profile; authentication is supplied by the caller."""
    return _build_request_headers(
        api_key, accept="text/event-stream" if streaming else "application/json",
    )


def build_openai_compat_headers(
    api_key: Optional[str] = None, *, streaming: bool = False,
) -> Dict[str, str]:
    """Same Google profile for its OpenAI-compatible endpoint."""
    return build_gemini_headers(api_key, streaming=streaming)


def build_embedding_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    return build_gemini_headers(api_key)


def build_key_probe_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """A bodyless model-list GET does not need Content-Type."""
    return _build_request_headers(api_key, accept="application/json", content_type=None)


# ---------------------------------------------------------------------------
# Strong random ID generators
# ---------------------------------------------------------------------------

# Charset for OpenAI-style random suffixes (alphanumeric, mixed case)
_OA_CHARS = string.ascii_letters + string.digits


def _rand_str(n: int) -> str:
    """Cryptographically-strong random alphanumeric string of length n."""
    return "".join(secrets.choice(_OA_CHARS) for _ in range(n))


def gen_openai_chunk_id() -> str:
    """OpenAI real format: `chatcmpl-` + 29 alphanumeric chars.
    Replaces the previous `chatcmpl-{int(time.time())}` which had
    second-level collisions and an obviously non-official length."""
    return f"chatcmpl-{_rand_str(29)}"


def gen_openai_tool_call_id(function_name: str = "") -> str:
    """OpenAI real format: `call_` + 24 lowercase alphanumeric chars.

    Previous implementation returned `call_{function_name}__{_rand_str(16)}`
    which contained an embedded function name and double underscores —
    both of which never appear in real OpenAI tool_call_ids (the official
    shape is strictly `call_` + 24 lowercase [a-z0-9]).  The previous
    shape was therefore trivially detectable by the regex
    `^call_[a-z0-9]{24}$`.  We now match the official shape exactly.

    The `function_name` argument is retained for backward compatibility
    with callers but is no longer embedded in the id; the round-trip
    parser in `gemini.py` has been updated to look up the function name
    via the tool_call's `function.name` field instead of parsing it back
    out of the id.
    """
    # Real OpenAI tool_call_ids use lowercase alphanumeric only.
    lower_chars = string.ascii_lowercase + string.digits
    return "call_" + "".join(secrets.choice(lower_chars) for _ in range(24))


def gen_anthropic_thinking_signature() -> str:
    """Generate a strong-random base64-style signature for Anthropic
    thinking blocks.

    Real Anthropic `signature` fields returned with thinking blocks are
    opaque ~200-byte base64-like strings.  We can't reproduce Anthropic's
    HMAC here, but we MUST emit a non-empty signature of a similar shape
    because:

    * Empty/short signatures are rejected by Anthropic's server-side
      validator on the next turn when the client round-trips the
      thinking block (which breaks Claude + extended thinking + tool_use
      chains).
    * A non-empty random signature with a realistic shape is also much
      less of a fingerprint than the previous `""` (empty string) or
      the literal `"skip_thought_signature_validator"` value previously
      used by Gemini's dummy signature.
    """
    # 192 bytes encoded as base64 ~ 256 chars; matches Anthropic's
    # typical signature length closely enough to fool shape-based
    # detection.
    raw = secrets.token_bytes(192)
    import base64
    return base64.b64encode(raw).decode("ascii")


def gen_openai_response_id() -> str:
    """OpenAI Responses real format: `resp_` + 30 alphanumeric chars.
    Replaces `resp_{int(time.time())}`."""
    return f"resp_{_rand_str(30)}"


def gen_openai_message_id() -> str:
    """Responses message id: `msg_` + 28 alphanumeric chars."""
    return f"msg_{_rand_str(28)}"


def gen_anthropic_message_id() -> str:
    """Anthropic real format: `msg_01` + 24 alphanumeric chars.
    Replaces `msg_{int(time.time())}`."""
    return f"msg_01{_rand_str(24)}"


def gen_anthropic_tool_use_id() -> str:
    """Anthropic real format: `toolu_01` + 24 alphanumeric chars.
    Replaces `toolu_{ts}_{idx}`."""
    return f"toolu_01{_rand_str(24)}"


def gen_responses_function_call_id() -> str:
    """Responses `function_call` id: `fc_` + 24 alphanumeric chars."""
    return f"fc_{_rand_str(24)}"


def gen_gemini_response_id() -> str:
    """Gemini responseId real format: 32-char base64-like string.
    Replaces `resp_{int(time.time())}`."""
    # Gemini responseIds look like `R7s8BvXkQ2yP9mN3L1tZ` (24 chars, mixed case)
    return _rand_str(24)


# ---------------------------------------------------------------------------
# Full-jitter exponential backoff (AWS recommended)
# ---------------------------------------------------------------------------

def full_jitter_backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """AWS-recommended "full jitter" backoff.

    Previous implementation used `min(1 * 2**attempt, 16)` with no jitter,
    which caused concurrent retriers to retry at the exact same instant
    (1s, 2s, 4s, 8s, 16s), producing visible retry spikes that upstream
    rate-limiters identify as bot behaviour.

    Full jitter spreads retries uniformly across `[0, min(base*2**attempt, cap)]`,
    which eliminates synchronised retry storms.
    """
    if attempt < 0:
        attempt = 0
    expo = min(base * (2 ** attempt), cap)
    return random.uniform(0.0, expo)
