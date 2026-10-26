"""Vertex 模式 /v1/chat/completions 端点（编排层）。

历史版本为 744 行的巨型端点函数（模型名解析 + 客户端选择 + OpenAI
Direct 全链路 + auto 模式 + Gemini 调用混作一团，另有 ~280 行 OpenAI
伪流式实现附在文件尾部）。现拆分为：

- model_names:        模型名前后缀解析与特殊变体校验（与 models_api 共享）
- routes/openai_direct: OpenAI Direct 路径全链路（客户端缓存/流生成器/编排）
- 本模块:             端点签名与三路分派（OpenAI Direct / auto / 普通 Gemini）、
                      Express 与 SA 凭证的客户端解析

对外契约（被测试与调用方锁定，不可改动）：
- chat_completions(fastapi_request, request, api_key) 三参签名
- chat_api._get_client_from_pool 可解析（tests/test_round3_regressions）
- chat_api.close_cached_openai_clients 可导入（app/main.py lifespan）
"""
import asyncio
import json
import random

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from google.genai import types
from google import genai

from app.config import settings
from app.utils.logging import vertex_log
from app.utils.thinking import apply_thinking_override_genai
from app.vertex.api_helpers import (
    create_generation_config,
    create_openai_error_response,
    execute_gemini_call,
)
from app.vertex.auth import get_api_key
from app.vertex.credentials_manager import get_credential_manager
from app.vertex.message_processing import (
    create_gemini_prompt,
    create_encrypted_gemini_prompt,
    create_encrypted_full_gemini_prompt,
)
from app.vertex.model_names import parse_model_request, validate_special_variants
from app.vertex.models import OpenAIRequest, OpenAIMessage
from app.utils.chat_requests import normalize_chat_messages
from app.vertex.routes.openai_direct import (
    close_cached_openai_clients,  # re-export: app/main.py 从本模块导入关闭钩子
    handle_openai_direct_request,
)
# Perf/anti-fingerprint fix: reuse the per-project genai client pool
# instead of re-creating a client (RSA parse + TLS renegotiation) on
# every SA-credential request.  The pool already exists in
# vertex_ai_init for exactly this purpose — the main chat path simply
# wasn't using it.
# （同时保留为模块级符号：test_round3 锁定 chat_api._get_client_from_pool）
from app.vertex.vertex_ai_init import _get_client_from_pool
from app.vertex import client_pool
from app.vertex.native_tools import NativeRequestError
import app.vertex.config as app_config

router = APIRouter()

# 外部契约符号（测试与调用方锁定，防止未来重构时被误删）：
# - close_cached_openai_clients: app/main.py lifespan 关闭阶段导入
# - _get_client_from_pool: tests/test_round3_regressions 的导入契约
__all__ = [
    "router",
    "chat_completions",
    "close_cached_openai_clients",
    "_get_client_from_pool",
    "get_credential_manager",
]

# 加密协议的 system_instruction 占位符（实际指令在 message_processing 中）
ENCRYPTION_INSTRUCTIONS_PLACEHOLDER = ["// Protocol Instructions Placeholder //"]


def _resolve_express_client(request_model: str, base_model_name: str):
    """Express 模式：随机打乱密钥顺序逐一尝试初始化 genai 客户端。

    Returns:
        (client, error_response): 二者其一为 None。
    """
    express_api_keys_list = app_config.resolve_express_api_keys()
    if not express_api_keys_list:
        error_msg = (
            f"模型 {request_model} 需要 Express 密钥，但当前未配置"
        )
        vertex_log("error", f"请求处理失败：{error_msg}")
        return None, JSONResponse(
            status_code=401,
            content=create_openai_error_response(401, error_msg, "authentication_error"),
        )

    vertex_log(
        "info",
        f"尝试 Vertex Express 模式：请求模型={request_model}，基础模型={base_model_name}",
    )
    indexed_keys = list(enumerate(express_api_keys_list))
    random.shuffle(indexed_keys)

    client_to_use = None
    for original_idx, key_val in indexed_keys:
        try:
            client_to_use = client_pool.get_client(("express", key_val), lambda: genai.Client(vertexai=True, api_key=key_val))
            vertex_log(
                "info",
                f"模型 {request_model} 使用 Vertex Express 模式，基础模型={base_model_name}，密钥序号={original_idx}",
            )
            break  # Successfully initialized client
        except Exception as e:
            vertex_log(
                "warning",
                f"Vertex Express 客户端初始化失败，密钥序号={original_idx}，模型={request_model}，错误={e}；尝试下一个密钥",
            )
            client_to_use = None  # Ensure client_to_use is None for this attempt

    if client_to_use is None:  # All configured Express keys failed
        error_msg = (
            f"模型 {request_model} 的全部 Express 密钥均初始化失败"
        )
        vertex_log("error", f"请求处理失败：{error_msg}")
        return None, JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, "Upstream service error.", "server_error"),
        )
    return client_to_use, None


def _resolve_sa_client(credential_manager_instance, request_model: str):
    """SA 凭证模式：轮换凭证并从客户端池取 client。

    Returns:
        (client, error_response): 二者其一为 None。
    """
    vertex_log(
        "info",
        f"模型 {request_model} 使用服务账号凭证，正在尝试认证",
    )
    rotated_credentials, rotated_project_id = (
        credential_manager_instance.get_random_credentials()
    )

    if rotated_credentials and rotated_project_id:
        # Perf/anti-fingerprint fix: previously constructed a new
        # genai.Client per request here, re-negotiating TLS (and
        # re-parsing the RSA key) every time — the exact pattern
        # the client pool in vertex_ai_init exists to prevent.
        client_to_use = _get_client_from_pool(
            rotated_project_id, rotated_credentials, location="global"
        )
        if client_to_use is None:
            error_msg = (
                f"Gemini 模型 {request_model} 的服务账号客户端初始化失败"
            )
            vertex_log("error", f"请求处理失败：{error_msg}")
            return None, JSONResponse(
                status_code=500,
                content=create_openai_error_response(500, "Upstream service error.", "server_error"),
            )
        vertex_log(
            "info",
            f"Gemini 模型 {request_model} 使用服务账号凭证，项目={rotated_project_id}",
        )
        return client_to_use, None

    # No SA credentials available for an SA model request
    error_msg = (
        f"Gemini 模型 {request_model} 需要服务账号凭证，但当前没有已加载的可用凭证"
    )
    vertex_log("error", f"请求处理失败：{error_msg}")
    return None, JSONResponse(
        status_code=401,
        content=create_openai_error_response(401, error_msg, "authentication_error"),
    )


async def _handle_auto_model(
    client_to_use,
    base_model_name: str,
    generation_config,
    request: OpenAIRequest,
):
    """auto 模式：base → encrypt → old_format 三连尝试，每次随机退避。"""
    vertex_log("info", f"正在处理自动路由模型：{request.model}")
    attempts = [
        {
            "name": "base",
            "model": base_model_name,
            "prompt_func": create_gemini_prompt,
            "config_modifier": lambda c: c,
        },
        {
            "name": "encrypt",
            "model": base_model_name,
            "prompt_func": create_encrypted_gemini_prompt,
            "config_modifier": lambda c: {
                **c,
                "system_instruction": ENCRYPTION_INSTRUCTIONS_PLACEHOLDER,
            },
        },
        {
            "name": "old_format",
            "model": base_model_name,
            "prompt_func": create_encrypted_full_gemini_prompt,
            "config_modifier": lambda c: c,
        },
    ]
    last_err = None
    for attempt in attempts:
        vertex_log(
            "info",
            f"自动路由尝试：{attempt['name']}，模型={attempt['model']}",
        )
        current_gen_config = attempt["config_modifier"](generation_config.copy())
        try:
            # Pass is_auto_attempt=True for auto-mode calls
            return await execute_gemini_call(
                client_to_use,
                attempt["model"],
                attempt["prompt_func"],
                current_gen_config,
                request,
                is_auto_attempt=True,
            )
        except Exception as e_auto:
            last_err = e_auto
            vertex_log(
                "info",
                f"自动路由尝试 {attempt['name']} 失败，模型={attempt['model']}，错误={e_auto}",
            )
            await asyncio.sleep(random.uniform(0.5, 2.0))

    vertex_log("info", f"自动路由的所有尝试均失败")
    # Hardening: previously returned `str(last_err)` directly to
    # the client.  Upstream error messages often contain
    # "Gemini API", "Vertex", "generativelanguage.googleapis.com",
    # internal gRPC status text etc. — directly exposing that
    # this is a proxy.  We now return a neutral upstream-error
    # message and only log the full error internally.
    err_msg = "Upstream service error. All auto-mode attempts failed."
    if last_err is not None:
        vertex_log(
            "warning",
            f"自动路由最后一次尝试失败：{type(last_err).__name__}: {last_err}",
        )
    if not request.stream and last_err:
        return JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, err_msg, "server_error"),
        )
    elif request.stream:
        # This is the final error handling for auto-mode if all attempts fail AND it was a streaming request
        async def final_auto_error_stream():
            err_content = create_openai_error_response(500, err_msg, "server_error")
            json_payload_final_auto_error = json.dumps(err_content)
            # Log the final error being sent to client after all auto-retries failed
            vertex_log(
                "debug",
                f"自动路由全部失败，正在返回最终错误响应",
            )
            yield f"data: {json_payload_final_auto_error}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            final_auto_error_stream(), media_type="text/event-stream"
        )
    return JSONResponse(
        status_code=500,
        content=create_openai_error_response(
            500,
            "All auto-mode attempts failed without specific error.",
            "server_error",
        ),
    )


def _apply_thinking_and_tools_overrides(
    flags, base_model_name: str, generation_config
):
    """普通 Gemini 路径：按请求标志位注入 search 工具 / 加密指令 / 思考预算。"""
    current_prompt_func = create_gemini_prompt

    if flags.is_grounded_search:
        search_tool = types.Tool(google_search=types.GoogleSearch())
        generation_config.setdefault("tools", []).append(search_tool)
    elif flags.is_encrypted_model:
        generation_config["system_instruction"] = ENCRYPTION_INSTRUCTIONS_PLACEHOLDER
        current_prompt_func = create_encrypted_gemini_prompt
    elif flags.is_encrypted_full_model:
        generation_config["system_instruction"] = ENCRYPTION_INSTRUCTIONS_PLACEHOLDER
        current_prompt_func = create_encrypted_full_gemini_prompt
    elif flags.is_nothinking_model:
        # 为 gemini-2.5-pro-preview-06-05 设置特定的 thinking_budget
        if base_model_name == "gemini-2.5-pro-preview-06-05":
            generation_config["thinking_config"] = {"thinking_budget": 128}
        else:
            generation_config["thinking_config"] = {"thinking_budget": 0}
    elif flags.is_max_thinking_model:
        # 为 gemini-2.5-pro-preview-06-05 设置特定的 thinking_budget
        if base_model_name == "gemini-2.5-pro-preview-06-05":
            generation_config["thinking_config"] = {"thinking_budget": 32768}
        else:
            generation_config["thinking_config"] = {"thinking_budget": 24576}

    # 最高思考强度全局覆盖（与 Gemini API 路径共享策略：
    # app/utils/thinking.py）。显式 -nothinking / -max 后缀是用户
    # 请求级的明确意图，优先级更高，不进行覆盖；其余模型统一
    # 应用「按模型指定等级 > 全局最高档」的覆盖策略。
    if not (flags.is_nothinking_model or flags.is_max_thinking_model):
        apply_thinking_override_genai(base_model_name, generation_config)

    return current_prompt_func


def _retain_stream_client(response, client):
    if not isinstance(response, StreamingResponse):
        return False
    response.body_iterator = client_pool.stream_with_release(response.body_iterator, client)
    return True


@router.post("/v1/chat/completions")
async def chat_completions(
    fastapi_request: Request,
    request: OpenAIRequest,
    api_key: str = Depends(get_api_key),
):
    retained_client = None
    transferred = False
    try:
        # Standalone Vertex requests also require nested content/tool validation.
        try:
            messages = normalize_chat_messages([m.model_dump(exclude_unset=True) for m in request.messages])
            request = request.model_copy(update={"messages": [OpenAIMessage.model_validate(m) for m in messages]})
        except ValueError as exc:
            raise NativeRequestError(str(exc)) from exc
        credential_manager_instance = get_credential_manager(fastapi_request)

        # 模型名解析：标志位 + 剥离前后缀后的基础模型名
        flags = parse_model_request(request.model)
        if flags.is_openai_direct_model and request.stream and getattr(settings, "FAKE_STREAMING", False) and request.n not in (None, 1):
            raise NativeRequestError("Vertex fake streaming currently supports n=1 only")
        if not flags.is_openai_direct_model:
            if request.n not in (None, 1):
                raise NativeRequestError("Native Vertex currently supports n=1 only")
            has_tools = bool(getattr(request, "tools", None)) or any(
                getattr(message, "tool_calls", None) or message.role == "tool"
                for message in request.messages
            )
            if has_tools and (flags.is_encrypted_model or flags.is_encrypted_full_model or flags.is_auto_model):
                raise NativeRequestError("Tool calling requires a plain native Vertex model or the -openai route")

        # -nothinking / -max 后缀的模型白名单校验
        variant_error = validate_special_variants(request.model, flags)
        if variant_error:
            return JSONResponse(
                status_code=400,
                content=create_openai_error_response(
                    400, variant_error, "invalid_request_error"
                ),
            )

        generation_config = {} if flags.is_openai_direct_model else create_generation_config(request)

        # --- 客户端选择（OpenAI Direct 自带客户端，走独立分支） ---
        client_to_use = None
        if not flags.is_openai_direct_model:
            if flags.is_express_model_request:
                client_to_use, error_response = _resolve_express_client(
                    request.model, flags.base_model_name
                )
            else:
                client_to_use, error_response = _resolve_sa_client(
                    credential_manager_instance, request.model
                )
            if client_to_use is None:
                # 特殊情况：Express/SA 之外的路径本不会到这里，兜底防御
                return error_response

        # --- 三路分派 ---
        if flags.is_openai_direct_model:
            return await handle_openai_direct_request(
                request, credential_manager_instance, flags.base_model_name
            )

        client_pool.retain(client_to_use)
        retained_client = client_to_use
        if flags.is_auto_model:
            result = await _handle_auto_model(
                client_to_use, flags.base_model_name, generation_config, request
            )
            transferred = _retain_stream_client(result, client_to_use)
            return result

        current_prompt_func = _apply_thinking_and_tools_overrides(
            flags, flags.base_model_name, generation_config
        )
        # For non-auto models, the 'base_model_name' might have suffix stripped.
        # We should use the original 'request.model' for API call if it's a suffixed one,
        # or 'base_model_name' if it's truly a base model without suffixes.
        # The current logic uses 'base_model_name' for the API call in the 'else' block.
        # This means if `request.model` was "gemini-1.5-pro-search", `base_model_name` becomes "gemini-1.5-pro"
        # but the API call might need the full "gemini-1.5-pro-search".
        # Let's use `request.model` for the API call here, and `base_model_name` for checks like Express eligibility.
        # For non-auto mode, is_auto_attempt defaults to False in execute_gemini_call
        result = await execute_gemini_call(
            client_to_use,
            flags.base_model_name,
            current_prompt_func,
            generation_config,
            request,
        )
        transferred = _retain_stream_client(result, client_to_use)
        return result

    except NativeRequestError as e:
        return JSONResponse(status_code=400, content=create_openai_error_response(
            400, str(e), "invalid_request_error"
        ))
    except Exception as e:
        error_msg = f"聊天接口发生未预期错误：{str(e)}"
        vertex_log("error", f"请求处理失败：{error_msg}")
        return JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, "Upstream service error.", "server_error"),
        )

    finally:
        if retained_client is not None and not transferred:
            await client_pool.release(retained_client)
