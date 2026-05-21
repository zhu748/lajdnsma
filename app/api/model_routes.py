from fastapi import APIRouter, Depends, HTTPException, Request, status

import app.config.settings as settings
from app.api import route_runtime
from app.api.model_handlers import build_aistudio_model_list
try:
    from app.api.model_handlers import build_claude_model_list
except ImportError:
    def build_claude_model_list(available_models, whitelist_models, blocked_models):
        filtered_models = [
            model
            for model in available_models
            if (not whitelist_models or model in whitelist_models)
            and model not in blocked_models
        ]
        return {"data": [{"type": "model", "id": model} for model in filtered_models]}
from app.models.schemas import ModelList
from app.services import GeminiClient
from app.utils.auth import custom_verify_password
from app.vertex.routes import models_api


router = APIRouter()


@router.get("/aistudio/models", response_model=ModelList)
async def aistudio_list_models(
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    return build_aistudio_model_list(
        GeminiClient.AVAILABLE_MODELS,
        settings.WHITELIST_MODELS,
        settings.BLOCKED_MODELS,
    )


@router.get("/vertex/models", response_model=ModelList)
async def vertex_list_models(
    request: Request,
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    assert route_runtime.current_api_key is not None
    return await models_api.list_models(request, route_runtime.current_api_key)


@router.get("/claude/v1/models")
@router.get("/anthropic/v1/models")
async def claude_list_models(
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    return build_claude_model_list(
        GeminiClient.AVAILABLE_MODELS,
        settings.WHITELIST_MODELS,
        settings.BLOCKED_MODELS,
    )


@router.get("/v1/models")
@router.get("/models")
async def list_models(
    request: Request,
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    if is_gemini_native_model_request(request):
        return await gemini_list_models(request, _, _2)

    if is_claude_model_request(request):
        return build_claude_model_list(
            GeminiClient.AVAILABLE_MODELS,
            settings.WHITELIST_MODELS,
            settings.BLOCKED_MODELS,
        )

    if settings.ENABLE_VERTEX:
        return await vertex_list_models(request, _, _2)
    return await aistudio_list_models(_, _2)


@router.get("/v1beta/models")
@router.get("/gemini/v1beta/models")
@router.get("/gemini/v1/models")
async def gemini_list_models(
    request: Request,
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    assert route_runtime.key_manager is not None
    api_key = await route_runtime.key_manager.get_available_key()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No valid API keys available.",
        )
    return await GeminiClient.list_native_models(api_key)


def is_claude_model_request(request: Request) -> bool:
    if not hasattr(request, "headers"):
        return False
    return bool(
        request.headers.get("anthropic-version")
        or request.headers.get("x-api-key")
        or request.url.path.startswith(("/claude/", "/anthropic/"))
    )


def is_gemini_native_model_request(request: Request) -> bool:
    if not hasattr(request, "headers"):
        return False
    return bool(
        request.headers.get("x-goog-api-key")
        or getattr(request, "query_params", {}).get("key")
        or request.url.path.startswith("/gemini/")
        or request.url.path.startswith("/v1beta/")
    )
