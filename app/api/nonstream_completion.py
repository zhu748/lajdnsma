import asyncio

import app.config.settings as settings
from app.models.schemas import ChatCompletionRequest
from app.services import GeminiClient
from app.utils.error_handling import handle_gemini_error
from app.utils.gemini_response_processing import (
    finalize_gemini_response,
    select_safety_settings,
)
from app.utils.stats import record_outbound_attempt


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
):
    # Round 6: RPM 窗口在发射时计数（旧逻辑只在完成时计数，在途/失败
    # 请求全部漏记，高负载下退避系统性迟到）。
    try:
        record_outbound_attempt(current_api_key, chat_request.model)
    except Exception:
        pass
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

    # Cleanup: 此处曾为每个请求创建一个 "keepalive" 后台任务，但该任务
    # 的循环体只有 asyncio.sleep —— 不发送任何字节，纯空转。真正的
    # keepalive 由 nonstream_handlers.process_nonstream_with_keepalive_stream
    # 的流式生成器（周期性 yield "\n"）实现，与此处无关。空转任务连同
    # 其取消/泄漏防护逻辑一并删除。
    try:
        response_content = await awaited_task
        return await finalize_gemini_response(
            response_content,
            api_key=current_api_key,
            request_type="non-stream",
            model=chat_request.model,
            response_cache_manager=response_cache_manager,
            cache_key=cache_key,
        )
    except Exception as e:
        handle_gemini_error(e, current_api_key)
        return "error"
    except BaseException:
        # Hardening: `asyncio.CancelledError` is a BaseException (not
        # a subclass of Exception), so the `except Exception` block
        # above doesn't catch it.  Cancel the underlying gemini_task
        # when the client disconnects mid-shield — `asyncio.shield` would
        # otherwise keep it alive, billing upstream tokens for a response
        # the client will never receive.  (Tradeoff: this means we lose
        # the cache-fill benefit of shielded runs when the client
        # disconnects, but in practice the leaked task was a bigger
        # problem than cache misses.)
        gemini_task.cancel()
        raise


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
    # 兼容签名：keepalive_interval 由 build_nonstream_task 透传，但实际
    # 的 keepalive 行为在流式包装层实现，此参数已不使用（保留以避免
    # 波及全部调用方）。
    _ = keepalive_interval
    return await _run_nonstream_completion(
        chat_request,
        contents,
        system_instruction,
        current_api_key,
        response_cache_manager,
        safety_settings,
        safety_settings_g2,
        cache_key,
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
