from typing import Union

from fastapi import APIRouter, Depends, Request

import app.config.settings as settings
from app.api import route_runtime
from app.api.chat_orchestrator import handle_aistudio_chat_completion
from app.api.nonstream_handlers import (
    process_nonstream_with_keepalive_stream,
    process_request,
)
from app.api.stream_handlers import process_stream_request
from app.api.vertex_request_adapter import build_vertex_openai_request
from app.models.schemas import AIRequest, ChatCompletionRequest, ChatCompletionResponse
from app.services import GeminiClient
from app.utils.auth import custom_verify_password
from app.vertex.routes import chat_api


router = APIRouter()


@router.post("/aistudio/chat/completions", response_model=ChatCompletionResponse)
async def aistudio_chat_completions(
    request: Union[ChatCompletionRequest, AIRequest],
    http_request: Request,
    _=Depends(custom_verify_password),
    _2=Depends(route_runtime.verify_user_agent),
):
    return await handle_aistudio_chat_completion(
        request=request,
        http_request=http_request,
        runtime=route_runtime,
        available_models=GeminiClient.AVAILABLE_MODELS,
        process_stream_request=process_stream_request,
        process_nonstream_with_keepalive_stream=process_nonstream_with_keepalive_stream,
        process_request=process_request,
    )


@router.post("/vertex/chat/completions", response_model=ChatCompletionResponse)
async def vertex_chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    _dp=Depends(custom_verify_password),
    _du=Depends(route_runtime.verify_user_agent),
):
    vertex_request = build_vertex_openai_request(request)
    assert route_runtime.current_api_key is not None
    return await chat_api.chat_completions(
        http_request,
        vertex_request,
        route_runtime.current_api_key,
    )


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    _dp=Depends(custom_verify_password),
    _du=Depends(route_runtime.verify_user_agent),
):
    if settings.ENABLE_VERTEX:
        return await vertex_chat_completions(request, http_request, _dp, _du)
    return await aistudio_chat_completions(request, http_request, _dp, _du)
