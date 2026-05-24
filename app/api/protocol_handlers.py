from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

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


def _resolve_claude_model_alias(request):
    """Map Claude model IDs to an available Gemini model for Anthropic clients."""
    try:
        import app.config.settings as settings
        from app.services import GeminiClient
        from app.utils.logging import log
    except ImportError:
        return

    if request.model in GeminiClient.AVAILABLE_MODELS:
        return

    fallback_model = select_alias_model(
        request.model, getattr(settings, "CLAUDE_MODEL_ALIASES", {}) or {}
    )
    if fallback_model and fallback_model not in GeminiClient.AVAILABLE_MODELS:
        fallback_model = ""

    if not fallback_model:
        fallback_model = settings.CLAUDE_DEFAULT_MODEL
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
            "Claude 模型名已映射到 Gemini 模型",
            extra={"model": original_model, "mapped_model": fallback_model},
        )


def _resolve_responses_model_alias(request):
    """Map OpenAI/Codex model IDs to an available Gemini model for Responses clients."""
    try:
        import app.config.settings as settings
        from app.services import GeminiClient
        from app.utils.logging import log
    except ImportError:
        return

    if request.model in GeminiClient.AVAILABLE_MODELS:
        return

    aliases = getattr(settings, "RESPONSES_MODEL_ALIASES", {}) or {}
    fallback_model = select_alias_model(request.model, aliases)
    if fallback_model and fallback_model not in GeminiClient.AVAILABLE_MODELS:
        fallback_model = ""

    if not fallback_model:
        fallback_model = settings.RESPONSES_DEFAULT_MODEL
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
            "Responses model mapped to Gemini model",
            extra={"model": original_model, "mapped_model": fallback_model},
        )


async def handle_responses_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    normalized_request = response_request_to_chat_request(payload)
    _resolve_responses_model_alias(normalized_request)
    try:
        response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)
    except HTTPException as exc:
        return JSONResponse(
            responses_error_response(str(exc.detail), exc.status_code),
            status_code=exc.status_code,
        )

    if isinstance(response, StreamingResponse):
        return StreamingResponse(
            openai_stream_to_responses_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
        )

    return openai_chat_to_response_api(response, payload)


async def handle_claude_messages_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    normalized_request = claude_request_to_chat_request(payload)
    _resolve_claude_model_alias(normalized_request)
    try:
        response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)
    except HTTPException as exc:
        return JSONResponse(
            anthropic_error_response(str(exc.detail), status_code=exc.status_code),
            status_code=exc.status_code,
        )

    if isinstance(response, StreamingResponse):
        return StreamingResponse(
            openai_stream_to_claude_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
        )

    return openai_chat_to_claude_response(response)
