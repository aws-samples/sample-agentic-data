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

## Fixed in v1.0.3 — the signing secret was readable from the task definition

Found by standing up cdk_nag, which this content's product security review had
asked for and which had never run. Two of these are defects introduced by
v1.0.1, the first remediation pass.

### The signing secret was stored in plaintext in the task definition

v1.0.1 added a `JwtSigningSecret` CloudFormation parameter with `noEcho: true`
and referenced it from the Fargate container's `environment` block. `noEcho` only
hides a value from CloudFormation's own parameter display. A parameter referenced
from `environment` is resolved by CloudFormation and written into the task
definition revision, in plaintext:

```
Environment: [{ Name: AGENTIC_AUTO_JWT_SECRET, Value: { Ref: JwtSigningSecret } }]
```

Anyone holding `ecs:DescribeTaskDefinition` could read the signing secret, and
with it mint valid local-auth tokens — the same authentication bypass the
original report was about, by a different route. Task definition revisions are
retained, so the value outlived any rotation.

`AdminPassword` had the same shape, twice over: as an environment variable on the
init Lambda, where `lambda:GetFunctionConfiguration` reads it, and as a custom
resource property, which is stored in the stack and passed to the Lambda in the
event payload.

Both parameters now land in Secrets Manager. The task definition references the
signing secret through the container `secrets` block, so the container receives
it at start-up and the task definition holds only an ARN. The init Lambda
receives the secret's ARN and reads it at run time. The custom resource property
is gone — `timestamp` already forced the update it was there for.

### Authentication was on by default, and nobody could log in

v1.0.1 changed the default to authentication-on. The init Lambda, unchanged,
wrote the admin record as:

```python
pw_hash = hashlib.sha256(pw.encode()).hexdigest()
...
'local_users': json.dumps({'admin': {'password_hash': pw_hash, 'role': 'admin', ...}})
```

No salt, keyed `admin`, and **no `password_salt` field at all**. The application
verifies with `local_auth._verify_password(password, hash, user['password_salt'])`,
so the first login attempt raised `KeyError`. Nothing set
`AGENTIC_AUTO_ADMIN_PASSWORD` on the container either, so
`local_auth._ensure_admin()` could not create a working account as a fallback.

This was invisible while authentication defaulted to off. Turning it on made a
deployment that could not be logged into at all.

The init Lambda now derives the hash exactly the way the application does —
PBKDF2-HMAC-SHA256, same iteration count, with the salt stored alongside — and
keys the record by `admin@agentic-data.com`, the address `_ensure_admin()` looks
for. `tests/test_hardening_regression.py` checks that a hash produced the
Lambda's way verifies through the application's own `_verify_password`.

### cdk_nag

cdk_nag was the sixth remediation item and had never been run; before v1.0.1 it
could not be, because a previous commit had deleted the build manifests and the
stack would not synthesise. It now runs as a policy validation plugin on every
`cdk synth`, so there is no separate command to remember and no way to ship a
change without seeing the findings.

The first run reported 25 errors and 6 warnings. Fixed rather than excused:

| Rule | Was |
|---|---|
| `VPC7` | no VPC flow log — the response to this report advised reviewing logs for unauthorised access, and there were none at the network layer |
| `ELB2` | no load balancer access logs — likewise |
| `S1` | no S3 server access logging |
| `S10` | the bucket policy permitted plaintext HTTP, so the encryption setting covered data at rest only |
| `ECS4` | Container Insights disabled |
| `DDB3` ×5 | no point-in-time recovery on any table |
| `L1` ×2 | Lambda runtimes behind the current release |
| — | no deployment circuit breaker: a task that cannot start took up to three hours to fail a deployment |

Two IAM grants were scoped down instead of acknowledged:

- The security-group Lambda held `ec2:AuthorizeSecurityGroupIngress` and
  `RevokeSecurityGroupIngress` on `Resource: "*"` — it could rewrite the ingress
  rules of **every security group in the account**. Now scoped to the one group
  it manages, with the two read-only `Describe*` calls, which do not support
  resource-level permissions, in a separate statement.
- The task role held Athena and Glue actions on `Resource: "*"`. Now confined to
  Athena workgroups and Glue catalog objects.

Scoping those **raised** the cdk_nag count from 12 to 15, because the rule flags
any `*` inside a resource ARN and a narrower ARN still contains one. The count
went up while the permissions got smaller, which is worth knowing before anyone
reads a finding count as a security score.

What remains is acknowledged individually, each with its reason, in
`cdk/lib/agentic-data-stack.ts`. cdk-nag v3 requires every finding to be named
exactly, including the action or resource it covers, so an acknowledgement cannot
silently grow to cover something new. The substantive ones:

- `ecr:GetAuthorizationToken`, `ec2:Describe*` and the ECS Exec
  `ssmmessages:*Channel` actions do not support resource-level permissions. `*`
  is the only form AWS accepts.
- The Bedrock model ARN is chosen by an administrator after deployment, so it
  cannot be named at synth time.
- `AwsSolutions-EC23` is a synth-time artefact: the ingress CIDR is a
  CloudFormation parameter with no value yet, and its `allowedPattern` rejects
  `0.0.0.0/0`.
- `AwsSolutions-SMG4`: rotating the token signing secret invalidates every live
  session the moment it changes. Doing that safely needs the application to accept
  two keys during an overlap window, which it does not yet do. **Both secrets are
  operator-rotated.**

`cdk synth` now exits 0 with an empty `validation-report.json`.

### A trade-off left in place, with a warning

The default deployment is an internal load balancer on plaintext HTTP. It is not
reachable from the internet, but bearer tokens still cross the VPC in the clear.
Passing `-c certificate_arn=<acm-arn>` serves HTTPS instead — this works for an
internal load balancer, not just a public one — and the stack now emits a
synth-time warning when no certificate is supplied, rather than defaulting
quietly.

### Build tooling

Installing cdk-nag floated `aws-cdk-lib` to 2.275.0, whose cloud assembly schema
the pinned `aws-cdk` CLI 2.1114.1 cannot read, and `cdk synth` stopped working.
The CLI is now `^2.1147.0`. Since `aws-cdk-lib` was already a floating `^2.245.0`
dependency, any fresh `npm install` would have hit this; it was only the lockfile
that hid it.

### Verifying

```bash
python tests/test_cdk_hardening.py        # 32 checks, synthesises the stack
python tests/test_hardening_regression.py # 24 checks
python tests/test_auth_regression.py      #  9 checks
```

`tests/test_cdk_hardening.py` asserts against the synthesised template rather
than the TypeScript, so a change that type-checks but produces the wrong template
is caught. It synthesises three times: the default, `publicly_accessible=true`
with no certificate (which must refuse to synthesise), and with a certificate
(which must produce HTTPS plus a redirect). It skips rather than passes if
`cdk/node_modules` is absent.

## Fixed in v1.0.4 — exception text was returned to callers

CodeQL reported 66 `py/stack-trace-exposure` findings, all in `api/main.py`.
Medium severity, and not part of the original report — fixed because the text
being returned was worse than the rule implies.

### What was wrong

Handlers returned `str(e)` straight to the client:

```python
return {"ok": False, "message": f"PostgreSQL 连接失败: {str(e)[:200]}"}
```

A driver puts its own choice of detail in that message. For a rejected
PostgreSQL login it is:

```
connection to server at 10.0.3.14, port 5432 failed: FATAL:
password authentication failed for user "svc_reporting"
```

So the response disclosed an internal address, a port, and a service account
name. Elsewhere the same pattern returned absolute filesystem paths, SQL text
with table and column names, and — in `/api/auth/callback` — the identity
provider's own error body, forwarded verbatim.

### What changed

`agentic_core/errors.py` is now the only place an exception becomes something a
caller sees. It logs the exception in full, under a short reference, and returns
a category plus that reference:

```
认证失败或权限不足(参考 4e000283327e)
```

The useful part of the old message was the *kind* of failure, not the detail: a
caller debugging a data source needs to know the credentials were rejected, not
which user the driver tried. Eleven categories cover it — timeout, auth, dns,
network, blocked, notfound, invalid, readonly, unsupported, conflict, internal —
chosen from the exception's type first and its message only as a fallback.

**The implementation detail that makes this a fix rather than a narrower leak:**
`safe_message` reads the exception only to pick a branch and to write the log
line. Every string it returns is a literal plus a random reference. Nothing the
exception contains is concatenated into the result.

Rewritten: 59 sites in `api/main.py` and 58 across eleven `agentic_core` modules,
117 in total. The 42 `print()` and `logger` calls that log exception text
server-side were left alone — that is where it belongs.

Also removed from the global exception handler: `"type": type(exc).__name__`,
which named the internal exception class on every unhandled error.

### Verifying

`tests/test_hardening_regression.py` grew to 28 checks. Three are about this:

- A simulated PostgreSQL authentication failure, asserting that none of the
  host, port, user, or driver phrasing survives into the returned message.
- The classifier, across eight exception kinds.
- **A structural check over `api/main.py` and every module in `agentic_core`**:
  no `str(e)` is reachable from a `return`, a `raise HTTPException`, or an
  assignment to an error field. Asserted with `ast` rather than grep, because the
  distinction that matters is `str(e)` in a response versus `str(e)` in a log
  line, and only the first is a defect.

### Note on the reference

The reference in an error message is the only part worth copying out of a UI
toast: it is what ties a user's report to the log line holding the real cause.
Searching CloudWatch for `err:<reference>` returns the exception and its
traceback.

## Dependency alerts

Dependabot reported 8 high-severity advisories. All eight were
resource-exhaustion or crash conditions in build tooling, reachable only by a
build that processes untrusted input, which neither of these manifests does. They
are fixed anyway, because "unreachable in this configuration" is an argument that
stops being true the moment someone adds a step.

### `cdk/`

`js-yaml`, `brace-expansion` and `browserslist` all arrived transitively, through
`ts-jest` and through `aws-cdk-lib`'s `minimatch`. Pinned with npm `overrides`
rather than `npm audit fix`:

```json
"overrides": {
  "js-yaml@^3": "^3.15.2",
  "brace-expansion@^1": "^1.1.16",
  "brace-expansion@^2": "^2.1.2",
  "browserslist": "^4.28.7"
}
```

`npm audit fix` is what to avoid here. Run earlier in this work, it floated
`aws-cdk-lib` to a cloud assembly schema the pinned CLI could not read, and
`cdk synth` stopped working with an error that says nothing about dependencies.
Version-scoped overrides change exactly the packages named.

`npm audit` in `cdk/`: **0 critical, 0 high**, 10 moderate and 5 low remaining.

### `web/`

The two `browserslist` advisories here were reported against `dependencies`
rather than `devDependencies`, so they read as runtime exposure. They were not,
and the fix is not a version bump.

`web/package.json` declared one dependency, `@babel/preset-react`, and nothing
used it:

- `web/` contains HTML and SVG. There is no bundler, no `babel.config.js`, and no
  build script.
- `web/index.html` loads Babel in the browser, from a bundle vendored at
  `web/vendor/babel.min.js`. `@babel/preset-react` is a Node-side preset for a
  build step that does not exist.
- `deploy/.dockerignore` excludes `web/package*.json` from the image.
- Both `npm install` instructions in the README are `cd cdk`.

So the dependency was dead, and `browserslist` came in behind it. Removing it
takes the lockfile to a single entry and the advisories disappear rather than
needing a future bump of a package nobody runs.

`npm audit` in `web/`: **clean**.

### What is left, and why it stays

Three advisories remain: one medium, two low. Two of the three cannot be fixed
from this repository, and saying so is more useful than leaving them to look
neglected.

| Package | Severity | Why it stays |
|---|---|---|
| `minimatch` 10.2.6 | medium | **Not fixable downstream.** It is a *bundled* dependency of `aws-cdk-lib` — `inBundle: true`, shipped inside that package's tarball — so npm `overrides` cannot reach it. `aws-cdk-lib` 2.275.0 is the current release. This closes when AWS publishes a version bundling `minimatch` ≥ 11.0.1. An override was tried, had no effect, and was removed rather than left in place looking like a fix. |
| `deepmerge` 4.3.1 | low | **No patch exists.** 4.3.1 is the latest published version; the advisory has no `first_patched_version`. Development scope, reached only through the test runner. |
| `@babel/core` | low | Fixed — overridden to `^7.29.6`, resolves to 7.29.13. |

`npm audit` in `cdk/`: 0 critical, 0 high, 10 moderate, 4 low.

## The authorization model

Requested by the Guardian on `D435608595` ("the default role shouldn't be 'full
administrator' for every caller — please document the authorization model"). This
is what it is after the changes above.

### Identity

`auth.py`'s `AuthMiddleware` runs on every request except a fixed list of public
paths (`/`, `/health`, `/favicon.ico`, `/static/*`, and the five `/api/auth/*`
endpoints that exist to establish a session). Everything else requires a token.

Three providers, selected by `AGENTIC_AUTO_AUTH_PROVIDER`. **All three verify
signatures:**

| Provider | Token | Verification |
|---|---|---|
| `cognito` (default) | Cognito ID or access token | RS256 against the user pool's JWKS; signature, issuer, audience and expiry all enforced |
| `authing` | Authing OIDC token | RS256 against Authing's JWKS, issuer and audience from its OIDC discovery document |
| `local` | HS256 token minted by `local_auth` | HMAC with `AGENTIC_AUTO_JWT_SECRET`, which has no default and is read from Secrets Manager |

A token that fails verification is rejected. There is no path that reads claims
from an unverified token — that was the defect behind this ticket, and
`tests/test_auth_regression.py` asserts it stays closed.

### Roles

Three: `admin`, `analyst`, `viewer`. The role is **never** taken from a
caller-supplied field:

- `local` and `authing`: the role comes from the user's record in DynamoDB, looked
  up by the verified subject. An Authing token carries no role claim at all.
- `cognito`: from the `custom:role` claim, which only the user pool can set, on a
  token whose signature has been verified.

`viewer` is the default for an identity with no role recorded. **There is no path
that produces `admin` without an explicit role assignment.**

### What each role can do

Counted from the source: **40 of the 187 endpoints are administrator-only.**
Thirteen through a `Depends(require_admin)` dependency, twenty-seven through an
inline `role != "admin"` check. The remaining 147 are available to any verified
identity.

| | `viewer` | `analyst` | `admin` |
|---|---|---|---|
| Ask questions, read results, read dashboards | yes | yes | yes |
| Submit feedback | yes | yes | yes |
| Register, edit or delete a **data source** | no | no | yes |
| Register or delete a **model endpoint** | no | no | yes |
| Register or delete an **MCP server** | no | no | yes |
| Run a connection **test** against a supplied URL | no | no | yes |
| Verified-query queue (`/api/vqr/*`) — **including reads** | no | no | yes |
| Alert and KPI rules (`/api/alert-rules`, `/api/kpi-rules`) | no | no | yes |
| Feedback statistics, stored user memories | no | no | yes |
| User management (`/api/users/*`) | no | no | yes |

`viewer` and `analyst` are not distinguished by any check in the current code.
Everything is either available to a verified identity or restricted to `admin`.
Stated because the role list implies a three-tier model that the enforcement does
not yet implement — `analyst` is a label, not a privilege level.

The thirteen `Depends(require_admin)` endpoints are the three connection tests and
the create/update/delete endpoints for models, data sources and MCP servers.
`require_admin()` raises 403 unless the request carries a verified identity whose
role is exactly `admin`; an identity flagged `unauthenticated` is refused
whatever its role field says.

The connection tests are administrator-gated **because of what they used to be
able to do**, not only because they write configuration: they were the command
execution and request forgery paths. Defence in depth, since both underlying
defects are fixed as well.

One oddity worth naming rather than leaving to be discovered:
`POST /api/auth/change-password` is behind the same `role != "admin"` check, so a
`viewer` cannot change their own password. That is over-restrictive rather than
unsafe, and it is not changed here because loosening an authorization check is not
something to do in a security fix without a reason to.

### The disabled-authentication mode

`AGENTIC_AUTO_AUTH_ENABLED=false` exists for local development. Four things
constrain it, and the Guardian asked for the opt-out to be explicit, documented
and warned about:

1. Authentication is **on** unless the variable is the exact string `false`. A
   typo, an empty value, or an unset variable all leave it on.
2. With it off, `auth.py` **raises at import** if `AGENTIC_DATA_ENV` is
   `production` — and `AGENTIC_DATA_ENV` itself defaults to `production`. A
   deployment cannot reach the opt-out by accident.
3. The identity injected in that mode is `viewer`, flagged `unauthenticated`. It
   is refused by `require_admin()`. It used to be `admin`.
4. The module logs a boxed warning at import saying every request runs
   unauthenticated and that the process must not be exposed to a network.

The CDK stack sets `AGENTIC_AUTO_AUTH_ENABLED: 'true'` explicitly on the Fargate
task rather than relying on the library default — relying on a default is what
produced the original defect.

### Where authorization is *not* the control

Two things the role model deliberately does not do, so nobody assumes otherwise:

- **Row- and column-level data access.** A `viewer` who can ask questions can ask
  about anything in a registered data source. Scoping by row or column is the
  data source's job, through the credentials the data source is registered with.
- **Outbound reachability.** `require_admin` limits *who* can ask the server to
  fetch a URL. What it may fetch is enforced separately, in
  `agentic_core/net_guard.py`, because an administrator should not be able to read
  the task role's credentials out of the metadata service either.
