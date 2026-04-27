#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Agentic Data — 推送镜像到客户 ECR
# SA 运行此脚本，将预构建镜像推到客户的 ECR repo
# ═══════════════════════════════════════════════════════════
set -e

SRC_IMAGE="039192856426.dkr.ecr.us-east-1.amazonaws.com/agentic-data:latest"

echo "╔══════════════════════════════════════════════╗"
echo "║  Agentic Data — Push to Customer ECR         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 客户信息 ──
read -p "客户 AWS Account ID: " CUST_ACCOUNT
read -p "客户 Region (如 us-east-1): " CUST_REGION
read -p "客户 ECR Repo 名称 (默认 agentic-data): " CUST_REPO
CUST_REPO=${CUST_REPO:-agentic-data}

CUST_URI="${CUST_ACCOUNT}.dkr.ecr.${CUST_REGION}.amazonaws.com/${CUST_REPO}"
TAG="v1.0.5-$(date +%Y%m%d)"

echo ""
echo "📋 推送计划:"
echo "   源:   $SRC_IMAGE"
echo "   目标: $CUST_URI:$TAG"
echo "   目标: $CUST_URI:latest"
echo ""

# ── Step 1: 确认客户 ECR repo 存在 ──
echo "🔍 检查客户 ECR repo..."
if ! aws ecr describe-repositories \
    --repository-names "$CUST_REPO" \
    --registry-id "$CUST_ACCOUNT" \
    --region "$CUST_REGION" &>/dev/null; then
    echo ""
    echo "⚠️  客户 ECR repo 不存在。请让客户先创建:"
    echo ""
    echo "   aws ecr create-repository \\"
    echo "     --repository-name $CUST_REPO \\"
    echo "     --region $CUST_REGION \\"
    echo "     --image-scanning-configuration scanOnPush=true"
    echo ""
    echo "   然后给 repo 加跨账户 push 权限:"
    echo ""
    cat << POLICY
   aws ecr set-repository-policy \\
     --repository-name $CUST_REPO \\
     --region $CUST_REGION \\
     --policy-text '{
       "Version": "2012-10-17",
       "Statement": [{
         "Sid": "AllowSAPush",
         "Effect": "Allow",
         "Principal": {"AWS": "arn:aws:iam::039192856426:root"},
         "Action": [
           "ecr:GetDownloadUrlForLayer",
           "ecr:BatchGetImage",
           "ecr:BatchCheckLayerAvailability",
           "ecr:PutImage",
           "ecr:InitiateLayerUpload",
           "ecr:UploadLayerPart",
           "ecr:CompleteLayerUpload"
         ]
       }]
     }'
POLICY
    echo ""
    read -p "客户已创建并授权? [Y/n]: " READY
    if [[ "$READY" =~ ^[nN] ]]; then
        echo "等客户准备好再跑"
        exit 0
    fi
fi

# ── Step 2: Login to both ECRs ──
echo ""
echo "🔑 登录源 ECR..."
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin 039192856426.dkr.ecr.us-east-1.amazonaws.com 2>/dev/null

echo "🔑 登录客户 ECR..."
aws ecr get-login-password --region "$CUST_REGION" | \
    docker login --username AWS --password-stdin "${CUST_ACCOUNT}.dkr.ecr.${CUST_REGION}.amazonaws.com" 2>/dev/null

# ── Step 3: Pull, tag, push ──
echo ""
echo "📥 拉取源镜像..."
docker pull "$SRC_IMAGE"

echo ""
echo "🏷️  打标签..."
docker tag "$SRC_IMAGE" "$CUST_URI:$TAG"
docker tag "$SRC_IMAGE" "$CUST_URI:latest"

echo ""
echo "📤 推送到客户 ECR..."
docker push "$CUST_URI:$TAG"
docker push "$CUST_URI:latest"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          ✅ 推送完成!                        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "📌 客户部署时使用:"
echo "   cd cdk && npm install"
echo "   npx cdk deploy -c ecr_image=$CUST_URI:$TAG"
echo ""
echo "   或修改 deploy.sh 里的 ECR_IMAGE 变量"
echo ""
