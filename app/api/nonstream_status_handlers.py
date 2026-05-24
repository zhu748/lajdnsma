import app.config.settings as settings
from app.utils.error_handling import handle_gemini_error
from app.utils.response import (
    ensure_gemini_timing_fields,
    include_reasoning_for_request,
    openAI_from_Gemini,
)
from app.utils.response_loop_helpers import (
    dump_json_response,
    log_empty_response_count,
    log_request_success,
)


async def handle_nonstream_task_status(
    *,
    task,
    api_key: str,
    chat_request,
    response_cache_manager,
    cache_key: str,
    is_gemini: bool,
    empty_response_count: int,
    serialize_json: bool = False,
):
    try:
        status = task.result()
        if status == "success":
            log_request_success(
                api_key,
                request_type="non-stream",
                model=chat_request.model,
                label="non-stream request success",
            )
            cached_response, _ = await response_cache_manager.get_and_remove(cache_key)
            if is_gemini:
                response = ensure_gemini_timing_fields(cached_response.data)
            else:
                response = openAI_from_Gemini(
                    cached_response,
                    stream=False,
                    include_reasoning=include_reasoning_for_request(
                        chat_request
                    ),
                )
            if serialize_json:
                response = dump_json_response(response)
            return "success", response, empty_response_count

        if status == "empty":
            empty_response_count += 1
            log_empty_response_count(
                api_key,
                request_type="non-stream",
                model=chat_request.model,
                empty_response_count=empty_response_count,
                max_empty_responses=settings.MAX_EMPTY_RESPONSES,
            )
            return "empty", None, empty_response_count

        return status, None, empty_response_count
    except Exception as e:
        handle_gemini_error(e, api_key)
        return "error", None, empty_response_count
