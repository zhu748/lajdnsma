import time
import asyncio
from fastapi import HTTPException, Request

rate_limit_data = {}
rate_limit_lock = asyncio.Lock()

# Hardening: previously rate_limit_data grew without bound — one entry
# per (path, minute) and per (ip, day) — so a long-running process
# would accumulate millions of stale entries.  We now periodically
# sweep expired entries.  Run the sweep roughly every 5 minutes
# (every 300 s) and only on the call path (no background thread).
#
# Bug fix: the old sweep used a flat 90 s cutoff for EVERY entry,
# which meant day buckets (86 400 s window) were deleted after just
# 90 seconds of inactivity — resetting that IP's daily counter and
# making MAX_REQUESTS_PER_DAY_PER_IP effectively a 90-second quota.
# Entries now carry their own TTL so each bucket type expires on its
# own schedule.
_last_sweep_ts = 0
_SWEEP_INTERVAL_S = 300

# TTLs for each bucket type.  Minute buckets only need to outlive
# their 60 s window; day buckets must survive their full 86 400 s
# window so an IP cannot reset its daily quota by idling.
_MINUTE_TTL_S = 90
_DAY_TTL_S = 86400 + 300


async def _maybe_sweep_expired(now: int) -> None:
    """Remove expired entries from rate_limit_data.

    Called inline on the protect_from_abuse path.  Cheap (a single
    pass over the dict) and runs at most once every 5 minutes.
    """
    global _last_sweep_ts
    if now - _last_sweep_ts < _SWEEP_INTERVAL_S:
        return
    _last_sweep_ts = now
    # Identify expired keys without mutating the dict while iterating.
    # Each entry is (count, ts, ttl); expire per-entry so day buckets
    # live for their full daily window.
    expired_keys = []
    for key, (_count, ts, ttl) in rate_limit_data.items():
        if now - ts > ttl:
            expired_keys.append(key)
    for key in expired_keys:
        rate_limit_data.pop(key, None)


def _client_host(request: Request) -> str:
    """Best-effort client host ("unknown" when not available, e.g. some
    test transports and ASGI wrappers without connection info)."""
    client = getattr(request, "client", None)
    return client.host if client and client.host else "unknown"


async def protect_from_abuse(
    request: Request,
    max_requests_per_minute: int = 30,
    max_requests_per_day_per_ip: int = 600,
):
    now = int(time.time())
    minute = now // 60
    day = now // (60 * 60 * 24)

    # Bug fix: the minute bucket used to be keyed by path+minute ONLY,
    # while the day bucket included the client host.  With the default
    # MAX_REQUESTS_PER_MINUTE=30 a single client sending 31 requests to
    # the same path within a minute exhausted a budget shared by ALL
    # users — one user could 429 the whole deployment.  Minute buckets
    # are now per-IP as well, matching the day-bucket semantics.
    host = _client_host(request)
    minute_key = f"{host}:{request.url.path}:{minute}"
    day_key = f"{host}:{day}"

    async with rate_limit_lock:
        await _maybe_sweep_expired(now)

        # Bug fix: 第一轮 H4 将条目改为 (count, ts, ttl) 三元组时，此处
        # 的解包仍按二元组 —— 任何聊天补全请求第一跳就抛
        # "too many values to unpack"，所有 chat 请求 500。补齐解包维度
        # 并加回归测试（protect_from_abuse 全路径）。
        minute_count, minute_timestamp, _minute_ttl = rate_limit_data.get(
            minute_key, (0, now, _MINUTE_TTL_S)
        )

        if now - minute_timestamp >= 60:
            minute_count = 0
            minute_timestamp = now
        minute_count += 1
        rate_limit_data[minute_key] = (minute_count, minute_timestamp, _MINUTE_TTL_S)

        day_count, day_timestamp, _day_ttl = rate_limit_data.get(
            day_key, (0, now, _DAY_TTL_S)
        )
        if now - day_timestamp >= 86400:
            day_count = 0
            day_timestamp = now
        day_count += 1
        rate_limit_data[day_key] = (day_count, day_timestamp, _DAY_TTL_S)

    if minute_count > max_requests_per_minute:
        raise HTTPException(
            status_code=429,
            detail={"message": "Too many requests per minute"},
        )
    if day_count > max_requests_per_day_per_ip:
        raise HTTPException(
            status_code=429,
            detail={"message": "Too many requests per day from this IP"},
        )
