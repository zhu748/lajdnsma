"""缓存隔离、容量边界与共享请求取消的行为回归。"""
import asyncio
from collections import deque
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    for name in ("app", "app.utils", "app.config"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    logs = types.ModuleType("app.utils.logging")
    logs.log = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "app.utils.logging", logs)
    settings = types.ModuleType("app.config.settings")
    settings.NONSTREAM_KEEPALIVE_ENABLED = False
    monkeypatch.setitem(sys.modules, "app.config.settings", settings)
    utils = sys.modules["app.utils"]
    utils.log = logs.log
    utils.generate_cache_key = lambda *args, **kwargs: "key"
    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = Exception
    fastapi.status = types.SimpleNamespace(HTTP_400_BAD_REQUEST=400)
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    return types.SimpleNamespace(
        cache=load_file("review_cache", "app/utils/cache.py"),
        helpers=load_file("review_helpers", "app/api/request_helpers.py"),
        requests=load_file("review_requests", "app/utils/request.py"),
        orchestration=load_file("review_orchestration", "app/api/orchestration_helpers.py"),
        settings=settings,
    )


def chat(**kwargs):
    return types.SimpleNamespace(
        model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}], **kwargs,
    )


@pytest.mark.parametrize("field,a,b", [
    ("tool_choice", "auto", "required"),
    ("parallel_tool_calls", True, False),
    ("n", 1, 2),
    ("top_k", 1, 40),
    ("thinking_budget", 0, 8192),
    ("reasoning_effort", "low", "high"),
    ("enable_thinking", False, True),
    ("expose_reasoning", False, True),
    ("source_protocol", None, "responses"),
])
def test_generation_options_do_not_share_cache(modules, field, a, b):
    key = modules.cache.generate_cache_key
    assert key(chat(**{field: a})) != key(chat(**{field: b}))


def test_function_call_history_changes_cache_key(modules):
    key = modules.cache.generate_cache_key
    def request(command):
        req = chat()
        req.messages = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": command}},
        ]}]
        return req
    assert key(request('{"cmd":"pwd"}')) != key(request('{"cmd":"ls"}'))


def test_tool_result_identity_changes_cache_key(modules):
    key = modules.cache.generate_cache_key
    a, b = chat(), chat()
    a.messages = [{"role": "tool", "tool_call_id": "call_a", "name": "weather", "content": "ok"}]
    b.messages = [{"role": "tool", "tool_call_id": "call_b", "name": "search", "content": "ok"}]
    assert key(a) != key(b)


def test_message_boundaries_cannot_collide_with_text(modules):
    a, b = chat(), chat()
    a.messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    b.messages = [{"role": "assistant", "content": "brole:usertext:a"}]
    assert modules.cache.generate_cache_key(a) != modules.cache.generate_cache_key(b)


def test_system_prompt_survives_history_truncation(modules):
    a, b = chat(), chat()
    a.messages.insert(0, {"role": "system", "content": "Translate to French"})
    b.messages.insert(0, {"role": "system", "content": "Translate to Chinese"})
    key = modules.cache.generate_cache_key
    assert key(a, last_n_messages=1) != key(b, last_n_messages=1)


@pytest.mark.parametrize("field,a,b", [
    ("system_instruction", {"parts": [{"text": "French"}]}, {"parts": [{"text": "Chinese"}]}),
    ("generationConfig", {"responseSchema": {"type": "STRING"}}, {"responseSchema": {"type": "INTEGER"}}),
    ("safetySettings", [{"category": "X", "threshold": "OFF"}], [{"category": "X", "threshold": "BLOCK_LOW_AND_ABOVE"}]),
    ("tool_config", {"functionCallingConfig": {"mode": "ANY"}}, {"functionCallingConfig": {"mode": "AUTO"}}),
])
def test_native_request_options_do_not_share_cache(modules, field, a, b):
    def request(value):
        return types.SimpleNamespace(model="gemini-2.5-pro", payload=types.SimpleNamespace(
            contents=[{"role": "user", "parts": [{"text": "hi"}]}], **{field: value},
        ))
    key = modules.cache.generate_cache_key
    assert key(request(a), is_gemini=True) != key(request(b), is_gemini=True)


@pytest.mark.parametrize("part", [
    lambda v: {"inlineData": {"mimeType": "image/png", "data": v}},
    lambda v: {"fileData": {"fileUri": v}},
    lambda v: {"functionCall": {"name": "weather", "args": {"city": v}}},
])
def test_native_parts_participate_fully_in_cache_key(modules, part):
    def request(value):
        return types.SimpleNamespace(model="gemini-2.5-pro", payload=types.SimpleNamespace(
            contents=[{"role": "user", "parts": [part(value)]}],
        ))
    key = modules.cache.generate_cache_key
    assert key(request("a"), is_gemini=True) != key(request("b"), is_gemini=True)


def test_dictionary_key_order_does_not_change_cache_key(modules):
    a, b = chat(tool_choice={"type": "function", "function": {"name": "weather"}}), chat()
    b.tool_choice = {"function": {"name": "weather"}, "type": "function"}
    b.messages = [{"content": "hi", "role": "user"}]
    assert modules.cache.generate_cache_key(a) == modules.cache.generate_cache_key(b)


@pytest.mark.parametrize("capacity", [0, 1, 5, 12])
def test_cache_respects_small_capacity(modules, capacity):
    async def run():
        manager = modules.cache.ResponseCacheManager(600, capacity)
        for i in range(30):
            await manager.store(str(i), i)
            assert manager.cur_cache_num <= capacity
        assert manager.cur_cache_num == sum(map(len, manager.cache.values()))
    asyncio.run(run())


def test_consumed_entries_do_not_accumulate_heap_metadata(modules):
    async def run():
        manager = modules.cache.ResponseCacheManager(600, 100)
        await manager.store("anchor", "retained")
        for i in range(1000):
            await manager.store("transient", i)
            assert await manager.get_and_remove("transient") == (i, True)
        assert manager.cur_cache_num == 1
        assert len(manager._evict_heap) <= 64
        assert await manager.get_and_remove("anchor") == ("retained", True)
        assert manager._evict_heap == []
    asyncio.run(run())


def test_existing_cache_is_counted_and_can_be_evicted(modules):
    import time
    async def run():
        initial = {"old": deque([{"response": "old", "created_at": 1, "expiry_time": time.time() + 600}])}
        manager = modules.cache.ResponseCacheManager(600, 1, initial)
        assert manager.cur_cache_num == 1
        await manager.store("new", "new")
        assert await manager.get_and_remove("old") == (None, False)
        assert await manager.get_and_remove("new") == ("new", True)
    asyncio.run(run())


def test_expiry_cleanup_compacts_index_and_logs_one_summary(modules):
    async def run():
        manager = modules.cache.ResponseCacheManager(600, 200)
        await manager.store("anchor", "valid")
        for i in range(100):
            await manager.store(str(i), i)
            manager.cache[str(i)][0]["expiry_time"] = 0
        with patch.object(modules.cache, "log") as log:
            await manager.clean_expired()
            assert log.call_count == 1
        assert manager.cur_cache_num == 1
        assert len(manager._evict_heap) == 1
        assert await manager.get_and_remove("anchor") == ("valid", True)
    asyncio.run(run())


def test_waiter_cancellation_leaves_original_request_running(modules):
    async def run():
        finish = asyncio.Event()
        async def original():
            await finish.wait()
            return {"ok": True}
        task = asyncio.create_task(original())
        manager = modules.requests.ActiveRequestsManager()
        manager.add("key", task)
        waiter = asyncio.create_task(modules.helpers.wait_for_existing_task(manager, "key", chat(stream=False)))
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not task.done()
            assert manager.get("key") is task
            finish.set()
            assert await task == {"ok": True}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_waiter_timeout_does_not_cancel_original(modules):
    async def run():
        task = asyncio.create_task(asyncio.Event().wait())
        manager = modules.requests.ActiveRequestsManager()
        manager.add("key", task)
        wait_for = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await wait_for(awaitable, timeout=0.001)
        try:
            with patch.object(modules.helpers.asyncio, "wait_for", short_wait):
                assert await modules.helpers.wait_for_existing_task(manager, "key", chat(stream=False)) is None
            assert not task.done()
            assert manager.get("key") is task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_keepalive_requests_do_not_share_response_iterators(modules):
    async def run():
        task = asyncio.create_task(asyncio.sleep(0, result="one-shot-response"))
        manager = modules.requests.ActiveRequestsManager()
        manager.add("key", task)
        modules.settings.NONSTREAM_KEEPALIVE_ENABLED = True
        result = await modules.helpers.wait_for_existing_task(manager, "key", chat(stream=False))
        assert result is None
        await task
    asyncio.run(run())


def test_cleanup_cannot_remove_replacement_task(modules):
    async def run():
        manager = modules.requests.ActiveRequestsManager()
        replacement = asyncio.create_task(asyncio.sleep(0))
        async def original():
            manager.add("key", replacement)
            return {"ok": True}
        task = asyncio.create_task(original())
        manager.add("key", task)
        try:
            assert await modules.helpers.wait_for_existing_task(manager, "key", chat(stream=False)) == {"ok": True}
            assert manager.get("key") is replacement
        finally:
            await replacement
    asyncio.run(run())


def test_original_task_cancellation_allows_waiter_to_retry(modules):
    async def run():
        task = asyncio.create_task(asyncio.Event().wait())
        manager = modules.requests.ActiveRequestsManager()
        manager.add("key", task)
        waiter = asyncio.create_task(modules.helpers.wait_for_existing_task(manager, "key", chat(stream=False)))
        await asyncio.sleep(0)
        task.cancel()
        assert await waiter is None
        assert manager.get("key") is None
    asyncio.run(run())


def test_owner_disconnect_cleans_cancelled_task_from_pool(modules):
    async def run():
        task = asyncio.create_task(asyncio.Event().wait())
        manager = modules.requests.ActiveRequestsManager()
        manager.add("key", task)
        owner = asyncio.create_task(modules.orchestration.await_process_task_result(
            process_task=task, public_mode=False, active_requests_manager=manager,
            pool_key="key", get_cache_func=None, cache_key="key", is_stream=False, is_gemini=False,
        ))
        await asyncio.sleep(0)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert task.cancelled()
        assert manager.get("key") is None
    asyncio.run(run())
