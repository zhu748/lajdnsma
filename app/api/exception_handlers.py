"""HTTP 请求校验失败也写入控制台和面板日志。"""
from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from app.utils.logging import log


async def handle_request_validation_error(request: Request, exc: RequestValidationError):
    # 校验发生在路由函数执行前，普通业务日志和 Exception 处理器收不到它。
    # 只记录字段位置和原因，不记录 input、请求正文、请求头或 URL 查询参数。
    errors = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )
    body = exc.body if isinstance(exc.body, dict) else {}
    model = body.get("model")
    log(
        "warning",
        f"请求参数校验失败: {request.method} {request.url.path} (HTTP 422)",
        extra={
            "status_code": 422,
            "request_type": "stream" if body.get("stream") is True else "non-stream",
            "model": model if isinstance(model, str) else "",
            "error_message": errors,
        },
    )
    # 保留 FastAPI 原有的 422 状态码及 detail 结构，兼容现有客户端。
    return await request_validation_exception_handler(request, exc)
