from app.utils.error_response import build_error_response
from app.utils.logging import log


# Previously this string was "空响应次数达到上限\n请修改输入提示词" which leaks
# the existence of an internal "empty-response retry mechanism" to the
# client.  Real models don't have such a concept, and exposing it is a
# proxy fingerprint.  The new message is a natural-sounding assistant
# reply that doesn't hint at internal retry logic.
EMPTY_RESPONSE_LIMIT_MESSAGE = (
    "I couldn't generate a response for that input. "
    "Could you rephrase or add more detail?"
)


def is_empty_gemini_response(response_content) -> bool:
    return not response_content or (
        not response_content.text and not response_content.function_call
    )


def build_empty_limit_response(is_gemini: bool, model: str, stream: bool):
    return build_error_response(
        is_gemini=is_gemini,
        model=model,
        content=EMPTY_RESPONSE_LIMIT_MESSAGE,
        stream=stream,
    )


def log_empty_response_limit(empty_response_count: int, max_empty_responses: int, request_type: str, model: str):
    log(
        "warning",
        f"空响应次数达到上限 ({empty_response_count}/{max_empty_responses})，停止轮询",
        extra={"request_type": request_type, "model": model},
    )
