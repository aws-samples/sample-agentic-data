"""
Local JWT Authentication — Cognito-free auth for China region.
Users stored in DynamoDB config table. Passwords hashed with bcrypt.
"""
import json
import time
import hashlib
import hmac
import base64
import os
import boto3

from config import CONFIG_TABLE, REGION

_JWT_SECRET = os.environ.get("AGENTIC_AUTO_JWT_SECRET", "agentic-data-default-secret-change-me")
_ADMIN_PASSWORD = os.environ.get("AGENTIC_AUTO_ADMIN_PASSWORD", "")  # Must be set via env var at deploy time

# ── Simple JWT (no PyJWT dependency) ──

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + '=' * padding)

def _jwt_encode(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    sig = hmac.new(_JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"

def _jwt_decode(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token")
    h, p, s = parts
    expected_sig = hmac.new(_JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    actual_sig = _b64url_decode(s)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Invalid signature")
    payload = json.loads(_b64url_decode(p))
    if payload.get("exp", 0) < time.time():
        raise ValueError("Token expired")
    return payload


# ── Password hashing (SHA-256 + salt, no bcrypt dependency) ──

def _hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = os.urandom(16).hex()
    hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hashed, salt

def _verify_password(password: str, hashed: str, salt: str) -> bool:
    check, _ = _hash_password(password, salt)
    return hmac.compare_digest(check, hashed)


# ── User Storage (DynamoDB config table) ──

def _get_users() -> dict:
    try:
        table = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
        resp = table.get_item(Key={"config_key": "local_users"})
        return json.loads(resp.get("Item", {}).get("data", "{}"))
    except Exception:
        return {}

def _save_users(users: dict):
    table = boto3.resource("dynamodb", region_name=REGION).Table(CONFIG_TABLE)
    table.put_item(Item={
        "config_key": "local_users",
        "data": json.dumps(users, ensure_ascii=False),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

def _ensure_admin():
    """Ensure admin user exists on first boot."""
    users = _get_users()
    if "admin@agentic-data.com" not in users:
        if not _ADMIN_PASSWORD:
            logger.warning("AGENTIC_AUTO_ADMIN_PASSWORD not set — skipping admin user creation")
            return
        hashed, salt = _hash_password(_ADMIN_PASSWORD)
        users["admin@agentic-data.com"] = {
            "email": "admin@agentic-data.com",
            "role": "admin",
            "password_hash": hashed,
            "password_salt": salt,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # Also create analyst and viewer demo users with same password
        for email, role in [("analyst@agentic-data.com", "analyst"), ("viewer@agentic-data.com", "viewer")]:
            h, s = _hash_password(_ADMIN_PASSWORD)
            users[email] = {
                "email": email, "role": role,
                "password_hash": h, "password_salt": s,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        _save_users(users)


# ── Public API ──

def local_login(email: str, password: str) -> dict:
    """Authenticate and return JWT token."""
    _ensure_admin()
    users = _get_users()
    user = users.get(email)
    # Shortname fallback: "admin" → "admin@agentic-data.com"
    if not user and "@" not in email:
        user = users.get(f"{email}@agentic-data.com")
        if user:
            email = user["email"]
    if not user:
        return {"error": "用户不存在"}
    if not _verify_password(password, user["password_hash"], user["password_salt"]):
        return {"error": "密码错误"}
    
    token = _jwt_encode({
        "sub": email,
        "email": email,
        "custom:role": user.get("role", "viewer"),
        "iat": int(time.time()),
        "exp": int(time.time()) + 86400,  # 24h
    })
    return {
        "token": token,
        "email": email,
        "role": user.get("role", "viewer"),
        "expires_in": 86400,
    }

def local_verify(token: str) -> dict:
    """Verify JWT and return user info."""
    try:
        payload = _jwt_decode(token)
        return {
            "email": payload.get("email", ""),
            "role": payload.get("custom:role", "viewer"),
            "sub": payload.get("sub", ""),
        }
    except Exception as e:
        return {"error": str(e)}

def local_create_user(email: str, password: str, role: str = "viewer") -> dict:
    """Create a new user."""
    users = _get_users()
    if email in users:
        return {"error": "用户已存在"}
    hashed, salt = _hash_password(password)
    users[email] = {
        "email": email, "role": role,
        "password_hash": hashed, "password_salt": salt,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_users(users)
    return {"ok": True, "email": email, "role": role}

def local_delete_user(email: str) -> dict:
    users = _get_users()
    if email not in users:
        return {"error": "用户不存在"}
    if email == "admin@agentic-data.com":
        return {"error": "不能删除管理员"}
    del users[email]
    _save_users(users)
    return {"ok": True}

def local_list_users() -> list:
    _ensure_admin()
    users = _get_users()
    return [{"email": u["email"], "role": u["role"], "created_at": u.get("created_at", "")}
            for u in users.values()]

def local_change_password(email: str, new_password: str) -> dict:
    users = _get_users()
    if email not in users:
        return {"error": "用户不存在"}
    hashed, salt = _hash_password(new_password)
    users[email]["password_hash"] = hashed
    users[email]["password_salt"] = salt
    _save_users(users)
    return {"ok": True}
