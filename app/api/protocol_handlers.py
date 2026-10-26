"""Responses / Claude 协议的业务处理：请求转换与响应适配。"""
import json

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.utils.model_mapping import select_alias_model
from app.utils.protocol_adapters import (
    claude_request_to_chat_request,
    openai_chat_to_claude_response,
    openai_chat_to_response_api,
    openai_stream_to_claude_stream,
    openai_stream_to_responses_stream,
    response_request_to_chat_request,
    responses_error_response,
)
from app.utils.errors import anthropic_error_response


def _normalize_protocol_request(payload, converter):
    try:
        return converter(payload)
    except ValidationError as exc:
        # dict 入口在适配器内构造 Pydantic 模型；其校验失败同样属于
        # 客户端请求错误，交给统一的 422 日志处理器，而非全局 500。
        errors = [dict(error, loc=("body", *error["loc"])) for error in exc.errors()]
        raise RequestValidationError(errors, body=payload) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _adapt_json_keepalive(response, converter):
    """保留 JSON 空白保活，将最终完整 JSON 转成客户端协议。"""
    async def generate():
        try:
            chunks = []
            async for chunk in response.body_iterator:
                if not chunks and not chunk.strip():
                    yield chunk
                else:
                    chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk))
            payload = json.loads(b"".join(chunks))
            yield json.dumps(converter(payload), ensure_ascii=False)
        finally:
            close = getattr(response.body_iterator, "aclose", None)
            if close is not None:
                await close()

    return StreamingResponse(
        generate(), media_type="application/json",
        status_code=response.status_code, background=response.background,
    )


def _adapt_json_response(response, converter):
    """Handlers may return JSONResponse for upstream errors; preserve HTTP status."""
    if isinstance(response, JSONResponse):
        data = json.loads(response.body)
        if isinstance(data.get("error"), dict):
            data["error"]["code"] = response.status_code
        return JSONResponse(
            converter(data), status_code=response.status_code,
            headers={k: v for k, v in response.headers.items()
                     if k.lower() not in {"content-length", "content-type"}},
            background=response.background,
        )
    return converter(response)


def _map_to_available_model(request, aliases_attr: str, default_attr: str, log_message: str):
    """把客户端协议的模型名解析为可用 Gemini 模型（两级回退 + 白名单兑底）。

    Claude / Responses 两个协议共享的实现（历史版本两份同构 38 行）：
      1. 模型已在可用列表 → 原样返回；
      2. 别名表命中且目标可用 → 用别名目标；
      3. 协议默认模型可用 → 用默认；
      4. 白名单内首个可用模型兑底；全部失败则不改写。
    """
    try:
        import app.config.settings as settings
        from app.services import GeminiClient
        from app.utils.logging import log
    except ImportError:
        return

    if getattr(settings, "ENABLE_VERTEX", False):
        # Vertex has its own model catalog (and suffix variants). Never rewrite
        # a native model just because it is absent from AI Studio's catalog.
        from app.vertex.model_names import parse_model_request
        aliases = getattr(settings, aliases_attr, {}) or {}
        mapped = select_alias_model(request.model, aliases)
        if not mapped and not parse_model_request(request.model).base_model_name.startswith("gemini-"):
            mapped = getattr(settings, default_attr, "")
        if mapped:
            request.model = mapped
        return

    if request.model in GeminiClient.AVAILABLE_MODELS:
        return

    aliases = getattr(settings, aliases_attr, {}) or {}
    fallback_model = select_alias_model(request.model, aliases)
    if fallback_model and fallback_model not in GeminiClient.AVAILABLE_MODELS:
        fallback_model = ""

    if not fallback_model:
        fallback_model = getattr(settings, default_attr, "")
    if fallback_model and fallback_model not in GeminiClient.AVAILABLE_MODELS:
        fallback_model = ""

    if not fallback_model:
        whitelisted = [
            model
            for model in GeminiClient.AVAILABLE_MODELS
            if not settings.WHITELIST_MODELS or model in settings.WHITELIST_MODELS
        ]
        fallback_model = whitelisted[0] if whitelisted else ""

    if fallback_model:
        original_model = request.model
        request.model = fallback_model
        log(
            "info",
            log_message,
            extra={"model": original_model, "mapped_model": fallback_model},
        )


def _resolve_claude_model_alias(request):
    """Map Claude model IDs to an available Gemini model for Anthropic clients."""
    _map_to_available_model(
        request,
        "CLAUDE_MODEL_ALIASES",
        "CLAUDE_DEFAULT_MODEL",
        "Claude 模型名已映射到 Gemini 模型",
    )


def _resolve_responses_model_alias(request):
    """Map OpenAI/Codex model IDs to an available Gemini model for Responses clients."""
    _map_to_available_model(
        request,
        "RESPONSES_MODEL_ALIASES",
        "RESPONSES_DEFAULT_MODEL",
        "Responses 模型已映射到 Gemini 模型",
    )


async def handle_responses_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    try:
        normalized_request = _normalize_protocol_request(payload, response_request_to_chat_request)
        _resolve_responses_model_alias(normalized_request)
        response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)
    except HTTPException as exc:
        return JSONResponse(
            responses_error_response(str(exc.detail), exc.status_code),
            status_code=exc.status_code,
        )

    if isinstance(response, StreamingResponse):
        if response.media_type == "application/json":
            return _adapt_json_keepalive(
                response, lambda data: openai_chat_to_response_api(data, payload)
            )
        return StreamingResponse(
            openai_stream_to_responses_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
            status_code=response.status_code,
            background=response.background,
        )

    return _adapt_json_response(response, lambda data: openai_chat_to_response_api(data, payload))


async def handle_claude_messages_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    try:
        normalized_request = _normalize_protocol_request(payload, claude_request_to_chat_request)
        _resolve_claude_model_alias(normalized_request)
        response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)
    except HTTPException as exc:
        return JSONResponse(
            anthropic_error_response(str(exc.detail), status_code=exc.status_code),
            status_code=exc.status_code,
        )

    if isinstance(response, StreamingResponse):
        if response.media_type == "application/json":
            return _adapt_json_keepalive(response, openai_chat_to_claude_response)
        return StreamingResponse(
            openai_stream_to_claude_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
            status_code=response.status_code,
            background=response.background,
        )

    return _adapt_json_response(response, openai_chat_to_claude_response)
