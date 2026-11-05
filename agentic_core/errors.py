"""One place to turn an exception into something safe to send to a caller.

Sixty-six handlers in api/main.py returned `str(e)` to the client. That text
carries whatever the raising library chose to put in it: absolute paths, SQL
text, table and column names, hostnames, and -- in the data-source and model
connection tests -- occasionally a connection string with the password still in
it. CodeQL reports the class as py/stack-trace-exposure.

The useful part of those messages was the *kind* of failure, not the detail: a
caller debugging a data source needs to know "the credentials were rejected"
rather than "connection to server at 10.0.3.14, port 5432 failed: FATAL:
password authentication failed for user 'svc_reporting'". So the detail goes to
the log under a short reference, and the caller gets the category plus that
reference.

Deliberate implementation detail: `safe_message` reads the exception only to
choose a branch and to log. Every string it returns is a literal plus a random
reference. Nothing the exception contains is concatenated into the result --
which is what makes this a real fix rather than a narrower leak, and incidentally
what lets static analysis see the sink as untainted.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# code -> message shown to the caller. Chinese to match the rest of the UI.
CATEGORIES = {
    "timeout": "连接超时",
    "auth": "认证失败或权限不足",
    "dns": "无法解析目标主机",
    "network": "无法建立网络连接",
    "blocked": "目标地址不被允许",
    "notfound": "目标资源不存在",
    "invalid": "请求参数无效",
    "readonly": "仅允许单条只读查询",
    "unsupported": "该操作不受支持",
    "conflict": "资源状态冲突",
    "internal": "内部错误",
}

# Matched against the exception's class name, lowercased. Checked before the
# message keywords, because a specific type is a stronger signal than a phrase.
_BY_TYPE = (
    ("blockedrequesterror", "blocked"),
    ("unterminatedsql", "readonly"),
    ("timeouterror", "timeout"),
    ("timeout", "timeout"),
    ("permissionerror", "auth"),
    ("accessdenied", "auth"),
    ("unauthorized", "auth"),
    ("notauthorized", "auth"),
    ("filenotfounderror", "notfound"),
    ("nosuchkey", "notfound"),
    ("nosuchbucket", "notfound"),
    ("resourcenotfound", "notfound"),
    ("gaierror", "dns"),
    ("connectionrefusederror", "network"),
    ("connectionreseterror", "network"),
    ("connectionerror", "network"),
    ("urlerror", "network"),
    ("httperror", "network"),
    ("ssl", "network"),
    ("notimplementederror", "unsupported"),
    ("conflict", "conflict"),
    ("alreadyexists", "conflict"),
    ("validationerror", "invalid"),
)

# The generic built-ins. Checked *after* the phrase table, because
# ValueError("Only SELECT and WITH statements may be executed") says a great deal
# more in its message than in its type.
_BY_GENERIC_TYPE = (
    ("keyerror", "invalid"),
    ("valueerror", "invalid"),
    ("typeerror", "invalid"),
    ("attributeerror", "internal"),
    ("indexerror", "internal"),
)

# Fallback: phrases that appear in driver and SDK messages. Only the chosen
# category label leaves this module, never the phrase or its surroundings.
_BY_PHRASE = (
    (("timed out", "timeout"), "timeout"),
    (("password authentication failed", "access denied", "accessdenied",
      "not authorized", "unauthorized", "invalid credentials", "permission denied",
      "authentication failed", "signaturedoesnotmatch", "invalidaccesskeyid",
      "expiredtoken", "forbidden"), "auth"),
    (("name or service not known", "nodename nor servname", "could not resolve",
      "temporary failure in name resolution", "getaddrinfo"), "dns"),
    (("connection refused", "no route to host", "network is unreachable",
      "could not connect", "connection reset", "endpointconnectionerror",
      "ssl", "certificate"), "network"),
    (("does not exist", "not found", "nosuchentity", "404"), "notfound"),
    (("already exists", "conditionalcheckfailed", "duplicate"), "conflict"),
    (("only select", "single sql statement", "read-only", "readonly"), "readonly"),
)


def classify(exc: BaseException) -> str:
    """Return a category code for an exception. Never returns exception text."""
    type_name = type(exc).__name__.lower()
    for needle, code in _BY_TYPE:
        if needle in type_name:
            return code
    try:
        text = str(exc).lower()
    except Exception:
        return "internal"
    for needles, code in _BY_PHRASE:
        for needle in needles:
            if needle in text:
                return code
    for needle, code in _BY_GENERIC_TYPE:
        if needle in type_name:
            return code
    return "internal"


def new_reference() -> str:
    return uuid.uuid4().hex[:12]


def safe_message(exc: BaseException, context: str = "") -> str:
    """Log the exception in full; return a message built only from literals.

    The reference in the returned string is what ties a user's report to the log
    line, so it is the one thing worth copying out of an error toast.
    """
    reference = new_reference()
    logger.warning(
        "[err:%s] %s: %s", reference, context or "-", type(exc).__name__,
        exc_info=exc,
    )
    return f"{CATEGORIES.get(classify(exc), CATEGORIES['internal'])}(参考 {reference})"


def safe_error(exc: BaseException, context: str = "", key: str = "error") -> dict:
    """safe_message as a dict fragment, for spreading into an existing response."""
    reference = new_reference()
    logger.warning(
        "[err:%s] %s: %s", reference, context or "-", type(exc).__name__,
        exc_info=exc,
    )
    code = classify(exc)
    return {
        key: CATEGORIES.get(code, CATEGORIES["internal"]),
        "code": code,
        "reference": reference,
    }
