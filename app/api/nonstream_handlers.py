import asyncio

from fastapi.responses import StreamingResponse

import app.config.settings as settings
from app.api.nonstream_batch_runner import run_nonstream_batch_until_success
from app.api.nonstream_completion import (
    build_nonstream_task,
    process_nonstream_request,
)
from app.utils.api_key_selection import select_valid_api_keys
from app.utils.empty_response import (
    build_empty_limit_response,
    log_empty_response_limit,
)
from app.utils.logging import log
from app.utils.request_format import prepare_request_messages
from app.utils.response_loop_helpers import (
    build_all_keys_failed_response,
    build_keyed_tasks,
    dump_json_response,
    log_all_keys_failed,
    log_concurrency_increase,
)
from app.utils.retry_state import (
    compute_inter_batch_backoff,
    increase_concurrency,
    decrease_concurrency,
    next_batch_size,
    reached_empty_response_limit,
    should_continue_retry,
)


async def process_request(
    chat_request,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
):
    is_gemini, contents, system_instruction = prepare_request_messages(chat_request)
    current_concurrent = settings.CONCURRENT_REQUESTS
    max_retry_num = settings.MAX_RETRY_NUM
    current_try_num = 0
    empty_response_count = 0
    # Round 4: 已失败的批次计数，用于批间 full-jitter 退避（防重试风暴）
    failed_batches = 0

    while should_continue_retry(
        current_try_num, max_retry_num, empty_response_count, settings.MAX_EMPTY_RESPONSES
    ):
        # 批间退避：仅在已经失败过至少一批、且循环还会继续时才等待，
        # 首个批次与成功路径零延迟。
        if failed_batches:
            await asyncio.sleep(compute_inter_batch_backoff(failed_batches))

        batch_num = next_batch_size(current_try_num, max_retry_num, current_concurrent)
        valid_keys = await select_valid_api_keys(
            key_manager=key_manager,
            batch_num=batch_num,
            request_type="non-stream",
            model=chat_request.model,
        )
        if not valid_keys:
            break

        current_try_num += len(valid_keys)
        tasks, tasks_map = build_keyed_tasks(
            valid_keys,
            request_type="non-stream",
            model=chat_request.model,
            label="non-stream request start",
            task_factory=lambda api_key: build_nonstream_task(
                chat_request,
                contents,
                system_instruction,
                api_key,
                response_cache_manager,
                safety_settings,
                safety_settings_g2,
                cache_key,
            ),
        )

        batch_result = await run_nonstream_batch_until_success(
            tasks=tasks,
            tasks_map=tasks_map,
            chat_request=chat_request,
            response_cache_manager=response_cache_manager,
            cache_key=cache_key,
            is_gemini=is_gemini,
            empty_response_count=empty_response_count,
        )
        empty_response_count = batch_result["empty_response_count"]
        if batch_result["status"] == "success":
            return batch_result["response"]

        # 批次失败：计数 +1，下一轮循环顶部会先退避
        failed_batches += 1

        if valid_keys:
            # Hardening: previously this called increase_concurrency on
            # failure, which from the upstream provider's perspective looks
            # exactly like a request-rate doubling DDoS pattern.  We now do
            # the opposite: reduce concurrency on failure to let upstream
            # recover.  The old "increase" path is preserved only for the
            # explicit case where the operator set
            # INCREASE_CONCURRENT_ON_FAILURE > 0 (legacy opt-in).
            if settings.INCREASE_CONCURRENT_ON_FAILURE > 0:
                current_concurrent = increase_concurrency(
                    current_concurrent,
                    settings.INCREASE_CONCURRENT_ON_FAILURE,
                    settings.MAX_CONCURRENT_REQUESTS,
                )
            else:
                current_concurrent = decrease_concurrency(current_concurrent, 1)
            log_concurrency_increase(
                request_type="non-stream",
                model=chat_request.model,
                current_concurrent=current_concurrent,
            )

        if reached_empty_response_limit(empty_response_count, settings.MAX_EMPTY_RESPONSES):
            log_empty_response_limit(
                empty_response_count,
                settings.MAX_EMPTY_RESPONSES,
                "non-stream",
                chat_request.model,
            )
            return build_empty_limit_response(
                is_gemini=is_gemini, model=chat_request.model, stream=False
            )

    log_all_keys_failed(request_type="switch_key", model=chat_request.model)
    return build_all_keys_failed_response(
        is_gemini=is_gemini, model=chat_request.model, stream=False
    )


async def process_nonstream_with_keepalive_stream(
    chat_request,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
    is_gemini: bool,
):
    async def keepalive_stream_generator():
        try:
            _, contents, system_instruction = prepare_request_messages(chat_request)
            current_concurrent = settings.CONCURRENT_REQUESTS
            max_retry_num = settings.MAX_RETRY_NUM
            current_try_num = 0
            empty_response_count = 0
            # Round 4: 批间退避计数（与 process_request 同策略）
            failed_batches = 0

            while should_continue_retry(
                current_try_num,
                max_retry_num,
                empty_response_count,
                settings.MAX_EMPTY_RESPONSES,
            ):
                # 批间退避：cap 8s，远小于 keepalive 间隔（默认 30s），
                # 不会造成客户端断连；此处不做分片 yield，保持简单。
                if failed_batches:
                    await asyncio.sleep(compute_inter_batch_backoff(failed_batches))

                batch_num = next_batch_size(
                    current_try_num, max_retry_num, current_concurrent
                )
                valid_keys = await select_valid_api_keys(
                    key_manager=key_manager,
                    batch_num=batch_num,
                    request_type="non-stream",
                    model=chat_request.model,
                )
                if not valid_keys:
                    break

                current_try_num += len(valid_keys)
                tasks, tasks_map = build_keyed_tasks(
                    valid_keys,
                    request_type="non-stream",
                    model=chat_request.model,
                    label="non-stream request start",
                    task_factory=lambda api_key: asyncio.create_task(
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
                    ),
                )

                batch_result = await run_nonstream_batch_until_success(
                    tasks=tasks,
                    tasks_map=tasks_map,
                    chat_request=chat_request,
                    response_cache_manager=response_cache_manager,
                    cache_key=cache_key,
                    is_gemini=is_gemini,
                    empty_response_count=empty_response_count,
                    wait_timeout=settings.NONSTREAM_KEEPALIVE_INTERVAL,
                    serialize_json=True,
                )
                empty_response_count = batch_result["empty_response_count"]
                while batch_result["status"] == "pending":
                    yield "\n"
                    tasks = batch_result["tasks"]
                    batch_result = await run_nonstream_batch_until_success(
                        tasks=tasks,
                        tasks_map=tasks_map,
                        chat_request=chat_request,
                        response_cache_manager=response_cache_manager,
                        cache_key=cache_key,
                        is_gemini=is_gemini,
                        empty_response_count=empty_response_count,
                        wait_timeout=settings.NONSTREAM_KEEPALIVE_INTERVAL,
                        serialize_json=True,
                    )
                    empty_response_count = batch_result["empty_response_count"]

                if batch_result["status"] == "success":
                    yield batch_result["response"]
                    return

                # 批次失败：计数 +1，下一轮循环顶部会先退避
                failed_batches += 1

                if valid_keys:
                    if settings.INCREASE_CONCURRENT_ON_FAILURE > 0:
                        current_concurrent = increase_concurrency(
                            current_concurrent,
                            settings.INCREASE_CONCURRENT_ON_FAILURE,
                            settings.MAX_CONCURRENT_REQUESTS,
                        )
                    else:
                        current_concurrent = decrease_concurrency(current_concurrent, 1)
                    log_concurrency_increase(
                        request_type="non-stream",
                        model=chat_request.model,
                        current_concurrent=current_concurrent,
                    )

                if reached_empty_response_limit(
                    empty_response_count, settings.MAX_EMPTY_RESPONSES
                ):
                    log_empty_response_limit(
                        empty_response_count,
                        settings.MAX_EMPTY_RESPONSES,
                        "non-stream",
                        chat_request.model,
                    )
                    error_response = build_empty_limit_response(
                        is_gemini=is_gemini, model=chat_request.model, stream=False
                    )
                    yield dump_json_response(error_response)
                    return

            log_all_keys_failed(request_type="switch_key", model=chat_request.model)
            error_response = build_all_keys_failed_response(
                is_gemini=is_gemini, model=chat_request.model, stream=False
            )
            yield dump_json_response(error_response)

        except Exception as e:
            log(
                "error",
                f"keepalive stream processing error: {str(e)}",
                extra={"request_type": "non-stream", "keepalive": True},
            )
            raise

    return StreamingResponse(
        keepalive_stream_generator(), media_type="application/json"
    )
