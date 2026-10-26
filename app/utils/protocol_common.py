"""protocol 系列共享底座：时间戳、usage 归一化、SSE 解析。"""
import codecs
from contextlib import aclosing
import io
import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List

from app.utils.tool_deltas import ToolDeltaNormalizer
from app.utils.stealth import (
    gen_openai_response_id,
    gen_openai_message_id,
    gen_responses_function_call_id,
    gen_anthropic_message_id,
    gen_anthropic_tool_use_id,
)


def _now_ts() -> int:
    return int(time.time())


def _now_iso_rfc3339() -> str:
    """RFC 3339 UTC timestamp, e.g. `2024-01-01T00:00:00.000Z`.

    Used by Anthropic `ping` events which carry a `timestamp` field.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Re-exported strong-random ID generators so protocol_streams.py has a
# single import surface for all ID helpers (avoids circular imports and
# keeps protocol_common.py the canonical place for protocol helpers).
_gen_response_id = gen_openai_response_id
_gen_message_id = gen_openai_message_id
_gen_function_call_id = gen_responses_function_call_id
_gen_anthropic_message_id = gen_anthropic_message_id
_gen_anthropic_tool_use_id = gen_anthropic_tool_use_id


def _extract_openai_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    total = int(usage.get("total_tokens", 0) or 0)
    if "completion_tokens" in usage and usage.get("completion_tokens") is not None:
        completion = int(usage.get("completion_tokens") or 0)
    elif "output_tokens" in usage and usage.get("output_tokens") is not None:
        completion = int(usage.get("output_tokens") or 0)
    elif total and prompt:
        completion = max(total - prompt, 0)
    else:
        completion = total
    if not total:
        total = prompt + completion
    result = {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
    }
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        result["output_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}
    return result


def _merge_stream_usage(
    latest_usage: Dict[str, int], usage: Dict[str, Any]
) -> Dict[str, int]:
    if not usage:
        return latest_usage

    merged = latest_usage.copy()
    if "prompt_tokens" in usage and usage.get("prompt_tokens") is not None:
        merged["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    elif "input_tokens" in usage and usage.get("input_tokens") is not None:
        merged["input_tokens"] = int(usage.get("input_tokens") or 0)

    if "completion_tokens" in usage and usage.get("completion_tokens") is not None:
        merged["output_tokens"] = int(usage.get("completion_tokens") or 0)
    elif "output_tokens" in usage and usage.get("output_tokens") is not None:
        merged["output_tokens"] = int(usage.get("output_tokens") or 0)
    elif "total_tokens" in usage and usage.get("total_tokens") is not None:
        total_tokens = int(usage.get("total_tokens") or 0)
        merged["output_tokens"] = (
            max(total_tokens - merged.get("input_tokens", 0), 0)
            if merged.get("input_tokens")
            else total_tokens
        )

    if "total_tokens" in usage and usage.get("total_tokens") is not None:
        merged["total_tokens"] = int(usage.get("total_tokens") or 0)
    else:
        merged["total_tokens"] = (
            merged.get("input_tokens", 0) + merged.get("output_tokens", 0)
        )
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        merged["output_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}

    return merged


def _openai_finish_reason_to_claude_stop_reason(finish_reason: Any) -> str:
    if not isinstance(finish_reason, str):
        return "end_turn"

    normalized = finish_reason.lower()
    if normalized in {"tool_calls", "function_call"}:
        return "tool_use"
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return "max_tokens"
    if normalized in {"stop", "stop_sequence", "end_turn"}:
        return "end_turn"
    if normalized in {"content_filter", "safety", "recitation", "refusal"}:
        return "refusal"

    return "end_turn"


def _responses_completion_status(finish_reason):
    reason = str(finish_reason or "").lower()
    if reason in {"length", "max_tokens", "max_output_tokens"}:
        return "incomplete", {"reason": "max_output_tokens"}
    if reason in {"content_filter", "safety", "recitation"}:
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


async def _iter_sse_json_events(body_iterator) -> AsyncIterator[Dict[str, Any]]:
    """Decode UTF-8 and frame SSE events independently of transport chunks."""
    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder("utf-8-sig")(), translate=True
    )
    buffer = ""
    finished = False

    def feed(text):
        nonlocal buffer, finished
        buffer += text
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            if any(line.startswith("data:") and line[5:].strip() == "[DONE]" for line in event.splitlines()):
                finished = True
                return
            if len(event) > 4 * 1024 * 1024:
                raise ValueError("SSE event exceeds the size limit")
            data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
            if data:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError("SSE data must be an object")
                yield parsed
        if len(buffer) > 4 * 1024 * 1024:
            raise ValueError("SSE event exceeds the size limit")

    try:
        async for chunk in body_iterator:
            encoded = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            for event in feed(decoder.decode(encoded)):
                yield event
            if finished:
                return
        for event in feed(decoder.decode(b"", final=True)):
            yield event
        # An unfinished event at EOF is discarded, as required by SSE framing.
    finally:
        close = getattr(body_iterator, "aclose", None)
        if close is not None:
            await close()


async def _iter_chat_stream_events(body_iterator):
    """Emit one terminal choice after trailing usage; never turn a broken stream into success.

    Chat Completions may send a choices=[] usage chunk after finish_reason.
    Both protocol adapters consume this normalized lifecycle.
    """
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    finish_reason = None
    tools = ToolDeltaNormalizer()
    try:
        async with aclosing(_iter_sse_json_events(body_iterator)) as events:
            async for parsed in events:
                if parsed.get("error"):
                    yield parsed
                    return
                usage = _merge_stream_usage(usage, parsed.get("usage") or {})
                choices = parsed.get("choices") or []
                if choices:
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = dict(choice.get("delta") or {})
                    for field in ("content", "reasoning_content"):
                        if delta.get(field) is not None and not isinstance(delta[field], str):
                            raise ValueError(f"Invalid {field} delta")
                    extra = delta.get("extra_content")
                    if extra is not None and (not isinstance(extra, dict) or
                                              not isinstance(extra.get("google", {}), dict)):
                        raise ValueError("Invalid reasoning metadata")
                    # SDKs may explicitly serialize an absent optional tool array as null.
                    if delta.get("tool_calls") is not None:
                        delta["tool_calls"] = tools.feed(delta["tool_calls"])
                    yield {**parsed, "choices": [{**choice, "delta": delta, "finish_reason": None}]}
                else:
                    yield parsed
            pending = tools.finish()
            if pending:
                yield {"choices": [{"delta": {"tool_calls": pending}, "finish_reason": None}]}
    except Exception:
        yield {"error": {"message": "Upstream stream interrupted.", "type": "server_error", "code": "stream_error"}}
        return
    if finish_reason is None:
        yield {"error": {"message": "Upstream stream ended without a finish reason.", "type": "server_error", "code": "incomplete_stream"}}
        return
    yield {"choices": [{"delta": {}, "finish_reason": finish_reason}], "usage": usage}
