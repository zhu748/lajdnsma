import json
import httpx
import logging
import asyncio
import re
from fastapi import HTTPException
from app.utils.logging import log
from app.utils.stealth import full_jitter_backoff

# Cleanup: `requests` 依赖已移除 —— 全部出站请求已统一走共享的 httpx
# AsyncClient，旧的 requests.exceptions.* isinstance 分支永远不会命中。

logger = logging.getLogger("my_logger")


# ---------------------------------------------------------------------------
# Key-cooldown activation (round 4)
# ---------------------------------------------------------------------------
# CRITICAL FIX: 全部 5 条真实请求路径（nonstream_completion /
# nonstream_status_handlers / native_stream_handlers / fake_stream_handlers /
# fake_stream_batch_runner）在异常分支调用的都是同步的
# handle_gemini_error()——它只记日志、从不触发 mark_key_failure()。
# 唯一会做冷却标记的 handle_api_error() 在整个仓库里没有任何调用方。
# 结果：429（限流）的密钥从未进入冷却，下一次 select_valid_api_keys
# 会再次选中同一个密钥 → 反复撞 429 → 持续限流甚至账号被风控升级，
# 这正是"密钥池被谷歌识别"的最典型加速器。
#
# 修复：handle_gemini_error() 在解析出上游 HTTP 状态码后，通过
# fire-and-forget 任务调度 mark_key_failure()（保持同步签名不变，
# 所有既有调用点零改动）。任务引用由模块级 set 持有，防止被 GC。
_cooldown_tasks: set = set()

_COOLDOWN_ELIGIBLE_STATUS = {429, 401, 403, 500, 503}


def _extract_status_code(error) -> int | None:
    """Best-effort HTTP status extraction from an upstream exception."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    # httpx.HTTPStatusError also exposes .request/.response; some
    # wrapped exceptions re-raise with the code embedded in args[0].
    if error.args and isinstance(error.args[0], int):
        return error.args[0]
    return None


def schedule_key_cooldown(error, api_key: str) -> None:
    """Fire-and-forget: place the failing key on cooldown per its status code.

    Safe to call from sync contexts inside a running event loop (which is
    always the case for the request handlers).  No-op when there is no
    running loop (e.g. unit tests calling handle_gemini_error directly).
    """
    if not api_key:
        return
    status_code = _extract_status_code(error)
    if status_code not in _COOLDOWN_ELIGIBLE_STATUS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _mark():
        try:
            from app.utils.api_key import mark_key_failure

            await mark_key_failure(api_key, status_code)
        except Exception:  # pragma: no cover - defensive
            logger.debug("schedule_key_cooldown: mark_key_failure failed", exc_info=True)

    try:
        task = loop.create_task(_mark())
        _cooldown_tasks.add(task)
        task.add_done_callback(_cooldown_tasks.discard)
    except RuntimeError:  # pragma: no cover - loop shutting down
        pass


def _key_id(api_key: str) -> str:
    """Return a stable hash-based key identifier for logging.

    Previously this returned api_key[:4]...api_key[-6:], but the first 4
    chars of a Gemini key are always "AIza" — logging that prefix is
    effectively equivalent to logging "this is a Gemini key" to anyone
    reading the logs.  Switch to a hash identifier.
    """
    if not api_key:
        return "key#unknown"
    return "key#" + str(hash(api_key) & 0xFFFFFF)


def sanitize_string(text: str) -> str:
    """Redact suspected API keys embedded in upstream error strings.

    Hardening: previously this returned `AIza.....abcdef` (preserving
    the `AIza` prefix and 6 trailing chars) for every matched key.
    But `AIza` is exactly the 4-char prefix that marks a string as a
    Google Gemini API key — keeping it in client-facing messages is
    effectively equivalent to telling the client "this is a Gemini
    key".  We now replace matched keys with a hash identifier so no
    part of the real key reaches the client.

    Round 4: 2025 起 Google AI Studio 签发的新版 `AQ.` 前缀密钥（见
    api_key.py 的 NEW_FORMAT_KEY_PATTERN）此前不在脱敏范围内——若
    上游错误串里嵌有该格式密钥，会原样泄露给客户端。现在两种
    已知格式都会被替换为 hash 标识。
    """
    # 经典格式：AIza + 35 位（总长 39）；新版格式：AQ. + 至少 20 位
    api_key_pattern = re.compile(
        r"(AIza[A-Za-z0-9\-_]{35})|(AQ\.[A-Za-z0-9_-]{20,})"
    )

    def redact_key(match):
        key = match.group(1) or match.group(2)
        # Use a stable hash identifier — no part of the real key is
        # preserved, and the `key#` prefix mirrors the logging format
        # used elsewhere so logs and client-facing messages stay
        # consistent.
        return f"key#{hash(key) & 0xFFFFFF:06x}"

    return api_key_pattern.sub(redact_key, text)


def handle_gemini_error(error, current_api_key) -> str:
    """
    统一处理来自Gemini的错误，并返回一个对用户友好的、清洗过的错误信息。

    所有返回给客户端的错误信息都不再包含 "Gemini API" 字样，避免
    暴露上游服务供应商的身份。

    Round 4: 这里同时调度密钥冷却（schedule_key_cooldown）——此前
    冷却机制只存在于从未被调用的 handle_api_error() 里，导致 429
    密钥反复被选中（详见模块顶部注释）。
    """
    # 先调度冷却标记（fire-and-forget，不阻塞错误处理本身）
    schedule_key_cooldown(error, current_api_key)

    # 清洗完整的错误字符串
    sanitized_full_error_str = sanitize_string(str(error))
    key_for_log = _key_id(current_api_key)

    # httpx HTTP 错误（旧 requests 分支已删除，见文件头注释）
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        log_extra = {"key": key_for_log, "status_code": status_code}
        error_message = ""  # 初始化 error_message

        if status_code == 400:
            try:
                error_data = error.response.json()
                if "error" in error_data:
                    if error_data["error"].get("code") == "invalid_argument":
                        error_message = "Invalid request argument"
                        log(
                            "ERROR",
                            f"{key_for_log} → 无效参数",
                            extra=log_extra,
                        )
                        return error_message

                    # 处理其他400错误
                    detail_message = sanitize_string(
                        error_data["error"].get("message", "Bad Request")
                    )
                    error_message = f"400 Bad Request: {detail_message}"
                    log("WARNING", error_message, extra=log_extra)
                    return error_message
                # 如果 'error' 键不存在，提供一个通用的400错误信息
                error_message = "400 Bad Request: unexpected response shape"
                log("WARNING", error_message, extra=log_extra)
                return error_message
            except (ValueError, json.JSONDecodeError):
                error_message = "400 Bad Request: response was not valid JSON"
                log("WARNING", error_message, extra=log_extra)
                return error_message

        elif status_code == 403:
            error_message = "Permission denied"
            log(
                "ERROR",
                error_message,
                extra={"key": key_for_log, "status_code": status_code},
            )
            return error_message

        elif status_code == 429:
            error_message = "Rate limited or quota exhausted"
            log("WARNING", error_message, extra=log_extra)
            return error_message

        elif status_code == 500:
            # 不再在返回给客户端的消息里写 "Gemini API 内部错误"
            error_message = "Upstream internal error"
            log("WARNING", error_message, extra=log_extra)
            return error_message

        elif status_code == 503:
            error_message = "Upstream service unavailable"
            log("WARNING", error_message, extra=log_extra)
            return error_message

        else:
            error_message = f"Upstream HTTP error: {status_code}"
            log(
                "WARNING",
                f"{error_message} - {sanitized_full_error_str}",
                extra=log_extra,
            )
            return error_message

    elif isinstance(error, httpx.TimeoutException):
        error_message = "Request timed out"
        log(
            "WARNING",
            f"{error_message}: {sanitized_full_error_str}",
            extra={"key": key_for_log},
        )
        return error_message

    elif isinstance(error, httpx.ConnectError):
        error_message = "Connection error"
        log(
            "WARNING",
            f"{error_message}: {sanitized_full_error_str}",
            extra={"key": key_for_log},
        )
        return error_message

    else:
        # 处理所有其他未知异常
        error_message = f"Unexpected error: {sanitized_full_error_str}"
        log("ERROR", error_message, extra={"key": key_for_log})
        return error_message


def translate_error(message: str) -> str:
    """Translate well-known error fragments to client-facing messages.

    Returned messages intentionally avoid naming the upstream provider so
    that an API consumer can't trivially detect that this is a proxy.
    """
    lower = message.lower()
    if "quota exceeded" in lower:
        return "Rate limited or quota exhausted"
    if "invalid argument" in lower:
        return "Invalid request argument"
    if "internal server error" in lower:
        return "Upstream internal error"
    if "service unavailable" in lower:
        return "Upstream service unavailable"
    return message


async def handle_api_error(
    e: Exception,
    api_key: str,
    key_manager,
    request_type: str,
    model: str,
    retry_count: int = 0,
):
    """统一处理API错误。

    Hardening:
    * 用 full-jitter 退避代替指数退避（无抖动版本会让并发重试同步风暴）
    * 429 时把该 key 加冷却 60s（通过 mark_key_failure）
    * 401/403 时把该 key 永久拉黑
    * 500/503 时把该 key 加冷却 5s
    * 抛给客户端的 HTTPException detail 不再含 "Gemini API" 字样
    """
    key_id = _key_id(api_key)

    # httpx HTTP 错误
    if isinstance(e, httpx.HTTPStatusError):
        status_code = e.response.status_code

        # 429 -> 切换 key + 冷却当前 key
        if status_code == 429:
            error_message = "Rate limited or quota exhausted"
            log(
                "WARNING",
                f"{key_id} 429 resource exhausted, switching key",
                extra={
                    "key": key_id,
                    "status_code": status_code,
                    "error_message": error_message,
                },
            )
            # 把这个 key 加冷却 60s（如果 key_manager 支持）
            try:
                from app.utils.api_key import mark_key_failure
                await mark_key_failure(api_key, 429)
            except Exception:
                pass
            return {
                "remove_cache": False,
                "error": error_message,
                "should_switch_key": True,
            }

        # 401/403 -> 永久拉黑当前 key + 切换
        if status_code in (401, 403):
            error_message = "Permission denied" if status_code == 403 else "Unauthorized"
            log(
                "ERROR",
                f"{key_id} {status_code} {error_message}, marking key invalid",
                extra={
                    "key": key_id,
                    "status_code": status_code,
                },
            )
            try:
                from app.utils.api_key import mark_key_failure
                await mark_key_failure(api_key, status_code)
            except Exception:
                pass
            return {
                "remove_cache": False,
                "error": error_message,
                "should_switch_key": True,
            }

        # 500/503 -> 加冷却 5s + 用 full-jitter 退避重试
        if retry_count < 3 and status_code in (500, 503):
            error_message = (
                "Upstream internal error"
                if status_code == 500
                else "Upstream service unavailable"
            )
            # AWS-recommended full jitter — spreads concurrent retriers
            # uniformly across [0, 2**attempt] so they don't retry at
            # the exact same instant (which is a bot fingerprint).
            wait_time = full_jitter_backoff(retry_count, base=1.0, cap=16.0)
            log(
                "warning",
                f"{key_id} {error_message}, waiting {wait_time:.1f}s before retry ({retry_count + 1}/3)",
                extra={
                    "key": key_id,
                    "request_type": request_type,
                    "model": model,
                    "status_code": int(status_code),
                },
            )
            # 加短冷却防止重试期间该 key 又被打
            try:
                from app.utils.api_key import mark_key_failure
                await mark_key_failure(api_key, status_code)
            except Exception:
                pass
            await asyncio.sleep(wait_time)
            return {"remove_cache": False}

        # 其它 HTTP 错误 -> 透传状态码 + 通用消息（不含 "Gemini API"）
        error_detail = handle_gemini_error(e, api_key)
        raise HTTPException(
            status_code=int(status_code),
            # 之前是 "Gemini API 服务器错误({status_code})，请稍后重试"
            # 现在不再泄露上游身份
            detail=f"Upstream service error ({status_code}). Please retry later.",
        )

    # 对于其他错误，返回切换密钥的信号，并输出错误信息到日志中
    error_detail = handle_gemini_error(e, api_key)
    return {"should_switch_key": True, "error": error_detail, "remove_cache": True}
