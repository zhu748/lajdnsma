from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from app.models.schemas import ErrorResponse
from app.services import GeminiClient
from app.utils import (
    APIKeyManager,
    test_api_key,
    ResponseCacheManager,
    ActiveRequestsManager,
    check_version,
    schedule_cache_cleanup,
    shutdown_scheduler,
    handle_exception,
    log,
)
from app.config.persistence import save_settings, load_settings
from app.api import router, init_router, dashboard_router, init_dashboard_router
from app.vertex.vertex_ai_init import init_vertex_ai
from app.vertex.credentials_manager import CredentialManager
from app.utils.http_client import close_async_client
import app.config.settings as settings
from app.config.safety import SAFETY_SETTINGS, SAFETY_SETTINGS_G2
import asyncio
import sys
import pathlib
import os
import webbrowser

# 设置模板目录
BASE_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Hardening: the previous `FastAPI(limit="50M")` kwarg is not a real
# Starlette/FastAPI parameter — it was silently ignored, so the
# process had no request body size limit at all (a DoS vector: a
# malicious client could submit a multi-GB body and OOM the worker).
# We now enforce an explicit body size cap via middleware (~50 MB
# matches the previous intent).
MAX_REQUEST_BODY_BYTES = 50 * 1024 * 1024  # 50 MB

# 后台任务引用持有：CPython 的 asyncio.create_task 只持弱引用，
# 不持引用的长耗时任务（如密钥后台检查，每 key 间隔 2-5s，可能跑
# 几分钟）可能在任一 GC 周期被静默回收中断。模块级 set + done
# callback 保证任务存活到完成。
_background_tasks: set = set()


def _spawn_background_task(coro) -> asyncio.Task:
    """创建后台任务并持有强引用，防止被垃圾回收静默中断。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# --------------- 生命周期（lifespan）---------------
# Modernization: FastAPI 0.109+ 已弃用 @app.on_event，这里改用官方
# 推荐的 lifespan 上下文管理器（startup 逻辑在 yield 前，shutdown
# 逻辑在 yield 后），消除 DeprecationWarning 且面向未来兼容。
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    yield
    await _shutdown()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def enforce_body_size_limit(request: Request, call_next):
    """Reject request bodies larger than MAX_REQUEST_BODY_BYTES.

    We check `Content-Length` for requests that declare it.  Requests
    that use chunked transfer encoding (no Content-Length) are
    streamed; the body itself is not pre-read, but FastAPI/Starlette
    will still parse it during routing — that's a known limitation
    that we accept for now (chunked clients are rare for chat APIs).
    """
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"error": {"message": "Request body too large.", "type": "invalid_request_error"}},
                )
        except (TypeError, ValueError):
            pass
    return await call_next(request)


# --------------- Response header hardening middleware ---------------
# Real OpenAI / Anthropic responses don't carry uvicorn's default
# `Server: uvicorn` header, and they don't send `X-Powered-By` at
# all.  Their absence + a neutral `Server` value removes one of the
# easiest-to-probe markers that an upstream (or client) can use to
# identify this as a uvicorn-hosted proxy.
@app.middleware("http")
async def harden_response_headers(request: Request, call_next):
    response = await call_next(request)
    # Replace uvicorn's default `Server: uvicorn` with a neutral value.
    # `cloudflare` is a safe choice because it's also commonly seen
    # in front of real OpenAI/Anthropic deployments.
    response.headers["Server"] = "cloudflare"
    # Strip FastAPI/Starlette-added disclosure headers if present.
    # CRITICAL FIX: 旧写法 `response.headers.pop("X-Powered-By", None)`
    # 调用了 MutableHeaders 根本不存在的 .pop() 方法 —— Starlette 的
    # MutableHeaders 只有 __delitem__，没有 pop。该异常被全局异常处
    # 理器吞掉后，**每个请求都返回 500**（冒烟测试发现）。改为
    # membership check + del，兼容所有 Starlette 版本。
    if "x-powered-by" in response.headers:
        del response.headers["x-powered-by"]
    return response


# --------------- CORS 中间件 ---------------
# Hardening: previously `allow_methods=["*"]` +
# `allow_headers=["*"]` was too permissive — real OpenAI/Anthropic
# CORS responses are a restricted whitelist (POST, OPTIONS +
# Authorization, Content-Type, Accept).  The wide `*` is itself a
# fingerprint because the OPTIONS preflight response then advertises
# every method.  We also turn off `allow_credentials` unless the
# operator explicitly sets it (proxy traffic doesn't need cookies).
if settings.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

# --------------- 全局实例 ---------------
load_settings()
# 初始化API密钥管理器
key_manager = APIKeyManager()

# 创建全局缓存字典，将作为缓存管理器的内部存储
response_cache = {}

# 初始化缓存管理器，使用全局字典作为存储
response_cache_manager = ResponseCacheManager(
    expiry_time=settings.CACHE_EXPIRY_TIME,
    max_entries=settings.MAX_CACHE_ENTRIES,
    cache_dict=response_cache,
)

# 活跃请求池 - 将作为活跃请求管理器的内部存储
active_requests_pool = {}

# 初始化活跃请求管理器
active_requests_manager = ActiveRequestsManager(requests_pool=active_requests_pool)

SKIP_CHECK_API_KEY = os.environ.get("SKIP_CHECK_API_KEY", "").lower() == "true"

# --------------- 工具函数 ---------------
# @app.middleware("http")
# async def log_requests(request: Request, call_next):
#     """
#     DEBUG用，接收并打印请求内容
#     """
#     log('info', f"接收到请求: {request.method} {request.url}")
#     try:
#         body = await request.json()
#         log('info', f"请求体: {body}")
#     except Exception:
#         log('info', "请求体不是 JSON 格式或者为空")

#     response = await call_next(request)
#     return response


async def check_remaining_keys_async(keys_to_check: list, initial_invalid_keys: list):
    """
    在后台异步检查剩余的 API 密钥。
    """
    import random as _random

    local_invalid_keys = []
    found_valid_keys = False

    log("info", " 开始在后台检查剩余 API Key 是否有效")
    for idx, key in enumerate(keys_to_check):
        is_valid = await test_api_key(key)
        if is_valid:
            if key not in key_manager.api_keys:  # 避免重复添加
                key_manager.api_keys.append(key)
                found_valid_keys = True
            # log('info', f"API Key {key[:8]}... 有效")
        else:
            local_invalid_keys.append(key)
            log("warning", f" API Key key#{hash(key) & 0xFFFFFF:06x} 无效")

        # Hardening: inter-probe delay.  A 0.05s gap between sequential
        # `:models` list calls is a textbook key-enumeration fingerprint
        # (50 probes in ~3s from one IP == bot).  Use a 2-5s uniform
        # jitter so the probe sequence resembles a human re-checking
        # individual keys, not a script iterating a list.
        if idx < len(keys_to_check) - 1:
            await asyncio.sleep(_random.uniform(2.0, 5.0))

    if found_valid_keys:
        key_manager._reset_key_stack()  # 如果找到新的有效key，重置栈

    # 合并所有无效密钥 (初始无效 + 后台检查出的无效)
    combined_invalid_keys = list(set(initial_invalid_keys + local_invalid_keys))

    # 获取当前设置中的无效密钥
    current_invalid_keys_str = settings.INVALID_API_KEYS or ""
    current_invalid_keys_set = set(
        k.strip() for k in current_invalid_keys_str.split(",") if k.strip()
    )

    # 更新无效密钥集合
    new_invalid_keys_set = current_invalid_keys_set.union(set(combined_invalid_keys))

    # 只有当无效密钥列表发生变化时才保存
    if new_invalid_keys_set != current_invalid_keys_set:
        settings.INVALID_API_KEYS = ",".join(sorted(list(new_invalid_keys_set)))
        save_settings()

    log("info", f"密钥检查任务完成。当前总可用密钥数量: {len(key_manager.api_keys)}")


# 设置全局异常处理
sys.excepthook = handle_exception

# --------------- 事件处理 ---------------


async def _startup():
    # 首先加载持久化设置，确保所有配置都是最新的
    load_settings()

    # 重新加载vertex配置，确保获取到最新的持久化设置
    import app.vertex.config as vertex_config

    vertex_config.reload_config()

    # 初始化CredentialManager
    credential_manager_instance = CredentialManager()
    # 添加到应用程序状态
    app.state.credential_manager = credential_manager_instance

    # 初始化Vertex AI服务
    await init_vertex_ai(credential_manager=credential_manager_instance)
    schedule_cache_cleanup(response_cache_manager, active_requests_manager)
    # 检查版本
    await check_version()

    # 密钥检查
    initial_keys = key_manager.api_keys.copy()
    key_manager.api_keys = []  # 清空，等待检查结果
    first_valid_key = None
    initial_invalid_keys = []
    keys_to_check_later = []

    # 阻塞式查找第一个有效密钥
    for index, key in enumerate(initial_keys):
        is_valid = await test_api_key(key)
        if is_valid:
            log("info", f"找到第一个有效密钥: key#{hash(key) & 0xFFFFFF:06x}")
            first_valid_key = key
            key_manager.api_keys.append(key)  # 添加到管理器
            key_manager._reset_key_stack()
            # 将剩余的key放入后台检查列表
            keys_to_check_later = initial_keys[index + 1 :]
            break  # 找到即停止
        else:
            log("warning", f"密钥 key#{hash(key) & 0xFFFFFF:06x} 无效")
            initial_invalid_keys.append(key)

    if not first_valid_key:
        log("error", "启动时未能找到任何有效 API 密钥！")
        keys_to_check_later = []  # 没有有效key，无需后台检查
    else:
        # 使用第一个有效密钥加载模型
        try:
            all_models = await GeminiClient.list_available_models(first_valid_key)
            GeminiClient.AVAILABLE_MODELS = [
                model.replace("models/", "") for model in all_models
            ]
            log("info", f"使用密钥 key#{hash(first_valid_key) & 0xFFFFFF:06x} 加载可用模型成功")
        except Exception as e:
            log(
                "warning",
                f"使用密钥 key#{hash(first_valid_key) & 0xFFFFFF:06x} 加载可用模型失败",
                extra={"error_message": str(e)},
            )

    if not SKIP_CHECK_API_KEY:
        # 创建后台任务检查剩余密钥
        # （_spawn_background_task 持有强引用，防止任务被 GC 静默中断）
        if keys_to_check_later:
            _spawn_background_task(
                check_remaining_keys_async(keys_to_check_later, initial_invalid_keys)
            )
        else:
            # 如果没有需要后台检查的key，也要处理初始无效key
            current_invalid_keys_str = settings.INVALID_API_KEYS or ""
            current_invalid_keys_set = set(
                k.strip() for k in current_invalid_keys_str.split(",") if k.strip()
            )
            new_invalid_keys_set = current_invalid_keys_set.union(
                set(initial_invalid_keys)
            )
            if new_invalid_keys_set != current_invalid_keys_set:
                settings.INVALID_API_KEYS = ",".join(sorted(list(new_invalid_keys_set)))
                save_settings()
                log(
                    "info",
                    f"更新初始无效密钥列表完成，总无效密钥数: {len(new_invalid_keys_set)}",
                )

    else:  # 跳过检查
        log("info", "跳过 API 密钥检查")
        key_manager.api_keys.extend(keys_to_check_later)
        key_manager._reset_key_stack()

    # 初始化路由器
    # （已清理 5 个只存不读的死参数：fake_streaming/interval/password/限流值，
    # 各处实际都是实时读 settings.*）
    init_router(
        key_manager,
        response_cache_manager,
        active_requests_manager,
        SAFETY_SETTINGS,
        SAFETY_SETTINGS_G2,
        first_valid_key,
    )

    # 初始化仪表盘路由器
    init_dashboard_router(
        key_manager,
        response_cache_manager,
        active_requests_manager,
        credential_manager_instance,
    )

    # 启动浏览器
    open_browser()


async def _shutdown():
    # Hardening: previously only closed the shared httpx client.
    # Orphan background schedulers + threads would keep running
    # after the HTTP server stopped, raising
    # `RuntimeError: Event loop is closed` and leaking file
    # descriptors.  We now stop the AsyncIOScheduler + stats worker
    # thread + close httpx in a proper order.
    try:
        await shutdown_scheduler()
    except Exception as e:
        log("error", f"shutdown_scheduler error: {str(e)}")
    # 释放 Vertex OpenAI Direct 路径缓存的客户端连接池
    try:
        from app.vertex.routes.chat_api import close_cached_openai_clients

        await close_cached_openai_clients()
    except Exception as e:
        log("error", f"close_cached_openai_clients error: {str(e)}")
    await close_async_client()


# --------------- 异常处理 ---------------


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler.

    Hardened: previously returned `ErrorResponse(message=str(exc))` to
    the client, which leaks internal stack/error information.  We now
    return only a generic "Internal Server Error" message to the
    client; the full exception is logged internally.
    """
    from app.utils import translate_error

    # Log the full exception internally for debugging
    error_message = translate_error(str(exc))
    extra_log_unhandled_exception = {"status_code": 500, "error_message": error_message}
    log(
        "error",
        f"Unhandled exception: {error_message}",
        extra=extra_log_unhandled_exception,
    )
    # Return a generic message to the client — no internal details.
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            message="Internal Server Error. Please retry later.",
            type="internal_error",
        ).model_dump(),
    )


# --------------- 路由 ---------------

app.include_router(router)
app.include_router(dashboard_router)


# Unauthenticated health endpoint for container orchestrators
# (Docker / k8s liveness probes).  Returns minimal info — no
# version, no key counts, no upstream identifiers — so it's safe
# to expose publicly.
@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}

# 挂载静态文件目录
# Fix: 旧写法用相对路径 "app/templates/assets"，依赖进程启动时的 CWD；
# 从非项目根目录启动会导致 500。与上方 templates 统一用 BASE_DIR 绝对路径。
app.mount(
    "/assets",
    StaticFiles(directory=str(BASE_DIR / "templates" / "assets")),
    name="assets",
)

# 设置根路由路径
dashboard_path = f"/{settings.DASHBOARD_URL}" if settings.DASHBOARD_URL else "/"


@app.api_route(dashboard_path, methods=["GET", "HEAD"], response_class=HTMLResponse)
async def root(request: Request):
    """
    根路由 - 返回静态 HTML 文件
    """
    # 只替换 URL scheme，不用 replace("http", "https") —— 后者会误伤
    # URL 中任意位置的 "http" 子串（如 https://my-http-host/ →
    # https://my-https-host/，主机名被改写）。
    base_url = str(request.base_url)
    if base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]
    api_url = f"{base_url}v1" if base_url.endswith("/") else f"{base_url}/v1"
    # 直接返回 index.html 文件
    return templates.TemplateResponse(
        "index.html", {"request": request, "api_url": api_url}
    )


# --------------- 自动启动浏览器 ---------------
def open_browser():
    """
    检查是否存在可用的浏览器，如果存在，则在默认浏览器中打开应用的 URL。
    此函数会特别检查 Linux 环境下的 'DISPLAY' 环境变量，以避免在无头服务器上出错。
    """
    # 首先，检查是否在无 GUI 的 Linux 环境中
    if os.name == "posix" and not os.environ.get("DISPLAY"):
        log("info", "检测到无 GUI 环境 (缺少 DISPLAY 环境变量)，跳过打开浏览器。")
        return

    try:
        # webbrowser.get() 会在找不到浏览器时抛出 webbrowser.Error
        browser = webbrowser.get()
        if browser:
            log("info", f"找到可用浏览器: {browser.name}。准备打开 URL...")
            webbrowser.open("http://127.0.0.1:7860")
            log("info", "已发送打开浏览器指令: http://127.0.0.1:7860")
        else:
            # 这种情况很少见，但作为备用逻辑
            log("warning", "webbrowser.get() 未返回浏览器实例，跳过打开浏览器。")

    except webbrowser.Error:
        # 捕获找不到浏览器的特定错误
        log("warning", "系统中未找到可用的浏览器，跳过自动打开。")
    # 捕获错误, 失败也不重新抛出异常
    # 后果也只是不会自动打开浏览器，不会对调用处产生影响
    except Exception as e:
        # 捕获其他可能的异常
        log("error", f"尝试打开浏览器时发生未知错误: {e}")
