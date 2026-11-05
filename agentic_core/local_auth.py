"""
Local JWT Authentication — Cognito-free auth for China region.
Users stored in DynamoDB config table. Passwords hashed with PBKDF2-HMAC-SHA256.
"""
import json
import time
import hashlib
import hmac
import base64
import os
import logging
import boto3

from config import CONFIG_TABLE, REGION

logger = logging.getLogger(__name__)

# No default. A shipped default secret is a published signing key: anyone reading
# this repository could mint a valid token. Absent configuration we refuse to start
# rather than accept forgeable tokens.
_JWT_SECRET = os.environ.get("AGENTIC_AUTO_JWT_SECRET", "")
if not _JWT_SECRET:
    raise RuntimeError(
        "AGENTIC_AUTO_JWT_SECRET is not set. Generate one with "
        "`openssl rand -base64 48` and supply it via the deployment's secret store. "
        "Local auth cannot run without it."
    )
if len(_JWT_SECRET) < 32:
    raise RuntimeError("AGENTIC_AUTO_JWT_SECRET must be at least 32 characters.")
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


# ── Password hashing (PBKDF2-HMAC-SHA256, stdlib) ──
#
# Was a single round of salted SHA-256. SHA-256 is built to be fast, which is the
# opposite of what password storage needs: commodity hardware tries it billions of
# times a second, so a stolen hash table is a stolen password list. PBKDF2 is in
# the standard library, so this needs no new dependency.
#
# Iteration count follows OWASP guidance for PBKDF2-HMAC-SHA256. Measured at
# roughly 46 ms per verification, which is a login-path cost, not a per-request one.
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_PREFIX = "pbkdf2_sha256$"


def _hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_PREFIX}{_PBKDF2_ITERATIONS}${dk.hex()}", salt


def _verify_password(password: str, hashed: str, salt: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith(_PBKDF2_PREFIX):
        try:
            _, iterations, digest = hashed.split("$", 2)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), digest)
    # Legacy single-round hash. Still accepted so existing accounts keep working;
    # local_login upgrades the stored hash on the next successful login.
    legacy = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return hmac.compare_digest(legacy, hashed)


def _needs_rehash(hashed: str) -> bool:
    return not str(hashed or "").startswith(_PBKDF2_PREFIX)


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

    # Upgrade a legacy single-round hash now that we hold the plaintext. Failing
    # to persist the upgrade must not fail the login.
    if _needs_rehash(user.get("password_hash", "")):
        try:
            new_hash, new_salt = _hash_password(password)
            users[email]["password_hash"] = new_hash
            users[email]["password_salt"] = new_salt
            _save_users(users)
            logger.info("Upgraded a stored password hash to PBKDF2")
        except Exception as exc:
            logger.warning("Password hash upgrade failed, login still allowed: %s", exc)
    
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
