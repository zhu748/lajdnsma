import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_http_client_module():
    fake_httpx = types.ModuleType("httpx")

    class Timeout:
        def __init__(self, value):
            self.value = value

    class Limits:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class AsyncClient:
        created_count = 0

        def __init__(self, **kwargs):
            type(self).created_count += 1
            self.kwargs = kwargs
            self.is_closed = False

        async def aclose(self):
            self.is_closed = True

    fake_httpx.Timeout = Timeout
    fake_httpx.Limits = Limits
    fake_httpx.AsyncClient = AsyncClient
    sys.modules["httpx"] = fake_httpx

    spec = importlib.util.spec_from_file_location(
        "http_client",
        ROOT / "app/utils/http_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HttpClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "module"):
            await self.module.close_async_client()
        sys.modules.pop("httpx", None)

    async def test_get_async_client_reuses_instance(self):
        self.module = load_http_client_module()

        first = await self.module.get_async_client()
        second = await self.module.get_async_client()

        self.assertIs(first, second)

    async def test_close_async_client_resets_instance(self):
        self.module = load_http_client_module()

        first = await self.module.get_async_client()
        await self.module.close_async_client()
        second = await self.module.get_async_client()

        self.assertIsNot(first, second)
        self.assertTrue(first.is_closed)

    async def test_concurrent_get_async_client_builds_once(self):
        self.module = load_http_client_module()

        clients = await asyncio.gather(
            *[self.module.get_async_client() for _ in range(20)]
        )

        self.assertTrue(all(client is clients[0] for client in clients))
        self.assertEqual(sys.modules["httpx"].AsyncClient.created_count, 1)


if __name__ == "__main__":
    unittest.main()
