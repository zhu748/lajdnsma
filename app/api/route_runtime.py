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
# Cleanup: 曾有 FAKE_STREAMING / FAKE_STREAMING_INTERVAL / PASSWORD /
# MAX_REQUESTS_PER_MINUTE / MAX_REQUESTS_PER_DAY_PER_IP 五个全局快照
# 变量，但全项目只写不读（各处实际都是实时读 settings.*），
# 属于误导后来者的死代码，已连同 init_runtime 的对应参数一并删除。


def init_runtime(
    _key_manager,
    _response_cache_manager,
    _active_requests_manager,
    _safety_settings,
    _safety_settings_g2,
    _current_api_key,
):
    global key_manager, response_cache_manager, active_requests_manager
    global safety_settings, safety_settings_g2, current_api_key

    key_manager = _key_manager
    response_cache_manager = _response_cache_manager
    active_requests_manager = _active_requests_manager
    safety_settings = _safety_settings
    safety_settings_g2 = _safety_settings_g2
    current_api_key = _current_api_key


async def verify_user_agent(request: Request):
    if not settings.WHITELIST_USER_AGENT:
        return
    user_agent = request.headers.get("User-Agent", "").lower()
    if not any(fnmatchcase(user_agent, pattern) for pattern in settings.WHITELIST_USER_AGENT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not allowed client",
        )


def _single_chunk_response(payload: str) -> StreamingResponse:
    """Build a StreamingResponse that sends `payload` as ONE chunk.

    Perf fix: passing a raw `str` to StreamingResponse makes Starlette
    fall back to `iterate_in_threadpool(content)`, which iterates the
    string **character by character** — a 4 KB cached SSE payload turned
    into ~4000 separate ASGI sends (in a worker thread!), right on the
    cache-hit path that is supposed to be the fastest.  Wrapping the
    payload in an async generator yields it as a single chunk.
    """
    return StreamingResponse(_one_shot(payload), media_type="text/event-stream")


async def _one_shot(payload: str):
    yield payload


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
                return _single_chunk_response(sse_data(payload))
            return ensure_gemini_timing_fields(cached_response.data)

        if is_stream:
            chunk = openAI_from_Gemini(cached_response, stream=True)
            return _single_chunk_response(f"{chunk}{sse_done()}")
        return openAI_from_Gemini(cached_response, stream=False)

    return None
