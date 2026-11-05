# Security

## Reporting a vulnerability

Report security issues to **aws-security@amazon.com** through the
[AWS Vulnerability Reporting Program](https://aws.amazon.com/security/vulnerability-reporting/).
Please do not open a public GitHub issue for a security report.

## Fixed in v1.0.1 — authentication disabled by default (Critical)

Reported by an external researcher through the AWS Vulnerability Reporting
Program. Tracked internally as `P529130405` / `TigerTeam-22217`.

**CVSS 3.1:** `AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H` — 9.8 (Critical)
**CWE:** CWE-306 (Missing Authentication for Critical Function),
CWE-1188 (Insecure Default Initialization of Resource)

### What was wrong

`AUTH_ENABLED` defaulted to `false`, and the CDK stack in this repository never
set `AGENTIC_AUTO_AUTH_ENABLED`, so every deployment produced by the sample's own
infrastructure-as-code ran with authentication off. In that state the middleware
assigned every request a hardcoded `role: "admin"` identity with no check, behind
a load balancer that was unconditionally internet-facing on plaintext HTTP port 80
with a security-group allowlist defaulting to `10.0.0.0/8`.

Any caller who could route to the load balancer was a full administrator with no
credential, able to enumerate, create, disable, delete and reset the password of
any account.

### What changed

| Area | Before | After |
|---|---|---|
| `config.py` | `AUTH_ENABLED` defaulted to `false` | defaults to **on**; only the exact string `false` disables it |
| `auth.py` | disabled mode injected `role: "admin"` | disabled mode injects an unprivileged `viewer`, flagged `unauthenticated` |
| `auth.py` | refused to run in production with auth off | **raises at import** when `AGENTIC_DATA_ENV=production` and auth is disabled |
| `auth.py` | Cognito claims read from an **unverified** JWT (`_decode_jwt_payload`) | full RS256 verification against the pool's JWKS, with issuer, audience and expiry enforced |
| `auth.py` | `get_current_user()` fell back to `role: "admin"` | falls back to an unprivileged identity |
| `api/main.py` | 10 `if not AUTH_ENABLED` branches short-circuited admin routes, one of them minting a forged `custom:role=admin` token | 9 privilege-granting branches now return `503`; the remaining one reports auth configuration to the UI and grants nothing |
| `local_auth.py` | signing secret defaulted to a value committed to this repository | no default; refuses to load without `AGENTIC_AUTO_JWT_SECRET` (min 32 chars) |
| CDK | `AllowedCIDRs` defaulted to `10.0.0.0/8` | required parameter, no default |
| CDK | load balancer always internet-facing on HTTP 80 | **internal by default**; going public requires `publicly_accessible=true` *and* a certificate, and then serves HTTPS with an HTTP redirect |
| CDK | never set `AGENTIC_AUTO_AUTH_ENABLED` | sets it explicitly on the Fargate task, and supplies a required `JwtSigningSecret` |

Two issues beyond the original report were found during remediation and fixed in
the same change: the Cognito path never verified JWT signatures, so a self-signed
token asserting `custom:role: admin` was accepted; and `local_auth.py` shipped a
signing secret in the repository, which any reader could use to mint valid tokens.

Also restored: `cdk/package.json`, `cdk/package-lock.json`, `web/package.json` and
`web/package-lock.json`, which an earlier commit (`f25e5c3`) removed, leaving the
documented build and deploy path non-functional.

### Verifying the fix

`tests/test_auth_regression.py` reproduces the researcher's proof of concept and
asserts the escalation is gone, including a positive control that a correctly
signed token is still accepted and a tampered one is not.

```bash
pip install -r requirements.txt
AGENTIC_AUTO_JWT_SECRET=$(openssl rand -base64 48) python tests/test_auth_regression.py
```

Both suites in `tests/` are self-contained scripts that exit non-zero on failure.
Run them directly; `pytest` cannot collect them.

### Deploying safely

```bash
cd cdk && npm install
npx cdk deploy \
  --parameters AllowedCIDRs=<your.cidr/32> \
  --parameters JwtSigningSecret="$(openssl rand -base64 48)" \
  --parameters AdminPassword=<strong-password>
```

The load balancer is internal unless you pass both
`-c publicly_accessible=true` and `-c certificate_arn=<acm-arn>`.

## Fixed in v1.0.2 — command execution, request forgery and an unverified token path

Found by static analysis (CodeQL) and by reviewing the first remediation, not by
an external report. Six findings were rated Critical by CodeQL; the most serious
one here was rated nothing at all, because it is a logic flaw rather than a taint
flow.

### Remote command execution — `POST /api/mcp/test`

The `stdio` branch ran:

```python
proc = subprocess.Popen([command] + args, ..., env={**os.environ, **req.get("env", {})})
```

with `command`, `args` and `env` all taken from the request body. That is
arbitrary command execution as the Fargate task role. Allow-listing `command`
would not have closed it, because the caller also supplied the environment and
therefore `LD_PRELOAD`.

The branch is gone. It now answers `400`. A server cannot test a stdio MCP server
on a caller's behalf: stdio servers are started by the client, so they are tested
where they run.

### Server-side request forgery — six endpoints

`POST /api/models/test`, `POST /api/datasources/test`, `POST /api/mcp/test` and
`POST /api/mcp/tools` fetched a URL supplied in the request body and returned the
response body to the caller. On ECS and Fargate that is credential disclosure,
not just reachability: the task role's keys are served over plain HTTP from
`169.254.170.2` and instance metadata from `169.254.169.254`.

The existing check was `url.startswith(("http://", "https://"))`, which both of
those URLs satisfy.

`agentic_core/net_guard.py` now gates every fetch of a caller-supplied URL:

| | |
|---|---|
| Scheme | `http` and `https` only |
| Userinfo | rejected — `http://user:pass@host/` |
| Address | **every** address the name resolves to is checked, not just the first |
| Always blocked | loopback, link-local (`169.254.0.0/16`, `fe80::/10`), multicast, reserved, unspecified |
| IPv6 embedding | `::ffff:169.254.169.254` and 6to4/Teredo forms are reduced to the embedded IPv4 address before checking |
| Private ranges | blocked unless `AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS=true`, for a self-hosted model endpoint inside the VPC. That opt-in does **not** unblock link-local. |
| Redirects | refused, not followed — a validated public URL answering `302` with a metadata address would otherwise defeat the address check |
| Response size | capped at 10 MB |

**Residual risk, stated rather than hidden:** validation resolves the hostname
and the connection resolves it again, so a name that returns different answers
between the two (DNS rebinding) is not covered. Closing that requires pinning the
connection to the validated address, which `urllib` does not expose. Refusing
redirects is what keeps the remaining window narrow.

### Authentication bypass on the Authing provider — not flagged by any scanner

`agentic_core/authing_auth.py` had its own copy of the unverified decoder that
the first pass removed from `auth.py`:

```python
def _decode_jwt_payload(token):
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
```

`verify_token()` called it, compared `exp`, and returned the claims. With
`AGENTIC_AUTO_AUTH_PROVIDER=authing`, anyone could base64-encode
`{"email": "<a known admin>", "exp": <future>}`, append arbitrary bytes as a
signature, and be authenticated as that user. `exp` was whatever they chose to
put there. This is the same missing-verification class as the originally reported
finding, on a different provider path, and no scanner flagged it.

Tokens are now verified against Authing's JWKS (RS256, issuer and audience from
the OIDC discovery document). Discovery or JWKS failure refuses the token rather
than falling back to reading it.

### Authorization — the middleware authenticated but never authorized

`require_admin()` existed and was called from nowhere. Any authenticated
viewer could register or delete a model endpoint, a data source or an MCP server,
and reach the command-execution and metadata paths above.

Thirteen configuration endpoints now depend on `require_admin`:
`/api/models/custom` (POST, DELETE), `/api/models/test`, `/api/datasources`
(POST, PUT, DELETE), `/api/datasources/test`, `/api/datasources/connect`,
`/api/datasources/connect/apply`, `/api/mcp/servers` (POST, DELETE),
`/api/mcp/test`, `/api/mcp/tools`.

**This is a behavioural change.** With authentication enabled, these endpoints
now require an account whose role is `admin`. Set `AGENTIC_AUTO_ADMIN_PASSWORD`
at deploy time; `admin@agentic-data.com` is created on first login.

### Password storage

`local_auth.py` hashed passwords with a single round of salted SHA-256, while its
own docstring claimed bcrypt. SHA-256 is built to be fast, which is the opposite
of what password storage needs.

Now PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP guidance, standard library,
no new dependency) — about 46 ms per verification. **Existing accounts keep
working:** a legacy hash is still accepted, and the stored hash is upgraded in
place on the next successful login.

### Also fixed in this pass

| File | Issue |
|---|---|
| `api/main.py` | **The application could not start.** The first remediation deleted `_decode_jwt_payload` from `auth.py` and left `from auth import ... _decode_jwt_payload` in place, so importing the app raised `ImportError`. Both call sites now use the verified decoder. |
| `agentic_core/local_auth.py` | `_ensure_admin()` called `logger.warning()` in a module that never imported `logging`, so a deployment without `AGENTIC_AUTO_ADMIN_PASSWORD` raised `NameError` on the first login instead of warning. |
| `agentic_core/vqr.py` | `_verify_sql_execution()` executes model-generated SQL for real against three engines. Only the PostgreSQL branch opened a read-only session; the Athena and Snowflake branches would have run a `CREATE TABLE AS` or an `INSERT`. A single read-only statement is now required for all three. |
| `agentic_core/db_engine.py` | New `ensure_read_only_query()`: strips comments, rejects a second statement, requires `SELECT` or `WITH`. The pattern used elsewhere in this package, `sql.upper().startswith("DROP ")`, misses both `/*x*/DROP TABLE t` and `SELECT 1; DROP TABLE t`. Comments are removed by a single forward pass, not a regular expression — see below. |
| `agentic_core/db_engine.py` | A Snowflake `private_key_path` from a data-source config was opened directly. It is now resolved and, if `AGENTIC_AUTO_KEY_DIR` is set, confined to that directory. A bad path still falls back to password auth so a stale config does not break a working deployment, but the fallback is logged. |
| `agentic_core/authing_auth.py` | Logged the email address of every auto-created SSO user in clear text. |

### A defect introduced by this change, and removed again

The first version of `ensure_read_only_query()` stripped block comments with
`/\*.*?\*/`. CodeQL flagged it as `py/polynomial-redos`, and the flag was right:
`"/*a"` repeated 20,000 times took **2.9 seconds**, because every start position
rescans to the end of the input before failing. Rewriting the pattern made it
*worse* (8.1 seconds) — the cost is in the number of start positions, not the
shape of the inner match.

It is now a single forward pass whose index only ever advances, so it is linear
by construction. The same payload takes **0.1 ms**. The pass also tracks quoted
text, which the regular expression did not: a semicolon inside a string literal
is no longer counted as a second statement, and unterminated quoting is refused
rather than ignored.

### Assessed and deliberately not changed

CodeQL reports `py/sql-injection` at the `SELECT COUNT(*)` calls in
`api/main.py`. Those interpolate only identifiers, and every one passes through
`_safe_identifier()`, which enforces `^[a-zA-Z_][a-zA-Z0-9_.]*$`. The sanitizer
is correct; CodeQL does not model it. These are false positives and are left in
place rather than worked around.

Four `py/full-ssrf` alerts are **dismissed as "won't fix", not fixed in code**,
and that distinction matters. The finding is accurate as stated: the URL does
depend on a user-provided value, because these endpoints exist to test a URL the
operator supplies. What is gone is the exploitable consequence — `net_guard.py`
resolves the name and checks every address before connecting, and refuses
redirects. CodeQL cannot see that as a barrier: the check happens at run time
against DNS, so it is not statically provable, and this repository uses CodeQL
default setup, which does not read a custom model pack. The twelve
request-forgery checks in `tests/test_hardening_regression.py` are what verify
the claim; they call the guard for real.

The same reasoning covers the dismissed `py/weak-sensitive-data-hashing` alert on
the legacy SHA-256 branch of `_verify_password`, which exists only so that
accounts created before this change can still log in and be upgraded, and the two
`py/path-injection` alerts inside `resolve_private_key_path` itself, which has to
touch the path in order to vet it.

### Verifying

```bash
python tests/test_hardening_regression.py   # 24 checks
python tests/test_auth_regression.py        #  9 checks
```

The request-forgery, SQL and password guards are imported and called for real.
The endpoint-level facts — that no process spawn remains, that every
caller-supplied fetch goes through the guard, and that each of the thirteen
configuration endpoints carries the dependency — are asserted against the source
with `ast`, because importing `api/main.py` needs the full runtime.

One of those checks exists because of the `ImportError` above: it parses every
name `api/main.py` imports from `auth.py` and asserts `auth.py` defines it.

### New environment variables

| Variable | Default | Effect |
|---|---|---|
| `AGENTIC_AUTO_ALLOW_PRIVATE_EGRESS` | unset | `true` permits outbound requests to private ranges, for a model endpoint inside the VPC. Never permits link-local. |
| `AGENTIC_AUTO_KEY_DIR` | unset | When set, a Snowflake `private_key_path` must resolve inside this directory. |

Both default to the secure behaviour, so no infrastructure change is needed.
