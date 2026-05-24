from app.services.gemini import GeminiClient


def prepare_request_messages(chat_request):
    """统一处理请求消息格式转换。"""
    is_gemini = getattr(chat_request, "format_type", None) == "gemini"
    if is_gemini:
        return True, None, None

    source_protocol = getattr(chat_request, "source_protocol", None)
    contents, system_instruction = GeminiClient.convert_messages(
        GeminiClient,
        chat_request.messages,
        use_system_prompt=True,
        model=chat_request.model,
        skip_random_string=source_protocol in {"claude", "responses"},
    )
    return False, contents, system_instruction
