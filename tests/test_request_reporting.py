"""请求结束摘要：真实 ASGI 字节、各协议用量、断连、缓存与脱敏。"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reporting(monkeypatch):
    monkeypatch.setenv("LOG_RESPONSE_PREVIEW_CHARS", "200")
    logs = load_file("report_test_logs", "app/utils/logging.py")
    try:
        with patch.dict(sys.modules, {"app.utils.logging": logs}):
            module = load_file("report_test", "app/utils/request_reporting.py")
        yield module, logs
    finally:
        logs.logger.removeHandler(logs.console_handler)


def scope(path="/v1/chat/completions"):
    return {"type": "http", "method": "POST", "path": path, "headers": [(b"authorization", b"Bearer private-request-key")]}


def completion():
    return {
        "model": "test-model", "choices": [{"message": {"content": "你好 世界", "reasoning_content": "想一想"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18, "completion_tokens_details": {"reasoning_tokens": 3}},
    }


def test_json_summary_is_chinese_and_does_not_change_response(reporting):
    module, logs = reporting
    body = json.dumps(completion(), ensure_ascii=False).encode()
    cut = body.index("你".encode()) + 1
    sent = []

    async def app(s, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body[:cut], "more_body": True})
        await send({"type": "http.response.body", "body": body[cut:]})

    async def send(message):
        sent.append(message)

    asyncio.run(module.RequestReportingMiddleware(app)(scope(), None, send))
    assert b"".join(m.get("body", b"") for m in sent) == body
    entry, = logs.log_manager.get_recent_logs()
    assert entry["event"] == "request_completed"
    assert entry["level_label"] == "信息"
    assert entry["request_type_label"] == "非流式"
    assert entry["outcome"] == "成功"
    assert entry["usage"] == {"input_tokens": 10, "output_tokens": 8, "reasoning_tokens": 3, "total_tokens": 18}
    assert entry["response_chars"] == 5
    assert entry["reasoning_chars"] == 3
    assert entry["response_preview"] == "你好 世界"
    assert entry["reasoning_preview"] == "想一想"
    assert "输入 10 token" in entry["message"]
    assert "思考预览" in entry["formatted"]
    assert "private-request-key" not in str(entry)
    assert entry["duration_ms"] >= 0


@pytest.mark.parametrize("limit", ["0", "2", "invalid"])
def test_preview_is_optional_and_bounded(reporting, monkeypatch, limit):
    module, logs = reporting
    monkeypatch.setenv("LOG_RESPONSE_PREVIEW_CHARS", limit)
    report = module.RequestReport(scope())
    report.observe(completion())
    report.status, report.complete = 200, True
    report.emit()
    entry, = logs.log_manager.get_recent_logs()
    expected = 200 if limit == "invalid" else int(limit)
    assert len(entry["response_preview"]) <= expected
    assert entry["response_chars"] == 5


@pytest.mark.parametrize("protocol", ["openai", "gemini", "responses", "claude"])
def test_stream_reports_keep_cumulative_usage_without_double_counting(reporting, protocol):
    module, _ = reporting
    report = module.RequestReport(scope())
    report.media_type = "text/event-stream"
    if protocol == "openai":
        events = [
            {"choices": [{"delta": {"content": "你", "reasoning_content": "想"}}]},
            {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}},
        ]
    elif protocol == "gemini":
        events = [{"candidates": [{"content": {"parts": [{"text": "你好"}]}, "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3, "totalTokenCount": 18}}]
    elif protocol == "responses":
        events = [
            {"type": "response.output_text.delta", "delta": "你好"},
            {"type": "response.completed", "response": {"object": "response", "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "你好"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}}},
        ]
    else:
        events = [
            {"type": "message_start", "message": {"type": "message", "usage": {"input_tokens": 10}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 8}},
        ]
    wire = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events).encode()
    for index in range(len(wire)):
        report.capture(wire[index:index + 1], index == len(wire) - 1)
    assert report.text_chars == 2
    assert report.text_preview == "你好"
    assert report.tokens["input_tokens"] == 10
    assert report.tokens["output_tokens"] == 8
    assert report.finished_reason


def test_cache_and_terminal_failures_are_visible_even_with_http_200(reporting):
    module, logs = reporting
    report = module.RequestReport(scope())
    token = logs._current_request_report.set(report)
    try:
        logs.log("info", "缓存命中", extra={"cache_hit": True})
        logs.log("error", "所有密钥均失败", extra={"terminal_failure": True})
        report.status, report.complete = 200, True
        report.emit()
        report.emit()
    finally:
        logs._current_request_report.reset(token)
    summaries = [entry for entry in logs.log_manager.get_recent_logs() if entry.get("event") == "request_completed"]
    assert len(summaries) == 1
    assert summaries[0]["source"] == "缓存"
    assert summaries[0]["outcome"] == "失败"
    assert summaries[0]["usage"]["reasoning_tokens"] is None
    assert "思考 未提供 token" in summaries[0]["message"]


def test_cancellation_is_propagated_and_reported_once(reporting):
    module, logs = reporting

    async def app(*args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module.RequestReportingMiddleware(app)(scope(), None, None))
    entry, = logs.log_manager.get_recent_logs()
    assert entry["outcome"] in {"中断", "失败"}
    assert logs._current_request_report.get() is None


def test_large_capture_is_bounded_and_unknown_usage_is_not_zero(reporting):
    module, logs = reporting
    report = module.RequestReport(scope())
    report.capture(b"x" * (module.MAX_CAPTURE_BYTES + 1), True)
    assert report.buffer == ""
    assert report.capture_truncated
    report.status, report.complete = 200, True
    report.emit()
    entry, = logs.log_manager.get_recent_logs()
    assert entry["usage"]["total_tokens"] is None


def test_log_redaction_and_zero_count(reporting):
    _, logs = reporting
    secret = "ghp_" + "x" * 36
    logs.log("warning", f"请求失败 Bearer abcdef https://example.test?password=secret {secret}", extra={"error_message": "Bearer private"})
    entry, = logs.log_manager.get_recent_logs()
    assert "abcdef" not in str(entry)
    assert "password=secret" not in str(entry)
    assert secret not in str(entry)
    assert "private" not in entry["error_message"]
    assert "错误详情" in entry["formatted"]
    assert logs.log_manager.get_recent_logs(0) == []
    assert secret not in logs.redact_log_text("密钥" + secret + "不可用")
    assert "private-fragment" not in logs.redact_log_text("-----BEGIN PRIVATE KEY-----\nprivate-fragment")
    assert "abc123" not in logs.redact_log_text("Basic abc123")


def test_only_api_requests_are_reported(reporting):
    module, _ = reporting
    assert module.should_report(scope("/v1/responses"))
    assert module.should_report(scope("/v1beta/models/gemini:generateContent"))
    assert not module.should_report(scope("/api/update-config"))
    assert not module.should_report({**scope(), "method": "GET"})
