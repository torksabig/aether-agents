# How to do it: Notion control plane + AWS workers

Three roles. Do not mix them.

| Role | What it is | What it is **not** |
|------|------------|--------------------|
| **Notion** | Queue + review — Command Center, Agent Tasks, Needs Review | Where agents “run” compute |
| **AWS** | Where **Aether** agents run (chat + queue drain) | Your Cursor coding agent |
| **Cursor Notion agent** | Coding helper on **Cursor Cloud** (@mention on pages) | Your BMS / lead→funding pipeline |

```
You queue / review in Notion
        │
        ▼
   AWS App Runner  →  aether-web chat (browser)
   AWS EventBridge →  ECS Fargate runner (drain Queued)
        │
        ▼
Results land back in Notion (Needs Review)

Separate: Notion Agents → Cursor  =  PR/code help only
```

---

## Fast path (Mac)

### 0. If `brew` is missing (your case)

Paste **one** line in Terminal (installer asks for your Mac password):

```bash
curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-aws-bootstrap.sh | bash
```

That script: fixes broken `~/.zprofile` brew lines → installs Homebrew → installs `git` / `python` / `awscli` / Docker Desktop → clones into **`~/aether-agent-platform`** (real path, not a placeholder).

Then:

```bash
cd ~/aether-agent-platform
open -e .env          # set NOTION_TOKEN, save
aws configure         # Access key, secret, region eu-north-1
aws sts get-caller-identity
# wait until Docker Desktop is Running (whale icon idle)
```

### 1. Chat on AWS (secret + ECR + App Runner)

```bash
cd ~/aether-agent-platform
./scripts/aws-deploy.sh
```

Script prints the App Runner URL. Smoke:

```bash
curl -s https://YOUR_URL/api/health
curl -s https://YOUR_URL/api/status   # expect notion_ready: true
```

Open that URL in a browser — same chat as local (`queue`, `process`, `ask …`).

Partial runs:

```bash
./scripts/aws-deploy.sh --secret-only   # refresh Secrets Manager from .env
./scripts/aws-deploy.sh --image-only    # rebuild/push image only
```

### 2. Queue worker on a schedule (true AWS drain)

```bash
./scripts/aws-worker-schedule.sh
```

Creates ECS cluster + Fargate task + EventBridge rule (4× daily UTC, same cron as GitHub Actions).

Until this is done, **`.github/workflows/agents.yml`** can keep draining Notion if repo secrets are set.

### 3. Daily loop

| You do | Where |
|--------|--------|
| Add company / set task **Queued** | Notion Command Center |
| Chat / hit **process** | App Runner URL |
| Auto-drain queue | EventBridge → ECS (or GitHub Actions) |
| Review results | Notion **Needs Review** |
| Coding / PR help | Notion Agents → **Cursor** (not AWS) |

### 4. Cursor Notion agent (2 minutes, optional)

Not the AWS pipeline.

1. Notion → **Agents** → **Cursor**
2. Share Command Center + DBs with that agent
3. @mention Cursor on a page

Needs Notion Business/Enterprise + Cursor API key — [docs](https://cursor.com/docs/integrations/notion).

---

## Manual steps (if you prefer Console)

Same outcome as the scripts. Region example: `eu-north-1`.

### Secrets

```bash
export AWS_REGION=eu-north-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export REPO=aether-agents
export IMAGE_TAG=latest
```

Put `NOTION_TOKEN` + data source IDs in Secrets Manager as `aether/agents/env` (JSON).  
Or run `./scripts/aws-deploy.sh --secret-only`.

### ECR image

```bash
aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" || true
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin \
    "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
docker build -t "$REPO:$IMAGE_TAG" .
docker tag "$REPO:$IMAGE_TAG" \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:$IMAGE_TAG"
docker push \
  "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:$IMAGE_TAG"
```

Image default CMD is `aether-web` (port **3847**). Worker overrides command to  
`python -m aether_agents.runner --batch-limit 10`.

### App Runner (console)

1. **App Runner** → Create service → ECR → `aether-agents:latest`
2. Port **3847**
3. Wire env from secret `aether/agents/env` (instance role needs `secretsmanager:GetSecretValue`)
4. Create → open Service URL

### EventBridge → ECS (console)

1. ECS cluster (Fargate) + task definition: same image, command override above, secrets from Secrets Manager  
2. EventBridge schedule `cron(23 7,11,15,19 * ? *)` → ECS RunTask  

Or: `./scripts/aws-worker-schedule.sh`

---

## Verify end-to-end

1. Notion: leave ≥1 task **Queued**
2. App Runner chat: `process` (or wait for the schedule)
3. Notion: task moves toward Needs Review / Done
4. `/api/status` still `notion_ready: true`

---

## Cost & safety

- Light App Runner + tiny Fargate schedule ≈ low tens of USD/month  
- Never commit `NOTION_TOKEN` — Secrets Manager / GitHub secrets only  
- Rotate any token that was pasted in chat  
- OpenRouter optional until Bedrock is wired  

## Stuck?

| Symptom | Fix |
|---------|-----|
| `notion_ready: false` | Secret not injected; re-run `--secret-only` and update App Runner |
| Worker runs, Notion 404 | Share DBs with integration **TT** again |
| Image won’t start | Port must be **3847**; check App Runner logs |
| No AWS CLI in Cursor Cloud | Run the scripts on your Mac |

Architecture sketch: [`aws-agent-hosting.md`](aws-agent-hosting.md).
