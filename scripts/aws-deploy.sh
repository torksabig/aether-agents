#!/usr/bin/env bash
# Deploy Aether agents to AWS. Notion stays the control plane.
#
# What this creates (eu-north-1):
#   1. Secrets Manager secret     aether/agents/env
#   2. ECR repo + pushed image    aether-agents:latest
#   3. ECS Express Mode service   aether-agents-chat  (public HTTPS URL, port 3847)
#
# Note: App Runner is NOT in eu-north-1 and is closed to new customers.
#       This script uses Amazon ECS Express Mode instead.
#
# Prerequisites (Mac):
#   eval "$(/opt/homebrew/bin/brew shellenv)"
#   brew install awscli && brew install --cask docker
#   aws configure   # Access Key ID starts with AKIA… (not account number)
#   .env filled (NOTION_TOKEN + data source IDs)
#
# Usage:
#   bash scripts/aws-deploy.sh
#   bash scripts/aws-deploy.sh --image-only
#   bash scripts/aws-deploy.sh --secret-only
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
EXEC_ROLE="${EXEC_ROLE:-aether-ecs-execution}"
INFRA_ROLE="${INFRA_ROLE:-aether-ecs-express-infra}"

MODE="full"
case "${1:-}" in
  --image-only)  MODE="image" ;;
  --secret-only) MODE="secret" ;;
  --help|-h)
    sed -n '2,22p' "$0"
    exit 0
    ;;
esac

need() { command -v "$1" >/dev/null || { echo "Missing: $1" >&2; exit 1; }; }
need aws
need python3
if [[ "$MODE" == "full" || "$MODE" == "image" ]]; then
  need docker
fi

echo "==> Caller"
aws sts get-caller-identity --region "$AWS_REGION"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO}:${IMAGE_TAG}"

# ---------- helpers ----------
json_from_env() {
  if [[ ! -f .env ]]; then
    echo "No .env in $ROOT — copy .env.example and fill NOTION_TOKEN" >&2
    exit 1
  fi
  python3 - <<'PY'
import json
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

ensure_express_roles() {
  local secret_arn
  secret_arn="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --region "$AWS_REGION" --query ARN --output text)"

  if ! aws iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
    echo "==> IAM $EXEC_ROLE"
    aws iam create-role --role-name "$EXEC_ROLE" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"ecs-tasks.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' >/dev/null
    aws iam attach-role-policy --role-name "$EXEC_ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
  fi
  # Always refresh secret-read policy (ARN can change on recreate)
  aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name aether-secret-pull \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Action\":[\"secretsmanager:GetSecretValue\"],
        \"Resource\":\"${secret_arn}\"
      }]
    }" >/dev/null

  if ! aws iam get-role --role-name "$INFRA_ROLE" >/dev/null 2>&1; then
    echo "==> IAM $INFRA_ROLE"
    aws iam create-role --role-name "$INFRA_ROLE" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{
          "Effect":"Allow",
          "Principal":{"Service":"ecs.amazonaws.com"},
          "Action":"sts:AssumeRole"
        }]
      }' >/dev/null
    aws iam attach-role-policy --role-name "$INFRA_ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices >/dev/null
    echo "    waiting for IAM propagation…"
    sleep 15
  fi

  EXEC_ARN="$(aws iam get-role --role-name "$EXEC_ROLE" --query Role.Arn --output text)"
  INFRA_ARN="$(aws iam get-role --role-name "$INFRA_ROLE" --query Role.Arn --output text)"
  SECRET_ARN="$secret_arn"
}

secrets_list_json() {
  python3 - "$SECRET_ARN" <<'PY'
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
# JSON key form for Secrets Manager
print(json.dumps([{"name": k, "valueFrom": f"{arn}:{k}::"} for k in keys]))
PY
}

deploy_express_chat() {
  ensure_express_roles
  local secrets_json
  secrets_json="$(secrets_list_json)"

  # primary-container as JSON (CLI shorthand struggles with nested secrets)
  local container_json
  container_json="$(python3 - "$ECR_URI" "$PORT" "$secrets_json" <<'PY'
import json, sys
image, port, secrets = sys.argv[1], int(sys.argv[2]), json.loads(sys.argv[3])
print(json.dumps({
    "image": image,
    "containerPort": port,
    "secrets": secrets,
}))
PY
)"

  local existing_arn=""
  if aws ecs list-express-gateway-services --region "$AWS_REGION" >/dev/null 2>&1; then
    existing_arn="$(aws ecs list-express-gateway-services --region "$AWS_REGION" \
      --query "serviceArns[?contains(@, '${SERVICE_NAME}')] | [0]" \
      --output text 2>/dev/null || true)"
    if [[ "$existing_arn" == "None" ]]; then
      existing_arn=""
    fi
  fi

  if [[ -n "$existing_arn" ]]; then
    echo "==> ECS Express: updating $SERVICE_NAME"
    aws ecs update-express-gateway-service \
      --region "$AWS_REGION" \
      --service-arn "$existing_arn" \
      --primary-container "$container_json" \
      --execution-role-arn "$EXEC_ARN" >/dev/null \
      || aws ecs update-express-gateway-service \
           --region "$AWS_REGION" \
           --service-arn "$existing_arn" \
           --primary-container "image=$ECR_URI,containerPort=$PORT" \
           --execution-role-arn "$EXEC_ARN" >/dev/null
  else
    echo "==> ECS Express: creating $SERVICE_NAME (ALB + HTTPS URL)…"
    # Try JSON container first; fall back to shorthand if CLI rejects structure
    if ! aws ecs create-express-gateway-service \
      --region "$AWS_REGION" \
      --service-name "$SERVICE_NAME" \
      --primary-container "$container_json" \
      --execution-role-arn "$EXEC_ARN" \
      --infrastructure-role-arn "$INFRA_ARN" \
      --cpu "1 vCPU" \
      --memory "2 GB" >/tmp/aether-express-create.json 2>/tmp/aether-express-create.err; then
      echo "    JSON container form failed; retrying shorthand + env from secret inject…"
      # Shorthand without nested secrets — inject via separate register if needed
      aws ecs create-express-gateway-service \
        --region "$AWS_REGION" \
        --service-name "$SERVICE_NAME" \
        --primary-container "image=${ECR_URI},containerPort=${PORT}" \
        --execution-role-arn "$EXEC_ARN" \
        --infrastructure-role-arn "$INFRA_ARN" \
        --cpu "1 vCPU" \
        --memory "2 GB" >/tmp/aether-express-create.json
      echo "    WARN: created without Secrets Manager injection."
      echo "    Set Notion env on the service in Console if /api/status shows notion_ready:false"
      cat /tmp/aether-express-create.err >&2 || true
    fi
  fi

  echo "    waiting for URL…"
  local url="" status=""
  for _ in $(seq 1 60); do
    # Prefer describe by name/arn
    if [[ -z "$existing_arn" ]]; then
      existing_arn="$(aws ecs list-express-gateway-services --region "$AWS_REGION" \
        --query "serviceArns[?contains(@, '${SERVICE_NAME}')] | [0]" \
        --output text 2>/dev/null || true)"
      [[ "$existing_arn" == "None" ]] && existing_arn=""
    fi
    if [[ -n "$existing_arn" ]]; then
      status="$(aws ecs describe-express-gateway-service --region "$AWS_REGION" \
        --service-arn "$existing_arn" \
        --query 'service.status.statusCode' --output text 2>/dev/null || echo PENDING)"
      url="$(aws ecs describe-express-gateway-service --region "$AWS_REGION" \
        --service-arn "$existing_arn" \
        --query 'service.endpointURL' --output text 2>/dev/null || true)"
      if [[ -z "$url" || "$url" == "None" ]]; then
        url="$(aws ecs describe-express-gateway-service --region "$AWS_REGION" \
          --service-arn "$existing_arn" \
          --query 'service.serviceEndpoint' --output text 2>/dev/null || true)"
      fi
      if [[ -n "$url" && "$url" != "None" ]]; then
        break
      fi
    fi
    sleep 15
  done

  echo
  if [[ -n "$url" && "$url" != "None" ]]; then
    [[ "$url" != https://* ]] && url="https://${url}"
    echo "Chat URL:  $url"
    echo "Health:    curl -s ${url}/api/health"
    echo "Status:    curl -s ${url}/api/status"
  else
    echo "Service created (status=${status:-unknown}). Fetch URL:"
    echo "  aws ecs list-express-gateway-services --region $AWS_REGION"
    echo "  aws ecs describe-express-gateway-service --region $AWS_REGION --service-arn <ARN>"
  fi
  echo
  echo "Next: bash scripts/aws-worker-schedule.sh"
}

# ---------- run ----------
case "$MODE" in
  secret) upsert_secret ;;
  image)  push_image ;;
  full)
    upsert_secret
    push_image
    deploy_express_chat
    ;;
esac
