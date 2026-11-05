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
AGENTIC_AUTO_JWT_SECRET=$(openssl rand -base64 48) python -m pytest tests/test_auth_regression.py -v
```

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
