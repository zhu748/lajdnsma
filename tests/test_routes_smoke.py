import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_routes_module():
    fake_app = types.ModuleType("app")
    fake_app.__path__ = [str(ROOT / "app")]
    fake_api_pkg = types.ModuleType("app.api")
    fake_api_pkg.__path__ = [str(ROOT / "app/api")]
    fake_config_pkg = types.ModuleType("app.config")
    fake_config_pkg.__path__ = []

    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def __init__(self):
            self.included = []

        def get(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

        def post(self, *args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

        def include_router(self, router):
            self.included.append(router)

    fake_fastapi.APIRouter = APIRouter
    fake_fastapi.Body = lambda *args, **kwargs: None
    fake_fastapi.HTTPException = HTTPException
    fake_fastapi.Path = lambda *args, **kwargs: None
    fake_fastapi.Query = lambda *args, **kwargs: None
    fake_fastapi.Request = object
    fake_fastapi.Depends = lambda dep: dep
    fake_fastapi.status = types.SimpleNamespace(
        HTTP_400_BAD_REQUEST=400,
        HTTP_401_UNAUTHORIZED=401,
        HTTP_403_FORBIDDEN=403,
    )

    fake_fastapi_responses = types.ModuleType("fastapi.responses")

    class StreamingResponse:
        def __init__(self, body_iterator=None, media_type=None):
            self.body_iterator = body_iterator
            self.media_type = media_type

    fake_fastapi_responses.StreamingResponse = StreamingResponse

    fake_services = types.ModuleType("app.services")

    class GeminiClient:
        AVAILABLE_MODELS = ["gemini-2.5-pro"]

        @staticmethod
        async def list_native_models(api_key):
            return {"api_key": api_key, "models": ["gemini-2.5-pro"]}

    fake_services.GeminiClient = GeminiClient

    fake_utils = types.ModuleType("app.utils")

    async def protect_from_abuse(*args, **kwargs):
        return None

    fake_utils.protect_from_abuse = protect_from_abuse
    fake_utils.log = lambda *args, **kwargs: None

    fake_response = types.ModuleType("app.utils.response")
    fake_response.openAI_from_Gemini = lambda *args, **kwargs: {"ok": True}

    fake_auth = types.ModuleType("app.utils.auth")
    fake_auth.custom_verify_password = lambda: None

    fake_request_helpers = types.ModuleType("app.api.request_helpers")
    fake_request_helpers.is_gemini_request = (
        lambda request: getattr(request, "format_type", None) == "gemini"
    )
    fake_request_helpers.build_request_cache_key = (
        lambda request, is_gemini: "cache-key"
    )
    fake_request_helpers.ensure_model_available = (
        lambda model, available_models: None
    )

    async def wait_for_existing_task(*args, **kwargs):
        return None

    fake_request_helpers.wait_for_existing_task = wait_for_existing_task
    fake_request_helpers.create_processing_task = lambda *args, **kwargs: None

    fake_orchestration = types.ModuleType("app.api.orchestration_helpers")

    async def get_cached_response_or_none(*args, **kwargs):
        return None

    async def reuse_or_wait_active_request(*args, **kwargs):
        return None

    def register_active_request_if_needed(*args, **kwargs):
        return None

    async def await_process_task_result(**kwargs):
        return {"awaited": True}

    fake_orchestration.get_cached_response_or_none = get_cached_response_or_none
    fake_orchestration.reuse_or_wait_active_request = reuse_or_wait_active_request
    fake_orchestration.register_active_request_if_needed = register_active_request_if_needed
    fake_orchestration.await_process_task_result = await_process_task_result

    fake_protocol_handlers = types.ModuleType("app.api.protocol_handlers")

    async def handle_responses_request(*args, **kwargs):
        return {"protocol": "responses"}

    async def handle_claude_messages_request(*args, **kwargs):
        return {"protocol": "claude"}

    fake_protocol_handlers.handle_responses_request = handle_responses_request
    fake_protocol_handlers.handle_claude_messages_request = handle_claude_messages_request

    fake_stream_handlers = types.ModuleType("app.api.stream_handlers")
    fake_stream_handlers.process_stream_request = lambda **kwargs: None

    fake_nonstream_handlers = types.ModuleType("app.api.nonstream_handlers")
    fake_nonstream_handlers.process_request = lambda **kwargs: None
    fake_nonstream_handlers.process_nonstream_with_keepalive_stream = (
        lambda **kwargs: None
    )

    fake_vertex_request_adapter = types.ModuleType("app.api.vertex_request_adapter")
    fake_vertex_request_adapter.build_vertex_openai_request = (
        lambda request: {"vertex_request_for": request.model}
    )

    fake_model_handlers = types.ModuleType("app.api.model_handlers")
    fake_model_handlers.build_aistudio_model_list = (
        lambda available, whitelist, blocked: {
            "models": available,
            "whitelist": whitelist,
            "blocked": blocked,
        }
    )

    fake_embedding_handlers = types.ModuleType("app.api.embedding_handlers")

    async def create_embeddings_with_key(*args, **kwargs):
        return {"embedding": True}

    async def handle_vector_query(*args, **kwargs):
        return {"vector_query": True}

    async def handle_vector_insert(*args, **kwargs):
        return {"vector_insert": True}

    fake_embedding_handlers.create_embeddings_with_key = create_embeddings_with_key
    fake_embedding_handlers.handle_vector_query = handle_vector_query
    fake_embedding_handlers.handle_vector_insert = handle_vector_insert

    fake_schemas = types.ModuleType("app.models.schemas")
    for name in [
        "ChatCompletionRequest",
        "ChatCompletionResponse",
        "ModelList",
        "EmbeddingRequest",
        "EmbeddingResponse",
    ]:
        setattr(fake_schemas, name, type(name, (), {}))

    class AIRequest:
        def __init__(self, payload=None, model=None, stream=False, format_type=None):
            self.payload = payload
            self.model = model
            self.stream = stream
            self.format_type = format_type

    class ChatRequestGemini:
        pass

    fake_schemas.AIRequest = AIRequest
    fake_schemas.ChatRequestGemini = ChatRequestGemini

    fake_embedding = types.ModuleType("app.services.embedding")
    fake_embedding.EmbeddingClient = type("EmbeddingClient", (), {})

    fake_settings = types.ModuleType("app.config.settings")
    fake_settings.WHITELIST_USER_AGENT = []
    fake_settings.WHITELIST_MODELS = ["gemini-2.5-pro"]
    fake_settings.BLOCKED_MODELS = []
    fake_settings.MAX_REQUESTS_PER_MINUTE = 10
    fake_settings.MAX_REQUESTS_PER_DAY_PER_IP = 20
    fake_settings.PUBLIC_MODE = False
    fake_settings.NONSTREAM_KEEPALIVE_ENABLED = False
    fake_settings.ENABLE_VERTEX = False

    fake_vertex_chat_api = types.ModuleType("app.vertex.routes.chat_api")

    async def chat_completions(http_request, vertex_request, current_api_key):
        return {
            "http_request": http_request,
            "vertex_request": vertex_request,
            "current_api_key": current_api_key,
        }

    fake_vertex_chat_api.chat_completions = chat_completions

    fake_vertex_models_api = types.ModuleType("app.vertex.routes.models_api")

    async def list_models(request, current_api_key):
        return {"request": request, "current_api_key": current_api_key}

    fake_vertex_models_api.list_models = list_models

    fake_vertex_routes = types.ModuleType("app.vertex.routes")
    fake_vertex_routes.chat_api = fake_vertex_chat_api
    fake_vertex_routes.models_api = fake_vertex_models_api

    sys.modules.update(
        {
            "app": fake_app,
            "app.api": fake_api_pkg,
            "app.config": fake_config_pkg,
            "fastapi": fake_fastapi,
            "fastapi.responses": fake_fastapi_responses,
            "app.services": fake_services,
            "app.utils": fake_utils,
            "app.utils.response": fake_response,
            "app.utils.auth": fake_auth,
            "app.api.request_helpers": fake_request_helpers,
            "app.api.orchestration_helpers": fake_orchestration,
            "app.api.protocol_handlers": fake_protocol_handlers,
            "app.api.stream_handlers": fake_stream_handlers,
            "app.api.nonstream_handlers": fake_nonstream_handlers,
            "app.api.vertex_request_adapter": fake_vertex_request_adapter,
            "app.api.model_handlers": fake_model_handlers,
            "app.api.embedding_handlers": fake_embedding_handlers,
            "app.models.schemas": fake_schemas,
            "app.services.embedding": fake_embedding,
            "app.config.settings": fake_settings,
            "app.vertex.routes": fake_vertex_routes,
            "app.vertex.routes.chat_api": fake_vertex_chat_api,
            "app.vertex.routes.models_api": fake_vertex_models_api,
        }
    )

    for name in [
        "app.api.route_runtime",
        "app.api.chat_orchestrator",
        "app.api.chat_routes",
        "app.api.model_routes",
        "app.api.embedding_routes",
        "app.api.protocol_routes",
        "app.api.routes",
    ]:
        sys.modules.pop(name, None)

    spec = importlib.util.spec_from_file_location(
        "app.api.routes",
        ROOT / "app/api/routes.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, fake_settings


class RoutesSmokeTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_aistudio_chat_completions_cached_path(self):
        module, settings = load_routes_module()
        settings.PUBLIC_MODE = False

        route_runtime = sys.modules["app.api.route_runtime"]

        class Manager:
            def get(self, key):
                return None

            def add(self, key, task):
                raise AssertionError("should not add")

            def remove(self, key):
                raise AssertionError("should not remove")

        route_runtime.active_requests_manager = Manager()

        async def fake_cache(*args, **kwargs):
            return {"cached": True}

        sys.modules["app.api.chat_orchestrator"].get_cached_response_or_none = fake_cache

        request = types.SimpleNamespace(
            model="gemini-2.5-pro",
            stream=False,
            format_type=None,
        )
        result = await module.aistudio_chat_completions(request, http_request=None)
        self.assertEqual(result, {"cached": True})

    async def test_protocol_routes_delegate(self):
        module, _ = load_routes_module()
        responses = await module.responses_api({"model": "gemini-2.5-pro"}, None)
        claude = await module.claude_messages({"model": "gemini-2.5-pro"}, None)
        self.assertEqual(responses["protocol"], "responses")
        self.assertEqual(claude["protocol"], "claude")

    async def test_vertex_chat_completions_uses_adapter(self):
        module, _ = load_routes_module()
        route_runtime = sys.modules["app.api.route_runtime"]
        route_runtime.current_api_key = "vertex-key"
        request = types.SimpleNamespace(model="gemini-2.5-pro")
        result = await module.vertex_chat_completions(request, http_request="req")
        self.assertEqual(
            result["vertex_request"]["vertex_request_for"],
            "gemini-2.5-pro",
        )
        self.assertEqual(result["current_api_key"], "vertex-key")

    async def test_list_models_switches_between_backends(self):
        module, settings = load_routes_module()
        route_runtime = sys.modules["app.api.route_runtime"]
        route_runtime.current_api_key = "vertex-key"

        settings.ENABLE_VERTEX = False
        aistudio = await module.list_models(request="req")
        self.assertEqual(aistudio["models"], ["gemini-2.5-pro"])

        settings.ENABLE_VERTEX = True
        vertex = await module.list_models(request="req")
        self.assertEqual(vertex["current_api_key"], "vertex-key")

    async def test_embedding_and_vector_routes_delegate(self):
        module, _ = load_routes_module()
        route_runtime = sys.modules["app.api.route_runtime"]
        route_runtime.key_manager = object()

        embedding = await module.create_embedding(request={}, http_request=None)
        vector_query = await module.vector_query(request=None)
        vector_insert = await module.vector_insert(request=None)

        self.assertEqual(embedding, {"embedding": True})
        self.assertEqual(vector_query, {"vector_query": True})
        self.assertEqual(vector_insert, {"vector_insert": True})

    async def test_gemini_routes_use_runtime_key_manager_and_adapter(self):
        module, _ = load_routes_module()
        route_runtime = sys.modules["app.api.route_runtime"]

        class KeyManager:
            async def get_available_key(self):
                return "gemini-key"

        route_runtime.key_manager = KeyManager()
        route_runtime.active_requests_manager = types.SimpleNamespace(
            get=lambda key: None,
            add=lambda key, task: None,
            remove=lambda key: None,
        )
        listed = await module.gemini_list_models(request="req")
        self.assertEqual(listed["api_key"], "gemini-key")

        payload = sys.modules["app.models.schemas"].ChatRequestGemini()
        response = await module.gemini_chat_completions(
            request="req",
            model_and_responseType="models/gemini-2.5-pro:streamGenerateContent",
            payload=payload,
        )
        self.assertEqual(response, {"awaited": True})


if __name__ == "__main__":
    unittest.main()

