from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

from app.api import route_runtime
from app.api.chat_routes import aistudio_chat_completions
from app.api.protocol_handlers import (
    handle_claude_messages_request,
    handle_responses_request,
)
from app.models.schemas import AIRequest, ChatRequestGemini
from app.utils.auth import custom_verify_password


router = APIRouter()


@router.post("/v1/responses")
@router.post("/responses")
async def responses_api(
    payload: dict = Body(...),
    http_request: Request = None,
    _dp=Depends(custom_verify_password),
    _du=Depends(route_runtime.verify_user_agent),
):
    return await handle_responses_request(
        payload,
        http_request,
        _dp,
        _du,
        aistudio_chat_completions,
    )


@router.post("/v1/messages")
@router.post("/claude/v1/messages")
@router.post("/anthropic/v1/messages")
async def claude_messages(
    payload: dict = Body(...),
    http_request: Request = None,
    _dp=Depends(custom_verify_password),
    _du=Depends(route_runtime.verify_user_agent),
):
    return await handle_claude_messages_request(
        payload,
        http_request,
        _dp,
        _du,
        aistudio_chat_completions,
    )


@router.post("/gemini/{api_version:str}/models/{model_and_responseType:path}")
async def gemini_chat_completions(
    request: Request,
    model_and_responseType: str = Path(...),
    key: Optional[str] = Query(None),
    alt: Optional[str] = Query(None, description="sse 或 None"),
    payload: ChatRequestGemini = Body(...),
    _dp=Depends(custom_verify_password),
    _du=Depends(route_runtime.verify_user_agent),
):
    _ = (key, alt)
    is_stream = False
    try:
        model_name, action_type = model_and_responseType.split(":", 1)
        model_name = model_name.removeprefix("models/")
        if action_type == "streamGenerateContent":
            is_stream = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的请求路径") from exc

    gemini_request = AIRequest(
        payload=payload,
        model=model_name,
        stream=is_stream,
        format_type="gemini",
    )
    return await aistudio_chat_completions(gemini_request, request, _dp, _du)
