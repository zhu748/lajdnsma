import json
from typing import Any, AsyncIterator, Dict


def sse_data(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_text(data: str) -> str:
    return f"data: {data}\n\n"


def sse_done() -> str:
    return sse_text("[DONE]")


def sse_event(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\n{sse_data(payload)}"


async def iter_sse_json(response) -> AsyncIterator[Dict[str, Any]]:
    """Yield JSON payloads from an SSE response body.

    Supports `data:` and `data: ` lines, skips comments/events/blank lines, and
    handles JSON payloads split across multiple lines by buffering until valid.
    """
    buffer = b""
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()

        if line == "[DONE]":
            break

        buffer += line.encode("utf-8")
        try:
            data = json.loads(buffer.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        buffer = b""
        yield data
