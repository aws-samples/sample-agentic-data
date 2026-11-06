"""Regression checks for the second remediation pass.

Covers the six CodeQL critical findings (one command execution, five
server-side request forgery), the unverified-signature path on the Authing
provider, password storage, the read-only SQL guard, and the import break that
the first pass left behind.

Runs as a script so it needs no pytest:

    python3 tests/test_hardening_regression.py

Checks that read source rather than calling the function say so in their name.
Importing api/main.py needs the full runtime (strands, boto3, a live config), so
the endpoint-level facts are asserted against the source text; the guards
themselves are imported and called for real.
"""
import ast
import functools
import hashlib
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASSED, FAILED = [], []


def check(name):
    def deco(fn):
        try:
            fn()
            PASSED.append(name)
            print(f"  PASS  {name}")
        except AssertionError as exc:
            FAILED.append(name)
            print(f"  FAIL  {name}\n          {exc}")
        except Exception as exc:
            FAILED.append(name)
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
        return fn
    return deco


def _load(path, name):
    """Load one module by path, bypassing the package __init__ and its heavy imports."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def _install_light_agentic_core():
    """Register a stand-in `agentic_core` package in sys.modules.

    The modules below are loaded by path so the suite does not need the agent
    runtime installed. They now import `agentic_core.errors`, and resolving that
    normally would execute the real `agentic_core/__init__.py`, which imports
    strands and the whole agent stack. `errors` itself depends on nothing but
    logging and uuid, so it is loaded directly and attached to a package object
    that has a __path__ and no __init__ side effects.
    """
    import types
    existing = sys.modules.get("agentic_core")
    if existing is not None and getattr(existing, "__test_stub__", False):
        return
    pkg = types.ModuleType("agentic_core")
    pkg.__path__ = [os.path.join(ROOT, "agentic_core")]
    pkg.__test_stub__ = True
    sys.modules["agentic_core"] = pkg
    for name in ("errors", "net_guard"):
        spec = importlib.util.spec_from_file_location(
            f"agentic_core.{name}", os.path.join(ROOT, "agentic_core", f"{name}.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"agentic_core.{name}"] = module
        spec.loader.exec_module(module)
        setattr(pkg, name, module)


_install_light_agentic_core()


@functools.lru_cache(maxsize=1)
def _db_engine():
    """agentic_core.db_engine, imported once through the stand-in package."""
    return _load("agentic_core/db_engine.py", "agentic_core.db_engine")


# ───────────────────────── the import break ─────────────────────────

@check("every name api/main.py imports from auth exists in auth.py")
def _():
    main_tree = ast.parse(_source("api/main.py"))
    wanted = set()
    for node in ast.walk(main_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "auth":
            wanted.update(alias.name for alias in node.names)
    assert wanted, "no `from auth import ...` found -- check moved?"

    auth_tree = ast.parse(_source("auth.py"))
    defined = set()
    for node in auth_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            defined.update((a.asname or a.name).split(".")[0] for a in node.names)

    missing = sorted(wanted - defined)
    # The first pass deleted _decode_jwt_payload from auth.py and left the import
    # in main.py, so the application raised ImportError before serving anything.
    assert not missing, f"api/main.py imports names auth.py does not define: {missing}"


@check("no unverified JWT decoder remains in either module")
def _():
    for path in ("api/main.py", "agentic_core/authing_auth.py"):
        src = _source(path)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "_decode_jwt_payload", \
                    f"{path} still calls _decode_jwt_payload"
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "_decode_jwt_payload", \
                    f"{path} still defines _decode_jwt_payload"


# ───────────────────── CWE-918: request forgery ─────────────────────

NG = _load("agentic_core/net_guard.py", "net_guard")


def _blocked(url):
    try:
        NG.validate_outbound_url(url)
    except NG.BlockedRequestError:
        return True
    return False


@check("instance metadata service is unreachable from a caller-supplied URL")
def _():
    assert _blocked("http://169.254.169.254/latest/meta-data/iam/security-credentials/")


@check("ECS task-role credential endpoint is unreachable")
def _():
    # The one that matters on Fargate: this returns usable AWS credentials over
    # plain HTTP, and the handlers echoed response bodies back to the caller.
    assert _blocked("http://169.254.170.2/v2/credentials/")


@check("IPv6-mapped form of the metadata address is blocked too")
def _():
    assert _blocked("http://[::ffff:169.254.169.254]/latest/meta-data/")


@check("loopback is blocked by address and by name")
def _():
    assert _blocked("http://127.0.0.1:8000/api/")
    assert _blocked("http://localhost/")


@check("only http and https are accepted")
def _():
    for url in ("file:///etc/passwd", "gopher://example.com/", "ftp://example.com/"):
        assert _blocked(url), url


@check("credentials embedded in the URL are refused")
def _():
    assert _blocked("http://user:password@example.com/")


@check("private addresses need an explicit opt-in, link-local never gets one")
def _():
    os.environ.pop("AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS", None)
    assert _blocked("http://10.0.0.5:11434/api/generate"), "private allowed by default"
    os.environ["AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS"] = "true"
    try:
        NG.validate_outbound_url("http://10.0.0.5:11434/api/generate")
    except NG.BlockedRequestError as exc:
        raise AssertionError(f"opt-in did not permit a VPC endpoint: {exc}")
    assert _blocked("http://169.254.170.2/"), \
        "the opt-in must not reach the credential endpoint"
    os.environ.pop("AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS", None)


@check("redirects are refused rather than followed")
def _():
    # A validated public URL answering 302 with a metadata address in Location
    # would otherwise defeat the address check entirely.
    handler = NG._NoRedirects()
    try:
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://169.254.169.254/latest/meta-data/")
    except NG.BlockedRequestError:
        return
    raise AssertionError("redirect was not refused")


@check("a legitimate public endpoint still passes")
def _():
    NG.validate_outbound_url("https://api.openai.com/v1/chat/completions")


@check("every caller-supplied fetch in api/main.py goes through the guard")
def _():
    src = _source("api/main.py")
    tree = ast.parse(src)
    lines = src.splitlines()
    unguarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        if name != "urlopen":
            continue
        # A literal URL is not caller-supplied; those are the localhost probe and
        # the Cognito token endpoint.
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            continue
        if isinstance(arg, ast.Name) and arg.id == "req":
            unguarded.append((node.lineno, lines[node.lineno - 1].strip()[:90]))
            continue
        unguarded.append((node.lineno, lines[node.lineno - 1].strip()[:90]))
    assert not unguarded, f"raw urlopen on a non-literal URL: {unguarded}"


# ──────────────── CWE-78: command execution removed ────────────────

@check("api/main.py no longer spawns a process anywhere")
def _():
    src = _source("api/main.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.name or "") for a in node.names]
            mod = getattr(node, "module", "") or ""
            assert "subprocess" not in names and mod != "subprocess", \
                f"subprocess imported at line {node.lineno}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("Popen", "system", "popen", "run", "call"), \
                f"process spawn at line {node.lineno}"


@check("the stdio MCP test refuses instead of executing the request's command")
def _():
    src = _source("api/main.py")
    i = src.index('if transport == "stdio":')
    branch = src[i:src.index('elif transport == "sse":', i)]
    # Strip comments first: the replacement explains what it removed, so the
    # word Popen legitimately appears in prose right there.
    code_only = "\n".join(
        line.split("#", 1)[0] for line in branch.splitlines()
    )
    assert "Popen" not in code_only, "the spawn is still there"
    assert "400" in branch, "the branch should answer 400"


# ──────────────────── authorization on the admin plane ────────────────────

ADMIN_HANDLERS = [
    "api_add_custom_model", "api_del_custom_model", "api_test_model",
    "api_datasource_test", "api_update_datasource", "api_add_datasource",
    "api_delete_datasource", "api_add_mcp", "api_del_mcp", "api_test_mcp",
    "api_list_mcp_tools", "api_connect_datasource", "api_apply_introspection",
]


@check("configuration endpoints require an administrator, not just a session")
def _():
    tree = ast.parse(_source("api/main.py"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ADMIN_HANDLERS:
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d]
            found[node.name] = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name) and d.func.id == "Depends"
                and d.args and isinstance(d.args[0], ast.Name)
                and d.args[0].id == "require_admin"
                for d in defaults
            )
    missing = sorted(n for n in ADMIN_HANDLERS if not found.get(n))
    assert not missing, f"no require_admin dependency on: {missing}"


@check("require_admin rejects an unauthenticated caller and a non-admin role")
def _():
    os.environ["AGENTIC_AUTO_AUTH_ENABLED"] = "true"
    os.environ.setdefault("AGENTIC_DATA_ENV", "dev")
    sys.path.insert(0, ROOT)
    import importlib
    import auth as auth_mod
    importlib.reload(auth_mod)
    from fastapi import HTTPException

    class Req:
        def __init__(self, user):
            self.state = types.SimpleNamespace()
            if user is not None:
                self.state.user = user

    for user, label in [
        (None, "no user on request state"),
        ({"user_id": "a", "role": "viewer", "unauthenticated": True}, "unauthenticated"),
        ({"user_id": "a", "role": "viewer"}, "viewer"),
        ({"user_id": "a", "role": "analyst"}, "analyst"),
    ]:
        try:
            auth_mod.require_admin(Req(user))
        except HTTPException as exc:
            assert exc.status_code == 403, f"{label}: expected 403, got {exc.status_code}"
            continue
        raise AssertionError(f"{label} was accepted as an administrator")

    assert auth_mod.require_admin(Req({"user_id": "a", "role": "admin"}))["role"] == "admin"


# ─────────────── CWE-347 on the Authing provider path ───────────────

@check("Authing no longer accepts a token it has not verified")
def _():
    import base64
    import json
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    aa = _load("agentic_core/authing_auth.py", "authing_auth_t")

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    import time
    forged = ".".join([
        seg({"alg": "RS256", "typ": "JWT"}),
        seg({"sub": "attacker", "email": "admin@agentic-data.com",
             "exp": int(time.time()) + 86400}),
        "not-a-signature",
    ])
    # Previously: base64-decode the payload, compare exp, return the claims.
    result = aa.verify_token(forged)
    assert isinstance(result, dict) and "error" in result, \
        f"forged Authing token accepted: {result}"


# ───────────────────────── password storage ─────────────────────────

@check("passwords are stored with PBKDF2, and legacy hashes still log in")
def _():
    os.environ["AGENTIC_AUTO_JWT_SECRET"] = "x" * 48
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    la = _load("agentic_core/local_auth.py", "local_auth_t")

    hashed, salt = la._hash_password("correct horse battery staple")
    assert hashed.startswith("pbkdf2_sha256$"), f"not PBKDF2: {hashed[:24]}"
    assert la._PBKDF2_ITERATIONS >= 210_000, "iteration count below OWASP guidance"
    assert la._verify_password("correct horse battery staple", hashed, salt)
    assert not la._verify_password("wrong", hashed, salt)
    assert not la._needs_rehash(hashed)

    legacy_salt = "00112233445566778899aabbccddeeff"
    legacy = hashlib.sha256(f"{legacy_salt}:hunter2".encode()).hexdigest()
    assert la._verify_password("hunter2", legacy, legacy_salt), "existing accounts locked out"
    assert not la._verify_password("nope", legacy, legacy_salt)
    assert la._needs_rehash(legacy), "legacy hash not marked for upgrade"

    assert not la._verify_password("x", "", "s")
    assert not la._verify_password("x", "pbkdf2_sha256$notanint$ff", "s")


@check("local_auth defines the logger it uses")
def _():
    # _ensure_admin called logger.warning() in a module that never imported
    # logging, so a deployment without AGENTIC_AUTO_ADMIN_PASSWORD raised
    # NameError on the first login instead of warning.
    os.environ["AGENTIC_AUTO_JWT_SECRET"] = "x" * 48
    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    la = _load("agentic_core/local_auth.py", "local_auth_t2")
    assert hasattr(la, "logger"), "local_auth has no logger"


# ──────────────────────── read-only SQL guard ────────────────────────

def _sql_guard():
    """The real ensure_read_only_query, imported rather than exec'd.

    This used to cut the function out of db_engine.py by string index and exec
    the slice, so the suite needed no agent runtime. The light package stand-in
    installed above makes a plain import work instead -- which also means a
    mis-cut slice can no longer pass silently, and there is no exec() in a file
    that runs in CI.
    """
    return _db_engine().ensure_read_only_query


@check("only a single SELECT or WITH statement may be dry-run")
def _():
    guard = _sql_guard()
    for sql in ("SELECT * FROM orders", "WITH t AS (SELECT 1) SELECT * FROM t",
                "  select a from b ;  ", "(SELECT 1)", "SELECT 1; -- trailing comment"):
        guard(sql)

    for sql, label in [
        ("SELECT 1; DROP TABLE users", "a second statement"),
        ("/*x*/DROP TABLE users", "a DROP hidden behind a block comment"),
        ("-- ok\nDELETE FROM orders", "a DELETE hidden behind a line comment"),
        ("SELECT 1 /* ; */ ; DROP TABLE t", "a semicolon outside a comment"),
        ("CREATE TABLE AS SELECT * FROM x", "CREATE TABLE AS SELECT"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET a=1", "UPDATE"),
        ("", "an empty statement"),
    ]:
        try:
            guard(sql)
        except ValueError:
            continue
        raise AssertionError(f"{label} was allowed")


@check("quoted text is not mistaken for a comment or a statement separator")
def _():
    guard = _sql_guard()
    # The regex this replaced did not know about quoting, so a semicolon inside a
    # string literal counted as a second statement and a legitimate query was
    # refused.
    for sql in ("SELECT '; DROP TABLE t' AS s",
                "SELECT '--not a comment' AS s",
                "SELECT 'it''s fine' AS s",
                'SELECT "weird;col" FROM t'):
        guard(sql)

    # Unterminated quoting fails closed: otherwise a trailing statement could
    # hide inside what looks like an open string literal.
    for sql, label in [("SELECT 1' ; DROP TABLE t", "an unterminated quote"),
                       ("SELECT 1 /* unterminated", "an unterminated block comment")]:
        try:
            guard(sql)
        except ValueError:
            continue
        raise AssertionError(f"{label} was allowed")


@check("comment stripping is linear, not quadratic, in the input")
def _():
    import time
    guard = _sql_guard()
    # /\*.*?\*/ took 2.9 s on "/*a" repeated 20,000 times: every start position
    # rescans to the end of the input before failing. The scanner that replaced it
    # only ever moves forward.
    for payload in ("/*a" * 20000, "/*" + "a/*" * 30000, "--a" * 50000):
        started = time.time()
        try:
            guard("SELECT 1 " + payload)
        except ValueError:
            pass
        elapsed = time.time() - started
        assert elapsed < 1.0, f"took {elapsed:.1f}s on a {len(payload)}-character payload"


@check("the SQL dry-run path applies the guard before choosing an engine")
def _():
    src = _source("agentic_core/vqr.py")
    fn = src[src.index("def _verify_sql_execution"):]
    fn = fn[:fn.index("\ndef ", 1)] if "\ndef " in fn[1:] else fn
    guard_at = fn.find("ensure_read_only_query")
    first_engine = fn.find('if engine == "postgresql"')
    assert guard_at != -1, "no read-only guard in _verify_sql_execution"
    assert guard_at < first_engine, "the guard runs after an engine branch is chosen"


# ──────────────────────── private key path ────────────────────────

@check("a private key path outside the configured directory is refused")
def _():
    resolve = _db_engine().resolve_private_key_path

    import tempfile
    with tempfile.TemporaryDirectory() as root:
        inside = os.path.join(root, "snowflake.pem")
        with open(inside, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.environ["AGENTIC_AUTO_KEY_DIR"] = root
        try:
            assert resolve(inside) == os.path.realpath(inside)
            for bad, label in [("/etc/passwd", "an unrelated absolute path"),
                               (os.path.join(root, "..", "escape.pem"), "a traversal"),
                               (root + "-evil/key.pem", "a sibling with a shared prefix")]:
                try:
                    resolve(bad)
                except ValueError:
                    continue
                raise AssertionError(f"{label} was accepted")
            try:
                resolve("")
            except ValueError:
                pass
            else:
                raise AssertionError("an empty path was accepted")
        finally:
            os.environ.pop("AGENTIC_AUTO_KEY_DIR", None)


# ──────────── CWE-209: exception text in responses ────────────

ERRORS = _load("agentic_core/errors.py", "agentic_core_errors_t")


@check("a returned error message carries no text from the exception")
def _():
    # The message a PostgreSQL driver raises on a rejected login includes the
    # host, the port and the user it tried. That used to be handed to the caller.
    leaky = Exception(
        'connection to server at 10.0.3.14, port 5432 failed: FATAL: password '
        'authentication failed for user "svc_reporting"'
    )
    message = ERRORS.safe_message(leaky, "unit test")
    for fragment in ("10.0.3.14", "5432", "svc_reporting", "password authentication",
                     "FATAL", "connection to server"):
        assert fragment not in message, f"leaked {fragment!r}: {message}"
    assert ERRORS.classify(leaky) == "auth", ERRORS.classify(leaky)
    # Shape: a category label and a reference, nothing else.
    import re as _re
    assert _re.fullmatch(r"[^()]+\(参考 [0-9a-f]{12}\)", message), message


@check("failures are classified into a category the caller can act on")
def _():
    import socket
    cases = [
        (TimeoutError("timed out"), "timeout"),
        (socket.gaierror("nodename nor servname provided"), "dns"),
        (ConnectionRefusedError("[Errno 61] Connection refused"), "network"),
        (PermissionError("denied"), "auth"),
        (FileNotFoundError("/etc/pki/secret.pem"), "notfound"),
        (ValueError("Only SELECT and WITH statements may be executed, not DROP"), "readonly"),
        (ValueError("bad input"), "invalid"),
        (Exception("something nobody has ever seen"), "internal"),
    ]
    for exc, expected in cases:
        got = ERRORS.classify(exc)
        assert got == expected, f"{type(exc).__name__}: got {got}, expected {expected}"
    # A path in the message must not survive into the output either.
    assert "/etc/pki/secret.pem" not in ERRORS.safe_message(cases[4][0], "t")


@check("no response anywhere passes exception text to the caller")
def _():
    # Checked structurally rather than by grep: the pattern that matters is
    # str(e) reachable from a `return`, a JSONResponse, or an HTTPException
    # detail -- not str(e) in a log line, which is where it belongs.
    offenders = []
    for path in ["api/main.py"] + sorted(
        os.path.join("agentic_core", f)
        for f in os.listdir(os.path.join(ROOT, "agentic_core"))
        if f.endswith(".py")
    ):
        tree = ast.parse(_source(path))

        def leaks(node):
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                if isinstance(inner.func, ast.Name) and inner.func.id == "str":
                    if inner.args and isinstance(inner.args[0], ast.Name) \
                            and inner.args[0].id in ("e", "exc", "_e", "err"):
                        return True
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value is not None and leaks(node.value):
                offenders.append(f"{path}:{node.lineno} return")
            elif isinstance(node, ast.Raise) and node.exc is not None and leaks(node.exc):
                offenders.append(f"{path}:{node.lineno} raise")
            elif isinstance(node, ast.Assign) and leaks(node.value):
                # An assignment is only a problem if the target is an error field
                # that gets returned; flag the shapes that were the actual bugs.
                target = ast.unparse(node.targets[0]) if node.targets else ""
                if any(k in target for k in ('["error"]', "['error']", "error_body",
                                             'err_msg', '["message"]', "['message']")):
                    offenders.append(f"{path}:{node.lineno} {target}")
    assert not offenders, "exception text reaches a response at: " + ", ".join(offenders[:12])


@check("errors.py depends on nothing that could make its import circular")
def _():
    tree = ast.parse(_source("agentic_core/errors.py"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    # Every module in the package imports this one, so it must import none of them.
    assert "agentic_core" not in imported, imported
    assert imported <= {"logging", "uuid", "__future__"}, imported


# ─────────── hosted sign-in must not point at someone else's domain ───────────

@check("no Cognito hosted-UI setting defaults to the author's demo environment")
def _():
    # These defaulted to agentic-data-demo.auth.us-west-1.amazoncognito.com and
    # https://demo.nubility.cn/, so a deployment that did not set them sent its
    # users' sign-in to that domain.
    tree = ast.parse(_source("config.py"))
    defaults = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                    and len(call.args) == 2 and isinstance(call.args[1], ast.Constant)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defaults[target.id] = call.args[1].value
    for name in ("COGNITO_DOMAIN", "COGNITO_REDIRECT_URI", "COGNITO_LOGOUT_URI"):
        assert name in defaults, f"{name} not found in config.py"
        assert defaults[name] == "", f"{name} defaults to {defaults[name]!r}"


@check("the hosted UI is only advertised once it is actually configured")
def _():
    # The login page renders only a hosted-UI button when hosted_ui is true, with
    # no password form. Returning it unconditionally left an unconfigured
    # deployment with no way to sign in.
    tree = ast.parse(_source("api/main.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "api_auth_config")

    def advertises(node):
        return (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == "hosted_ui"
                        and isinstance(v, ast.Constant) and v.value is True
                        for k, v in zip(node.value.keys, node.value.values)))

    body = fn.body
    idx = next(i for i, stmt in enumerate(body) if advertises(stmt))
    guarded = any(
        isinstance(stmt, ast.If) and "COGNITO_DOMAIN" in ast.unparse(stmt.test)
        for stmt in body[:idx]
    )
    assert guarded, "hosted_ui: True is returned without checking COGNITO_DOMAIN first"


print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"All {len(PASSED)} hardening checks passed.")
