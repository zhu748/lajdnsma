import app.config.settings as settings
from app.utils import update_api_call_stats
from app.utils.empty_response import is_empty_gemini_response
from app.utils.logging import log


def select_safety_settings(model: str, safety_settings, safety_settings_g2):
    return safety_settings_g2 if "gemini-2.5" in model else safety_settings


async def finalize_gemini_response(
    response_content,
    *,
    api_key: str,
    request_type: str,
    model: str,
    response_cache_manager,
    cache_key: str,
    update_stats: bool = True,
):
    response_content.set_model(model)

    if is_empty_gemini_response(response_content):
        log(
            "warning",
            f"API密钥 {api_key[:8]}... 返回空响应",
            extra={"key": api_key[:8], "request_type": request_type, "model": model},
        )
        return "empty"

    await response_cache_manager.store(cache_key, response_content)

    if update_stats:
        await update_api_call_stats(
            settings.api_call_stats,
            endpoint=api_key,
            model=model,
            token=response_content.total_token_count,
        )

    return "success"
