# AWS-powered Aether agents

Notion stays the **command center**. AWS runs the **workers** (and optionally the LLM).

```
Notion (queue / review)
        │
        ▼
┌───────────────────────────────┐
│  AWS                          │
│  • App Runner / ECS: aether-web (chat UI + API) │
│  • EventBridge → ECS/Fargate: aether runner     │
│  • Secrets Manager: NOTION_TOKEN, LLM keys      │
│  • Optional: Bedrock instead of OpenRouter      │
└───────────────────────────────┘
```

## What runs where

| Piece | Where | Notes |
|-------|--------|--------|
| Agent Tasks & Runs, Companies, … | Notion | System of record |
| `aether-web` chat | AWS App Runner or ECS Fargate | Port 3847 |
| Queue worker (`runner`) | AWS EventBridge every few hours → Fargate task | Same as GitHub Actions schedule |
| LLM | OpenRouter **or** Amazon Bedrock | Swap via env later |
| Cursor ↔ Notion Agents | Cursor Cloud | Coding agent — separate from this pipeline |

Cursor’s Notion agent page is **not** AWS-hosted. Your **Aether pipeline agents** (lead → funding) are what you put on AWS.

## Minimal AWS layout

1. **ECR** image from `Dockerfile` (`CMD aether-web`).
2. **Secrets Manager** secret with the same keys as `.env` (`NOTION_TOKEN`, data source IDs, `OPENROUTER_API_KEY`).
3. **App Runner** (or ECS service + ALB) serving chat on 3847.
4. **EventBridge rule** (e.g. 4× daily) → ECS RunTask with override:
   `python -m aether_agents.runner --batch-limit 10`
5. IAM task role: Secrets Manager read + (optional) `bedrock:InvokeModel`.

## Local parity

```bash
docker compose up --build chat
# http://127.0.0.1:3847

docker compose --profile worker run --rm worker
```

## Deploy (one command each)

Step-by-step + scripts: **[`aws-deploy-steps.md`](aws-deploy-steps.md)**

```bash
./scripts/aws-deploy.sh              # secret + ECR + App Runner chat
./scripts/aws-worker-schedule.sh     # EventBridge → ECS queue drain
```

## Deploy checklist

- [ ] AWS account + region (e.g. `eu-north-1`)
- [ ] `./scripts/aws-deploy.sh` (or manual ECR + App Runner)
- [ ] Smoke: `/api/health`, `/api/status` → `notion_ready: true`
- [ ] `./scripts/aws-worker-schedule.sh` (or keep GitHub Actions)
- [ ] One Queued task moves after `process` / schedule
- [ ] (Optional) Point LLM at Bedrock; drop OpenRouter

## Notion ↔ AWS seamless loop

1. Human/CLI queues a row in Notion (`Status = Queued`).
2. AWS worker picks it up on schedule (or chat `process`).
3. Results write back to Notion → **Needs Review**.
4. Team reviews in Notion; next task stays Queued.

No Azure required. GitHub Actions can keep running as a backup until EventBridge is live.
