"""
Centralized configuration — all values from environment variables with sensible defaults.
CloudFormation UserData injects these via /etc/environment or systemd EnvironmentFile.
"""
import os

# App
VERSION = "1.0.0"
ENVIRONMENT = os.environ.get("AGENTIC_DATA_ENV", "production")

# AWS Region
REGION = os.environ.get("AGENTIC_AUTO_REGION", "us-east-1")
# Bedrock may be in a different region than the app (e.g. app in ap-southeast-1, Bedrock in us-east-1)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", REGION)

# S3
DATA_BUCKET = os.environ.get("AGENTIC_AUTO_DATA_BUCKET", "")
REPORTS_BUCKET = os.environ.get("AGENTIC_AUTO_REPORTS_BUCKET", "")

# DynamoDB
EVENTS_TABLE = os.environ.get("AGENTIC_AUTO_EVENTS_TABLE", "agentic-auto-events")
CHAT_TABLE = os.environ.get("AGENTIC_AUTO_CHAT_TABLE", "agentic-auto-chat-history")
COST_TABLE = os.environ.get("AGENTIC_AUTO_COST_TABLE", "agentic-auto-cost")
CONFIG_TABLE = os.environ.get("AGENTIC_AUTO_CONFIG_TABLE", "agentic-auto-config")
FEEDBACK_TABLE = os.environ.get("AGENTIC_AUTO_FEEDBACK_TABLE", "agentic-auto-feedback")

# Athena
ATHENA_DATABASE = os.environ.get("AGENTIC_AUTO_ATHENA_DB", "agentic_auto")

# Bedrock
_provider = os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock")
DEFAULT_SUPERVISOR_MODEL = os.environ.get("AGENTIC_AUTO_SUP_MODEL",
    "deepseek-ai/DeepSeek-V3" if _provider == "siliconflow" else "us.anthropic.claude-sonnet-4-6")
DEFAULT_SUB_AGENT_MODEL = os.environ.get("AGENTIC_AUTO_SUB_MODEL",
    "Qwen/Qwen2.5-7B-Instruct" if _provider == "siliconflow" else "us.anthropic.claude-haiku-4-5-20251001-v1:0")
GUARDRAIL_ID = os.environ.get("AGENTIC_AUTO_GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("AGENTIC_AUTO_GUARDRAIL_VERSION", "")

# Model Provider: bedrock | siliconflow | openai-compatible
MODEL_PROVIDER = os.environ.get("AGENTIC_AUTO_MODEL_PROVIDER", "bedrock")
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

# Auth (Cognito)
COGNITO_USER_POOL_ID = os.environ.get("AGENTIC_AUTO_COGNITO_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("AGENTIC_AUTO_COGNITO_CLIENT_ID", "")
COGNITO_REGION = os.environ.get("AGENTIC_AUTO_COGNITO_REGION", REGION)
AUTH_ENABLED = os.environ.get("AGENTIC_AUTO_AUTH_ENABLED", "false").lower() == "true"

# Rate Limiting
RATE_LIMIT_PER_MINUTE = int(os.environ.get("AGENTIC_AUTO_RATE_LIMIT", "15"))

# App
APP_PORT = int(os.environ.get("AGENTIC_AUTO_PORT", "8501"))
APP_HOST = os.environ.get("AGENTIC_AUTO_HOST", "0.0.0.0")  # nosec B104 — required for container networking

# SQL Engine (sqlite | snowflake | postgresql)
SQL_ENGINE = os.environ.get("SQL_ENGINE", "sqlite")

# Snowflake
SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "")
SNOWFLAKE_SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")
SNOWFLAKE_ROLE = os.environ.get("SNOWFLAKE_ROLE", "")
SNOWFLAKE_PRIVATE_KEY_PATH = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")

# PostgreSQL (real RDS)
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_DATABASE = os.environ.get("POSTGRES_DATABASE", "agentic_auto")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
POSTGRES_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "public")

# Tableau
TABLEAU_SERVER_URL = os.environ.get("TABLEAU_SERVER_URL", "")
TABLEAU_SITE_ID = os.environ.get("TABLEAU_SITE_ID", "")
TABLEAU_TOKEN_NAME = os.environ.get("TABLEAU_TOKEN_NAME", "")
TABLEAU_TOKEN_SECRET = os.environ.get("TABLEAU_TOKEN_SECRET", "")
TABLEAU_USERNAME = os.environ.get("TABLEAU_USERNAME", "")
TABLEAU_PASSWORD = os.environ.get("TABLEAU_PASSWORD", "")

# Cognito Hosted UI
COGNITO_DOMAIN = os.environ.get("AGENTIC_AUTO_COGNITO_DOMAIN", "agentic-data-demo.auth.us-west-1.amazoncognito.com")
COGNITO_REDIRECT_URI = os.environ.get("AGENTIC_AUTO_COGNITO_REDIRECT_URI", "https://demo.nubility.cn/")
COGNITO_LOGOUT_URI = os.environ.get("AGENTIC_AUTO_COGNITO_LOGOUT_URI", "https://demo.nubility.cn/")
