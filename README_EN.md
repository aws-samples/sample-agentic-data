# Agentic ChatBI

**Semantic Layer + Multi-Agent Analytics Platform**

[中文](README.md) | **English**

Multi-agent ChatBI platform — three-tier deterministic architecture (Semantic Layer + VQR + NL2SQL) for intelligent data analysis through natural language conversations across multiple data sources.

## Overview

Agentic Data is an enterprise-grade AI Agent-powered data analytics platform. Users can ask questions in natural language, which are automatically routed to the appropriate data source (Athena / PostgreSQL / MySQL / Snowflake), translated into SQL, and returned with visualized results.

Core capabilities:
- **Natural Language Queries** — ask in Chinese or English, auto-generates and executes SQL
- **Multi-Data Source Support** — Athena, PostgreSQL, MySQL, Snowflake, S3
- **Multi-Agent Collaboration** — Supervisor intelligent routing + specialized Sub-Agent parallel analysis
- **Semantic Layer** — predefined metrics, dimensions, and synonyms to improve query accuracy
- **VQR (Verified Query Repository)** — prioritize matching verified SQL to avoid redundant generation
- **Real-time Streaming** — SSE push for analysis process and results
- **Auto Visualization** — query results automatically rendered as ECharts charts
- **Alerts & KPI** — configure alert rules and dashboard metrics via natural language

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    ALB (HTTPS:443)                     │
├─────────────────────────────────────────────────────┤
│              ECS Fargate (ARM64)                     │
│  ┌───────────────────────────────────────────────┐  │
│  │  FastAPI (uvicorn)                            │  │
│  │  ├── /api/chat (SSE streaming)                │  │
│  │  ├── /api/config, /api/cost, /api/data-catalog│  │
│  │  └── Static Web (React SPA)                   │  │
│  ├───────────────────────────────────────────────┤  │
│  │  Agentic Core                                 │  │
│  │  ├── Supervisor Agent (routing + orchestration)│  │
│  │  ├── DataAnalyst Agent (SQL gen + execution)  │  │
│  │  ├── Orchestrator (direct/parallel/pipeline)  │  │
│  │  ├── Semantic Layer (metrics/dimensions)       │  │
│  │  └── VQR (verified query cache)               │  │
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│  AWS Services                                        │
│  ├── DynamoDB x5 (config/chat/cost/events/feedback) │
│  ├── S3 (data lake + report storage)                │
│  ├── Athena (data lake queries)                     │
│  ├── Bedrock / OpenAI Compatible (LLM)              │
│  └── RDS PostgreSQL/MySQL (optional)                │
└─────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React (CDN) + ECharts + Leaflet |
| Backend | Python 3.12 + FastAPI + Uvicorn |
| AI Agent | [Strands Agents SDK](https://github.com/strands-agents/sdk-python) |
| LLM | Bedrock (Claude) / OpenAI Compatible / SiliconFlow / LiteLLM |
| Infrastructure | AWS CDK (TypeScript) |
| Container | ECS Fargate (ARM64) |
| Storage | DynamoDB + S3 + Athena |
| Auth | Local Auth / Cognito / Authing |

## Prerequisites

- **AWS CLI** — configured with credentials and region
- **Node.js 18+** — for CDK and frontend build
- **Docker** — for container image build
- **AWS CDK CLI** — for infrastructure deployment

```bash
# Verify environment
aws sts get-caller-identity
node --version        # >= 18
docker info
cdk --version         # if not installed: npm install -g aws-cdk
```

## Quick Deploy

### 1. CDK Bootstrap (first time only)

```bash
cd cdk
npm install
npx cdk bootstrap
```

> To specify a region: `npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>`

### 2. Interactive Deployment

```bash
bash deploy.sh
```

The script will guide you through configuration:

| Setting | Description | Default |
|---------|-------------|---------|
| Region | Deployment region | AWS CLI default region |
| Admin Password | Admin login password | `Admin@2026` |
| Security Group Allowlist | IPs allowed to access ALB | `10.0.0.0/8` |
| VPC | Use existing VPC or create new | Create new |
| Model | LLM provider config | Global: Bedrock / China: manual config |
| RDS | Enable PostgreSQL | No |

### 3. Manual Deployment (skip interactive)

```bash
cd cdk
npm install
npx cdk deploy \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32" \
  --require-approval broadening
```

### 4. Advanced Configuration (CDK Context)

```bash
# China region + OpenAI compatible model
npx cdk deploy \
  -c model_provider=openai \
  -c openai_base_url=https://api.siliconflow.cn/v1 \
  -c openai_api_key=sk-xxx \
  -c openai_model=Pro/Qwen/Qwen2.5-72B-Instruct \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"

# Use existing VPC + enable RDS
npx cdk deploy \
  -c vpc_id=vpc-xxx \
  -c enable_rds=true \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"

# Specify Bedrock region (app in ap-southeast-1, Bedrock in us-east-1)
npx cdk deploy \
  -c bedrock_region=us-east-1 \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"
```

## Deployment Outputs

After deployment, CDK outputs:

```
Outputs:
  AgenticDataStack.ApplicationURL = http://Agenti-ALB-xxxxx.elb.amazonaws.com
  AgenticDataStack.AdminUser = admin
  AgenticDataStack.ConfigTableName = agentic-data-config
  AgenticDataStack.DataBucketName = agenticdatastack-databucket-xxxxx
```

## Post-Deployment Setup

### 1. Login

Open `ApplicationURL` and log in with `admin` / your configured password.

### 2. Configure Models (if skipped during deployment)

Go to **Admin Console > Model Management** and add an LLM:

| Region | Recommended Model |
|--------|-------------------|
| Global | Bedrock Claude Sonnet 4.6 (configured by default) |
| China | SiliconFlow / ZhipuAI (GLM) / DeepSeek |

### 3. Connect Data Sources

Go to **Admin Console > Data Source Management**:

- **Athena** — S3 data lake queries (requires database, tables, output location)
- **PostgreSQL / MySQL** — RDS or self-hosted databases
- **Snowflake** — cloud data warehouse
- **S3** — analyze CSV/JSON files directly

### 4. Configure Semantic Layer (optional)

Go to **Admin Console > Semantic Layer** to define:
- **Metrics** — e.g. "daily output" = `SUM(output_qty)`
- **Dimensions** — e.g. "factory" = `factory_name`
- **Synonyms** — e.g. "production" -> "daily output"
- **Query Templates** — predefined SQL for common queries

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export AGENTIC_AUTO_REGION=us-east-1
export AGENTIC_AUTO_CONFIG_TABLE=agentic-data-config
export AGENTIC_AUTO_CHAT_TABLE=agentic-data-chat-history
export AGENTIC_AUTO_COST_TABLE=agentic-data-cost
export AGENTIC_AUTO_EVENTS_TABLE=agentic-data-events
export AGENTIC_AUTO_FEEDBACK_TABLE=agentic-data-feedback
export AGENTIC_AUTO_DATA_BUCKET=your-data-bucket

# Start
python -m uvicorn api.main:app --host 0.0.0.0 --port 8501 --reload
```

Visit `http://localhost:8501`

## Project Structure

```
├── api/
│   └── main.py              # FastAPI entry (REST + SSE)
├── agentic_core/
│   ├── agents.py             # Supervisor + Sub-Agent definitions
│   ├── orchestrator.py       # Orchestration engine (direct/parallel/pipeline/supervisor)
│   ├── tools.py              # Agent tools (NL2SQL, semantic query, reports, etc.)
│   ├── semantic_layer.py     # Semantic layer (metrics/dimensions/templates)
│   ├── dynamic_context.py    # Dynamic prompt construction
│   ├── vqr.py                # Verified Query Repository
│   ├── db_engine.py          # Multi-database engine abstraction
│   ├── agent_registry.py     # Agent definition registry
│   ├── alert_rules.py        # Alert/KPI rule engine
│   ├── sql_cache.py          # SQL cache
│   ├── user_memory.py        # User memory
│   ├── local_auth.py         # Local authentication
│   ├── cognito_auth.py       # Cognito authentication
│   └── authing_auth.py       # Authing authentication
├── web/
│   └── index.html            # React SPA (single file)
├── cdk/
│   ├── lib/
│   │   └── agentic-data-stack.ts  # CDK infrastructure definition
│   ├── bin/cdk.ts            # CDK entry point
│   └── deploy.sh             # Interactive deploy script
├── config.py                 # Centralized config (env vars)
├── auth.py                   # Auth middleware
├── Dockerfile                # Production image
└── requirements.txt          # Python dependencies
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AGENTIC_AUTO_REGION` | AWS region | `us-east-1` |
| `BEDROCK_REGION` | Bedrock region (can differ from app region) | Same as REGION |
| `AGENTIC_AUTO_MODEL_PROVIDER` | Model provider (`bedrock` / `openai`) | `bedrock` |
| `AGENTIC_AUTO_SUP_MODEL` | Supervisor model ID | Claude Sonnet 4.6 |
| `AGENTIC_AUTO_SUB_MODEL` | Sub-Agent model ID | Claude Haiku 4.5 |
| `SQL_ENGINE` | SQL engine (`sqlite` / `postgresql` / `snowflake`) | `sqlite` |
| `AGENTIC_AUTO_AUTH_ENABLED` | Enable authentication | `false` |
| `AGENTIC_AUTO_PORT` | Application port | `8501` |

See [config.py](config.py) for the full list.

## Security

- ALB security group blocks `0.0.0.0/0` — allowlisted IPs required
- Supports Local Auth / Cognito / Authing authentication
- DynamoDB tables set to `RETAIN` deletion protection
- S3 Bucket defaults to Block All Public Access
- Supports Bedrock Guardrails for content safety filtering

## Cleanup

```bash
cd cdk
npx cdk destroy
```

> Note: DynamoDB tables and S3 Bucket are set to `RETAIN` and will not be deleted with the stack. Manual cleanup required.

## License

Private — All rights reserved.
