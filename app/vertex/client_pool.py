"""Bounded SDK clients, isolated by credential and released after active streams."""
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.utils.logging import vertex_log


@dataclass
class _Entry:
    client: object
    created: float
    active: int = 0
    retired: bool = False
    closer: object = None


_clients = OrderedDict()
_entries = {}
_closing = set()
_MAX_CLIENTS = 128
_TTL_SECONDS = 50 * 60


async def _close(entry):
    if _entries.pop(id(entry.client), None) is None:
        return
    if entry.closer is not None:
        await entry.closer(entry.client)
        return
    try:
        await entry.client.aio.aclose()
    except Exception:
        vertex_log('warning', '关闭 Vertex 异步客户端失败')
    finally:
        try:
            entry.client.close()
        except Exception:
            vertex_log('warning', '关闭 Vertex 同步客户端失败')


def _retire(entry):
    entry.retired = True
    if entry.active:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_close(entry))
    else:
        task = loop.create_task(_close(entry))
        _closing.add(task)
        task.add_done_callback(_closing.discard)


def get_client(key, factory, *, closer=None):
    now = time.monotonic()
    cached = _clients.pop(key, None)
    if cached and now - cached.created < _TTL_SECONDS:
        _clients[key] = cached
        return cached.client
    if cached:
        _retire(cached)
    client = factory()
    entry = _Entry(client, now, closer=closer)
    _clients[key] = entry
    _entries[id(client)] = entry
    while len(_clients) > _MAX_CLIENTS:
        _, old = _clients.popitem(last=False)
        _retire(old)
    return client


def retain(client):
    entry = _entries.get(id(client))
    if entry is None:
        # Unpooled clients (including test clients) close after this request.
        entry = _Entry(client, time.monotonic(), retired=True)
        _entries[id(client)] = entry
    entry.active += 1


async def release(client):
    entry = _entries.get(id(client))
    if entry is None:
        return
    entry.active -= 1
    if entry.retired and entry.active == 0:
        await _close(entry)


def reset(namespace=None):
    keys = [key for key in _clients if namespace is None or key[0] == namespace]
    for key in keys:
        _retire(_clients.pop(key))


async def shutdown(namespace=None):
    reset(namespace)
    if _closing:
        await asyncio.gather(*list(_closing))


async def stream_with_release(source, client):
    try:
        async for chunk in source:
            yield chunk
    finally:
        try:
            if hasattr(source, "aclose"):
                await source.aclose()
        finally:
            await release(client)
