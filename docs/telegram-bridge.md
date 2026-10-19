# Telegram front door → Notion output subpages

Talk to Aether agents in Telegram. Results land as child pages under **Telegram Agent Outputs** in Notion; the bot replies with a short summary and an **Open in Notion** link.

```text
Telegram message
    → ECS long-poller (aether-web)
    → NL map → AetherChat / agent modules
    → Notion child page
    → Telegram notification + link
```

Notion Custom Agents are not required. Notion remains the durable store.

## Why long polling (not webhook)

Telegram webhooks need **HTTPS**. The current ALB URL is HTTP-only, so the bot uses `getUpdates` long polling inside the same ECS chat task. A webhook route can be added later once you have a TLS certificate and domain.

## Setup

### 1. Create the bot

1. Open Telegram → [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the bot token → `TELEGRAM_BOT_TOKEN`

### 2. Allowlist your chat

1. Message your bot `/start`
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy `message.chat.id` into `TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated for multiple people)

Empty allowlist = nobody is accepted (safe default).

### 3. Notion outputs parent

Either:

- Create a page **Telegram Agent Outputs** under Command Center, share it with your Internal Integration, set `NOTION_OUTPUTS_PARENT_PAGE_ID`, **or**
- Leave `NOTION_OUTPUTS_PARENT_PAGE_ID` blank and the bot will auto-create the parent under `NOTION_COMMAND_CENTER_PAGE_ID` on first run (still needs the Command Center shared with the integration).

### 4. Enable and redeploy

Add to Secrets Manager `aether/agents/env` (and local `.env`):

```bash
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
NOTION_OUTPUTS_PARENT_PAGE_ID=...   # optional if auto-create is OK
OPENROUTER_API_KEY=...              # optional; enables free-text NL mapping
```

Redeploy the ECS chat service (`./scripts/aws-deploy.sh`) or restart `aether-web` locally.

Check `/api/status` — `telegram_ready` should be `true`.

## Example messages

- `lead Acme Oy acme.fi`
- `research Acme Oy`
- `qualify co_acme.fi`
- `run Demo Building Owner Oy`
- Free text: `look into Helsingin Energia BMS` (needs OpenRouter)

## Security notes

- Only allowlisted chat IDs can run agents.
- Keep the bot token only in Secrets Manager / `.env`.
- Do not set `TELEGRAM_ENABLED=true` without an allowlist.
