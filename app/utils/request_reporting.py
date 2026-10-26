"""ASGI 请求结束摘要：旁路观察返回数据，不改写响应或预读请求正文。"""
import asyncio
import codecs
import io
import json
import os
import time
import uuid
import math
from app.utils.request_registry import request_registry, utc_now

from app.utils.logging import _current_request_report, log, redact_log_text


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
FINISH_LABELS = {
    "stop": "正常结束", "end_turn": "正常结束", "tool_calls": "调用工具",
    "tool_use": "调用工具", "length": "达到输出上限", "max_tokens": "达到输出上限",
    "max_output_tokens": "达到输出上限", "safety": "安全限制", "content_filter": "内容限制",
    "recitation": "引用限制", "error": "上游错误",
    "completed": "正常结束", "incomplete": "响应不完整", "failed": "请求失败",
}


def _preview_limit():
    try:
        return max(0, min(4096, int(os.environ.get("LOG_RESPONSE_PREVIEW_CHARS", "200"))))
    except ValueError:
        return 200


def should_report(scope):
    path = scope.get("path", "")
    return scope.get("method") == "POST" and (
        path.endswith(("/chat/completions", "/responses", "/messages", "/embeddings"))
        or path in {"/api/vector/query", "/api/vector/insert"}
        or ("/models/" in path and path.endswith((":generateContent", ":streamGenerateContent")))
    )


class RequestReport:
    def __init__(self, scope):
        self.started = time.perf_counter()
        self.received_at = utc_now()
        self.stage = "已接收"
        self.backend = "待路由"
        self.requested_model = ""
        self.request_stream = None
        self.timeline = []
        self.attempts = []
        self.attempt_count = 0
        self.upstream_active = 0
        self.first_output_ms = None
        self.first_text_ms = None
        self.upstream_first_output_ms = None
        self.upstream_first_text_ms = None
        self.authoritative_usage = None
        self.usage_source = "未知"
        self.ended_at = None
        self.completed_upstream_usage = dict(input_tokens=0, output_tokens=0, reasoning_tokens=0, total_tokens=0)
        self.completed_unknown_usage = 0
        self.request_id = uuid.uuid4().hex[:12]
        self.path = scope.get("path", "")
        self.model = self.path.split("/models/", 1)[-1].split(":", 1)[0] if "/models/" in self.path else ""
        self.status = 0
        self.media_type = ""
        self.source = "上游"
        self.terminal_failure = False
        self.upstream_error = False
        self.disconnected = False
        self.complete = False
        self.emitted = False
        self.finished_reason = ""
        self.tokens = dict(input_tokens=None, output_tokens=None, reasoning_tokens=None, total_tokens=None)
        self.preview_limit = _preview_limit()
        self.text_preview = ""
        self.thinking_preview = ""
        self.text_chars = 0
        self.thinking_chars = 0
        self.tool_ids = set()
        self.response_bytes = 0
        self.buffer = ""
        self.capture_truncated = False
        self.decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8-sig")("replace"), True)

    def elapsed(self):
        return round((time.perf_counter() - self.started) * 1000, 2)

    def append_event(self, label, **fields):
        fields = {name: redact_log_text(value)[:400] if isinstance(value, str) else value
                  for name, value in fields.items()}
        entry = {"label": label, "elapsed_ms": self.elapsed(), "timestamp": utc_now(), **fields}
        self.timeline.append(entry)
        self.timeline = self.timeline[-100:]

    def event(self, label, **fields):
        self.append_event(label, **fields)
        log("info", label, extra={"event": "request_stage", "stage": self.stage, **fields})

    def snapshot(self):
        usage = dict(self.tokens)
        if self.authoritative_usage is not None:
            usage = dict(self.authoritative_usage)
        elif self.attempts and self.source == "上游":
            successful = [a for a in self.attempts if a.get('outcome') == '完成']
            if successful:
                usage = dict(successful[-1]['usage'])
        derived = False
        if usage['total_tokens'] is None and usage['input_tokens'] is not None and usage['output_tokens'] is not None:
            usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
            derived = True
        source = '未知' if all(v is None for v in usage.values()) else '推导' if derived else '上游报告'
        if self.source == '缓存' and source != '未知':
            source = '缓存结果'
        upstream_usage = {key: self.completed_upstream_usage[key] + sum(a['usage'][key] or 0 for a in self.attempts if a['ended_ms'] is None) for key in usage}
        return {
            "request_id": self.request_id, "path": self.path, "model": self.model,
            "requested_model": self.requested_model, "backend": self.backend,
            "received_at": self.received_at, "ended_at": self.ended_at,
            "stage": self.stage, "outcome": getattr(self, 'outcome', ''),
            "source": self.source, "duration_ms": self.elapsed(),
            "first_output_ms": self.first_output_ms, "first_text_ms": self.first_text_ms,
            "upstream_first_output_ms": self.upstream_first_output_ms,
            "upstream_first_text_ms": self.upstream_first_text_ms,
            "request_type": 'stream' if any(a['streaming'] for a in self.attempts) else 'fake-stream' if self.media_type == 'text/event-stream' else 'non-stream',
            "usage": usage, "usage_source": source, "upstream_usage": upstream_usage,
            "unknown_usage_attempts": self.completed_unknown_usage + sum(a['usage']['total_tokens'] is None for a in self.attempts if a['ended_ms'] is None),
            "attempt_count": self.attempt_count, "attempts": self.attempts,
            "attempts_truncated": self.attempt_count > len(self.attempts),
            "timeline": self.timeline, "status_code": self.status,
            "finish_reason": self.finished_reason,
            "response_preview": redact_log_text(self.text_preview), "reasoning_preview": redact_log_text(self.thinking_preview),
            "response_chars": self.text_chars, "reasoning_chars": self.thinking_chars,
        }

    def begin_attempt(self, backend, model, key, streaming):
        previous = self.attempts[-1] if self.attempts else None
        self.attempt_count += 1
        self.upstream_active += 1
        request_registry.upstream_reports[self.request_id] = self
        self.backend, self.model = backend, model or self.model
        self.stage = "正在输出" if self.first_output_ms is not None else "等待上游"
        record = {"attempt_id": f"{self.request_id}-{self.attempt_count}", "backend": backend,
                  "number": self.attempt_count,
                  "model": model, "key": "key#" + str(hash(key) & 0xFFFFFF) if key else "",
                  "streaming": streaming, "started_ms": self.elapsed(), "ended_ms": None,
                  "first_output_ms": None, "first_text_ms": None, "outcome": "运行中",
                  "usage": dict.fromkeys(self.tokens), "content_seen": False,
                  "response_seen": False, "finish_seen": False}
        self.attempts.append(record)
        # Keep live attempts and a bounded completed history.
        if len(self.attempts) > 100:
            old = next((a for a in self.attempts if a['ended_ms'] is not None), None)
            if old is not None:
                self.attempts.remove(old)
        if previous and previous['ended_ms'] is not None:
            same_key = previous['key'] == record['key']
            same_model = previous['model'] == model and previous['backend'] == backend
            label = "切换模型或后端" if not same_model else "切换密钥" if not same_key else "再次请求上游"
            self.event(label, attempt_id=record['attempt_id'],
                       previous_attempt_id=previous['attempt_id'],
                       previous_key=previous['key'], next_key=record['key'],
                       previous_model=previous['model'], next_model=model,
                       reason=previous.get('reason') or '继续尝试其他响应路径',
                       status_code=previous.get('status_code'),
                       attempt_number=self.attempt_count)
        self.event("请求上游", attempt_id=record['attempt_id'], backend=backend, model=model,
                   key=record['key'], attempt_number=self.attempt_count)
        return record

    def observe_attempt(self, record, payload):
        record['response_seen'] = True
        probe = RequestReport({"path": self.path})
        probe.observe(payload)
        record['finish_seen'] |= bool(probe.finished_reason)
        if probe.terminal_failure:
            record['reason'] = '上游返回错误事件'
        for key, value in probe.tokens.items():
            if value is not None:
                record['usage'][key] = max(record['usage'][key] or 0, value)
        meaningful = probe.text_chars or probe.thinking_chars or probe.tool_ids
        record['content_seen'] |= bool(meaningful)
        if meaningful and record['first_output_ms'] is None and record['streaming']:
            record['first_output_ms'] = self.elapsed()
            if self.upstream_first_output_ms is None:
                self.upstream_first_output_ms = record['first_output_ms']
            self.event("上游开始输出", attempt_id=record['attempt_id'])
        if probe.text_chars and record['first_text_ms'] is None and record['streaming']:
            record['first_text_ms'] = self.elapsed()
            if self.upstream_first_text_ms is None:
                self.upstream_first_text_ms = record['first_text_ms']
            self.event("上游开始正文", attempt_id=record['attempt_id'])

    def end_attempt(self, record, error=None):
        if record['ended_ms'] is not None:
            return
        record['ended_ms'] = self.elapsed()
        record['outcome'] = "中断" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "失败" if error else "完成"
        record['error'] = type(error).__name__ if error else ''
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        if not isinstance(status, int):
            status = getattr(error, 'status_code', None) or getattr(error, 'code', None)
        record['status_code'] = status if isinstance(status, int) else None
        if error:
            record['reason'] = (
                '请求取消或连接中断' if record['outcome'] == '中断' else
                '上游限流或配额耗尽' if status == 429 else
                '上游认证失败或权限不足' if status in (401, 403) else
                '上游服务暂时不可用' if isinstance(status, int) and status >= 500 else
                '上游拒绝请求参数' if isinstance(status, int) and status >= 400 else
                '上游调用超时' if 'timeout' in type(error).__name__.lower() else
                '上游调用异常：' + type(error).__name__)
        elif record.get('reason'):
            record['outcome'] = '失败'
        elif record['response_seen'] and not record['content_seen'] and not record.get('observation_incomplete'):
            record['outcome'], record['reason'] = '空响应', '上游未返回正文、思考或工具调用'
        elif record['streaming'] and record['response_seen'] and not record['finish_seen'] and not record.get('observation_incomplete'):
            record['outcome'], record['reason'] = '响应不完整', '上游流缺少结束标记'
        else:
            record['reason'] = ''
        for name, value in record['usage'].items():
            self.completed_upstream_usage[name] += value or 0
        self.completed_unknown_usage += record['usage']['total_tokens'] is None
        self.upstream_active = max(0, self.upstream_active - 1)
        if not self.upstream_active:
            request_registry.upstream_reports.pop(self.request_id, None)
        self.event("上游调用结束", attempt_id=record['attempt_id'], outcome=record['outcome'],
                   reason=record['reason'], status_code=record['status_code'], key=record['key'])
        if record['outcome'] != '完成' and not self.upstream_active and self.first_output_ms is None:
            self.stage = "重试等待"

    def update_event(self, extra):
        if extra.get("model"):
            self.model = str(extra["model"])
        if extra.get("cache_hit"):
            self.source = "缓存"
        if extra.get("coalesced"):
            self.source = "复用"
            self.stage = "合并等待"
        if extra.get("retry_delay") is not None:
            if self.first_output_ms is None and not self.upstream_active:
                self.stage = "重试等待"
            delay = extra['retry_delay']
            if isinstance(delay, (int, float)) and not isinstance(delay, bool) and math.isfinite(delay) and delay >= 0:
                previous = self.attempts[-1] if self.attempts else {}
                self.append_event('重试退避', retry_delay=delay,
                                  reason=extra.get('reason') or previous.get('reason') or '上一批尝试未取得可用结果',
                                  attempt_id=previous.get('attempt_id', ''),
                                  status_code=extra.get('status_code') or previous.get('status_code'),
                                  next_attempt_number=self.attempt_count + 1)
        if extra.get('event') == 'key_decision':
            self.append_event('密钥选择', reason=extra.get('reason', ''),
                              key=extra.get('key', ''), decision=extra.get('decision', ''),
                              next_attempt_number=self.attempt_count + 1)
        if extra.get('event') == 'model_fallback':
            self.append_event('路由回退', reason=extra.get('reason', ''),
                              previous_model=extra.get('previous_model', ''),
                              next_model=extra.get('next_model', ''))
        self.terminal_failure |= bool(extra.get("terminal_failure"))
        self.upstream_error |= bool(extra.get("upstream_error"))
        if isinstance(extra.get("usage_metadata"), dict):
            self._usage(extra["usage_metadata"], native=True)
            self.authoritative_usage = dict(self.tokens)

    def _text(self, value, *, thinking=False, replace=False):
        if not isinstance(value, str):
            return
        if value:
            if self.first_output_ms is None:
                self.first_output_ms = self.elapsed()
            if not thinking and self.first_text_ms is None:
                self.first_text_ms = self.elapsed()
        preview = "thinking_preview" if thinking else "text_preview"
        count = "thinking_chars" if thinking else "text_chars"
        if replace:
            setattr(self, count, len(value))
            setattr(self, preview, value[:self.preview_limit])
        else:
            setattr(self, count, getattr(self, count) + len(value))
            remaining = self.preview_limit - len(getattr(self, preview))
            if remaining > 0:
                setattr(self, preview, getattr(self, preview) + value[:remaining])

    def _usage(self, usage, *, native=False):
        if not isinstance(usage, dict):
            return
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        values = {
            "input_tokens": usage.get("promptTokenCount") if native else usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("candidatesTokenCount") if native else usage.get("completion_tokens", usage.get("output_tokens")),
            "reasoning_tokens": usage.get("thoughtsTokenCount") if native else details.get("reasoning_tokens"),
            "total_tokens": usage.get("totalTokenCount") if native else usage.get("total_tokens"),
        }
        if native and isinstance(values["output_tokens"], int) and isinstance(values["reasoning_tokens"], int):
            values["output_tokens"] += values["reasoning_tokens"]
        for name, value in values.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                # 流式 usage 通常是累计值；同一结果在终止事件中重放不能重复累加。
                self.tokens[name] = max(self.tokens[name] or 0, value)

    def _content(self, content, *, replace=False):
        if isinstance(content, str):
            self._text(content, replace=replace)
        elif isinstance(content, list):
            text, thinking = [], []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "output_text"}:
                    text.append(part.get("text", ""))
                elif part.get("type") == "thinking":
                    thinking.append(part.get("thinking", ""))
                elif part.get("type") in {"tool_use", "function_call"}:
                    self.tool_ids.add(part.get("id") or part.get("name") or "tool")
            self._text("".join(text), replace=replace)
            self._text("".join(thinking), thinking=True, replace=replace)

    def observe(self, payload, *, stream=False):
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("model"), str):
            self.model = payload["model"]
        if payload.get("error") or payload.get("type") == "error" or payload.get("status") == "failed":
            self.terminal_failure = True
        self._usage(payload.get("usage"))
        self._usage(payload.get("usageMetadata"), native=True)
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("delta") if "delta" in choice else choice.get("message")
            if isinstance(message, dict):
                self._content(message.get("content"), replace=not stream)
                self._text(message.get("reasoning_content"), thinking=True, replace=not stream)
                for index, call in enumerate(message.get("tool_calls") or []):
                    if isinstance(call, dict):
                        self.tool_ids.add((choice.get("index", 0), call.get("index", index)))
            if choice.get("finish_reason"):
                self.finished_reason = str(choice["finish_reason"])
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            parts = (candidate.get("content") or {}).get("parts") or []
            for index, part in enumerate(parts):
                if isinstance(part, dict):
                    self._text(part.get("text"), thinking=bool(part.get("thought")))
                    if part.get("functionCall"):
                        self.tool_ids.add((candidate.get("index", 0), index, part["functionCall"].get("name")))
            if candidate.get("finishReason"):
                self.finished_reason = str(candidate["finishReason"])
        kind = payload.get("type", "")
        if kind == "response.output_text.delta":
            self._text(payload.get("delta"))
        elif kind == "response.reasoning_summary_text.delta":
            self._text(payload.get("delta"), thinking=True)
        elif kind in {"response.completed", "response.incomplete", "response.failed"}:
            self.observe(payload.get("response"), stream=False)
        elif kind == "content_block_delta":
            delta = payload.get("delta") or {}
            self._text(delta.get("text"))
            self._text(delta.get("thinking"), thinking=True)
        elif kind == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                self.tool_ids.add(block.get("id") or payload.get("index", 0))
        elif kind == "message_start":
            self.observe(payload.get("message"), stream=False)
        elif kind == "message_delta":
            self.finished_reason = (payload.get("delta") or {}).get("stop_reason") or self.finished_reason
        elif kind == "message":
            self._content(payload.get("content"), replace=True)
            self.finished_reason = payload.get("stop_reason") or self.finished_reason
        if payload.get("object") == "response":
            content = []
            reasoning = []
            for item in payload.get("output") or []:
                if isinstance(item, dict):
                    if item.get("type") == "message":
                        content.extend(item.get("content") or [])
                    elif item.get("type") == "reasoning":
                        reasoning.extend(part.get('text', '') for part in item.get('summary', []) if isinstance(part, dict))
                    elif item.get("type") == "function_call":
                        self.tool_ids.add(item.get("call_id") or item.get("id") or "tool")
            previous_thinking = (self.thinking_chars, self.thinking_preview)
            self._content(content, replace=True)
            if reasoning:
                self._text(''.join(reasoning), thinking=True, replace=True)
            else:
                self.thinking_chars, self.thinking_preview = previous_thinking
            self.finished_reason = (payload.get("incomplete_details") or {}).get("reason") or payload.get("status") or self.finished_reason

    def capture(self, body, final):
        self.response_bytes += len(body)
        if self.capture_truncated:
            return
        self.buffer += self.decoder.decode(body, final=final)
        if len(self.buffer) > MAX_CAPTURE_BYTES:
            self.buffer = ""
            self.capture_truncated = True
            return
        if self.media_type == "text/event-stream":
            while "\n\n" in self.buffer:
                event, self.buffer = self.buffer.split("\n\n", 1)
                data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                if data and data != "[DONE]":
                    try:
                        self.observe(json.loads(data), stream=True)
                    except (ValueError, TypeError, AttributeError):
                        continue
        elif final and self.buffer.strip():
            try:
                self.observe(json.loads(self.buffer))
            except (ValueError, TypeError, AttributeError):
                pass
            self.buffer = ""

    def emit(self):
        if self.emitted:
            return
        self.emitted = True
        failed = self.status >= 400 or self.terminal_failure
        self.disconnected = self.disconnected and not self.complete
        interrupted = self.disconnected or not self.complete or (self.upstream_error and not self.finished_reason)
        outcome = "中断" if self.disconnected else "失败" if failed else "中断" if interrupted else "成功"
        self.outcome = outcome
        self.stage = outcome
        self.ended_at = utc_now()
        self.timeline.append({'label': f'请求结束：{outcome}', 'elapsed_ms': self.elapsed(), 'timestamp': self.ended_at})
        self.timeline = self.timeline[-100:]
        record = self.snapshot()
        duration = record['duration_ms']
        known = lambda value: "未提供" if value is None else str(value)
        reason = FINISH_LABELS.get(self.finished_reason.lower(), self.finished_reason or "未提供")
        usage = record["usage"]
        message = (
            f"请求结束：{outcome}｜耗时 {duration / 1000:.3f} 秒｜来源 {self.source}｜"
            f"输入 {known(usage['input_tokens'])} token｜输出（含思考）{known(usage['output_tokens'])} token｜"
            f"思考 {known(usage['reasoning_tokens'])} token｜合计 {known(usage['total_tokens'])} token｜"
            f"返回正文 {self.text_chars} 字｜思考内容 {self.thinking_chars} 字｜"
            f"工具调用 {len(self.tool_ids)} 次｜结束原因 {reason}"
        )
        level = "error" if outcome == "失败" else "warning" if outcome != "成功" else "info"
        request_registry.finish(self, record)
        log(level, message, extra={
            "event": "request_completed", "request_id": self.request_id, "path": self.path,
            "model": self.model, "status_code": self.status,
            "request_type": "stream" if self.media_type == "text/event-stream" else "non-stream",
            "outcome": outcome, "source": self.source, "duration_ms": duration, "usage": usage,
            "response_chars": self.text_chars, "reasoning_chars": self.thinking_chars,
            "response_preview": redact_log_text(self.text_preview), "reasoning_preview": redact_log_text(self.thinking_preview),
            "tool_calls": len(self.tool_ids), "finish_reason": reason,
            "capture_truncated": self.capture_truncated,
        })


class RequestReportingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not should_report(scope):
            return await self.app(scope, receive, send)
        report = RequestReport(scope)
        context_token = _current_request_report.set(report)
        request_registry.start(report)
        report.event("收到请求")
        inbound = bytearray()

        async def observe_receive():
            message = await receive()
            if message['type'] == 'http.disconnect':
                report.disconnected = True
            elif message['type'] == 'http.request':
                if len(inbound) < 262144:
                    inbound.extend(message.get('body', b'')[:262144 - len(inbound)])
                if not message.get('more_body'):
                    try:
                        body = json.loads(inbound)
                        if isinstance(body, dict):
                            report.requested_model = body.get('model', '')
                            report.model = report.model or report.requested_model
                            report.request_stream = bool(body.get('stream', False))
                    except (ValueError, TypeError):
                        pass
                    inbound.clear()
            return message

        async def observe_send(message):
            if message["type"] == "http.response.start":
                report.status = message["status"]
                message = dict(message)
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'x-request-id'] + [(b'x-request-id', report.request_id.encode())]
                headers = dict(message.get("headers") or [])
                report.media_type = headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0]
            elif message["type"] == "http.response.body":
                try:
                    report.capture(message.get("body", b""), not message.get("more_body", False))
                except Exception:
                    report.capture_truncated = True
            if report.tool_ids and report.first_output_ms is None:
                report.first_output_ms = report.elapsed()
            await send(message)
            if report.first_output_ms is not None and report.stage not in {"正在输出", "成功", "失败", "中断"}:
                report.stage = "正在输出"
                report.event("开始返回有效内容")
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                report.complete = True

        try:
            await self.app(scope, observe_receive, observe_send)
        except (asyncio.CancelledError, GeneratorExit, OSError):
            report.disconnected = True
            raise
        except Exception:
            report.terminal_failure = True
            if not report.status:
                report.status = 500
            raise
        finally:
            try:
                try:
                    report.emit()
                except Exception:
                    pass  # 诊断失败不能改变已经发送的响应或吞掉取消信号。
            finally:
                _current_request_report.reset(context_token)
