from fastapi import APIRouter

from app.api.chat_routes import (
    aistudio_chat_completions,
    chat_completions,
    router as chat_router,
    vertex_chat_completions,
)
from app.api.embedding_routes import (
    create_embedding,
    router as embedding_router,
    vector_insert,
    vector_query,
)
from app.api.model_routes import (
    aistudio_list_models,
    claude_list_models,
    gemini_list_models,
    list_models,
    router as model_router,
    vertex_list_models,
)
from app.api.protocol_routes import (
    claude_messages,
    gemini_chat_completions,
    responses_api,
    router as protocol_router,
)
from app.api.route_runtime import get_cache, init_runtime, verify_user_agent


router = APIRouter()
router.include_router(model_router)
router.include_router(chat_router)
router.include_router(protocol_router)
router.include_router(embedding_router)


def init_router(
    _key_manager,
    _response_cache_manager,
    _active_requests_manager,
    _safety_settings,
    _safety_settings_g2,
    _current_api_key,
):
    # Cleanup: 曾接收 11 个参数，其中 5 个（fake_streaming、interval、
    # password、两个限流值）是只存不读的死参数，已删除。
    init_runtime(
        _key_manager,
        _response_cache_manager,
        _active_requests_manager,
        _safety_settings,
        _safety_settings_g2,
        _current_api_key,
    )


__all__ = [
    "router",
    "init_router",
    "verify_user_agent",
    "get_cache",
    "aistudio_list_models",
    "claude_list_models",
    "vertex_list_models",
    "list_models",
    "aistudio_chat_completions",
    "vertex_chat_completions",
    "chat_completions",
    "responses_api",
    "claude_messages",
    "gemini_list_models",
    "gemini_chat_completions",
    "create_embedding",
    "vector_query",
    "vector_insert",
]
