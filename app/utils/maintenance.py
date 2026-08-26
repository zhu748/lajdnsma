import sys
import asyncio

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
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.excepthook(exc_type, exc_value, exc_traceback)
        return
    from app.utils.error_handling import translate_error

    error_message = translate_error(str(exc_value))
    log(
        "error",
        f"未捕获的异常: {error_message}",
        status_code=500,
        error_message=error_message,
    )


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
        active_requests_manager.clean_long_running, "interval", minutes=5, args=[300]
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
