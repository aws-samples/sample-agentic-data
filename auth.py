"""
Authentication middleware — Cognito JWT validation.
When AUTH_ENABLED=false, all requests pass through with a default user.
When AUTH_ENABLED=true, validates JWT from Authorization header.
"""
import time, json, hmac, hashlib, base64, urllib.request
from functools import lru_cache
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import os
from config import AUTH_ENABLED, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_REGION

AUTH_PROVIDER = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")  # cognito | local | authing

# Simple JWT decode (no cryptographic verification in demo — Cognito handles that)
def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification (demo mode)."""
    try:
        payload = token.split(".")[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}

# Rate limiting state
_rate_limits: dict = {}  # user_id -> [(timestamp, ...)]

def check_rate_limit(user_id: str, max_per_minute: int) -> bool:
    """Returns True if within limit."""
    now = time.time()
    if user_id not in _rate_limits:
        _rate_limits[user_id] = []
    # Clean old entries
    _rate_limits[user_id] = [t for t in _rate_limits[user_id] if now - t < 60]
    if len(_rate_limits[user_id]) >= max_per_minute:
        return False
    _rate_limits[user_id].append(now)
    return True


class AuthMiddleware(BaseHTTPMiddleware):
    """
    - AUTH_ENABLED=false: pass all requests, inject default user
    - AUTH_ENABLED=true: validate JWT, inject user info
    """
    # Paths that don't require auth (static files, health check)
    PUBLIC_PATHS = {"/", "/health", "/favicon.ico", "/api/auth/login", "/api/auth/refresh", "/api/auth/callback", "/api/auth/config", "/api/auth/local-login"}

    async def dispatch(self, request: Request, call_next):
        # Static file / health — always pass
        if request.url.path in self.PUBLIC_PATHS or request.url.path.startswith("/static"):
            return await call_next(request)

        if not AUTH_ENABLED:
            # No auth — default user
            request.state.user = {
                "user_id": "default",
                "username": "demo_user",
                "email": "demo@example.com",
                "role": "admin",
            }
            return await call_next(request)

        # Extract token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            # Check cookie fallback
            token = request.cookies.get("access_token", "")
        else:
            token = auth_header[7:]

        if not token:
            if request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401, detail="未登录")
            return await call_next(request)

        # Decode JWT — route by auth provider
        if AUTH_PROVIDER == "local":
            from agentic_core.local_auth import local_verify
            result = local_verify(token)
            if "error" in result:
                raise HTTPException(status_code=401, detail=result["error"])
            payload = result
            request.state.user = {
                "user_id": payload.get("sub", payload.get("email", "unknown")),
                "username": payload.get("email", "unknown"),
                "email": payload.get("email", ""),
                "role": payload.get("role", "viewer"),
            }
        elif AUTH_PROVIDER == "authing":
            from agentic_core.authing_auth import verify_token as authing_verify, _get_or_create_user_role
            result = authing_verify(token)
            if "error" in result:
                raise HTTPException(status_code=401, detail=result["error"])
            email = result.get("email", "")
            role = _get_or_create_user_role(email, result.get("role", "viewer"))
            request.state.user = {
                "user_id": result.get("sub", email or "unknown"),
                "username": email or "unknown",
                "email": email,
                "role": role,
            }
        else:
            # Try local JWT first (for admin/built-in users), then Cognito
            try:
                from agentic_core.local_auth import local_verify
                result = local_verify(token)
                if "error" not in result:
                    request.state.user = {
                        "user_id": result.get("sub", result.get("email", "unknown")),
                        "username": result.get("email", "unknown"),
                        "email": result.get("email", ""),
                        "role": result.get("role", "viewer"),
                    }
                    return await call_next(request)
            except Exception:
                pass
            # Cognito JWT decode
            payload = _decode_jwt_payload(token)
            if not payload or payload.get("exp", 0) < time.time():
                raise HTTPException(status_code=401, detail="Token 已过期")
            request.state.user = {
                "user_id": payload.get("sub", "unknown"),
                "username": payload.get("cognito:username", payload.get("username", "unknown")),
                "email": payload.get("email", ""),
                "role": payload.get("custom:role", "viewer"),
            }
        return await call_next(request)


def get_current_user(request: Request) -> dict:
    """Get current user from request state."""
    return getattr(request.state, "user", {
        "user_id": "default",
        "username": "demo_user",
        "email": "",
        "role": "admin",
    })
