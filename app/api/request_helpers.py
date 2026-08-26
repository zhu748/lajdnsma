import asyncio

from fastapi import HTTPException, status

import app.config.settings as settings
from app.utils import generate_cache_key, log


def is_gemini_request(request) -> bool:
    return getattr(request, "format_type", None) == "gemini"


def build_request_cache_key(request, *, is_gemini: bool) -> str:
    if settings.PRECISE_CACHE:
        return generate_cache_key(request, is_gemini=is_gemini)
    return generate_cache_key(
        request,
        last_n_messages=settings.CALCULATE_CACHE_ENTRIES,
        is_gemini=is_gemini,
    )


def ensure_model_available(model: str, available_models: list[str]):
    if model not in available_models:
        log("error", "无效的模型", extra={"model": model, "status_code": 400})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的模型",
        )


async def wait_for_existing_task(active_requests_manager, pool_key: str, request):
    # Correctness: 请求合并只对非流式请求启用。流式任务的 result 是一个
    # StreamingResponse 对象，其 body_iterator 只能被消费一次 —— 如果把
    # 同一个对象交给等待者，主请求与等待者会并发迭代同一个 async
    # generator（SSE chunk 被任意瓜分，或直接抛
    # "asynchronous generator is already running"），两条流同时损坏。
    # 等待者放弃合流，走自己的完整处理路径。
    if getattr(request, "stream", False):
        return None

    active_task = active_requests_manager.get(pool_key)
    if not active_task or active_task.done():
        return None

    log(
        "info",
        "发现相同请求的进行中任务",
        extra={
            "request_type": "non-stream",
            "model": request.model,
        },
    )

    try:
        await asyncio.wait_for(active_task, timeout=240)
        if active_task.done() and not active_task.cancelled():
            result = active_task.result()
            active_requests_manager.remove(pool_key)
            return result
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        error_type = "超时" if isinstance(e, asyncio.TimeoutError) else "被取消"
        log(
            "warning",
            f"等待已有任务{error_type}: {pool_key}",
            extra={"request_type": "non-stream", "model": request.model},
        )
        if active_task.done() or active_task.cancelled():
            active_requests_manager.remove(pool_key)
            log(
                "info",
                f"已从活跃请求池移除{error_type}任务: {pool_key}",
                extra={"request_type": "non-stream"},
            )
    except Exception as e:
        # 等待的任务本身失败了。不向上抛（抛出会把原始异常以未脱敏的
        # 500 直接返回给等待者），移除失败任务后让等待者走自己的完整
        # 处理路径 —— 那条路径有标准的错误脱敏与缓存回退逻辑。
        log(
            "warning",
            f"等待的已有任务失败，改为独立处理: {pool_key} ({e})",
            extra={"request_type": "non-stream", "model": request.model},
        )
        active_requests_manager.remove(pool_key)

    return None


def create_processing_task(
    request,
    *,
    is_gemini: bool,
    key_manager,
    response_cache_manager,
    safety_settings,
    safety_settings_g2,
    cache_key: str,
    process_stream_request,
    process_nonstream_with_keepalive_stream,
    process_request,
):
    if request.stream:
        return asyncio.create_task(
            process_stream_request(
                chat_request=request,
                key_manager=key_manager,
                response_cache_manager=response_cache_manager,
                safety_settings=safety_settings,
                safety_settings_g2=safety_settings_g2,
                cache_key=cache_key,
            )
        )

    if settings.NONSTREAM_KEEPALIVE_ENABLED:
        return asyncio.create_task(
            process_nonstream_with_keepalive_stream(
                chat_request=request,
                key_manager=key_manager,
                response_cache_manager=response_cache_manager,
                safety_settings=safety_settings,
                safety_settings_g2=safety_settings_g2,
                cache_key=cache_key,
                is_gemini=is_gemini,
            )
        )

    return asyncio.create_task(
        process_request(
            chat_request=request,
            key_manager=key_manager,
            response_cache_manager=response_cache_manager,
            safety_settings=safety_settings,
            safety_settings_g2=safety_settings_g2,
            cache_key=cache_key,
        )
    )
