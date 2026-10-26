"""LogManager：有界环形缓冲 + JSON 行格式的双通道日志。"""
import logging
import json
import os
import re
import time
import uuid
import sys
from contextvars import ContextVar
from datetime import datetime
from collections import deque
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_current_request_report = ContextVar("current_request_report", default=None)
LEVEL_LABELS = {"DEBUG": "调试", "INFO": "信息", "WARNING": "警告", "ERROR": "错误", "CRITICAL": "严重错误"}
TYPE_LABELS = {"stream": "流式", "non-stream": "非流式", "fake-stream": "伪流式", "switch_key": "密钥切换", "vertex": "Vertex"}
REPORT_FIELDS = (
    "event", "request_id", "path", "outcome", "source", "duration_ms", "usage",
    "response_chars", "reasoning_chars", "response_preview", "reasoning_preview",
    "tool_calls", "finish_reason", "capture_truncated", "attempt_id", "stage", "backend", "retry_delay",
    "reason", "decision", "attempt_number", "previous_attempt_id", "previous_key", "next_key",
    "previous_model", "next_model",
)
try:
    LOG_TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Shanghai"))
except (ZoneInfoNotFoundError, ValueError):
    LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")


def redact_log_text(value):
    text = str(value)
    text = re.sub(r"(?i)((?:Bearer|Basic)\s+)[^\s\"'<>]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?:AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)([?&](?:key|api_key|token|password)=)[^&\s]+", r"\1[已隐藏]", text)
    text = re.sub(r"-----BEGIN (?:RSA )?PRIVATE KEY-----.*?(?:-----END (?:RSA )?PRIVATE KEY-----|$)", "[私钥已隐藏]", text, flags=re.DOTALL)
    return text

DEBUG = False  # 可以从环境变量中获取

LOG_FORMAT_DEBUG = "%(asctime)s - %(levelname)s - [%(key)s]-%(request_type)s-[%(model)s]-%(status_code)s: %(message)s - %(error_message)s"
LOG_FORMAT_NORMAL = "[%(asctime)s] [%(levelname)s] [%(key)s]-%(request_type)s-[%(model)s]-%(status_code)s: %(message)s"

# Vertex日志格式
VERTEX_LOG_FORMAT_DEBUG = "%(asctime)s - %(levelname)s - [%(vertex_id)s]-%(operation)s-[%(status)s]: %(message)s - %(error_message)s"
VERTEX_LOG_FORMAT_NORMAL = "[%(asctime)s] [%(levelname)s] [%(vertex_id)s]-%(operation)s-[%(status)s]: %(message)s"

# 配置 logger
logger = logging.getLogger("my_logger")
logger.setLevel(logging.DEBUG)

# 控制台处理器
console_handler = logging.StreamHandler()

# 设置日志格式
console_formatter = logging.Formatter("%(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


# 日志缓存，用于在网页上显示最近的日志
_feed = deque(maxlen=1000)
_feed_lock = Lock()
_feed_seq = 0
_feed_instance = uuid.uuid4().hex[:12]

def get_log_feed(after=0):
    with _feed_lock:
        if after > _feed_seq:
            after = 0
        oldest = _feed[0]["seq"] if _feed else _feed_seq + 1
        return {"instance_id": _feed_instance, "cursor": _feed_seq,
                "gap": bool(after and after < oldest - 1), "capacity": _feed.maxlen,
                "oldest_timestamp": _feed[0]["timestamp"] if _feed else None,
                "logs": [dict(entry) for entry in _feed if entry["seq"] > after]}

class LogManager:
    """环形缓冲日志管理器。

    Cleanup: 原本有 LogManager 与 VertexLogManager 两个逐字相同的类
    （以及逐字相同的实例逻辑），纯复制粘贴。合并为单类，VertexLogManager
    保留为别名以兼容既有导入。
    """

    def __init__(self, max_logs=200):
        self.logs = deque(maxlen=max_logs)  # 使用双端队列存储最近的日志
        self.lock = Lock()

    def add_log(self, log_entry):
        global _feed_seq
        with _feed_lock:
            _feed_seq += 1
            log_entry.update(seq=_feed_seq, log_id=f"{_feed_instance}-{_feed_seq}")
            _feed.append(log_entry)
        with self.lock:
            self.logs.append(log_entry)

    def get_recent_logs(self, count=50):
        with self.lock:
            return list(self.logs)[-count:] if count > 0 else []


# 创建日志管理器实例 (输出到前端)
log_manager = LogManager()

# 兼容别名：Vertex 日志同样使用 LogManager（原实现为重复类）
VertexLogManager = LogManager
# 创建Vertex日志管理器实例 (输出到前端)
vertex_log_manager = VertexLogManager()


def format_log_message(level, message, extra=None):
    extra = extra or {}
    message = redact_log_text(message)
    extra = dict(extra)
    for name in ("key", "model", "error_message", "response_preview", "reasoning_preview", "reason"):
        if name in extra and isinstance(extra[name], str):
            extra[name] = redact_log_text(extra[name])
    # Perf: 时间戳只计算一次（旧实现每条日志调用两次
    # datetime.now().strftime，log() 是每请求多次的热路径）
    now_str = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    log_values = {
        "asctime": now_str,
        "levelname": LEVEL_LABELS.get(level, level),
        "key": extra.get("key", ""),
        "request_type": TYPE_LABELS.get(extra.get("request_type", ""), extra.get("request_type", "")),
        "model": extra.get("model", ""),
        "status_code": extra.get("status_code", ""),
        "error_message": extra.get("error_message", ""),
        "message": message,
    }
    log_format = LOG_FORMAT_DEBUG if DEBUG else LOG_FORMAT_NORMAL
    formatted_log = log_format % log_values
    if extra.get("error_message") and not DEBUG:
        formatted_log += f"｜错误详情：{extra['error_message']}"
    if extra.get("request_id"):
        formatted_log += f"｜请求编号 {extra['request_id']}｜路径 {extra.get('path', '')}"
    if extra.get('attempt_id'):
        formatted_log += f"｜上游尝试 {extra['attempt_id']}"
    if extra.get('reason'):
        formatted_log += f"｜原因 {extra['reason']}"
    for field, label in (("response_preview", "返回预览"), ("reasoning_preview", "思考预览")):
        if extra.get(field):
            formatted_log += f"\n  {label}：{json.dumps(extra[field], ensure_ascii=False)}"

    # 将格式化后的日志添加到日志管理器
    log_entry = {
        "created_ns": time.time_ns(),
        "timestamp": now_str,
        "level": level,
        "level_label": LEVEL_LABELS.get(level, level),
        "key": extra.get("key", ""),
        "request_type": extra.get("request_type", ""),
        "request_type_label": TYPE_LABELS.get(extra.get("request_type", ""), extra.get("request_type", "")),
        "model": extra.get("model", ""),
        "status_code": extra.get("status_code", ""),
        "message": message,
        "error_message": extra.get("error_message", ""),
        "formatted": formatted_log,
    }
    log_entry.update({name: extra[name] for name in REPORT_FIELDS if name in extra})
    log_manager.add_log(log_entry)

    return formatted_log


def vertex_format_log_message(level, message, extra=None):
    extra = extra or {}
    message = redact_log_text(message)
    extra = dict(extra)
    for name in ('error_message', 'reason', 'model'):
        if isinstance(extra.get(name), str):
            extra[name] = redact_log_text(extra[name])
    now_str = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    log_values = {
        "asctime": now_str,
        "levelname": LEVEL_LABELS.get(level, level),
        "vertex_id": extra.get("vertex_id", ""),
        "operation": extra.get("operation", ""),
        "status": extra.get("status", ""),
        "error_message": extra.get("error_message", ""),
        "message": message,
    }
    log_format = VERTEX_LOG_FORMAT_DEBUG if DEBUG else VERTEX_LOG_FORMAT_NORMAL
    formatted_log = log_format % log_values
    if extra.get("error_message") and not DEBUG:
        formatted_log += f"｜错误详情：{extra['error_message']}"

    if extra.get('request_id'):
        formatted_log += f"｜请求编号 {extra['request_id']}"
    if extra.get('attempt_id'):
        formatted_log += f"｜上游尝试 {extra['attempt_id']}"

    # 将格式化后的Vertex日志添加到Vertex日志管理器
    log_entry = {
        "created_ns": time.time_ns(),
        "timestamp": now_str,
        "level": level,
        "level_label": LEVEL_LABELS.get(level, level),
        "request_type": "vertex",
        "request_type_label": "Vertex",
        "model": extra.get("model", ""),
        "status_code": extra.get("status", ""),
        "vertex_id": extra.get("vertex_id", ""),
        "operation": extra.get("operation", ""),
        "status": extra.get("status", ""),
        "message": message,
        "error_message": extra.get("error_message", ""),
        "formatted": formatted_log,
    }
    log_entry.update({name: extra[name] for name in REPORT_FIELDS if name in extra})
    vertex_log_manager.add_log(log_entry)

    return formatted_log


def _merge_extra(extra: dict = None, **kwargs) -> dict:
    """合并 extra 字典与 kwargs（kwargs 覆盖同名键）。

    Cleanup: 原 log() 与 vertex_log() 逐字重复的合并逻辑。
    """
    final_extra = {}
    if extra is not None and isinstance(extra, dict):
        final_extra.update(extra)
    final_extra.update(kwargs)
    report = _current_request_report.get()
    if report is not None:
        final_extra.setdefault('request_id', report.request_id)
        final_extra.setdefault('path', report.path)
        final_extra.setdefault('model', report.model)
    module = sys.modules.get('app.utils.upstream_reporting')
    attempt = module._current_attempt.get() if module else None
    if attempt:
        final_extra.setdefault('attempt_id', attempt['attempt_id'])
    return final_extra


def record_response_usage(usage_metadata):
    """只向当前请求补充用量；不额外输出日志，也不记录原始响应正文。"""
    report = _current_request_report.get()
    if report is not None and usage_metadata is None:
        report.authoritative_usage = dict.fromkeys(report.tokens)
    if report is not None and isinstance(usage_metadata, dict):
        try:
            report.update_event({"usage_metadata": usage_metadata})
        except (TypeError, ValueError, AttributeError):
            pass


def log(level: str, message: str, extra: dict = None, **kwargs):
    final_extra = _merge_extra(extra, **kwargs)
    report = _current_request_report.get()
    if report is not None:
        report.update_event(final_extra)
    msg = format_log_message(level.upper(), message, extra=final_extra)
    getattr(logger, level.lower())(msg)


def vertex_log(level: str, message: str, extra: dict = None, **kwargs):
    final_extra = _merge_extra(extra, **kwargs)
    report = _current_request_report.get()
    if report is not None:
        report.update_event(final_extra)
    msg = vertex_format_log_message(level.upper(), message, extra=final_extra)
    getattr(logger, level.lower())(msg)
