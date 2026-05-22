import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_route_runtime():
    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi.HTTPException = HTTPException
    fake_fastapi.Request = object
    fake_fastapi.status = types.SimpleNamespace(HTTP_403_FORBIDDEN=403)

    fake_fastapi_responses = types.ModuleType("fastapi.responses")
    fake_fastapi_responses.StreamingResponse = type("StreamingResponse", (), {})

    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.WHITELIST_USER_AGENT = {"claude-code*"}

    fake_utils = types.ModuleType("app.utils")
    fake_utils.log = lambda *args, **kwargs: None

    fake_response = types.ModuleType("app.utils.response")
    fake_response.ensure_gemini_timing_fields = lambda data: data
    fake_response.openAI_from_Gemini = lambda *args, **kwargs: {"ok": True}

    fake_sse = types.ModuleType("app.utils.sse")
    fake_sse.sse_data = lambda payload: f"data: {payload}\n\n"
    fake_sse.sse_done = lambda: "data: [DONE]\n\n"

    sys.modules["fastapi"] = fake_fastapi
    sys.modules["fastapi.responses"] = fake_fastapi_responses
    sys.modules["app.config.settings"] = fake_settings
    sys.modules["app.utils"] = fake_utils
    sys.modules["app.utils.response"] = fake_response
    sys.modules["app.utils.sse"] = fake_sse

    spec = importlib.util.spec_from_file_location(
        "route_runtime_for_test", ROOT / "app/api/route_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RouteRuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_verify_user_agent_supports_wildcards(self):
        module = load_route_runtime()
        request = types.SimpleNamespace(headers={"User-Agent": "claude-code/1.0"})
        await module.verify_user_agent(request)

    async def test_verify_user_agent_rejects_non_matching_wildcard(self):
        module = load_route_runtime()
        request = types.SimpleNamespace(headers={"User-Agent": "other-client/1.0"})
        with self.assertRaises(module.HTTPException):
            await module.verify_user_agent(request)


if __name__ == "__main__":
    unittest.main()
