from fastapi import HTTPException, status

from app.services.gemini import GeminiClient


def prepare_request_messages(chat_request):
    """统一处理请求消息格式转换。"""
    is_gemini = getattr(chat_request, "format_type", None) == "gemini"
    if is_gemini:
        return True, None, None

    source_protocol = getattr(chat_request, "source_protocol", None)
    try:
        contents, system_instruction = GeminiClient.convert_messages(
            GeminiClient,
            chat_request.messages,
            use_system_prompt=True,
            model=chat_request.model,
            skip_random_string=source_protocol in {"claude", "responses"},
        )
    except ValueError as exc:
        # convert_messages 校验失败（非法 role / 非法图片 URL 等）。
        # 这是客户端输入错误，必须以 400 拒绝，而不是让其演变成 500
        # 或把错误串发给上游。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request messages: {exc}",
        ) from exc
    return False, contents, system_instruction
