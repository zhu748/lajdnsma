import app.config.settings as settings
from app.utils import update_api_call_stats
from app.utils.empty_response import is_empty_gemini_response
from app.utils.logging import log
from app.config.safety import get_safety_settings


def select_safety_settings(model: str, safety_settings, safety_settings_g2):
    """Select the appropriate safety_settings list to send for a model.

    Honours SAFETY_MODE from app.config.safety:
      * "default"     -> returns None  (don't send the field at all)
      * "permissive"  -> returns 4-class OFF + CIVIC_INTEGRITY=BLOCK_ONLY_HIGH
      * "off_all"     -> returns legacy 5-class all-OFF list

    Important: in "default" mode we return None even if callers passed
    non-empty legacy lists — the whole point of default mode is "do not
    send safetySettings", and falling back to the caller-supplied list
    would defeat the anti-fingerprint purpose.
    """
    is_g2 = "gemini-2" in model
    chosen = get_safety_settings(is_gemini_2=is_g2)
    if chosen is not None:
        return chosen
    # In default mode, always return None — do NOT fall back to the
    # caller-supplied SAFETY_SETTINGS/SAFETY_SETTINGS_G2 lists, because
    # those are the legacy "all-OFF" lists which are exactly what we're
    # trying to avoid sending.
    return None


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
    key_id = "key#" + str(hash(api_key) & 0xFFFFFF)

    if is_empty_gemini_response(response_content):
        log(
            "warning",
            f"{key_id} 返回空响应",
            extra={"key": key_id, "request_type": request_type, "model": model},
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
