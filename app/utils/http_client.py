import asyncio

import httpx

from app.utils.stealth import pick_user_agent


_async_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Round 6: 分层超时常量（供 gemini.py 等出站调用方复用）
#
# 此前所有上游请求都传标量 timeout=600 —— httpx 会把标量同时应用到
# connect/read/write/pool 四个阶段：
#   * connect=600s：网络黑洞（DNS 挂起 / SYN 无响应）时连接阶段就能
#     占住一个 key 槽位整整 10 分钟；
#   * read=600s：上游卡流时，一个 SSE 连接空转 10 分钟才判超时，期间
#     连接池 + 客户端 + 重试体系全部被占死。
# 真实 SDK 的行为是分层的：连接快速失败（~10s），读取给足时间
# （thinking 模型合法长响应），从而把黑洞连接的代价限制在一个
# 可预期的上界内。
UPSTREAM_CONNECT_TIMEOUT = 10.0   # TCP+TLS 建立阶段
UPSTREAM_READ_TIMEOUT = 300.0     # 两个数据块之间 / 完整响应的最长等待
UPSTREAM_WRITE_TIMEOUT = 60.0     # 发送请求体（含数 MB 图片 base64）
UPSTREAM_POOL_TIMEOUT = 10.0      # 从连接池获取连接的等待

UPSTREAM_TIMEOUT = httpx.Timeout(
    connect=UPSTREAM_CONNECT_TIMEOUT,
    read=UPSTREAM_READ_TIMEOUT,
    write=UPSTREAM_WRITE_TIMEOUT,
    pool=UPSTREAM_POOL_TIMEOUT,
)

# Round 6: HTTP/2 支持（若 h2 已安装则启用）。
# 官方 google-genai SDK 与 Chrome 浏览器的 ALPN 都协商 h2；一个只报
# http/1.1 的 TLS 客户端，无论 UA 怎么伪装，都在协议层暴露了
# "这不是它声称的那个客户端"。启用 http2=True 后 httpx 会同时
# 声明 h2 + http/1.1，与真实 SDK 行为一致。
try:  # pragma: no cover - 环境探测
    import h2  # noqa: F401

    _HTTP2_ENABLED = True
except ImportError:  # pragma: no cover
    _HTTP2_ENABLED = False


def _build_async_client() -> httpx.AsyncClient:
    """Create the shared outbound HTTP client with connection pooling enabled.

    Hardening (anti-fingerprint):
    * Sets a process-level default User-Agent so any call that forgets to
      override `headers=` still presents a realistic UA instead of httpx's
      default `python-httpx/x.x.x`.
    * Keeps connection pool size moderate.  50 keepalive connections is
      plenty for a single-instance proxy and avoids the "TLS session
      pinned to one client identity" smell of much larger pools.
    * Round 6: HTTP/2 enabled when the `h2` extra is installed, matching
      the ALPN behaviour of the official SDKs and Chrome.
    * Round 6: layered timeouts instead of a flat 600s scalar (see
      UPSTREAM_TIMEOUT above for rationale).
    """
    return httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        http2=_HTTP2_ENABLED,
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        ),
        headers={
            "User-Agent": pick_user_agent(None),
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


async def get_async_client() -> httpx.AsyncClient:
    """Return a process-wide AsyncClient so upstream requests can reuse connections."""
    global _async_client
    if _async_client is None or _async_client.is_closed:
        async with _client_lock:
            if _async_client is None or _async_client.is_closed:
                _async_client = _build_async_client()
    return _async_client


async def close_async_client():
    """Close the shared AsyncClient during application shutdown."""
    global _async_client
    async with _client_lock:
        if _async_client is not None and not _async_client.is_closed:
            await _async_client.aclose()
        _async_client = None
