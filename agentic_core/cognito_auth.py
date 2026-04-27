"""
Cognito Auth — Login, Signup, Password Change, User Management.
Uses boto3 cognito-idp client directly (no SDK dependency).
"""
import boto3
import logging
from config import COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_REGION

logger = logging.getLogger(__name__)

def _client():
    return boto3.client("cognito-idp", region_name=COGNITO_REGION)


def login(email: str, password: str) -> dict:
    """Authenticate user, return tokens."""
    try:
        resp = _client().initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        result = resp.get("AuthenticationResult", {})
        if not result:
            # Could be NEW_PASSWORD_REQUIRED challenge
            challenge = resp.get("ChallengeName")
            if challenge == "NEW_PASSWORD_REQUIRED":
                return {"ok": False, "error": "需要修改密码", "challenge": challenge,
                        "session": resp.get("Session", "")}
            return {"ok": False, "error": f"认证挑战: {challenge}"}
        return {
            "ok": True,
            "access_token": result["AccessToken"],
            "id_token": result["IdToken"],
            "refresh_token": result.get("RefreshToken", ""),
            "expires_in": result.get("ExpiresIn", 86400),
        }
    except _client().exceptions.NotAuthorizedException:
        return {"ok": False, "error": "邮箱或密码错误"}
    except _client().exceptions.UserNotFoundException:
        return {"ok": False, "error": "用户不存在"}
    except _client().exceptions.UserNotConfirmedException:
        return {"ok": False, "error": "用户未验证"}
    except Exception as e:
        logger.error(f"Login failed: {e}")
        return {"ok": False, "error": str(e)}


def refresh_token(refresh_tok: str) -> dict:
    """Refresh access token."""
    try:
        resp = _client().initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={"REFRESH_TOKEN": refresh_tok},
        )
        result = resp["AuthenticationResult"]
        return {
            "ok": True,
            "access_token": result["AccessToken"],
            "id_token": result["IdToken"],
            "expires_in": result.get("ExpiresIn", 86400),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def change_password(access_token: str, old_password: str, new_password: str) -> dict:
    """Change password for authenticated user."""
    try:
        _client().change_password(
            AccessToken=access_token,
            PreviousPassword=old_password,
            ProposedPassword=new_password,
        )
        return {"ok": True}
    except _client().exceptions.NotAuthorizedException:
        return {"ok": False, "error": "当前密码错误"}
    except _client().exceptions.InvalidPasswordException as e:
        return {"ok": False, "error": f"密码不符合要求: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════ Admin: User Management ═══════

def list_users() -> list:
    """List all users in the pool."""
    try:
        resp = _client().list_users(
            UserPoolId=COGNITO_USER_POOL_ID,
            Limit=60,
        )
        users = []
        for u in resp.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            users.append({
                "username": u["Username"],
                "email": attrs.get("email", ""),
                "role": attrs.get("custom:role", "viewer"),
                "status": u["UserStatus"],
                "enabled": u["Enabled"],
                "created": u["UserCreateDate"].isoformat() if u.get("UserCreateDate") else "",
                "modified": u["UserLastModifiedDate"].isoformat() if u.get("UserLastModifiedDate") else "",
            })
        return users
    except Exception as e:
        logger.error(f"list_users failed: {e}")
        return []


def create_user(email: str, password: str, role: str = "viewer") -> dict:
    """Admin create a new user."""
    try:
        _client().admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:role", "Value": role},
            ],
            TemporaryPassword=password,
            MessageAction="SUPPRESS",
        )
        # Set permanent password
        _client().admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            Password=password,
            Permanent=True,
        )
        return {"ok": True}
    except _client().exceptions.UsernameExistsException:
        return {"ok": False, "error": "用户已存在"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def update_user_role(email: str, role: str) -> dict:
    """Update user's role."""
    try:
        _client().admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[{"Name": "custom:role", "Value": role}],
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def disable_user(email: str) -> dict:
    """Disable a user."""
    try:
        _client().admin_disable_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def enable_user(email: str) -> dict:
    """Enable a user."""
    try:
        _client().admin_enable_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_user(email: str) -> dict:
    """Delete a user."""
    try:
        _client().admin_delete_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def reset_user_password(email: str, new_password: str) -> dict:
    """Admin reset user password."""
    try:
        _client().admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            Password=new_password,
            Permanent=True,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
