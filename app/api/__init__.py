"""API 路由层门面：聚合业务路由与面板路由的初始化入口。"""
from app.api.routes import router, init_router
from app.api.dashboard import dashboard_router, init_dashboard_router

__all__ = ["router", "init_router", "dashboard_router", "init_dashboard_router"]
