#!/bin/bash
set -euo pipefail

# ============================================================
# Agentic ChatBI — One-Click Deployment
# Usage: ./deploy.sh [OPTIONS]
#
# Options:
#   --mode ec2|ecs       Compute mode (default: ec2)
#   --region REGION      AWS region (default: us-east-1)
#   --china              Deploy to China region (cn-north-1)
#   --stack NAME         Stack name (default: agentic-data)
#   --instance TYPE      EC2 instance type (default: t4g.medium)
#   --init-data          Upload sample data after deploy
#   --destroy            Delete the stack
#   --help               Show this help
# ============================================================

# Defaults
MODE="EC2"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK_NAME="agentic-data"
INSTANCE_TYPE="t4g.medium"
CHINA=""
INIT_DATA=""
DESTROY=""
SUP_MODEL="us.anthropic.claude-sonnet-4-6"
SUB_MODEL="us.anthropic.claude-haiku-4-5-20251001-v1:0"
KEY_PAIR=""

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --mode)     MODE="${2^^}"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --china)    CHINA="true"; REGION="${REGION:-cn-north-1}"; shift ;;
    --stack)    STACK_NAME="$2"; shift 2 ;;
    --instance) INSTANCE_TYPE="$2"; shift 2 ;;
    --key)      KEY_PAIR="$2"; shift 2 ;;
    --init-data) INIT_DATA="true"; shift ;;
    --destroy)  DESTROY="true"; shift ;;
    --help)
      head -14 "$0" | tail -12
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# China region adjustments
if [[ "$CHINA" == "true" ]] || [[ "$REGION" == cn-* ]]; then
  CHINA="true"
  SUP_MODEL="anthropic.claude-3-sonnet-20240229-v1:0"
  SUB_MODEL="anthropic.claude-3-haiku-20240307-v1:0"
  echo "🇨🇳 China region mode: $REGION"
  echo "   Models: Sonnet 3 / Haiku 3 (China Bedrock)"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

if [[ "$REGION" == cn-* ]]; then
  ECR_DOMAIN="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com.cn"
else
  ECR_DOMAIN="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
fi
ECR_REPO="${ECR_DOMAIN}/agentic-data"
IMAGE_TAG="latest"
IMAGE_URI="${ECR_REPO}:${IMAGE_TAG}"

echo "🚀 Agentic ChatBI — Deployment"
echo "   Mode:     $MODE"
echo "   Region:   $REGION"
echo "   Stack:    $STACK_NAME"
echo "   Account:  $ACCOUNT_ID"
echo "   Image:    $IMAGE_URI"
echo ""

# ── Destroy ──
if [[ "$DESTROY" == "true" ]]; then
  echo "🗑️  Destroying stack: $STACK_NAME"
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  echo "⏳ Waiting for deletion..."
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
  echo "✅ Stack deleted"
  exit 0
fi

# ── Step 1: Create ECR Repository ──
echo "📦 Step 1: ECR Repository"
aws ecr describe-repositories --repository-names agentic-data --region "$REGION" 2>/dev/null || \
  aws ecr create-repository --repository-name agentic-data --region "$REGION" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256
echo "   ✅ ECR repo ready"

# ── Step 2: Build Docker Image ──
echo "📦 Step 2: Building Docker image..."
cd "$PROJECT_DIR"

# Copy Dockerfile to project root for context
cp deploy/Dockerfile .
cp deploy/.dockerignore .

# Build for ARM64 (Graviton)
docker buildx build --platform linux/arm64 -t "$IMAGE_URI" -f Dockerfile . --load 2>/dev/null || \
  docker build -t "$IMAGE_URI" -f Dockerfile .

# Cleanup
rm -f Dockerfile .dockerignore
echo "   ✅ Image built"

# ── Step 3: Push to ECR ──
echo "📦 Step 3: Pushing to ECR..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_DOMAIN"
docker push "$IMAGE_URI"
echo "   ✅ Image pushed"

# ── Step 4: Deploy CloudFormation ──
echo "☁️  Step 4: Deploying CloudFormation..."
PARAMS="DeploymentMode=$MODE"
PARAMS="$PARAMS InstanceType=$INSTANCE_TYPE"
PARAMS="$PARAMS ContainerImageUri=$IMAGE_URI"
PARAMS="$PARAMS SupervisorModel=$SUP_MODEL"
PARAMS="$PARAMS SubAgentModel=$SUB_MODEL"
[[ -n "$KEY_PAIR" ]] && PARAMS="$PARAMS KeyPairName=$KEY_PAIR"

# shellcheck disable=SC2086  # $PARAMS is intentionally unquoted for word-split (CFN Key=Value list)
aws cloudformation deploy \
  --template-file "$SCRIPT_DIR/cfn-agentic-data.yaml" \
  --stack-name "$STACK_NAME" \
  --parameter-overrides $PARAMS \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset

echo "   ✅ Stack deployed"

# ── Step 5: Get Outputs ──
echo ""
echo "📋 Stack Outputs:"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' --output table

ALB_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ALBURL`].OutputValue' --output text)
echo ""
echo "🌐 Access: $ALB_URL"

# ── Step 6: Init Data (optional) ──
if [[ "$INIT_DATA" == "true" ]]; then
  echo ""
  echo "📊 Step 6: Initializing sample data..."
  BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`DataBucketName`].OutputValue' --output text)

  # Upload ChatBI data
  if [[ -d "$PROJECT_DIR/data/chatbi" ]]; then
    aws s3 sync "$PROJECT_DIR/data/chatbi/" "s3://${BUCKET}/chatbi/" --region "$REGION"
    echo "   ✅ ChatBI data uploaded"
  fi

  # Upload vehicle info
  if [[ -f "$PROJECT_DIR/data/vehicle_info.json" ]]; then
    aws s3 cp "$PROJECT_DIR/data/vehicle_info.json" "s3://${BUCKET}/data/vehicle_info.json" --region "$REGION"
    echo "   ✅ Vehicle info uploaded"
  fi

  # Upload sample events
  if [[ -f "$PROJECT_DIR/sample_data/output/events.json" ]]; then
    aws s3 cp "$PROJECT_DIR/sample_data/output/events.json" "s3://${BUCKET}/data/events.json" --region "$REGION"
  fi

  # Load DynamoDB events
  EVENTS_TABLE=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`EventsTableName`].OutputValue' --output text 2>/dev/null || echo "")
  if [[ -n "$EVENTS_TABLE" ]] && [[ -f "$PROJECT_DIR/scripts/load_events.py" ]]; then
    AGENTIC_AUTO_EVENTS_TABLE="$EVENTS_TABLE" AGENTIC_AUTO_REGION="$REGION" \
      python3 "$PROJECT_DIR/scripts/load_events.py" 2>/dev/null && echo "   ✅ DynamoDB events loaded" || echo "   ⚠️ DynamoDB load skipped"
  fi

  echo "   ✅ Sample data initialization complete"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  🎉 Agentic ChatBI deployed!"
echo "  🌐 URL: $ALB_URL"
echo "  📊 Mode: $MODE | Region: $REGION"
echo "═══════════════════════════════════════════════"
