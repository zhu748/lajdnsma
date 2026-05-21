import asyncio

import httpx


_async_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


def _build_async_client() -> httpx.AsyncClient:
    """Create the shared outbound HTTP client with connection pooling enabled."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(600.0),
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        ),
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
