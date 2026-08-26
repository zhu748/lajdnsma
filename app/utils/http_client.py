import asyncio

import httpx

from app.utils.stealth import pick_user_agent


_async_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _build_async_client() -> httpx.AsyncClient:
    """Create the shared outbound HTTP client with connection pooling enabled.

    Hardening (anti-fingerprint):
    * Sets a process-level default User-Agent so any call that forgets to
      override `headers=` still presents a realistic UA instead of httpx's
      default `python-httpx/x.x.x`.
    * Keeps connection pool size moderate.  50 keepalive connections is
      plenty for a single-instance proxy and avoids the "TLS session
      pinned to one client identity" smell of much larger pools.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(600.0),
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
