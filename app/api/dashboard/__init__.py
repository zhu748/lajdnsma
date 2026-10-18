"""管理面板 API（/api 前缀）包入口。

对外只导出 dashboard_router 与 init_dashboard_router（app/api/__init__.py
与 app/main.py 的装配契约）。端点按职责拆分到子模块，导入即完成路由注册：

- runtime:        路由器、运行时全局引用、init_dashboard_router、Vertex 重初始化
- auth:           统一鉴权依赖（Header/Query 与 JSON body 两种形态）
- stats:          GET /dashboard-data、POST /reset-stats
- config_updates: POST /update-config（配置项 handler 注册表分派）
- model_mapping:  POST /update-responses-model-mapping、POST /update-claude-model-mapping
- api_keys:       密钥检测 / 进度 / 清理 / 导出端点与后台检测任务
"""
from app.api.dashboard.runtime import dashboard_router, init_dashboard_router

# 导入端点模块以注册路由（必须放在 runtime 之后）
from app.api.dashboard import (  # noqa: F401  (side-effect: route registration)
    api_keys,
    config_updates,
    model_mapping,
    stats,
)

__all__ = ["dashboard_router", "init_dashboard_router"]
