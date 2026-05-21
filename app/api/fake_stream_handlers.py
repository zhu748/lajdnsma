from app.services import GeminiClient
from app.utils import handle_gemini_error, log
from app.utils.gemini_response_processing import (
    finalize_gemini_response,
    select_safety_settings,
)


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
                "key": api_key[:8],
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
