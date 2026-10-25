"""重试循环的日志 / keyed 任务构建 / 全失败响应工具。"""
import json

from app.utils.error_response import build_error_response
from app.utils.logging import log


# Previously this string was "当前 API 密钥已全部失效\n请联系管理员查看查询日志"
# which leaks two internal facts: (1) there's a key pool behind this endpoint,
# (2) there's a "query log" that an admin can read.  Real OpenAI/Anthropic
# responses never say such things.  The new message is a generic upstream
# unavailability message that matches what real OpenAI/Anthropic return on
# upstream outages.
ALL_KEYS_FAILED_CONTENT = (
    "The model is currently overloaded. Please try again later."
)


def key_preview(api_key: str) -> str:
    # Previously this returned api_key[:8], but the first 8 characters of a
    # Gemini key are always the highly-recognisable prefix "AIzaSy12".
    # Logging that prefix anywhere effectively announces "this is a Gemini
    # key" to anyone reading the logs.  Switch to a stable hash identifier.
    return "key#" + str(hash(api_key) & 0xFFFFFF)


def dump_json_response(response) -> str:
    return json.dumps(response, ensure_ascii=False)


def build_keyed_tasks(valid_keys, *, request_type: str, model: str, label: str, task_factory):
    tasks = []
    tasks_map = {}
    for api_key in valid_keys:
        log_request_start(api_key, request_type=request_type, model=model, label=label)
        task = task_factory(api_key)
        tasks.append((api_key, task))
        tasks_map[task] = api_key
    return tasks, tasks_map


def log_request_start(api_key: str, *, request_type: str, model: str, label: str):
    log(
        "info",
        f"{label}，使用密钥：{key_preview(api_key)}",
        extra={"key": key_preview(api_key), "request_type": request_type, "model": model},
    )


def log_request_success(api_key: str, *, request_type: str, model: str, label: str):
    log(
        "info",
        label,
        extra={"key": key_preview(api_key), "request_type": request_type, "model": model},
    )


def log_empty_response_count(
    api_key: str,
    *,
    request_type: str,
    model: str,
    empty_response_count: int,
    max_empty_responses: int,
    label: str = "空响应次数",
):
    log(
        "warning",
        f"{label}: {empty_response_count}/{max_empty_responses}",
        extra={"key": key_preview(api_key), "request_type": request_type, "model": model},
    )


def log_request_failure(
    api_key: str,
    *,
    request_type: str,
    model: str,
    error_detail: str,
    label: str = "请求失败",
):
    log(
        "error",
        f"{label}: {error_detail}",
        extra={"key": key_preview(api_key), "request_type": request_type, "model": model, "upstream_error": True},
    )


def log_concurrency_increase(
    *,
    request_type: str,
    model: str,
    current_concurrent: int,
    label: str = "失败后调整并发数",
):
    log(
        "info",
        f"{label}: {current_concurrent}",
        extra={"request_type": request_type, "model": model},
    )


def log_all_keys_failed(*, request_type: str, model: str, key: str | None = None):
    extra = {"request_type": request_type, "model": model, "terminal_failure": True}
    if key is not None:
        extra["key"] = key
    log("error", f"密钥轮换失败：所有可用密钥均已尝试", extra=extra)


def build_all_keys_failed_response(*, is_gemini: bool, model: str, stream: bool):
    return build_error_response(
        is_gemini=is_gemini,
        model=model,
        content=ALL_KEYS_FAILED_CONTENT,
        stream=stream,
    )
