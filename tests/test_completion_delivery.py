"""真实路由回归：任务结果不能依赖缓存保留，命中缓存也须遵守隐藏思考。"""
from pathlib import Path
import subprocess
import sys


def test_completed_responses_survive_cache_misses_and_preserve_visibility():
    # 与现有 sys.modules 假桩隔离；只模拟出站 API 和密钥选择。
    code = r'''
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.api import nonstream_handlers, stream_handlers, route_runtime
from app.services import GeminiClient
from app.services.gemini import GeminiResponseWrapper
from app.utils.auth import custom_verify_password
from app.utils.cache import ResponseCacheManager
from app.utils.request import ActiveRequestsManager
from app.utils.logging import log_manager
import app.config.settings as settings

calls = 0
async def complete(*args, **kwargs):
    global calls
    calls += 1
    return GeminiResponseWrapper({
        "candidates": [{"content": {"role": "model", "parts": [
            {"text": "secret-thought", "thought": True}, {"text": "答案"},
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "thoughtsTokenCount": 3, "totalTokenCount": 5},
    })

async def select(**kwargs):
    return ["test-key"]

class ConsumedCache(ResponseCacheManager):
    async def store(self, key, response):
        await super().store(key, response)
        await self.get_and_remove(key)

class BrokenCache(ResponseCacheManager):
    async def store(self, key, response):
        raise RuntimeError("simulated cache failure")

app.dependency_overrides[custom_verify_password] = lambda: True
app.dependency_overrides[route_runtime.verify_user_agent] = lambda: True
GeminiClient.AVAILABLE_MODELS = ["gemini-2.5-pro"]
settings.ENABLE_VERTEX = False
settings.PUBLIC_MODE = False
settings.MAX_RETRY_NUM = 1
settings.CONCURRENT_REQUESTS = 1
settings.MAX_EMPTY_RESPONSES = 3
settings.MAX_REQUESTS_PER_MINUTE = 1000
settings.MAX_REQUESTS_PER_DAY_PER_IP = 1000
settings.FAKE_STREAMING_INTERVAL = .001
settings.NONSTREAM_KEEPALIVE_INTERVAL = .001
route_runtime.key_manager = None
route_runtime.safety_settings = []
route_runtime.safety_settings_g2 = []

def request(client, stream, expose=False):
    before = {entry.get("request_id") for entry in log_manager.get_recent_logs(500) if entry.get("event") == "request_completed"}
    response = client.post("/v1/chat/completions", json={
        "model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}],
        "stream": stream, "expose_reasoning": expose,
    })
    summaries = [entry for entry in log_manager.get_recent_logs(500) if entry.get("event") == "request_completed" and entry["request_id"] not in before]
    assert len(summaries) == 1, summaries
    report = summaries[0]
    assert report["outcome"] == "成功", report
    assert report["usage"]["reasoning_tokens"] == 3, report
    assert report["usage"]["total_tokens"] == 5, report
    assert report["response_preview"] == "答案", report
    assert bool(report["reasoning_preview"]) == expose, report
    return response

with (
    patch.object(GeminiClient, "complete_chat", complete),
    patch.object(nonstream_handlers, "select_valid_api_keys", select),
    patch.object(stream_handlers, "select_valid_api_keys", select),
):
    client = TestClient(app)
    try:
        for stream, keepalive in ((False, False), (False, True), (True, False)):
            settings.NONSTREAM_KEEPALIVE_ENABLED = keepalive
            settings.FAKE_STREAMING = stream
            for cache_type, capacity in ((ResponseCacheManager, 0), (ConsumedCache, 20), (BrokenCache, 20)):
                route_runtime.response_cache_manager = cache_type(600, capacity)
                route_runtime.active_requests_manager = ActiveRequestsManager()
                before = calls
                response = request(client, stream)
                assert response.status_code == 200, response.text
                assert "答案" in response.text, response.text
                assert "secret-thought" not in response.text, response.text
                assert calls == before + 1, "a successful response was retried"

        settings.NONSTREAM_KEEPALIVE_ENABLED = False
        for stream in (False, True):
            for expose in (False, True):
                route_runtime.response_cache_manager = ResponseCacheManager(600, 20)
                before = calls
                first = request(client, False, expose)
                assert first.status_code == 200, first.text
                second = request(client, stream, expose)
                assert second.status_code == 200, second.text
                assert "答案" in second.text
                assert ("secret-thought" in second.text) == expose, second.text
                assert calls == before + 1, "the second request should hit cache"
                assert log_manager.get_recent_logs()[-1]["source"] == "缓存"

        # 协议适配可能不向客户端暴露 reasoning_tokens，日志仍须保留上游明确提供的思考用量。
        settings.NONSTREAM_KEEPALIVE_ENABLED = True
        for route, extra in (("/v1/responses", {"input": "hi"}), ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}]})):
            for stream in (False, True):
                route_runtime.response_cache_manager = ResponseCacheManager(600, 20)
                ids = {e.get("request_id") for e in log_manager.get_recent_logs(500) if e.get("event") == "request_completed"}
                response = client.post(route, json={"model": "gemini-2.5-pro", "stream": stream, **extra})
                assert response.status_code == 200, response.text
                reports = [e for e in log_manager.get_recent_logs(500) if e.get("event") == "request_completed" and e["request_id"] not in ids]
                assert len(reports) == 1, reports
                assert reports[0]["usage"]["reasoning_tokens"] == 3, reports
                assert reports[0]["usage"]["total_tokens"] == 5, reports
                assert reports[0]["response_preview"] == "答案", reports
    finally:
        client.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
