"""Vertex authentication shares the main gateway's password contract."""
from fastapi import HTTPException, Header
from typing import Optional
from app.config import settings
from app.utils.auth import password_matches, extract_bearer_token


def validate_api_key(api_key_to_validate: str) -> bool:
    return password_matches(api_key_to_validate, getattr(settings, "PASSWORD", ""))


async def get_api_key(authorization: Optional[str] = Header(None)):
    token = extract_bearer_token(authorization)
    if not validate_api_key(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid token")
    return token
