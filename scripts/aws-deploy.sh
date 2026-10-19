#!/usr/bin/env bash
# Deploy Aether agents to AWS. Notion stays the control plane.
#
# What this creates:
#   1. Secrets Manager secret  aether/agents/env
#   2. ECR repo + pushed image aether-agents:latest
#   3. App Runner service      aether-agents-chat  (port 3847)
#
# Prerequisites (Mac):
#   brew install awscli docker
#   aws configure   # region eu-north-1 recommended
#   .env filled next to this repo (NOTION_TOKEN + data source IDs)
#
# Usage:
#   ./scripts/aws-deploy.sh              # full path: secret + image + App Runner
#   ./scripts/aws-deploy.sh --image-only # rebuild/push image only
#   ./scripts/aws-deploy.sh --secret-only
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

AWS_REGION="${AWS_REGION:-eu-north-1}"
REPO="${REPO:-aether-agents}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SECRET_NAME="${SECRET_NAME:-aether/agents/env}"
SERVICE_NAME="${SERVICE_NAME:-aether-agents-chat}"
PORT="${PORT:-3847}"

MODE="full"
case "${1:-}" in
  --image-only)  MODE="image" ;;
  --secret-only) MODE="secret" ;;
  --help|-h)
    sed -n '2,20p' "$0"
    exit 0
    ;;
esac

need() { command -v "$1" >/dev/null || { echo "Missing: $1" >&2; exit 1; }; }
need aws
if [[ "$MODE" == "full" || "$MODE" == "image" ]]; then
  need docker
fi

echo "==> Caller"
aws sts get-caller-identity --region "$AWS_REGION"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO}:${IMAGE_TAG}"

# ---------- helpers ----------
json_from_env() {
  # Build Secrets Manager JSON from .env (only known keys).
  if [[ ! -f .env ]]; then
    echo "No .env in $ROOT — copy .env.example and fill NOTION_TOKEN" >&2
    exit 1
  fi
  python3 - <<'PY'
import json, re
from pathlib import Path
keys = [
    "NOTION_TOKEN",
    "NOTION_TASKS_DATA_SOURCE_ID",
    "NOTION_COMPANIES_DATA_SOURCE_ID",
    "NOTION_RESEARCH_DATA_SOURCE_ID",
    "NOTION_PROJECTS_DATA_SOURCE_ID",
    "NOTION_FUNDING_DATA_SOURCE_ID",
    "NOTION_COMMAND_CENTER_PAGE_ID",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "AGENT_BATCH_LIMIT",
    "AGENT_DRY_RUN",
]
vals = {}
for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    if k in keys and v:
        vals[k] = v
if not vals.get("NOTION_TOKEN"):
    raise SystemExit("NOTION_TOKEN missing in .env")
vals.setdefault("AGENT_BATCH_LIMIT", "10")
vals.setdefault("AGENT_DRY_RUN", "false")
print(json.dumps(vals))
PY
}

upsert_secret() {
  echo "==> Secrets Manager: $SECRET_NAME"
  local payload
  payload="$(json_from_env)"
  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
      --secret-id "$SECRET_NAME" \
      --region "$AWS_REGION" \
      --secret-string "$payload" >/dev/null
    echo "    updated"
  else
    aws secretsmanager create-secret \
      --name "$SECRET_NAME" \
      --region "$AWS_REGION" \
      --secret-string "$payload" >/dev/null
    echo "    created"
  fi
}

push_image() {
  echo "==> ECR: $ECR_URI"
  aws ecr describe-repositories --repository-names "$REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" >/dev/null

  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin \
      "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  docker build -t "${REPO}:${IMAGE_TAG}" .
  docker tag "${REPO}:${IMAGE_TAG}" "$ECR_URI"
  docker push "$ECR_URI"
  echo "    pushed $ECR_URI"
}

ensure_apprunner_roles() {
  # Access role: App Runner pulls from ECR.
  # Instance role: runtime reads Secrets Manager.
  local access_role="aether-apprunner-ecr-access"
  local instance_role="aether-apprunner-instance"
  local account="$AWS_ACCOUNT_ID"

  if ! aws iam get-role --role-name "$access_role" >/dev/null 2>&1; then
    echo "==> IAM role $access_role"
    aws iam create-role \
      --role-name "$access_role" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"build.apprunner.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' >/dev/null
    aws iam attach-role-policy \
      --role-name "$access_role" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess >/dev/null
    sleep 8
  fi

  if ! aws iam get-role --role-name "$instance_role" >/dev/null 2>&1; then
    echo "==> IAM role $instance_role"
    aws iam create-role \
      --role-name "$instance_role" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"tasks.apprunner.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' >/dev/null
    aws iam put-role-policy \
      --role-name "$instance_role" \
      --policy-name aether-secrets-read \
      --policy-document "{
        \"Version\":\"2012-10-17\",
        \"Statement\":[{
          \"Effect\":\"Allow\",
          \"Action\":[\"secretsmanager:GetSecretValue\"],
          \"Resource\":\"arn:aws:secretsmanager:${AWS_REGION}:${account}:secret:aether/agents/*\"
        }]
      }" >/dev/null
    sleep 8
  fi

  ACCESS_ROLE_ARN="$(aws iam get-role --role-name "$access_role" --query Role.Arn --output text)"
  INSTANCE_ROLE_ARN="$(aws iam get-role --role-name "$instance_role" --query Role.Arn --output text)"
}

runtime_env_from_secret() {
  # App Runner RuntimeEnvironmentSecrets map env var → secret ARN:json-key
  local secret_arn
  secret_arn="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --region "$AWS_REGION" \
    --query ARN --output text)"

  python3 - "$secret_arn" <<'PY'
import json, sys
arn = sys.argv[1]
keys = [
    "NOTION_TOKEN",
    "NOTION_TASKS_DATA_SOURCE_ID",
    "NOTION_COMPANIES_DATA_SOURCE_ID",
    "NOTION_RESEARCH_DATA_SOURCE_ID",
    "NOTION_PROJECTS_DATA_SOURCE_ID",
    "NOTION_FUNDING_DATA_SOURCE_ID",
    "NOTION_COMMAND_CENTER_PAGE_ID",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "AGENT_BATCH_LIMIT",
    "AGENT_DRY_RUN",
]
print(json.dumps({k: f"{arn}:{k}" for k in keys}))
PY
}

deploy_apprunner() {
  ensure_apprunner_roles
  local image_id="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO}:${IMAGE_TAG}"
  local secrets_json
  secrets_json="$(runtime_env_from_secret)"

  local existing
  existing="$(aws apprunner list-services --region "$AWS_REGION" \
    --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" \
    --output text 2>/dev/null || true)"

  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo "==> App Runner: updating $SERVICE_NAME"
    aws apprunner update-service \
      --region "$AWS_REGION" \
      --service-arn "$existing" \
      --source-configuration "{
        \"AuthenticationConfiguration\": {
          \"AccessRoleArn\": \"${ACCESS_ROLE_ARN}\"
        },
        \"AutoDeploymentsEnabled\": false,
        \"ImageRepository\": {
          \"ImageIdentifier\": \"${image_id}\",
          \"ImageRepositoryType\": \"ECR\",
          \"ImageConfiguration\": {
            \"Port\": \"${PORT}\",
            \"RuntimeEnvironmentSecrets\": ${secrets_json}
          }
        }
      }" \
      --instance-configuration "{
        \"Cpu\": \"0.25 vCPU\",
        \"Memory\": \"0.5 GB\",
        \"InstanceRoleArn\": \"${INSTANCE_ROLE_ARN}\"
      }" >/dev/null
  else
    echo "==> App Runner: creating $SERVICE_NAME"
    aws apprunner create-service \
      --region "$AWS_REGION" \
      --service-name "$SERVICE_NAME" \
      --source-configuration "{
        \"AuthenticationConfiguration\": {
          \"AccessRoleArn\": \"${ACCESS_ROLE_ARN}\"
        },
        \"AutoDeploymentsEnabled\": false,
        \"ImageRepository\": {
          \"ImageIdentifier\": \"${image_id}\",
          \"ImageRepositoryType\": \"ECR\",
          \"ImageConfiguration\": {
            \"Port\": \"${PORT}\",
            \"RuntimeEnvironmentSecrets\": ${secrets_json}
          }
        }
      }" \
      --instance-configuration "{
        \"Cpu\": \"0.25 vCPU\",
        \"Memory\": \"0.5 GB\",
        \"InstanceRoleArn\": \"${INSTANCE_ROLE_ARN}\"
      }" >/dev/null
  fi

  echo "    waiting for RUNNING…"
  local arn url status
  arn="$(aws apprunner list-services --region "$AWS_REGION" \
    --query "ServiceSummaryList[?ServiceName=='${SERVICE_NAME}'].ServiceArn | [0]" \
    --output text)"
  for _ in $(seq 1 60); do
    status="$(aws apprunner describe-service --region "$AWS_REGION" --service-arn "$arn" \
      --query Service.Status --output text)"
    if [[ "$status" == "RUNNING" ]]; then
      break
    fi
    if [[ "$status" == "CREATE_FAILED" || "$status" == "UPDATE_FAILED" || "$status" == "DELETED" ]]; then
      echo "App Runner status: $status" >&2
      exit 1
    fi
    sleep 15
  done
  url="$(aws apprunner describe-service --region "$AWS_REGION" --service-arn "$arn" \
    --query Service.ServiceUrl --output text)"
  echo
  echo "Chat URL:  https://${url}"
  echo "Health:    curl -s https://${url}/api/health"
  echo "Status:    curl -s https://${url}/api/status"
  echo
  echo "Next: queue worker — see docs/aws-deploy-steps.md §4 (EventBridge→ECS)"
  echo "      or keep GitHub Actions (.github/workflows/agents.yml) as backup."
}

# ---------- run ----------
case "$MODE" in
  secret) upsert_secret ;;
  image)  push_image ;;
  full)
    upsert_secret
    push_image
    deploy_apprunner
    ;;
esac
