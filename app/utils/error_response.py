from app.utils.response import gemini_from_text, openAI_from_text


def build_error_response(is_gemini: bool, model: str, content: str, stream: bool):
    """统一构造 Gemini / OpenAI 兼容错误响应。

    Hardening: previously used `role="error"` for the OpenAI branch —
    `error` is not a valid `role` value in the OpenAI Chat Completions
    spec (only `system`, `user`, `assistant`, `tool` are allowed) and
    was therefore a non-standard fingerprint.  We now use the
    canonical `assistant` role.
    """
    if is_gemini:
        return gemini_from_text(
            content=content,
            finish_reason="STOP",
            stream=stream,
        )

    return openAI_from_text(
        model=model,
        content=content,
        finish_reason="stop",
        stream=stream,
        role="assistant",
    )
