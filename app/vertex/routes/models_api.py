import time
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List, Dict, Any
from app.vertex.auth import get_api_key, validate_api_key
from app.vertex.model_names import (
    EXPERIMENTAL_MARKER,
    OPENAI_DIRECT_SUFFIX,
    PAY_PREFIX,
)
from app.vertex.model_loader import (
    get_vertex_models,
    get_vertex_express_models,
    get_models_config,
    refresh_models_config_cache,
)
import app.vertex.config as app_config
from app.vertex.credentials_manager import get_credential_manager
from app.utils.logging import vertex_log

router = APIRouter(prefix="/models")

# Setup security
security = HTTPBearer()


@router.get("/list")
async def list_models_basic(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    List available models for Vertex generation.

    （历史遗留：本函数原名 list_models，与下方 /v1/models 端点的同名函数
    在模块命名空间相互遮蔽——路由注册不受影响，但 models_api.list_models
    符号意外指向后一个定义。现显式改名消除遮蔽。）
    """
    # Validate API key
    # Bug fix: 此前传入的是 HTTPAuthorizationCredentials 对象而非其中的
    # token 字符串——validate_api_key 的 isinstance(str) 检查恒 False，
    # 导致 /models/list 恒 401。对齐 auth.get_api_key 的正确用法。
    if not validate_api_key(credentials.credentials):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Get available models
    vertex_log("info", "Retrieving list of available models")
    standard_models = await get_vertex_models()
    express_models = await get_vertex_express_models()

    # Combine and format
    all_models = []

    # Format standard models
    for model_name in standard_models:
        all_models.append(
            {
                "id": model_name,
                "object": "model",
                "created": 1677610602,  # Placeholder timestamp
                "owned_by": "google",
                "permission": [],
                "root": model_name,
                "parent": None,
                "pricing": {
                    "prompt": "0.0020",  # Placeholder pricing
                    "completion": "0.0020",  # Placeholder pricing
                },
            }
        )

    # Format express models
    for model_name in express_models:
        all_models.append(
            {
                "id": model_name,
                "object": "model",
                "created": 1677610602,  # Placeholder timestamp
                "owned_by": "google-express",
                "permission": [],
                "root": model_name,
                "parent": None,
                "pricing": {
                    "prompt": "0.0010",  # Lower placeholder pricing for express
                    "completion": "0.0010",  # Lower placeholder pricing for express
                },
            }
        )

    # Add encryption variants if available
    for base_model in standard_models:
        encrypt_model_name = f"{base_model}-encrypt-full"
        all_models.append(
            {
                "id": encrypt_model_name,
                "object": "model",
                "created": 1677610602,  # Placeholder timestamp
                "owned_by": "google-secure",
                "permission": [],
                "root": base_model,
                "parent": base_model,
                "pricing": {
                    "prompt": "0.0025",  # Higher placeholder pricing for encrypt
                    "completion": "0.0025",  # Higher placeholder pricing for encrypt
                },
            }
        )

    vertex_log("info", f"Found {len(all_models)} available models")

    return {"object": "list", "data": all_models}


@router.post("/refresh")
async def refresh_models(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Refresh the models configuration cache.

    Forces a refresh of the models available from the configuration source.
    """
    # Validate API key（bug fix 同 /list：此前传入 credentials 对象恒 401）
    if not validate_api_key(credentials.credentials):
        raise HTTPException(status_code=401, detail="Invalid API key")

    vertex_log("info", "Attempting to refresh models configuration")

    # Attempt refresh
    success = await refresh_models_config_cache()

    if success:
        # Get updated counts
        standard_models = await get_vertex_models()
        express_models = await get_vertex_express_models()

        vertex_log(
            "info",
            f"Models refresh successful. Standard: {len(standard_models)}, Express: {len(express_models)}",
        )

        return {
            "success": True,
            "message": "Models configuration refreshed successfully",
            "models_count": {
                "standard": len(standard_models),
                "express": len(express_models),
                "total": len(standard_models) + len(express_models),
            },
        }
    else:
        vertex_log("error", "Failed to refresh models configuration")

        raise HTTPException(
            status_code=500, detail="Failed to refresh models configuration"
        )


@router.get("/v1/models")
async def list_models(fastapi_request: Request, api_key: str = Depends(get_api_key)):
    # Perf fix: previously forced `refresh_models_config_cache()` on EVERY
    # request — an outbound HTTP call (up to 3 retries with backoff when the
    # config host is slow) executed while holding the module-wide cache
    # lock, stalling every concurrent model-list request and the chat
    # path's model validation.  We now read the cache (fetching only when
    # empty); the explicit /models/refresh endpoint remains for forced
    # refreshes, and startup pre-warms the cache once.
    await get_models_config()

    # 获取credential_manager（共享助手，缺失时兜底新建）
    credential_manager_instance = get_credential_manager(fastapi_request)

    # 检查是否有SA凭证和Express Key（共享解析助手）
    has_sa_creds = credential_manager_instance.get_total_credentials() > 0
    express_keys = app_config.resolve_express_api_keys()
    has_express_key = bool(express_keys)

    raw_vertex_models = await get_vertex_models()
    raw_express_models = await get_vertex_express_models()

    candidate_model_ids = set()

    if has_express_key:
        candidate_model_ids.update(raw_express_models)
        # If *only* express key is available, only express models (and their variants) should be listed.
        # The current `vertex_model_ids` from remote config might contain non-express models.
        # The `get_vertex_express_models()` should be the source of truth for express-eligible base models.
        if not has_sa_creds:
            # Only list models that are explicitly in the express list.
            # Suffix generation will apply only to these if they are not gemini-2.0
            all_model_ids = set(raw_express_models)
        else:
            # Both SA and Express are available, combine all known models
            all_model_ids = set(raw_vertex_models + raw_express_models)
    elif has_sa_creds:
        # Only SA creds available, use all vertex_models (which might include express-eligible ones)
        all_model_ids = set(raw_vertex_models)
    else:
        # No credentials available
        all_model_ids = set()

    # Create extended model list with variations (search, encrypt, auto etc.)
    # This logic might need to be more sophisticated based on actual supported features per base model.
    # For now, let's assume for each base model, we might have these variations.
    # A better approach would be if the remote config specified these variations.

    dynamic_models_data: List[Dict[str, Any]] = []
    current_time = int(time.time())

    def add_suffix_variants(
        suffixes: List[str],
        log_label: str,
        base_model_without_prefix: str,
        current_display_prefix: str,
        is_express_model: bool,
    ):
        """为当前基础模型追加上后缀变体（保留 EXPRESS/PAY 显示前缀）。

        历史版本中标准后缀 / flash 特殊后缀 / pro 特殊后缀三段近同构代码
        复制三遍，现收敛为单一实现。"""
        for suffix in suffixes:
            suffixed_model_part = f"{base_model_without_prefix}{suffix}"
            # Retain EXPRESS prefix if original model had it
            if is_express_model:
                final_suffixed_display_id = f"[EXPRESS] {suffixed_model_part}"
            else:
                # 非EXPRESS模型的后缀版本也需要正确显示PAY前缀
                final_suffixed_display_id = (
                    f"{current_display_prefix}{suffixed_model_part}"
                )

            # Check if this suffixed ID is already in all_model_ids (unlikely with prefix) or already added
            if final_suffixed_display_id not in all_model_ids and not any(
                m["id"] == final_suffixed_display_id for m in dynamic_models_data
            ):
                vertex_log(
                    "info", f"添加{log_label}模型到列表: {final_suffixed_display_id}"
                )
                dynamic_models_data.append(
                    {
                        "id": final_suffixed_display_id,
                        "object": "model",
                        "created": current_time,
                        "owned_by": "google",
                        "permission": [],
                        "root": base_model_without_prefix,
                        "parent": None,
                    }
                )

    # Add base models and their variations
    for original_model_id in sorted(list(all_model_ids)):
        current_display_prefix = ""
        # 检查是否为[EXPRESS]模型，保留原始前缀
        is_express_model = original_model_id.startswith("[EXPRESS]")
        base_model_without_prefix = original_model_id

        # 如果是EXPRESS模型，移除前缀用于后续处理，但在显示时会保留
        if is_express_model:
            # 从原始ID中提取不带[EXPRESS]前缀的模型名
            base_model_without_prefix = original_model_id[len("[EXPRESS] ") :]
            vertex_log(
                "info",
                f"处理EXPRESS模型: {original_model_id}, 基础名称: {base_model_without_prefix}",
            )

        # 只有非EXPRESS模型才考虑添加PAY_PREFIX
        if (
            not is_express_model
            and has_sa_creds
            and EXPERIMENTAL_MARKER not in original_model_id
        ):
            # 只要有SA凭证就应该显示PAY前缀，不管是否有Express Key
            current_display_prefix = PAY_PREFIX
            vertex_log(
                "info",
                f"为非EXPRESS模型添加PAY前缀: {current_display_prefix}{original_model_id}",
            )

        # 构建显示ID，如果是EXPRESS模型，确保保留[EXPRESS]前缀
        base_display_id = (
            original_model_id
            if is_express_model
            else f"{current_display_prefix}{original_model_id}"
        )

        vertex_log("info", f"添加模型到列表: {base_display_id}")
        dynamic_models_data.append(
            {
                "id": base_display_id,
                "object": "model",
                "created": current_time,
                "owned_by": "google",
                "permission": [],
                "root": base_model_without_prefix,
                "parent": None,
            }
        )

        # Conditionally add common variations (standard suffixes)
        if not base_model_without_prefix.startswith(
            "gemini-2.0"
        ):  # Suffix rules based on original_model_id
            add_suffix_variants(
                ["-search", "-encrypt", "-encrypt-full", "-auto"],
                "后缀",
                base_model_without_prefix,
                current_display_prefix,
                is_express_model,
            )

        # Apply special suffixes for models starting with "gemini-2.5-flash"
        if base_model_without_prefix.startswith("gemini-2.5-flash"):
            add_suffix_variants(
                ["-nothinking", "-max"],
                "特殊后缀",
                base_model_without_prefix,
                current_display_prefix,
                is_express_model,
            )

        # Apply special suffixes for gemini-2.5-pro-preview-06-05
        if base_model_without_prefix == "gemini-2.5-pro-preview-06-05":
            add_suffix_variants(
                ["-nothinking", "-max"],
                "gemini-2.5-pro-preview-06-05特殊后缀",
                base_model_without_prefix,
                current_display_prefix,
                is_express_model,
            )

        # Ensure uniqueness again after adding suffixes
        # Add OpenAI direct variations if SA creds are available
        if has_sa_creds:  # OpenAI direct mode only works with SA credentials
            # `all_model_ids` contains the comprehensive list of base models that are eligible based on current credentials
            # We iterate through this to determine which ones get an -openai variation.
            # `raw_vertex_models` is used here to ensure we only add -openai suffix to models that are
            # fundamentally Vertex models, not just any model that might appear in `all_model_ids` (e.g. from Express list exclusively)
            # if express only key is provided.
            # We iterate through the base models from the main Vertex list.
            for base_model_id_for_openai in (
                raw_vertex_models
            ):  # Iterate through original list of GAIA/Vertex base models
                display_model_id = ""
                if EXPERIMENTAL_MARKER in base_model_id_for_openai:
                    display_model_id = (
                        f"{base_model_id_for_openai}{OPENAI_DIRECT_SUFFIX}"
                    )
                else:
                    # OpenAI直接模式下也应该保持PAY前缀
                    display_model_id = (
                        f"{PAY_PREFIX}{base_model_id_for_openai}{OPENAI_DIRECT_SUFFIX}"
                    )

                # Check if already added (e.g. if remote config somehow already listed it or added as a base model)
                if display_model_id and not any(
                    m["id"] == display_model_id for m in dynamic_models_data
                ):
                    dynamic_models_data.append(
                        {
                            "id": display_model_id,
                            "object": "model",
                            "created": current_time,
                            "owned_by": "google",
                            "permission": [],
                            "root": base_model_id_for_openai,
                            "parent": None,
                        }
                    )

    return {
        "object": "list",
        "data": sorted(dynamic_models_data, key=lambda x: x["id"]),
    }
