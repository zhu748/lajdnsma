import json
import time
import asyncio
import random
from typing import List, Dict, Any, Callable, Union, Optional
from fastapi.responses import JSONResponse, StreamingResponse

from google.genai import types
from app.vertex.message_processing import (
    parse_gemini_response_for_reasoning_and_content,
)

# Local module imports
from app.vertex.models import OpenAIRequest, OpenAIMessage  # Changed from relative
from app.vertex.message_processing import (
    deobfuscate_text,
    convert_to_openai_format,
    convert_chunk_to_openai,
    create_final_chunk,
)  # Changed from relative
import app.vertex.config as app_config  # Changed from relative
from app.config import settings  # 导入settings模块
from app.utils.stealth import gen_openai_chunk_id


def create_openai_error_response(
    status_code: int, message: str, error_type: str
) -> Dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": status_code,
            "param": None,
        }
    }


def create_generation_config(request: OpenAIRequest) -> Dict[str, Any]:
    config = {}
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.max_tokens is not None:
        config["max_output_tokens"] = request.max_tokens
    if request.top_p is not None:
        config["top_p"] = request.top_p
    if request.top_k is not None:
        config["top_k"] = request.top_k
    if request.stop is not None:
        config["stop_sequences"] = request.stop
    if request.seed is not None:
        config["seed"] = request.seed
    if request.presence_penalty is not None:
        config["presence_penalty"] = request.presence_penalty
    if request.frequency_penalty is not None:
        config["frequency_penalty"] = request.frequency_penalty
    if request.n is not None:
        config["candidate_count"] = request.n
    # Hardened: previously hardcoded all 5 categories to OFF.  Real
    # clients almost never do that, and sending 5-OFF safetySettings on
    # every request is a strong proxy fingerprint.  Now we omit
    # safety_settings entirely (let Vertex use its defaults), unless
    # the operator explicitly sets SAFETY_MODE!=default (see
    # app/config/safety.py:get_safety_settings).
    from app.config.safety import get_safety_settings
    safety_list = get_safety_settings(is_gemini_2=True)
    if safety_list:
        # Convert plain dict form to google.genai.types.SafetySetting
        config["safety_settings"] = [
            types.SafetySetting(
                category=entry.get("category", ""),
                threshold=entry.get("threshold", "BLOCK_NONE"),
            )
            for entry in safety_list
        ]
    return config


def is_response_valid(response):
    if response is None:
        print("DEBUG: Response is None, therefore invalid.")
        return False

    # Check for direct text attribute
    if (
        hasattr(response, "text")
        and isinstance(response.text, str)
        and response.text.strip()
    ):
        # print("DEBUG: Response valid due to response.text")
        return True

    # Check candidates for text content
    if hasattr(response, "candidates") and response.candidates:
        for candidate in response.candidates:  # Iterate through all candidates
            if (
                hasattr(candidate, "text")
                and isinstance(candidate.text, str)
                and candidate.text.strip()
            ):
                # print(f"DEBUG: Response valid due to candidate.text in candidate")
                return True
            if (
                hasattr(candidate, "content")
                and hasattr(candidate.content, "parts")
                and candidate.content.parts
            ):
                for part in candidate.content.parts:
                    if (
                        hasattr(part, "text")
                        and isinstance(part.text, str)
                        and part.text.strip()
                    ):
                        # print(f"DEBUG: Response valid due to part.text in candidate's content part")
                        return True

    # Removed prompt_feedback as a sole criterion for validity.
    # It should only be valid if actual text content is found.
    # Block reasons will be checked explicitly by callers if they need to treat it as an error for retries.
    print(
        "DEBUG: Response is invalid, no usable text content found by is_response_valid."
    )
    return False


async def _base_fake_stream_engine(
    api_call_task_creator: Callable[[], asyncio.Task],
    extract_text_from_response_func: Callable[[Any], str],
    response_id: str,
    sse_model_name: str,
    is_auto_attempt: bool,
    is_valid_response_func: Callable[[Any], bool],
    keep_alive_interval_seconds: float,
    process_text_func: Optional[Callable[[str, str], str]] = None,
    check_block_reason_func: Optional[Callable[[Any], None]] = None,
    reasoning_text_to_yield: Optional[str] = None,
    actual_content_text_to_yield: Optional[str] = None,
):
    api_call_task = api_call_task_creator()

    # OpenAI streams share a single `created` Unix timestamp across all
    # chunks of the same response (keepalive + content + final).  We
    # also share the `response_id` so the keepalive chunks are clearly
    # part of the same response — previously each keepalive chunk got
    # a new random id + new timestamp, which is detectable as
    # non-OpenAI behaviour.
    created_ts = int(time.time())

    if keep_alive_interval_seconds > 0:
        while not api_call_task.done():
            # Hardened: previously sent `delta: {reasoning_content: ""}`
            # which is a non-standard OpenAI stream pattern (real OpenAI
            # never emits empty reasoning_content deltas).  We now use an
            # empty delta object (no content field at all) which is the
            # closest thing to a real "no-content" heartbeat chunk OpenAI
            # ever emits.
            keep_alive_data = {
                # Use the shared response_id so keepalive chunks are
                # clearly part of the same stream (was: new random id
                # per keepalive, which is detectable).
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": sse_model_name,
                "choices": [
                    {
                        "delta": {},
                        "index": 0,
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(keep_alive_data)}\n\n"
            # Jittered interval (was fixed `keep_alive_interval_seconds`).
            jittered = keep_alive_interval_seconds * random.uniform(0.75, 1.25)
            await asyncio.sleep(jittered)

    try:
        full_api_response = await api_call_task

        if check_block_reason_func:
            check_block_reason_func(full_api_response)

        if not is_valid_response_func(full_api_response):
            raise ValueError(
                f"Invalid/empty API response in fake stream for model {sse_model_name}: {str(full_api_response)[:200]}"
            )

        final_reasoning_text = reasoning_text_to_yield
        final_actual_content_text = actual_content_text_to_yield

        if final_reasoning_text is None and final_actual_content_text is None:
            extracted_full_text = extract_text_from_response_func(full_api_response)
            if process_text_func:
                final_actual_content_text = process_text_func(
                    extracted_full_text, sse_model_name
                )
            else:
                final_actual_content_text = extracted_full_text
        else:
            if process_text_func:
                if final_reasoning_text is not None:
                    final_reasoning_text = process_text_func(
                        final_reasoning_text, sse_model_name
                    )
                if final_actual_content_text is not None:
                    final_actual_content_text = process_text_func(
                        final_actual_content_text, sse_model_name
                    )

        if final_reasoning_text:
            reasoning_delta_data = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": sse_model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": final_reasoning_text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(reasoning_delta_data)}\n\n"
            if final_actual_content_text:
                await asyncio.sleep(0.05)

        content_to_chunk = final_actual_content_text or ""
        # Hardened: previously split into exactly 10 equal chunks at 50ms
        # interval.  A 10-bucket/50ms pattern in the SSE byte-stream is
        # trivially detectable as a fake-stream proxy.  We now use a
        # randomised chunk size (between 24-96 chars) and randomised
        # inter-chunk delay (20-150ms) so the distribution looks like
        # real token-bound streaming.
        chunk_size = (
            max(20, random.randint(24, 96)) if content_to_chunk else 0
        )

        if not content_to_chunk and content_to_chunk != "":
            # Real OpenAI never emits `delta: {"content": ""}`.  We
            # emit an empty delta object instead — the closest legal
            # shape for a no-content heartbeat.
            empty_delta_data = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": sse_model_name,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(empty_delta_data)}\n\n"
        else:
            for i in range(0, len(content_to_chunk), chunk_size):
                chunk_text = content_to_chunk[i : i + chunk_size]
                content_delta_data = {
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
                yield f"data: {json.dumps(content_delta_data)}\n\n"
                if len(content_to_chunk) > chunk_size:
                    await asyncio.sleep(random.uniform(0.02, 0.15))

        yield create_final_chunk(sse_model_name, response_id, created_ts=created_ts)
        yield "data: [DONE]\n\n"

    except Exception as e:
        # Hardening: previously embedded `type(e).__name__ - str(e)`
        # directly into the SSE error message sent to the client.  We
        # now log the full exception internally and emit a neutral
        # upstream-error message to the client.
        vertex_log(
            "error",
            f"_base_fake_stream_engine error (internal only, model: {sse_model_name}): {type(e).__name__}: {e}",
        )
        err_resp_for_sse = create_openai_error_response(
            500, "Upstream service error during streaming.", "server_error"
        )
        json_payload_for_fake_stream_error = json.dumps(err_resp_for_sse)
        if not is_auto_attempt:
            yield f"data: {json_payload_for_fake_stream_error}\n\n"
            yield "data: [DONE]\n\n"
        raise


async def gemini_fake_stream_generator(
    gemini_client_instance: Any,
    model_for_api_call: str,
    prompt_for_api_call: Union[types.Content, List[types.Content]],
    gen_config_for_api_call: Dict[str, Any],
    request_obj: OpenAIRequest,
    is_auto_attempt: bool,
):
    model_name_for_log = getattr(
        gemini_client_instance, "model_name", "unknown_gemini_model_object"
    )
    print(
        f"FAKE STREAMING (Gemini): Prep for '{request_obj.model}' (API model string: '{model_for_api_call}', client obj: '{model_name_for_log}') with reasoning separation."
    )
    response_id = gen_openai_chunk_id()

    # 1. Create and await the API call task
    api_call_task = asyncio.create_task(
        gemini_client_instance.aio.models.generate_content(
            model=model_for_api_call,
            contents=prompt_for_api_call,
            config=gen_config_for_api_call,
        )
    )

    # Keep-alive loop while the main API call is in progress
    outer_keep_alive_interval = app_config.FAKE_STREAMING_INTERVAL_SECONDS
    if outer_keep_alive_interval > 0:
        while not api_call_task.done():
            # Hardened: empty delta (no content fields at all) instead of
            # empty reasoning_content.  Strong random chunk id instead of
            # the fixed "chatcmpl-keepalive" string.  Jittered interval.
            keep_alive_data = {
                "id": gen_openai_chunk_id(),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": request_obj.model,
                "choices": [
                    {
                        "delta": {},
                        "index": 0,
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(keep_alive_data)}\n\n"
            jittered = outer_keep_alive_interval * random.uniform(0.75, 1.25)
            await asyncio.sleep(jittered)

    try:
        raw_response = await api_call_task  # Get the full Gemini response

        # 2. Parse the response for reasoning and content using the centralized parser
        separated_reasoning_text = ""
        separated_actual_content_text = ""
        if hasattr(raw_response, "candidates") and raw_response.candidates:
            # Typically, fake streaming would focus on the first candidate
            separated_reasoning_text, separated_actual_content_text = (
                parse_gemini_response_for_reasoning_and_content(
                    raw_response.candidates[0]
                )
            )
        elif (
            hasattr(raw_response, "text") and raw_response.text is not None
        ):  # Fallback for simpler response structures
            separated_actual_content_text = raw_response.text

        # 3. Define a text processing function (e.g., for deobfuscation)
        def _process_gemini_text_if_needed(text: str, model_name: str) -> str:
            if model_name.endswith("-encrypt-full"):
                return deobfuscate_text(text)
            return text

        final_reasoning_text = _process_gemini_text_if_needed(
            separated_reasoning_text, request_obj.model
        )
        final_actual_content_text = _process_gemini_text_if_needed(
            separated_actual_content_text, request_obj.model
        )

        # Define block checking for the raw response
        def _check_gemini_block_wrapper(response_to_check: Any):
            if (
                hasattr(response_to_check, "prompt_feedback")
                and hasattr(response_to_check.prompt_feedback, "block_reason")
                and response_to_check.prompt_feedback.block_reason
            ):
                block_message = f"Response blocked by safety filter: {response_to_check.prompt_feedback.block_reason}"
                if (
                    hasattr(response_to_check.prompt_feedback, "block_reason_message")
                    and response_to_check.prompt_feedback.block_reason_message
                ):
                    block_message += f" (Message: {response_to_check.prompt_feedback.block_reason_message})"
                raise ValueError(block_message)

        # Call _base_fake_stream_engine with pre-split and processed texts
        async for chunk in _base_fake_stream_engine(
            api_call_task_creator=lambda: asyncio.create_task(
                asyncio.sleep(0, result=raw_response)
            ),  # Dummy task
            extract_text_from_response_func=lambda r: "",  # Not directly used as text is pre-split
            is_valid_response_func=is_response_valid,  # Validates raw_response
            check_block_reason_func=_check_gemini_block_wrapper,  # Checks raw_response
            process_text_func=None,  # Text processing already done above
            response_id=response_id,
            sse_model_name=request_obj.model,
            keep_alive_interval_seconds=0,  # Keep-alive for this inner call is 0
            is_auto_attempt=is_auto_attempt,
            reasoning_text_to_yield=final_reasoning_text,
            actual_content_text_to_yield=final_actual_content_text,
        ):
            yield chunk

    except Exception as e_outer_gemini:
        # Hardening: previously embedded `type(e).__name__ - str(e)`
        # into the SSE error message sent to the client.  We now log
        # the full exception internally and emit a neutral upstream-
        # error message to the client.
        vertex_log(
            "error",
            f"gemini_fake_stream_generator error (internal only, model: {request_obj.model}): {type(e_outer_gemini).__name__}: {e_outer_gemini}",
        )
        err_resp_sse = create_openai_error_response(
            500, "Upstream service error during streaming.", "server_error"
        )
        json_payload_error = json.dumps(err_resp_sse)
        if not is_auto_attempt:
            yield f"data: {json_payload_error}\n\n"
            yield "data: [DONE]\n\n"


async def execute_gemini_call(
    current_client: Any,
    model_to_call: str,
    prompt_func: Callable[
        [List[OpenAIMessage]], Union[types.Content, List[types.Content]]
    ],
    gen_config_for_call: Dict[str, Any],
    request_obj: OpenAIRequest,
    is_auto_attempt: bool = False,
):
    actual_prompt_for_call = prompt_func(request_obj.messages)
    client_model_name_for_log = getattr(
        current_client, "model_name", "unknown_direct_client_object"
    )
    print(
        f"INFO: execute_gemini_call for requested API model '{model_to_call}', using client object with internal name '{client_model_name_for_log}'. Original request model: '{request_obj.model}'"
    )

    # 每次调用时直接从settings获取最新的FAKE_STREAMING值
    fake_streaming_enabled = False
    if hasattr(settings, "FAKE_STREAMING"):
        fake_streaming_enabled = settings.FAKE_STREAMING
    else:
        fake_streaming_enabled = app_config.FAKE_STREAMING_ENABLED

    print(
        f"DEBUG: FAKE_STREAMING setting is {fake_streaming_enabled} for model {request_obj.model}"
    )

    if request_obj.stream:
        if fake_streaming_enabled:
            return StreamingResponse(
                gemini_fake_stream_generator(
                    current_client,
                    model_to_call,
                    actual_prompt_for_call,
                    gen_config_for_call,
                    request_obj,
                    is_auto_attempt,
                ),
                media_type="text/event-stream",
            )

        response_id_for_stream = gen_openai_chunk_id()
        cand_count_stream = request_obj.n or 1
        # Share a single `created` timestamp across the whole stream so
        # all chunks (including the final chunk) carry the same value —
        # matching real OpenAI streaming behaviour (previously each
        # chunk re-computed int(time.time())).
        created_ts_for_stream = int(time.time())

        async def _gemini_real_stream_generator_inner():
            try:
                async for (
                    chunk_item_call
                ) in await current_client.aio.models.generate_content_stream(
                    model=model_to_call,
                    contents=actual_prompt_for_call,
                    config=gen_config_for_call,
                ):
                    yield convert_chunk_to_openai(
                        chunk_item_call, request_obj.model, response_id_for_stream, 0,
                        created_ts=created_ts_for_stream,
                    )
                yield create_final_chunk(
                    request_obj.model, response_id_for_stream, cand_count_stream,
                    created_ts=created_ts_for_stream,
                )
                yield "data: [DONE]\n\n"
            except Exception as e_stream_call:
                # Hardening: previously embedded "Streaming Error (Gemini
                # API, model string: ...)" into the SSE error message
                # sent to the client.  Now logs internally and emits a
                # neutral upstream-error message.
                vertex_log(
                    "error",
                    f"Streaming call error (internal only, model: {model_to_call}): {type(e_stream_call).__name__}: {e_stream_call}",
                )
                err_resp = create_openai_error_response(500, "Upstream service error during streaming.", "server_error")
                j_err = json.dumps(err_resp)
                if not is_auto_attempt:
                    yield f"data: {j_err}\n\n"
                    yield "data: [DONE]\n\n"
                raise e_stream_call

        return StreamingResponse(
            _gemini_real_stream_generator_inner(), media_type="text/event-stream"
        )
    else:
        response_obj_call = await current_client.aio.models.generate_content(
            model=model_to_call,
            contents=actual_prompt_for_call,
            config=gen_config_for_call,
        )
        if (
            hasattr(response_obj_call, "prompt_feedback")
            and hasattr(response_obj_call.prompt_feedback, "block_reason")
            and response_obj_call.prompt_feedback.block_reason
        ):
            block_msg = (
                f"Blocked by safety filter: {response_obj_call.prompt_feedback.block_reason}"
            )
            if (
                hasattr(response_obj_call.prompt_feedback, "block_reason_message")
                and response_obj_call.prompt_feedback.block_reason_message
            ):
                block_msg += (
                    f" ({response_obj_call.prompt_feedback.block_reason_message})"
                )
            raise ValueError(block_msg)

        if not is_response_valid(response_obj_call):
            raise ValueError(
                f"Invalid non-streaming Gemini response for model string '{model_to_call}'. Resp: {str(response_obj_call)[:200]}"
            )
        return JSONResponse(
            content=convert_to_openai_format(response_obj_call, request_obj.model)
        )
