from fastapi import APIRouter, Depends, HTTPException, Request

import app.config.settings as settings
from app.api import route_runtime
from app.api.embedding_handlers import (
    create_embeddings_with_key,
    handle_vector_insert,
    handle_vector_query,
)
from app.models.schemas import EmbeddingRequest, EmbeddingResponse
from app.services.embedding import EmbeddingClient
from app.utils import log, protect_from_abuse
from app.utils.auth import custom_verify_password


router = APIRouter()


@router.post("/v1/embeddings", response_model=EmbeddingResponse)
async def create_embedding(
    request: EmbeddingRequest,
    http_request: Request,
    _du=Depends(route_runtime.verify_user_agent),
    _dp=Depends(custom_verify_password),
):
    await protect_from_abuse(
        http_request,
        settings.MAX_REQUESTS_PER_MINUTE,
        settings.MAX_REQUESTS_PER_DAY_PER_IP,
    )
    try:
        assert route_runtime.key_manager is not None
        return await create_embeddings_with_key(
            request,
            route_runtime.key_manager,
            EmbeddingClient,
        )
    except Exception as e:
        log("ERROR", f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@router.post("/api/vector/query")
async def vector_query(
    request: Request,
    _du=Depends(route_runtime.verify_user_agent),
    _dp=Depends(custom_verify_password),
):
    try:
        assert route_runtime.key_manager is not None
        return await handle_vector_query(
            request,
            route_runtime.key_manager,
            EmbeddingRequest,
            EmbeddingClient,
            log,
        )
    except Exception as e:
        log("ERROR", f"An unexpected error occurred during vector query: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@router.post("/api/vector/insert")
async def vector_insert(
    request: Request,
    _du=Depends(route_runtime.verify_user_agent),
    _dp=Depends(custom_verify_password),
):
    try:
        assert route_runtime.key_manager is not None
        return await handle_vector_insert(
            request,
            route_runtime.key_manager,
            EmbeddingRequest,
            EmbeddingClient,
            log,
        )
    except Exception as e:
        log("ERROR", f"An unexpected error occurred during vector insert: {e}")
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")
