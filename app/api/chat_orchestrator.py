from fastapi import HTTPException, status

import app.config.settings as settings
from app.api.orchestration_helpers import (
    await_process_task_result,
    get_cached_response_or_none,
    register_active_request_if_needed,
    reuse_or_wait_active_request,
)
from app.api.request_helpers import (
    build_request_cache_key,
    create_processing_task,
    ensure_model_available,
    is_gemini_request,
    wait_for_existing_task,
)
from app.utils import log, protect_from_abuse


async def handle_aistudio_chat_completion(
    *,
    request,
    http_request,
    runtime,
    available_models,
    process_stream_request,
    process_nonstream_with_keepalive_stream,
    process_request,
):
    """Run the shared AI Studio chat completion orchestration.

    The route layer owns HTTP wiring only; this helper owns cache lookup,
    rate limiting, active-request reuse and dispatching to stream/nonstream
    processors.
    """
    is_gemini = is_gemini_request(request)

    # Correctness: AIRequest.payload 是 Optional（schemas.py），而
    # GeminiClient._convert_request_data 在 format_type == "gemini" 且
    # payload 为 None 时 data 永不赋值即被使用 → UnboundLocalError，被
    # 重试循环当作上游失败处理，白白烧完所有 key 后返回误导性 500。
    # 在任务创建前直接拒绝（400），既避免崩溃也避免无意义重试风暴。
    if is_gemini and getattr(request, "payload", None) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gemini-format request requires a `payload` field",
        )

    # Round 6（性能/防滥用）：限流提前到缓存键计算之前。旧顺序先
    # build_request_cache_key（对含数 MB base64 图片的请求是一次全量
    # 哈希，CPU 密集）再 protect_from_abuse —— 已认证客户端可以高频
    # 发送大 body，在 429 生效前强制服务端做重复重哈希（CPU 放大）。
    # 先过限流再算键，超频请求的代价只是一次计数器递增。
    await protect_from_abuse(
        http_request,
        settings.MAX_REQUESTS_PER_MINUTE,
        settings.MAX_REQUESTS_PER_DAY_PER_IP,
    )

    cache_key = build_request_cache_key(request, is_gemini=is_gemini)

    ensure_model_available(request.model, available_models)
    log(
        "info",
        f"请求缓存键: {cache_key[:8]}...",
        extra={"request_type": "non-stream", "model": request.model},
    )

    cached_response = await get_cached_response_or_none(
        runtime.get_cache,
        cache_key,
        is_stream=request.stream,
        is_gemini=is_gemini,
    )
    if cached_response:
        return cached_response

    pool_key = cache_key if not settings.PUBLIC_MODE else None
    if not settings.PUBLIC_MODE:
        assert runtime.active_requests_manager is not None
        result = await reuse_or_wait_active_request(
            public_mode=settings.PUBLIC_MODE,
            active_requests_manager=runtime.active_requests_manager,
            pool_key=pool_key,
            request=request,
            wait_for_existing_task=wait_for_existing_task,
        )
        if result:
            return result

    process_task = create_processing_task(
        request,
        is_gemini=is_gemini,
        key_manager=runtime.key_manager,
        response_cache_manager=runtime.response_cache_manager,
        safety_settings=runtime.safety_settings,
        safety_settings_g2=runtime.safety_settings_g2,
        cache_key=cache_key,
        process_stream_request=process_stream_request,
        process_nonstream_with_keepalive_stream=process_nonstream_with_keepalive_stream,
        process_request=process_request,
    )

    register_active_request_if_needed(
        public_mode=settings.PUBLIC_MODE,
        active_requests_manager=runtime.active_requests_manager,
        pool_key=pool_key,
        process_task=process_task,
    )

    return await await_process_task_result(
        process_task=process_task,
        public_mode=settings.PUBLIC_MODE,
        active_requests_manager=runtime.active_requests_manager,
        pool_key=pool_key,
        get_cache_func=runtime.get_cache,
        cache_key=cache_key,
        is_stream=request.stream,
        is_gemini=is_gemini,
    )
