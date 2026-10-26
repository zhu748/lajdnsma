"""Observe actual upstream attempts, including generators, without reading ahead."""
import asyncio
from contextvars import ContextVar
from functools import wraps
import sys

_current_attempt = ContextVar('upstream_attempt', default=None)


def _report():
    module = sys.modules.get('app.utils.logging')
    context = getattr(module, '_current_request_report', None)
    return context.get() if context else None


def _payload(value):
    if isinstance(value, dict):
        return value
    data = getattr(value, 'data', None)
    if isinstance(data, dict):
        return data
    if hasattr(value, 'model_dump'):
        # Diagnostics do not need JSON encoding. SDK thought signatures can be
        # arbitrary bytes and must never cause a generation to fail here.
        return value.model_dump(mode='python', by_alias=True, exclude_none=True)
    return {}


class Attempt:
    def __init__(self, backend, model='', key='', streaming=False):
        self.report = _report()
        self.record = self.report.begin_attempt(backend, model, key, streaming) if self.report else None

    def observe(self, value):
        if self.record:
            if getattr(value, 'object', None) == 'list':
                return  # Embedding adapter emits synthetic zeros; provider has no usage.
            try:
                self.report.observe_attempt(self.record, _payload(value))
            except Exception:
                self.record['observation_incomplete'] = True

    def finish(self, error=None):
        if self.record:
            self.report.end_attempt(self.record, error)


def observe_upstream(backend, *, streaming=False):
    def decorate(fn):
        if streaming:
            @wraps(fn)
            async def generator(self, request, *args, **kwargs):
                attempt = Attempt(backend, getattr(request, 'model', ''), getattr(self, 'api_key', ''), True)
                token = _current_attempt.set(attempt.record)
                source = fn(self, request, *args, **kwargs)
                error = None
                try:
                    async for chunk in source:
                        attempt.observe(chunk)
                        yield chunk
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    try:
                        await source.aclose()
                    finally:
                        attempt.finish(error)
                        _current_attempt.reset(token)
            return generator
        @wraps(fn)
        async def call(self, request, *args, **kwargs):
            attempt = Attempt(backend, getattr(request, 'model', ''), getattr(self, 'api_key', ''))
            token = _current_attempt.set(attempt.record)
            error = None
            try:
                result = await fn(self, request, *args, **kwargs)
                attempt.observe(result)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                attempt.finish(error)
                _current_attempt.reset(token)
        return call
    return decorate


async def tracked_sdk_call(method, *, backend='Vertex AI', **kwargs):
    streaming = bool(kwargs.get('stream')) or getattr(method, '__name__', '') == 'generate_content_stream'
    attempt = Attempt(backend, kwargs.get('model', ''), streaming=streaming)
    token = _current_attempt.set(attempt.record)
    try:
        result = await method(**kwargs)
        if not streaming:
            attempt.observe(result)
            attempt.finish()
            return result
    except BaseException as exc:
        attempt.finish(exc)
        raise
    finally:
        _current_attempt.reset(token)

    async def consume():
        token = _current_attempt.set(attempt.record)
        error = None
        try:
            async for chunk in result:
                attempt.observe(chunk)
                yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                if hasattr(result, 'aclose'):
                    await result.aclose()
                elif hasattr(result, 'close'):
                    await result.close()
            finally:
                attempt.finish(error)
                _current_attempt.reset(token)
    return consume()


def route_request(backend, request):
    report = _report()
    if report:
        report.backend = backend
        report.requested_model = report.requested_model or getattr(request, 'model', '')
        report.model = getattr(request, 'model', '') or report.model
        report.request_stream = bool(getattr(request, 'stream', False))
        report.event('路由确定', backend=backend, model=report.model)
