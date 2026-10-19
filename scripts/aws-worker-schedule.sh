#!/usr/bin/env bash
# Schedule Notion queue drains on AWS (EventBridge → ECS Fargate).
# Run AFTER scripts/aws-deploy.sh (needs ECR image + secret aether/agents/env).
#
# Creates:
#   • ECS cluster          aether-agents
#   • Task definition      aether-agents-worker
#   • EventBridge rule     aether-agents-queue  (4× daily UTC, same as GHA)
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-eu-north-1}"
REPO="${REPO:-aether-agents}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SECRET_NAME="${SECRET_NAME:-aether/agents/env}"
CLUSTER="${CLUSTER:-aether-agents}"
TASK_FAMILY="${TASK_FAMILY:-aether-agents-worker}"
RULE_NAME="${RULE_NAME:-aether-agents-queue}"
# Same cadence as .github/workflows/agents.yml
SCHEDULE="${SCHEDULE:-cron(23 7,11,15,19 * ? *)}"

need() { command -v "$1" >/dev/null || { echo "Missing: $1" >&2; exit 1; }; }
need aws
need python3

echo "==> Caller"
aws sts get-caller-identity --region "$AWS_REGION"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO}:${IMAGE_TAG}"
SECRET_ARN="$(aws secretsmanager describe-secret \
  --secret-id "$SECRET_NAME" --region "$AWS_REGION" --query ARN --output text)"

# --- IAM: task execution (ECR + secrets) + task role ---
EXEC_ROLE="aether-ecs-execution"
TASK_ROLE="aether-ecs-task"

if ! aws iam get-role --role-name "$EXEC_ROLE" >/dev/null 2>&1; then
  echo "==> IAM $EXEC_ROLE"
  aws iam create-role --role-name "$EXEC_ROLE" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null
  aws iam attach-role-policy --role-name "$EXEC_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
  aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name aether-secret-pull \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Action\":[\"secretsmanager:GetSecretValue\"],
        \"Resource\":\"${SECRET_ARN}\"
      }]
    }" >/dev/null
  sleep 8
fi

if ! aws iam get-role --role-name "$TASK_ROLE" >/dev/null 2>&1; then
  echo "==> IAM $TASK_ROLE"
  aws iam create-role --role-name "$TASK_ROLE" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null
  sleep 5
fi

EXEC_ARN="$(aws iam get-role --role-name "$EXEC_ROLE" --query Role.Arn --output text)"
TASK_ARN="$(aws iam get-role --role-name "$TASK_ROLE" --query Role.Arn --output text)"

# --- Cluster ---
echo "==> ECS cluster $CLUSTER"
aws ecs describe-clusters --clusters "$CLUSTER" --region "$AWS_REGION" \
  --query "clusters[?status=='ACTIVE'].clusterName" --output text 2>/dev/null | grep -q "$CLUSTER" \
  || aws ecs create-cluster --cluster-name "$CLUSTER" --region "$AWS_REGION" >/dev/null

# --- Task definition (one-shot runner) ---
echo "==> Task definition $TASK_FAMILY"
KEYS=(
  NOTION_TOKEN
  NOTION_TASKS_DATA_SOURCE_ID
  NOTION_COMPANIES_DATA_SOURCE_ID
  NOTION_RESEARCH_DATA_SOURCE_ID
  NOTION_PROJECTS_DATA_SOURCE_ID
  NOTION_FUNDING_DATA_SOURCE_ID
  NOTION_COMMAND_CENTER_PAGE_ID
  OPENROUTER_API_KEY
  OPENROUTER_BASE_URL
  OPENROUTER_MODEL
  AGENT_BATCH_LIMIT
  AGENT_DRY_RUN
)
SECRETS_JSON="$(python3 - "$SECRET_ARN" "${KEYS[@]}" <<'PY'
import json, sys
arn = sys.argv[1]
keys = sys.argv[2:]
print(json.dumps([{"name": k, "valueFrom": f"{arn}:{k}::"} for k in keys]))
PY
)"

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
    \"name\": \"worker\",
    \"image\": \"${IMAGE}\",
    \"essential\": true,
    \"command\": [\"python\", \"-m\", \"aether_agents.runner\", \"--batch-limit\", \"10\"],
    \"secrets\": ${SECRETS_JSON},
    \"logConfiguration\": {
      \"logDriver\": \"awslogs\",
      \"options\": {
        \"awslogs-group\": \"/ecs/${TASK_FAMILY}\",
        \"awslogs-region\": \"${AWS_REGION}\",
        \"awslogs-stream-prefix\": \"worker\",
        \"awslogs-create-group\": \"true\"
      }
    }
  }]" >/dev/null

# Default VPC + public subnets (simple; override with AETHER_SUBNETS / AETHER_SG if set)
if [[ -z "${AETHER_SUBNETS:-}" ]]; then
  VPC="$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"
  AETHER_SUBNETS="$(aws ec2 describe-subnets --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$VPC" \
    --query 'Subnets[*].SubnetId' --output text | tr '\t' ',')"
fi
if [[ -z "${AETHER_SG:-}" ]]; then
  AETHER_SG="$(aws ec2 describe-security-groups --region "$AWS_REGION" \
    --filters "Name=group-name,Values=default" "Name=vpc-id,Values=${VPC:-}" \
    --query 'SecurityGroups[0].GroupId' --output text)"
fi

# --- EventBridge schedule ---
echo "==> EventBridge rule $RULE_NAME ($SCHEDULE)"
aws events put-rule \
  --region "$AWS_REGION" \
  --name "$RULE_NAME" \
  --schedule-expression "$SCHEDULE" \
  --state ENABLED >/dev/null

EVENTS_ROLE="aether-events-ecs"
if ! aws iam get-role --role-name "$EVENTS_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$EVENTS_ROLE" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"events.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null
  aws iam put-role-policy --role-name "$EVENTS_ROLE" --policy-name run-ecs-task \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[
        {\"Effect\":\"Allow\",\"Action\":[\"ecs:RunTask\"],\"Resource\":\"arn:aws:ecs:${AWS_REGION}:${AWS_ACCOUNT_ID}:task-definition/${TASK_FAMILY}:*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"iam:PassRole\"],\"Resource\":[\"${EXEC_ARN}\",\"${TASK_ARN}\"]}
      ]
    }" >/dev/null
  sleep 8
fi
EVENTS_ARN="$(aws iam get-role --role-name "$EVENTS_ROLE" --query Role.Arn --output text)"

# Build subnet JSON array
SUBNET_JSON="$(python3 -c "import json,os; print(json.dumps(os.environ['S'].split(',')))" S="$AETHER_SUBNETS")"

aws events put-targets \
  --region "$AWS_REGION" \
  --rule "$RULE_NAME" \
  --targets "[{
    \"Id\": \"aether-worker\",
    \"Arn\": \"arn:aws:ecs:${AWS_REGION}:${AWS_ACCOUNT_ID}:cluster/${CLUSTER}\",
    \"RoleArn\": \"${EVENTS_ARN}\",
    \"EcsParameters\": {
      \"TaskDefinitionArn\": \"arn:aws:ecs:${AWS_REGION}:${AWS_ACCOUNT_ID}:task-definition/${TASK_FAMILY}\",
      \"TaskCount\": 1,
      \"LaunchType\": \"FARGATE\",
      \"NetworkConfiguration\": {
        \"awsvpcConfiguration\": {
          \"Subnets\": ${SUBNET_JSON},
          \"SecurityGroups\": [\"${AETHER_SG}\"],
          \"AssignPublicIp\": \"ENABLED\"
        }
      }
    }
  }]" >/dev/null

echo
echo "Scheduled. Manual one-shot now:"
echo "  aws ecs run-task --region $AWS_REGION --cluster $CLUSTER --launch-type FARGATE \\"
echo "    --task-definition $TASK_FAMILY \\"
echo "    --network-configuration 'awsvpcConfiguration={subnets=[${AETHER_SUBNETS%%,*}],securityGroups=[$AETHER_SG],assignPublicIp=ENABLED}'"
echo
echo "Then check Notion → Agent Tasks & Runs (Queued should move)."
