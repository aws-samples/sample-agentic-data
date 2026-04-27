#!/bin/bash
set -e

# ═══════════════════════════════════════════════════
# Agentic Data — China Region Deployment Script
# Usage: bash deploy/china/deploy-cn.sh [options]
# ═══════════════════════════════════════════════════

# Defaults
CN_REGION="${CN_REGION:-cn-northwest-1}"
CN_ACCOUNT="${CN_ACCOUNT:-107327642275}"
CN_PROFILE="${CN_PROFILE:-cn}"
SILICONFLOW_KEY="${SILICONFLOW_KEY:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:?\"Error: ADMIN_PASSWORD is required. Export it before running this script.\"}"
JWT_SECRET="${JWT_SECRET:-$(openssl rand -hex 16)}"
INSTANCE_ID="${INSTANCE_ID:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.large}"

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --region) CN_REGION="$2"; shift 2;;
    --account) CN_ACCOUNT="$2"; shift 2;;
    --profile) CN_PROFILE="$2"; shift 2;;
    --siliconflow-key) SILICONFLOW_KEY="$2"; shift 2;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2;;
    --instance-id) INSTANCE_ID="$2"; shift 2;;
    --instance-type) INSTANCE_TYPE="$2"; shift 2;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

if [ -z "$SILICONFLOW_KEY" ]; then
  echo "ERROR: --siliconflow-key required"
  exit 1
fi

ECR_REPO="${CN_ACCOUNT}.dkr.ecr.${CN_REGION}.amazonaws.com.cn/agentic-data"
S3_BUCKET="agentic-data-${CN_ACCOUNT}-${CN_REGION}"

echo "═══════════════════════════════════════════"
echo " Agentic Data — China Region Deployment"
echo " Region:    ${CN_REGION}"
echo " Account:   ${CN_ACCOUNT}"
echo " Profile:   ${CN_PROFILE}"
echo " ECR:       ${ECR_REPO}"
echo " S3:        ${S3_BUCKET}"
echo "═══════════════════════════════════════════"

# Step 1: Create ECR repo if not exists
echo ""
echo "[1/6] Creating ECR repository..."
aws ecr describe-repositories --repository-names agentic-data \
  --profile "$CN_PROFILE" --region "$CN_REGION" 2>/dev/null || \
aws ecr create-repository --repository-name agentic-data \
  --profile "$CN_PROFILE" --region "$CN_REGION" \
  --image-tag-mutability MUTABLE

# Step 2: Create S3 buckets
echo ""
echo "[2/6] Creating S3 bucket..."
aws s3 mb "s3://${S3_BUCKET}" --profile "$CN_PROFILE" --region "$CN_REGION" 2>/dev/null || true

# Step 3: Create DynamoDB tables
echo ""
echo "[3/6] Creating DynamoDB tables..."
for TABLE_SUFFIX in events chat-history cost config feedback; do
  TABLE_NAME="agentic-data-${TABLE_SUFFIX}"
  
  # Choose key schema based on table
  if [ "$TABLE_SUFFIX" = "config" ]; then
    KEY_SCHEMA='[{"AttributeName":"config_key","KeyType":"HASH"}]'
    ATTR_DEFS='[{"AttributeName":"config_key","AttributeType":"S"}]'
  elif [ "$TABLE_SUFFIX" = "cost" ]; then
    KEY_SCHEMA='[{"AttributeName":"date","KeyType":"HASH"},{"AttributeName":"timestamp","KeyType":"RANGE"}]'
    ATTR_DEFS='[{"AttributeName":"date","AttributeType":"S"},{"AttributeName":"timestamp","AttributeType":"S"}]'
  elif [ "$TABLE_SUFFIX" = "chat-history" ]; then
    KEY_SCHEMA='[{"AttributeName":"session_id","KeyType":"HASH"},{"AttributeName":"timestamp","KeyType":"RANGE"}]'
    ATTR_DEFS='[{"AttributeName":"session_id","AttributeType":"S"},{"AttributeName":"timestamp","AttributeType":"S"}]'
  elif [ "$TABLE_SUFFIX" = "feedback" ]; then
    KEY_SCHEMA='[{"AttributeName":"feedback_id","KeyType":"HASH"}]'
    ATTR_DEFS='[{"AttributeName":"feedback_id","AttributeType":"S"}]'
  else
    KEY_SCHEMA='[{"AttributeName":"event_id","KeyType":"HASH"}]'
    ATTR_DEFS='[{"AttributeName":"event_id","AttributeType":"S"}]'
  fi

  aws dynamodb describe-table --table-name "$TABLE_NAME" \
    --profile "$CN_PROFILE" --region "$CN_REGION" 2>/dev/null && continue

  echo "  Creating ${TABLE_NAME}..."
  aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --key-schema "$KEY_SCHEMA" \
    --attribute-definitions "$ATTR_DEFS" \
    --billing-mode PAY_PER_REQUEST \
    --profile "$CN_PROFILE" --region "$CN_REGION"
done

# Step 4: Build & push Docker image
echo ""
echo "[4/6] Building Docker image..."
cd "$(dirname "$0")/../.."
docker build -f deploy/Dockerfile -t agentic-data:latest .

echo "Pushing to China ECR..."
aws ecr get-login-password --region "$CN_REGION" --profile "$CN_PROFILE" | \
  docker login --username AWS --password-stdin "${CN_ACCOUNT}.dkr.ecr.${CN_REGION}.amazonaws.com.cn"
docker tag agentic-data:latest "${ECR_REPO}:latest"
docker push "${ECR_REPO}:latest"

# Step 5: Upload sample data to S3
echo ""
echo "[5/6] Uploading sample data to S3..."
if [ -d "data/chatbi" ]; then
  aws s3 sync data/chatbi/ "s3://${S3_BUCKET}/chatbi/" \
    --profile "$CN_PROFILE" --region "$CN_REGION"
fi

# Step 6: Deploy to EC2 (if instance ID provided)
if [ -n "$INSTANCE_ID" ]; then
  echo ""
  echo "[6/6] Deploying to EC2 ${INSTANCE_ID}..."
  
  DOCKER_CMD="docker run -d --restart always -p 8501:8501 \
    -e AWS_DEFAULT_REGION=${CN_REGION} \
    -e AGENTIC_AUTO_REGION=${CN_REGION} \
    -e AGENTIC_AUTO_MODEL_PROVIDER=siliconflow \
    -e SILICONFLOW_API_KEY=${SILICONFLOW_KEY} \
    -e AGENTIC_AUTO_AUTH_ENABLED=true \
    -e AGENTIC_AUTO_AUTH_PROVIDER=local \
    -e AGENTIC_AUTO_JWT_SECRET=${JWT_SECRET} \
    -e AGENTIC_AUTO_ADMIN_PASSWORD=${ADMIN_PASSWORD} \
    -e AGENTIC_AUTO_EVENTS_TABLE=agentic-data-events \
    -e AGENTIC_AUTO_CHAT_TABLE=agentic-data-chat-history \
    -e AGENTIC_AUTO_COST_TABLE=agentic-data-cost \
    -e AGENTIC_AUTO_CONFIG_TABLE=agentic-data-config \
    -e AGENTIC_AUTO_FEEDBACK_TABLE=agentic-data-feedback \
    -e AGENTIC_AUTO_DATA_BUCKET=${S3_BUCKET} \
    -e AGENTIC_AUTO_REPORTS_BUCKET=${S3_BUCKET} \
    ${ECR_REPO}:latest"

  aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[
      \"aws ecr get-login-password --region ${CN_REGION} | docker login --username AWS --password-stdin ${CN_ACCOUNT}.dkr.ecr.${CN_REGION}.amazonaws.com.cn\",
      \"docker pull ${ECR_REPO}:latest\",
      \"docker stop \\\$(docker ps -q) 2>/dev/null; docker rm \\\$(docker ps -aq) 2>/dev/null\",
      \"${DOCKER_CMD}\"
    ]" \
    --profile "$CN_PROFILE" --region "$CN_REGION"
  
  echo ""
  echo "Deployment command sent. Check SSM for status."
else
  echo ""
  echo "[6/6] No instance ID provided. Skipping EC2 deployment."
  echo ""
  echo "To deploy manually, run on the target EC2:"
  echo ""
  echo "  docker run -d --restart always -p 8501:8501 \\"
  echo "    -e AGENTIC_AUTO_REGION=${CN_REGION} \\"
  echo "    -e AGENTIC_AUTO_MODEL_PROVIDER=siliconflow \\"
  echo "    -e SILICONFLOW_API_KEY=\${SILICONFLOW_KEY} \\"
  echo "    -e AGENTIC_AUTO_AUTH_ENABLED=true \\"
  echo "    -e AGENTIC_AUTO_AUTH_PROVIDER=local \\"
  echo "    -e AGENTIC_AUTO_JWT_SECRET=\${JWT_SECRET} \\"
  echo "    -e AGENTIC_AUTO_ADMIN_PASSWORD=\${ADMIN_PASSWORD} \\"
  echo "    -e AGENTIC_AUTO_EVENTS_TABLE=agentic-data-events \\"
  echo "    -e AGENTIC_AUTO_CHAT_TABLE=agentic-data-chat-history \\"
  echo "    -e AGENTIC_AUTO_COST_TABLE=agentic-data-cost \\"
  echo "    -e AGENTIC_AUTO_CONFIG_TABLE=agentic-data-config \\"
  echo "    -e AGENTIC_AUTO_FEEDBACK_TABLE=agentic-data-feedback \\"
  echo "    -e AGENTIC_AUTO_DATA_BUCKET=${S3_BUCKET} \\"
  echo "    -e AGENTIC_AUTO_REPORTS_BUCKET=${S3_BUCKET} \\"
  echo "    ${ECR_REPO}:latest"
fi

echo ""
echo "═══════════════════════════════════════════"
echo " Done! Resources created in ${CN_REGION}"
echo "═══════════════════════════════════════════"
