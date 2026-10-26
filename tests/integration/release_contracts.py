import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def numeric():
    from fastapi import HTTPException
    from app.api.dashboard import config_updates as c
    from app.config import settings
    with patch.object(c, 'save_settings') as save:
        for key, invalid in [
            ('max_retry_num', [None, [], {}, True, 1.9, '1.2', 'NaN', float('inf'), -1]),
            ('fake_streaming_interval', [None, [], {}, True, 0, 'NaN', float('inf'), float('nan'), -0.1]),
            ('max_thinking_model_levels', [{'test': value} for value in [True, None, 'banana', '', 'NaN', -2, 1.5, float('inf')]]),
        ]:
            original = getattr(settings, key.upper())
            for value in invalid:
                try:
                    await c.update_config({'key': key, 'value': value})
                except HTTPException as exc:
                    assert exc.status_code == 422, exc
                else:
                    raise AssertionError((key, value))
                assert getattr(settings, key.upper()) == original
        for key in [None, [], {}, True, 3]:
            try:
                await c.update_config({'key': key, 'value': 1})
            except HTTPException as exc:
                assert exc.status_code == 400
            else:
                raise AssertionError(key)
        save.assert_not_called()
        for key, value in [('max_retry_num', '3'), ('fake_streaming_interval', '0.2'),
                           ('max_thinking_model_levels', {'test': 'HIGH', 'other': '-1'})]:
            assert (await c.update_config({'key': key, 'value': value}))['status'] == 'success'
        assert settings.MAX_RETRY_NUM == 3
        assert settings.MAX_THINKING_MODEL_LEVELS == {'test': 'high', 'other': -1}


def credential_fixture():
    import rsa
    _, key = rsa.newkeys(1024)  # Ephemeral test key; never sent to Google.
    return {
        'type': 'service_account', 'project_id': 'test-project', 'private_key_id': 'test-key',
        'private_key': key.save_pkcs1().decode(),
        'client_email': 'test@example.invalid', 'token_uri': 'https://oauth2.googleapis.com/token',
        'note': 'literal { and } in JSON strings, with an escaped quote "',
    }


def credentials():
    from app.vertex.credentials_manager import CredentialManager, parse_credentials_json, prepare_json_credentials
    first = credential_fixture()
    second = {**first, 'client_email': 'second@example.invalid'}
    rotated = {**first, 'private_key_id': 'rotated-key'}
    a, b = json.dumps(first), json.dumps(second)
    for text in [a + ',' + b, a + '\n' + b, a + b, json.dumps([first, second])]:
        assert len(prepare_json_credentials(text)) == 2
    for text in ['', 'null', '3', '[]', '[3]', a + ',', a + ' junk', a + '{', a + ', null', '{"x": NaN}', '{}']:
        try:
            prepare_json_credentials(text)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid credentials accepted')
    assert parse_credentials_json(a)[0]['note'] == first['note']
    manager = CredentialManager()
    manager.credentials_files = ['/synthetic/test-project.json']
    assert manager.load_credentials_from_json_list([first, first, second, rotated]) == 3
    assert manager.load_credentials_from_json_list([first, second]) == 0
    assert len(manager.in_memory_credentials) == 3


async def credential_update():
    from fastapi import HTTPException
    from app.api.dashboard import config_updates as c
    from app.vertex.credentials_manager import CredentialManager
    from app.config import settings
    manager = CredentialManager()
    manager.credentials_files = ['/synthetic/existing.json']
    old = credential_fixture()
    assert manager.add_credential_from_json(old)
    new = {**old, 'client_email': 'new@example.invalid'}
    original_entries = manager.in_memory_credentials
    settings.GOOGLE_CREDENTIALS_JSON = json.dumps(old)
    c.app_config.GOOGLE_CREDENTIALS_JSON = settings.GOOGLE_CREDENTIALS_JSON
    with patch.object(c.runtime, 'credential_manager', manager), patch.object(c, 'reset_client_pool') as reset, \
         patch.object(c, 'save_settings') as save, patch.object(c, 'refresh_models_config_cache') as remote:
        for text in ['null', '{}', json.dumps([new, {**old, 'private_key': 'invalid-secret-material'}]), json.dumps(new) + 'truncated']:
            try:
                await c.update_config({'key': 'google_credentials_json', 'value': text})
            except HTTPException as exc:
                assert exc.status_code == 422
                assert 'invalid-secret-material' not in exc.detail
            else:
                raise AssertionError('invalid upload accepted')
            assert manager.in_memory_credentials is original_entries
            assert settings.GOOGLE_CREDENTIALS_JSON == json.dumps(old)
            reset.assert_not_called()
            save.assert_not_called()
        await c.update_config({'key': 'google_credentials_json', 'value': json.dumps(new)})
        assert len(manager.in_memory_credentials) == 1
        assert manager.in_memory_credentials[0]['credentials'].service_account_email == new['client_email']
        assert settings.GOOGLE_CREDENTIALS_JSON == c.app_config.GOOGLE_CREDENTIALS_JSON == json.dumps(new)
        assert manager.credentials_files == ['/synthetic/existing.json']
        reset.assert_called_once()
        save.assert_called_once()
        remote.assert_not_called()
        await c.update_config({'key': 'google_credentials_json', 'value': '   '})
        assert settings.GOOGLE_CREDENTIALS_JSON == json.dumps(new)


async def auth():
    from fastapi import HTTPException
    from app.utils import auth as auth
    from app.vertex.auth import get_api_key, validate_api_key
    from app.api.dashboard.auth import require_body_password, _extract_token
    from app.config import settings
    settings.WEB_PASSWORD = '中文管理密码'
    assert auth.verify_web_password(settings.WEB_PASSWORD)
    assert (await require_body_password({'password': settings.WEB_PASSWORD}))['password'] == settings.WEB_PASSWORD
    for value in [None, [], '', '错误', '\ud800']:
        assert not auth.verify_web_password(value)
    settings.WEB_PASSWORD = ''
    assert not auth.verify_web_password('')
    settings.PASSWORD = 'abc'
    assert validate_api_key('abc')
    for scheme in ['Bearer', 'bearer', 'BEARER']:
        assert await get_api_key(scheme + ' abc') == 'abc'
        assert _extract_token(scheme + ' abc', None) == 'abc'
        await auth.custom_verify_password(authorization=scheme + ' abc', x_api_key=None, x_goog_api_key=None, key=None)
    for value in ['Basic abc', 'Bearer bad', 'Bearer ', None]:
        try:
            await get_api_key(value)
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError('invalid token accepted')


async def vertex_input():
    from fastapi.responses import JSONResponse
    from app.vertex.routes import chat_api
    from app.vertex.models import OpenAIRequest
    bad = OpenAIRequest(model='[PAY]test-openai', messages=[{'role': 'tool', 'tool_call_id': 'missing', 'content': 'x'}])
    with patch.object(chat_api, 'get_credential_manager', side_effect=AssertionError('must validate first')):
        response = await chat_api.chat_completions(None, bad, 'test')
        assert response.status_code == 400
    request = OpenAIRequest(model='[PAY]test-openai', messages=[
        {'role': 'assistant', 'tool_calls': [{'id': 'call-id', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-id', 'content': [{'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]},
    ])
    async def direct(normalized, *args):
        assert normalized.messages[1].content == 'one\ntwo'
        assert normalized.messages[1].name == 'lookup'
        return JSONResponse({'ok': True})
    with patch.object(chat_api, 'get_credential_manager', return_value=Mock()), patch.object(chat_api, 'handle_openai_direct_request', direct):
        response = await chat_api.chat_completions(None, request, 'test')
        assert response.status_code == 200
        assert isinstance(request.messages[1].content, list)


async def vertex_lifespan():
    from unittest.mock import AsyncMock
    from app.vertex import main
    startup, close_pool, close_http = AsyncMock(), AsyncMock(), AsyncMock()
    with patch.object(main, 'startup_event', startup), patch.object(main.client_pool, 'shutdown', close_pool), patch.object(main, 'close_async_client', close_http):
        try:
            async with main.lifespan(main.app):
                raise RuntimeError('application stopped')
        except RuntimeError:
            pass
        startup.assert_awaited_once()
        close_pool.assert_awaited_once()
        close_http.assert_awaited_once()


if __name__ == '__main__':
    result = globals()[sys.argv[1]]()
    if asyncio.iscoroutine(result):
        asyncio.run(result)
