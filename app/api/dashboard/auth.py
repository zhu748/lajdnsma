"""管理面板端点的统一鉴权依赖。

历史版本中 8 个端点各自手写密码校验样板（Header/Query 两处逐字重复、
body 三段校验六处重复，且中英文错误消息混用），现统一为两个可复用的
FastAPI 依赖：

- require_dashboard_token: 适用于 GET 端点，从 Authorization: Bearer
  头或 ?password= 查询参数取 token；
- require_body_password:   适用于 POST 端点，从 JSON body 的 password
  字段校验，通过后原样返回 body 供端点继续取用。

错误消息统一为原多数端点使用的中文文案（两个 model mapping 端点此前
为英文文案，前端仅展示 detail 不做文本匹配，统一无兼容风险）。
"""
from typing import Optional

from fastapi import Header, HTTPException, Query

from app.utils.auth import verify_web_password, extract_bearer_token


def _extract_token(authorization: Optional[str], password: Optional[str]) -> Optional[str]:
    """从 Bearer 头或查询参数中提取面板口令。"""
    token = None
    if authorization:
        token = extract_bearer_token(authorization) or authorization
    if not token and password:
        token = password
    return token


async def require_dashboard_token(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    password: Optional[str] = Query(None),
) -> None:
    """GET 端点鉴权：Authorization: Bearer <password> 或 ?password=<password>。

    Hardening（沿用原注释）：此前面板数据/进度端点无鉴权，任何人都能
    拉取统计、日志、密钥池规模——这些信息组合起来可唯一指纹识别本代理。
    """
    token = _extract_token(authorization, password)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not verify_web_password(token):
        raise HTTPException(status_code=401, detail="Authentication failed")


async def require_body_password(body: dict) -> dict:
    """POST 端点鉴权：校验 JSON body 中的 password 字段，通过后返回 body。"""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="请求体格式错误：应为JSON对象")
    password = body.get("password")
    if not password:
        raise HTTPException(status_code=400, detail="缺少密码参数")
    if not isinstance(password, str):
        raise HTTPException(status_code=422, detail="密码参数类型错误：应为字符串")
    if not verify_web_password(password):
        raise HTTPException(status_code=401, detail="密码错误")
    return body
