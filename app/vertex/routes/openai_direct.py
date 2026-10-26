"""Vertex OpenAI Direct 路径的实现（从 routes/chat_api.py 拆出）。

包含该路径的完整链路：
1. AsyncOpenAI 客户端缓存（按 endpoint/token/headers 键控，防 FD 泄漏）
2. Google REST 请求头与最终出站头清理
3. 真/伪流式与非流式三种调用形态
4. extra_content.google.thought → reasoning_content 的三种提取路径

对外主入口：handle_openai_direct_request()（由 chat_api 的
chat_completions 端点调用）与 close_cached_openai_clients()（由
main.py 的 lifespan 关闭阶段调用）。
"""
import asyncio
import json
import random
import time
from typing import Any, Dict

import openai
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.config.safety import get_safety_settings
from app.utils.logging import vertex_log
from app.utils.stealth import gen_openai_chunk_id, build_openai_compat_headers
from app.utils.http_client import build_async_client, UPSTREAM_TIMEOUT
from app.vertex.api_helpers import create_openai_error_response
from app.vertex.credentials_manager import _refresh_auth
from app.vertex.models import OpenAIRequest
import app.vertex.config as app_config
from app.vertex import client_pool

# OpenAI and native clients share reference-counted retirement so evicting a
# cached client never interrupts a response that is still being streamed.
async def _close_client_quietly(client):
    try:
        await client.close()
    except Exception:
        vertex_log("warning", "关闭 OpenAI 客户端失败")


async def _get_cached_openai_client(base_url, gcp_token, stealth_headers):
    key = ("openai", base_url, gcp_token, tuple(sorted(stealth_headers.items())))
    return client_pool.get_client(key, lambda: openai.AsyncOpenAI(
        base_url=base_url, api_key=gcp_token, default_headers=stealth_headers,
        http_client=build_async_client(event_hooks={"request": [_prepare_google_request]}),
        timeout=UPSTREAM_TIMEOUT, max_retries=0,
    ), closer=_close_client_quietly)


async def _prepare_google_request(request):
    """Remove SDK telemetry after all SDK merges, just before HTTP transport.

    Empty strings still send headers, and retry/timeout headers are inserted
    after default_headers. OpenAI account environment settings must not be
    forwarded to Google either. Keep Google's auth/quota headers intact.
    """
    for name in list(request.headers):
        if name.startswith("x-stainless-") or name in {"openai-organization", "openai-project"}:
            del request.headers[name]


async def close_cached_openai_clients():
    await client_pool.shutdown("openai")


# ---------------------------------------------------------------------------
# extra_content.google.thought 提取（真流 / 非流两个形态）
# ---------------------------------------------------------------------------


def _strip_openai_extra_content(delta: dict) -> None:
    """原位处理流式 chunk delta 中的 extra_content.google.thought 标记。

    thought=True 时把 delta.content 移入 reasoning_content 并剥掉
    extra_content 信封（真实 OpenAI chunk 从不携带 extra_content）；
    否则仅剥离 extra_content 信封以免下游识别出 Vertex 直通。
    """
    extra_content = delta.get("extra_content")
    if not isinstance(extra_content, dict):
        return
    google_content = extra_content.get("google")
    if not isinstance(google_content, dict):
        return
    if google_content.get("thought") is True:
        reasoning_text = delta.get("content")
        if reasoning_text is not None:
            delta["reasoning_content"] = reasoning_text
        delta.pop("content", None)
    delta.pop("extra_content", None)


def _extract_reasoning_from_message(message_dict: dict) -> None:
    """原位处理非流式响应 message 中的 extra_content（thought → reasoning）。"""
    extra_content = message_dict.get("extra_content", {})
    google_content = extra_content.get("google", {})
    if google_content and google_content.get("thought") is True:
        message_dict["reasoning_content"] = message_dict.get("content", "")
        message_dict["content"] = ""
    message_dict.pop("extra_content", None)


# ---------------------------------------------------------------------------
# 真流式：SSE 透传
# ---------------------------------------------------------------------------


async def openai_true_stream_generator(
    openai_client: openai.AsyncOpenAI,
    openai_params: Dict[str, Any],
    openai_extra_body: Dict[str, Any],
    model_name: str,
):
    """OpenAI Direct 真流式：透传上游 SSE chunk 并剥离 extra_content。"""
    stream_response = None
    finished = set()
    try:
        # Ensure stream=True is explicitly passed for real streaming
        openai_params_for_true_stream = {
            **openai_params, "stream": True,
            "stream_options": {**(openai_params.get("stream_options") or {}), "include_usage": True},
        }
        stream_response = await openai_client.chat.completions.create(
            **openai_params_for_true_stream, extra_body=openai_extra_body,
            extra_headers={"Accept": "text/event-stream"},
        )
        async for chunk in stream_response:
            try:
                chunk_as_dict = chunk.model_dump(exclude_unset=True, exclude_none=True)

                choices = chunk_as_dict.get("choices")
                for choice in choices or []:
                    if choice.get("finish_reason"):
                        finished.add(choice.get("index", 0))
                for choice in choices or []:
                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        _strip_openai_extra_content(delta)

                yield f"data: {json.dumps(chunk_as_dict)}\n\n"

            except Exception as chunk_processing_error:
                error_msg_chunk = (
                    f"处理模型 {model_name} 的流式响应失败：{str(chunk_processing_error)}"
                )
                vertex_log("error", f"请求处理失败：{error_msg_chunk}")
                if len(error_msg_chunk) > 1024:
                    error_msg_chunk = error_msg_chunk[:1024] + "..."
                error_response_chunk = create_openai_error_response(
                    500, "Upstream service error during streaming.", "server_error"
                )
                json_payload_for_chunk_error = json.dumps(error_response_chunk)
                yield f"data: {json_payload_for_chunk_error}\n\n"
                yield "data: [DONE]\n\n"
                return
        if len(finished) < (openai_params.get("n") or 1):
            raise ValueError("Upstream stream ended without all finish reasons")
        yield "data: [DONE]\n\n"
    except Exception as stream_error:
        original_error_message = str(stream_error)
        if len(original_error_message) > 1024:
            original_error_message = original_error_message[:1024] + "..."
        error_msg_stream = (
            f"模型 {model_name} 的 OpenAI 流式调用失败：{original_error_message}"
        )
        vertex_log("error", f"请求处理失败：{error_msg_stream}")
        error_response_content = create_openai_error_response(
            500, "Upstream service error during streaming.", "server_error"
        )
        json_payload_for_stream_error = json.dumps(error_response_content)
        yield f"data: {json_payload_for_stream_error}\n\n"
        yield "data: [DONE]\n\n"


    finally:
        if stream_response is not None:
            close = getattr(stream_response, "aclose", None) or getattr(stream_response, "close", None)
            if close is not None:
                await close()


# ---------------------------------------------------------------------------
# 伪流式：非流式调用 + SSE 模拟
# ---------------------------------------------------------------------------


async def _openai_fake_stream_engine(
    api_call_task_creator,
    extract_text_from_response_func,
    is_valid_response_func,
    response_id,
    sse_model_name,
    keep_alive_interval_seconds=0,
    is_auto_attempt=False,
    reasoning_text_to_yield="",
    actual_content_text_to_yield="",
    created_ts=None,
):
    """OpenAI Direct 伪流式引擎。与 api_helpers._base_fake_stream_engine
    （Gemini 版本）是两份独立实现——keepalive 位置、空响应处理与异常
    重抛语义不同，刻意不合并；命名区分以避免误读为同一实现。"""
    try:
        # Wait for the API call to complete
        api_response = await api_call_task_creator()

        # Validate the response
        if not is_valid_response_func(api_response):
            error_msg = (
                f"模型 {sse_model_name} 的上游响应结构无效"
            )
            vertex_log("error", f"请求处理失败：{error_msg}")
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
        created_ts = created_ts if created_ts is not None else int(time.time())
        choices = getattr(api_response, "choices", None) or []
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        finish_reason = finish_reason or "stop"
        message = getattr(choices[0], "message", None) if choices else None
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            calls = [call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else dict(call) for call in tool_calls]
            for index, call in enumerate(calls):
                call["index"] = index
            yield "data: " + json.dumps({
                "id": response_id, "object": "chat.completion.chunk",
                "created": created_ts, "model": sse_model_name,
                "choices": [{"index": 0, "delta": {"tool_calls": calls}, "finish_reason": None}],
            }) + "\n\n"

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
                    {"index": 0, "delta": {}, "finish_reason": finish_reason}
                ],
            }
            usage = getattr(api_response, "usage", None)
            if usage is not None:
                empty_chunk["usage"] = usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else usage
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
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        usage = getattr(api_response, "usage", None)
        if usage is not None:
            final_chunk["usage"] = usage.model_dump(exclude_none=True) if hasattr(usage, "model_dump") else usage
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        # Hardening: previously embedded str(e) directly into the
        # SSE error message sent to the client.  Now logs internally
        # and emits a neutral message.
        vertex_log(
            "error",
            f"OpenAI 伪流式引擎出错，模型={sse_model_name}，错误={type(e).__name__}: {e}",
        )
        if not is_auto_attempt:  # Only yield error for non-auto attempts
            err_resp = create_openai_error_response(
                500, "Upstream service error during streaming.", "server_error"
            )
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
    """OpenAI Direct 伪流式：后台非流式调用 + keepalive 心跳 + 分块回放。"""
    api_model_name = openai_params.get("model", "unknown-openai-model")
    vertex_log(
        "info",
        f"准备 OpenAI 伪流式请求：请求模型={request_obj.model}，上游模型={api_model_name}",
    )
    response_id = gen_openai_chunk_id()

    async def _openai_api_call_wrapper():
        params_for_non_stream_call = openai_params.copy()
        params_for_non_stream_call["stream"] = False

        _api_call_task = asyncio.create_task(
            openai_client.chat.completions.create(
                **params_for_non_stream_call, extra_body=openai_extra_body,
                extra_headers={"Accept": "application/json"},
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
    # Resource fix: if the client disconnects while keep-alive chunks
    # are being yielded, this generator is closed at the `yield` below and
    # the `await temp_task_for_keepalive_check` further down never runs —
    # the upstream OpenAI request then runs to completion as an orphan,
    # burning quota and the shared connection pool.  try/finally cancels
    # it on every exit path (cancel on a finished task is a no-op).
    try:
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

            async for chunk in _openai_fake_stream_engine(
                api_call_task_creator=lambda: asyncio.create_task(
                    asyncio.sleep(0, result=full_api_response)
                ),
                extract_text_from_response_func=_extract_openai_full_text,
                is_valid_response_func=_is_openai_response_valid,
                response_id=response_id,
                created_ts=created_ts,
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
                f"OpenAI 伪流式响应处理失败：{type(e_outer).__name__}: {e_outer}",
            )
            sse_err_msg_display = "Upstream service error during streaming."
            err_resp_sse = create_openai_error_response(
                500, sse_err_msg_display, "server_error"
            )
            json_payload_error = json.dumps(err_resp_sse)
            if not is_auto_attempt:
                yield f"data: {json_payload_error}\n\n"
                yield "data: [DONE]\n\n"
    finally:
        temp_task_for_keepalive_check.cancel()
        await asyncio.gather(temp_task_for_keepalive_check, return_exceptions=True)


# ---------------------------------------------------------------------------
# 编排主入口
# ---------------------------------------------------------------------------

OPENAI_DIRECT_LOCATION = "global"  # 固定为 global（用户确认）


def _build_openai_direct_headers(gcp_token: str, is_stream: bool) -> Dict[str, str]:
    """Build the shared Google REST profile; SDK-added headers are removed later."""
    return build_openai_compat_headers(gcp_token, streaming=is_stream)


async def handle_openai_direct_request(
    request: OpenAIRequest,
    credential_manager_instance,
    base_model_name: str,
):
    """OpenAI Direct 路径的编排主入口（由 chat_completions 调用）。"""
    vertex_log("info", f"模型 {request.model} 使用 OpenAI 直连路径")
    # This mode exclusively uses rotated credentials, not express keys.
    rotated_credentials, rotated_project_id = (
        credential_manager_instance.get_random_credentials()
    )

    if not rotated_credentials or not rotated_project_id:
        error_msg = (
            f"OpenAI 直连模式需要 GCP 凭证，但当前没有已加载的可用凭证"
        )
        vertex_log("error", f"请求处理失败：{error_msg}")
        return JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, error_msg, "server_error"),
        )

    vertex_log(
        "info",
        f"OpenAI 直连使用项目 {rotated_project_id} 的凭证",
    )
    gcp_token = await asyncio.to_thread(_refresh_auth, rotated_credentials)

    if not gcp_token:
        error_msg = (
            f"无法为 OpenAI 客户端获取有效 GCP 令牌，项目={rotated_project_id}"
        )
        vertex_log("error", f"请求处理失败：{error_msg}")
        return JSONResponse(
            status_code=500,
            content=create_openai_error_response(500, error_msg, "server_error"),
        )

    project_id = rotated_project_id
    location = OPENAI_DIRECT_LOCATION
    vertex_ai_openai_endpoint_url = (
        f"https://aiplatform.googleapis.com/v1beta1/"
        f"projects/{project_id}/locations/{location}/endpoints/openapi"
    )
    underlying_model_id = f"google/{base_model_name}"

    # Pool identity must not depend on downstream streaming: a fake stream
    # sends a JSON request upstream. True SSE sets Accept on that request only.
    stealth_headers = _build_openai_direct_headers(gcp_token, False)
    quota_project = getattr(rotated_credentials, "quota_project_id", None)
    if quota_project:
        stealth_headers["x-goog-user-project"] = quota_project
    # Perf fix: 客户端由有界缓存按 (endpoint, token, headers) 提供，
    # 连接池在同一 GCP token 期间跨请求复用（见 _get_cached_openai_client）。
    openai_client = await _get_cached_openai_client(
        vertex_ai_openai_endpoint_url,
        gcp_token,
        stealth_headers,
    )

    # Hardening: previously hardcoded all 5 safety categories to "OFF"
    # on every OpenAI Direct path request — the same "all-OFF
    # safetySettings" fingerprint already fixed on the Gemini path.
    # We now respect SAFETY_MODE (default = don't send safety_settings
    # at all).  See app/config/safety.py:get_safety_settings.
    openai_safety_settings = get_safety_settings()

    openai_params = {
        "model": underlying_model_id,
        "messages": [msg.model_dump(exclude_unset=True) for msg in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "top_p": request.top_p,
        "stream": request.stream,
        "stop": request.stop,
        "seed": request.seed,
        "n": request.n,
        **{key: getattr(request, key) for key in (
            "tools", "tool_choice", "parallel_tool_calls", "response_format", "reasoning_effort",
        ) if getattr(request, key, None) is not None},
    }
    openai_params = {k: v for k, v in openai_params.items() if v is not None}

    # Only attach safety_settings when SAFETY_MODE != "default"
    # (None means "don't send the field at all").  Sending an
    # all-OFF block on every request is a fingerprint.
    if openai_safety_settings:
        openai_extra_body = {"google": {"safety_settings": openai_safety_settings}}
    else:
        openai_extra_body = None

    client_pool.retain(openai_client)

    if request.stream:
        # 每次调用时直接从 settings 获取最新的 FAKE_STREAMING 值
        if hasattr(settings, "FAKE_STREAMING"):
            fake_streaming_enabled = settings.FAKE_STREAMING
        else:
            fake_streaming_enabled = app_config.FAKE_STREAMING_ENABLED

        vertex_log(
            "info",
            f"OpenAI 模型 {request.model} 的伪流式开关：{fake_streaming_enabled}",
        )

        if fake_streaming_enabled:
            vertex_log(
                "info",
                f"模型 {request.model} 已启用 OpenAI 伪流式响应",
            )
            # openai_params 已含 stream=True；伪流式生成器内部会用
            # stream=False 的非流式调用。
            return StreamingResponse(
                client_pool.stream_with_release(openai_fake_stream_generator(
                    openai_client=openai_client,
                    openai_params=openai_params,
                    openai_extra_body=openai_extra_body,
                    request_obj=request,
                    is_auto_attempt=False,
                    # --- tokenizer / reasoning 拆分所需的凭证信息 ---
                    gcp_credentials=rotated_credentials,
                    gcp_project_id=project_id,
                    gcp_location=location,
                    base_model_id_for_tokenizer=base_model_name,
                ), openai_client),
                media_type="text/event-stream",
            )
        else:  # Regular OpenAI streaming
            vertex_log(
                "info",
                f"模型 {request.model} 已启用 OpenAI 真流式响应",
            )
            return StreamingResponse(
                client_pool.stream_with_release(openai_true_stream_generator(
                    openai_client, openai_params, openai_extra_body, request.model
                ), openai_client),
                media_type="text/event-stream",
            )

    # --- 非流式调用 ---
    try:
        # Ensure stream=False is explicitly passed for non-streaming
        openai_params_for_non_stream = {**openai_params, "stream": False}
        response = await openai_client.chat.completions.create(
            **openai_params_for_non_stream,
            extra_body=openai_extra_body,
            extra_headers={"Accept": "application/json"},
        )
        response_dict = response.model_dump(exclude_unset=True, exclude_none=True)

        try:
            # Extract reasoning directly from the response
            choices = response_dict.get("choices")
            if choices and isinstance(choices, list) and len(choices) > 0:
                message_dict = choices[0].get("message")
                if message_dict and isinstance(message_dict, dict):
                    if "extra_content" in message_dict:
                        _extract_reasoning_from_message(message_dict)
                        vertex_log(
                            "debug",
                            f"已处理响应消息中的扩展内容 extra_content",
                        )
        except Exception as e_reasoning_processing:
            vertex_log(
                "warning",
                f"模型 {request.model} 的非流式思考内容处理失败：{e_reasoning_processing}",
            )

        return JSONResponse(content=response_dict)
    except Exception as generate_error:
        error_msg_generate = (
            f"Error calling OpenAI client for {request.model}: {str(generate_error)}"
        )
        vertex_log("error", error_msg_generate)
        error_response = create_openai_error_response(
            500, "Upstream service error.", "server_error"
        )
        return JSONResponse(status_code=500, content=error_response)

    finally:
        await client_pool.release(openai_client)
