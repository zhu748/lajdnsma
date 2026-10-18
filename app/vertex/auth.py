"""Vertex 子应用的 Bearer Token 校验（与主 app PASSWORD 一致）。"""
from fastapi import HTTPException, Header
from typing import Optional
from app.config import settings


# Function to validate an API key
# Hardening: previously this only checked that the token was a
# plausible string (non-empty, 4-512 chars, no whitespace) and returned
# True for *anything* that matched — meaning an attacker could pass
# arbitrary junk as the bearer token and it'd be accepted.  We now
# compare the token against `settings.PASSWORD` (the same password used
# by the rest of the app's auth path) and reject anything that doesn't
# match.  This makes the Vertex FastAPI sub-app's auth consistent with
# the main app's `custom_verify_password`.
def validate_api_key(api_key_to_validate: str) -> bool:
    """Validate an inbound bearer token against the configured PASSWORD."""
    if not api_key_to_validate:
        return False
    if not isinstance(api_key_to_validate, str):
        return False
    # Quick sanity check: plausible length and no whitespace.
    if len(api_key_to_validate) < 4 or len(api_key_to_validate) > 512:
        return False
    if any(c.isspace() for c in api_key_to_validate):
        return False
    # Real auth: must match the operator-configured PASSWORD.
    expected = getattr(settings, "PASSWORD", "") or ""
    if not expected:
        # Fail-closed when no password is configured.
        return False
    # Use a constant-time comparison to avoid timing side channels.
    import hmac

    return hmac.compare_digest(api_key_to_validate, expected)


# Dependency for API key validation
async def get_api_key(authorization: Optional[str] = Header(None)):
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Please include 'Authorization: Bearer YOUR_API_KEY' header.",
        )

    # Check if the header starts with "Bearer "
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key format. Use 'Authorization: Bearer YOUR_API_KEY'",
        )

    # Extract the API key
    api_key = authorization.replace("Bearer ", "")

    # Validate the API key
    if not validate_api_key(api_key):  # Call local validate_api_key
        raise HTTPException(status_code=401, detail="Invalid API key")

    return api_key
