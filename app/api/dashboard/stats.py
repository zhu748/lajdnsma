"""面板统计聚合端点：GET /dashboard-data 与 POST /reset-stats。"""
from datetime import datetime

from fastapi import Depends, HTTPException

from app.api.dashboard import runtime
from app.api.dashboard.auth import require_body_password, require_dashboard_token
from app.services import GeminiClient
from app.utils import log_manager
from app.utils.logging import vertex_log_manager
from app.utils.pvp import sanitize_pvp_selector
from app.utils.stats import api_stats_manager
import app.config.settings as settings


@runtime.dashboard_router.get("/dashboard-data")
async def get_dashboard_data(_: None = Depends(require_dashboard_token)):
    """聚合返回统计 / 日志 / 配置 / 模型列表（面板主数据源）。"""
    # 先清理过期数据，确保统计数据是最新的
    await api_stats_manager.maybe_cleanup()
    # Perf: 此处曾对每次 dashboard 轮询调用 response_cache_manager.
    # clean_expired() —— 该方法持有全局缓存锁做全量扫描，而 maintenance.py
    # 的每分钟定时任务已经在做同样的清理。高频轮询 + 锁全量扫描会把所有
    # 请求的缓存热路径挂起，故移除；maybe_cleanup 内部按小时级节流，保留。
    runtime.active_requests_manager.clean_completed()

    # 使用统计系统获取调用与 Token 消耗数据
    now = datetime.now()
    last_24h_calls = api_stats_manager.get_calls_last_24h()
    hourly_calls = api_stats_manager.get_calls_last_hour(now)
    minute_calls = api_stats_manager.get_calls_last_minute(now)
    last_24h_tokens = api_stats_manager.get_tokens_last_24h()
    hourly_tokens = api_stats_manager.get_tokens_last_hour(now)
    minute_tokens = api_stats_manager.get_tokens_last_minute(now)
    time_series_data, tokens_time_series = api_stats_manager.get_time_series_data(30, now)
    api_key_stats = api_stats_manager.get_api_key_stats(runtime.key_manager.api_keys)

    # 根据 ENABLE_VERTEX 设置决定返回哪种日志
    if settings.ENABLE_VERTEX:
        recent_logs = vertex_log_manager.get_recent_logs(500)  # 最近500条Vertex日志
    else:
        recent_logs = log_manager.get_recent_logs(500)  # 最近500条普通日志

    # 缓存与活跃请求统计
    total_cache = runtime.response_cache_manager.cur_cache_num
    active_count = len(runtime.active_requests_manager.active_requests)
    active_done = sum(
        1 for task in runtime.active_requests_manager.active_requests.values() if task.done()
    )
    active_pending = active_count - active_done

    # 凭证数量
    credentials_count = 0
    if runtime.credential_manager is not None:
        credentials_count = runtime.credential_manager.get_total_credentials()

    return {
        "key_count": len(runtime.key_manager.api_keys),
        "model_count": len(GeminiClient.AVAILABLE_MODELS),
        "available_models": GeminiClient.AVAILABLE_MODELS,
        "responses_default_model": settings.RESPONSES_DEFAULT_MODEL,
        "responses_model_aliases": settings.RESPONSES_MODEL_ALIASES,
        "claude_default_model": settings.CLAUDE_DEFAULT_MODEL,
        "claude_model_aliases": settings.CLAUDE_MODEL_ALIASES,
        "retry_count": settings.MAX_RETRY_NUM,
        "credentials_count": credentials_count,
        "last_24h_calls": last_24h_calls,
        "hourly_calls": hourly_calls,
        "minute_calls": minute_calls,
        "last_24h_tokens": last_24h_tokens,
        "hourly_tokens": hourly_tokens,
        "minute_tokens": minute_tokens,
        "calls_time_series": time_series_data,
        "tokens_time_series": tokens_time_series,
        "current_time": datetime.now().strftime("%H:%M:%S"),
        "logs": recent_logs,
        "api_key_stats": api_key_stats,
        # 配置信息
        "max_requests_per_minute": settings.MAX_REQUESTS_PER_MINUTE,
        "max_requests_per_day_per_ip": settings.MAX_REQUESTS_PER_DAY_PER_IP,
        # 版本信息
        "local_version": settings.version["local_version"],
        "remote_version": settings.version["remote_version"],
        "has_update": settings.version["has_update"],
        # 流式响应配置
        "fake_streaming": settings.FAKE_STREAMING,
        "fake_streaming_interval": settings.FAKE_STREAMING_INTERVAL,
        # 随机字符串配置
        "random_string": settings.RANDOM_STRING,
        "random_string_length": settings.RANDOM_STRING_LENGTH,
        # 联网搜索配置
        "search_mode": settings.search["search_mode"],
        "search_prompt": settings.search["search_prompt"],
        # 思考配置（总闸 / 最高档 / 按模型等级，见 app/utils/thinking.py）
        "enable_thinking": settings.ENABLE_THINKING,
        "max_thinking": getattr(settings, "MAX_THINKING", True),
        "max_thinking_model_levels": getattr(settings, "MAX_THINKING_MODEL_LEVELS", {}),
        # 缓存信息
        "cache_entries": total_cache,
        "cache_expiry_time": settings.CACHE_EXPIRY_TIME,
        "max_cache_entries": settings.MAX_CACHE_ENTRIES,
        # 活跃请求池信息
        "active_count": active_count,
        "active_done": active_done,
        "active_pending": active_pending,
        # 并发请求配置
        "concurrent_requests": settings.CONCURRENT_REQUESTS,
        "increase_concurrent_on_failure": settings.INCREASE_CONCURRENT_ON_FAILURE,
        "max_concurrent_requests": settings.MAX_CONCURRENT_REQUESTS,
        # Key rotation strategy: "fill" (sticky, default) or "polling" (round-robin)
        "key_rotation_strategy": getattr(settings, "KEY_ROTATION_STRATEGY", "fill"),
        # 启用 vertex
        "enable_vertex": settings.ENABLE_VERTEX,
        # Vertex Express 配置
        "enable_vertex_express": settings.ENABLE_VERTEX_EXPRESS,
        "vertex_express_api_key": bool(settings.VERTEX_EXPRESS_API_KEY),  # 只返回是否设置的状态
        "google_credentials_json": bool(settings.GOOGLE_CREDENTIALS_JSON),  # 只返回是否设置的状态
        # 最大重试次数
        "max_retry_num": settings.MAX_RETRY_NUM,
        # 空响应重试次数限制
        "max_empty_responses": settings.MAX_EMPTY_RESPONSES,
        # Round 8（PVP 模式）：指定 key 持续重试
        "pvp_mode": getattr(settings, "PVP_MODE", False),
        # 选择器经 sanitize 后存储/回显：完整密钥只会以尾片段形式出现，
        # 不经由本接口泄露（与 GEMINI_API_KEYS 不回传同一安全决策）。
        "pvp_key": sanitize_pvp_selector(getattr(settings, "PVP_KEY", "")),
        "pvp_max_retries": getattr(settings, "PVP_MAX_RETRIES", 50),
    }


@runtime.dashboard_router.post("/reset-stats")
async def reset_stats(password_data: dict = Depends(require_body_password)):
    """重置 API 调用统计数据。"""
    try:
        await api_stats_manager.reset()
        return {"status": "success", "message": "API调用统计数据已重置"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重置失败：{str(e)}")
