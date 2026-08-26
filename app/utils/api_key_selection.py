from typing import List

import app.config.settings as settings
from app.utils.logging import log
from app.utils.stats import (
    get_api_key_usage,
    get_calls_last_minute_for_key,
    MAX_OUTBOUND_RPM,
    OUTBOUND_RPM_BACKOFF_FRACTION,
)
from app.utils.api_key import is_key_cooled_down


async def select_valid_api_keys(
    key_manager,
    batch_num: int,
    request_type: str,
    model: str,
) -> List[str]:
    """选择当前批次可用的 API keys。

    Hardening vs. old behaviour:
    1. 跳过被冷却的 key（429/401/403/500/503）
    2. 跳过 RPM 即将达到上限的 key（基于过去 60s 计数）
    3. 不再在所有 key 都达日额度时强行重置栈取一个出来用——
       这等于绕过自设的日额度上限，会触发上游 RPM 限制。
       现在返回空列表，让调用方走"所有 key 都不可用"路径。
    """
    valid_keys: List[str] = []
    checked_keys = set()

    rpm_threshold = max(1, int(MAX_OUTBOUND_RPM * OUTBOUND_RPM_BACKOFF_FRACTION))

    while len(valid_keys) < batch_num:
        api_key = await key_manager.get_available_key()
        if not api_key:
            break

        if api_key in checked_keys:
            # All keys have been tried in this round.
            break

        checked_keys.add(api_key)

        # Skip keys on cooldown (429/401/403/500/503).
        if await is_key_cooled_down(api_key):
            continue

        # Skip keys whose last-minute call count is at or above the RPM
        # safety threshold (avoid hitting upstream RPM cap).
        try:
            last_minute = get_calls_last_minute_for_key(api_key)
        except Exception:
            last_minute = 0
        if last_minute >= rpm_threshold:
            log(
                "info",
                f"key#{hash(api_key) & 0xFFFFFF:06x} at {last_minute}/{MAX_OUTBOUND_RPM} RPM in last 60s, skipping",
                extra={
                    "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                    "request_type": request_type,
                    "model": model,
                },
            )
            continue

        usage = await get_api_key_usage(settings.api_call_stats, api_key)
        if usage < settings.API_KEY_DAILY_LIMIT:
            valid_keys.append(api_key)
            continue

        log(
            "warning",
            f"key#{hash(api_key) & 0xFFFFFF:06x} exceeded daily limit ({usage}/{settings.API_KEY_DAILY_LIMIT})",
            extra={
                "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                "request_type": request_type,
                "model": model,
            },
        )

    if not valid_keys:
        log(
            "warning",
            "No API keys available (all on cooldown, RPM-limited, or daily-limited)",
            extra={"request_type": request_type, "model": model},
        )

    return valid_keys
