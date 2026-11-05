"""
Authentication middleware.

Authentication is enabled by default. When AGENTIC_AUTO_AUTH_ENABLED is set to
the exact string "false", requests are let through with an *unprivileged*
identity for local development only; that mode is refused outright when
AGENTIC_DATA_ENV is "production".

Cognito tokens are verified against the user pool's JWKS (RS256), including
signature, issuer, audience and expiry. Claims are never read from an
unverified token.
"""
import time, json, logging, os
from functools import lru_cache
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from config import (
    AUTH_ENABLED, ENVIRONMENT,
    COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_REGION,
)

logger = logging.getLogger(__name__)

AUTH_PROVIDER = os.environ.get("AGENTIC_AUTO_AUTH_PROVIDER", "cognito")  # cognito | local | authing

# A deployment must not be able to reach production with authentication off.
if not AUTH_ENABLED:
    if str(ENVIRONMENT).strip().lower() == "production":
        raise RuntimeError(
            "AGENTIC_AUTO_AUTH_ENABLED=false is refused when AGENTIC_DATA_ENV=production. "
            "Authentication cannot be disabled in a production deployment."
        )
    logger.warning(
        "=" * 78 + "\n"
        "AUTHENTICATION IS DISABLED (AGENTIC_AUTO_AUTH_ENABLED=false).\n"
        "Every request runs as an unauthenticated, read-only identity.\n"
        "This is for local development only. Never expose this process to a network.\n"
        + "=" * 78
    )


@lru_cache(maxsize=1)
def _jwks_client():
    """PyJWKClient for the configured Cognito user pool. Cached; it caches keys itself."""
    import jwt  # PyJWT
    if not (COGNITO_USER_POOL_ID and COGNITO_REGION):
        raise HTTPException(
            status_code=503,
            detail="Authentication is enabled but Cognito is not configured.",
        )
    url = (
        f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com"
        f"/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    )
    return jwt.PyJWKClient(url)


def _verify_cognito_token(token: str) -> dict:
    """Verify a Cognito JWT and return its claims.

    Signature, expiry and issuer are all enforced. Raises 401 on any failure, so
    a malformed or forged token can never reach the claim-reading path below.
    """
    import jwt
    from jwt import InvalidTokenError
    issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            # Cognito access tokens carry client_id rather than aud, so audience is
            # checked explicitly below instead of by the decoder.
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
        )
    except HTTPException:
        raise
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    except Exception as exc:  # network / JWKS failure
        logger.warning("JWKS verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Token could not be verified") from exc

    if COGNITO_CLIENT_ID:
        presented = claims.get("aud") or claims.get("client_id")
        if presented != COGNITO_CLIENT_ID:
            raise HTTPException(status_code=401, detail="Token was not issued for this client")
    return claims

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
            # Local development only. Deliberately NOT an administrator: the
            # previous "admin" default meant any caller of any deployment held
            # full administrative rights with no credential.
            request.state.user = {
                "user_id": "anonymous",
                "username": "anonymous",
                "email": "",
                "role": "viewer",
                "unauthenticated": True,
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
            # Cognito JWT — signature verified against the pool's JWKS.
            payload = _verify_cognito_token(token)
            request.state.user = {
                "user_id": payload.get("sub", "unknown"),
                "username": payload.get("cognito:username", payload.get("username", "unknown")),
                "email": payload.get("email", ""),
                "role": payload.get("custom:role", "viewer"),
            }
        return await call_next(request)


def get_current_user(request: Request) -> dict:
    """Current user from request state.

    The fallback is deliberately unprivileged. It previously returned
    role "admin", so any path that reached a route without passing through the
    middleware was treated as an administrator.
    """
    return getattr(request.state, "user", {
        "user_id": "anonymous",
        "username": "anonymous",
        "email": "",
        "role": "viewer",
        "unauthenticated": True,
    })


def require_admin(request: Request) -> dict:
    """Return the current user, or raise 403 unless they are an administrator."""
    user = get_current_user(request)
    if user.get("unauthenticated") or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator privileges required")
    return user
