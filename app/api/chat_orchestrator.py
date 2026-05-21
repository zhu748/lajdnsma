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
    cache_key = build_request_cache_key(request, is_gemini=is_gemini)

    await protect_from_abuse(
        http_request,
        settings.MAX_REQUESTS_PER_MINUTE,
        settings.MAX_REQUESTS_PER_DAY_PER_IP,
    )

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
