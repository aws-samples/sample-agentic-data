# Agentic ChatBI — Deployment Guide

## Prerequisites

- AWS CLI v2 configured with credentials
- Docker (with buildx for multi-arch)
- Python 3.12+ (for sample data generation)
- Bedrock model access enabled in target region

## Quick Start

```bash
# One-click deploy (EC2 mode, us-east-1)
cd deploy
./deploy.sh --init-data

# ECS Fargate mode
./deploy.sh --mode ecs --init-data

# China region
./deploy.sh --china --region cn-north-1 --init-data

# Custom stack name + instance
./deploy.sh --stack my-agentic --instance t4g.large --key my-keypair
```

## Architecture

```
Internet → ALB (HTTP:80)
              ↓
    ┌─────────────────────┐
    │  EC2 or ECS Fargate  │
    │  ┌─────────────────┐ │
    │  │ Docker Container │ │
    │  │ FastAPI + React  │ │
    │  │    port 8501     │ │
    │  └─────────────────┘ │
    └─────────┬───────────┘
              ↓
    ┌─────────────────────┐
    │   S3 (ChatBI data)  │
    │   DynamoDB (5 tables)│
    │   Bedrock (LLM)     │
    │   Athena (telemetry) │
    └─────────────────────┘
```

## Deployment Modes

| Mode | Pros | Cons | Cost |
|------|------|------|------|
| **EC2** | Simple, SSH access, fast deploy | Single instance | ~$30/mo (t4g.medium) |
| **ECS** | Auto-scaling, managed, no SSH needed | Slower deploys | ~$40/mo (1 vCPU/2GB) |

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| DeploymentMode | EC2 | EC2 or ECS |
| InstanceType | t4g.medium | Graviton ARM instance |
| SupervisorModel | us.anthropic.claude-sonnet-4-6 | Bedrock model for Supervisor |
| SubAgentModel | us.anthropic.claude-haiku-4-5 | Bedrock model for Sub-agents |
| ContainerImageUri | (required) | ECR image URI |
| EnableAuth | false | Enable Cognito auth |

## China Region

When deploying to `cn-north-1` or `cn-northwest-1`:
- Use `--china` flag or set `--region cn-north-1`
- Models auto-switch to Claude 3 Sonnet/Haiku (China Bedrock)
- ARNs use `aws-cn` partition
- ECR domain uses `.amazonaws.com.cn`

```bash
./deploy.sh --china --init-data
```

## Post-Deployment

1. **Upload sample data** (if not using `--init-data`):
   ```bash
   ./deploy.sh --init-data
   ```

2. **Enable HTTPS**: Add ACM certificate + HTTPS listener to ALB

3. **Custom domain**: Create Route53 alias record pointing to ALB

## Cost Estimate

| Component | Monthly Cost |
|-----------|-------------|
| EC2 t4g.medium | ~$25 |
| NAT Gateway | ~$32 + data |
| ALB | ~$16 + data |
| DynamoDB (on-demand) | ~$1-5 |
| S3 | ~$1 |
| Bedrock (1000 queries) | ~$30-50 |
| **Total** | **~$105-130/mo** |

## Cleanup

```bash
./deploy.sh --destroy
# Then manually delete S3 bucket (retained by policy):
aws s3 rb s3://agentic-data-ACCOUNT-REGION --force
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| EC2 not healthy | Check UserData logs: `/var/log/cloud-init-output.log` |
| ECS task keeps restarting | Check CloudWatch logs: `/agentic-data/STACK-NAME` |
| 504 timeout | ALB health check failing, container may need more startup time |
| Bedrock access denied | Enable model access in Bedrock console for the region |
