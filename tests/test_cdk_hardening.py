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
import shutil
import subprocess  # nosec B404
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDK = os.path.join(ROOT, "cdk")

# Resolved to an absolute path rather than relying on PATH lookup: a relative
# executable name is resolved against whatever PATH happens to hold when this
# runs, which is not something a test should leave to chance.
NPX = shutil.which("npx")

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}{('  — ' + str(detail)) if detail else ''}")


def _run_synth(context=None):
    """Run `cdk synth --json`. The only place in this file that starts a process.

    Running synth *is* the cdk_nag scan, since the pack is registered as a
    validation plugin, so a non-zero exit is itself a finding.

    Every element of the argument list is a literal: the executable is resolved
    from PATH once at import, the subcommand is fixed, and the context keys and
    values come from the two call sites below, which are also literals. shell is
    False, so the list goes to execve as-is and nothing parses it as a command
    line. There is one call site rather than two so that this reasoning lives in
    one place.
    """
    if not os.path.isdir(os.path.join(CDK, "node_modules")):
        print("SKIP: cdk/node_modules is absent. Run `npm install` in cdk/ first.")
        sys.exit(0)
    if not NPX:
        print("SKIP: npx is not on PATH.")
        sys.exit(0)
    args = [NPX, "cdk", "synth", "--json"]
    for key, value in (context or {}).items():
        args += ["-c", f"{key}={value}"]
    # Pinned to the stack's own default region so the result does not depend on
    # whatever AWS_REGION the developer's shell happens to export. The VPC needs
    # an availability-zone lookup per region; in an uncached region that lookup
    # calls AWS and fails without live credentials, which made this suite pass or
    # fail depending on the machine rather than on the template.
    env = dict(os.environ, AWS_REGION="us-east-1", AWS_DEFAULT_REGION="us-east-1",
               CDK_DEFAULT_REGION="us-east-1")
    # nosemgrep: dangerous-subprocess-use-audit
    return subprocess.run(  # nosec B603 B607
        args, cwd=CDK, capture_output=True, text=True, timeout=900, shell=False, env=env,
    )


def synth():
    proc = _run_synth()
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
# Names only. The values are ARNs, but a variable holding anything taken from a
# block called "Secrets" and then printed is a shape static analysis flags on
# sight, and there is nothing here worth printing anyway.
injected_names = sorted(entry["Name"] for entry in container.get("Secrets", []))

check("the task sets AGENTIC_AUTO_AUTH_ENABLED=true",
      env.get("AGENTIC_AUTO_AUTH_ENABLED") == "true", env.get("AGENTIC_AUTO_AUTH_ENABLED"))
check("the task sets AGENTIC_DATA_ENV=production",
      env.get("AGENTIC_DATA_ENV") == "production", env.get("AGENTIC_DATA_ENV"))

jwt_param = PARAMS.get("JwtSigningSecret", {})
jwt_facts = {
    "no default": "Default" not in jwt_param,
    "noEcho": jwt_param.get("NoEcho") in (True, "true"),
    "min length 32": jwt_param.get("MinLength") == 32,
}
check("JwtSigningSecret is required, NoEcho, min 32",
      all(jwt_facts.values()),
      "failing: " + ", ".join(k for k, v in jwt_facts.items() if not v))


# ── secrets must not be written into resource properties ────────────
# A noEcho parameter referenced from `environment` is resolved into the task
# definition in plaintext, where ecs:DescribeTaskDefinition reads it. Task
# definition revisions are retained, so it outlives a rotation.
check("the JWT signing secret is not in the environment block",
      "AGENTIC_AUTO_JWT_SECRET" not in env)
check("the JWT signing secret is injected from Secrets Manager",
      "AGENTIC_AUTO_JWT_SECRET" in injected_names)
check("Secrets Manager secrets are created for the deploy-time credentials",
      len(of_type("AWS::SecretsManager::Secret")) >= 2)

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
    proc = _run_synth(ctx)
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

# ── no deploy-time credential may reach a resource property ─────────
# Failure details in this section are counts only. Anything derived from the
# container's Secrets block or from credential-named variables stays out of
# print(), even when it holds names rather than values.
# Done with canaries rather than by reading the TypeScript: synthesise with
# distinctive values and then search the whole template for them. A parameter or
# context value referenced from a task definition's `environment` or a Lambda's
# `environment` is resolved into that resource in plaintext, where
# ecs:DescribeTaskDefinition and lambda:GetFunctionConfiguration return it, and
# task definition revisions are retained so it outlives a rotation.

CANARY_OPENAI = "CANARY-OPENAI-KEY-DO-NOT-SHIP"
CANARY_RDS = "CANARY-RDS-PASSWORD-DO-NOT-SHIP"

canary_template, _ = synth_with({
    "openai_api_key": CANARY_OPENAI,
    "rds_engine": "postgresql",
    "rds_host": "db.example.invalid",
    "rds_user": "svc",
    "rds_password": CANARY_RDS,
})

if canary_template is None:
    check("a deploy-time credential never reaches a task definition or Lambda", False,
          "canary synth failed")
else:
    canary_res = canary_template["Resources"]

    def _carriers(canary):
        return {
            res.get("Type"): lid
            for lid, res in canary_res.items()
            if canary in json.dumps(res)
        }

    for canary, label in ((CANARY_OPENAI, "model API key"), (CANARY_RDS, "data-source password")):
        carriers = _carriers(canary)
        # A context value is a literal at synth time, so a Secrets Manager
        # resource is the one place it legitimately lands; the stack warns and
        # offers a *_secret_arn context value to avoid even that.
        unexpected = {t: l for t, l in carriers.items() if t != "AWS::SecretsManager::Secret"}
        check(f"the {label} reaches nothing but a Secrets Manager resource",
              not unexpected, f"{len(unexpected)} other resource type(s)")

    canary_td = [r for r in canary_res.values() if r["Type"] == "AWS::ECS::TaskDefinition"][0]
    canary_container = canary_td["Properties"]["ContainerDefinitions"][0]
    env_names = [e["Name"] for e in canary_container.get("Environment", [])]
    secret_names = [x["Name"] for x in canary_container.get("Secrets", [])]

    check("no environment variable on the task is named like a credential",
          not [n for n in env_names if "PASSWORD" in n or "API_KEY" in n or "SECRET" in n])
    check("a configured data-source password arrives through the secrets block",
          "POSTGRES_PASSWORD" in secret_names)

    canary_lambdas = {
        lid: r for lid, r in canary_res.items() if r["Type"] == "AWS::Lambda::Function"
    }
    offenders = []
    for lid, fn in canary_lambdas.items():
        for name, value in (fn["Properties"].get("Environment", {}).get("Variables", {}) or {}).items():
            if not any(t in name for t in ("PASSWORD", "API_KEY", "SECRET")):
                continue
            # Only an ARN reference is acceptable here.
            if not name.endswith("_SECRET_ARN"):
                offenders.append(f"{lid}.{name}")
    check("a Lambda receives credentials only as a secret ARN", not offenders,
          f"{len(offenders)} variable(s)")

    # OPENAI_API_KEY was on the task definition and nothing read it from the
    # environment: the application takes the key from the model record in
    # DynamoDB. It disclosed a credential no consumer wanted.
    check("the unread OPENAI_API_KEY environment variable is gone",
          "OPENAI_API_KEY" not in env_names)


print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"All {len(PASSED)} CDK hardening checks passed.")
