"""管理面板 API 的运行时装配层。

`dashboard_router` 与四个运行时全局引用（key_manager / 缓存 / 活跃请求 /
凭证管理器）集中在此。历史版本把全部内容塞在单文件 dashboard.py 中
（1362 行、路由 + 配置服务 + 后台任务 + 统计聚合四合一），现按职责拆分：

- runtime（本模块）: 路由器、全局引用、init_dashboard_router、Vertex 重初始化
- auth:               面板鉴权依赖（Header/Query 与 JSON body 两种形态）
- stats:              GET /dashboard-data、POST /reset-stats
- config_updates:     POST /update-config（配置项 handler 注册表分派）
- model_mapping:      Responses / Claude 模型映射端点与校验
- api_keys:           密钥检测 / 进度 / 清理 / 导出端点与后台检测任务

注意：端点模块必须以 `runtime.key_manager` 的属性访问方式读取全局引用
（import 期绑定会在 init_dashboard_router 注入前冻结为 None）。
"""
from fastapi import APIRouter

from app.utils.logging import log
from app.vertex.credentials_manager import CredentialManager
from app.vertex.vertex_ai_init import init_vertex_ai as re_init_vertex_ai_function

# 创建路由器
dashboard_router = APIRouter(prefix="/api", tags=["dashboard"])

# 全局变量引用，将在 init_dashboard_router 中设置（由 app/main.py 启动时注入）
key_manager = None
response_cache_manager = None
active_requests_manager = None
credential_manager = None


def init_dashboard_router(key_mgr, cache_mgr, active_req_mgr, cred_mgr=None):
    """注入运行时全局引用并返回面板路由器。"""
    global key_manager, response_cache_manager, active_requests_manager, credential_manager
    key_manager = key_mgr
    response_cache_manager = cache_mgr
    active_requests_manager = active_req_mgr
    credential_manager = cred_mgr
    return dashboard_router


async def run_blocking_init_vertex():
    """以当前 credential_manager 重新执行 init_vertex_ai（响应凭证 JSON 更新）。"""
    try:
        if credential_manager is None:
            # credential_manager 为 None 时记录警告并创建临时实例
            log("warning", "Credential Manager不存在，将创建一个新的实例用于初始化")
            temp_credential_manager = CredentialManager()
            credentials_count = temp_credential_manager.get_total_credentials()
            log("info", f"临时Credential Manager已创建，包含{credentials_count}个凭证")
            success = await re_init_vertex_ai_function(
                credential_manager=temp_credential_manager
            )
        else:
            credentials_count = credential_manager.get_total_credentials()
            log(
                "info",
                f"使用现有Credential Manager进行初始化，当前有{credentials_count}个凭证",
            )
            success = await re_init_vertex_ai_function(
                credential_manager=credential_manager
            )

        if success:
            log(
                "info",
                "异步重新执行 init_vertex_ai 成功，以响应 Google Credentials JSON 的更新。",
            )
        else:
            log(
                "warning",
                "异步重新执行 init_vertex_ai 失败或未完成，在 Google Credentials JSON 更新后。",
            )
    except Exception as e:
        log("error", f"执行 run_blocking_init_vertex 时出错: {e}")


__all__ = [
    "dashboard_router",
    "init_dashboard_router",
    "key_manager",
    "response_cache_manager",
    "active_requests_manager",
    "credential_manager",
    "run_blocking_init_vertex",
]
