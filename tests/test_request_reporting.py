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
    entry, = [e for e in logs.log_manager.get_recent_logs() if e.get("event") == "request_completed"]
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
    entry, = [e for e in logs.log_manager.get_recent_logs() if e.get("event") == "request_completed"]
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
    entry, = [e for e in logs.log_manager.get_recent_logs() if e.get("event") == "request_completed"]
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
    entry, = [e for e in logs.log_manager.get_recent_logs() if e.get("event") == "request_completed"]
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


def test_responses_reasoning_survives_terminal_replay(reporting):
    module, _ = reporting
    report = module.RequestReport(scope('/v1/responses'))
    report.observe({'type': 'response.reasoning_summary_text.delta', 'delta': '思考摘要'}, stream=True)
    payload = {'object': 'response', 'status': 'completed', 'output': [
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '思考摘要'}]},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': '结果'}]},
    ]}
    report.observe({'type': 'response.completed', 'response': payload}, stream=True)
    assert report.thinking_chars == 4
    assert report.thinking_preview == '思考摘要'
    other = module.RequestReport(scope())
    other.observe(payload)
    assert other.thinking_preview == '思考摘要'


def test_attempts_do_not_count_heartbeat_as_first_output(reporting):
    module, _ = reporting
    report = module.RequestReport(scope())
    attempt = report.begin_attempt('AI Studio', 'test', 'private-key', True)
    report.observe_attempt(attempt, {'choices': [{'delta': {}, 'finish_reason': None}]})
    assert report.upstream_first_output_ms is None
    report.observe_attempt(attempt, {'choices': [{'delta': {'reasoning_content': 'thinking'}}]})
    assert report.upstream_first_output_ms is not None
    assert report.upstream_first_text_ms is None
    report.observe_attempt(attempt, completion())
    report.end_attempt(attempt)
    report.end_attempt(attempt)
    assert report.upstream_active == 0
    assert report.snapshot()['upstream_usage']['total_tokens'] == 18
    assert 'private-key' not in str(report.snapshot())


def test_registry_counts_all_http_requests_and_survives_reset(reporting):
    module, _ = reporting
    registry = module.request_registry
    registry.reset()
    report = module.RequestReport(scope())
    registry.start(report)
    assert report.request_id in registry.active
    registry.reset()
    report.status, report.complete = 200, True
    report.emit()
    assert report.request_id not in registry.active
    assert registry.counts['completed'] == 0


def test_raw_unknown_usage_is_not_replaced_by_adapter_zero(reporting):
    module, _ = reporting
    report = module.RequestReport(scope())
    attempt = report.begin_attempt('Vertex AI', 'test', '', False)
    report.observe_attempt(attempt, {'candidates': [{'content': {'parts': [{'text': 'x'}]}}]})
    report.end_attempt(attempt)
    report.observe({'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}})
    assert report.snapshot()['usage']['total_tokens'] is None
    assert report.snapshot()['usage_source'] == '未知'


def test_nonstream_first_output_is_not_invented_and_usage_is_derived(reporting):
    module, _ = reporting
    report = module.RequestReport(scope())
    attempt = report.begin_attempt('Vertex AI', 'test', '', False)
    report.observe_attempt(attempt, {'usage': {'input_tokens': 4, 'output_tokens': 6}, 'choices': [{'message': {'content': 'x'}}]})
    report.end_attempt(attempt)
    assert report.upstream_first_output_ms is None
    assert report.snapshot()['usage']['total_tokens'] == 10
    assert report.snapshot()['usage_source'] == '推导'


def test_log_cursor_gap_and_request_correlation(reporting):
    module, logs = reporting
    report = module.RequestReport(scope())
    token = logs._current_request_report.set(report)
    try:
        logs.log('info', 'attempt')
        logs.vertex_log('info', 'vertex attempt')
    finally:
        logs._current_request_report.reset(token)
    feed = logs.get_log_feed()
    assert all(e['request_id'] == report.request_id for e in feed['logs'])
    cursor = feed['cursor']
    assert logs.get_log_feed(cursor)['logs'] == []
    for i in range(1001):
        logs.log_manager.add_log({'timestamp': 'test', 'message': str(i)})
    assert logs.get_log_feed(cursor)['gap']
    assert logs.get_log_feed(cursor)['capacity'] == 1000


def test_upstream_remains_visible_while_http_request_is_already_closed(reporting):
    module, _ = reporting
    report = module.RequestReport(scope())
    attempt = report.begin_attempt('Vertex AI', 'model', '', True)
    report.status, report.complete = 200, True
    report.emit()
    assert report.request_id not in module.request_registry.active
    assert module.request_registry.snapshot()['upstream_active'] >= 1
    report.end_attempt(attempt, asyncio.CancelledError())
    assert report.request_id not in module.request_registry.upstream_reports


def test_retry_wait_and_key_switch_have_reason_status_and_attempt_numbers(reporting):
    from types import SimpleNamespace
    module, logs = reporting
    report = module.RequestReport(scope())
    first = report.begin_attempt('AI Studio', 'model', 'private-key-a', False)
    error = RuntimeError('must not expose Bearer private-credential')
    error.response = SimpleNamespace(status_code=429)
    report.end_attempt(first, error)
    token = logs._current_request_report.set(report)
    try:
        logs.log('info', '等待重试', extra={'event': 'retry_wait', 'retry_delay': .25})
        logs.log('info', '选择密钥', extra={'event': 'key_decision', 'key': first['key'], 'decision': 'skip', 'reason': '密钥处于冷却'})
    finally:
        logs._current_request_report.reset(token)
    second = report.begin_attempt('AI Studio', 'model', 'private-key-b', False)
    report.observe_attempt(second, completion())
    report.end_attempt(second)
    wait = next(e for e in report.timeline if e['label'] == '重试退避')
    switch = next(e for e in report.timeline if e['label'] == '切换密钥')
    assert wait['retry_delay'] == .25 and wait['status_code'] == 429
    assert wait['next_attempt_number'] == 2
    assert switch['previous_key'] == first['key'] and switch['next_key'] == second['key']
    assert switch['attempt_number'] == 2 and switch['reason'] == '上游限流或配额耗尽'
    assert any(e.get('decision') == 'skip' for e in report.timeline)
    assert 'private-key' not in str(report.snapshot())
    assert 'private-credential' not in str(report.snapshot())


def test_transient_same_key_retry_is_distinct_from_model_fallback(reporting):
    module, _ = reporting
    report = module.RequestReport(scope())
    first = report.begin_attempt('Vertex AI', 'model-a', 'same-key', False)
    error = RuntimeError('sdk failure'); error.code = 503
    report.end_attempt(first, error)
    second = report.begin_attempt('Vertex AI', 'model-a', 'same-key', False)
    assert any(e['label'] == '再次请求上游' and e['status_code'] == 503 for e in report.timeline)
    report.end_attempt(second, error)
    third = report.begin_attempt('Vertex AI', 'model-b', 'same-key', False)
    assert any(e['label'] == '切换模型或后端' and e['next_model'] == 'model-b' for e in report.timeline)
    report.end_attempt(third, asyncio.CancelledError())


@pytest.mark.parametrize('payload,expected', [
    ({'choices': [{'delta': {}}]}, '空响应'),
    ({'choices': [{'delta': {'content': 'partial'}}]}, '响应不完整'),
    ({'error': {'message': 'failure'}}, '失败'),
])
def test_attempts_distinguish_empty_truncated_and_error_results(reporting, payload, expected):
    module, _ = reporting
    report = module.RequestReport(scope())
    attempt = report.begin_attempt('Vertex AI', 'model', '', True)
    report.observe_attempt(attempt, payload)
    report.end_attempt(attempt)
    assert attempt['outcome'] == expected
    assert attempt['reason']
    assert report.upstream_active == 0


def test_retry_wait_is_recorded_when_optional_pvp_is_unavailable(reporting):
    module, logs = reporting
    retry = load_file('retry_observation_test', 'app/utils/retry_state.py')
    report = module.RequestReport(scope())
    token = logs._current_request_report.set(report)
    try:
        with patch.dict(sys.modules, {'app.utils.pvp': None, 'app.utils.logging': logs}):
            assert asyncio.run(retry.elevate_pvp_backoff(.2)) == .2
    finally:
        logs._current_request_report.reset(token)
    assert report.timeline[-1]['label'] == '重试退避'
    assert report.timeline[-1]['retry_delay'] == .2


def test_upstream_observer_failure_cannot_replace_a_valid_response(reporting):
    module, logs = reporting
    upstream = load_file('upstream_observation_test', 'app/utils/upstream_reporting.py')
    report = module.RequestReport(scope())
    token = logs._current_request_report.set(report)
    class UnserializableResponse:
        def model_dump(self, **kwargs):
            raise ValueError('custom SDK serializer failed')
    value = UnserializableResponse()
    class Client:
        @upstream.observe_upstream('Vertex AI')
        async def complete(self, request):
            return value
    from types import SimpleNamespace
    try:
        with patch.dict(sys.modules, {'app.utils.logging': logs}):
            result = asyncio.run(Client().complete(SimpleNamespace(model='test')))
    finally:
        logs._current_request_report.reset(token)
    assert result is value
    assert report.attempts[0]['observation_incomplete']
    assert report.upstream_active == 0
