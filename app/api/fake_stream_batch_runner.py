import asyncio

from app.utils import handle_gemini_error, openAI_from_text
from app.utils.response import (
    ensure_gemini_timing_fields,
    gemini_from_text,
    openAI_from_Gemini,
)
from app.utils.response_loop_helpers import (
    dump_json_response,
    log_empty_response_count,
    log_request_failure,
    log_request_success,
)
from app.utils.retry_state import remove_completed_tasks
from app.utils.retry_state import cancel_pending_tasks
from app.utils.sse import sse_text


def _fake_stream_keepalive_chunk(*, chat_request, is_gemini: bool):
    """Build an empty fake-stream chunk to keep clients receiving time ticks."""
    if is_gemini:
        return gemini_from_text(content="", stream=True)

    return openAI_from_text(model=chat_request.model, content="", stream=True)


async def run_fake_stream_batch_until_success(
    *,
    tasks,
    tasks_map,
    chat_request,
    response_cache_manager,
    cache_key: str,
    is_gemini: bool,
    empty_response_count: int,
    settings,
):
    """Run a fake-stream batch and yield keepalive/final chunks plus a summary."""
    yield "chunk", _fake_stream_keepalive_chunk(
        chat_request=chat_request, is_gemini=is_gemini
    )

    while tasks:
        done, _ = await asyncio.wait(
            [task for _, task in tasks],
            timeout=settings.FAKE_STREAMING_INTERVAL,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            yield "chunk", _fake_stream_keepalive_chunk(
                chat_request=chat_request, is_gemini=is_gemini
            )
            continue

        for task in done:
            api_key = tasks_map[task]
            if task.cancelled():
                continue
            try:
                status = task.result()
                if status == "success":
                    log_request_success(
                        api_key,
                        request_type="fake-stream",
                        model=chat_request.model,
                        label="fake stream request success",
                    )
                    cached_response, cache_hit = await response_cache_manager.get_and_remove(
                        cache_key
                    )
                    if cache_hit and cached_response:
                        if is_gemini:
                            json_payload = dump_json_response(
                                ensure_gemini_timing_fields(cached_response.data)
                            )
                            yield "chunk", sse_text(json_payload)
                        else:
                            yield "chunk", openAI_from_Gemini(
                                cached_response,
                                stream=True,
                                include_reasoning=bool(
                                    getattr(chat_request, "enable_thinking", True)
                                ),
                            )
                        cancel_pending_tasks(tasks)
                        yield "summary", {
                            "success": True,
                            "empty_response_count": empty_response_count,
                            "tasks": tasks,
                        }
                        return

                if status == "empty":
                    empty_response_count += 1
                    log_empty_response_count(
                        api_key,
                        request_type="stream",
                        model=chat_request.model,
                        empty_response_count=empty_response_count,
                        max_empty_responses=settings.MAX_EMPTY_RESPONSES,
                    )
            except Exception as e:
                error_detail = handle_gemini_error(e, api_key)
                log_request_failure(
                    api_key,
                    request_type="stream",
                    model=chat_request.model,
                    error_detail=error_detail,
                )

        tasks = remove_completed_tasks(tasks)

    yield "summary", {
        "success": False,
        "empty_response_count": empty_response_count,
        "tasks": tasks,
    }
