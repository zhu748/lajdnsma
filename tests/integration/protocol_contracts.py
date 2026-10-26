"""Real dependency contracts, run in fresh processes to avoid legacy sys.modules stubs."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def request_contract():
    from app.models.schemas import ChatCompletionRequest
    from app.utils.protocol_requests import response_request_to_chat_request, claude_request_to_chat_request
    from app.services.gemini import GeminiClient
    from app.utils.cache import generate_cache_key
    from app.config import settings

    request = response_request_to_chat_request({
        'model': 'gemini-2.5-flash', 'max_output_tokens': 30,
        'input': [{'role': 'developer', 'content': 'rules'}, {'role': 'user', 'content': [
            {'type': 'input_text', 'text': ' before '},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,eA=='},
            {'type': 'input_text', 'text': '\n after '},
        ]}],
        'text': {'format': {'type': 'json_schema', 'name': 'reply', 'schema': {'type': 'object'}}},
    })
    assert request.messages[0]['role'] == 'system'
    assert [p['type'] for p in request.messages[1]['content']] == ['text', 'image_url', 'text']
    assert request.messages[1]['content'][0]['text'] == ' before '
    assert request.messages[1]['content'][-1]['text'] == '\n after '
    settings.ENABLE_THINKING = False
    _, data = GeminiClient('dummy')._convert_openAI_request(request, [], None, None)
    assert data['generationConfig']['maxOutputTokens'] == 30
    assert data['generationConfig']['responseJsonSchema'] == {'type': 'object'}
    assert data['generationConfig']['responseMimeType'] == 'application/json'
    assert generate_cache_key(request) != generate_cache_key(request.model_copy(update={'response_format': None}))
    chat = ChatCompletionRequest(model='test', messages=[], max_completion_tokens=17)
    assert chat.max_tokens == 17
    claude = claude_request_to_chat_request({'model': 'test', 'messages': [{'role': 'assistant', 'content': '  code\n'}]})
    assert claude.messages[0]['content'] == '  code\n'


def tool_roundtrip_contract():
    from app.services.gemini import GeminiClient, GeminiResponseWrapper
    from app.utils.response import openAI_from_Gemini
    from app.utils.protocol_nonstream import openai_chat_to_response_api
    from app.utils.protocol_requests import response_request_to_chat_request
    from app.config import settings
    settings.RANDOM_STRING = False
    source = {'candidates': [{'content': {'parts': [
        {'text': 'Checking now.'},
        {'functionCall': {'id': 'upstream-call-id', 'name': 'lookup', 'args': {'q': 'x'}}, 'thoughtSignature': 'real-signature'},
    ]}, 'finishReason': 'STOP'}]}
    wrapper = GeminiResponseWrapper(source)
    wrapper.set_model('gemini-2.5-flash')
    chat = openAI_from_Gemini(wrapper, stream=False)
    assert chat['choices'][0]['message']['content'] == 'Checking now.'
    assert chat['choices'][0]['message']['tool_calls'][0]['id'] == 'upstream-call-id'
    event = json.loads(openAI_from_Gemini(wrapper).split('data: ', 1)[1])
    assert event['choices'][0]['delta']['tool_calls'][0]['index'] == 0
    response = openai_chat_to_response_api(chat)
    call = next(item for item in response['output'] if item['type'] == 'function_call')
    request = response_request_to_chat_request({'model': 'test', 'input': [call, {
        'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'found',
    }]})
    history, _ = GeminiClient('dummy').convert_messages(request.messages)
    assert history[0]['parts'][0]['functionCall']['id'] == 'upstream-call-id'
    assert history[0]['parts'][0]['thoughtSignature'] == 'real-signature'
    assert history[1]['parts'][0]['functionResponse']['id'] == 'upstream-call-id'
    # A function-call chunk cannot finish a still-active Chat Completions stream.
    wrapper = GeminiResponseWrapper({'candidates': [{'content': {'parts': [
        {'functionCall': {'id': 'second-id', 'name': 'lookup', 'args': {}}},
    ]}}]})
    wrapper.set_model('test')
    event = json.loads(openAI_from_Gemini(wrapper, tool_index_offset=1).split('data: ', 1)[1])
    assert event['choices'][0]['finish_reason'] is None
    assert event['choices'][0]['delta']['tool_calls'][0]['index'] == 1
    final = GeminiResponseWrapper({'candidates': [{'finishReason': 'STOP'}]})
    final.set_model('test')
    event = json.loads(openAI_from_Gemini(final, has_tool_calls=True).split('data: ', 1)[1])
    assert event['choices'][0]['finish_reason'] == 'tool_calls'



async def stream_contract():
    from app.utils.protocol_streams import openai_stream_to_responses_stream, openai_stream_to_claude_stream
    converters = [openai_stream_to_responses_stream, openai_stream_to_claude_stream]
    async def source(events, fail=False):
        for event in events:
            yield 'data: ' + json.dumps(event) + '\n\n'
        if fail:
            raise RuntimeError('private upstream detail')
        yield 'data: [DONE]\n\n'
    def payloads(chunks):
        return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines() if line.startswith('data: ')]
    finish = {'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]}
    usage = {'choices': [], 'usage': {'prompt_tokens': 9, 'completion_tokens': 4, 'total_tokens': 13,
                                     'completion_tokens_details': {'reasoning_tokens': 2}}}
    for converter in converters:
        result = payloads([part async for part in converter(source([finish, usage]), 'test')])
        if converter is converters[0]:
            terminal = [part for part in result if part['type'] == 'response.completed']
            assert len(terminal) == 1
            assert terminal[0]['response']['usage']['output_tokens'] == 4
            assert terminal[0]['response']['usage']['output_tokens_details']['reasoning_tokens'] == 2
        else:
            terminal = [part for part in result if part['type'] == 'message_delta']
            assert terminal[0]['usage'] == {'input_tokens': 9, 'output_tokens': 4}
            assert result[-1]['type'] == 'message_stop'
        for events, fail in [([{'error': {'message': 'quota'}}], False),
                             ([finish, {'error': {'message': 'late failure'}}], False),
                             ([{'choices': [{'delta': {'content': 'partial'}}]}], False),
                             ([finish], True)]:
            result = payloads([part async for part in converter(source(events, fail), 'test')])
            assert not any(p['type'] in {'response.completed', 'message_stop'} for p in result)
            assert result[-1]['type'] in {'response.failed', 'error'}
            assert 'private upstream detail' not in str(result)


async def handler_contract():
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.background import BackgroundTask
    from app.api.protocol_handlers import handle_responses_request, handle_claude_messages_request
    marker = BackgroundTask(lambda: None)
    async def handler(*args):
        return JSONResponse({'error': {'message': 'quota', 'code': 429}}, status_code=429,
                            headers={'Retry-After': '3'}, background=marker)
    for adapter, payload in [(handle_responses_request, {'model': 'test', 'input': 'hi'}),
                             (handle_claude_messages_request, {'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]})]:
        response = await adapter(payload, None, None, None, handler)
        assert response.status_code == 429
        assert response.headers['retry-after'] == '3'
        assert response.background is marker
        assert json.loads(response.body)['error']['message'] == 'quota'
        async def source():
            yield '\n'
            yield '{"error":{"message":"failed","code":503}}'
        async def keepalive(*args):
            return StreamingResponse(source(), media_type='application/json')
        stream = await adapter(payload, None, None, None, keepalive)
        result = ''.join([part async for part in stream.body_iterator])
        assert json.loads(result)['error']['message'] == 'failed'
    response = await handle_responses_request({'model': 'test', 'input': 'hi', 'previous_response_id': 'old'}, None, None, None, handler)
    assert response.status_code == 400


async def body_limit_contract():
    from fastapi import FastAPI, Request
    from app.middleware import BodySizeLimitMiddleware, ResponseHeadersMiddleware
    endpoint = FastAPI()
    executed = []
    @endpoint.post('/')
    async def read(request: Request):
        data = await request.json()
        executed.append(data)
        return data
    app = ResponseHeadersMiddleware(BodySizeLimitMiddleware(endpoint, max_bytes=10))
    async def call(chunks, declared=None):
        messages = []
        pending = [{'type': 'http.request', 'body': chunk, 'more_body': i < len(chunks)-1} for i, chunk in enumerate(chunks)]
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
                 'scheme': 'http', 'path': '/', 'raw_path': b'/', 'query_string': b'',
                 'headers': [(b'content-type', b'application/json')] + ([(b'content-length', str(declared).encode())] if declared is not None else []),
                 'server': ('test', 80), 'client': ('test', 1)}
        async def receive():
            return pending.pop(0) if pending else {'type': 'http.disconnect'}
        async def send(message):
            messages.append(message)
        await app(scope, receive, send)
        return messages
    for chunks, declared in [([b'{"a":', b'"0123456789"}'], None), ([b'{"a":', b'"0123456789"}'], 1), ([b'{}'], 99)]:
        messages = await call(chunks, declared)
        assert messages[0]['status'] == 413, messages
        assert len([m for m in messages if m['type'] == 'http.response.start']) == 1
        assert dict(messages[0]['headers'])[b'server'] == b'cloudflare'
    assert not executed
    messages = await call([b'{"a":', b'1234}'])  # exactly ten bytes
    assert messages[0]['status'] == 200, messages


async def rpm_contract():
    from app.utils.stats import ApiStatsManager
    manager = ApiStatsManager(enable_background=False)
    for _ in range(6):
        manager.record_outbound_attempt('key')
        await manager.update_stats('key', 'model', 2)
    assert manager.get_calls_last_minute_for_key('key') == 6
    assert manager.get_calls_last_24h() == 6
    assert manager.get_tokens_last_24h() == 12
    manager.enable_background = True
    await manager.update_stats('stale', 'model', 10)
    detached = [manager._update_queue.get_nowait()]
    await manager.reset()
    await manager.update_stats('new', 'model', 3)
    manager._process_batch(detached)
    manager._process_batch([manager._update_queue.get_nowait()])
    assert manager.get_calls_last_24h() == 1
    assert manager.get_tokens_last_24h() == 3
    assert manager._update_queue.maxsize == 4096
    manager.shutdown()
    await manager.update_stats('after-shutdown', 'model', 2)
    assert manager.get_calls_last_24h() == 2


async def vertex_contract():
    from app.models.schemas import ChatCompletionRequest
    from app.api.vertex_request_adapter import build_vertex_openai_request
    from app.vertex.routes import chat_api
    request = ChatCompletionRequest(model='gemini-3-pro', messages=[
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-id', 'type': 'function', 'function': {'name': 'f', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-id', 'content': 'ok'},
    ], stop='END', tools=[{'type': 'function', 'function': {'name': 'f'}}])
    mapped = build_vertex_openai_request(request)
    assert mapped.stop == ['END']
    assert mapped.messages[0].tool_calls[0]['id'] == 'call-id'
    assert mapped.messages[1].tool_call_id == 'call-id'
    assert mapped.tools == request.tools
    from google.genai import types
    from app.vertex.api_helpers import create_generation_config, is_response_valid, execute_gemini_call
    from app.vertex.message_processing import create_gemini_prompt, convert_to_openai_format
    from app.config import settings
    import base64
    signature = base64.b64encode(b"real-signature-bytes").decode()
    mapped.messages[0].tool_calls[0]["extra_content"] = {"google": {"thought_signature": signature}}
    mapped.messages.insert(0, type(mapped.messages[0])(role="system", content="Keep tools intact"))
    config = create_generation_config(mapped)
    types.GenerateContentConfig.model_validate(config)
    history = create_gemini_prompt(mapped.messages[1:])
    assert history[0].parts[0].function_call.id == "call-id"
    assert history[0].parts[0].thought_signature == b"real-signature-bytes"
    assert history[1].parts[0].function_response.name == "f"
    assert history[1].parts[0].function_response.id == "call-id"
    response = types.GenerateContentResponse.model_validate({
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"id": "upstream-1", "name": "f", "args": {"q": "中"}}, "thoughtSignature": signature},
            {"functionCall": {"id": "upstream-2", "name": "f", "args": {}}},
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
    })
    assert is_response_valid(response)
    chat = convert_to_openai_format(response, mapped.model)
    calls = chat['choices'][0]['message']['tool_calls']
    assert chat['choices'][0]['finish_reason'] == 'tool_calls'
    assert calls[0]['extra_content']['google']['thought_signature'] == signature
    assert json.loads(calls[0]['function']['arguments']) == {'q': '中'}
    received = []
    async def generate(**kwargs):
        received.append(kwargs)
        return response
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    result = await execute_gemini_call(client, mapped.model, create_gemini_prompt, config, mapped)
    assert result.status_code == 200
    assert received[0]['config']['system_instruction'].parts[0].text == 'Keep tools intact'
    assert all(c.role != 'system' for c in received[0]['contents'])
    mapped.stream = True
    settings.FAKE_STREAMING = True
    import app.vertex.config as vertex_config
    vertex_config.FAKE_STREAMING_INTERVAL_SECONDS = 0
    result = await execute_gemini_call(client, mapped.model, create_gemini_prompt, config, mapped)
    frames = [frame async for frame in result.body_iterator]
    events = [json.loads(f[6:]) for f in frames if f.startswith('data: ') and '[DONE]' not in f]
    assert events[-1]['usage']['total_tokens'] == 8
    assert events[-1]['choices'][0]['finish_reason'] == 'tool_calls'
    tool_event = next(e for e in events if e['choices'][0]['delta'].get('tool_calls'))
    assert [t['index'] for t in tool_event['choices'][0]['delta']['tool_calls']] == [0, 1]
    assert len({e['id'] for e in events}) == 1
    assert len({e['created'] for e in events}) == 1

    # Native streaming: multiple call chunks, a separate finish and trailing usage.
    settings.FAKE_STREAMING = False
    closed = []
    async def chunks(truncated=False):
        try:
            for candidate_part in response.candidates[0].content.parts:
                yield types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[candidate_part]))])
            if not truncated:
                yield types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="STOP")])
                yield types.GenerateContentResponse(usage_metadata=response.usage_metadata)
        finally:
            closed.append(True)
    async def stream(**kwargs):
        return chunks()
    client.aio.models.generate_content_stream = stream
    result = await execute_gemini_call(client, mapped.model, create_gemini_prompt, config, mapped)
    frames = [f async for f in result.body_iterator]
    events = [json.loads(f[6:]) for f in frames if f.startswith('data: ') and '[DONE]' not in f]
    assert closed
    assert [e['choices'][0]['delta']['tool_calls'][0]['index'] for e in events if e['choices'][0]['delta'].get('tool_calls')] == [0, 1]
    assert events[-1]['choices'][0]['finish_reason'] == 'tool_calls'
    assert any(e.get('usage', {}).get('total_tokens') == 8 for e in events)
    async def truncated(**kwargs):
        return chunks(True)
    client.aio.models.generate_content_stream = truncated
    result = await execute_gemini_call(client, mapped.model, create_gemini_prompt, config, mapped, is_auto_attempt=True)
    frames = [f async for f in result.body_iterator]
    assert any('"error"' in f for f in frames)
    assert not any('"finish_reason": "stop"' in f for f in frames)
    # Closing downstream promptly closes the SDK stream.
    result = await execute_gemini_call(client, mapped.model, create_gemini_prompt, config, mapped)
    await anext(result.body_iterator)
    count = len(closed)
    await result.body_iterator.aclose()
    assert len(closed) == count + 1

    chat_api.get_credential_manager = lambda request: None
    mapped.n = 2
    rejected = await chat_api.chat_completions(None, mapped, 'test')
    assert rejected.status_code == 400


def error_contract():
    from app.services.gemini import GeminiResponseWrapper
    from app.utils.response import openAI_from_Gemini
    from app.utils.error_response import build_error_response
    from app.models.schemas import ChatCompletionResponse
    for reason, expected in [('STOP', 'stop'), ('MAX_TOKENS', 'length'), ('SAFETY', 'content_filter')]:
        wrapped = GeminiResponseWrapper({'candidates': [{'content': {'parts': []}, 'finishReason': reason}]})
        wrapped.set_model('test')
        response = openAI_from_Gemini(wrapped, stream=False)
        assert response['choices'][0]['finish_reason'] == expected
        ChatCompletionResponse.model_validate(response)
    for gemini in (True, False):
        response = build_error_response(gemini, 'test', 'failure', False)
        assert 'error' in response and 'choices' not in response and 'candidates' not in response


async def embedding_contract():
    import base64
    import struct
    from app.services import embedding
    from app.models.schemas import EmbeddingRequest
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"embeddings": [{"values": [1.0, -2.5]}]}
    class Client:
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    async def get_client():
        return Client()
    embedding.get_async_client = get_client
    result = await embedding.EmbeddingClient('dummy').create_embeddings(EmbeddingRequest(
        input='text', model='models/text-embedding-004', encoding_format='base64',
    ))
    assert struct.unpack('<2f', base64.b64decode(result.data[0].embedding)) == (1.0, -2.5)
    assert '/models/models/' not in calls[0][0]


async def http_contract():
    import os
    os.environ['ALLOWED_ORIGINS'] = 'https://client.example'
    import httpx
    from app.main import app
    from app.config import settings
    from app.api import chat_routes
    settings.PASSWORD = 'test'
    async def failed_handler(**kwargs):
        return {'error': {'message': 'upstream failed', 'code': '503'}}
    chat_routes.handle_aistudio_chat_completion = failed_handler
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        root = await client.get('/')
        assert root.status_code == 200
        assert '/assets/index-' in root.text
        assert (await client.get('/health')).json() == {'status': 'ok'}
        preflight = await client.options('/v1/messages', headers={
            'Origin': 'https://client.example', 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'x-api-key,anthropic-version',
        })
        assert preflight.status_code == 200, preflight.text
        response = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test'}, json={
            'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}],
        })
        assert response.status_code == 503, response.text
        assert response.json()['error']['message'] == 'upstream failed'
        assert response.headers['server'] == 'cloudflare'
        response = await client.post('/v1beta/models/test:typo', headers={'x-goog-api-key': 'test'}, json={'contents': []})
        assert response.status_code == 400, response.text


async def native_failure_contract():
    from app.api import native_stream_handlers as handlers
    from app.services.gemini import GeminiResponseWrapper
    from app.utils.protocol_common import _iter_sse_json_events
    class Client:
        def __init__(self, key):
            pass
        async def stream_chat(self, *args):
            wrapped = GeminiResponseWrapper({'candidates': [{'content': {'parts': [{'text': 'partial'}]}}]})
            wrapped.set_model('test')
            yield wrapped
            raise RuntimeError('interrupted')
    handlers.GeminiClient = Client
    handlers.handle_gemini_error = lambda error, key: 'interrupted'
    items = [item async for item in handlers.generate_native_stream_chunks(
        api_key='dummy', chat_request=SimpleNamespace(model='test'), contents=[],
        system_instruction=None, safety_settings=[], safety_settings_g2=[], is_gemini=False,
        settings=SimpleNamespace(MAX_EMPTY_RESPONSES=3, api_call_stats={}),
    )]
    assert items[-1][1]['failed_after_output'] is True
    assert items[-1][1]['success'] is False
    assert 'error' in items[-2][1]


async def vertex_fake_contract():
    from openai.types.chat import ChatCompletion
    from app.vertex.routes.openai_direct import _openai_fake_stream_engine
    response = ChatCompletion.model_validate({
        'id': 'test', 'created': 1, 'model': 'test', 'object': 'chat.completion',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call-id', 'type': 'function', 'function': {'name': 'f', 'arguments': '{}'}}],
        }}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3},
    })
    async def create():
        return response
    parts = [p async for p in _openai_fake_stream_engine(
        create, lambda r: '', lambda r: bool(r.choices), 'test-id', 'test-model',
    )]
    events = [json.loads(p[6:]) for p in parts if p.startswith('data: ') and '[DONE]' not in p]
    calls = [e['choices'][0]['delta']['tool_calls'] for e in events if e['choices'][0]['delta'].get('tool_calls')]
    assert calls[0][0]['id'] == 'call-id'
    assert calls[0][0]['index'] == 0
    assert events[-1]['choices'][0]['finish_reason'] == 'tool_calls'

async def vertex_sdk_contract():
    from vertex_sdk_contract import run
    await run()


def capabilities_contract():
    from app.utils.protocol_requests import response_request_to_chat_request
    base = {"model": "test", "input": "hello"}
    for option in [{"store": True}, {"background": True}, {"conversation": "id"},
                   {"truncation": "auto"}, {"tools": [{"type": "web_search"}]},
                   {"tools": [{"type": "function", "function": ["bad"]}]},
                   {"tool_choice": {"type": "bad"}}, {"text": {"format": "bad"}},
                   {"input": [{"role": [], "content": "bad"}]},
                   {"input": [{"type": "input_file", "file_id": "id"}]},
                   {"input": [{"role": "unknown", "content": "text"}]},
                   {"input": [{"role": "user", "content": [{"type": "input_image", "file_id": "id"}]}]}]:
        try:
            response_request_to_chat_request({**base, **option})
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsupported capability silently accepted: {option}")
    assert response_request_to_chat_request({**base, "store": False, "background": False}).messages


async def dispatch_contract():
    from app.api import chat_routes, protocol_routes
    from app.config import settings
    from app.services import GeminiClient
    seen = []
    async def handler(request, *args):
        seen.append(request.source_protocol)
        return {"id": "test", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
    settings.ENABLE_VERTEX = True
    chat_routes.vertex_chat_completions = handler
    GeminiClient.AVAILABLE_MODELS = ['test']
    await protocol_routes.responses_api({'model': 'test', 'input': 'hi'}, None, None, None)
    await protocol_routes.claude_messages({'model': 'test', 'messages': [{'role': 'user', 'content': 'hi'}]}, None, None, None)
    assert seen == ['responses', 'claude']
    from app.api import model_routes, protocol_handlers
    from app.models.schemas import ChatCompletionRequest
    settings.CLAUDE_MODEL_ALIASES = {'claude-x': '[PAY]gemini-3-pro-openai'}
    settings.CLAUDE_DEFAULT_MODEL = ''
    async def vertex_models(*args):
        return {'data': [{'id': '[PAY]gemini-3-pro-openai'}]}
    model_routes.vertex_list_models = vertex_models
    models = await model_routes.claude_list_models(None, None, None)
    assert [m['id'] for m in models['data']] == ['claude-x']
    settings.RESPONSES_DEFAULT_MODEL = 'test'
    request = ChatCompletionRequest(model='[PAY]gemini-3-pro-openai', messages=[])
    protocol_handlers._resolve_responses_model_alias(request)
    assert request.model == '[PAY]gemini-3-pro-openai'


async def client_pool_contract():
    from app.vertex import client_pool as pool
    from app.vertex import vertex_ai_init as init
    from app.vertex.routes.chat_api import _retain_stream_client
    from fastapi.responses import StreamingResponse
    class Client:
        def __init__(self, **kwargs):
            self.closed = 0
            self.async_closed = 0
            self.aio = SimpleNamespace(aclose=self.aclose)
        async def aclose(self):
            self.async_closed += 1
        def close(self):
            self.closed += 1
    init.genai.Client = Client
    a = SimpleNamespace(service_account_email='a@example.test', signer=SimpleNamespace(key_id='a'), project_id='p')
    b = SimpleNamespace(service_account_email='b@example.test', signer=SimpleNamespace(key_id='b'), project_id='p')
    first = init._get_client_from_pool('p', a)
    assert init._get_client_from_pool('p', a) is first
    second = init._get_client_from_pool('p', b)
    assert second is not first
    assert init._get_client_from_pool('p', a, 'other') is not first
    pool.retain(first)
    closed_source = []
    async def source():
        try:
            yield 'one'
            yield 'two'
        finally:
            closed_source.append(True)
    response = StreamingResponse(source())
    assert _retain_stream_client(response, first)
    await anext(response.body_iterator)
    init.reset_client_pool()
    await asyncio.sleep(0)
    assert first.closed == 0
    assert second.closed == 1
    await response.body_iterator.aclose()
    assert first.closed == first.async_closed == 1
    assert closed_source
    await pool.shutdown()
    assert not pool._entries
    # Eviction cannot close an in-flight client, even when the cache is full.
    pool._MAX_CLIENTS = 1
    first = pool.get_client(('x', 1), Client)
    pool.retain(first)
    second = pool.get_client(('x', 2), Client)
    await asyncio.sleep(0)
    assert not first.closed
    await pool.release(first)
    assert first.closed == 1
    await pool.shutdown()
    assert second.closed == 1


def token_identity_contract():
    from app.vertex.credentials_manager import _refresh_auth, clear_token_cache
    from concurrent.futures import ThreadPoolExecutor
    clear_token_cache()
    class Credential:
        project_id = 'same-project'
        scopes = ['scope']
        expiry = None
        def __init__(self, account, key='key'):
            self.service_account_email = account
            self.signer = SimpleNamespace(key_id=key)
            self.refreshes = 0
        def refresh(self, request):
            self.refreshes += 1
            self.token = self.service_account_email + ':' + self.signer.key_id
    a, b = Credential('a'), Credential('b')
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(_refresh_auth, [a, b] * 10))
    assert tokens == ['a:key', 'b:key'] * 10
    assert a.refreshes == b.refreshes == 1
    replacement = Credential('a', 'new-key')
    assert _refresh_auth(replacement) == 'a:new-key'
    assert replacement.refreshes == 1
    equivalent = Credential('a')
    assert _refresh_auth(equivalent) == 'a:key'
    assert equivalent.refreshes == 0


async def direct_stream_contract():
    from app.vertex.routes.openai_direct import openai_true_stream_generator
    class Stream:
        def __init__(self, complete):
            self.complete = complete
            self.closed = False
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(model_dump=lambda **kwargs: {
                    'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': None}],
                })
                if self.complete:
                    yield SimpleNamespace(model_dump=lambda **kwargs: {
                        'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                    })
            return chunks()
        async def close(self):
            self.closed = True
    for complete in (True, False):
        stream = Stream(complete)
        async def create(**kwargs):
            return stream
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        frames = [f async for f in openai_true_stream_generator(client, {}, None, 'test')]
        assert stream.closed
        assert any('"error"' in f for f in frames) == (not complete)
    stream = Stream(True)
    generator = openai_true_stream_generator(client, {}, None, 'test')
    await anext(generator)
    await generator.aclose()
    assert stream.closed


if __name__ == '__main__':
    result = globals()[sys.argv[1] + '_contract']()
    if asyncio.iscoroutine(result):
        asyncio.run(result)
