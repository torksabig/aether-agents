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

# Avoid getting stuck in `less` on aws table/json output
export AWS_PAGER=""

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

  # Fargate needs a plain linux/amd64 image (no attestation index from Docker Desktop)
  export BUILDX_NO_DEFAULT_ATTESTATIONS=1
  docker buildx build \
    --platform linux/amd64 \
    --provenance=false \
    --sbom=false \
    -t "$ECR_URI" \
    --push \
    .
  echo "    pushed $ECR_URI (linux/amd64, no attestations)"
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

env_from_secret_json() {
  # Resolve secret → plain ECS "environment" entries at deploy time.
  # Avoids ResourceInitializationError on JSON-key secret refs (common with partial keys).
  # Source of truth remains Secrets Manager (upsert_secret); task def holds a snapshot.
  python3 - "$SECRET_ARN" "$AWS_REGION" <<'PY'
import json, sys, subprocess
arn, region = sys.argv[1], sys.argv[2]
raw = subprocess.check_output([
    "aws", "secretsmanager", "get-secret-value",
    "--secret-id", arn, "--region", region,
    "--query", "SecretString", "--output", "text",
], text=True)
data = json.loads(raw)
print(json.dumps([
    {"name": k, "value": str(v)}
    for k, v in sorted(data.items())
    if v not in (None, "")
]))
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
  local env_json
  env_json="$(env_from_secret_json)"
  echo "    injecting $(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$env_json") env keys from Secrets Manager"
  # Write container def to a file (avoids shell-escaping secrets)
  python3 - "$ECR_URI" "$PORT" "$TASK_FAMILY" "$AWS_REGION" "$env_json" <<'PY' >/tmp/aether-chat-container.json
import json, sys
image, port, family, region, env_raw = sys.argv[1:6]
env = json.loads(env_raw)
print(json.dumps([{
    "name": "chat",
    "image": image,
    "essential": True,
    "portMappings": [{"containerPort": int(port), "protocol": "tcp"}],
    "environment": env,
    "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
            "awslogs-group": f"/ecs/{family}",
            "awslogs-region": region,
            "awslogs-stream-prefix": "chat",
        },
    },
}]))
PY
  TASK_REV_ARN="$(aws ecs register-task-definition \
    --region "$AWS_REGION" \
    --family "$TASK_FAMILY" \
    --requires-compatibilities FARGATE \
    --network-mode awsvpc \
    --cpu "256" \
    --memory "512" \
    --execution-role-arn "$EXEC_ARN" \
    --task-role-arn "$TASK_ARN" \
    --runtime-platform "cpuArchitecture=X86_64,operatingSystemFamily=LINUX" \
    --container-definitions file:///tmp/aether-chat-container.json \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
  rm -f /tmp/aether-chat-container.json
  echo "    registered $TASK_REV_ARN"

  echo "==> ECS service $SERVICE_NAME"
  if aws ecs describe-services --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE_NAME" \
      --query 'services[?status==`ACTIVE`].serviceName' --output text 2>/dev/null | grep -q "$SERVICE_NAME"; then
    # Pin exact revision so we never keep an old secrets-based task def
    aws ecs update-service --region "$AWS_REGION" --cluster "$CLUSTER" --service "$SERVICE_NAME" \
      --task-definition "$TASK_REV_ARN" --force-new-deployment >/dev/null
  else
    aws ecs create-service --region "$AWS_REGION" \
      --cluster "$CLUSTER" \
      --service-name "$SERVICE_NAME" \
      --task-definition "$TASK_REV_ARN" \
      --desired-count 1 \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=[${ECS_SUBNETS}],securityGroups=[${TASK_SG}],assignPublicIp=ENABLED}" \
      --load-balancers "targetGroupArn=${TG_ARN},containerName=chat,containerPort=${PORT}" \
      --health-check-grace-period-seconds 60 >/dev/null
  fi

  echo "    waiting up to ~3 min for tasks…"
  # Don't hang forever — poll a few times then print diagnostics
  ok=0
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    running="$(aws ecs describe-services --region "$AWS_REGION" --cluster "$CLUSTER" \
      --services "$SERVICE_NAME" --query 'services[0].runningCount' --output text 2>/dev/null || echo 0)"
    healthy="$(aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn "$TG_ARN" \
      --query 'length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])' --output text 2>/dev/null || echo 0)"
    echo "      try $i: running=$running healthy=$healthy"
    if [[ "$running" != "0" && "$healthy" != "0" ]]; then
      ok=1
      break
    fi
    sleep 15
  done

  if [[ "$ok" != "1" ]]; then
    echo "    Service not healthy yet. Recent events:"
    aws ecs describe-services --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE_NAME" \
      --query 'services[0].events[:5].[createdAt,message]' --output table || true
    echo "    Stopped tasks (if any):"
    aws ecs list-tasks --region "$AWS_REGION" --cluster "$CLUSTER" --service-name "$SERVICE_NAME" \
      --desired-status STOPPED --query 'taskArns[:2]' --output text | tr '\t' '\n' | while read -r t; do
      [[ -z "$t" || "$t" == "None" ]] && continue
      aws ecs describe-tasks --region "$AWS_REGION" --cluster "$CLUSTER" --tasks "$t" \
        --query 'tasks[0].{stop:stoppedReason,containers:containers[0].reason}' --output json || true
    done
  fi

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
