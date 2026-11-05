"""
Authing OIDC Authentication — China region SSO.
Replaces Cognito Hosted UI for China deployments.
Uses standard OIDC Authorization Code Flow.
"""
import json
import time
import urllib.request
import urllib.parse
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

# ── Config from env ──
AUTHING_DOMAIN = os.environ.get("AGENTIC_AUTO_AUTHING_DOMAIN", "")
AUTHING_APP_ID = os.environ.get("AGENTIC_AUTO_AUTHING_APP_ID", "")
AUTHING_APP_SECRET = os.environ.get("AGENTIC_AUTO_AUTHING_APP_SECRET", "")
AUTHING_REDIRECT_URI = os.environ.get("AGENTIC_AUTO_AUTHING_REDIRECT_URI", "")
AUTHING_POOL_ID = os.environ.get("AGENTIC_AUTO_AUTHING_POOL_ID", "")


@lru_cache(maxsize=1)
def _oidc_metadata() -> dict:
    """Authing's OIDC discovery document. Gives the issuer and JWKS URI authoritatively."""
    if not AUTHING_DOMAIN:
        raise ValueError("AGENTIC_AUTO_AUTHING_DOMAIN is not configured")
    url = f"https://{AUTHING_DOMAIN}/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310 - fixed, configured host
        return json.loads(resp.read(1024 * 1024))


@lru_cache(maxsize=1)
def _jwks_client():
    import jwt  # PyJWT
    return jwt.PyJWKClient(_oidc_metadata()["jwks_uri"])


def _verify_jwt(token: str) -> dict:
    """Verify an Authing JWT and return its claims.

    This replaces a function that base64-decoded the payload and returned it
    without touching the signature. Any caller could mint
    {"email": "<an admin>", "exp": <future>}, append arbitrary bytes as a
    signature, and be authenticated as that user -- the same missing-verification
    class as the reported finding, on the Authing provider path.

    Raises ValueError on any failure so the caller fails closed.
    """
    import jwt
    from jwt import InvalidTokenError
    if not token:
        raise ValueError("No token supplied")
    try:
        meta = _oidc_metadata()
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=meta["issuer"],
            audience=AUTHING_APP_ID or None,
            options={
                "verify_aud": bool(AUTHING_APP_ID),
                "require": ["exp", "iss", "sub"],
            },
        )
    except InvalidTokenError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
    except Exception as exc:
        # JWKS or discovery failure. Refuse the token rather than fall back to
        # reading it unverified.
        logger.warning("Authing token verification failed: %s", exc)
        raise ValueError("Token could not be verified") from exc


def get_auth_config() -> dict:
    """Return OIDC config for frontend — same structure as Cognito Hosted UI."""
    if not AUTHING_DOMAIN or not AUTHING_APP_ID:
        return {"hosted_ui": False}
    return {
        "hosted_ui": True,
        "domain": AUTHING_DOMAIN,
        "client_id": AUTHING_APP_ID,
        "redirect_uri": AUTHING_REDIRECT_URI,
        "logout_uri": AUTHING_REDIRECT_URI,
        "scopes": "openid profile email",
        "provider": "authing",
    }



def _get_or_create_user_role(email: str, default_role: str = "viewer") -> str:
    """Get user role from DynamoDB. Auto-create user record on first Authing login."""
    try:
        from agentic_core.local_auth import _get_users, _save_users
        users = _get_users()
        if email in users:
            return users[email].get("role", default_role)
        # First login via Authing — create local record (no password, SSO only)
        import time as _time
        users[email] = {
            "email": email,
            "role": default_role,
            "password_hash": "",
            "password_salt": "",
            "auth_provider": "authing",
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        _save_users(users)
        logger.info("Auto-created Authing user on first SSO login with role=%s", default_role)
        return default_role
    except Exception as e:
        logger.error(f"Failed to get/create user role: {e}")
        return default_role


def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens via Authing OIDC token endpoint."""
    token_url = f"https://{AUTHING_DOMAIN}/oidc/token"
    if not token_url.startswith("https://"):
        raise ValueError("Authing token URL must use HTTPS")
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": AUTHING_APP_ID,
        "client_secret": AUTHING_APP_SECRET,
        "code": code,
        "redirect_uri": AUTHING_REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        token_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            result = json.loads(resp.read())

        id_token = result.get("id_token", "")
        access_token = result.get("access_token", "")
        refresh_token = result.get("refresh_token", "")

        payload = _verify_jwt(id_token)
        email = payload.get("email", payload.get("phone_number", ""))
        role = payload.get("custom:role", payload.get("role", "viewer"))

        # Look up role from local user DB (DynamoDB), auto-create if first login
        role = _get_or_create_user_role(email, role)
        return {
            "ok": True,
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": refresh_token,
            "expires_in": result.get("expires_in", 3600),
            "user": {
                "email": email,
                "role": role,
                "username": payload.get("preferred_username", payload.get("nickname", email)),
            },
        }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        logger.error(f"Authing token exchange failed: {error_body}")
        return {"ok": False, "error": f"Token exchange failed: {error_body}"}
    except Exception as e:
        logger.error(f"Authing token exchange error: {e}")
        return {"ok": False, "error": str(e)}


def verify_token(token: str) -> dict:
    """Verify an Authing token's signature and claims. Role always from DynamoDB.

    Expiry is enforced by the decoder, which also checks the signature, issuer
    and audience. The previous implementation checked only `exp`, on a payload it
    had not verified -- so `exp` was whatever the caller chose to put there.
    """
    try:
        payload = _verify_jwt(token)
    except ValueError as exc:
        return {"error": str(exc)}

    email = payload.get("email", payload.get("phone_number", ""))
    # Always get role from DynamoDB — Authing JWT doesn't carry role
    role = _get_or_create_user_role(email, "viewer")

    return {
        "email": email,
        "role": role,
        "sub": payload.get("sub", ""),
    }


def refresh_tokens(refresh_token_str: str) -> dict:
    """Refresh tokens via Authing OIDC."""
    token_url = f"https://{AUTHING_DOMAIN}/oidc/token"
    if not token_url.startswith("https://"):
        raise ValueError("Authing token URL must use HTTPS")
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": AUTHING_APP_ID,
        "client_secret": AUTHING_APP_SECRET,
        "refresh_token": refresh_token_str,
    }).encode()

    req = urllib.request.Request(
        token_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            result = json.loads(resp.read())
        return {
            "ok": True,
            "access_token": result.get("access_token", ""),
            "id_token": result.get("id_token", ""),
            "refresh_token": result.get("refresh_token", refresh_token_str),
            "expires_in": result.get("expires_in", 3600),
        }
    except Exception as e:
        logger.error(f"Authing token refresh error: {e}")
        return {"ok": False, "error": str(e)}
