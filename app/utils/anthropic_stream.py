"""Anthropic SSE 内容块与工具调用状态。"""
from contextlib import aclosing
from typing import Any, AsyncIterator

from app.utils.protocol_common import (
    _merge_stream_usage, _now_iso_rfc3339,
    _openai_finish_reason_to_claude_stop_reason, _iter_chat_stream_events,
    _gen_anthropic_message_id,
)
from app.utils.sse import sse_event


async def openai_stream_to_claude_stream(
    body_iterator: AsyncIterator[Any],
    model: str,
) -> AsyncIterator[str]:
    message_id = _gen_anthropic_message_id()
    yield sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    latest_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    thinking_started = False
    thinking_stopped = False
    text_started = False
    text_stopped = False
    next_content_index = 0
    text_index = None
    thinking_index = None
    tool_blocks: dict[int, int] = {}

    # Pings accompany received chunks; idle heartbeats belong to the transport.
    _chunk_count_since_ping = 0

    def maybe_ping() -> str | None:
        nonlocal _chunk_count_since_ping
        _chunk_count_since_ping += 1
        if _chunk_count_since_ping >= 15:
            _chunk_count_since_ping = 0
            return sse_event(
                "ping",
                {
                    "type": "ping",
                    "timestamp": _now_iso_rfc3339(),
                },
            )
        return None

    def next_index() -> int:
        nonlocal next_content_index
        index = next_content_index
        next_content_index += 1
        return index

    def stop_thinking_event() -> str | None:
        nonlocal thinking_stopped
        if thinking_started and not thinking_stopped:
            thinking_stopped = True
            return sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": thinking_index},
            )
        return None

    def stop_text_event() -> str | None:
        nonlocal text_stopped
        if text_started and not text_stopped:
            text_stopped = True
            return sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": text_index},
            )
        return None

    async with aclosing(_iter_chat_stream_events(body_iterator)) as events:
        async for parsed in events:
            if parsed.get("error"):
                error = parsed["error"]
                message = error.get("message", "Upstream service error") if isinstance(error, dict) else str(error)
                yield sse_event("error", {"type": "error", "error": {"type": "api_error", "message": message}})
                return
            # Emit Anthropic keepalive `ping` event periodically.
            ping = maybe_ping()
            if ping:
                yield ping

            choice = (parsed.get("choices") or [{}])[0]
            delta = choice.get("delta", {})
            reasoning_text = delta.get("reasoning_content")
            if reasoning_text:
                stop_event = stop_text_event()
                if stop_event:
                    yield stop_event
                if thinking_stopped:
                    thinking_started = False
                    thinking_stopped = False
                if not thinking_started:
                    thinking_started = True
                    thinking_index = next_index()
                    yield sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": thinking_index,
                            "content_block": {
                                "type": "thinking",
                                "thinking": "",
                                "signature": "",
                            },
                        },
                    )
                yield sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": thinking_index,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": reasoning_text,
                        },
                    },
                )

            # Preserve an upstream signature when available; random data is
            # not a valid cryptographic signature for either provider.
            signature_payload = ""
            extra_content = delta.get("extra_content") or {}
            google_extra = extra_content.get("google", {}) if isinstance(extra_content, dict) else {}
            if isinstance(google_extra, dict):
                signature_payload = (
                    google_extra.get("thought_signature")
                    or google_extra.get("thoughtSignature")
                    or ""
                )
            if signature_payload and thinking_started and not thinking_stopped:
                yield sse_event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": thinking_index,
                     "delta": {"type": "signature_delta", "signature": signature_payload}},
                )
            text = delta.get("content")
            if text:
                stop_event = stop_thinking_event()
                if stop_event:
                    yield stop_event
                if text_stopped:
                    text_started = False
                    text_stopped = False
                if not text_started:
                    text_started = True
                    text_index = next_index()
                    yield sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": text_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": text_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )

            # The shared normalizer guarantees stable IDs/names before the
            # first argument delta. This renderer only owns content-block IDs.
            for tool_call in delta.get("tool_calls") or []:
                tool_index = tool_call["index"]
                function_data = tool_call["function"]
                content_index = tool_blocks.get(tool_index)
                if content_index is None:
                    for stop_event in (stop_thinking_event(), stop_text_event()):
                        if stop_event:
                            yield stop_event
                    content_index = next_index()
                    tool_blocks[tool_index] = content_index
                    block = {
                        "type": "tool_use", "id": tool_call["id"],
                        "name": function_data["name"], "input": {},
                    }
                    google_extra = (tool_call.get("extra_content") or {}).get("google", {})
                    signature = google_extra.get("thought_signature") or google_extra.get("thoughtSignature")
                    if signature:
                        block["thought_signature"] = signature
                    yield sse_event("content_block_start", {
                        "type": "content_block_start", "index": content_index, "content_block": block,
                    })
                arguments_delta = function_data.get("arguments")
                if arguments_delta is not None:
                    yield sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": content_index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments_delta},
                    })

            usage = parsed.get("usage", {})
            if usage:
                latest_usage = _merge_stream_usage(latest_usage, usage)

            if not choice.get("finish_reason"):
                continue
            stop_reason = _openai_finish_reason_to_claude_stop_reason(
                choice.get("finish_reason")
            )

            for stop_event in (stop_text_event(), stop_thinking_event()):
                if stop_event:
                    yield stop_event

            for content_index in tool_blocks.values():
                yield sse_event("content_block_stop", {
                    "type": "content_block_stop", "index": content_index,
                })

            if not text_started and not thinking_started and not tool_blocks:
                empty_index = next_index()
                yield sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": empty_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                yield sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": empty_index},
                )
            yield sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "input_tokens": latest_usage["input_tokens"],
                        "output_tokens": latest_usage["output_tokens"],
                    },
                },
            )
            yield sse_event("message_stop", {"type": "message_stop"})
