"""Regression checks on the synthesised CloudFormation template.

The Python suites cover the application. These cover the deployment, because the
reported defect was half an infrastructure defect: the stack never set the
variable that turned authentication on, and put the load balancer on the
internet in plaintext.

Synthesises the stack and asserts against the result rather than reading the
TypeScript, so a change that type-checks but produces the wrong template is
caught. Run as a script:

    python3 tests/test_cdk_hardening.py

Needs `npm install` in cdk/ to have been run. Without it the suite SKIPS with
that reason rather than passing silently.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDK = os.path.join(ROOT, "cdk")

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}{('  — ' + str(detail)) if detail else ''}")


def synth():
    if not os.path.isdir(os.path.join(CDK, "node_modules")):
        print("SKIP: cdk/node_modules is absent. Run `npm install` in cdk/ first.")
        sys.exit(0)
    # Runs the cdk_nag plugin too, so a non-zero exit is itself a finding.
    proc = subprocess.run(
        ["npx", "cdk", "synth", "--json"],
        cwd=CDK, capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        print("FAIL  cdk synth exited non-zero. cdk_nag findings, or a synth error:")
        print("\n".join(proc.stderr.splitlines()[-25:]))
        sys.exit(1)
    return json.loads(proc.stdout)


template = synth()
print("  PASS  cdk synth succeeds with no unacknowledged cdk_nag finding")
PASSED.append("cdk synth clean")

RES = template["Resources"]
PARAMS = template.get("Parameters", {})


def of_type(t):
    return [r for r in RES.values() if r.get("Type") == t]


# ── network exposure ────────────────────────────────────────────────
alb = of_type("AWS::ElasticLoadBalancingV2::LoadBalancer")[0]
check("load balancer is internal unless explicitly opted out",
      alb["Properties"].get("Scheme") == "internal",
      alb["Properties"].get("Scheme"))

alb_attrs = {a["Key"]: a.get("Value") for a in alb["Properties"].get("LoadBalancerAttributes", [])}
check("load balancer access logs are enabled",
      str(alb_attrs.get("access_logs.s3.enabled")).lower() == "true",
      alb_attrs.get("access_logs.s3.enabled"))

# The default deployment is internal on plaintext HTTP. That is a trade-off, not
# an oversight -- tokens still cross the VPC in the clear -- so the stack emits a
# synth-time warning and a certificate switches it to HTTPS. What must hold
# unconditionally is the pair below: nothing plaintext is ever internet-facing,
# and supplying a certificate really does produce TLS plus a redirect.
check("the default internal listener is the only plaintext one",
      all(
          l["Properties"].get("Protocol") != "HTTP"
          or alb["Properties"].get("Scheme") == "internal"
          or any(a.get("Type") == "redirect" for a in l["Properties"].get("DefaultActions", []))
          for l in of_type("AWS::ElasticLoadBalancingV2::Listener")
      ))

cidr = PARAMS.get("AllowedCIDRs", {})
check("AllowedCIDRs has no default", "Default" not in cidr)
check("AllowedCIDRs pattern rejects 0.0.0.0/0",
      "0\\.0\\.0\\.0/0" in cidr.get("AllowedPattern", ""),
      cidr.get("AllowedPattern"))

check("a VPC flow log exists", bool(of_type("AWS::EC2::FlowLog")))


# ── authentication ──────────────────────────────────────────────────
taskdefs = of_type("AWS::ECS::TaskDefinition")
check("exactly one task definition", len(taskdefs) == 1, len(taskdefs))
container = taskdefs[0]["Properties"]["ContainerDefinitions"][0]
env = {e["Name"]: e.get("Value") for e in container.get("Environment", [])}
secrets = {s["Name"]: s.get("ValueFrom") for s in container.get("Secrets", [])}

check("the task sets AGENTIC_AUTO_AUTH_ENABLED=true",
      env.get("AGENTIC_AUTO_AUTH_ENABLED") == "true", env.get("AGENTIC_AUTO_AUTH_ENABLED"))
check("the task sets AGENTIC_DATA_ENV=production",
      env.get("AGENTIC_DATA_ENV") == "production", env.get("AGENTIC_DATA_ENV"))

jwt = PARAMS.get("JwtSigningSecret", {})
check("JwtSigningSecret is required, NoEcho, min 32",
      "Default" not in jwt and jwt.get("NoEcho") in (True, "true") and jwt.get("MinLength") == 32,
      {k: jwt.get(k) for k in ("Default", "NoEcho", "MinLength")})


# ── secrets must not be written into resource properties ────────────
# A noEcho parameter referenced from `environment` is resolved into the task
# definition in plaintext, where ecs:DescribeTaskDefinition reads it. Task
# definition revisions are retained, so it outlives a rotation.
check("the JWT signing secret is not in the environment block",
      "AGENTIC_AUTO_JWT_SECRET" not in env)
check("the JWT signing secret is injected from Secrets Manager",
      "AGENTIC_AUTO_JWT_SECRET" in secrets, list(secrets))
check("Secrets Manager secrets are created for the deploy-time credentials",
      len(of_type("AWS::SecretsManager::Secret")) >= 2,
      len(of_type("AWS::SecretsManager::Secret")))

lambdas = {k: v for k, v in RES.items() if v.get("Type") == "AWS::Lambda::Function"}
plaintext_pw = [
    k for k, v in lambdas.items()
    if "ADMIN_PASSWORD" in (v["Properties"].get("Environment", {}).get("Variables", {}) or {})
]
check("no Lambda carries the admin password as an environment variable",
      not plaintext_pw, plaintext_pw)

init = [v for k, v in lambdas.items() if "InitDataFn" in k]
check("the init Lambda receives a secret ARN instead of the password",
      bool(init) and "ADMIN_PASSWORD_SECRET_ARN" in init[0]["Properties"]["Environment"]["Variables"])

custom = [r for r in RES.values() if str(r.get("Type", "")).startswith(("Custom::", "AWS::CloudFormation::CustomResource"))]
check("no custom resource property carries the admin password",
      not any("adminPassword" in json.dumps(r.get("Properties", {})) for r in custom))


# ── data protection ─────────────────────────────────────────────────
tables = of_type("AWS::DynamoDB::Table")
check("every DynamoDB table has point-in-time recovery",
      bool(tables) and all(
          t["Properties"].get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled")
          for t in tables
      ))
check("every DynamoDB table is retained on stack delete",
      all(
          [r for r in RES.values() if r is t][0].get("DeletionPolicy") == "Retain"
          for t in tables
      ) if tables else False)

buckets = of_type("AWS::S3::Bucket")
check("every bucket blocks all public access",
      bool(buckets) and all(b["Properties"].get("PublicAccessBlockConfiguration") for b in buckets))
check("a bucket is configured for server access logging",
      any("LoggingConfiguration" in b["Properties"] for b in buckets))
check("a bucket policy denies requests that are not over TLS",
      any("aws:SecureTransport" in json.dumps(p) for p in of_type("AWS::S3::BucketPolicy")))


# ── least privilege ─────────────────────────────────────────────────
# The security-group Lambda could rewrite the ingress rules of every security
# group in the account while its mutating actions were on Resource: "*".
sg_policies = [
    v for k, v in RES.items()
    if v.get("Type") == "AWS::IAM::Policy" and "SgRuleFn" in k
]
mutating = []
for pol in sg_policies:
    for st in pol["Properties"]["PolicyDocument"]["Statement"]:
        acts = st.get("Action")
        acts = acts if isinstance(acts, list) else [acts]
        if any(str(a).endswith("SecurityGroupIngress") for a in acts):
            mutating.append(st)
check("security group mutation is scoped to a specific group, not '*'",
      bool(mutating) and all(st.get("Resource") != "*" for st in mutating),
      [st.get("Resource") for st in mutating])

task_policies = [
    v for k, v in RES.items()
    if v.get("Type") == "AWS::IAM::Policy" and "TaskRoleDefaultPolicy" in k
]
star_actions = []
for pol in task_policies:
    for st in pol["Properties"]["PolicyDocument"]["Statement"]:
        res = st.get("Resource")
        if res == "*" or (isinstance(res, list) and "*" in res):
            acts = st.get("Action")
            star_actions += acts if isinstance(acts, list) else [acts]
# Only the ECS Exec channel actions should remain on "*": they do not support
# resource-level permissions. Anything else appearing here is a regression.
check("the task role's only wildcard resource is the ECS Exec channel set",
      all(str(a).startswith("ssmmessages:") for a in star_actions),
      star_actions)


# ── observability ───────────────────────────────────────────────────
clusters = of_type("AWS::ECS::Cluster")
check("Container Insights is enabled on the cluster",
      bool(clusters) and any(
          s.get("Name") == "containerInsights" and s.get("Value") in ("enabled", "enhanced")
          for s in clusters[0]["Properties"].get("ClusterSettings", [])
      ),
      clusters[0]["Properties"].get("ClusterSettings") if clusters else None)

services = of_type("AWS::ECS::Service")
check("the deployment circuit breaker is enabled",
      bool(services) and services[0]["Properties"]["DeploymentConfiguration"]
      ["DeploymentCircuitBreaker"]["Enable"])


# ── the TLS and public-exposure invariants, by synthesising again ───
def synth_with(ctx, expect_failure=False):
    args = ["npx", "cdk", "synth", "--json"]
    for k, v in ctx.items():
        args += ["-c", f"{k}={v}"]
    proc = subprocess.run(args, cwd=CDK, capture_output=True, text=True, timeout=900)
    if expect_failure:
        return proc.returncode != 0, proc.stderr
    if proc.returncode != 0:
        return None, proc.stderr
    return json.loads(proc.stdout), proc.stderr


CERT = "arn:aws:acm:us-east-1:111111111111:certificate/00000000-0000-0000-0000-000000000000"

failed, err = synth_with({"publicly_accessible": "true"}, expect_failure=True)
check("publicly_accessible without a certificate refuses to synthesise",
      failed and "requires certificate_arn" in err,
      err.splitlines()[-1] if err else "")

tls, _ = synth_with({"publicly_accessible": "true", "certificate_arn": CERT})
if tls is None:
    check("a certificate produces an internet-facing HTTPS listener", False, "synth failed")
else:
    tls_res = tls["Resources"]
    tls_alb = [r for r in tls_res.values()
               if r.get("Type") == "AWS::ElasticLoadBalancingV2::LoadBalancer"][0]
    tls_listeners = [r for r in tls_res.values()
                     if r.get("Type") == "AWS::ElasticLoadBalancingV2::Listener"]
    check("publicly_accessible=true makes the load balancer internet-facing",
          tls_alb["Properties"].get("Scheme") == "internet-facing",
          tls_alb["Properties"].get("Scheme"))
    https = [l for l in tls_listeners if l["Properties"].get("Protocol") == "HTTPS"]
    check("a certificate produces an HTTPS listener on 443",
          bool(https) and https[0]["Properties"].get("Port") == 443)
    check("the HTTPS listener uses a recommended TLS policy",
          bool(https) and "TLS13" in str(https[0]["Properties"].get("SslPolicy", "")).upper()
          or bool(https) and "FS" in str(https[0]["Properties"].get("SslPolicy", "")),
          https[0]["Properties"].get("SslPolicy") if https else None)
    plain = [l for l in tls_listeners if l["Properties"].get("Protocol") == "HTTP"]
    check("every plaintext listener redirects when a certificate is present",
          bool(plain) and all(
              any(a.get("Type") == "redirect" for a in l["Properties"].get("DefaultActions", []))
              for l in plain
          ),
          [l["Properties"].get("DefaultActions") for l in plain])
    check("no plaintext listener is internet-facing without a redirect",
          all(
              any(a.get("Type") == "redirect" for a in l["Properties"].get("DefaultActions", []))
              for l in plain
          ))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"All {len(PASSED)} CDK hardening checks passed.")
