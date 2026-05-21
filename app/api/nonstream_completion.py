import asyncio

import app.config.settings as settings
from app.models.schemas import ChatCompletionRequest
from app.services import GeminiClient
from app.utils.error_handling import handle_gemini_error
from app.utils.gemini_response_processing import (
    finalize_gemini_response,
    select_safety_settings,
)
from app.utils.logging import log


async def _run_nonstream_completion(
    chat_request: ChatCompletionRequest,
    contents,
    system_instruction,
    current_api_key: str,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
    *,
    use_shield: bool = False,
    keepalive_interval: float | None = None,
):
    gemini_client = GeminiClient(current_api_key)
    gemini_task = asyncio.create_task(
        gemini_client.complete_chat(
            chat_request,
            contents,
            select_safety_settings(
                chat_request.model, safety_settings, safety_settings_g2
            ),
            system_instruction,
        )
    )

    awaited_task = asyncio.shield(gemini_task) if use_shield else gemini_task
    keepalive_task = None
    if keepalive_interval is not None:
        keepalive_task = asyncio.create_task(send_keepalive_messages(keepalive_interval))

    try:
        response_content = await awaited_task
        if keepalive_task is not None:
            keepalive_task.cancel()
        return await finalize_gemini_response(
            response_content,
            api_key=current_api_key,
            request_type="non-stream",
            model=chat_request.model,
            response_cache_manager=response_cache_manager,
            cache_key=cache_key,
        )
    except Exception as e:
        if keepalive_task is not None:
            keepalive_task.cancel()
        handle_gemini_error(e, current_api_key)
        return "error"


async def process_nonstream_request(
    chat_request: ChatCompletionRequest,
    contents,
    system_instruction,
    current_api_key: str,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
):
    return await _run_nonstream_completion(
        chat_request,
        contents,
        system_instruction,
        current_api_key,
        response_cache_manager,
        safety_settings,
        safety_settings_g2,
        cache_key,
        use_shield=True,
    )


async def process_nonstream_request_with_keepalive(
    chat_request: ChatCompletionRequest,
    contents,
    system_instruction,
    current_api_key: str,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
    keepalive_interval: float = 30.0,
):
    return await _run_nonstream_completion(
        chat_request,
        contents,
        system_instruction,
        current_api_key,
        response_cache_manager,
        safety_settings,
        safety_settings_g2,
        cache_key,
        keepalive_interval=keepalive_interval,
    )


async def send_keepalive_messages(interval: float):
    try:
        while True:
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log(
            "error",
            f"keepalive task error: {str(e)}",
            extra={"request_type": "non-stream", "keepalive": True},
        )


def build_nonstream_task(
    chat_request,
    contents,
    system_instruction,
    api_key,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key,
):
    if settings.NONSTREAM_KEEPALIVE_ENABLED:
        return asyncio.create_task(
            process_nonstream_request_with_keepalive(
                chat_request,
                contents,
                system_instruction,
                api_key,
                response_cache_manager,
                safety_settings,
                safety_settings_g2,
                cache_key,
                settings.NONSTREAM_KEEPALIVE_INTERVAL,
            )
        )

    return asyncio.create_task(
        process_nonstream_request(
            chat_request,
            contents,
            system_instruction,
            api_key,
            response_cache_manager,
            safety_settings,
            safety_settings_g2,
            cache_key,
        )
    )
