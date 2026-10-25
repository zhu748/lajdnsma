"""Responses / Claude 模型映射端点与共享校验逻辑。

历史版本中 update-responses-model-mapping 与 update-claude-model-mapping
两个端点逐行同构（各 35 行，仅校验函数与 settings 字段不同），且
update_config 中还有四个同构的映射配置分支。现收敛为：

- _validate_model_mapping: 统一校验（默认模型 + 别名白名单约束）
- _update_model_mapping:   参数化的端点实现（validator / settings 字段 /
  响应键名 / 日志标签作为参数注入）
"""
from fastapi import Depends, HTTPException

from app.api.dashboard import runtime
from app.api.dashboard.auth import require_body_password
from app.config.persistence import save_settings
from app.services import GeminiClient
from app.utils.logging import log
import app.config.settings as settings


def _validate_model_mapping(default_model, aliases, label="Model"):
    """校验默认模型与别名表，返回 (default_model, cleaned_aliases)。

    校验规则：
    - default_model 可为空字符串（表示 auto），否则必须在可用模型列表内；
    - aliases 的键不能含空白、不能与真实模型名冲突，值必须指向可用模型。
    """
    if not isinstance(default_model, str):
        raise HTTPException(status_code=422, detail=f"{label} default model must be a string")

    default_model = default_model.strip()
    if default_model and default_model not in GeminiClient.AVAILABLE_MODELS:
        raise HTTPException(status_code=422, detail=f"{label} default model is not in available models")

    if not isinstance(aliases, dict):
        raise HTTPException(status_code=422, detail=f"{label} model aliases must be an object")

    cleaned_aliases = {}
    for alias, target in aliases.items():
        alias_name = str(alias).strip()
        target_model = str(target).strip()
        if not alias_name or not target_model:
            continue
        if any(ch.isspace() for ch in alias_name):
            raise HTTPException(status_code=422, detail=f"Alias {alias_name} must not contain whitespace")
        if alias_name in GeminiClient.AVAILABLE_MODELS:
            raise HTTPException(status_code=422, detail=f"Alias {alias_name} conflicts with an available model")
        if target_model not in GeminiClient.AVAILABLE_MODELS:
            raise HTTPException(
                status_code=422,
                detail=f"Alias {alias_name} target model is not in available models",
            )
        cleaned_aliases[alias_name] = target_model

    return default_model, cleaned_aliases


def _validate_responses_model_mapping(default_model, aliases):
    return _validate_model_mapping(default_model, aliases, "Responses")


def _validate_claude_model_mapping(default_model, aliases):
    return _validate_model_mapping(default_model, aliases, "Claude")


async def _update_model_mapping(
    config_data: dict,
    validate,
    default_attr: str,
    aliases_attr: str,
    response_prefix: str,
    log_label: str,
):
    """原子更新某一协议的默认模型 + 别名表（两个端点的共享实现）。

    validate:               (default_model, aliases) -> (default, cleaned)
    default_attr/aliases_attr: settings 上的字段名
    response_prefix:        响应 JSON 的键名前缀（responses / claude）
    log_label:              日志中的协议标签
    """
    default_model, aliases = validate(
        config_data.get("default_model", ""),
        config_data.get("aliases", {}),
    )
    setattr(settings, default_attr, default_model)
    setattr(settings, aliases_attr, aliases)
    save_settings()
    log(
        "info",
        f"{log_label} 模型映射已更新：默认模型={default_model or '自动选择'}，别名={len(aliases)} 条",
    )
    return {
        "status": "success",
        "message": f"{log_label} model mapping updated",
        f"{response_prefix}_default_model": default_model,
        f"{response_prefix}_model_aliases": aliases,
    }


@runtime.dashboard_router.post("/update-responses-model-mapping")
async def update_responses_model_mapping(config_data: dict = Depends(require_body_password)):
    """原子更新 Responses 默认模型与自定义别名。"""
    try:
        return await _update_model_mapping(
            config_data,
            _validate_responses_model_mapping,
            "RESPONSES_DEFAULT_MODEL",
            "RESPONSES_MODEL_ALIASES",
            "responses",
            "Responses",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")


@runtime.dashboard_router.post("/update-claude-model-mapping")
async def update_claude_model_mapping(config_data: dict = Depends(require_body_password)):
    """原子更新 Claude 默认模型与自定义别名。"""
    try:
        return await _update_model_mapping(
            config_data,
            _validate_claude_model_mapping,
            "CLAUDE_DEFAULT_MODEL",
            "CLAUDE_MODEL_ALIASES",
            "claude",
            "Claude",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")
