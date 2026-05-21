from typing import List

import app.config.settings as settings
from app.utils.logging import log
from app.utils.stats import get_api_key_usage


async def select_valid_api_keys(
    key_manager,
    batch_num: int,
    request_type: str,
    model: str,
) -> List[str]:
    """选择当前批次可用的 API keys。"""
    valid_keys: List[str] = []
    checked_keys = set()
    all_keys_checked = False

    while len(valid_keys) < batch_num:
        api_key = await key_manager.get_available_key()
        if not api_key:
            break

        if api_key in checked_keys:
            all_keys_checked = True
            break

        checked_keys.add(api_key)
        usage = await get_api_key_usage(settings.api_call_stats, api_key)
        if usage < settings.API_KEY_DAILY_LIMIT:
            valid_keys.append(api_key)
            continue

        log(
            "warning",
            f"API key {api_key[:8]}... exceeded daily limit ({usage}/{settings.API_KEY_DAILY_LIMIT})",
            extra={
                "key": api_key[:8],
                "request_type": request_type,
                "model": model,
            },
        )

    if all_keys_checked and not valid_keys:
        log(
            "warning",
            "All API keys have reached the daily limit; resetting key stack",
            extra={"request_type": request_type, "model": model},
        )
        key_manager._reset_key_stack()
        api_key = await key_manager.get_available_key()
        if api_key:
            valid_keys = [api_key]

    return valid_keys
