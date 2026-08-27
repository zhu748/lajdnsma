import time

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
from app.utils.stealth import gen_openai_chunk_id


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
    # Protocol: OpenAI 规定同一次流式补全的所有 chunk 共享相同的 id 与
    # created。旧实现每收到一个上游 chunk 就生成全新 id —— 按 id 聚合
    # chunk 的客户端会把一次响应拆成 N 个"补全"，且"每 chunk 换 id"
    # 本身就是极易识别的代理指纹。每个重试尝试生成一次并贯穿整个流。
    stream_chunk_id = gen_openai_chunk_id()
    stream_created = int(time.time())

    stream_generator = None
    try:
        # Round 6: RPM 窗口发射计数（见 stats.record_outbound_attempt）。
        try:
            from app.utils.stats import record_outbound_attempt

            record_outbound_attempt(api_key, chat_request.model)
        except Exception:
            pass
        client = GeminiClient(api_key)
        stream_generator = client.stream_chat(
            chat_request,
            contents,
            select_safety_settings(
                chat_request.model, safety_settings, safety_settings_g2
            ),
            system_instruction,
        )

        # 注：保留 `if chunk:` 真值分支的空 chunk（None）防御处理 ——
        # 生产路径 stream_chat 只产出 GeminiResponseWrapper（恒真），但
        # 该防御路径由 tests/test_native_stream_handlers.py::
        # test_empty_chunk_marks_empty 显式覆盖，属有意保留。
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
                            chat_request
                        ),
                        chunk_id=stream_chunk_id,
                        created=stream_created,
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
    finally:
        # Resource-safety: 客户端断开时本生成器被关闭（GeneratorExit 在
        # yield 点抛出），旧实现对内层上游流（httpx stream）不做任何显式
        # 清理，连接释放完全依赖异步生成器的 GC 终结器 —— 在非确定性
        # 延迟期间持续占用共享连接池（上限 200）。显式 aclose 确定性
        # 地关闭内层流；对已耗尽/已关闭的生成器是安全的 no-op。
        if stream_generator is not None:
            try:
                await stream_generator.aclose()
            except Exception:
                pass

    if success:
        await update_api_call_stats(
            settings.api_call_stats,
            endpoint=api_key,
            model=chat_request.model,
            token=token,
        )

    yield "summary", {"success": success, "empty": empty, "token": token}
