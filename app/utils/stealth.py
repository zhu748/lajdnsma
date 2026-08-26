"""Stealth helpers for outbound upstream requests.

This module centralises "anti-fingerprint" helpers shared across the
Gemini / OpenAI / Embedding upstream services:

1. A pool of realistic User-Agent strings that mimic official Google SDKs.
2. A small consistent header builder for outbound HTTP requests.
3. A stable per-key UA binder so a single API key always uses the same UA
   (avoids short-term UA flapping that itself becomes a fingerprint).
4. Strong random ID generators that match the length/charset of official
   OpenAI / Anthropic / Gemini responses (replaces time-stamp based IDs).

All helpers are pure (no global state outside the per-key UA cache) and
safe to call from async contexts.
"""

from __future__ import annotations

import random
import secrets
import string
import threading
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# User-Agent pool
# ---------------------------------------------------------------------------

# These mirror the UA strings emitted by the official Google Gen AI SDKs.
# Using one of these (rather than httpx's default "python-httpx/x.x.x")
# dramatically reduces the most obvious fingerprint of an unofficial client.
_USER_AGENT_POOL = (
    # google-genai-sdk-python (most common in 2024-2025)
    "google-genai-sdk-python/0.3.0 gl-python/3.11.4",
    "google-genai-sdk-python/0.4.1 gl-python/3.11.6",
    "google-genai-sdk-python/0.5.2 gl-python/3.12.0",
    "google-genai-sdk-python/0.6.0 gl-python/3.11.4",
    # AI Studio web client fallback (used by generativelanguage.googleapis.com)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # curl fallback (some official scripts use curl)
    "curl/8.4.0",
)

# Lock-protected cache that pins each API key to a stable UA so the same
# key doesn't flap between UAs across requests within a process lifetime.
_key_to_ua: Dict[str, str] = {}
_key_to_ua_lock = threading.Lock()


def pick_user_agent(api_key: Optional[str] = None) -> str:
    """Return a realistic User-Agent string.

    If `api_key` is supplied, the same UA is returned for that key for the
    rest of the process lifetime.  This is intentional: real SDK clients
    don't change their UA between requests, and changing UA per-request on
    the same key is itself a fingerprint.  Different keys still get
    different UAs, which spreads TLS+UA binding across the pool.
    """
    if api_key:
        with _key_to_ua_lock:
            cached = _key_to_ua.get(api_key)
            if cached:
                return cached
            chosen = random.choice(_USER_AGENT_POOL)
            _key_to_ua[api_key] = chosen
            return chosen
    return random.choice(_USER_AGENT_POOL)


# ---------------------------------------------------------------------------
# Outbound header builders
# ---------------------------------------------------------------------------

def build_gemini_headers(
    api_key: Optional[str] = None,
    *,
    streaming: bool = False,
) -> Dict[str, str]:
    """Build a realistic header set for outbound calls to
    generativelanguage.googleapis.com.

    Key hardening vs. previous behaviour:
    * `User-Agent` is set (was missing -> defaulted to `python-httpx/x.x`).
    * `x-goog-api-client` is set to mimic the official Python SDK.
    * `Accept` is set per request type (SSE vs JSON).
    * `Accept-Encoding`, `Accept-Language` are added like real SDKs.
    """
    ua = pick_user_agent(api_key)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept": "text/event-stream" if streaming else "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        # Official google-genai-sdk-python sets this header on every call.
        "x-goog-api-client": "google-genai-sdk-python gl-python/3.11",
    }
    return headers


def build_openai_compat_headers(
    api_key: Optional[str] = None,
    *,
    streaming: bool = False,
) -> Dict[str, str]:
    """Headers for calls to the OpenAI-compat endpoint on
    generativelanguage.googleapis.com/v1beta/openai/..."""
    ua = pick_user_agent(api_key)
    return {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept": "text/event-stream" if streaming else "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }


def build_embedding_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for embedding endpoint.  Already used `x-goog-api-key`,
    we just add the rest of the realistic header set."""
    ua = pick_user_agent(api_key)
    return {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "x-goog-api-client": "google-genai-sdk-python gl-python/3.11",
    }


def build_key_probe_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for the `/v1beta/models` key probe.  Same shape as real
    SDK calls so a probe doesn't stand out from real traffic."""
    ua = pick_user_agent(api_key)
    return {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "x-goog-api-client": "google-genai-sdk-python gl-python/3.11",
    }


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
