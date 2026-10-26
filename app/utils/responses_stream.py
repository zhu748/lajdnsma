"""Responses SSE: stable output identities and shared text/summary lifecycles."""
from contextlib import aclosing
from uuid import uuid4

from app.utils.protocol_common import (
    _iter_chat_stream_events, _merge_stream_usage, _now_ts,
    _gen_response_id, _gen_message_id, _gen_function_call_id,
    _responses_completion_status,
)
from app.utils.sse import sse_event


async def openai_stream_to_responses_stream(body_iterator, model):
    response_id, created_at = _gen_response_id(), _now_ts()
    sequence = 0
    outputs, tools = [], {}
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def event(kind, **payload):
        nonlocal sequence
        sequence += 1
        return sse_event(kind, {"type": kind, **payload, "sequence_number": sequence})

    def item_value(state, status="in_progress"):
        item = {**state["item"], "status": status}
        text = "".join(state["parts"])
        if state["kind"] == "function_call":
            item["arguments"] = text
        elif state["kind"] == "reasoning":
            item["summary"] = [{"type": "summary_text", "text": text}]
        else:
            item["content"] = [{"type": "output_text", "text": text, "annotations": []}]
        return item

    def response(status, details=None, error=None):
        return {
            "id": response_id, "object": "response", "created_at": created_at,
            "model": model, "status": status, "error": error,
            "incomplete_details": details, "usage": usage,
            "output": [item_value(s, "incomplete" if status == "failed" else status) for s in outputs],
        }

    def location(state):
        return {"response_id": response_id, "item_id": state["item"]["id"], "output_index": state["index"]}

    def add_text(kind):
        reasoning = kind == "reasoning"
        state = {"kind": kind, "index": len(outputs), "parts": [], "item": {
            "id": "rs_" + uuid4().hex if reasoning else _gen_message_id(), "type": kind,
        }}
        state["item"].update({"summary": []} if reasoning else {"role": "assistant", "content": []})
        outputs.append(state)
        return state

    def start_text(state):
        reasoning = state["kind"] == "reasoning"
        yield event("response.output_item.added", response_id=response_id,
                    output_index=state["index"], item={**state["item"], "status": "in_progress"})
        yield event("response.reasoning_summary_part.added" if reasoning else "response.content_part.added",
                    **location(state), **({"summary_index": 0} if reasoning else {"content_index": 0}),
                    part={"type": "summary_text", "text": ""} if reasoning else {"type": "output_text", "text": "", "annotations": []})

    yield event("response.created", response=response("in_progress"))
    yield event("response.in_progress", response=response("in_progress"))
    async with aclosing(_iter_chat_stream_events(body_iterator)) as events:
        async for parsed in events:
            if parsed.get("error"):
                yield event("response.failed", response=response("failed", error=parsed["error"]))
                return
            usage = _merge_stream_usage(usage, parsed.get("usage") or {})
            choice = (parsed.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            for field, kind, event_name in (("reasoning_content", "reasoning", "reasoning_summary_text"),
                                             ("content", "message", "output_text")):
                text = delta.get(field)
                if not text:
                    continue
                state = outputs[-1] if outputs and outputs[-1]["kind"] == kind else None
                if state is None:
                    state = add_text(kind)
                    for frame in start_text(state):
                        yield frame
                state["parts"].append(text)
                yield event(f"response.{event_name}.delta", **location(state),
                            **({"summary_index": 0} if kind == "reasoning" else {"content_index": 0}), delta=text)

            for call in delta.get("tool_calls") or []:
                index, function = call["index"], call.get("function") or {}
                state = tools.get(index)
                if state is None:
                    state = {"kind": "function_call", "index": len(outputs), "parts": [], "item": {
                        "id": _gen_function_call_id(), "type": "function_call",
                        "call_id": call["id"], "name": function["name"], "arguments": "",
                    }}
                    if call.get("extra_content"):
                        state["item"]["extra_content"] = call["extra_content"]
                    tools[index] = state
                    outputs.append(state)
                    yield event("response.output_item.added", response_id=response_id,
                                output_index=state["index"], item=item_value(state))
                if call.get("extra_content"):
                    state["item"]["extra_content"] = call["extra_content"]
                arguments = function.get("arguments")
                if arguments is not None:
                    state["parts"].append(arguments)
                    yield event("response.function_call_arguments.delta", **location(state), delta=arguments)

            if not choice.get("finish_reason"):
                continue
            status, details = _responses_completion_status(choice["finish_reason"])
            if not outputs:
                state = add_text("message")
                for frame in start_text(state):
                    yield frame
            for state in outputs:
                item, text = item_value(state, status), "".join(state["parts"])
                if state["kind"] == "function_call":
                    yield event("response.function_call_arguments.done", **location(state),
                                name=item["name"], arguments=text)
                else:
                    reasoning = state["kind"] == "reasoning"
                    index = {"summary_index": 0} if reasoning else {"content_index": 0}
                    yield event("response.reasoning_summary_text.done" if reasoning else "response.output_text.done",
                                **location(state), **index, text=text)
                    yield event("response.reasoning_summary_part.done" if reasoning else "response.content_part.done",
                                **location(state), **index, part=item["summary" if reasoning else "content"][0])
                yield event("response.output_item.done", response_id=response_id, output_index=state["index"], item=item)
            yield event(f"response.{status}", response=response(status, details), usage=usage)
