# Agentic Data — One-Click CloudFormation Deployment Spec

## Overview
Build a **production-grade CloudFormation template** that deploys the entire Agentic ChatBI.
The output files go in `/home/ubuntu/projects/agentic-auto/deploy/`.

## Architecture

```
Internet → ALB (HTTPS optional) → Target Group
                                      ↓
                            EC2 (Option A) or ECS Fargate (Option B)
                                      ↓
                              Docker Container
                              (FastAPI + React SPA)
                                      ↓
                    S3 (data) + DynamoDB (5 tables) + Bedrock
```

## Requirements

### 1. CloudFormation Template (`cfn-agentic-data.yaml`)
- **Single YAML file**, max 2 templates (main + optional nested)
- **Parameters:**
  - `DeploymentMode`: EC2 | ECS (default: EC2)
  - `InstanceType`: default t4g.medium (ARM/Graviton)
  - `SupervisorModel`: Bedrock model ID (default: us.anthropic.claude-sonnet-4-6-20250514)
  - `SubAgentModel`: Bedrock model ID (default: us.anthropic.claude-haiku-4-5-20250515)
  - `VpcCidr`: default 10.0.0.0/16
  - `EnableAuth`: true/false (default: false)
  - `ChinaRegion`: true/false (default: false) — switches endpoints, model prefixes, partition
  - `KeyPairName`: EC2 SSH key (optional, for EC2 mode)

### 2. Resources to Create

**Networking:**
- VPC (2 public + 2 private subnets, NAT Gateway)
- ALB (public subnets) with HTTP listener (port 80)
- Security Groups: ALB (80/443 from 0.0.0.0/0), App (8501 from ALB SG only)

**Compute (EC2 mode):**
- EC2 instance (t4g.medium, Amazon Linux 2023 ARM)
- UserData: install Docker, pull image, run container
- Auto-recovery via CloudWatch alarm

**Compute (ECS mode):**
- ECS Cluster + Fargate Service + Task Definition
- 1 task, 1 vCPU, 2GB memory
- Container port 8501

**Storage:**
- S3 bucket: `agentic-data-${AWS::AccountId}-${AWS::Region}` (data + reports)
- DynamoDB tables (5): events, chat-history, cost, config, feedback (PAY_PER_REQUEST)

**IAM (Customer Managed ONLY — NO aws managed policies):**
- EC2/ECS Task Role with ONLY:
  - bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream (specific model ARNs)
  - bedrock:ApplyGuardrail (specific guardrail ARN with wildcard version)
  - s3:GetObject, s3:PutObject, s3:ListBucket (only our bucket)
  - dynamodb:GetItem, dynamodb:PutItem, dynamodb:Query, dynamodb:Scan, dynamodb:UpdateItem, dynamodb:DeleteItem (only our 5 tables)
  - athena:StartQueryExecution, athena:GetQueryExecution, athena:GetQueryResults (scoped)
  - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
- EC2 Instance Profile (EC2 mode)
- ECS Task Execution Role (ECS mode): ONLY ecr:GetAuthorizationToken, ecr:BatchGetImage, ecr:GetDownloadUrlForLayer, logs:*

**China Region Conditions:**
- When ChinaRegion=true:
  - Use `aws-cn` partition in ARNs
  - Use `*.amazonaws.com.cn` endpoints
  - Model IDs: strip `us.` prefix, use `anthropic.claude-3-sonnet-20240229-v1:0` / `anthropic.claude-3-haiku-20240307-v1:0`
  - S3 bucket names: append `-cn`
  - ECR: use Chinese mirror or build locally

### 3. Dockerfile (`Dockerfile`)
```dockerfile
FROM python:3.12-slim
# Install deps, copy code, init sample data, expose 8501
# Multi-stage: build SQLite data in build stage, copy to runtime
# Include chatbi JSON data generation
# Health check: curl localhost:8501/api/health
```

### 4. Docker Compose for local testing (`docker-compose.yml`)

### 5. Init Script (`init-sample-data.sh`)
- Generate ChatBI JSON datasets (8 files) 
- Init SQLite DB (vehicle_sales + customer_profiles)
- Upload to S3 bucket

### 6. Deploy Script (`deploy.sh`)
```bash
#!/bin/bash
# Usage: ./deploy.sh [--mode ec2|ecs] [--china] [--region us-east-1]
# 1. Build Docker image
# 2. Push to ECR (or load locally for EC2)
# 3. Deploy CloudFormation stack
# 4. Wait for stack completion
# 5. Output ALB URL
```

## Code References
- **Backend**: `api/main.py` (FastAPI, port 8501, serves React SPA from web/)
- **Frontend**: `web/index.html` (React SPA, served as static from FastAPI)
- **Config**: `config.py` (all env vars with AGENTIC_AUTO_ prefix)
- **Data init**: `scripts/init_sqlite.py`, `scripts/load_events.py`
- **ChatBI data**: loaded from S3 `chatbi/*.json` keys
- **Sample data**: `sample_data/output/*.json`

## Key Constraints
1. **NO AWS Managed IAM Policies** — all policies must be inline or customer-managed
2. **Minimum privilege** — scope every permission to specific resources
3. **China Region compatible** — aws-cn partition, .cn endpoints
4. **Front/back separation** — React SPA in web/, API in api/, same container but clear separation
5. **ALB health check** → GET /api/health (or GET / returning 200)
6. **Graviton (ARM)** preferred for EC2 (cost-effective)

## File Structure to Create
```
deploy/
├── cfn-agentic-data.yaml     # Main CloudFormation template
├── Dockerfile                  # Multi-stage Docker build
├── docker-compose.yml          # Local testing
├── deploy.sh                   # One-click deploy script
├── init-sample-data.py         # Generate all sample data
├── .dockerignore               # Exclude unnecessary files
└── README.md                   # Deployment guide
```
