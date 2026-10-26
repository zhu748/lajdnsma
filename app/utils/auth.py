"""面板 / WEB 密码校验。"""
import hmac
from typing import Optional
from fastapi import HTTPException, Header, Query
import app.config.settings as settings


def password_matches(candidate, expected) -> bool:
    """Fail closed and compare UTF-8 bytes, including non-ASCII web passwords."""
    if not isinstance(candidate, str) or not isinstance(expected, str) or not candidate or not expected:
        return False
    try:
        return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        return False


def extract_bearer_token(authorization) -> Optional[str]:
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.partition(" ")
    return token.lstrip(" ") if separator and scheme.lower() == "bearer" else None


# 自定义密码校验依赖函数
async def custom_verify_password(
    authorization: Optional[str] = Header(
        None, description="OpenAI 格式请求 Key, 格式: Bearer sk-xxxx"
    ),
    x_api_key: Optional[str] = Header(
        None, description="Claude/Anthropic 格式请求 Key, 从请求头 x-api-key 获取"
    ),
    x_goog_api_key: Optional[str] = Header(
        None, description="Gemini 格式请求 Key, 从请求头 x-goog-api-key 获取"
    ),
    key: Optional[str] = Query(
        None, description="Gemini 格式请求 Key, 从查询参数 key 获取"
    ),
    alt: Optional[str] = None,
):
    """Accept protocol-specific keys or a Bearer token, then verify PASSWORD."""
    client_provided_api_key: Optional[str] = None

    # 提取客户端提供的 Key
    if x_api_key:
        client_provided_api_key = x_api_key
    elif x_goog_api_key:
        client_provided_api_key = x_goog_api_key
    elif key:
        client_provided_api_key = key
    else:
        client_provided_api_key = extract_bearer_token(authorization)

    # 进行校验和比对
    # Hardening: 改用常量时间比较（hmac.compare_digest），防止通过响应
    # 时间差异逐字节猜测密码的时序侧信道攻击。
    if not password_matches(
        client_provided_api_key, settings.PASSWORD
    ):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid token")


def verify_web_password(password: str):
    return password_matches(password, settings.WEB_PASSWORD)
