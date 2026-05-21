import json
import time
from typing import Any, Dict, List, Optional

from app.utils.sse import sse_data


def _now_ts() -> int:
    return int(time.time())


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


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _sse_data(payload: Dict[str, Any]) -> str:
    return sse_data(payload)


def _parse_sse_json(chunk: str) -> Optional[Dict[str, Any]]:
    """Parse SSE chunk and return JSON payload from one or more data lines."""
    data_lines: List[str] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    if not data_lines:
        return None

    payload = "\n".join(data_lines).strip()
    if not payload or payload == "[DONE]":
        return None

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None

