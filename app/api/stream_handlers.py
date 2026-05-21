import asyncio
from fastapi.responses import StreamingResponse

import app.config.settings as settings
from app.api.fake_stream_batch_runner import run_fake_stream_batch_until_success
from app.api.fake_stream_handlers import handle_fake_streaming
from app.api.native_stream_handlers import generate_native_stream_chunks
from app.models.schemas import ChatCompletionRequest
from app.utils.api_key_selection import select_valid_api_keys
from app.utils.empty_response import (
    build_empty_limit_response,
    log_empty_response_limit,
)
from app.utils.request_format import prepare_request_messages
from app.utils.response_loop_helpers import (
    build_all_keys_failed_response,
    build_keyed_tasks,
    log_all_keys_failed,
    log_concurrency_increase,
)
from app.utils.retry_state import (
    increase_concurrency,
    next_batch_size,
    reached_empty_response_limit,
    should_continue_retry,
)
from app.utils.sse import sse_done


async def generate_fake_stream_response(
    *,
    chat_request,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
    is_gemini: bool,
    contents,
    system_instruction,
):
    current_concurrent = settings.CONCURRENT_REQUESTS
    max_retry_num = settings.MAX_RETRY_NUM
    current_try_num = 0
    empty_response_count = 0

    while should_continue_retry(
        current_try_num, max_retry_num, empty_response_count, settings.MAX_EMPTY_RESPONSES
    ):
        batch_num = next_batch_size(current_try_num, max_retry_num, current_concurrent)
        valid_keys = await select_valid_api_keys(
            key_manager=key_manager,
            batch_num=batch_num,
            request_type="stream",
            model=chat_request.model,
        )
        if not valid_keys:
            break

        current_try_num += len(valid_keys)
        tasks, tasks_map = build_keyed_tasks(
            valid_keys,
            request_type="fake-stream",
            model=chat_request.model,
            label="fake stream request start",
            task_factory=lambda api_key: asyncio.create_task(
                handle_fake_streaming(
                    api_key,
                    chat_request,
                    contents,
                    response_cache_manager,
                    system_instruction,
                    safety_settings,
                    safety_settings_g2,
                    cache_key,
                )
            ),
        )

        batch_summary = {"success": False, "empty_response_count": empty_response_count}
        async for item_type, payload in run_fake_stream_batch_until_success(
            tasks=tasks,
            tasks_map=tasks_map,
            chat_request=chat_request,
            response_cache_manager=response_cache_manager,
            cache_key=cache_key,
            is_gemini=is_gemini,
            empty_response_count=empty_response_count,
            settings=settings,
        ):
            if item_type == "chunk":
                yield payload
            else:
                batch_summary = payload

        empty_response_count = batch_summary["empty_response_count"]
        if batch_summary["success"]:
            return

        if reached_empty_response_limit(empty_response_count, settings.MAX_EMPTY_RESPONSES):
            log_empty_response_limit(
                empty_response_count,
                settings.MAX_EMPTY_RESPONSES,
                "fake-stream",
                chat_request.model,
            )
            yield build_empty_limit_response(
                is_gemini=is_gemini, model=chat_request.model, stream=True
            )
            return

        current_concurrent = increase_concurrency(
            current_concurrent,
            settings.INCREASE_CONCURRENT_ON_FAILURE,
            settings.MAX_CONCURRENT_REQUESTS,
        )
        log_concurrency_increase(
            request_type="stream",
            model=chat_request.model,
            current_concurrent=current_concurrent,
            label="all fake stream requests failed, increased concurrency",
        )

    log_all_keys_failed(request_type="stream", model=chat_request.model, key="ALL")
    yield build_all_keys_failed_response(
        is_gemini=is_gemini, model=chat_request.model, stream=True
    )


async def generate_native_stream_response(
    *,
    chat_request,
    key_manager,
    safety_settings,
    safety_settings_g2,
    is_gemini: bool,
    contents,
    system_instruction,
):
    max_retry_num = settings.MAX_RETRY_NUM
    current_try_num = 0
    empty_response_count = 0

    while should_continue_retry(
        current_try_num, max_retry_num, empty_response_count, settings.MAX_EMPTY_RESPONSES
    ):
        valid_keys = await select_valid_api_keys(
            key_manager=key_manager,
            batch_num=1,
            request_type="stream",
            model=chat_request.model,
        )
        if not valid_keys:
            break

        current_try_num += 1
        api_key = valid_keys[0]
        stream_summary = {"success": False, "empty": False, "token": 0}
        async for item_type, payload in generate_native_stream_chunks(
            api_key=api_key,
            chat_request=chat_request,
            contents=contents,
            system_instruction=system_instruction,
            safety_settings=safety_settings,
            safety_settings_g2=safety_settings_g2,
            is_gemini=is_gemini,
            settings=settings,
        ):
            if item_type == "chunk":
                yield payload
            else:
                stream_summary = payload

        if stream_summary["empty"]:
            empty_response_count += 1

        if stream_summary["success"]:
            return

        if reached_empty_response_limit(empty_response_count, settings.MAX_EMPTY_RESPONSES):
            log_empty_response_limit(
                empty_response_count,
                settings.MAX_EMPTY_RESPONSES,
                "stream",
                chat_request.model,
            )
            yield build_empty_limit_response(
                is_gemini=is_gemini, model=chat_request.model, stream=True
            )
            return

    log_all_keys_failed(request_type="stream", model=chat_request.model, key="ALL")
    yield build_all_keys_failed_response(
        is_gemini=is_gemini, model=chat_request.model, stream=True
    )


async def stream_response_generator(
    chat_request,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
):
    is_gemini, contents, system_instruction = prepare_request_messages(chat_request)
    if settings.FAKE_STREAMING:
        async for chunk in generate_fake_stream_response(
            chat_request=chat_request,
            key_manager=key_manager,
            response_cache_manager=response_cache_manager,
            safety_settings=safety_settings,
            safety_settings_g2=safety_settings_g2,
            cache_key=cache_key,
            is_gemini=is_gemini,
            contents=contents,
            system_instruction=system_instruction,
        ):
            yield chunk
        if not is_gemini:
            yield sse_done()
        return

    async for chunk in generate_native_stream_response(
        chat_request=chat_request,
        key_manager=key_manager,
        safety_settings=safety_settings,
        safety_settings_g2=safety_settings_g2,
        is_gemini=is_gemini,
        contents=contents,
        system_instruction=system_instruction,
    ):
        yield chunk
    if not is_gemini:
        yield sse_done()


async def process_stream_request(
    chat_request: ChatCompletionRequest,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
) -> StreamingResponse:
    """处理流式 API 请求。"""
    return StreamingResponse(
        stream_response_generator(
            chat_request,
            key_manager,
            response_cache_manager,
            safety_settings,
            safety_settings_g2,
            cache_key,
        ),
        media_type="text/event-stream",
    )
