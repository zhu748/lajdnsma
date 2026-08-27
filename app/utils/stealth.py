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
#
# Round 4 refresh: 旧池只有 7 条且版本已过时（Chrome 126 / SDK 0.6.0，
# 2024 年中水平）——一个从不升级的 UA 版本本身就是长期指纹。现扩充
# 到当前主流版本段（Chrome 13x / genai-sdk 1.x），并保留少量旧版本
# 模拟"懒更新的真实客户端"，让池内版本呈自然分布而非全员最新。
_USER_AGENT_POOL = (
    # google-genai-sdk-python 1.x (2025 主流版本)
    "google-genai-sdk-python/1.0.10 gl-python/3.12.3",
    "google-genai-sdk-python/1.5.0 gl-python/3.11.9",
    "google-genai-sdk-python/1.8.2 gl-python/3.12.6",
    "google-genai-sdk-python/1.12.1 gl-python/3.13.1",
    # google-genai-sdk-python 0.x (仍有大量存量用户)
    "google-genai-sdk-python/0.8.5 gl-python/3.11.4",
    "google-genai-sdk-python/0.3.0 gl-python/3.11.4",
    # AI Studio web client fallback (used by generativelanguage.googleapis.com)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # curl fallback (some official scripts use curl)
    "curl/8.4.0",
    "curl/8.9.1",
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


def _x_goog_api_client_for(ua: str) -> Optional[str]:
    """Derive a CONSISTENT `x-goog-api-client` value from the chosen UA.

    Round 4 consistency fix: 此前该头硬编码为
    `google-genai-sdk-python gl-python/3.11`，而同一请求的 User-Agent
    可能随机选中 Chrome 或 curl —— "UA=curl 却携带 SDK 头"这种自相
    矛盾的组合本身就是可检测的代理指纹。真实流量的规则是：
      * google-genai-sdk-python 客户端 → 发送 x-goog-api-client，
        内容与 UA 中的 SDK/Python 版本一致；
      * 浏览器（AI Studio）与 curl → 不发送该头。
    现在与 UA 联动：SDK UA 时返回匹配版本的头，否则返回 None（省略）。
    """
    if ua.startswith("google-genai-sdk-python/"):
        # UA 形如 "google-genai-sdk-python/1.8.2 gl-python/3.12.6"，
        # 官方 SDK 发送的 x-goog-api-client 与其完全一致。
        return ua
    return None


def _is_browser_ua(ua: str) -> bool:
    return ua.startswith("Mozilla/")


# Chrome "Consistency" 版本字符串到 sec-ch-ua 的映射。Chrome 的
# major version 与其品牌版本有固定差值（如 Chrome 131 ↔ "Chromium";v="131",
# "Google Chrome";v="131"），这里只填 major version 即与 UA 一致。
def _sec_ch_ua_for(ua: str) -> str:
    import re

    m = re.search(r"Chrome/(\d+)", ua)
    major = m.group(1) if m else "131"
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"'


def _sec_ch_platform_for(ua: str) -> str:
    if "Windows NT" in ua:
        return '"Windows"'
    if "Macintosh" in ua or "Mac OS X" in ua:
        return '"macOS"'
    if "X11" in ua or "Linux" in ua:
        return '"Linux"'
    return '"Windows"'


def _sec_ch_mobile_for(ua: str) -> str:
    return "?1" if any(
        token in ua for token in ("Android", "iPhone", "iPad", "Mobile")
    ) else "?0"


def _apply_browser_fingerprint_headers(headers: Dict[str, str], ua: str) -> Dict[str, str]:
    """Round 6: 为浏览器 UA 补齐真实浏览器必带的 Client Hints / Fetch Metadata 头。

    此前 UA 池已保证 UA 与 x-goog-api-client 的一致性，但 UA 选中
    Chrome 时请求却不携带任何 `sec-ch-ua` / `sec-fetch-*` 头 —— 真实
    Chrome 发往 Google API 的 XHR 一定带这组头，"Chrome UA + 无 sec 头"
    是 TLS/UA 一致性修完后剩下的头部层指纹。配套规则：

    * `sec-ch-ua` 系列与 UA 中的 Chrome major version 联动（版本不一致
      同样是指纹）。
    * `Origin`/`Referer` 指向 aistudio.google.com —— 因为该 UA 对应的
      真实流量来源就是 AI Studio Web 客户端。
    * `sec-fetch-*` 是 XHR 的标准取值组合（dest=empty/mode=cors）。
    * SDK / curl UA 完全不携带这些头（真实 SDK 也不带）。
    """
    if not _is_browser_ua(ua):
        return headers
    headers["sec-ch-ua"] = _sec_ch_ua_for(ua)
    headers["sec-ch-ua-mobile"] = _sec_ch_mobile_for(ua)
    headers["sec-ch-ua-platform"] = _sec_ch_platform_for(ua)
    headers["Origin"] = "https://aistudio.google.com"
    headers["Referer"] = "https://aistudio.google.com/"
    headers["sec-fetch-dest"] = "empty"
    headers["sec-fetch-mode"] = "cors"
    headers["sec-fetch-site"] = "cross-site"
    return headers


def _apply_goog_client_header(headers: Dict[str, str], ua: str) -> Dict[str, str]:
    """Attach x-goog-api-client only when the UA is an SDK client."""
    client_header = _x_goog_api_client_for(ua)
    if client_header:
        headers["x-goog-api-client"] = client_header
    return headers


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
    * `x-goog-api-client` is set to mimic the official Python SDK
      (round 4: now consistent with the chosen UA, see
      _x_goog_api_client_for).
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
    }
    _apply_browser_fingerprint_headers(headers, ua)
    return _apply_goog_client_header(headers, ua)


def build_openai_compat_headers(
    api_key: Optional[str] = None,
    *,
    streaming: bool = False,
) -> Dict[str, str]:
    """Headers for calls to the OpenAI-compat endpoint on
    generativelanguage.googleapis.com/v1beta/openai/..."""
    ua = pick_user_agent(api_key)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept": "text/event-stream" if streaming else "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # OpenAI-compat 路径同样只在 SDK UA 下携带 x-goog-api-client，
    # 保持与 UA 的组合一致性；浏览器 UA 则补齐 sec-ch-ua/sec-fetch 头。
    _apply_browser_fingerprint_headers(headers, ua)
    return _apply_goog_client_header(headers, ua)


def build_embedding_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for embedding endpoint.  Already used `x-goog-api-key`,
    we just add the rest of the realistic header set."""
    ua = pick_user_agent(api_key)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    _apply_browser_fingerprint_headers(headers, ua)
    return _apply_goog_client_header(headers, ua)


def build_key_probe_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Headers for the `/v1beta/models` key probe.  Same shape as real
    SDK calls so a probe doesn't stand out from real traffic."""
    ua = pick_user_agent(api_key)
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    _apply_browser_fingerprint_headers(headers, ua)
    return _apply_goog_client_header(headers, ua)


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
