from fastapi import HTTPException

# Cleanup: sanitize_string 导入未使用，已删除。

async def get_cached_response_or_none(
    get_cache_func,
    cache_key: str,
    *,
    is_stream: bool,
    is_gemini: bool,
):
    return await get_cache_func(cache_key, is_stream=is_stream, is_gemini=is_gemini)


async def reuse_or_wait_active_request(
    *,
    public_mode: bool,
    active_requests_manager,
    pool_key: str | None,
    request,
    wait_for_existing_task,
):
    if public_mode or not pool_key:
        return None
    return await wait_for_existing_task(active_requests_manager, pool_key, request)


def register_active_request_if_needed(
    *,
    public_mode: bool,
    active_requests_manager,
    pool_key: str | None,
    process_task,
):
    if not public_mode and pool_key:
        active_requests_manager.add(pool_key, process_task)


def remove_active_request_if_needed(
    *,
    public_mode: bool,
    active_requests_manager,
    pool_key: str | None,
):
    if not public_mode and pool_key:
        active_requests_manager.remove(pool_key)


async def await_process_task_result(
    *,
    process_task,
    public_mode: bool,
    active_requests_manager,
    pool_key: str | None,
    get_cache_func,
    cache_key: str,
    is_stream: bool,
    is_gemini: bool,
):
    try:
        response = await process_task
        remove_active_request_if_needed(
            public_mode=public_mode,
            active_requests_manager=active_requests_manager,
            pool_key=pool_key,
        )
        return response
    except HTTPException:
        # 处理任务内部主动抛出的 HTTPException（例如消息格式校验失败的
        # 400）必须原样透传 —— 旧实现被下方 except Exception 统一转换成
        # 500 "Upstream service error"，把客户端错误伪装成了服务端错误。
        remove_active_request_if_needed(
            public_mode=public_mode,
            active_requests_manager=active_requests_manager,
            pool_key=pool_key,
        )
        raise
    except Exception as e:
        remove_active_request_if_needed(
            public_mode=public_mode,
            active_requests_manager=active_requests_manager,
            pool_key=pool_key,
        )

        cached_response = await get_cache_func(
            cache_key, is_stream=is_stream, is_gemini=is_gemini
        )
        if cached_response:
            return cached_response

        # Hardening: previously embedded the project name "hajimi" plus
        # the raw exception text in the client-visible error detail.  We
        # now return only a generic upstream-error message to the client
        # and rely on internal logging for debugging.
        raise HTTPException(
            status_code=500,
            detail="Upstream service error. Please retry later.",
        )
