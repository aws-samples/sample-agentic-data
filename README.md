# Agentic ChatBI

**Semantic Layer + Multi-Agent Analytics Platform**

**中文** | [English](README_EN.md)

多智能体对话式 BI 平台 — 语义层 + VQR + NL2SQL 三层确定性架构，通过自然语言对话连接多种数据源进行智能分析。

## 平台简介

Agentic Data 是一个基于 AI Agent 的企业级数据分析平台，支持用户通过自然语言提问，自动路由到合适的数据源（Athena / PostgreSQL / MySQL / Snowflake），生成 SQL 并返回可视化结果。

核心能力：
- **自然语言查询**：用中文或英文提问，自动生成 SQL 并执行
- **多数据源支持**：Athena、PostgreSQL、MySQL、Snowflake、S3
- **多 Agent 协作**：Supervisor 智能路由 + 专业 Sub-Agent 并行分析
- **语义层**：预定义指标、维度、同义词，提升查询准确率
- **VQR（Verified Query Repository）**：验证过的 SQL 优先匹配，避免重复生成
- **实时流式输出**：SSE 推送分析过程和结果
- **自动可视化**：查询结果自动生成 ECharts 图表
- **告警与 KPI**：自然语言配置告警规则和看板指标

## 架构

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
│  │  ├── Supervisor Agent (路由 + 编排)            │  │
│  │  ├── DataAnalyst Agent (SQL 生成 + 执行)       │  │
│  │  ├── Orchestrator (direct/parallel/pipeline)  │  │
│  │  ├── Semantic Layer (指标/维度/模板)            │  │
│  │  └── VQR (验证查询缓存)                        │  │
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│  AWS Services                                        │
│  ├── DynamoDB × 5 (config/chat/cost/events/feedback)│
│  ├── S3 (数据湖 + 报告存储)                          │
│  ├── Athena (数据湖查询)                             │
│  ├── Bedrock / OpenAI Compatible (LLM)              │
│  └── RDS PostgreSQL/MySQL (可选)                     │
└─────────────────────────────────────────────────────┘
```

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React (CDN) + ECharts + Leaflet |
| 后端 | Python 3.12 + FastAPI + Uvicorn |
| AI Agent | [Strands Agents SDK](https://github.com/strands-agents/sdk-python) |
| LLM | Bedrock (Claude) / OpenAI Compatible / SiliconFlow / LiteLLM |
| 基础设施 | AWS CDK (TypeScript) |
| 容器 | ECS Fargate (ARM64) |
| 存储 | DynamoDB + S3 + Athena |
| 认证 | Local Auth / Cognito / Authing |

## 前置条件

- **AWS CLI** — 已配置 credentials 和 region
- **Node.js 18+** — CDK 和前端构建
- **Docker** — 容器镜像构建
- **AWS CDK CLI** — 基础设施部署

```bash
# 验证环境
aws sts get-caller-identity
node --version        # >= 18
docker info
cdk --version         # 如未安装: npm install -g aws-cdk
```

## 快速部署

### 1. CDK Bootstrap（首次部署）

```bash
cd cdk
npm install
npx cdk bootstrap
```

> 如需指定区域：`npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>`

### 2. 交互式部署

```bash
bash deploy.sh
```

脚本会引导你配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| Region | 部署区域 | AWS CLI 默认 region |
| Admin 密码 | 管理员登录密码 | `Admin@2026` |
| 安全组白名单 | ALB 允许访问的 IP | `10.0.0.0/8` |
| VPC | 使用已有 VPC 或新建 | 新建 |
| 模型 | LLM 提供商配置 | Global: Bedrock / 中国区: 手动配置 |
| RDS | 是否启用 PostgreSQL | 否 |

### 3. 手动部署（跳过交互）

```bash
cd cdk
npm install
npx cdk deploy \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32" \
  --require-approval broadening
```

### 4. 高级配置（CDK Context）

```bash
# 中国区 + OpenAI 兼容模型
npx cdk deploy \
  -c model_provider=openai \
  -c openai_base_url=https://api.siliconflow.cn/v1 \
  -c openai_api_key=sk-xxx \
  -c openai_model=Pro/Qwen/Qwen2.5-72B-Instruct \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"

# 使用已有 VPC + 启用 RDS
npx cdk deploy \
  -c vpc_id=vpc-xxx \
  -c enable_rds=true \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"

# 指定 Bedrock 区域（应用在 ap-southeast-1，Bedrock 在 us-east-1）
npx cdk deploy \
  -c bedrock_region=us-east-1 \
  --parameters AdminPassword=<YOUR_PASSWORD> \
  --parameters AllowedCIDRs="1.2.3.4/32"
```

## 部署产出

部署完成后 CDK 输出：

```
Outputs:
  AgenticDataStack.ApplicationURL = http://Agenti-ALB-xxxxx.elb.amazonaws.com
  AgenticDataStack.AdminUser = admin
  AgenticDataStack.ConfigTableName = agentic-data-config
  AgenticDataStack.DataBucketName = agenticdatastack-databucket-xxxxx
```

## 部署后配置

### 1. 登录

打开 `ApplicationURL`，使用 `admin` / 你设置的密码登录。

### 2. 配置模型（如部署时跳过）

进入 **管理后台 → 模型管理**，添加 LLM：

| 区域 | 推荐模型 |
|------|----------|
| Global | Bedrock Claude Sonnet 4.6（默认已配置） |
| 中国区 | SiliconFlow / ZhipuAI (GLM) / DeepSeek |

### 3. 连接数据源

进入 **管理后台 → 数据源管理**，支持：

- **Athena** — S3 数据湖查询（需提供 database、tables、output location）
- **PostgreSQL / MySQL** — RDS 或自建数据库
- **Snowflake** — 云数据仓库
- **S3** — CSV/JSON 文件直接分析

### 4. 配置语义层（可选）

进入 **管理后台 → 语义层**，定义：
- **指标**：如"日产量"= `SUM(output_qty)`
- **维度**：如"工厂"= `factory_name`
- **同义词**：如"产量" → "日产量"
- **查询模板**：预定义常用查询的 SQL

## 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export AGENTIC_AUTO_REGION=us-east-1
export AGENTIC_AUTO_CONFIG_TABLE=agentic-data-config
export AGENTIC_AUTO_CHAT_TABLE=agentic-data-chat-history
export AGENTIC_AUTO_COST_TABLE=agentic-data-cost
export AGENTIC_AUTO_EVENTS_TABLE=agentic-data-events
export AGENTIC_AUTO_FEEDBACK_TABLE=agentic-data-feedback
export AGENTIC_AUTO_DATA_BUCKET=your-data-bucket

# 启动
python -m uvicorn api.main:app --host 0.0.0.0 --port 8501 --reload
```

访问 `http://localhost:8501`

## 项目结构

```
├── api/
│   └── main.py              # FastAPI 入口 (REST + SSE)
├── agentic_core/
│   ├── agents.py             # Supervisor + Sub-Agent 定义
│   ├── orchestrator.py       # 编排引擎 (direct/parallel/pipeline/supervisor)
│   ├── tools.py              # Agent 工具 (NL2SQL, 语义查询, 报告等)
│   ├── semantic_layer.py     # 语义层 (指标/维度/模板)
│   ├── dynamic_context.py    # 动态 Prompt 构建
│   ├── vqr.py                # Verified Query Repository
│   ├── db_engine.py          # 多数据库引擎抽象
│   ├── agent_registry.py     # Agent 定义注册
│   ├── alert_rules.py        # 告警/KPI 规则引擎
│   ├── sql_cache.py          # SQL 缓存
│   ├── user_memory.py        # 用户记忆
│   ├── local_auth.py         # 本地认证
│   ├── cognito_auth.py       # Cognito 认证
│   └── authing_auth.py       # Authing 认证
├── web/
│   └── index.html            # React SPA (单文件)
├── cdk/
│   ├── lib/
│   │   └── agentic-data-stack.ts  # CDK 基础设施定义
│   ├── bin/cdk.ts            # CDK 入口
│   └── deploy.sh             # 交互式部署脚本
├── config.py                 # 集中配置 (环境变量)
├── auth.py                   # 认证中间件
├── Dockerfile                # 生产镜像
└── requirements.txt          # Python 依赖
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `AGENTIC_AUTO_REGION` | AWS 区域 | `us-east-1` |
| `BEDROCK_REGION` | Bedrock 区域（可与应用区域不同） | 同 REGION |
| `AGENTIC_AUTO_MODEL_PROVIDER` | 模型提供商 (`bedrock` / `openai`) | `bedrock` |
| `AGENTIC_AUTO_SUP_MODEL` | Supervisor 模型 ID | Claude Sonnet 4.6 |
| `AGENTIC_AUTO_SUB_MODEL` | Sub-Agent 模型 ID | Claude Haiku 4.5 |
| `SQL_ENGINE` | SQL 引擎 (`sqlite` / `postgresql` / `snowflake`) | `sqlite` |
| `AGENTIC_AUTO_AUTH_ENABLED` | 是否启用认证 | `false` |
| `AGENTIC_AUTO_PORT` | 应用端口 | `8501` |

完整变量列表见 [config.py](config.py)。

## 安全说明

- ALB 安全组禁止 `0.0.0.0/0`，必须指定白名单 IP
- 支持 Local Auth / Cognito / Authing 三种认证方式
- DynamoDB 表设置 `RETAIN` 删除保护
- S3 Bucket 默认 Block All Public Access
- 支持 Bedrock Guardrails 内容安全过滤

## 清理资源

```bash
cd cdk
npx cdk destroy
```

> 注意：DynamoDB 表和 S3 Bucket 设置了 `RETAIN`，不会随 Stack 删除。需手动清理。

## License

Private — All rights reserved.
