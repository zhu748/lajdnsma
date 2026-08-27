import sys
import asyncio
import traceback

# from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # 替换为异步调度器
from app.utils.logging import log
from app.utils.stats import api_stats_manager
from app.utils import check_version
from zoneinfo import ZoneInfo
from app.config import persistence


def handle_exception(exc_type, exc_value, exc_traceback):
    """
    全局异常处理函数

    处理未捕获的异常，并记录到日志中

    Round 6（详细日志）：旧实现丢弃了 exc_traceback —— 未捕获异常
    只记 str(exc_value)，无法定位堆栈位置，线上排障只能靠猜。现在
    附带最后 5 帧的精简堆栈（完整堆栈对环形日志缓冲太长，末尾几帧
    已覆盖绝大多数定位需求）。
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.excepthook(exc_type, exc_value, exc_traceback)
        return
    from app.utils.error_handling import translate_error

    error_message = translate_error(str(exc_value))
    # 精简堆栈：取末尾 5 帧，每帧一行「文件:行号 函数」。
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    tail = "".join(tb_lines[-8:]) if tb_lines else ""
    if len(tail) > 800:
        tail = tail[-800:]
    log(
        "error",
        f"未捕获的异常: {error_message}",
        status_code=500,
        error_message=tail or error_message,
    )


def _async_exception_handler(loop, context):
    """Round 6（详细日志）：asyncio 事件循环的异常钩子。

    此前未被 retrieve 的 Task 异常 / 回调异常走 asyncio 默认 handler
    —— 只打印到 stderr，**不进面板日志**（LogManager），运维在网页
    上看不到这些错误。现在统一路由到面板日志。
    """
    exception = context.get("exception")
    message = context.get("message", "Unhandled error in event loop")
    detail = ""
    if exception is not None:
        try:
            from app.utils.error_handling import translate_error

            detail = translate_error(str(exception))
            tb = traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
            tail = "".join(tb[-5:]) if tb else ""
            if len(tail) > 600:
                tail = tail[-600:]
            detail = (detail + " | " + tail).strip(" |")
        except Exception:
            pass
    log(
        "error",
        f"asyncio 未处理异常: {message}",
        status_code=500,
        error_message=detail,
    )


def install_async_exception_handler():
    """把 _async_exception_handler 挂到当前运行的事件循环上。

    在 FastAPI lifespan 的 startup 阶段调用一次。重复调用安全
    （同一 loop 上幂等）。
    """
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_async_exception_handler)
    except RuntimeError:
        pass


# Module-level handle to the scheduler so the FastAPI shutdown event
# can stop it cleanly (previously the scheduler was a local variable
# that escaped scope on shutdown, leaving background jobs orphaned).
_scheduler: AsyncIOScheduler | None = None


def schedule_cache_cleanup(response_cache_manager, active_requests_manager):
    """
    设置定期清理缓存和活跃请求的定时任务
    顺便定时检查更新
    Args:
        response_cache_manager: 响应缓存管理器实例
        active_requests_manager: 活跃请求管理器实例
    """
    global _scheduler
    beijing_tz = ZoneInfo("Asia/Shanghai")
    _scheduler = AsyncIOScheduler(
        timezone=beijing_tz
    )  # 使用 AsyncIOScheduler 替代 BackgroundScheduler

    # 添加任务时直接传递异步函数（无需额外包装）
    _scheduler.add_job(response_cache_manager.clean_expired, "interval", minutes=1)
    _scheduler.add_job(active_requests_manager.clean_completed, "interval", seconds=30)
    _scheduler.add_job(
        active_requests_manager.clean_long_running,
        "interval",
        minutes=5,
        # Round 6: 300s 与上游 600s（现分层 read=300s）超时矛盾 ——
        # 非流式 + thinking 的合法长请求会在 300s 被 cancel，而
        # CancelledError 不被 except Exception 捕获，直接冒泡成模糊
        # 500。阈值抬到 620s（> 任意单阶段超时上限），只杀真正泄漏的
        # 僵尸任务。
        args=[620],
    )

    # Cleanup: 旧实现用「新建 event loop 的同步包装器」来跑这两个协程
    # ——但 AsyncIOScheduler 本身就在主事件循环上调度，原生支持直接
    # 注册协程函数。每 5 分钟/每天白建一个 event loop 既慢又容易踩
    # 「loop 与共享资源绑定的循环不一致」的坑。改为直接注册。
    _scheduler.add_job(api_stats_manager.cleanup, "interval", minutes=5)

    _scheduler.add_job(check_version, "interval", hours=4)
    _scheduler.add_job(api_call_stats_clean, "cron", hour=15, minute=0)
    _scheduler.start()
    return _scheduler


async def shutdown_scheduler():
    """Stop the background scheduler cleanly.

    Called from the FastAPI shutdown event.  Without this, the
    AsyncIOScheduler would keep running background jobs after the
    HTTP server stopped accepting connections, which raises
    `RuntimeError: Event loop is closed` on the next tick.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    # Also signal the stats manager's worker thread to stop.
    try:
        api_stats_manager.shutdown()
    except Exception as e:
        log("error", f"Stats manager shutdown error: {str(e)}")


async def api_call_stats_clean():
    """
    每天定时重置API调用统计数据

    使用新的统计系统重置
    """
    from app.utils.logging import log

    try:
        # 记录重置前的状态
        log("info", "开始重置API调用统计数据")

        # 使用新的统计系统重置
        await api_stats_manager.reset()

        log("info", "API调用统计数据已成功重置")
        persistence.save_settings()

    except Exception as e:
        log("error", f"重置API调用统计数据时发生错误: {str(e)}")
        raise
