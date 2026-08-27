from app.services import GeminiClient
from app.utils import handle_gemini_error, log
from app.utils.gemini_response_processing import (
    finalize_gemini_response,
    select_safety_settings,
)
from app.utils.stats import record_outbound_attempt


async def handle_fake_streaming(
    api_key,
    chat_request,
    contents,
    response_cache_manager,
    system_instruction,
    safety_settings,
    safety_settings_g2,
    cache_key,
):
    # Round 6: RPM 窗口发射计数（见 stats.record_outbound_attempt）。
    try:
        record_outbound_attempt(api_key, chat_request.model)
    except Exception:
        pass
    gemini_client = GeminiClient(api_key)

    try:
        response_content = await gemini_client.complete_chat(
            chat_request,
            contents,
            select_safety_settings(
                chat_request.model, safety_settings, safety_settings_g2
            ),
            system_instruction,
        )
        log(
            "info",
            "fake stream response received; caching result",
            extra={
                "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                "request_type": "fake-stream",
                "model": chat_request.model,
            },
        )
        return await finalize_gemini_response(
            response_content,
            api_key=api_key,
            request_type="fake-stream",
            model=chat_request.model,
            response_cache_manager=response_cache_manager,
            cache_key=cache_key,
        )
    except Exception as e:
        handle_gemini_error(e, api_key)
        return "error"
