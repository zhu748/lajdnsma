from fastapi import FastAPI, Depends, APIRouter, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse # Not used
# import os # Not used


# Local module imports
from app.vertex.auth import validate_api_key
from app.vertex.credentials_manager import CredentialManager
from app.vertex.vertex_ai_init import init_vertex_ai
from app.utils.logging import vertex_log
from app.config import settings
# import config as app_config # Not directly used in main.py

# Routers
from app.vertex.routes import models_api
from app.vertex.routes import chat_api

app = FastAPI(title="API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS if hasattr(settings, "ALLOWED_ORIGINS") and settings.ALLOWED_ORIGINS else [],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

credential_manager = CredentialManager()
app.state.credential_manager = credential_manager  # Store manager on app state

# Include API routers
app.include_router(models_api.router)
app.include_router(chat_api.router)

# Create router
vertex_router = APIRouter(prefix="/vertex")

# Security scheme
security = HTTPBearer()

# Include vertex_router in the app
app.include_router(vertex_router)


@app.on_event("startup")
async def startup_event():
    try:
        # 检查是否有Google Credentials JSON
        if (
            hasattr(settings, "GOOGLE_CREDENTIALS_JSON")
            and settings.GOOGLE_CREDENTIALS_JSON
        ):
            vertex_log("info", "检测到持久化的Google Credentials JSON，准备加载")
            from app.vertex.credentials_manager import parse_multiple_json_credentials

            parsed_json_objects = parse_multiple_json_credentials(
                settings.GOOGLE_CREDENTIALS_JSON
            )
            if parsed_json_objects:
                loaded_count = credential_manager.load_credentials_from_json_list(
                    parsed_json_objects
                )
                vertex_log(
                    "info",
                    f"从持久化的Google Credentials JSON中加载了{loaded_count}个凭据",
                )

        # 检查是否有Vertex Express API Key
        if (
            hasattr(settings, "VERTEX_EXPRESS_API_KEY")
            and settings.VERTEX_EXPRESS_API_KEY
        ):
            vertex_log("info", "检测到持久化的Vertex Express API Key")

        # 初始化Vertex AI
        if await init_vertex_ai(credential_manager):
            vertex_log(
                "info",
                "Vertex AI credential and model config initialization check completed successfully.",
            )

            # 刷新模型配置缓存
            from app.vertex.model_loader import refresh_models_config_cache

            refresh_success = await refresh_models_config_cache()
            if refresh_success:
                vertex_log("info", "成功刷新模型配置缓存")
            else:
                vertex_log("warning", "刷新模型配置缓存失败")
        else:
            vertex_log(
                "error",
                "Failed to initialize a fallback Vertex AI client. API will likely fail.",
            )
    except Exception as e:
        vertex_log("error", f"启动时初始化Vertex AI服务出错: {str(e)}")


@app.get("/")
async def root():
    # Hardening: previously returned the literal string
    # "OpenAI to Gemini Adapter is running." which directly
    # disclosed the project identity to any caller.  We now
    # return a minimal status object with no project-identifying
    # text.
    return {"status": "ok"}


@vertex_router.get("/health")
async def health_check(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Simple health check endpoint for the Vertex AI integration.

    Note: validate_api_key takes a raw token string, not an
    HTTPAuthorizationCredentials object.  We previously passed the
    whole credentials object — which was always truthy, so the
    auth check was effectively bypassed.  Now we extract
    credentials.credentials (the raw bearer token string) and pass
    that.
    """
    # Extract the raw bearer token from the HTTPAuthorizationCredentials
    # wrapper.  validate_api_key takes a string token, not the wrapper.
    token = credentials.credentials if credentials is not None else ""
    if not validate_api_key(token):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"status": "ok"}


@vertex_router.get("/status")
async def status():
    """
    Public status endpoint (no auth required)
    """
    # Hardening: previously echoed a static version string and an
    # explicit list of available endpoints.  We now return only a
    # minimal online status — no version, no endpoint list.
    vertex_log("info", "Status check requested")
    return {"status": "online"}
