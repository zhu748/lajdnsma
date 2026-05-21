import json

from app.utils.error_response import build_error_response
from app.utils.logging import log


ALL_KEYS_FAILED_CONTENT = "当前 API 密钥已全部失效\n请联系管理员查看查询日志"


def key_preview(api_key: str) -> str:
    return api_key[:8]


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
        f"{label}, using key: {key_preview(api_key)}...",
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
    label: str = "empty response count",
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
    label: str = "request failed",
):
    log(
        "error",
        f"{label}: {error_detail}",
        extra={"key": key_preview(api_key), "request_type": request_type, "model": model},
    )


def log_concurrency_increase(
    *,
    request_type: str,
    model: str,
    current_concurrent: int,
    label: str = "increased concurrency after failed batch",
):
    log(
        "info",
        f"{label}: {current_concurrent}",
        extra={"request_type": request_type, "model": model},
    )


def log_all_keys_failed(*, request_type: str, model: str, key: str | None = None):
    extra = {"request_type": request_type, "model": model}
    if key is not None:
        extra["key"] = key
    log("error", "API key rotation failed; all available keys were tried", extra=extra)


def build_all_keys_failed_response(*, is_gemini: bool, model: str, stream: bool):
    return build_error_response(
        is_gemini=is_gemini,
        model=model,
        content=ALL_KEYS_FAILED_CONTENT,
        stream=stream,
    )
