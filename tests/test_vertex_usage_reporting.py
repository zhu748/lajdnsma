"""Vertex 真/伪流式用量与仅思考响应的真实模块回归。"""
from pathlib import Path
import subprocess
import sys


def test_vertex_fake_streams_preserve_usage_for_reasoning_only_outputs():
    code = r'''
import asyncio, json
from types import SimpleNamespace
from app.vertex.api_helpers import _base_fake_stream_engine
from app.vertex.routes.openai_direct import _openai_fake_stream_engine, openai_true_stream_generator

def payloads(chunks):
    return [json.loads(line[5:]) for chunk in chunks for line in chunk.splitlines() if line.startswith("data:") and line[5:].strip() != "[DONE]"]

async def run():
    raw = SimpleNamespace(
        usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=0, thoughts_token_count=3, total_token_count=13),
        candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
    )
    chunks = [chunk async for chunk in _base_fake_stream_engine(
        api_call_task_creator=lambda: asyncio.create_task(asyncio.sleep(0, result=raw)),
        extract_text_from_response_func=lambda value: "", response_id="id", sse_model_name="model",
        is_auto_attempt=False, is_valid_response_func=lambda value: True,
        keep_alive_interval_seconds=0, reasoning_text_to_yield="思考", actual_content_text_to_yield="",
    )]
    last = payloads(chunks)[-1]
    assert last["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
    assert last["choices"][0]["finish_reason"] == "length"
    assert chunks[-1] == "data: [DONE]\n\n"

    usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13, "completion_tokens_details": {"reasoning_tokens": 3}}
    raw = SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason="length")])
    chunks = [chunk async for chunk in _openai_fake_stream_engine(
        api_call_task_creator=lambda: asyncio.create_task(asyncio.sleep(0, result=raw)),
        extract_text_from_response_func=lambda value: "", is_valid_response_func=lambda value: True,
        response_id="id", sse_model_name="model", reasoning_text_to_yield="思考", actual_content_text_to_yield="",
    )]
    last = payloads(chunks)[-1]
    assert last["usage"] == usage
    assert last["choices"][0]["finish_reason"] == "length"

    class Completions:
        async def create(self, **kwargs):
            assert kwargs["stream_options"]["include_usage"] is True
            async def stream():
                yield SimpleNamespace(model_dump=lambda **kwargs: {"choices": [], "usage": usage})
            return stream()
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    chunks = [chunk async for chunk in openai_true_stream_generator(client, {}, None, "model")]
    assert payloads(chunks)[0]["usage"] == usage

asyncio.run(run())
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
