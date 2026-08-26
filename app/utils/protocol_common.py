import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.utils.sse import sse_data
from app.utils.stealth import (
    gen_openai_response_id,
    gen_openai_message_id,
    gen_responses_function_call_id,
    gen_anthropic_message_id,
    gen_anthropic_tool_use_id,
    gen_anthropic_thinking_signature,
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
_gen_anthropic_thinking_signature = gen_anthropic_thinking_signature


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
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
    }


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


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _sse_data(payload: Dict[str, Any]) -> str:
    return sse_data(payload)


def _parse_sse_json_events(chunk: str) -> List[Dict[str, Any]]:
    """Parse one transport chunk that may contain one or more SSE events."""
    events: List[Dict[str, Any]] = []
    data_lines: List[str] = []

    def flush_event() -> None:
        if not data_lines:
            return

        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload or payload == "[DONE]":
            return

        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            return

    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            flush_event()
            continue
        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    flush_event()
    return events


def _parse_sse_json(chunk: str) -> Optional[Dict[str, Any]]:
    """Parse SSE chunk and return the first JSON payload, if present."""
    events = _parse_sse_json_events(chunk)
    return events[0] if events else None
