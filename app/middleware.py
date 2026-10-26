"""Streaming-safe ASGI middleware; no response buffering or task-group bridge."""
from starlette.responses import JSONResponse


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        rejection = JSONResponse(
            {"error": {"message": "Request body too large.", "type": "invalid_request_error"}},
            status_code=413,
        )
        headers = dict(scope.get("headers", []))
        try:
            declared_size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared_size = 0
        if declared_size > self.max_bytes:
            return await rejection(scope, receive, send)

        received = 0
        exceeded = False
        rejected = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal rejected
            if exceeded:
                # FastAPI may turn a receive exception into a parsing 400.
                # The size-limit response must still be 413, exactly once.
                if not rejected:
                    rejected = True
                    await rejection(scope, receive, send)
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not rejected:
                await rejection(scope, receive, send)


class ResponseHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"server", b"x-powered-by"}]
                message = {**message, "headers": [*headers, (b"server", b"cloudflare")]}
            await send(message)
        await self.app(scope, receive, send_headers)
