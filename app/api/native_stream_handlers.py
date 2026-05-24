from app.services import GeminiClient
from app.utils import handle_gemini_error, update_api_call_stats
from app.utils.gemini_response_processing import select_safety_settings
from app.utils.response import (
    ensure_gemini_timing_fields,
    include_reasoning_for_request,
    openAI_from_Gemini,
)
from app.utils.response_loop_helpers import (
    dump_json_response,
    log_empty_response_count,
    log_request_failure,
)
from app.utils.sse import sse_text


async def generate_native_stream_chunks(
    *,
    api_key: str,
    chat_request,
    contents,
    system_instruction,
    safety_settings,
    safety_settings_g2,
    is_gemini: bool,
    settings,
):
    """Yield native Gemini stream chunks and return a stream attempt summary."""
    success = False
    token = 0
    empty = False

    try:
        client = GeminiClient(api_key)
        stream_generator = client.stream_chat(
            chat_request,
            contents,
            select_safety_settings(
                chat_request.model, safety_settings, safety_settings_g2
            ),
            system_instruction,
        )

        async for chunk in stream_generator:
            if chunk:
                if chunk.total_token_count:
                    token = int(chunk.total_token_count)
                success = True
                if is_gemini:
                    json_payload = dump_json_response(
                        ensure_gemini_timing_fields(chunk.data)
                    )
                    yield "chunk", sse_text(json_payload)
                else:
                    yield "chunk", openAI_from_Gemini(
                        chunk,
                        stream=True,
                        include_reasoning=include_reasoning_for_request(
                            chat_request,
                            expose_protocol_thinking=getattr(
                                settings, "CLAUDE_EXPOSE_THINKING", False
                            ),
                        ),
                    )
            else:
                log_empty_response_count(
                    api_key,
                    request_type="stream",
                    model=chat_request.model,
                    empty_response_count=0,
                    max_empty_responses=settings.MAX_EMPTY_RESPONSES,
                    label="stream returned empty response count",
                )
                empty = True
                await update_api_call_stats(
                    settings.api_call_stats,
                    endpoint=api_key,
                    model=chat_request.model,
                    token=token,
                )
                break
    except Exception as e:
        error_detail = handle_gemini_error(e, api_key)
        log_request_failure(
            api_key,
            request_type="stream",
            model=chat_request.model,
            error_detail=error_detail,
            label="stream response request failed",
        )

    if success:
        await update_api_call_stats(
            settings.api_call_stats,
            endpoint=api_key,
            model=chat_request.model,
            token=token,
        )

    yield "summary", {"success": success, "empty": empty, "token": token}
