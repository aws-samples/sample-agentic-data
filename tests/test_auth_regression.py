"""Regression test for P529130405 / H1-4077739.

Reproduces the researcher's PoC against the patched code and asserts the
privilege escalation is gone. Exercises the middleware directly so it needs
only fastapi, not the application's full dependency set.
"""
import base64, json, os, pathlib, sys, time, importlib
import importlib.util

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# local_auth now imports agentic_core.errors. Resolving that normally would run
# the real agentic_core/__init__.py, which pulls in strands and the whole agent
# stack; errors itself needs only logging and uuid. Register a stand-in package
# so the module can be loaded by path, as the checks below do.
def _install_light_agentic_core():
    import types
    root = pathlib.Path(__file__).resolve().parent.parent
    existing = sys.modules.get('agentic_core')
    if existing is not None and getattr(existing, '__test_stub__', False):
        return
    pkg = types.ModuleType('agentic_core')
    pkg.__path__ = [str(root / 'agentic_core')]
    pkg.__test_stub__ = True
    sys.modules['agentic_core'] = pkg
    spec = importlib.util.spec_from_file_location(
        'agentic_core.errors', str(root / 'agentic_core' / 'errors.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules['agentic_core.errors'] = module
    spec.loader.exec_module(module)
    pkg.errors = module


_install_light_agentic_core()
os.environ['AGENTIC_AUTO_JWT_SECRET'] = 'x' * 48   # local_auth refuses to load without one
FAILED = []

def case(name, fn):
    try:
        fn(); print(f'  PASS  {name}')
    except AssertionError as e:
        FAILED.append(name); print(f'  FAIL  {name}\n          {e}')
    except Exception as e:
        FAILED.append(name); print(f'  ERROR {name}\n          {type(e).__name__}: {e}')

def fresh(**env):
    """Reload config+auth under a given environment."""
    for k in ('AGENTIC_AUTO_AUTH_ENABLED', 'AGENTIC_DATA_ENV'):
        os.environ.pop(k, None)
    os.environ.update(env)
    for m in ('config', 'auth'):
        sys.modules.pop(m, None)
    import config, auth
    importlib.reload(config); importlib.reload(auth)
    return config, auth

# 1 — the default. The researcher's PoC set no auth variables at all.
def t1():
    config, _ = fresh()
    assert config.AUTH_ENABLED is True, \
        f'default must be secure, got AUTH_ENABLED={config.AUTH_ENABLED}'
case('default with no env set -> authentication ENABLED', t1)

# 2 — a typo must not disable auth
def t2():
    config, _ = fresh(AGENTIC_AUTO_AUTH_ENABLED='False ', AGENTIC_DATA_ENV='dev')
    assert config.AUTH_ENABLED is False, 'explicit "False " should disable'
    config, _ = fresh(AGENTIC_AUTO_AUTH_ENABLED='0', AGENTIC_DATA_ENV='dev')
    assert config.AUTH_ENABLED is True, '"0" is not "false"; must stay enabled'
    config, _ = fresh(AGENTIC_AUTO_AUTH_ENABLED='no', AGENTIC_DATA_ENV='dev')
    assert config.AUTH_ENABLED is True, '"no" is not "false"; must stay enabled'
case('only the exact string "false" disables; typos fail secure', t2)

# 3 — production may not run with auth off
def t3():
    try:
        fresh(AGENTIC_AUTO_AUTH_ENABLED='false', AGENTIC_DATA_ENV='production')
    except RuntimeError as e:
        assert 'refused' in str(e).lower() or 'production' in str(e).lower()
        return
    raise AssertionError('auth disabled in production was allowed')
case('AUTH_ENABLED=false + ENV=production -> refuses to start', t3)

# 4 — dev opt-out grants viewer, never admin (the core of the report)
def t4():
    _, auth = fresh(AGENTIC_AUTO_AUTH_ENABLED='false', AGENTIC_DATA_ENV='dev')
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)

    def users(request: Request):
        u = auth.get_current_user(request)
        if u.get('role') != 'admin':
            raise HTTPException(status_code=403, detail='admin only')
        return {'users': ['leaked']}

    def toggle(e: str, request: Request):
        u = auth.get_current_user(request)
        if u.get('role') != 'admin':
            raise HTTPException(status_code=403, detail='admin only')
        return {'ok': True}

    # add_api_route rather than @app.get / @app.put: identical behaviour, but the
    # handlers are passed as arguments and so are visibly used. Behind the
    # decorators a scanner reports them as inner functions that are defined and
    # never called, because a decorator registration is not a call it can see.
    app.add_api_route('/api/users', users, methods=['GET'])
    app.add_api_route('/api/users/{e}/toggle', toggle, methods=['PUT'])

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get('/api/users')
    assert r.status_code == 403, f'PoC step 2 still works: {r.status_code} {r.text[:120]}'
    r = c.put('/api/users/viewer@agentic-data.com/toggle')
    assert r.status_code == 403, f'PoC step 3 still works: {r.status_code} {r.text[:120]}'
case('PoC steps 2-3 with no credential -> 403, not admin', t4)

# 5 — get_current_user fallback is unprivileged
def t5():
    _, auth = fresh()
    class Bare:
        class state: pass
    u = auth.get_current_user(Bare())
    assert u['role'] != 'admin', f'fallback still administrator: {u}'
    assert u.get('unauthenticated') is True, f'fallback not marked unauthenticated: {u}'
case('get_current_user fallback -> unprivileged, flagged', t5)

# 6 — a forged Cognito token must not be accepted
def t6():
    _, auth = fresh(AGENTIC_DATA_ENV='dev')
    claims = {'sub': 'attacker', 'email': 'a@b.c',
              'custom:role': 'admin', 'exp': int(time.time()) + 86400}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    forged = 'eyJhbGciOiJSUzI1NiJ9.' + body + '.notasignature'
    from fastapi import HTTPException
    try:
        out = auth._verify_cognito_token(forged)
    except HTTPException as e:
        assert e.status_code in (401, 503), f'unexpected status {e.status_code}'
        return
    raise AssertionError(f'forged token accepted, claims returned: {out}')
case('forged JWT asserting custom:role=admin -> rejected', t6)

# 7 — the unverified decoder is gone
def t7():
    _, auth = fresh()
    assert not hasattr(auth, '_decode_jwt_payload'), \
        'unverified _decode_jwt_payload still present'
case('unverified JWT decoder removed', t7)

# 8 — local auth refuses a published default secret
def t8():
    import importlib.util
    os.environ.pop('AGENTIC_AUTO_JWT_SECRET', None)
    spec = importlib.util.spec_from_file_location(
        '_la_probe', str(pathlib.Path(__file__).resolve().parent.parent / 'agentic_core' / 'local_auth.py'))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except RuntimeError as e:
        assert 'JWT_SECRET' in str(e), f'wrong error: {e}'
        os.environ['AGENTIC_AUTO_JWT_SECRET'] = 'x' * 48
        return
    finally:
        os.environ['AGENTIC_AUTO_JWT_SECRET'] = 'x' * 48
    raise AssertionError('local_auth loaded with no signing secret')
case('local_auth without a secret -> refuses to load', t8)

# 9 - positive control: real RS256 signature, correct issuer/audience, accepted.
def t9():
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    os.environ['AGENTIC_AUTO_COGNITO_POOL_ID'] = 'us-east-1_TESTPOOL'
    os.environ['AGENTIC_AUTO_COGNITO_CLIENT_ID'] = 'test-client-id'
    os.environ['AGENTIC_AUTO_COGNITO_REGION'] = 'us-east-1'
    _, auth = fresh(AGENTIC_DATA_ENV='dev')

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    iss = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL'
    good = pyjwt.encode({'sub': 'alice', 'iss': iss, 'client_id': 'test-client-id',
                         'custom:role': 'admin', 'exp': int(time.time()) + 600},
                        pem, algorithm='RS256')

    class FakeKey:  # stand in for the JWKS lookup
        def __init__(self, k): self.key = k
    class FakeClient:
        def get_signing_key_from_jwt(self, _t): return FakeKey(key.public_key())
    auth._jwks_client.cache_clear()
    auth._jwks_client = lambda: FakeClient()

    claims = auth._verify_cognito_token(good)
    assert claims['sub'] == 'alice', f'valid token rejected: {claims}'

    # same key, tampered payload -> must fail
    head, body, sig = good.split('.')
    bad_body = base64.urlsafe_b64encode(
        json.dumps({'sub': 'attacker', 'iss': iss, 'client_id': 'test-client-id',
                    'custom:role': 'admin', 'exp': int(time.time()) + 600}).encode()
    ).decode().rstrip('=')
    from fastapi import HTTPException
    try:
        auth._verify_cognito_token(f'{head}.{bad_body}.{sig}')
    except HTTPException:
        return
    raise AssertionError('tampered payload with a valid signature was accepted')
case('valid RS256 token accepted; tampered payload rejected', t9)

print()
if FAILED:
    print(f'{len(FAILED)} FAILED: {FAILED}'); sys.exit(1)
print('All regression checks passed.')
