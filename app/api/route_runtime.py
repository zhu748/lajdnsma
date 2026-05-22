from fnmatch import fnmatchcase

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse

import app.config.settings as settings
from app.utils import log
from app.utils.response import ensure_gemini_timing_fields, openAI_from_Gemini
from app.utils.sse import sse_data, sse_done


key_manager = None
response_cache_manager = None
active_requests_manager = None
safety_settings = None
safety_settings_g2 = None
current_api_key = None
FAKE_STREAMING = None
FAKE_STREAMING_INTERVAL = None
PASSWORD = None
MAX_REQUESTS_PER_MINUTE = None
MAX_REQUESTS_PER_DAY_PER_IP = None


def init_runtime(
    _key_manager,
    _response_cache_manager,
    _active_requests_manager,
    _safety_settings,
    _safety_settings_g2,
    _current_api_key,
    _fake_streaming,
    _fake_streaming_interval,
    _password,
    _max_requests_per_minute,
    _max_requests_per_day_per_ip,
):
    global key_manager, response_cache_manager, active_requests_manager
    global safety_settings, safety_settings_g2, current_api_key
    global FAKE_STREAMING, FAKE_STREAMING_INTERVAL
    global PASSWORD, MAX_REQUESTS_PER_MINUTE, MAX_REQUESTS_PER_DAY_PER_IP

    key_manager = _key_manager
    response_cache_manager = _response_cache_manager
    active_requests_manager = _active_requests_manager
    safety_settings = _safety_settings
    safety_settings_g2 = _safety_settings_g2
    current_api_key = _current_api_key
    FAKE_STREAMING = _fake_streaming
    FAKE_STREAMING_INTERVAL = _fake_streaming_interval
    PASSWORD = _password
    MAX_REQUESTS_PER_MINUTE = _max_requests_per_minute
    MAX_REQUESTS_PER_DAY_PER_IP = _max_requests_per_day_per_ip


async def verify_user_agent(request: Request):
    if not settings.WHITELIST_USER_AGENT:
        return
    user_agent = request.headers.get("User-Agent", "").lower()
    if not any(fnmatchcase(user_agent, pattern) for pattern in settings.WHITELIST_USER_AGENT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not allowed client",
        )


async def get_cache(cache_key, is_stream: bool, is_gemini=False):
    assert response_cache_manager is not None
    cached_response, cache_hit = await response_cache_manager.get_and_remove(cache_key)

    if cache_hit and cached_response:
        log(
            "info",
            f"缓存命中: {cache_key[:8]}...",
            extra={"request_type": "non-stream", "model": cached_response.model},
        )

        if is_gemini:
            if is_stream:
                payload = ensure_gemini_timing_fields(cached_response.data)
                data = sse_data(payload)
                return StreamingResponse(data, media_type="text/event-stream")
            return ensure_gemini_timing_fields(cached_response.data)

        if is_stream:
            chunk = openAI_from_Gemini(cached_response, stream=True)
            return StreamingResponse(
                f"{chunk}{sse_done()}", media_type="text/event-stream"
            )
        return openAI_from_Gemini(cached_response, stream=False)

    return None
