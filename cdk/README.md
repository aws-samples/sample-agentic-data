# Agentic Data — CDK Deployment

Multi-Agent Data Analytics Platform powered by Strands Agents SDK + Amazon Bedrock.

## Prerequisites

- AWS CLI configured with appropriate credentials
- Node.js 18+
- Docker (for building container image)
- CDK CLI: `npm install -g aws-cdk`

## Quick Start

```bash
cd cdk
npm install

# Deploy with defaults (Bedrock + local auth)
cdk deploy AgenticDataStack \
  --parameters AdminPassword=<YOUR_SECURE_PASSWORD> \
  --parameters AllowedCIDRs=10.0.0.0/8

# Deploy with OpenAI-compatible model
cdk deploy AgenticDataStack \
  --context model_provider=openai_compatible \
  --context openai_base_url=https://api.siliconflow.cn/v1 \
  --context openai_api_key=sk-xxx \
  --context openai_model=Pro/zai-org/GLM-5 \
  --parameters AdminPassword=<YOUR_SECURE_PASSWORD> \
  --parameters AllowedCIDRs=10.0.0.0/8

# Deploy with existing VPC + RDS
cdk deploy AgenticDataStack \
  --context vpc_id=vpc-xxx \
  --context enable_rds=true \
  --parameters AdminPassword=<YOUR_SECURE_PASSWORD> \
  --parameters AllowedCIDRs=10.0.0.0/8

# China region
cdk deploy AgenticDataStack \
  --context region_type=china \
  --context model_provider=openai_compatible \
  --context openai_base_url=https://api.siliconflow.cn/v1 \
  --context openai_api_key=sk-xxx \
  --context openai_model=Pro/zai-org/GLM-5 \
  --parameters AdminPassword=<YOUR_SECURE_PASSWORD> \
  --parameters AllowedCIDRs=10.0.0.0/8
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `AdminPassword` | CFN Parameter | `<YOUR_SECURE_PASSWORD>` | Admin user password |
| `AllowedCIDRs` | CFN Parameter | `10.0.0.0/8` | Comma-separated CIDRs for ALB (⛔ NEVER 0.0.0.0/0) |

## Context Variables

| Variable | Default | Options |
|----------|---------|---------|
| `region_type` | `global` | `global`, `china` |
| `model_provider` | `bedrock` | `bedrock`, `openai_compatible` |
| `model_id` | `us.anthropic.claude-sonnet-4-6` | Any Bedrock model ID |
| `openai_base_url` | - | OpenAI-compatible endpoint URL |
| `openai_api_key` | - | API key |
| `openai_model` | - | Model name |
| `auth_provider` | `local` | `local`, `cognito`, `authing`, `entra_id` |
| `vpc_id` | - | Use existing VPC (creates new if empty) |
| `enable_rds` | `false` | `true` to create RDS PostgreSQL |

## Architecture

```
Internet → ALB (port 80, CIDR whitelist)
              → ECS Fargate (1 task, 1vCPU/2GB)
                  → DynamoDB × 5 (config, chat, cost, events, feedback)
                  → S3 (data + reports)
                  → Bedrock / OpenAI API (model inference)
                  → [Optional] RDS PostgreSQL
                  → [Optional] Athena + Glue (data lake)
```

## Post-Deployment

1. Open the `ApplicationURL` from CDK output
2. Login: `admin` / your password
3. Follow the onboarding guide:
   - Connect data sources (PG/Athena/Snowflake/S3)
   - Create Agents
   - Configure Scenes
   - Start asking questions

## Cost Estimate

| Component | Monthly Cost |
|-----------|-------------|
| ECS Fargate (1 task) | ~$15 |
| DynamoDB (on-demand) | ~$1-5 |
| S3 | ~$1 |
| ALB | ~$16 |
| NAT Gateway | ~$32 |
| RDS (if enabled) | ~$30 |
| **Total (without RDS)** | **~$65** |
| **Total (with RDS)** | **~$95** |

*Bedrock model costs are usage-based and not included.*

## Cleanup

```bash
cdk destroy AgenticDataStack
```

Note: DynamoDB tables and S3 bucket have `RETAIN` policy and must be deleted manually.
