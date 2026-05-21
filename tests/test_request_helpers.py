import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_request_helpers():
    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_status = types.SimpleNamespace(HTTP_400_BAD_REQUEST=400)
    fake_fastapi.HTTPException = HTTPException
    fake_fastapi.status = fake_status

    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.PRECISE_CACHE = False
    fake_settings.CALCULATE_CACHE_ENTRIES = 3
    fake_settings.NONSTREAM_KEEPALIVE_ENABLED = False

    fake_utils = types.ModuleType("app.utils")
    log_calls = []

    def generate_cache_key(request, last_n_messages=65536, is_gemini=False):
        return f"cache::{request.model}::{last_n_messages}::{is_gemini}"

    def log(level, message, extra=None):
        log_calls.append((level, message, extra))

    fake_utils.generate_cache_key = generate_cache_key
    fake_utils.log = log

    sys.modules["fastapi"] = fake_fastapi
    sys.modules["app.config.settings"] = fake_settings
    sys.modules["app.utils"] = fake_utils

    spec = importlib.util.spec_from_file_location(
        "request_helpers", ROOT / "app/api/request_helpers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, fake_settings, log_calls, HTTPException


class RequestHelpersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_basic_request_helpers(self):
        module, settings, _, HTTPException = load_request_helpers()
        req = types.SimpleNamespace(model="gemini-2.5-pro", format_type="gemini")

        self.assertTrue(module.is_gemini_request(req))
        self.assertEqual(
            module.build_request_cache_key(req, is_gemini=True),
            "cache::gemini-2.5-pro::3::True",
        )

        settings.PRECISE_CACHE = True
        self.assertEqual(
            module.build_request_cache_key(req, is_gemini=False),
            "cache::gemini-2.5-pro::65536::False",
        )

        with self.assertRaises(HTTPException):
            module.ensure_model_available("bad-model", ["good-model"])

    async def test_wait_for_existing_task(self):
        module, _, _, _ = load_request_helpers()

        async def done_task():
            return {"ok": True}

        task = asyncio.create_task(done_task())
        await task

        class Manager:
            def __init__(self):
                self.removed = []

            def get(self, key):
                return task

            def remove(self, key):
                self.removed.append(key)

        result = await module.wait_for_existing_task(
            Manager(), "pool-key", types.SimpleNamespace(stream=False, model="m")
        )
        self.assertIsNone(result)

    async def test_create_processing_task(self):
        module, settings, _, _ = load_request_helpers()

        async def fake_stream(**kwargs):
            return ("stream", kwargs["cache_key"])

        async def fake_keepalive(**kwargs):
            return ("keepalive", kwargs["is_gemini"])

        async def fake_nonstream(**kwargs):
            return ("nonstream", kwargs["cache_key"])

        req = types.SimpleNamespace(stream=True, model="m")
        task = module.create_processing_task(
            req,
            is_gemini=False,
            key_manager="km",
            response_cache_manager="rcm",
            safety_settings="s1",
            safety_settings_g2="s2",
            cache_key="c1",
            process_stream_request=fake_stream,
            process_nonstream_with_keepalive_stream=fake_keepalive,
            process_request=fake_nonstream,
        )
        self.assertEqual(await task, ("stream", "c1"))

        settings.NONSTREAM_KEEPALIVE_ENABLED = True
        req.stream = False
        task = module.create_processing_task(
            req,
            is_gemini=True,
            key_manager="km",
            response_cache_manager="rcm",
            safety_settings="s1",
            safety_settings_g2="s2",
            cache_key="c2",
            process_stream_request=fake_stream,
            process_nonstream_with_keepalive_stream=fake_keepalive,
            process_request=fake_nonstream,
        )
        self.assertEqual(await task, ("keepalive", True))


if __name__ == "__main__":
    unittest.main()
