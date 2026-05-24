from typing import Any, AsyncIterator, Dict, List

from app.utils.protocol_common import (
    _ensure_list,
    _merge_stream_usage,
    _now_ts,
    _openai_finish_reason_to_claude_stop_reason,
    _parse_sse_json_events,
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
    sequence_number = 0

    def response_event(payload: Dict[str, Any]) -> str:
        nonlocal sequence_number
        sequence_number += 1
        return _sse_data({**payload, "sequence_number": sequence_number})

    yield response_event(
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
    yield response_event(
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
            "error": None,
            "incomplete_details": None,
            "output": output_items,
            "usage": latest_usage,
        }

    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        for parsed in _parse_sse_json_events(chunk):
            choice = parsed.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            text = delta.get("content")
            if text:
                if text_output_index is None:
                    text_output_index = len(output_items)
                    output_items.append(build_message_item())
                    yield response_event(
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": text_output_index,
                            "item": output_items[text_output_index],
                        }
                    )
                    yield response_event(
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
                yield response_event(
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
                        "added": False,
                        "done": False,
                    }
                    active_tool_calls[tool_index] = state
                    output_items.append(item)

                item = state["item"]
                if tool_call.get("id"):
                    item["id"] = tool_call["id"]
                    item["call_id"] = tool_call["id"]
                if function_data.get("name"):
                    item["name"] = function_data["name"]

                arguments_delta = function_data.get("arguments")
                if not state["added"] and (item.get("name") or arguments_delta is not None):
                    yield response_event(
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": state["output_index"],
                            "item": item,
                        }
                    )
                    state["added"] = True

                if arguments_delta is not None:
                    state["arguments_parts"].append(arguments_delta)
                    item["arguments"] = "".join(state["arguments_parts"])
                    output_items[state["output_index"]] = item
                    yield response_event(
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

            if not choice.get("finish_reason"):
                continue
            if text_output_index is None and not output_items:
                text_output_index = len(output_items)
                output_items.append(build_message_item())
                yield response_event(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": text_output_index,
                        "item": output_items[text_output_index],
                    }
                )
                yield response_event(
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
                yield response_event(
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
                yield response_event(
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
                yield response_event(
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
                if not state["added"]:
                    yield response_event(
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": state["output_index"],
                            "item": item,
                        }
                    )
                    state["added"] = True
                item["arguments"] = "".join(state["arguments_parts"])
                yield response_event(
                    {
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "name": item.get("name"),
                        "output_index": state["output_index"],
                        "arguments": item["arguments"],
                    }
                )
                item["status"] = "completed"
                output_items[state["output_index"]] = item
                state["done"] = True
                yield response_event(
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": state["output_index"],
                        "item": item,
                    }
                )

            yield response_event(
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
    text_stopped = False
    next_content_index = 0
    text_index = None
    thinking_index = None
    active_tool_calls: Dict[int, Dict[str, Any]] = {}

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

    def build_tool_start_event(state: Dict[str, Any]) -> str:
        state["started"] = True
        content_block = {
            "type": "tool_use",
            "id": state["id"],
            "name": state["name"] or "tool",
            "input": {},
        }
        if state.get("thought_signature"):
            content_block["thought_signature"] = state["thought_signature"]
        return sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": state["index"],
                "content_block": content_block,
            },
        )

    async for raw_chunk in body_iterator:
        chunk = raw_chunk.decode("utf-8") if isinstance(raw_chunk, bytes) else raw_chunk
        for parsed in _parse_sse_json_events(chunk):
            choice = parsed.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            reasoning_text = delta.get("reasoning_content")
            if reasoning_text:
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

            text = delta.get("content")
            if text:
                stop_event = stop_thinking_event()
                if stop_event:
                    yield stop_event
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

            for tool_call in _ensure_list(delta.get("tool_calls")):
                if not isinstance(tool_call, dict):
                    continue
                tool_index = int(tool_call.get("index", len(active_tool_calls)) or 0)
                function_data = tool_call.get("function", {}) or {}
                state = active_tool_calls.get(tool_index)
                if state is None:
                    stop_event = stop_thinking_event()
                    if stop_event:
                        yield stop_event
                    stop_event = stop_text_event()
                    if stop_event:
                        yield stop_event
                    content_index = next_index()
                    tool_id = tool_call.get("id") or f"toolu_{_now_ts()}_{tool_index}"
                    extra_content = tool_call.get("extra_content") or {}
                    google_extra = extra_content.get("google", {})
                    state = {
                        "index": content_index,
                        "id": tool_id,
                        "name": function_data.get("name") or "",
                        "input_parts": [],
                        "thought_signature": google_extra.get("thought_signature")
                        or google_extra.get("thoughtSignature"),
                        "started": False,
                        "stopped": False,
                    }
                    active_tool_calls[tool_index] = state

                if tool_call.get("id"):
                    state["id"] = tool_call["id"]
                if function_data.get("name"):
                    state["name"] = function_data["name"]
                extra_content = tool_call.get("extra_content") or {}
                google_extra = extra_content.get("google", {})
                thought_signature = google_extra.get("thought_signature") or google_extra.get(
                    "thoughtSignature"
                )
                if thought_signature:
                    state["thought_signature"] = thought_signature
                if not state["started"] and state["name"]:
                    yield build_tool_start_event(state)

                arguments_delta = function_data.get("arguments")
                if arguments_delta is not None:
                    state["input_parts"].append(arguments_delta)
                    if not state["started"]:
                        yield build_tool_start_event(state)
                    yield sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": state["index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": arguments_delta,
                            },
                        },
                    )

            usage = parsed.get("usage", {})
            if usage:
                latest_usage = _merge_stream_usage(latest_usage, usage)

            if not choice.get("finish_reason"):
                continue
            stop_reason = _openai_finish_reason_to_claude_stop_reason(
                choice.get("finish_reason")
            )

            if text_started:
                stop_event = stop_text_event()
                if stop_event:
                    yield stop_event
            else:
                stop_event = stop_thinking_event()
                if stop_event:
                    yield stop_event

            for state in active_tool_calls.values():
                if not state.get("started"):
                    yield build_tool_start_event(state)
                if not state.get("stopped"):
                    yield sse_event(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": state["index"]},
                    )
                    state["stopped"] = True

            if not text_started and not thinking_started and not active_tool_calls:
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
