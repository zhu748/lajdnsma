import logging
from datetime import datetime
from collections import deque
from threading import Lock

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
        with self.lock:
            self.logs.append(log_entry)

    def get_recent_logs(self, count=50):
        with self.lock:
            return list(self.logs)[-count:]


# 创建日志管理器实例 (输出到前端)
log_manager = LogManager()

# 兼容别名：Vertex 日志同样使用 LogManager（原实现为重复类）
VertexLogManager = LogManager
# 创建Vertex日志管理器实例 (输出到前端)
vertex_log_manager = VertexLogManager()


def format_log_message(level, message, extra=None):
    extra = extra or {}
    # Perf: 时间戳只计算一次（旧实现每条日志调用两次
    # datetime.now().strftime，log() 是每请求多次的热路径）
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_values = {
        "asctime": now_str,
        "levelname": level,
        "key": extra.get("key", ""),
        "request_type": extra.get("request_type", ""),
        "model": extra.get("model", ""),
        "status_code": extra.get("status_code", ""),
        "error_message": extra.get("error_message", ""),
        "message": message,
    }
    log_format = LOG_FORMAT_DEBUG if DEBUG else LOG_FORMAT_NORMAL
    formatted_log = log_format % log_values

    # 将格式化后的日志添加到日志管理器
    log_entry = {
        "timestamp": now_str,
        "level": level,
        "key": extra.get("key", ""),
        "request_type": extra.get("request_type", ""),
        "model": extra.get("model", ""),
        "status_code": extra.get("status_code", ""),
        "message": message,
        "error_message": extra.get("error_message", ""),
        "formatted": formatted_log,
    }
    log_manager.add_log(log_entry)

    return formatted_log


def vertex_format_log_message(level, message, extra=None):
    extra = extra or {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_values = {
        "asctime": now_str,
        "levelname": level,
        "vertex_id": extra.get("vertex_id", ""),
        "operation": extra.get("operation", ""),
        "status": extra.get("status", ""),
        "error_message": extra.get("error_message", ""),
        "message": message,
    }
    log_format = VERTEX_LOG_FORMAT_DEBUG if DEBUG else VERTEX_LOG_FORMAT_NORMAL
    formatted_log = log_format % log_values

    # 将格式化后的Vertex日志添加到Vertex日志管理器
    log_entry = {
        "timestamp": now_str,
        "level": level,
        "vertex_id": extra.get("vertex_id", ""),
        "operation": extra.get("operation", ""),
        "status": extra.get("status", ""),
        "message": message,
        "error_message": extra.get("error_message", ""),
        "formatted": formatted_log,
    }
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
    return final_extra


def log(level: str, message: str, extra: dict = None, **kwargs):
    final_extra = _merge_extra(extra, **kwargs)
    msg = format_log_message(level.upper(), message, extra=final_extra)
    getattr(logger, level.lower())(msg)


def vertex_log(level: str, message: str, extra: dict = None, **kwargs):
    final_extra = _merge_extra(extra, **kwargs)
    msg = vertex_format_log_message(level.upper(), message, extra=final_extra)
    getattr(logger, level.lower())(msg)
