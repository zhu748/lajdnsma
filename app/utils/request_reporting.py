"""ASGI 请求结束摘要：旁路观察返回数据，不改写响应或预读请求正文。"""
import asyncio
import codecs
import io
import json
import os
import time
import uuid

from app.utils.logging import _current_request_report, log


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

    def update_event(self, extra):
        if extra.get("cache_hit"):
            self.source = "缓存"
        self.terminal_failure |= bool(extra.get("terminal_failure"))
        self.upstream_error |= bool(extra.get("upstream_error"))
        self._usage(extra.get("usage_metadata"), native=True)

    def _text(self, value, *, thinking=False, replace=False):
        if not isinstance(value, str):
            return
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
        if payload.get("error") or payload.get("type") == "error":
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
            for item in payload.get("output") or []:
                if isinstance(item, dict):
                    if item.get("type") == "message":
                        content.extend(item.get("content") or [])
                    elif item.get("type") == "function_call":
                        self.tool_ids.add(item.get("call_id") or item.get("id") or "tool")
            self._content(content, replace=True)
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
        interrupted = self.disconnected or not self.complete or (self.upstream_error and not self.finished_reason)
        outcome = "中断" if self.disconnected else "失败" if failed else "中断" if interrupted else "成功"
        duration = round((time.perf_counter() - self.started) * 1000, 2)
        known = lambda value: "未提供" if value is None else str(value)
        reason = FINISH_LABELS.get(self.finished_reason.lower(), self.finished_reason or "未提供")
        usage = self.tokens
        message = (
            f"请求结束：{outcome}｜耗时 {duration / 1000:.3f} 秒｜来源 {self.source}｜"
            f"输入 {known(usage['input_tokens'])} token｜输出（含思考）{known(usage['output_tokens'])} token｜"
            f"思考 {known(usage['reasoning_tokens'])} token｜合计 {known(usage['total_tokens'])} token｜"
            f"返回正文 {self.text_chars} 字｜思考内容 {self.thinking_chars} 字｜"
            f"工具调用 {len(self.tool_ids)} 次｜结束原因 {reason}"
        )
        level = "error" if failed and self.status >= 500 else "warning" if outcome != "成功" else "info"
        log(level, message, extra={
            "event": "request_completed", "request_id": self.request_id, "path": self.path,
            "model": self.model, "status_code": self.status,
            "request_type": "stream" if self.media_type == "text/event-stream" else "non-stream",
            "outcome": outcome, "source": self.source, "duration_ms": duration, "usage": usage,
            "response_chars": self.text_chars, "reasoning_chars": self.thinking_chars,
            "response_preview": self.text_preview, "reasoning_preview": self.thinking_preview,
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

        async def observe_send(message):
            if message["type"] == "http.response.start":
                report.status = message["status"]
                headers = dict(message.get("headers") or [])
                report.media_type = headers.get(b"content-type", b"").decode("latin-1").split(";", 1)[0]
            elif message["type"] == "http.response.body":
                try:
                    report.capture(message.get("body", b""), not message.get("more_body", False))
                except Exception:
                    report.capture_truncated = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                report.complete = True

        try:
            await self.app(scope, receive, observe_send)
        except (asyncio.CancelledError, GeneratorExit):
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
