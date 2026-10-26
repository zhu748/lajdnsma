"""Protocol boundary regressions with actual SDK models, isolated from legacy stubs."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.utils.protocol_streams import openai_stream_to_responses_stream, openai_stream_to_claude_stream


async def source(deltas):
    for delta in deltas:
        if isinstance(delta, str):
            yield delta
        else:
            yield 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}) + '\n\n'
    yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    yield 'data: [DONE]\n\n'


async def events(converter, deltas):
    frames = [frame async for frame in converter(source(deltas), 'test')]
    return [json.loads(line[6:]) for frame in frames for line in frame.splitlines() if line.startswith('data: ')]


async def interleaved():
    deltas = [{'reasoning_content': 'first'}, {'content': 'answer'}, {'reasoning_content': 'second'}, {'content': 'tail'}]
    anthropic = await events(openai_stream_to_claude_stream, deltas)
    opened, closed = {}, set()
    for event in anthropic:
        if event['type'] == 'content_block_start':
            assert event['index'] not in opened
            opened[event['index']] = event['content_block']['type']
        elif event['type'] == 'content_block_delta':
            assert event['index'] in opened and event['index'] not in closed
        elif event['type'] == 'content_block_stop':
            assert event['index'] not in closed
            closed.add(event['index'])
    assert closed == set(opened)
    assert list(opened.values()) == ['thinking', 'text', 'thinking', 'text']
    responses = await events(openai_stream_to_responses_stream, deltas)
    from openai.types.responses import ResponseReasoningSummaryTextDeltaEvent
    summaries = [e for e in responses if e['type'] == 'response.reasoning_summary_text.delta']
    assert [ResponseReasoningSummaryTextDeltaEvent.model_validate(e).delta for e in summaries] == ['first', 'second']
    output = responses[-1]['response']['output']
    assert [item['type'] for item in output] == ['reasoning', 'message', 'reasoning', 'message']
    assert output[0]['summary'][0]['text'] == 'first'
    assert len({item['id'] for item in output}) == 4
    from app.utils.protocol_nonstream import openai_chat_to_response_api
    nonstream = openai_chat_to_response_api({'choices': [{'message': {'reasoning_content': 'first', 'content': 'answer'}}]})
    assert nonstream['output'][0]['summary'][0]['text'] == 'first'


async def fragmented_tools():
    deltas = [
        {'tool_calls': [{'index': 0, 'function': {'arguments': '{"q":'}}]},
        {'tool_calls': [{'index': 0, 'id': 'call_real', 'function': {'name': 'look'}}]},
        {'tool_calls': [{'index': 0, 'function': {'name': 'up', 'arguments': '"x"}'}}]},
    ]
    # Name fragments arrive before the second arguments fragment; do not open
    # the tool until that name has finished arriving.
    for converter in [openai_stream_to_responses_stream, openai_stream_to_claude_stream]:
        result = await events(converter, deltas)
        if converter is openai_stream_to_responses_stream:
            tool = result[-1]['response']['output'][0]
            assert tool['call_id'] == 'call_real' and tool['id'] != tool['call_id']
            assert tool['name'] == 'lookup'
            assert json.loads(tool['arguments']) == {'q': 'x'}
            item_ids = {e['item_id'] for e in result if 'item_id' in e}
            assert item_ids == {tool['id']}
        else:
            tool = next(e['content_block'] for e in result if e['type'] == 'content_block_start')
            assert tool['id'] == 'call_real' and tool['name'] == 'lookup'
            args = ''.join(e['delta']['partial_json'] for e in result if e['type'] == 'content_block_delta')
            assert json.loads(args) == {'q': 'x'}


async def malformed():
    for converter in [openai_stream_to_responses_stream, openai_stream_to_claude_stream]:
        for deltas in [['data: {broken}\n\n'], ['data: []\n\n'],
                       [{'content': ['invalid']}], [{'reasoning_content': {'invalid': True}}],
                       [{'extra_content': {'google': []}}],
                       [{'tool_calls': [{'index': -1, 'function': {}}]}],
                       [{'tool_calls': [{'index': 0, 'extra_content': {'google': []}}]}],
                       [{'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'f', 'arguments': '{}'}}]}, {'tool_calls': [{'index': 0, 'id': 'b'}]}],
                       [{'tool_calls': [{'index': 0, 'function': {'arguments': '{}'}}]}]]:
            result = await events(converter, deltas)
            assert result[-1]['type'] in {'response.failed', 'error'}
            assert not any(e['type'] in {'response.completed', 'message_stop'} for e in result)


async def optional_fields():
    deltas = [
        {'role': 'assistant', 'content': None, 'tool_calls': None},
        {'reasoning_content': 'thinking'},
        {'extra_content': {'google': {'thought_signature': 'opaque-signature'}}},
        {'content': 'answer', 'tool_calls': None},
    ]
    result = await events(openai_stream_to_claude_stream, deltas)
    signature = next(e for e in result if e.get('delta', {}).get('type') == 'signature_delta')
    assert signature['delta']['signature'] == 'opaque-signature'
    assert result[-1]['type'] == 'message_stop'
    result = await events(openai_stream_to_responses_stream, deltas)
    assert result[-1]['type'] == 'response.completed'
    assert result[-1]['response']['output'][1]['content'][0]['text'] == 'answer'


async def parallel_history():
    from app.utils.protocol_requests import response_request_to_chat_request
    from app.services.gemini import GeminiClient
    from app.vertex.message_processing import create_gemini_prompt
    from app.api.vertex_request_adapter import build_vertex_openai_request
    from app.config import settings
    settings.RANDOM_STRING = False
    request = response_request_to_chat_request({'model': 'gemini-2.5-flash', 'input': [
        {'role': 'assistant', 'content': 'Checking both'},
        *[{'type': 'function_call', 'call_id': key, 'name': 'lookup', 'arguments': {'q': key},
           'extra_content': {'google': {'thought_signature': 'c2ln'}}} for key in ['a', 'b']],
        *[{'type': 'function_call_output', 'call_id': key, 'output': 'result-' + key} for key in ['b', 'a']],
    ]})
    assert [m['role'] for m in request.messages] == ['assistant', 'tool', 'tool']
    native, _ = GeminiClient('local-test').convert_messages(request.messages)
    assert [p['functionCall']['id'] for p in native[0]['parts'] if 'functionCall' in p] == ['a', 'b']
    assert [p['functionResponse']['id'] for p in native[1]['parts']] == ['b', 'a']
    vertex = create_gemini_prompt(build_vertex_openai_request(request).messages)
    assert len(vertex) == 2
    assert [p.function_call.id for p in vertex[0].parts if p.function_call] == ['a', 'b']
    assert [p.function_response.id for p in vertex[1].parts] == ['b', 'a']
    assert vertex[0].parts[1].thought_signature == b'sig'


async def chat_input():
    from app.api import chat_routes
    from app.models.schemas import ChatCompletionRequest
    from app.utils.chat_requests import normalize_chat_messages
    from app.services.gemini import GeminiClient
    from fastapi import HTTPException
    called = False
    async def dispatch(**kwargs):
        nonlocal called
        called = True
        return {}
    chat_routes.handle_aistudio_chat_completion = dispatch
    for handler in [chat_routes.aistudio_chat_completions, chat_routes.vertex_chat_completions]:
        for messages in [
            [{'role': 'user', 'content': [7]}],
            [{'role': 'user', 'content': 7}],
            [{'role': [], 'content': 'x'}],
            [{'role': 'tool', 'tool_call_id': 'unknown', 'content': 'result'}],
            [{'role': 'assistant', 'tool_calls': [{'id': 'a', 'function': {'name': 'f', 'arguments': '{bad}'}}]}],
        ]:
            try:
                await handler(ChatCompletionRequest(model='test', messages=messages), None)
            except HTTPException as exc:
                assert exc.status_code == 400
            else:
                raise AssertionError('Invalid Chat input reached backend dispatch')
    assert not called
    original = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a', 'type': 'function', 'function': {'name': 'f', 'arguments': {}}}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': [{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]},
    ]
    messages = normalize_chat_messages(original)
    assert isinstance(original[1]['content'], list)  # do not mutate caller-owned blocks
    assert messages[1]['content'] == 'one\ntwo' and messages[1]['name'] == 'f'
    history, _ = GeminiClient('local-test').convert_messages(messages)
    assert history[-1]['parts'][0]['functionResponse']['response']['content'] == 'one\ntwo'


asyncio.run(globals()[sys.argv[1]]())
