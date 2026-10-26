"""Real ASGI and HTTP transport contracts; no network or paid upstream calls."""
from pathlib import Path
import subprocess
import sys


def test_dashboard_observes_real_upstream_attempts_and_preserves_wire_content():
    code = r'''
import asyncio
import httpx
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.api import route_runtime, nonstream_handlers, stream_handlers
from app.utils.auth import custom_verify_password
from app.utils.cache import ResponseCacheManager
from app.utils.request import ActiveRequestsManager
from app.utils.request_registry import request_registry
from app.services import GeminiClient
from app.services import gemini
import app.config.settings as settings
settings.WEB_PASSWORD = 'dashboard-test'
settings.ENABLE_VERTEX = False
settings.PUBLIC_MODE = False
settings.NONSTREAM_KEEPALIVE_ENABLED = False
settings.FAKE_STREAMING = False
settings.MAX_RETRY_NUM = 1
settings.CONCURRENT_REQUESTS = 1
settings.MAX_EMPTY_RESPONSES = 3
settings.MAX_REQUESTS_PER_MINUTE = 1000
settings.MAX_REQUESTS_PER_DAY_PER_IP = 1000
GeminiClient.AVAILABLE_MODELS = ['test-model']
route_runtime.key_manager = None
route_runtime.response_cache_manager = ResponseCacheManager(600, 0)
route_runtime.active_requests_manager = ActiveRequestsManager()
route_runtime.safety_settings = route_runtime.safety_settings_g2 = []
app.dependency_overrides[custom_verify_password] = lambda: True
app.dependency_overrides[route_runtime.verify_user_agent] = lambda: True
headers = {'Authorization':'Bearer dashboard-test'}
async def select(**kwargs): return ['private-upstream-key']
wire = {'candidates':[{'content':{'parts':[{'text':'answer'}]}, 'finishReason':'STOP'}], 'usageMetadata':{'promptTokenCount':4,'candidatesTokenCount':6,'totalTokenCount':10}}
async def handler(request):
    import json
    if ':streamGenerateContent' in str(request.url):
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, text='data: '+json.dumps(wire)+'\n\n')
    return httpx.Response(200,json=wire)
transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
async def client(): return transport
with patch.object(gemini, 'get_async_client', client), patch.object(nonstream_handlers, 'select_valid_api_keys', select), patch.object(stream_handlers, 'select_valid_api_keys', select):
    client = TestClient(app)
    from types import SimpleNamespace
    from app.api.dashboard import runtime as dashboard_runtime
    dashboard_runtime.key_manager = SimpleNamespace(api_keys=[])
    dashboard_runtime.response_cache_manager = route_runtime.response_cache_manager
    dashboard_runtime.active_requests_manager = route_runtime.active_requests_manager
    dashboard_runtime.credential_manager = None
    full=client.get('/api/dashboard-data',headers=headers).json()
    compact=client.get('/api/dashboard-data?summary=true',headers=headers).json()
    assert 'monitor' in full and 'logs' in full
    assert 'monitor' not in compact and 'logs' not in compact and 'fake_streaming' not in compact
    assert 'api_key_stats' in compact and 'stats_period_started_at' in compact
    for path in ['/api/runtime-data','/api/logs']:
        assert client.get(path).status_code == 401
        assert client.get(path,headers=headers).status_code == 200
    for path, extra in [('/v1/chat/completions',{'messages':[{'role':'user','content':'hi'}]}),('/v1/responses',{'input':'hi'}),('/v1/messages',{'messages':[{'role':'user','content':'hi'}]})]:
        for streaming in [False,True]:
            result=client.post(path,json={'model':'test-model','stream':streaming,**extra})
            assert result.status_code==200, result.text
            assert 'answer' in result.text
            identifier=result.headers['x-request-id']
            runtime=client.get('/api/runtime-data',headers=headers).json()
            record=next(r for r in runtime['requests'] if r['request_id']==identifier)
            assert record['outcome']=='成功', record
            assert record['attempt_count']==1, record
            assert record['upstream_usage']['total_tokens']==10, record
            assert record['usage']['total_tokens']==10, record
            assert record['upstream_first_output_ms'] is not None if streaming else record['upstream_first_output_ms'] is None
            assert any(e['label']=='请求上游' for e in record['timeline'])
            assert runtime['active_count']==0 and runtime['upstream_active']==0
    retry_calls = 0
    async def retry_handler(request):
        global retry_calls
        retry_calls += 1
        if retry_calls == 1:
            return httpx.Response(429, json={'error':{'code':429,'message':'quota exceeded'}})
        return httpx.Response(200, json=wire)
    retry_transport = httpx.AsyncClient(transport=httpx.MockTransport(retry_handler))
    async def retry_client(): return retry_transport
    async def select_retry(**kwargs): return ['private-first-key' if retry_calls == 0 else 'private-second-key']
    settings.MAX_RETRY_NUM = 2
    with patch.object(gemini, 'get_async_client', retry_client), patch.object(nonstream_handlers, 'select_valid_api_keys', select_retry), patch.object(nonstream_handlers, 'compute_inter_batch_backoff', lambda n: .01):
        result = client.post('/v1/chat/completions', json={'model':'test-model','messages':[{'role':'user','content':'hi'}]})
        assert result.status_code == 200, result.text
        runtime=client.get('/api/runtime-data',headers=headers).json()
        record=next(r for r in runtime['requests'] if r['request_id']==result.headers['x-request-id'])
        assert record['attempt_count'] == 2 and record['outcome'] == '成功', record
        assert record['attempts'][0]['status_code'] == 429
        assert record['attempts'][0]['reason'] == '上游限流或配额耗尽'
        wait=next(e for e in record['timeline'] if e['label']=='重试退避')
        assert wait['retry_delay']==.01 and wait['next_attempt_number']==2
        switch=next(e for e in record['timeline'] if e['label']=='切换密钥')
        assert switch['status_code']==429 and switch['previous_key'] != switch['next_key']
        assert 'private-first-key' not in str(record) and 'private-second-key' not in str(record)
    asyncio.run(retry_transport.aclose())
    feed=client.get('/api/logs',headers=headers).json()
    assert len({e['log_id'] for e in feed['logs']})==len(feed['logs'])
    assert 'private-upstream-key' not in str(feed)
    assert client.get('/api/logs?after='+str(feed['cursor']),headers=headers).json()['logs']==[]
    assert client.get('/api/logs?after=-1',headers=headers).status_code==422
    assert client.get('/api/runtime-data?after='+str(runtime['cursor']),headers=headers).json()['requests']==[]
    client.close()
asyncio.run(transport.aclose())
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
