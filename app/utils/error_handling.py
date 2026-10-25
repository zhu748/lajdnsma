"""上游错误处理：失败分类（quota/dead/transient）、冷却调度、
错误脱敏与客户端消息翻译。"""
import json
import httpx
import logging
import asyncio
import re
from app.utils.logging import log

# Cleanup: `requests` 依赖已移除 —— 全部出站请求已统一走共享的 httpx
# AsyncClient，旧的 requests.exceptions.* isinstance 分支永远不会命中。

logger = logging.getLogger("my_logger")


# ---------------------------------------------------------------------------
# Key-cooldown activation (round 4) + key-affinity retry (round 7)
# ---------------------------------------------------------------------------
# CRITICAL FIX: 全部 5 条真实请求路径（nonstream_completion /
# nonstream_status_handlers / native_stream_handlers / fake_stream_handlers /
# fake_stream_batch_runner）在异常分支调用的都是同步的
# handle_gemini_error()——它此前只记日志、从不触发 mark_key_failure()。
# （历史遗留的 handle_api_error() 曾是唯一会做冷却标记的入口，但全仓
# 无任何调用方，已删除；其冷却/切换语义由下方的 schedule_key_cooldown
# 与 _key_failure_kinds 承接。）
# 结果：429（限流）的密钥从未进入冷却，下一次 select_valid_api_keys
# 会再次选中同一个密钥 → 反复撞 429 → 持续限流甚至账号被风控升级，
# 这正是"密钥池被谷歌识别"的最典型加速器。
#
# 修复：handle_gemini_error() 在解析出上游 HTTP 状态码后，通过
# fire-and-forget 任务调度 mark_key_failure()（保持同步签名不变，
# 所有既有调用点零改动）。任务引用由模块级 set 持有，防止被 GC。
#
# Round 7（key 亲和重试）: 冷却触发条件收紧为"配额耗尽（429）+
# 密钥失效（401/403）"两类 —— 500/503 与网络类错误不再触发冷却，
# 重试循环通过 preferred_keys 机制继续用原 key 退避重试（用户策略：
# 只有配额耗尽才换 key）。
_cooldown_tasks: set = set()

# Round 7（key 亲和重试）：只有"配额耗尽"（429）与"密钥已死"
# （401/403，重试永远不可能成功）才触发冷却并轮换 key。
# 500/503（上游内部错误/过载）与网络类故障（超时/连接失败/DNS/TLS）
# 是**与 key 无关**的瞬时故障 —— 换 key 既不能提高成功率，反而会把
# 轮换行为暴露给上游风控（多 key 池的典型指纹）。此类错误一律
# 用原 key 带退避重试（见 _classify_failure / should_retry_same_key）。
_COOLDOWN_ELIGIBLE_STATUS = {429, 401, 403}

# Round 7: 同步记录每个 key 最近一次失败的类别。
#
# 为什么需要它（而不是直接查冷却状态）：冷却标记是 fire-and-forget
# 任务（见 schedule_key_cooldown），事件循环调度时序不保证在下一批
# 选 key 之前完成；重试循环需要一个**同步、确定**的信号来判断
# "这个 key 刚才的失败是不是配额耗尽"，从而决定下一批是换 key
# 还是继续用原 key。记录在 handle_gemini_error 入口处同步写入
# （所有 5 条真实请求路径都会经过它），失败类别含义：
#   "quota"     —— 429：配额耗尽（分钟级/日级），必须换 key
#   "dead"      —— 401/403：密钥无效/被封，重试无意义，必须换 key
#   "transient" —— 500/503/网络错误/未知：与 key 无关的瞬时故障，
#                  原 key 退避重试（key 亲和）
_key_failure_kinds: dict[str, str] = {}


def _classify_failure(error) -> str:
    """Round 7: 把上游异常归类为 quota / dead / transient 三类。

    归类直接决定重试循环的换 key 策略（见 should_retry_same_key）：
    只有 quota 与 dead 让出 key；transient 保持 key 亲和。
    """
    status_code = _extract_status_code(error)
    if status_code == 429:
        return "quota"
    if status_code in (401, 403):
        return "dead"
    # 500/503、网络类（TransportError，无状态码）、未知异常均属瞬时。
    return "transient"


def get_key_failure_kind(api_key: str) -> str | None:
    """返回该 key 最近一次失败的类别（quota/dead/transient），无记录返回 None。"""
    return _key_failure_kinds.get(api_key)


def should_retry_same_key(api_key: str) -> bool:
    """Round 7: 该 key 失败后是否应该继续用**同一个 key** 重试。

    True  —— 最近失败是 transient（500/503/网络错误），或没有失败
             记录（如空响应路径不经过 handle_gemini_error）：按用户
             策略"只有配额耗尽才换 key"，继续用原 key。
    False —— 最近失败是 quota（429 配额耗尽）或 dead（401/403 密钥
             失效）：必须轮换到下一个 key。

    注意：跨请求共享"最后一次失败"的语义是安全的 —— 若另一个并发
    请求刚把该 key 打成 429，本请求读到 False 而轮换，行为恰好正确
    （该 key 确实已配额受限）。选 key 侧还会叠加冷却/RPM/日额度三
    重校验（见 api_key_selection.select_valid_api_keys 的 preferred
    阶段），不依赖本函数单点判断。
    """
    return _key_failure_kinds.get(api_key, "transient") == "transient"


def clear_key_failure_kind(api_key: str) -> None:
    """清除单个 key 的失败类别记录（成功恢复或运维重置时使用）。"""
    _key_failure_kinds.pop(api_key, None)


def reset_key_failure_kinds() -> None:
    """清空全部失败类别记录（测试/运维批量重置时使用）。"""
    _key_failure_kinds.clear()


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


def _is_network_error(error) -> bool:
    """True for transport-level failures with no HTTP status code.

    httpx.TimeoutException（含 ConnectTimeout/ReadTimeout/WriteTimeout）
    与 ConnectError 都是 httpx.TransportError 的子类 —— DNS 解析失败、
    TLS 握手失败、连接被重置等也归 TransportError。这些错误意味着
    "没能从上游得到任何 HTTP 响应"，_extract_status_code 对它们恒返
    回 None。

    Round 7（key 亲和重试）：网络类故障归入 transient —— 不冷却、
    不换 key，重试循环带退避继续用原 key。连接层故障与具体哪个 key
    无关（换 key 无法救 DNS/网络黑洞），保留在 _is_network_error 里
    仅为分类与日志用途。
    """
    return isinstance(error, httpx.TransportError)


def extract_retry_delay(response) -> float | None:
    """从 429 响应体解析上游指示的重试延迟（秒）。

    Gemini 的 429 响应体形如：
      {"error": {"code": 429, "details": [
          {"@type": "type.googleapis.com/google.rpc.RetryInfo",
           "retryDelay": "26s"},
          {"@type": "...QuotaFailure", "violations": [
              {"quotaMetric": "GenerateRequestsPerDayPerProjectPerModel"}]}],
        "message": "..."}}

    Round 6 之前冷却固定 60s，不看上游指示：
      * retryDelay=5s 的分钟级限流 → key 白白闲置 55s（吞吐损失）；
      * 日配额耗尽（retryDelay 通常很大）→ key 每分钟被拉起来重试
        一次，持续撞 429（浪费 RPM + 持续风控曝光）。
    现在优先采纳上游指示，并限制在 [5s, 3600s] 区间内防止异常值。
    """
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    details = (((body or {}).get("error") or {}).get("details")) or []
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        raw = detail.get("retryDelay")
        if not isinstance(raw, str) or not raw:
            continue
        raw = raw.strip().lower()
        try:
            if raw.endswith("s"):
                seconds = float(raw[:-1])
            elif raw.endswith("ms"):
                seconds = float(raw[:-2]) / 1000.0
            elif raw.endswith("m"):
                seconds = float(raw[:-1]) * 60.0
            elif raw.endswith("h"):
                seconds = float(raw[:-1]) * 3600.0
            else:
                seconds = float(raw)
        except ValueError:
            continue
        if seconds > 0:
            return min(max(seconds, 5.0), 3600.0)
    return None


def summarize_upstream_error(response, *, max_len: int = 400) -> str:
    """从上游错误响应提取脱敏后的摘要（供日志使用）。

    Round 6（报错详细日志）：此前非 200 响应体被 aread() 消费后直接
    丢弃，日志里只剩 URL + 状态码 —— 429 的配额维度（PerMinute 还是
    PerDay）、5xx 的详细 message 全部丢失，排障与冷却决策都缺数据。
    现在提取 error.message + quota 维度，经 sanitize_string 脱敏后
    返回单行摘要。失败时返回空串（不影响调用方）。响应体可能很大，
    摘要截断到 max_len。
    """
    if response is None:
        return ""
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error") or {}
    if not isinstance(err, dict):
        return ""
    parts = []
    msg = err.get("message")
    if isinstance(msg, str) and msg:
        parts.append(sanitize_string(msg))
    status = err.get("status")
    if isinstance(status, str) and status:
        parts.append(f"status={status}")
    for detail in err.get("details") or []:
        if not isinstance(detail, dict):
            continue
        quota = detail.get("quotaMetric") or detail.get("quotaId")
        if quota:
            parts.append(f"quota={quota}")
    summary = " | ".join(p for p in parts if p)
    if len(summary) > max_len:
        summary = summary[: max_len - 3] + "..."
    return summary


def schedule_key_cooldown(error, api_key: str) -> None:
    """Fire-and-forget: place the failing key on cooldown per its failure kind.

    Safe to call from sync contexts inside a running event loop (which is
    always the case for the request handlers).  No-op when there is no
    running loop (e.g. unit tests calling handle_gemini_error directly).

    Round 7（key 亲和重试）：只有配额耗尽（429）与密钥失效（401/403）
    才冷却并轮换 key —— 500/503 与网络类错误不再触发冷却，重试循环
    会带着退避继续用原 key（见模块顶部 _COOLDOWN_ELIGIBLE_STATUS
    注释）。429 仍优先采纳响应体的 retryDelay。
    """
    if not api_key:
        return
    status_code = _extract_status_code(error)
    retry_delay = None
    if status_code == 429:
        response = getattr(error, "response", None)
        retry_delay = extract_retry_delay(response)
    if status_code not in _COOLDOWN_ELIGIBLE_STATUS:
        # transient（500/503/网络错误/未知）：不冷却、不换 key。
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _mark():
        try:
            from app.utils.api_key import mark_key_failure

            await mark_key_failure(api_key, status_code, cooldown_seconds=retry_delay)
        except Exception:  # pragma: no cover - defensive
            logger.debug(f"记录密钥失败状态时发生错误", exc_info=True)

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

    Round 4: 这里同时调度密钥冷却（schedule_key_cooldown）——历史上
    冷却机制只存在于从未被调用的 handle_api_error() 里（该死函数已
    删除），曾导致 429 密钥反复被选中（详见模块顶部注释）。

    Round 7: 入口处**同步**记录失败类别（quota/dead/transient）——
    重试循环用它决定下一批是否继续用原 key；冷却标记仍是
    fire-and-forget（时序不保证），两者职责分离。
    """
    # Round 7: 同步记录失败类别（重试循环的换 key 决策依据）。
    if current_api_key:
        _key_failure_kinds[current_api_key] = _classify_failure(error)
    # 再调度冷却标记（fire-and-forget，不阻塞错误处理本身）
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
                        error_message = "请求参数无效"
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
                    error_message = f"400 请求参数错误：{detail_message}"
                    log("WARNING", error_message, extra=log_extra)
                    return error_message
                # 如果 'error' 键不存在，提供一个通用的400错误信息
                error_message = "400 请求参数错误：上游响应结构异常"
                log("WARNING", error_message, extra=log_extra)
                return error_message
            except (ValueError, json.JSONDecodeError):
                error_message = "400 请求参数错误：上游未返回有效 JSON"
                log("WARNING", error_message, extra=log_extra)
                return error_message

        elif status_code == 403:
            error_message = "访问被拒绝，请检查密钥权限和服务开通状态"
            # Round 6（详细日志）：403 的上游 message 能区分
            # "key 无效" / "API 未开启" / "地区限制"等不同原因。
            summary = summarize_upstream_error(error.response)
            log(
                "ERROR",
                f"{error_message}: {summary}" if summary else error_message,
                extra={"key": key_for_log, "status_code": status_code},
            )
            return error_message

        elif status_code == 429:
            error_message = "请求受限或配额已耗尽"
            # Round 6（详细日志 + 冷却精度）：429 摘要包含配额维度
            # （PerMinute/PerDay）与 retryDelay，运维能直接看出是哪种
            # 配额耗尽；冷却时长已由 schedule_key_cooldown 按
            # retryDelay 调整。
            summary = summarize_upstream_error(error.response)
            retry_delay = extract_retry_delay(error.response)
            delay_hint = f"，建议等待 {retry_delay:.0f} 秒后重试" if retry_delay else ""
            log(
                "WARNING",
                f"{error_message}{delay_hint}: {summary}" if summary else f"{error_message}{delay_hint}",
                extra=log_extra,
            )
            return error_message

        elif status_code == 500:
            # 不再在返回给客户端的消息里写 "Gemini API 内部错误"
            error_message = "上游服务内部错误"
            summary = summarize_upstream_error(error.response)
            log(
                "WARNING",
                f"{error_message}: {summary}" if summary else error_message,
                extra=log_extra,
            )
            return error_message

        elif status_code == 503:
            error_message = "上游服务暂时不可用"
            summary = summarize_upstream_error(error.response)
            log(
                "WARNING",
                f"{error_message}: {summary}" if summary else error_message,
                extra=log_extra,
            )
            return error_message

        else:
            error_message = f"上游 HTTP 请求失败：状态码 {status_code}"
            summary = summarize_upstream_error(error.response)
            log(
                "WARNING",
                f"{error_message} - {summary or sanitized_full_error_str}",
                extra=log_extra,
            )
            return error_message

    elif isinstance(error, httpx.TimeoutException):
        error_message = "上游请求超时"
        log(
            "WARNING",
            f"{error_message}: {sanitized_full_error_str}",
            extra={"key": key_for_log},
        )
        return error_message

    elif isinstance(error, httpx.ConnectError):
        error_message = "连接上游服务失败"
        log(
            "WARNING",
            f"{error_message}: {sanitized_full_error_str}",
            extra={"key": key_for_log},
        )
        return error_message

    else:
        # 处理所有其他未知异常
        error_message = f"未预期错误：{sanitized_full_error_str}"
        log("ERROR", error_message, extra={"key": key_for_log})
        return error_message


def translate_error(message: str) -> str:
    """Translate well-known error fragments to client-facing messages.

    Returned messages intentionally avoid naming the upstream provider so
    that an API consumer can't trivially detect that this is a proxy.
    """
    lower = message.lower()
    if "quota exceeded" in lower:
        return "请求受限或配额已耗尽"
    if "invalid argument" in lower:
        return "请求参数无效"
    if "internal server error" in lower:
        return "上游服务内部错误"
    if "service unavailable" in lower:
        return "上游服务暂时不可用"
    return message
