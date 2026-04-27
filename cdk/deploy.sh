#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Agentic Data — Interactive Deployment Script
# ═══════════════════════════════════════════════════════════
set -e

echo "╔══════════════════════════════════════════╗"
echo "║   Agentic ChatBI — CDK Deploy     ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Detect region ──
REGION=${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo "")}
if [ -z "$REGION" ]; then
    read -p "AWS Region (e.g. us-west-2, cn-northwest-1): " REGION
fi
# Ensure CDK picks up the correct region
export CDK_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_ACCOUNT=${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}
export AWS_DEFAULT_REGION="$REGION"
echo "📍 Region: $REGION"

IS_CHINA=false
if [[ "$REGION" == cn-* ]]; then
    IS_CHINA=true
    echo "🇨🇳 中国区部署模式"
else
    echo "🌍 Global region"
fi
echo ""

# ── Admin password ──
read -sp "🔑 Admin 密码 (必填，至少8位): " ADMIN_PW
echo ""
while [[ -z "$ADMIN_PW" || ${#ADMIN_PW} -lt 8 ]]; do
  read -sp "❌ 密码不能为空且至少8位，请重新输入: " ADMIN_PW
  echo ""
done

# ── Allowed CIDRs ──
echo ""
echo "🛡️  安全组白名单 IP (逗号分隔, 例: 114.255.117.3/32,38.98.189.250/32)"
echo "   ⚠️  绝不允许 0.0.0.0/0！"
read -p "   CIDRs: " ALLOWED_CIDRS
ALLOWED_CIDRS=${ALLOWED_CIDRS:-10.0.0.0/8}

# Validate no 0.0.0.0/0
if [[ "$ALLOWED_CIDRS" == *"0.0.0.0/0"* ]]; then
    echo "❌ 拒绝 0.0.0.0/0！请指定具体 IP 段。"
    exit 1
fi

# ── Optional: existing VPC ──
echo ""
read -p "🔗 已有 VPC ID? (回车跳过, 自动创建新 VPC): " VPC_ID

# ── Docker ──
if ! command -v docker &>/dev/null; then
    echo "❌ Docker 未安装！CDK 需要 Docker 构建容器镜像。"
    echo "   安装: https://docs.docker.com/engine/install/"
    exit 1
fi
echo "✅ Docker 已就绪"

# ── Optional: Model config ──
CDK_CONTEXT=""
echo ""
echo "🤖 模型配置 (可跳过, 部署后在管理后台配置)"
if [ "$IS_CHINA" = true ]; then
    read -p "   配置模型? [y/N]: " CONFIGURE_MODEL
    if [[ "$CONFIGURE_MODEL" =~ ^[yY] ]]; then
        echo "   支持: SiliconFlow / ZhipuAI / 任何 OpenAI 兼容 API"
        read -p "   API Base URL (如 https://api.siliconflow.cn/v1): " OPENAI_URL
        read -sp "   API Key: " OPENAI_KEY
        echo ""
        read -p "   Model ID (如 Pro/zai-org/GLM-5): " OPENAI_MODEL
        if [ -n "$OPENAI_URL" ]; then
            CDK_CONTEXT="$CDK_CONTEXT -c openai_base_url=$OPENAI_URL"
        fi
        if [ -n "$OPENAI_KEY" ]; then
            CDK_CONTEXT="$CDK_CONTEXT -c openai_api_key=$OPENAI_KEY"
        fi
        if [ -n "$OPENAI_MODEL" ]; then
            CDK_CONTEXT="$CDK_CONTEXT -c openai_model=$OPENAI_MODEL"
        fi
    else
        echo "   ⏭️  跳过, 部署后在 管理后台 → 模型管理 中配置"
    fi
else
    echo "   Global 区域默认使用 Bedrock (Claude Sonnet 4.6 + Haiku 4.5)"
    echo "   部署后可在管理后台切换模型"
fi

# ═══════ 数据源配置 (可选) ═══════
RDS_CTX=""
ATHENA_CTX=""
RDS_ENGINE=""
RDS_HOST=""
RDS_PORT=""
RDS_DATABASE=""
ATHENA_DB=""

echo ""
echo "═══════════════════════════════════════════"
echo "🗄️  数据源配置 (可选, 部署后也可在管理后台配置)"
echo ""
echo "   1) RDS MySQL"
echo "   2) RDS PostgreSQL"
echo "   3) 跳过 (部署后配置)"
echo ""
read -p "   选择 [1/2/3, 默认3]: " DS_CHOICE
DS_CHOICE=${DS_CHOICE:-3}

if [[ "$DS_CHOICE" == "1" || "$DS_CHOICE" == "2" ]]; then
    if [ "$DS_CHOICE" == "1" ]; then
        RDS_ENGINE="mysql"
        DEFAULT_PORT="3306"
        echo ""
        echo "   🐬 配置 MySQL 数据源"
    else
        RDS_ENGINE="postgresql"
        DEFAULT_PORT="5432"
        echo ""
        echo "   🐘 配置 PostgreSQL 数据源"
    fi

    read -p "   RDS Endpoint (host): " RDS_HOST
    read -p "   Port [${DEFAULT_PORT}]: " RDS_PORT
    RDS_PORT=${RDS_PORT:-$DEFAULT_PORT}
    read -p "   Database name: " RDS_DATABASE
    read -p "   Username: " RDS_USER
    read -sp "   Password: " RDS_PASSWORD
    echo ""

    if [ -z "$RDS_HOST" ] || [ -z "$RDS_DATABASE" ] || [ -z "$RDS_USER" ]; then
        echo "   ⚠️  RDS 配置不完整, 跳过"
        RDS_ENGINE=""
    else
        RDS_CTX="-c rds_engine=$RDS_ENGINE -c rds_host=$RDS_HOST -c rds_port=$RDS_PORT -c rds_database=$RDS_DATABASE -c rds_user=$RDS_USER -c rds_password=$RDS_PASSWORD"
        echo "   ✅ ${RDS_ENGINE^^} 数据源已配置"
    fi
fi

echo ""
read -p "   📊 配置 Athena 数据源? [y/N]: " CONFIGURE_ATHENA
if [[ "$CONFIGURE_ATHENA" =~ ^[yY] ]]; then
    read -p "   Athena Database name: " ATHENA_DB
    read -p "   Query output S3 path (如 s3://my-bucket/athena-results/): " ATHENA_OUTPUT
    if [ -n "$ATHENA_DB" ]; then
        ATHENA_CTX="-c athena_database=$ATHENA_DB"
        if [ -n "$ATHENA_OUTPUT" ]; then
            ATHENA_CTX="$ATHENA_CTX -c athena_output=$ATHENA_OUTPUT"
        fi
        echo "   ✅ Athena 数据源已配置"
    fi
fi

# ── Build command ──
VPC_CTX=""
if [ -n "$VPC_ID" ]; then
    VPC_CTX="-c vpc_id=$VPC_ID"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "📋 部署配置确认:"
echo "   Region:      $REGION"
echo "   Admin PW:    ****"
echo "   CIDRs:       $ALLOWED_CIDRS"
echo "   VPC:         ${VPC_ID:-新建}"
if [ -n "$RDS_ENGINE" ]; then
    echo "   数据源:      ${RDS_ENGINE^^} → ${RDS_HOST}:${RDS_PORT}/${RDS_DATABASE}"
else
    echo "   RDS 数据源:  未配置 (部署后在管理后台添加)"
fi
if [ -n "$ATHENA_DB" ]; then
    echo "   Athena:      ${ATHENA_DB}"
else
    echo "   Athena:      未配置"
fi
if [ "$IS_CHINA" = true ]; then
    echo "   模型:        ${CONFIGURE_MODEL:-OpenAI 兼容 (部署后配置)}"
else
    echo "   模型:        Bedrock (Claude Sonnet 4.6 + Haiku 4.5)"
fi
echo "═══════════════════════════════════════════"
echo ""

read -p "🚀 确认部署? [Y/n]: " CONFIRM
if [[ "$CONFIRM" =~ ^[nN] ]]; then
    echo "已取消"
    exit 0
fi

# ── Install deps ──
echo ""
echo "📦 安装依赖..."
npm install --silent

# ── Deploy ──
echo "🚀 开始部署..."
npx cdk deploy \
    --parameters AdminPassword="$ADMIN_PW" \
    --parameters AllowedCIDRs="$ALLOWED_CIDRS" \
    $VPC_CTX $RDS_CTX $ATHENA_CTX $CDK_CONTEXT \
    --require-approval broadening

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║          ✅ 部署完成!                    ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "📌 下一步:"
echo "   1. 打开上方 ApplicationURL"
echo "   2. 用 admin / 你设置的密码 登录"
if [ -n "$RDS_ENGINE" ]; then
    echo "   3. ${RDS_ENGINE^^} 数据源已自动配置, 可直接在聊天中查询"
    echo "      系统会自动发现表结构并加载语义层"
else
    echo "   3. 进入 管理后台 → 数据源管理 → 添加 MySQL/PostgreSQL/Athena"
fi
if [ "$IS_CHINA" = true ] && [[ ! "$CONFIGURE_MODEL" =~ ^[yY] ]]; then
    echo "   4. 进入 管理后台 → 模型管理 → 添加 OpenAI 兼容模型"
    echo "      推荐: SiliconFlow (GLM-5) / ZhipuAI / DeepSeek"
fi
echo ""
echo "💡 提示: 连接数据源后，系统会自动发现表结构，在 Agent 配置中推荐工具"
echo ""
