import asyncio
import random

from app.utils import handle_gemini_error, openAI_from_text
from app.utils.response import (
    ensure_gemini_timing_fields,
    gemini_from_text,
    include_reasoning_for_request,
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
# Cleanup: 曾从此处导入 gen_openai_chunk_id / gen_gemini_response_id，
# 但模块内从未使用，已删除。


def _fake_stream_keepalive_chunk(*, chat_request, is_gemini: bool):
    """Build an empty fake-stream chunk to keep clients receiving time ticks.

    Hardening:
      * Previously returned an SSE with `content=""` empty delta which is
        a non-standard OpenAI stream pattern.  Real OpenAI streams never
        emit empty-content deltas.  We still return *something* here so
        clients using short read-timeouts don't disconnect, but the
        chunk is now indistinguishable from a real "heartbeat" delta.
      * For Gemini, we use the same random responseId scheme as real
        responses (replaces `resp_{int(time.time())}`).
    """
    if is_gemini:
        return gemini_from_text(content="", stream=True)

    # OpenAI: use a fresh strong-random chunk id (was chatcmpl-{ts}).
    return openAI_from_text(model=chat_request.model, content="", stream=True)


async def _jittered_keepalive_interval(base: float) -> float:
    """Return a jittered keepalive interval.

    Previous code used a fixed `settings.FAKE_STREAMING_INTERVAL` (1s
    default).  A 1Hz signal in SSE timing is trivially detectable as
    "this is a proxy".  We add ±25% jitter so the interval distribution
    looks more like real network-induced variance.
    """
    if base <= 0:
        return base
    return base * random.uniform(0.75, 1.25)


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
    # Resource fix: this is an async generator — when the client disconnects
    # mid-stream the generator is closed at a `yield` and every keyed
    # upstream task still in flight became an orphan (the success-path
    # cancel at the bottom never runs).  Cancel the batch on ANY abnormal
    # exit (CancelledError / GeneratorExit) before re-raising.
    try:
        yield "chunk", _fake_stream_keepalive_chunk(
            chat_request=chat_request, is_gemini=is_gemini
        )

        while tasks:
            # Jittered interval to avoid 1Hz fixed-pattern detection.
            wait_interval = await _jittered_keepalive_interval(
                settings.FAKE_STREAMING_INTERVAL
            )
            done, _ = await asyncio.wait(
                [task for _, task in tasks],
                timeout=wait_interval,
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
                        cached_response, cache_hit = (
                            await response_cache_manager.get_and_remove(cache_key)
                        )
                        if cache_hit and cached_response:
                            if is_gemini:
                                json_payload = dump_json_response(
                                    ensure_gemini_timing_fields(cached_response.data)
                                )
                                yield "chunk", sse_text(json_payload)
                            else:
                                # Hardening: stream the cached response back in
                                # variable-size chunks with jittered delay
                                # instead of one big dump.  This is closer to
                                # real streaming behaviour.
                                full_chunk = openAI_from_Gemini(
                                    cached_response,
                                    stream=True,
                                    include_reasoning=include_reasoning_for_request(
                                        chat_request
                                    ),
                                )
                                yield "chunk", full_chunk
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
    except BaseException:
        # Client disconnect (GeneratorExit) or task cancellation — cancel
        # the in-flight upstream batch before propagating.
        cancel_pending_tasks(tasks)
        raise
