#!/usr/bin/env bash
# Deploy Aether agents to AWS. Notion stays the control plane.
#
# What this creates (eu-north-1):
#   1. Secrets Manager secret     aether/agents/env
#   2. ECR repo + pushed image    aether-agents:latest
#   3. ECS Fargate service + ALB  aether-agents-chat  (http://<alb-dns>:80 → :3847)
#
# Prerequisites (Mac):
#   eval "$(/opt/homebrew/bin/brew shellenv)"
#   brew install awscli && brew install --cask docker
#   aws configure   # Access Key ID starts with AKIA…
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
CLUSTER="${CLUSTER:-aether-agents}"
SERVICE_NAME="${SERVICE_NAME:-aether-agents-chat}"
TASK_FAMILY="${TASK_FAMILY:-aether-agents-chat}"
PORT="${PORT:-3847}"
EXEC_ROLE="${EXEC_ROLE:-aether-ecs-execution}"
TASK_ROLE="${TASK_ROLE:-aether-ecs-task}"

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
need python3
if [[ "$MODE" == "full" || "$MODE" == "image" ]]; then
  need docker
fi

echo "==> Caller"
aws sts get-caller-identity --region "$AWS_REGION"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO}:${IMAGE_TAG}"

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
    k, v = k.strip(), v.strip().strip('"').strip("'")
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
      --secret-id "$SECRET_NAME" --region "$AWS_REGION" --secret-string "$payload" >/dev/null
    echo "    updated"
  else
    aws secretsmanager create-secret \
      --name "$SECRET_NAME" --region "$AWS_REGION" --secret-string "$payload" >/dev/null
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

ensure_ecs_roles() {
  local secret_arn
  secret_arn="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --region "$AWS_REGION" --query ARN --output text)"
  SECRET_ARN="$secret_arn"

  # Service-linked role — ignore "already exists" / "name taken"
  if ! aws iam get-role --role-name AWSServiceRoleForECS >/dev/null 2>&1; then
    echo "==> Ensuring ECS service-linked role"
    aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null 2>&1 \
      || aws iam restore-service-linked-role --role-name AWSServiceRoleForECS >/dev/null 2>&1 \
      || true
    sleep 5
  else
    echo "==> ECS service-linked role OK"
  fi

  if ! aws iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
    echo "==> IAM $EXEC_ROLE"
    aws iam create-role --role-name "$EXEC_ROLE" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
      }' >/dev/null
    aws iam attach-role-policy --role-name "$EXEC_ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
  fi
  aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name aether-secret-pull \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Action\":[\"secretsmanager:GetSecretValue\"],
        \"Resource\":\"${secret_arn}\"
      }]
    }" >/dev/null

  if ! aws iam get-role --role-name "$TASK_ROLE" >/dev/null 2>&1; then
    echo "==> IAM $TASK_ROLE"
    aws iam create-role --role-name "$TASK_ROLE" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
      }' >/dev/null
  fi

  EXEC_ARN="$(aws iam get-role --role-name "$EXEC_ROLE" --query Role.Arn --output text)"
  TASK_ARN="$(aws iam get-role --role-name "$TASK_ROLE" --query Role.Arn --output text)"
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
print(json.dumps([{"name": k, "valueFrom": f"{arn}:{k}::"} for k in keys]))
PY
}

deploy_ecs_alb_chat() {
  ensure_ecs_roles

  echo "==> ECS cluster $CLUSTER"
  aws ecs describe-clusters --clusters "$CLUSTER" --region "$AWS_REGION" \
    --query "clusters[?status=='ACTIVE'].clusterName" --output text 2>/dev/null | grep -q "$CLUSTER" \
    || aws ecs create-cluster --cluster-name "$CLUSTER" --region "$AWS_REGION" >/dev/null

  # Default VPC networking
  VPC="$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"
  if [[ -z "$VPC" || "$VPC" == "None" ]]; then
    echo "No default VPC in $AWS_REGION. Create one in the VPC console, then re-run." >&2
    exit 1
  fi
  SUBNET_LIST="$(aws ec2 describe-subnets --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC" "Name=default-for-az,Values=true" \
    --query 'Subnets[*].SubnetId' --output text | tr '\t' ' ' | xargs)"
  if [[ -z "$SUBNET_LIST" ]]; then
    SUBNET_LIST="$(aws ec2 describe-subnets --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$VPC" \
      --query 'Subnets[*].SubnetId' --output text | tr '\t' ' ' | xargs)"
  fi
  if [[ -z "$SUBNET_LIST" ]]; then
    echo "No subnets in $VPC" >&2
    exit 1
  fi
  # ALB needs ≥2 AZs when possible
  SUBNET_ARGS=""
  n=0
  for s in $SUBNET_LIST; do
    SUBNET_ARGS="$SUBNET_ARGS --subnets $s"
    n=$((n + 1))
    [[ $n -ge 4 ]] && break
  done
  # shellcheck disable=SC2086
  set -- $SUBNET_LIST
  ECS_SUBNETS="$1"
  [[ -n "${2:-}" ]] && ECS_SUBNETS="$1,$2"

  echo "==> Security groups"
  ALB_SG_NAME="aether-alb-sg"
  TASK_SG_NAME="aether-task-sg"
  ALB_SG="$(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=group-name,Values=$ALB_SG_NAME" "Name=vpc-id,Values=$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [[ -z "$ALB_SG" || "$ALB_SG" == "None" ]]; then
    ALB_SG="$(aws ec2 create-security-group --region "$AWS_REGION" \
      --group-name "$ALB_SG_NAME" --description "Aether ALB" --vpc-id "$VPC" \
      --query GroupId --output text)"
    aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$ALB_SG" \
      --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
  fi
  TASK_SG="$(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=group-name,Values=$TASK_SG_NAME" "Name=vpc-id,Values=$VPC" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  if [[ -z "$TASK_SG" || "$TASK_SG" == "None" ]]; then
    TASK_SG="$(aws ec2 create-security-group --region "$AWS_REGION" \
      --group-name "$TASK_SG_NAME" --description "Aether ECS tasks" --vpc-id "$VPC" \
      --query GroupId --output text)"
    aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$TASK_SG" \
      --protocol tcp --port "$PORT" --source-group "$ALB_SG" >/dev/null
  fi

  echo "==> ALB + target group"
  ALB_ARN="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --names aether-agents-alb --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
  if [[ -z "$ALB_ARN" || "$ALB_ARN" == "None" ]]; then
    # shellcheck disable=SC2086
    ALB_ARN="$(aws elbv2 create-load-balancer --region "$AWS_REGION" \
      --name aether-agents-alb --type application --scheme internet-facing \
      $SUBNET_ARGS --security-groups "$ALB_SG" \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  fi
  ALB_DNS="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --load-balancer-arns "$ALB_ARN" --query 'LoadBalancers[0].DNSName' --output text)"

  TG_ARN="$(aws elbv2 describe-target-groups --region "$AWS_REGION" \
    --names aether-agents-tg --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
  if [[ -z "$TG_ARN" || "$TG_ARN" == "None" ]]; then
    TG_ARN="$(aws elbv2 create-target-group --region "$AWS_REGION" \
      --name aether-agents-tg --protocol HTTP --port "$PORT" --vpc-id "$VPC" \
      --target-type ip \
      --health-check-path /api/health --health-check-interval-seconds 30 \
      --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
      --query 'TargetGroups[0].TargetGroupArn' --output text)"
  fi

  LISTENER="$(aws elbv2 describe-listeners --region "$AWS_REGION" --load-balancer-arn "$ALB_ARN" \
    --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || true)"
  if [[ -z "$LISTENER" || "$LISTENER" == "None" ]]; then
    aws elbv2 create-listener --region "$AWS_REGION" --load-balancer-arn "$ALB_ARN" \
      --protocol HTTP --port 80 \
      --default-actions "Type=forward,TargetGroupArn=$TG_ARN" >/dev/null
  fi

  echo "==> CloudWatch log group"
  aws logs create-log-group --region "$AWS_REGION" --log-group-name "/ecs/$TASK_FAMILY" 2>/dev/null || true

  echo "==> Task definition $TASK_FAMILY"
  local secrets_json
  secrets_json="$(secrets_list_json)"
  aws ecs register-task-definition \
    --region "$AWS_REGION" \
    --family "$TASK_FAMILY" \
    --requires-compatibilities FARGATE \
    --network-mode awsvpc \
    --cpu "256" \
    --memory "512" \
    --execution-role-arn "$EXEC_ARN" \
    --task-role-arn "$TASK_ARN" \
    --container-definitions "[{
      \"name\": \"chat\",
      \"image\": \"${ECR_URI}\",
      \"essential\": true,
      \"portMappings\": [{\"containerPort\": ${PORT}, \"protocol\": \"tcp\"}],
      \"secrets\": ${secrets_json},
      \"logConfiguration\": {
        \"logDriver\": \"awslogs\",
        \"options\": {
          \"awslogs-group\": \"/ecs/${TASK_FAMILY}\",
          \"awslogs-region\": \"${AWS_REGION}\",
          \"awslogs-stream-prefix\": \"chat\"
        }
      }
    }]" >/dev/null

  echo "==> ECS service $SERVICE_NAME"
  if aws ecs describe-services --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE_NAME" \
      --query 'services[?status==`ACTIVE`].serviceName' --output text 2>/dev/null | grep -q "$SERVICE_NAME"; then
    aws ecs update-service --region "$AWS_REGION" --cluster "$CLUSTER" --service "$SERVICE_NAME" \
      --task-definition "$TASK_FAMILY" --force-new-deployment >/dev/null
  else
    aws ecs create-service --region "$AWS_REGION" \
      --cluster "$CLUSTER" \
      --service-name "$SERVICE_NAME" \
      --task-definition "$TASK_FAMILY" \
      --desired-count 1 \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${ECS_SUBNETS}],securityGroups=[${TASK_SG}],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=${TG_ARN},containerName=chat,containerPort=${PORT}" \
      --health-check-grace-period-seconds 60 >/dev/null
  fi

  echo "    waiting for service stable (may take 2–3 min)…"
  aws ecs wait services-stable --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE_NAME" \
    || echo "    (wait timed out — check target health; URL may still work soon)"

  echo
  echo "Chat URL:  http://${ALB_DNS}"
  echo "Health:    curl -s http://${ALB_DNS}/api/health"
  echo "Status:    curl -s http://${ALB_DNS}/api/status"
  echo
  echo "Next: bash scripts/aws-worker-schedule.sh"
}

case "$MODE" in
  secret) upsert_secret ;;
  image)  push_image ;;
  full)
    upsert_secret
    push_image
    deploy_ecs_alb_chat
    ;;
esac
