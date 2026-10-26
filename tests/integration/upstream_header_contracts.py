"""No external network: synthetic credentials and real SDK HTTP serialization."""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# All HTTP transports below are local. Environment proxy/SDK credentials must
# not affect a deterministic test (and are never printed).
for name in list(os.environ):
    if name.lower() in {'http_proxy', 'https_proxy', 'all_proxy'} or name.startswith(('GOOGLE_', 'GEMINI_', 'OPENAI_')):
        os.environ.pop(name)

GOOGLE_ID = f"google-genai-sdk/{version('google-genai')} gl-python/{sys.version.split()[0]}"
NATIVE_REPLY = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]}
CHAT_REPLY = {'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
              'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}


def check_identity(request):
    h = request.headers
    assert h['user-agent'] == h['x-goog-api-client'] == GOOGLE_ID
    assert not any(k.startswith(('sec-', 'x-stainless-')) for k in h)
    for name in ('origin', 'referer', 'cookie', 'accept-language', 'x-api-key',
                 'anthropic-version', 'openai-organization', 'openai-project'):
        assert name not in h, name
    assert 'key' not in request.url.params
    for name in ('authorization', 'x-goog-api-key', 'user-agent', 'content-type'):
        assert len(h.get_list(name)) <= 1, name


async def google_rest():
    from app.services import gemini, embedding
    from app.utils import api_key
    from app.models.schemas import AIRequest, ChatCompletionRequest, ChatRequestGemini, EmbeddingRequest
    from app.utils.protocol_requests import response_request_to_chat_request, claude_request_to_chat_request
    from app.config import settings
    from app.utils import http_client

    settings.RANDOM_STRING = False
    received = []
    def receive(request):
        received.append(request)
        check_identity(request)
        assert request.headers['x-goog-api-key'] in {'first-key', 'second-key'}
        assert 'authorization' not in request.headers
        assert request.headers['accept-encoding'] == 'gzip, deflate'
        if request.method == 'GET':
            assert 'content-type' not in request.headers
            return httpx.Response(200, json={'models': []})
        assert request.headers['content-type'] == 'application/json'
        if request.url.params.get('alt') == 'sse':
            assert request.headers['accept'] == 'text/event-stream'
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  text='data: ' + json.dumps(NATIVE_REPLY) + '\n\n')
        assert request.headers['accept'] == 'application/json'
        body = {'embeddings': [{'values': [1.0]}]} if request.url.path.endswith(':batchEmbedContents') else NATIVE_REPLY
        return httpx.Response(200, json=body)

    transport = http_client.build_async_client(transport=httpx.MockTransport(receive))
    async def get_transport():
        return transport
    with patch.object(gemini, 'get_async_client', get_transport), \
         patch.object(embedding, 'get_async_client', get_transport), \
         patch.object(api_key, 'get_async_client', get_transport):
        try:
            requests = [
                ChatCompletionRequest(model='test', messages=[{'role': 'user', 'content': 'hello'}]),
                response_request_to_chat_request({'model': 'test', 'input': 'hello'}),
                claude_request_to_chat_request({'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}]}),
                AIRequest(model='test', payload=ChatRequestGemini(contents=[{'role': 'user', 'parts': [{'text': 'hello'}]}])),
            ]
            for key in ('first-key', 'second-key'):
                client = gemini.GeminiClient(key)
                for request in requests:
                    contents = [{'role': 'user', 'parts': [{'text': 'hello'}]}]
                    await client.complete_chat(request, contents, [], None)
                    frames = [chunk async for chunk in client.stream_chat(request, contents, [], None)]
                    assert frames
                await client._fetch_models_raw(key)
                assert await api_key.test_api_key(key)
                await embedding.EmbeddingClient(key).create_embeddings(EmbeddingRequest(model='test', input='hello'))
            assert len(received) == 22
            # Pool defaults must not retain the previous model request's secret.
            auxiliary = transport.build_request('GET', 'https://config.example/version')
            assert 'x-goog-api-key' not in auxiliary.headers
            assert 'x-goog-api-client' not in auxiliary.headers
            assert auxiliary.headers['user-agent'].startswith('python-httpx/')
        finally:
            await transport.aclose()


async def native(mode):
    from google import genai
    from google.oauth2.credentials import Credentials
    from app.vertex import client_pool, vertex_ai_init
    from app.vertex.routes import chat_api
    from app.utils.stealth import build_gemini_headers

    received = []
    def receive(request):
        received.append(request)
        check_identity(request)
        assert request.headers['user-agent'] == build_gemini_headers()['User-Agent']
        if mode == 'express':
            assert request.headers['x-goog-api-key'] == 'express-test'
            assert 'authorization' not in request.headers
        else:
            assert request.headers['authorization'] == 'Bearer sa-test'
            assert request.headers['x-goog-user-project'] == 'quota-test'
            assert 'x-goog-api-key' not in request.headers
        if request.url.params.get('alt') == 'sse':
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  text='data: ' + json.dumps(NATIVE_REPLY) + '\n\n')
        return httpx.Response(200, json=NATIVE_REPLY)

    real_client = genai.Client
    def factory(**kwargs):
        return real_client(**kwargs, http_options={
            'client_args': {'trust_env': False},
            'async_client_args': {'transport': httpx.MockTransport(receive), 'trust_env': False},
        })
    with patch.object(genai, 'Client', factory):
        if mode == 'express':
            with patch.object(chat_api.app_config, 'resolve_express_api_keys', return_value=['express-test']):
                client, error = chat_api._resolve_express_client('test', 'test')
                assert error is None
        else:
            client = vertex_ai_init._get_client_from_pool('project-test', Credentials('sa-test', quota_project_id='quota-test'))
        assert client is not None
        try:
            await client.aio.models.generate_content(model='test', contents='hello')
            stream = await client.aio.models.generate_content_stream(model='test', contents='hello')
            assert [chunk async for chunk in stream]
            assert len(received) == 2
        finally:
            await client_pool.shutdown()


async def native_express():
    await native('express')


async def native_sa():
    await native('sa')


async def direct(mode):
    from google.oauth2.credentials import Credentials
    from app.config import settings
    from app.vertex import client_pool
    from app.vertex.models import OpenAIRequest
    from app.vertex.routes import openai_direct as direct

    # OpenAI SDK reads these even when base_url points to Google.
    os.environ['OPENAI_ORG_ID'] = 'unrelated-openai-org'
    os.environ['OPENAI_PROJECT_ID'] = 'unrelated-openai-project'
    credentials = Credentials('direct-test', quota_project_id='quota-test')
    manager = SimpleNamespace(get_random_credentials=lambda: (credentials, 'project-test'))
    received = []
    def receive(request):
        received.append(request)
        check_identity(request)
        assert request.headers['authorization'] == 'Bearer direct-test'
        assert request.headers['x-goog-user-project'] == credentials.quota_project_id
        assert 'x-goog-api-key' not in request.headers
        assert request.headers['content-type'] == 'application/json'
        body = json.loads(request.content)
        assert request.headers['accept'] == ('text/event-stream' if body['stream'] else 'application/json')
        if body['stream']:
            reply = {**CHAT_REPLY, 'object': 'chat.completion.chunk', 'choices': [
                {'index': 0, 'delta': {'content': 'ok'}, 'finish_reason': 'stop'},
            ]}
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  text='data: ' + json.dumps(reply) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json=CHAT_REPLY)

    real_build = direct.build_async_client
    with patch.object(direct, 'build_async_client', side_effect=lambda **kwargs: real_build(transport=httpx.MockTransport(receive), **kwargs)), \
         patch.object(direct, '_refresh_auth', return_value='direct-test'), \
         patch.object(direct.app_config, 'FAKE_STREAMING_INTERVAL_SECONDS', 0):
        try:
            modes = ('json', 'stream', 'fake') if mode == 'pool' else (mode,)
            for current in modes:
                settings.FAKE_STREAMING = current == 'fake'
                request = OpenAIRequest(model='test', messages=[{'role': 'user', 'content': 'hello'}], stream=current != 'json')
                response = await direct.handle_openai_direct_request(request, manager, 'test')
                assert response.status_code == 200
                if current != 'json':
                    frames = [frame async for frame in response.body_iterator]
                    assert not any('"error"' in frame for frame in frames), frames
                    assert any('[DONE]' in frame for frame in frames)
                else:
                    assert json.loads(response.body)['choices'][0]['message']['content'] == 'ok'
                assert json.loads(received[-1].content)['stream'] == (current == 'stream')
            assert len(received) == len(modes)
            assert len(client_pool._clients) == 1  # JSON, SSE and fake SSE share the client.
            if mode == 'pool':
                credentials._quota_project_id = 'another-quota'
                response = await direct.handle_openai_direct_request(
                    OpenAIRequest(model='test', messages=[{'role': 'user', 'content': 'hello'}]), manager, 'test')
                assert response.status_code == 200
                assert len(client_pool._clients) == 2  # Different quota identity must not share defaults.
        finally:
            await client_pool.shutdown()


async def direct_json():
    await direct('json')


async def direct_stream():
    await direct('stream')


async def direct_fake():
    await direct('fake')


async def direct_pool():
    await direct('pool')


async def check_cors(module):
    from importlib import import_module
    os.environ['ALLOWED_ORIGINS'] = 'https://client.example'
    app = import_module(module).app
    from app.config import settings
    settings.PASSWORD = 'test-gateway-password'
    headers = 'authorization,content-type,x-stainless-lang,x-stainless-runtime,x-goog-api-client,anthropic-dangerous-direct-browser-access'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for origin, expected in [('https://client.example', 200), ('https://untrusted.example', 400)]:
            response = await client.options('/v1/messages', headers={
                'Origin': origin, 'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': headers,
            })
            assert response.status_code == expected, response.text
            if expected == 200:
                assert response.headers['access-control-allow-headers'] == headers
                assert response.headers['access-control-allow-origin'] == origin
            else:
                assert 'access-control-allow-origin' not in response.headers
            assert 'access-control-allow-credentials' not in response.headers
        response = await client.post('/v1/chat/completions', headers={'Origin': 'https://client.example'},
                                     json={'model': 'test', 'messages': []})
        assert response.status_code == 401  # CORS does not bypass authentication.


async def cors():
    await check_cors('app.main')


async def vertex_cors():
    await check_cors('app.vertex.main')


if __name__ == '__main__':
    asyncio.run(globals()[sys.argv[1]]())
