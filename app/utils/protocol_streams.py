from typing import Any, AsyncIterator, Dict, List

from app.utils.protocol_common import (
    _ensure_list,
    _merge_stream_usage,
    _now_ts,
    _parse_sse_json,
    _sse_data,
)
from app.utils.sse import sse_event


async def openai_stream_to_responses_stream(
    body_iterator: AsyncIterator[Any],
    model: str,
) -> AsyncIterator[str]:
    created_at = _now_ts()
    response_id = f"resp_{created_at}"
    message_item_id = f"msg_{created_at}"
    yield _sse_data(
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "model": model,
                "object": "response",
                "status": "in_progress",
                "created_at": created_at,
                "output": [],
            },
        }
    )
    yield _sse_data(
        {
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "model": model,
                "object": "response",
                "status": "in_progress",
                "created_at": created_at,
                "output": [],
            },
        }
    )

    content_index = 0
    text_output_index = None
    output_text_parts: List[str] = []
    output_items: List[Dict[str, Any]] = []
    active_tool_calls: Dict[int, Dict[str, Any]] = {}
    latest_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def build_message_item(status: str = "in_progress") -> Dict[str, Any]:
        return {
            "id": message_item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "".join(output_text_parts),
                    "annotations": [],
                }
            ],
        }

    def build_completed_response() -> Dict[str, Any]:
        return {
            "id": response_id,
            "model": model,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "output": output_items,
            "usage": latest_usage,
        }

    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        parsed = _parse_sse_json(chunk)
        if not parsed:
            continue

        choice = parsed.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        text = delta.get("content")
        if text:
            if text_output_index is None:
                text_output_index = len(output_items)
                output_items.append(build_message_item())
                yield _sse_data(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": text_output_index,
                        "item": output_items[text_output_index],
                    }
                )
                yield _sse_data(
                    {
                        "type": "response.content_part.added",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": content_index,
                        "part": {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                        },
                    }
                )
            output_text_parts.append(text)
            output_items[text_output_index] = build_message_item()
            yield _sse_data(
                {
                    "type": "response.output_text.delta",
                    "response_id": response_id,
                    "item_id": message_item_id,
                    "output_index": text_output_index,
                    "content_index": content_index,
                    "delta": text,
                }
            )

        for tool_call in _ensure_list(delta.get("tool_calls")):
            if not isinstance(tool_call, dict):
                continue
            tool_index = int(tool_call.get("index", len(active_tool_calls)) or 0)
            function_data = tool_call.get("function", {}) or {}
            state = active_tool_calls.get(tool_index)
            if state is None:
                call_id = tool_call.get("id") or f"fc_{created_at}_{tool_index}"
                item = {
                    "id": call_id,
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function_data.get("name"),
                    "arguments": "",
                    "status": "in_progress",
                }
                state = {
                    "output_index": len(output_items),
                    "item": item,
                    "arguments_parts": [],
                    "done": False,
                }
                active_tool_calls[tool_index] = state
                output_items.append(item)
                yield _sse_data(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": state["output_index"],
                        "item": item,
                    }
                )

            item = state["item"]
            if tool_call.get("id"):
                item["id"] = tool_call["id"]
                item["call_id"] = tool_call["id"]
            if function_data.get("name"):
                item["name"] = function_data["name"]

            arguments_delta = function_data.get("arguments")
            if arguments_delta:
                state["arguments_parts"].append(arguments_delta)
                item["arguments"] = "".join(state["arguments_parts"])
                output_items[state["output_index"]] = item
                yield _sse_data(
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": state["output_index"],
                        "delta": arguments_delta,
                    }
                )

        usage = parsed.get("usage", {})
        latest_usage = _merge_stream_usage(latest_usage, usage)

        if choice.get("finish_reason"):
            if text_output_index is None and not output_items:
                text_output_index = len(output_items)
                output_items.append(build_message_item())
                yield _sse_data(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": text_output_index,
                        "item": output_items[text_output_index],
                    }
                )
                yield _sse_data(
                    {
                        "type": "response.content_part.added",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": content_index,
                        "part": {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                        },
                    }
                )

            if text_output_index is not None:
                done_text = "".join(output_text_parts)
                yield _sse_data(
                    {
                        "type": "response.output_text.done",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": content_index,
                        "text": done_text,
                    }
                )
                done_part = {
                    "type": "output_text",
                    "text": done_text,
                    "annotations": [],
                }
                yield _sse_data(
                    {
                        "type": "response.content_part.done",
                        "response_id": response_id,
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": content_index,
                        "part": done_part,
                    }
                )
                done_item = build_message_item(status="completed")
                output_items[text_output_index] = done_item
                yield _sse_data(
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": text_output_index,
                        "item": done_item,
                    }
                )

            for state in active_tool_calls.values():
                if state.get("done"):
                    continue
                item = state["item"]
                item["arguments"] = "".join(state["arguments_parts"])
                yield _sse_data(
                    {
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": state["output_index"],
                        "arguments": item["arguments"],
                    }
                )
                item["status"] = "completed"
                output_items[state["output_index"]] = item
                state["done"] = True
                yield _sse_data(
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": state["output_index"],
                        "item": item,
                    }
                )

            yield _sse_data(
                {
                    "type": "response.completed",
                    "response": build_completed_response(),
                    "usage": latest_usage,
                }
            )


async def openai_stream_to_claude_stream(
    body_iterator: AsyncIterator[Any],
    model: str,
) -> AsyncIterator[str]:
    message_id = f"msg_{_now_ts()}"
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

    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        parsed = _parse_sse_json(chunk)
        if not parsed:
            continue

        choice = parsed.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        reasoning_text = delta.get("reasoning_content")
        if reasoning_text:
            if not thinking_started:
                thinking_started = True
                yield sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
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
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": reasoning_text,
                    },
                },
            )

        text = delta.get("content")
        if text:
            text_index = 1 if thinking_started else 0
            if thinking_started and not thinking_stopped:
                yield sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                )
                thinking_stopped = True
            if not text_started:
                text_started = True
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

        usage = parsed.get("usage", {})
        if usage:
            latest_usage = _merge_stream_usage(latest_usage, usage)

        if choice.get("finish_reason"):
            stop_reason = "end_turn"
            if choice.get("finish_reason") == "tool_calls":
                stop_reason = "tool_use"

            if text_started:
                yield sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 1 if thinking_started else 0},
                )
            elif thinking_started and not thinking_stopped:
                yield sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                )
            else:
                yield sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                yield sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
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
