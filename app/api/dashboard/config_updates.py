"""配置热更新端点：POST /update-config。

历史版本是单文件 dashboard.py 中 ~615 行的巨型 if-elif 链（约 30 个
config_key 分支，每个分支自带类型校验 + 副作用 + 持久化）。现改为注册表
分派：每个 config_key 对应一个独立的小 handler，CONFIG_HANDLERS 字典
统一调度，公共的类型校验（bool / int / float / str）收敛为 4 个助手函数。

handler 约定：
- 签名 async handler(config_value) -> Optional[dict]
- 返回 None    → 走通用收尾（save_settings + 统一成功响应）
- 返回 dict    → 端点原样返回（供 google_credentials_json 的"未更新"早退
  等需要自定义响应的场景使用，此时通用收尾不执行）
- 校验失败     → 抛 HTTPException(4xx)，由端点原样上抛
"""
import asyncio
import math

from fastapi import Depends, HTTPException

from app.api.dashboard import runtime
from app.api.dashboard.auth import require_body_password
from app.api.dashboard.model_mapping import (
    _validate_claude_model_mapping,
    _validate_responses_model_mapping,
)
from app.config.persistence import save_settings
from app.services import GeminiClient
from app.utils.logging import log
from app.utils.pvp import resolve_pvp_key, sanitize_pvp_selector
from app.vertex.credentials_manager import prepare_json_credentials
from app.vertex.model_loader import refresh_models_config_cache
from app.vertex.vertex_ai_init import reset_client_pool
import app.config.settings as settings
import app.vertex.config as app_config


# ---------------------------------------------------------------------------
# 类型校验助手（消息文案与历史版本逐字保持一致）
# ---------------------------------------------------------------------------


def _bool_setting(config_value) -> bool:
    if not isinstance(config_value, bool):
        raise HTTPException(status_code=422, detail="参数类型错误：应为布尔值")
    return config_value


def _int_setting(config_value, *, err_msg: str, min_allowed: int) -> int:
    """Accept integer values without silently truncating floats or booleans."""
    try:
        if isinstance(config_value, bool) or not isinstance(config_value, (int, str, float)):
            raise ValueError(err_msg)
        if isinstance(config_value, float) and (not math.isfinite(config_value) or not config_value.is_integer()):
            raise ValueError(err_msg)
        value = int(config_value)
        if value < min_allowed:
            raise ValueError(err_msg)
        return value
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=422, detail=f"参数类型错误：{err_msg}，应为整数") from None


def _float_setting(config_value, *, err_msg: str) -> float:
    try:
        if isinstance(config_value, bool) or not isinstance(config_value, (int, str, float)):
            raise ValueError(err_msg)
        value = float(config_value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(err_msg)
        return value
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=422, detail=f"参数类型错误：{err_msg}，应为有限数字") from None


def _str_setting(config_value, *, err_msg: str = "参数类型错误：应为字符串") -> str:
    if not isinstance(config_value, str):
        raise HTTPException(status_code=422, detail=err_msg)
    return config_value


async def _refresh_available_models(
    keys,
    *,
    probe_msg: str,
    success_msg: str,
    empty_msg: str | None = None,
    error_msg: str = "刷新可用模型列表时发生错误: {error}",
):
    """遍历 keys 逐个尝试拉取可用模型列表，成功即停（search_mode 与
    gemini_api_keys 两个分支共享的实现）。异常整体降级为 warning——与
    历史行为一致：某 key 拉取抛错时不再尝试后续 key。"""
    try:
        for key in keys:
            key_id = f"{hash(key) & 0xFFFFFF:06x}"
            log("info", probe_msg.format(key_id=key_id))
            all_models = await GeminiClient.list_available_models(key)
            GeminiClient.AVAILABLE_MODELS = [
                model.replace("models/", "") for model in all_models
            ]
            if GeminiClient.AVAILABLE_MODELS:
                log("info", success_msg.format(count=len(GeminiClient.AVAILABLE_MODELS)))
                return
        if empty_msg:
            log("warning", empty_msg)
    except Exception as e:
        log("warning", error_msg.format(error=str(e)))


# ---------------------------------------------------------------------------
# 限流 / 并发 / 重试（数值型配置）
# ---------------------------------------------------------------------------


async def _update_max_requests_per_minute(config_value):
    value = _int_setting(config_value, err_msg="每分钟请求限制必须大于0", min_allowed=1)
    settings.MAX_REQUESTS_PER_MINUTE = value
    log("info", f"每分钟请求限制已更新为：{value}")


async def _update_max_requests_per_day_per_ip(config_value):
    value = _int_setting(config_value, err_msg="每IP每日请求限制必须大于0", min_allowed=1)
    settings.MAX_REQUESTS_PER_DAY_PER_IP = value
    log("info", f"每IP每日请求限制已更新为：{value}")


async def _update_concurrent_requests(config_value):
    value = _int_setting(config_value, err_msg="并发请求数必须大于0", min_allowed=1)
    settings.CONCURRENT_REQUESTS = value
    log("info", f"并发请求数已更新为：{value}")


async def _update_increase_concurrent_on_failure(config_value):
    value = _int_setting(
        config_value, err_msg="失败时增加的并发数不能为负数", min_allowed=0
    )
    settings.INCREASE_CONCURRENT_ON_FAILURE = value
    log("info", f"失败时增加的并发数已更新为：{value}")


async def _update_max_concurrent_requests(config_value):
    value = _int_setting(config_value, err_msg="最大并发请求数必须大于0", min_allowed=1)
    settings.MAX_CONCURRENT_REQUESTS = value
    log("info", f"最大并发请求数已更新为：{value}")


async def _update_max_retry_num(config_value):
    value = _int_setting(config_value, err_msg="最大重试次数必须大于0", min_allowed=1)
    settings.MAX_RETRY_NUM = value
    log("info", f"最大重试次数已更新为：{value}")


async def _update_max_empty_responses(config_value):
    value = _int_setting(config_value, err_msg="空响应重试次数不能为负数", min_allowed=0)
    settings.MAX_EMPTY_RESPONSES = value
    log("info", f"空响应重试次数已更新为：{value}")


# ---------------------------------------------------------------------------
# 流式 / 随机字符串 / 联网搜索
# ---------------------------------------------------------------------------


async def _update_fake_streaming(config_value):
    config_value = _bool_setting(config_value)
    settings.FAKE_STREAMING = config_value
    log("info", f"假流式请求已更新为：{config_value}")

    # 同步更新 vertex 配置中的假流式设置（直接改全局变量 + 调用更新函数，
    # 双写以兼容 vertex 侧的影子配置层）
    try:
        app_config.FAKE_STREAMING_ENABLED = config_value
        app_config.update_config("FAKE_STREAMING", config_value)
        log("info", f"已同步更新Vertex中的假流式设置为：{config_value}")
    except Exception as e:
        log("warning", f"更新Vertex假流式设置时出错: {str(e)}")


async def _update_fake_streaming_interval(config_value):
    value = _float_setting(config_value, err_msg="假流式间隔必须大于0")
    settings.FAKE_STREAMING_INTERVAL = value
    log("info", f"假流式间隔已更新为：{value}")

    try:
        app_config.update_config("FAKE_STREAMING_INTERVAL", value)
        log("info", f"已同步更新Vertex中的假流式间隔设置为：{value}")
    except Exception as e:
        log("warning", f"更新Vertex假流式间隔设置时出错: {str(e)}")


async def _update_random_string(config_value):
    config_value = _bool_setting(config_value)
    settings.RANDOM_STRING = config_value
    log("info", f"随机字符串已更新为：{config_value}")


async def _update_random_string_length(config_value):
    value = _int_setting(config_value, err_msg="随机字符串长度必须大于0", min_allowed=1)
    settings.RANDOM_STRING_LENGTH = value
    log("info", f"随机字符串长度已更新为：{value}")


async def _update_search_mode(config_value):
    config_value = _bool_setting(config_value)
    settings.search["search_mode"] = config_value
    log("info", f"联网搜索模式已更新为：{config_value}")

    # 切换 search_mode 时重取一次可用模型列表（重置密钥栈以保证随机性；
    # 与历史版本一致：重置异常降级 warning，不阻断配置更新）
    try:
        runtime.key_manager._reset_key_stack()
    except Exception as e:
        log("warning", f"刷新可用模型列表时发生错误: {str(e)}")
    await _refresh_available_models(
        runtime.key_manager.api_keys,
        probe_msg="使用API密钥 key#{key_id} 刷新可用模型列表",
        success_msg="可用模型列表已更新，当前模型数量：{count}",
        empty_msg="没有可用的API密钥，无法刷新可用模型列表",
    )


async def _update_search_prompt(config_value):
    config_value = _str_setting(config_value)
    settings.search["search_prompt"] = config_value
    log("info", f"联网搜索提示已更新为：{config_value}")


# ---------------------------------------------------------------------------
# Vertex 相关
# ---------------------------------------------------------------------------


async def _update_enable_vertex(config_value):
    config_value = _bool_setting(config_value)
    settings.ENABLE_VERTEX = config_value
    log("info", f"Vertex AI 已更新为：{config_value}")


async def _update_enable_vertex_express(config_value):
    config_value = _bool_setting(config_value)
    settings.ENABLE_VERTEX_EXPRESS = config_value
    log("info", f"Vertex Express已更新为：{config_value}")


async def _update_vertex_express_api_key(config_value):
    config_value = _str_setting(config_value)

    # 空字符串或 "true"（前端把布尔设置误传到该字段时的形态）不更新
    if not config_value or config_value.lower() == "true":
        log("info", "Vertex Express API Key未更新，因为值为空或为'true'")
        return

    settings.VERTEX_EXPRESS_API_KEY = config_value
    # 更新 vertex 影子配置中的 API 密钥列表
    app_config.VERTEX_EXPRESS_API_KEY_VAL = [
        key.strip() for key in config_value.split(",") if key.strip()
    ]
    log(
        "info",
        f"Vertex Express API Key已更新，共{len(app_config.VERTEX_EXPRESS_API_KEY_VAL)}个有效密钥",
    )

    # 尝试刷新模型配置
    try:
        refresh_success = await refresh_models_config_cache()
        if refresh_success:
            log("info", "更新Express API Key后成功刷新模型配置")
        else:
            log(
                "warning",
                "更新Express API Key后刷新模型配置失败，将使用默认模型或现有缓存",
            )
    except Exception as e:
        log("warning", f"尝试刷新模型配置时出错: {str(e)}")


async def _update_google_credentials_json(config_value):
    config_value = _str_setting(
        config_value, err_msg="参数类型错误：Google Credentials JSON 应为字符串"
    ).strip()
    # Blank secret inputs preserve the existing credential, matching the UI.
    if not config_value or config_value.lower() == "true":
        return {"status": "success", "message": "Google Credentials JSON 未更新，保留原凭证"}
    manager = runtime.credential_manager
    if manager is None:
        raise HTTPException(status_code=503, detail="凭证管理器尚未初始化")
    try:
        prepared = await asyncio.to_thread(prepare_json_credentials, config_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # No await between replacing active identities and updating config/pools.
    # Parsing and private-key validation have already succeeded for the batch.
    manager.replace_json_string_credentials(prepared)
    app_config.update_config("GOOGLE_CREDENTIALS_JSON", config_value)
    reset_client_pool()
    log("info", f"Google Credentials JSON 已更新，共 {len(prepared)} 个凭证")
    # Normal endpoint finalization persists once. No remote model-list refresh
    # is needed: changing credentials does not change MODELS_CONFIG_URL.


# ---------------------------------------------------------------------------
# 思考配置（总闸 / 最高档 / 按模型等级）
# ---------------------------------------------------------------------------


async def _update_enable_thinking(config_value):
    # 思考功能总闸：关闭后代理不注入/不覆盖任何思考参数
    # （原生 Gemini 请求自带的 thinkingConfig 仍原样透传）。
    config_value = _bool_setting(config_value)
    settings.ENABLE_THINKING = config_value
    log("info", f"思考功能总开关已更新为：{config_value}")


async def _update_max_thinking(config_value):
    # 最高思考强度：开启后忽略请求携带的思考强度，
    # 对所有支持思考的模型强制使用其家族最高档。
    config_value = _bool_setting(config_value)
    settings.MAX_THINKING = config_value
    log("info", f"最高思考强度已更新为：{config_value}")


async def _update_max_thinking_model_levels(config_value):
    # 按模型指定思考等级（优先级高于全局最高档开关）。
    # 形如 {"gemini-3.8-flash": "low", "gemini-2.5-flash": 16384}；
    # 值支持等级字符串 / off / dynamic / 数字，运行时统一由
    # app/utils/thinking.py 校验与换算，此处先做类型硬校验拒绝
    # 垃圾数据落盘。
    if not isinstance(config_value, dict):
        raise HTTPException(
            status_code=422,
            detail="参数类型错误：max_thinking_model_levels 应为 JSON 对象（模型名 → 等级）",
        )
    cleaned = {}
    for k, v in config_value.items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"模型 {key} 的思考等级值无效："
                    "应为 minimal/low/medium/high、off、dynamic 或数字"
                ),
            )
        if isinstance(v, str) and v.strip().lower() in {
            "minimal", "low", "medium", "high", "off", "none", "disable", "disabled", "dynamic", "auto",
        }:
            cleaned[key] = v.strip().lower()
        else:
            cleaned[key] = _int_setting(v, err_msg=f"模型 {key} 的 budget 必须为不小于 -1 的整数", min_allowed=-1)
    settings.MAX_THINKING_MODEL_LEVELS = cleaned
    log("info", f"按模型思考等级已更新，共 {len(cleaned)} 项")


# ---------------------------------------------------------------------------
# 密钥池 / 轮换策略 / PVP
# ---------------------------------------------------------------------------


async def _update_gemini_api_keys(config_value):
    if not isinstance(config_value, str):
        raise HTTPException(
            status_code=422, detail="参数类型错误：API密钥应为逗号分隔的字符串"
        )

    # 将换行符替换为逗号，然后分割并清理API密钥
    config_value = config_value.replace("\n", ",").replace("\r", ",")
    new_keys = [key.strip() for key in config_value.split(",") if key.strip()]
    if not new_keys:
        raise HTTPException(status_code=400, detail="未提供有效的API密钥")

    # 添加到现有的API密钥字符串中（合并去重）
    current_keys = (
        settings.GEMINI_API_KEYS.split(",") if settings.GEMINI_API_KEYS else []
    )
    current_keys = [key.strip() for key in current_keys if key.strip()]
    all_keys = list(set(current_keys + new_keys))
    settings.GEMINI_API_KEYS = ",".join(all_keys)

    # 计算新添加的密钥数量
    added_key_count = 0
    for key in new_keys:
        if key not in runtime.key_manager.api_keys:
            runtime.key_manager.api_keys.append(key)
            added_key_count += 1

    # 重置密钥栈
    runtime.key_manager._reset_key_stack()

    # 如果可用模型为空，尝试用新添加的密钥获取模型列表
    if not GeminiClient.AVAILABLE_MODELS:
        await _refresh_available_models(
            new_keys,
            probe_msg="使用新添加的API密钥 key#{key_id} 获取可用模型列表",
            success_msg="成功获取可用模型列表，共 {count} 个模型",
            error_msg="获取可用模型列表时发生错误: {error}",
        )

    log(
        "info",
        f"已添加 {added_key_count} 个新API密钥，当前共有 {len(runtime.key_manager.api_keys)} 个",
    )


async def _update_key_rotation_strategy(config_value):
    # Toggle between "fill" (sticky, anti-fingerprint default) and
    # "polling" (round-robin, legacy behaviour).
    if not isinstance(config_value, str):
        raise HTTPException(
            status_code=422,
            detail="参数类型错误：key_rotation_strategy 应为字符串 'fill' 或 'polling'",
        )
    normalized = config_value.strip().lower()
    if normalized not in ("fill", "polling"):
        raise HTTPException(
            status_code=422,
            detail=f"不支持的 key_rotation_strategy 值：{config_value}（仅支持 'fill' 或 'polling'）",
        )
    settings.KEY_ROTATION_STRATEGY = normalized
    # 重置密钥栈，让新策略立即生效，而不是延续先前弹出的栈状态
    runtime.key_manager._reset_key_stack()
    log(
        "info",
        f"密钥轮换策略已更新为：{normalized} "
        f"（{'固定使用一个密钥直到额度耗尽' if normalized == 'fill' else '轮询使用全部密钥'}）",
    )


async def _update_pvp_mode(config_value):
    # Round 8（PVP）：开启后所有请求钉在 pvp_key 指定的 key 上
    # 持续重试（见 app/utils/pvp.py）。
    # 注：保留历史专属校验文案（其余 bool 配置用通用文案）。
    if not isinstance(config_value, bool):
        raise HTTPException(
            status_code=422, detail="参数类型错误：pvp_mode 应为布尔值"
        )
    settings.PVP_MODE = config_value
    log("info", f"PVP 模式已更新为：{config_value}")
    # 开启时若未指定 Key，明确提示不会生效（避免运维以为已生效
    # 却在日志里找不到 PVP 行为的原因）。
    if config_value and not str(getattr(settings, "PVP_KEY", "") or "").strip():
        log(
            "warning",
            "PVP 模式已开启但未指定 Key（pvp_key 为空），"
            "本次开启不会生效；请在面板填写指定 Key",
        )


async def _update_pvp_key(config_value):
    # 钉住的 key 选择器。完整密钥会被 sanitize 成尾片段（不落盘
    # 明文、不经 API 回显）；序号/哈希/片段写法原样保留。
    if not isinstance(config_value, str):
        raise HTTPException(status_code=422, detail="参数类型错误：pvp_key 应为字符串")
    selector = sanitize_pvp_selector(config_value)
    settings.PVP_KEY = selector

    if not selector:
        log("info", "PVP 指定密钥已清空（PVP 模式将不再生效）")
    else:
        # 尽力校验：解析失败不拒绝保存 —— 密钥池可能在之后变化
        # （新增密钥/重测后恢复），届时选择器会自动生效。
        # force=True：允许"先存 key、后开 PVP 模式"的操作顺序。
        try:
            resolved = resolve_pvp_key(runtime.key_manager, force=True)
        except Exception:
            resolved = None
        if resolved:
            log(
                "info",
                f"PVP 指定密钥已设置，匹配到 key#{hash(resolved) & 0xFFFFFF:06x}",
            )
        else:
            log(
                "warning",
                "PVP 指定密钥已保存，但当前密钥池未匹配到该标识；"
                "密钥池变化后自动生效，期间回退正常轮换策略",
            )


async def _update_pvp_max_retries(config_value):
    # PVP 模式的防无限重试安全阀（最少 1 次）。
    value = _int_setting(config_value, err_msg="PVP 最大重试次数必须大于等于 1", min_allowed=1)
    settings.PVP_MAX_RETRIES = value
    log("info", f"PVP 最大重试次数已更新为：{value}")


# ---------------------------------------------------------------------------
# 模型映射（Responses / Claude）
# ---------------------------------------------------------------------------


async def _update_responses_default_model(config_value):
    value, _ = _validate_responses_model_mapping(
        config_value, settings.RESPONSES_MODEL_ALIASES
    )
    settings.RESPONSES_DEFAULT_MODEL = value
    log("info", f"Responses 默认模型已更新为：{value or '自动选择'}")


async def _update_responses_model_aliases(config_value):
    _, aliases = _validate_responses_model_mapping(
        settings.RESPONSES_DEFAULT_MODEL, config_value
    )
    settings.RESPONSES_MODEL_ALIASES = aliases
    log("info", f"Responses 模型别名已更新，共 {len(aliases)} 条")


async def _update_claude_default_model(config_value):
    value, _ = _validate_claude_model_mapping(
        config_value, settings.CLAUDE_MODEL_ALIASES
    )
    settings.CLAUDE_DEFAULT_MODEL = value
    log("info", f"Claude 默认模型已更新为：{value or '自动选择'}")


async def _update_claude_model_aliases(config_value):
    _, aliases = _validate_claude_model_mapping(
        settings.CLAUDE_DEFAULT_MODEL, config_value
    )
    settings.CLAUDE_MODEL_ALIASES = aliases
    log("info", f"Claude 模型别名已更新，共 {len(aliases)} 条")


# ---------------------------------------------------------------------------
# 注册表与端点
# ---------------------------------------------------------------------------

CONFIG_HANDLERS = {
    # 限流 / 并发 / 重试
    "max_requests_per_minute": _update_max_requests_per_minute,
    "max_requests_per_day_per_ip": _update_max_requests_per_day_per_ip,
    "concurrent_requests": _update_concurrent_requests,
    "increase_concurrent_on_failure": _update_increase_concurrent_on_failure,
    "max_concurrent_requests": _update_max_concurrent_requests,
    "max_retry_num": _update_max_retry_num,
    "max_empty_responses": _update_max_empty_responses,
    # 流式 / 随机字符串 / 联网搜索
    "fake_streaming": _update_fake_streaming,
    "fake_streaming_interval": _update_fake_streaming_interval,
    "random_string": _update_random_string,
    "random_string_length": _update_random_string_length,
    "search_mode": _update_search_mode,
    "search_prompt": _update_search_prompt,
    # Vertex
    "enable_vertex": _update_enable_vertex,
    "enable_vertex_express": _update_enable_vertex_express,
    "vertex_express_api_key": _update_vertex_express_api_key,
    "google_credentials_json": _update_google_credentials_json,
    # 思考
    "enable_thinking": _update_enable_thinking,
    "max_thinking": _update_max_thinking,
    "max_thinking_model_levels": _update_max_thinking_model_levels,
    # 密钥池 / 轮换 / PVP
    "gemini_api_keys": _update_gemini_api_keys,
    "key_rotation_strategy": _update_key_rotation_strategy,
    "pvp_mode": _update_pvp_mode,
    "pvp_key": _update_pvp_key,
    "pvp_max_retries": _update_pvp_max_retries,
    # 模型映射
    "responses_default_model": _update_responses_default_model,
    "responses_model_aliases": _update_responses_model_aliases,
    "claude_default_model": _update_claude_default_model,
    "claude_model_aliases": _update_claude_model_aliases,
}


@runtime.dashboard_router.post("/update-config")
async def update_config(config_data: dict = Depends(require_body_password)):
    """更新配置项（按 CONFIG_HANDLERS 注册表分派）。"""
    config_key = config_data.get("key")
    config_value = config_data.get("value")

    if not isinstance(config_key, str) or not config_key:
        raise HTTPException(status_code=400, detail="配置项键名必须为非空字符串")

    handler = CONFIG_HANDLERS.get(config_key)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"不支持的配置项：{config_key}")

    try:
        custom_response = await handler(config_value)
        if custom_response is not None:
            return custom_response
        save_settings()
        return {"status": "success", "message": f"配置项 {config_key} 已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失败：{str(e)}")
