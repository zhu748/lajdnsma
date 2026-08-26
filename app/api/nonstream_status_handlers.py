import app.config.settings as settings
from app.utils.error_handling import handle_gemini_error
from app.utils.logging import log
from app.utils.response import (
    ensure_gemini_timing_fields,
    include_reasoning_for_request,
    openAI_from_Gemini,
)
from app.utils.response_loop_helpers import (
    dump_json_response,
    log_empty_response_count,
    log_request_success,
)


async def handle_nonstream_task_status(
    *,
    task,
    api_key: str,
    chat_request,
    response_cache_manager,
    cache_key: str,
    is_gemini: bool,
    empty_response_count: int,
    serialize_json: bool = False,
):
    try:
        status = task.result()
        if status == "success":
            log_request_success(
                api_key,
                request_type="non-stream",
                model=chat_request.model,
                label="non-stream request success",
            )
            cached_response, cache_hit = await response_cache_manager.get_and_remove(
                cache_key
            )
            # Correctness: 丢弃 cache_hit 的旧写法在缓存被并发请求"偷走"
            # 时（另一请求在 store() 与本处 get_and_remove() 之间的 await
            # 窗口内用同一 cache_key 消费了缓存）拿到 None，随后
            # cached_response.data 直接 AttributeError，被外层 except 记为
            # 该 key "error" 并触发无意义的全 key 重试。显式检查命中标志，
            # 未命中按失败处理（该响应已被其他请求取走，本请求重试是
            # 唯一正确的恢复路径）。
            if not cache_hit or cached_response is None:
                log(
                    "warning",
                    f"任务成功但缓存条目已被并发请求消费: {cache_key[:8]}...",
                    extra={
                        "request_type": "non-stream",
                        "model": chat_request.model,
                    },
                )
                return "error", None, empty_response_count
            if is_gemini:
                response = ensure_gemini_timing_fields(cached_response.data)
            else:
                response = openAI_from_Gemini(
                    cached_response,
                    stream=False,
                    include_reasoning=include_reasoning_for_request(
                        chat_request
                    ),
                )
            if serialize_json:
                response = dump_json_response(response)
            return "success", response, empty_response_count

        if status == "empty":
            empty_response_count += 1
            log_empty_response_count(
                api_key,
                request_type="non-stream",
                model=chat_request.model,
                empty_response_count=empty_response_count,
                max_empty_responses=settings.MAX_EMPTY_RESPONSES,
            )
            return "empty", None, empty_response_count

        return status, None, empty_response_count
    except Exception as e:
        handle_gemini_error(e, api_key)
        return "error", None, empty_response_count
