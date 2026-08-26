import asyncio
import json  # Needed for error streaming
import random
import time
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Dict, Any

from app.utils.logging import vertex_log
from app.config import settings

# Google and OpenAI specific imports
from google.genai import types
from google import genai
import openai
from app.vertex.credentials_manager import _refresh_auth, CredentialManager

# Local module imports
from app.vertex.models import OpenAIRequest
from app.vertex.auth import get_api_key
import app.vertex.config as app_config
from app.vertex.message_processing import (
    create_gemini_prompt,
    create_encrypted_gemini_prompt,
    create_encrypted_full_gemini_prompt,
)
from app.vertex.api_helpers import (
    create_generation_config,
    create_openai_error_response,
    execute_gemini_call,
)
from app.utils.stealth import gen_openai_chunk_id  # full_jitter_backoff 未使用，已删除

router = APIRouter()


# ---------------------------------------------------------------------------
# OpenAI Direct 路径的 AsyncOpenAI 客户端缓存
# ---------------------------------------------------------------------------
# Perf/resource fix: 旧实现每个请求都 new 一个 `openai.AsyncOpenAI`
# 且从不 close —— 每个请求都完整 TCP+TLS 握手（+100~300ms 延迟），
# 且未关闭的客户端持有连接/socket，长期运行会 FD 泄漏（Too many open
# files）。
#
# GCP OAuth token 约一小时才轮换一次，同一 token 期间的所有请求完全
# 可以复用同一个客户端（AsyncOpenAI 内部的 httpx 连接池本身就是为
# 并发共享设计的）。因此按 (endpoint, token, stream 模式) 键控缓存，
# 上限 _MAX_CACHED_OPENAI_CLIENTS，淘汰时后台关闭旧客户端释放连接。
_openai_client_cache: Dict[tuple, openai.AsyncOpenAI] = {}
_openai_client_cache_lock = asyncio.Lock()
_MAX_CACHED_OPENAI_CLIENTS = 32


async def _close_client_quietly(client: openai.AsyncOpenAI) -> None:
    try:
        await client.close()
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        vertex_log("warning", f"Failed to close evicted OpenAI client: {e}")


async def _get_cached_openai_client(
    base_url: str,
    gcp_token: str,
    stealth_headers: Dict[str, str],
) -> openai.AsyncOpenAI:
    """按 (endpoint, token, headers) 复用 AsyncOpenAI 客户端。"""
    key = (base_url, gcp_token, tuple(sorted(stealth_headers.items())))
    async with _openai_client_cache_lock:
        client = _openai_client_cache.get(key)
        if client is not None:
            return client

        client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=gcp_token,
            default_headers=stealth_headers,
            timeout=600.0,
            max_retries=0,  # we handle retries ourselves with jitter
        )
        # 插入末尾保持插入序，淘汰时从头部取最旧的
        _openai_client_cache[key] = client

        # 超出容量时淘汰最旧的（>=1 小时未轮换的 token，几乎不可能仍在
        # 被在途请求使用；关闭放到后台任务，不阻塞请求路径）
        while len(_openai_client_cache) > _MAX_CACHED_OPENAI_CLIENTS:
            _old_key = next(iter(_openai_client_cache))
            _old_client = _openai_client_cache.pop(_old_key)
            asyncio.create_task(_close_client_quietly(_old_client))
        return client


async def close_cached_openai_clients() -> None:
    """进程关闭时释放全部缓存客户端（由 main lifespan 调用）。"""
    async with _openai_client_cache_lock:
        clients = list(_openai_client_cache.values())
        _openai_client_cache.clear()
    for client in clients:
        await _close_client_quietly(client)


@router.post("/v1/chat/completions")
async def chat_completions(
    fastapi_request: Request,
    request: OpenAIRequest,
    api_key: str = Depends(get_api_key),
):
    try:
        # 获取credential_manager，如果不存在则创建一个新的
        try:
            credential_manager_instance = fastapi_request.app.state.credential_manager
            vertex_log("info", "Using existing credential manager from app state")
        except AttributeError:
            # 如果app.state中没有credential_manager，则创建一个新的
            vertex_log(
                "warning",
                "No credential_manager found in app.state, creating a new one",
            )
            credential_manager_instance = CredentialManager()

        OPENAI_DIRECT_SUFFIX = "-openai"
        EXPERIMENTAL_MARKER = "-exp-"
        PAY_PREFIX = "[PAY]"
        EXPRESS_PREFIX = "[EXPRESS] "  # Note the space for easier stripping

        # Model validation based on a predefined list has been removed as per user request.
        # The application will now attempt to use any provided model string.
        # We still need to fetch vertex_express_model_ids for the Express Mode logic.
        # vertex_express_model_ids = await get_vertex_express_models() # We'll use the prefix now

        # Updated logic for is_openai_direct_model
        is_openai_direct_model = False
        if request.model.endswith(OPENAI_DIRECT_SUFFIX):
            temp_name_for_marker_check = request.model[: -len(OPENAI_DIRECT_SUFFIX)]
            if temp_name_for_marker_check.startswith(PAY_PREFIX):
                is_openai_direct_model = True
            elif EXPERIMENTAL_MARKER in temp_name_for_marker_check:
                is_openai_direct_model = True
        is_auto_model = request.model.endswith("-auto")
        is_grounded_search = request.model.endswith("-search")
        is_encrypted_model = request.model.endswith("-encrypt")
        is_encrypted_full_model = request.model.endswith("-encrypt-full")
        is_nothinking_model = request.model.endswith("-nothinking")
        is_max_thinking_model = request.model.endswith("-max")
        base_model_name = request.model  # Start with the full model name

        # Determine base_model_name by stripping known prefixes and suffixes
        # Order of stripping: Prefixes first, then suffixes.

        is_express_model_request = False
        if base_model_name.startswith(EXPRESS_PREFIX):
            is_express_model_request = True
            base_model_name = base_model_name[len(EXPRESS_PREFIX) :]

        if base_model_name.startswith(PAY_PREFIX):
            base_model_name = base_model_name[len(PAY_PREFIX) :]

        # Suffix stripping (applied to the name after prefix removal)
        # This order matters if a model could have multiple (e.g. -encrypt-auto, though not currently a pattern)
        if (
            is_openai_direct_model
        ):  # This check is based on request.model, so it's fine here
            # If it was an OpenAI direct model, its base name is request.model minus suffix.
            # We need to ensure PAY_PREFIX or EXPRESS_PREFIX are also stripped if they were part of the original.
            temp_base_for_openai = request.model[: -len(OPENAI_DIRECT_SUFFIX)]
            if temp_base_for_openai.startswith(EXPRESS_PREFIX):
                temp_base_for_openai = temp_base_for_openai[len(EXPRESS_PREFIX) :]
            if temp_base_for_openai.startswith(PAY_PREFIX):
                temp_base_for_openai = temp_base_for_openai[len(PAY_PREFIX) :]
            base_model_name = temp_base_for_openai  # Assign the fully stripped name
        elif is_auto_model:
            base_model_name = base_model_name[: -len("-auto")]
        elif is_grounded_search:
            base_model_name = base_model_name[: -len("-search")]
        elif is_encrypted_full_model:
            base_model_name = base_model_name[
                : -len("-encrypt-full")
            ]  # Must be before -encrypt
        elif is_encrypted_model:
            base_model_name = base_model_name[: -len("-encrypt")]
        elif is_nothinking_model:
            base_model_name = base_model_name[: -len("-nothinking")]
        elif is_max_thinking_model:
            base_model_name = base_model_name[: -len("-max")]

        # Define supported models for these specific variants
        supported_flash_variants = [
            "gemini-2.5-flash-preview-04-17",
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-pro-preview-06-05",
        ]
        supported_flash_variants_str = "' or '".join(supported_flash_variants)

        # Specific model variant checks (if any remain exclusive and not covered dynamically)
        if is_nothinking_model and base_model_name not in supported_flash_variants:
            return JSONResponse(
                status_code=400,
                content=create_openai_error_response(
                    400,
                    f"Model '{request.model}' (-nothinking) is only supported for '{supported_flash_variants_str}'.",
                    "invalid_request_error",
                ),
            )
        if is_max_thinking_model and base_model_name not in supported_flash_variants:
            return JSONResponse(
                status_code=400,
                content=create_openai_error_response(
                    400,
                    f"Model '{request.model}' (-max) is only supported for '{supported_flash_variants_str}'.",
                    "invalid_request_error",
                ),
            )

        generation_config = create_generation_config(request)

        client_to_use = None

        # 优先从settings获取配置，如果没有则使用app_config中的配置
        express_api_keys_list = []
        if (
            hasattr(settings, "VERTEX_EXPRESS_API_KEY")
            and settings.VERTEX_EXPRESS_API_KEY
        ):
            express_api_keys_list = [
                key.strip()
                for key in settings.VERTEX_EXPRESS_API_KEY.split(",")
                if key.strip()
            ]
            vertex_log(
                "info",
                f"Using {len(express_api_keys_list)} Express API keys from settings",
            )
        # 如果settings中没有配置，则使用app_config中的配置
        if not express_api_keys_list and app_config.VERTEX_EXPRESS_API_KEY_VAL:
            express_api_keys_list = app_config.VERTEX_EXPRESS_API_KEY_VAL
            vertex_log(
                "info",
                f"Using {len(express_api_keys_list)} Express API keys from app_config",
            )

        # This client initialization logic is for Gemini models.
        # OpenAI Direct models have their own client setup and will return before this.
        if is_openai_direct_model:
            # OpenAI Direct logic is self-contained and will return.
            # If it doesn't return, it means we proceed to Gemini logic, which shouldn't happen
            # if is_openai_direct_model is true. The main if/elif/else for model types handles this.
            pass
        elif is_express_model_request:
            if not express_api_keys_list:
                error_msg = f"Model '{request.model}' is an Express model and requires an Express API key, but none are configured."
                vertex_log("error", error_msg)
                return JSONResponse(
                    status_code=401,
                    content=create_openai_error_response(
                        401, error_msg, "authentication_error"
                    ),
                )

            vertex_log(
                "info",
                f"INFO: Attempting Vertex Express Mode for model request: {request.model} (base: {base_model_name})",
            )
            indexed_keys = list(enumerate(express_api_keys_list))
            random.shuffle(indexed_keys)

            for original_idx, key_val in indexed_keys:
                try:
                    client_to_use = genai.Client(vertexai=True, api_key=key_val)
                    vertex_log(
                        "info",
                        f"INFO: Using Vertex Express Mode for model {request.model} (base: {base_model_name}) with API key (original index: {original_idx}).",
                    )
                    break  # Successfully initialized client
                except Exception as e:
                    vertex_log(
                        "warning",
                        f"WARNING: Vertex Express Mode client init failed for API key (original index: {original_idx}) for model {request.model}: {e}. Trying next key.",
                    )
                    client_to_use = (
                        None  # Ensure client_to_use is None for this attempt
                    )

            if client_to_use is None:  # All configured Express keys failed
                error_msg = f"All configured Express API keys failed to initialize for model '{request.model}'."
                vertex_log("error", error_msg)
                return JSONResponse(
                    status_code=500,
                    content=create_openai_error_response(
                        500, error_msg, "server_error"
                    ),
                )

        else:  # Not an Express model request, therefore an SA credential model request for Gemini
            vertex_log(
                "info",
                f"INFO: Model '{request.model}' is an SA credential request for Gemini. Attempting SA credentials.",
            )
            rotated_credentials, rotated_project_id = (
                credential_manager_instance.get_random_credentials()
            )

            if rotated_credentials and rotated_project_id:
                try:
                    client_to_use = genai.Client(
                        vertexai=True,
                        credentials=rotated_credentials,
                        project=rotated_project_id,
                        location="global",
                    )
                    vertex_log(
                        "info",
                        f"INFO: Using SA credential for Gemini model {request.model} (project: {rotated_project_id})",
                    )
                except Exception as e:
                    client_to_use = None  # Ensure it's None on failure
                    error_msg = f"SA credential client initialization failed for Gemini model '{request.model}': {e}."
                    vertex_log("error", error_msg)
                    return JSONResponse(
                        status_code=500,
                        content=create_openai_error_response(
                            500, error_msg, "server_error"
                        ),
                    )
            else:  # No SA credentials available for an SA model request
                error_msg = f"Model '{request.model}' requires SA credentials for Gemini, but none are available or loaded."
                vertex_log("error", error_msg)
                return JSONResponse(
                    status_code=401,
                    content=create_openai_error_response(
                        401, error_msg, "authentication_error"
                    ),
                )

        # If we reach here and client_to_use is still None, it means it's an OpenAI Direct Model,
        # which handles its own client and responses.
        # For Gemini models (Express or SA), client_to_use must be set, or an error returned above.
        if not is_openai_direct_model and client_to_use is None:
            # This case should ideally not be reached if the logic above is correct,
            # as each path (Express/SA for Gemini) should either set client_to_use or return an error.
            # This is a safeguard.
            vertex_log(
                "critical",
                f"CRITICAL ERROR: Client for Gemini model '{request.model}' was not initialized, and no specific error was returned. This indicates a logic flaw.",
            )
            return JSONResponse(
                status_code=500,
                content=create_openai_error_response(
                    500,
                    "Critical internal server error: Gemini client not initialized.",
                    "server_error",
                ),
            )

        encryption_instructions_placeholder = [
            "// Protocol Instructions Placeholder //"
        ]  # Actual instructions are in message_processing
        if is_openai_direct_model:
            vertex_log(
                "info", f"INFO: Using OpenAI Direct Path for model: {request.model}"
            )
            # This mode exclusively uses rotated credentials, not express keys.
            rotated_credentials, rotated_project_id = (
                credential_manager_instance.get_random_credentials()
            )

            if not rotated_credentials or not rotated_project_id:
                error_msg = "OpenAI Direct Mode requires GCP credentials, but none were available or loaded successfully."
                vertex_log("error", error_msg)
                return JSONResponse(
                    status_code=500,
                    content=create_openai_error_response(
                        500, error_msg, "server_error"
                    ),
                )

            vertex_log(
                "info",
                f"INFO: [OpenAI Direct Path] Using credentials for project: {rotated_project_id}",
            )
            gcp_token = _refresh_auth(rotated_credentials)

            if not gcp_token:
                error_msg = f"Failed to obtain valid GCP token for OpenAI client (Source: Credential Manager, Project: {rotated_project_id})."
                vertex_log("error", error_msg)
                return JSONResponse(
                    status_code=500,
                    content=create_openai_error_response(
                        500, error_msg, "server_error"
                    ),
                )

            PROJECT_ID = rotated_project_id
            LOCATION = "global"  # Fixed as per user confirmation
            VERTEX_AI_OPENAI_ENDPOINT_URL = (
                f"https://aiplatform.googleapis.com/v1beta1/"
                f"projects/{PROJECT_ID}/locations/{LOCATION}/endpoints/openapi"
            )
            # base_model_name is already extracted (e.g., "gemini-1.5-pro-exp-v1")
            UNDERLYING_MODEL_ID = f"google/{base_model_name}"

            # Hardening: previously constructed a bare `openai.AsyncOpenAI`
            # client which (a) leaks the SDK's default UA
            # `AsyncOpenAI/Python/x.x.x` plus a full set of
            # `X-Stainless-*` introspection headers (Lang/Package-Version/
            # OS/Arch/Runtime/Async) on every request — a very strong
            # "this is a python OpenAI-SDK client" fingerprint — and
            # (b) constructed a new client per request, bypassing the
            # shared httpx connection pool.  We now inject stealth UA +
            # Content-Type/Accept headers via `default_headers` and strip
            # the X-Stainless family by setting them to empty strings
            # (the SDK explicitly allows overriding/blanking them).
            #
            # Perf fix: the client is now served from a bounded cache
            # keyed by (endpoint, token, headers) — see
            # _get_cached_openai_client above — so connection pools are
            # reused across requests sharing the same GCP token instead
            # of being rebuilt (and leaked) per request.
            from app.utils.stealth import pick_user_agent

            stealth_headers = {
                "User-Agent": pick_user_agent(gcp_token),
                "Accept": "text/event-stream" if request.stream else "application/json",
                "Accept-Encoding": "gzip, deflate",
                "Accept-Language": "en-US,en;q=0.9",
                # Blank the X-Stainless-* introspection headers that the
                # OpenAI Python SDK sets by default.  An empty string
                # tells the SDK "don't send this header".
                "X-Stainless-Lang": "",
                "X-Stainless-Package-Version": "",
                "X-Stainless-OS": "",
                "X-Stainless-Arch": "",
                "X-Stainless-Runtime": "",
                "X-Stainless-Runtime-Version": "",
                "X-Stainless-Async": "",
                "X-Stainless-Retry-Count": "",
            }
            openai_client = await _get_cached_openai_client(
                VERTEX_AI_OPENAI_ENDPOINT_URL,
                gcp_token,
                stealth_headers,
            )

            # Hardening: previously hardcoded all 5 safety categories
            # to "OFF" on every OpenAI Direct path request — the same
            # "all-OFF safetySettings" fingerprint already fixed on
            # the Gemini path.  We now respect SAFETY_MODE (default =
            # don't send safety_settings at all).  See
            # app/config/safety.py:get_safety_settings.
            from app.config.safety import get_safety_settings

            openai_safety_settings = get_safety_settings()

            openai_params = {
                "model": UNDERLYING_MODEL_ID,
                "messages": [
                    msg.model_dump(exclude_unset=True) for msg in request.messages
                ],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "top_p": request.top_p,
                "stream": request.stream,
                "stop": request.stop,
                "seed": request.seed,
                "n": request.n,
            }
            openai_params = {k: v for k, v in openai_params.items() if v is not None}

            # Only attach safety_settings when SAFETY_MODE != "default"
            # (None means "don't send the field at all").  Sending an
            # all-OFF block on every request is a fingerprint.
            if openai_safety_settings:
                openai_extra_body = {"google": {"safety_settings": openai_safety_settings}}
            else:
                openai_extra_body = None

            if request.stream:
                # 每次调用时直接从settings获取最新的FAKE_STREAMING值
                fake_streaming_enabled = False
                if hasattr(settings, "FAKE_STREAMING"):
                    fake_streaming_enabled = settings.FAKE_STREAMING
                else:
                    fake_streaming_enabled = app_config.FAKE_STREAMING_ENABLED

                vertex_log(
                    "info",
                    f"DEBUG: FAKE_STREAMING setting is {fake_streaming_enabled} for OpenAI model {request.model}",
                )

                if fake_streaming_enabled:
                    vertex_log(
                        "info",
                        f"INFO: OpenAI Fake Streaming (SSE Simulation) ENABLED for model '{request.model}'.",
                    )
                    # openai_params already has "stream": True from initial setup,
                    # but openai_fake_stream_generator will make a stream=False call internally.
                    # Call the now async generator
                    return StreamingResponse(
                        openai_fake_stream_generator(
                            openai_client=openai_client,
                            openai_params=openai_params,
                            openai_extra_body=openai_extra_body,
                            request_obj=request,
                            is_auto_attempt=False,
                            # --- New parameters for tokenizer and reasoning split ---
                            gcp_credentials=rotated_credentials,
                            gcp_project_id=PROJECT_ID,  # This is rotated_project_id
                            gcp_location=LOCATION,  # This is "global"
                            base_model_id_for_tokenizer=base_model_name,  # Stripped model ID for tokenizer
                        ),
                        media_type="text/event-stream",
                    )
                else:  # Regular OpenAI streaming
                    vertex_log(
                        "info",
                        f"INFO: OpenAI True Streaming ENABLED for model '{request.model}'.",
                    )

                    async def openai_true_stream_generator():  # Renamed to avoid conflict
                        try:
                            # Ensure stream=True is explicitly passed for real streaming
                            openai_params_for_true_stream = {
                                **openai_params,
                                "stream": True,
                            }
                            stream_response = (
                                await openai_client.chat.completions.create(
                                    **openai_params_for_true_stream,
                                    extra_body=openai_extra_body,
                                )
                            )
                            async for chunk in stream_response:
                                try:
                                    chunk_as_dict = chunk.model_dump(
                                        exclude_unset=True, exclude_none=True
                                    )

                                    choices = chunk_as_dict.get("choices")
                                    if (
                                        choices
                                        and isinstance(choices, list)
                                        and len(choices) > 0
                                    ):
                                        delta = choices[0].get("delta")
                                        if delta and isinstance(delta, dict):
                                            extra_content = delta.get("extra_content")
                                            if isinstance(extra_content, dict):
                                                google_content = extra_content.get(
                                                    "google"
                                                )
                                                if isinstance(google_content, dict):
                                                    # When the upstream
                                                    # chunk carried a
                                                    # `thought: true`
                                                    # marker, treat its
                                                    # `content` as
                                                    # reasoning and
                                                    # strip the
                                                    # `extra_content`
                                                    # envelope (real
                                                    # OpenAI chunks
                                                    # never carry
                                                    # `extra_content`).
                                                    if google_content.get("thought") is True:
                                                        reasoning_text = delta.get(
                                                            "content"
                                                        )
                                                        if reasoning_text is not None:
                                                            delta["reasoning_content"] = (
                                                                reasoning_text
                                                            )
                                                        if "content" in delta:
                                                            del delta["content"]
                                                        if "extra_content" in delta:
                                                            del delta["extra_content"]
                                                    else:
                                                        # Even when no
                                                        # `thought` flag
                                                        # is set, real
                                                        # OpenAI chunks
                                                        # never carry
                                                        # `extra_content.google.*`
                                                        # — strip it
                                                        # from the
                                                        # client-visible
                                                        # chunk so the
                                                        # downstream
                                                        # client can't
                                                        # detect this
                                                        # is a Vertex
                                                        # passthrough.
                                                        if "extra_content" in delta:
                                                            del delta["extra_content"]

                                    # vertex_log('debug', f"DEBUG OpenAI Stream Chunk: {chunk_as_dict}") # Potential verbose log
                                    yield f"data: {json.dumps(chunk_as_dict)}\n\n"

                                except Exception as chunk_processing_error:
                                    error_msg_chunk = f"Error processing/serializing OpenAI chunk for {request.model}: {str(chunk_processing_error)}. Chunk: {str(chunk)[:200]}"
                                    vertex_log("error", error_msg_chunk)
                                    if len(error_msg_chunk) > 1024:
                                        error_msg_chunk = error_msg_chunk[:1024] + "..."
                                    error_response_chunk = create_openai_error_response(
                                        500, error_msg_chunk, "server_error"
                                    )
                                    json_payload_for_chunk_error = json.dumps(
                                        error_response_chunk
                                    )
                                    yield f"data: {json_payload_for_chunk_error}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return
                            yield "data: [DONE]\n\n"
                        except Exception as stream_error:
                            original_error_message = str(stream_error)
                            if len(original_error_message) > 1024:
                                original_error_message = (
                                    original_error_message[:1024] + "..."
                                )
                            error_msg_stream = f"Error during OpenAI client true streaming for {request.model}: {original_error_message}"
                            vertex_log("error", error_msg_stream)
                            error_response_content = create_openai_error_response(
                                500, error_msg_stream, "server_error"
                            )
                            json_payload_for_stream_error = json.dumps(
                                error_response_content
                            )
                            yield f"data: {json_payload_for_stream_error}\n\n"
                            yield "data: [DONE]\n\n"

                    return StreamingResponse(
                        openai_true_stream_generator(), media_type="text/event-stream"
                    )
            else:  # Not streaming (is_openai_direct_model and not request.stream)
                try:
                    # Ensure stream=False is explicitly passed for non-streaming
                    openai_params_for_non_stream = {**openai_params, "stream": False}
                    response = await openai_client.chat.completions.create(
                        **openai_params_for_non_stream,
                        # Removed redundant **openai_params spread
                        extra_body=openai_extra_body,
                    )
                    response_dict = response.model_dump(
                        exclude_unset=True, exclude_none=True
                    )

                    try:
                        # Extract reasoning directly from the response
                        choices = response_dict.get("choices")
                        if choices and isinstance(choices, list) and len(choices) > 0:
                            message_dict = choices[0].get("message")
                            if message_dict and isinstance(message_dict, dict):
                                # Always remove extra_content from the message if it exists
                                if "extra_content" in message_dict:
                                    extra_content = message_dict.get(
                                        "extra_content", {}
                                    )
                                    google_content = extra_content.get("google", {})

                                    # If this is a thought, move content to reasoning_content
                                    if (
                                        google_content
                                        and google_content.get("thought") is True
                                    ):
                                        message_dict["reasoning_content"] = (
                                            message_dict.get("content", "")
                                        )
                                        message_dict["content"] = ""

                                    # Always remove extra_content
                                    del message_dict["extra_content"]
                                    vertex_log(
                                        "debug",
                                        "DEBUG: Processed 'extra_content' from response message.",
                                    )

                    except Exception as e_reasoning_processing:
                        vertex_log(
                            "warning",
                            f"WARNING: Error during non-streaming reasoning processing for model {request.model} due to: {e_reasoning_processing}.",
                        )

                    return JSONResponse(content=response_dict)
                except Exception as generate_error:
                    error_msg_generate = f"Error calling OpenAI client for {request.model}: {str(generate_error)}"
                    vertex_log("error", error_msg_generate)
                    error_response = create_openai_error_response(
                        500, error_msg_generate, "server_error"
                    )
                    return JSONResponse(status_code=500, content=error_response)
        elif is_auto_model:
            vertex_log("info", f"Processing auto model: {request.model}")
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
                        "system_instruction": encryption_instructions_placeholder,
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
                    f"Auto-mode attempting: '{attempt['name']}' for model {attempt['model']}",
                )
                current_gen_config = attempt["config_modifier"](
                    generation_config.copy()
                )
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
                        f"Auto-attempt '{attempt['name']}' for model {attempt['model']} failed: {e_auto}",
                    )
                    await asyncio.sleep(random.uniform(0.5, 2.0))

            vertex_log("info", "All auto attempts failed.")
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
                    f"Last auto-attempt error (internal only): {type(last_err).__name__}: {last_err}",
                )
            if not request.stream and last_err:
                return JSONResponse(
                    status_code=500,
                    content=create_openai_error_response(500, err_msg, "server_error"),
                )
            elif request.stream:
                # This is the final error handling for auto-mode if all attempts fail AND it was a streaming request
                async def final_auto_error_stream():
                    err_content = create_openai_error_response(
                        500, err_msg, "server_error"
                    )
                    json_payload_final_auto_error = json.dumps(err_content)
                    # Log the final error being sent to client after all auto-retries failed
                    vertex_log(
                        "debug",
                        f"DEBUG: Auto-mode all attempts failed. Yielding final error JSON: {json_payload_final_auto_error}",
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

        else:  # Not an auto model
            current_prompt_func = create_gemini_prompt
            # Determine the actual model string to call the API with (e.g., "gemini-1.5-pro-search")
            api_model_string = request.model

            if is_grounded_search:
                search_tool = types.Tool(google_search=types.GoogleSearch())
                generation_config["tools"] = [search_tool]
            elif is_encrypted_model:
                generation_config["system_instruction"] = (
                    encryption_instructions_placeholder
                )
                current_prompt_func = create_encrypted_gemini_prompt
            elif is_encrypted_full_model:
                generation_config["system_instruction"] = (
                    encryption_instructions_placeholder
                )
                current_prompt_func = create_encrypted_full_gemini_prompt
            elif is_nothinking_model:
                # 为gemini-2.5-pro-preview-06-05设置特定的thinking_budget
                if base_model_name == "gemini-2.5-pro-preview-06-05":
                    generation_config["thinking_config"] = {"thinking_budget": 128}
                else:
                    generation_config["thinking_config"] = {"thinking_budget": 0}
            elif is_max_thinking_model:
                # 为gemini-2.5-pro-preview-06-05设置特定的thinking_budget
                if base_model_name == "gemini-2.5-pro-preview-06-05":
                    generation_config["thinking_config"] = {"thinking_budget": 32768}
                else:
                    generation_config["thinking_config"] = {"thinking_budget": 24576}

            # For non-auto models, the 'base_model_name' might have suffix stripped.
            # We should use the original 'request.model' for API call if it's a suffixed one,
            # or 'base_model_name' if it's truly a base model without suffixes.
            # The current logic uses 'base_model_name' for the API call in the 'else' block.
            # This means if `request.model` was "gemini-1.5-pro-search", `base_model_name` becomes "gemini-1.5-pro"
            # but the API call might need the full "gemini-1.5-pro-search".
            # Let's use `request.model` for the API call here, and `base_model_name` for checks like Express eligibility.
            # For non-auto mode, is_auto_attempt defaults to False in execute_gemini_call
            return await execute_gemini_call(
                client_to_use,
                base_model_name,
                current_prompt_func,
                generation_config,
                request,
            )

    except Exception as e:
        error_msg = f"Unexpected error in chat_completions endpoint: {str(e)}"
        vertex_log("error", error_msg)
        return JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, error_msg, "server_error"),
        )


async def _base_fake_stream_engine(
    api_call_task_creator,
    extract_text_from_response_func,
    is_valid_response_func,
    response_id,
    sse_model_name,
    keep_alive_interval_seconds=0,
    is_auto_attempt=False,
    reasoning_text_to_yield="",
    actual_content_text_to_yield="",
):
    """Base engine for fake streaming that handles common logic for both Gemini and OpenAI."""
    try:
        # Wait for the API call to complete
        api_response = await api_call_task_creator()

        # Validate the response
        if not is_valid_response_func(api_response):
            error_msg = (
                f"Invalid response structure from API for model {sse_model_name}"
            )
            vertex_log("error", error_msg)
            err_resp = create_openai_error_response(500, error_msg, "server_error")
            yield f"data: {json.dumps(err_resp)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # OpenAI streams share a single `created` Unix timestamp across
        # all chunks of the same response.  Per-chunk `int(time.time())`
        # previously produced chunks that all had a slightly different
        # timestamp within the same second — which is detectable when
        # streamed over >1s as "all chunks within one second all share
        # the same `created`" is the official behaviour.
        created_ts = int(time.time())

        # Get the full text from the response
        full_text = ""
        if reasoning_text_to_yield or actual_content_text_to_yield:
            # If we already have separated reasoning and content, use them
            if reasoning_text_to_yield:
                # First yield the reasoning content in a separate chunk
                reasoning_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": sse_model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": reasoning_text_to_yield},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(reasoning_chunk)}\n\n"

            # Then use the actual content for streaming
            full_text = actual_content_text_to_yield
        else:
            # Otherwise extract the full text from the response
            full_text = extract_text_from_response_func(api_response)

        if not full_text:
            # If there's no text to stream, send an empty delta (no
            # content field at all) and finish.  Hardening: previously
            # emitted `delta: {"content": ""}` — real OpenAI never
            # emits empty-content deltas, so the empty string was a
            # fingerprint.
            empty_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": sse_model_name,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }
            yield f"data: {json.dumps(empty_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Simulate streaming by yielding chunks of the full text.
        # Hardening: previously used a fixed chunk_size + fixed delay
        # — a 10-bucket/50ms pattern in the SSE byte-stream is
        # trivially detectable as a fake-stream proxy.  We now use a
        # randomised chunk size (24-96 chars) and randomised
        # inter-chunk delay (20-150ms) so the distribution looks like
        # real token-bound streaming.
        chunk_size = max(20, random.randint(24, 96))
        # Initial chunk with role
        initial_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": sse_model_name,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(initial_chunk)}\n\n"

        # Stream the content in chunks
        for i in range(0, len(full_text), chunk_size):
            chunk_text = full_text[i : i + chunk_size]
            content_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": sse_model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk_text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(content_chunk)}\n\n"

            if i + chunk_size < len(full_text):
                await asyncio.sleep(random.uniform(0.02, 0.15))

        # Final chunk to indicate completion
        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": sse_model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        # Hardening: previously embedded str(e) directly into the
        # SSE error message sent to the client.  Now logs internally
        # and emits a neutral message.
        vertex_log(
            "error",
            f"_base_fake_stream_engine error (internal only, model: {sse_model_name}): {type(e).__name__}: {e}",
        )
        if not is_auto_attempt:  # Only yield error for non-auto attempts
            err_resp = create_openai_error_response(500, "Upstream service error during streaming.", "server_error")
            yield f"data: {json.dumps(err_resp)}\n\n"
            yield "data: [DONE]\n\n"


async def openai_fake_stream_generator(
    openai_client: openai.AsyncOpenAI,
    openai_params: Dict[str, Any],
    openai_extra_body: Dict[str, Any],
    request_obj: OpenAIRequest,
    is_auto_attempt: bool,
    gcp_credentials: Any,
    gcp_project_id: str,
    gcp_location: str,
    base_model_id_for_tokenizer: str,
):
    api_model_name = openai_params.get("model", "unknown-openai-model")
    vertex_log(
        "info",
        f"FAKE STREAMING (OpenAI): Prep for '{request_obj.model}' (API model: '{api_model_name}')",
    )
    response_id = gen_openai_chunk_id()

    async def _openai_api_call_wrapper():
        params_for_non_stream_call = openai_params.copy()
        params_for_non_stream_call["stream"] = False

        _api_call_task = asyncio.create_task(
            openai_client.chat.completions.create(
                **params_for_non_stream_call, extra_body=openai_extra_body
            )
        )
        raw_response = await _api_call_task

        # Extract reasoning and content directly from the response
        full_content_from_api = ""
        reasoning_text = ""

        if raw_response.choices and raw_response.choices[0].message:
            # Check for extra_content with google.thought
            message = raw_response.choices[0].message
            if hasattr(message, "extra_content") and message.extra_content:
                google_content = message.extra_content.get("google", {})
                if google_content and google_content.get("thought") is True:
                    reasoning_text = message.content
                    full_content_from_api = ""  # Clear content as it's reasoning
                else:
                    full_content_from_api = message.content
            else:
                full_content_from_api = message.content

        return raw_response, reasoning_text, full_content_from_api

    temp_task_for_keepalive_check = asyncio.create_task(_openai_api_call_wrapper())
    outer_keep_alive_interval = app_config.FAKE_STREAMING_INTERVAL_SECONDS
    # OpenAI streams share a single `id` and `created` across all
    # chunks of the same response.  Previously the keepalive loop
    # generated a new random id + new `created` per chunk — which is
    # detectable.  We now use `response_id` and a frozen `created_ts`
    # for the whole stream.
    created_ts = int(time.time())
    if outer_keep_alive_interval > 0:
        while not temp_task_for_keepalive_check.done():
            keep_alive_data = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": request_obj.model,
                "choices": [
                    {"delta": {}, "index": 0, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(keep_alive_data)}\n\n"
            jittered = outer_keep_alive_interval * random.uniform(0.75, 1.25)
            await asyncio.sleep(jittered)

    try:
        (
            full_api_response,
            separated_reasoning_text,
            separated_actual_content_text,
        ) = await temp_task_for_keepalive_check

        def _extract_openai_full_text(response: Any) -> str:
            if (
                response.choices
                and response.choices[0].message
                and response.choices[0].message.content is not None
            ):
                return response.choices[0].message.content
            return ""

        def _is_openai_response_valid(response: Any) -> bool:
            return bool(response.choices and response.choices[0].message is not None)

        async for chunk in _base_fake_stream_engine(
            api_call_task_creator=lambda: asyncio.create_task(
                asyncio.sleep(0, result=full_api_response)
            ),
            extract_text_from_response_func=_extract_openai_full_text,
            is_valid_response_func=_is_openai_response_valid,
            response_id=response_id,
            sse_model_name=request_obj.model,
            keep_alive_interval_seconds=0,
            is_auto_attempt=is_auto_attempt,
            reasoning_text_to_yield=separated_reasoning_text,
            actual_content_text_to_yield=separated_actual_content_text,
        ):
            yield chunk

    except Exception as e_outer:
        # Hardening: previously embedded type(e).__name__ - str(e)
        # into the SSE error message sent to the client.  Upstream
        # exceptions often contain "Gemini API" / "Vertex" / internal
        # gRPC text — leaking that this is a proxy.  We now log the
        # full error internally and emit a neutral upstream-error
        # message to the client.
        vertex_log(
            "error",
            f"openai_fake_stream_generator outer error (internal only): {type(e_outer).__name__}: {e_outer}",
        )
        sse_err_msg_display = "Upstream service error during streaming."
        err_resp_sse = create_openai_error_response(
            500, sse_err_msg_display, "server_error"
        )
        json_payload_error = json.dumps(err_resp_sse)
        if not is_auto_attempt:
            yield f"data: {json_payload_error}\n\n"
            yield "data: [DONE]\n\n"
