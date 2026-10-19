# Aether Applied — Agent Platform

Coordinated multi-agent opportunity pipeline for Aether Applied. Notion is the command center and knowledge base; GitHub Actions / AWS ECS run scheduled Python workers. The only variable cost is LLM tokens.

**Pipeline:** Extract → Verify → Qualify → Assess Feasibility → Match Funding → Human Review → Next Action

Agents never treat another agent’s unsupported conclusion as verified evidence. Original source URLs are retained throughout.

**Purpose (short):** [`docs/PURPOSE.md`](docs/PURPOSE.md) · **Telegram front door:** [`docs/telegram-bridge.md`](docs/telegram-bridge.md) · **Notion Agents Library (optional):** [`docs/notion-agents-library.md`](docs/notion-agents-library.md)

> Preferred interaction path: **Telegram → AWS agents → Notion subpage + Telegram link**. See Telegram setup in `.env.example` and `docs/telegram-bridge.md`.

## Repository layout

```
aether-agent-platform/
├── .github/workflows/     # quality.yml + agents.yml
├── src/aether_agents/     # agents, Notion/LLM clients, pipeline runner
├── tests/                 # unit + fixtures (no live Notion/LLM required)
├── pyproject.toml
└── .env.example
```

## Quick start — hosted agent chat (browser)

Agents run on a local web server you can open from any browser on the machine:

```bash
source .venv/bin/activate   # if needed
aether-web
# open http://127.0.0.1:3847
```

Same commands as the CLI (`status`, `queue`, `process`, `ask …`) — results write to Notion when `NOTION_TOKEN` is set.

### AWS (production power)

**Notion** = queue + review. **AWS** = where Aether agents run. **Cursor Notion agent** = coding helper only (not this pipeline).

If Terminal says `brew: command not found`, install tools + clone first:

```bash
curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-aws-bootstrap.sh | bash
```

Then (exact path — your home folder):

```bash
cd ~/aether-agent-platform
open -e .env && aws configure    # NOTION_TOKEN + AWS keys, region eu-north-1
bash scripts/aws-deploy.sh       # secret → ECR → ECS Express chat
bash scripts/aws-worker-schedule.sh  # EventBridge → ECS drain Queued
```

Full how-to: [`docs/aws-deploy-steps.md`](docs/aws-deploy-steps.md) · sketch: [`docs/aws-agent-hosting.md`](docs/aws-agent-hosting.md)

```bash
docker compose up --build chat          # local stand-in for App Runner
docker compose --profile worker run --rm worker   # one queue drain
```

## Quick start — chat with the agents (CLI)

### macOS (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-setup.sh | bash
```

Then:

```bash
cd ~/aether-agent-platform
source .venv/bin/activate
aether
```

### Manual install

```bash
git clone https://github.com/torksabig/aether-agents.git
cd aether-agent-platform
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env   # add NOTION_TOKEN when you want Notion writes
aether                 # opens chat
```

Talk normally:

```text
aether> help
aether> ask Demo Building Owner Oy
aether> lead Acme Oy acme.fi https://acme.fi
aether> run co_demo-building.fi
aether> queue
aether> process
```

One-shot without entering the REPL:

```bash
aether ask Demo Building Owner Oy
aether run co_demo-building.fi
```

With Notion configured, `run` / `lead` / `qualify` / … create tasks in the Command Center. Without Notion, the same commands still work locally.

### Other entrypoints

```bash
python -m aether_agents.eval_runner          # fixed evaluation set
python -m aether_agents.runner --batch-limit 10   # CI / scheduled queue worker
```

### Notion Command Center (live)

https://app.notion.com/p/3feb22873e66818f983ade85647877b4

Data source IDs: `config/notion_command_center.json` and `.env.example`.

Create a Notion **Internal connection** at [app.notion.com/developers/connections](https://app.notion.com/developers/connections) (Workspace Owner + Developer Mode), share the five databases with it, set `NOTION_TOKEN`, then use `aether` chat.

### Quality gates

```bash
ruff check .
ruff format --check .
mypy src/
pytest tests/unit --cov=src/aether_agents --cov-fail-under=80
```

### Process Notion queue

```bash
python -m aether_agents.runner --batch-limit 10
```

## Notion Command Center

Create a top-level page **Aether Applied — Agent Command Center** with five databases:

| Database | Required properties |
| --- | --- |
| Companies & Leads | Name, Domain, External ID, Stage, Owner, Source URL, Last Updated |
| Research & Evidence | Finding, Source URL, Related Company, Fact/Hypothesis, Confidence, Verified Date |
| Projects & Pilots | Project, Company, Use Case, Technical Status, Data Access, Owner, Decision |
| Funding & Partnerships | Programme, Organization, Region, Deadline, Eligibility, Project Match, Status, Source URL |
| Agent Tasks & Runs | Task, Agent, Target ID, Status, Attempts, Last Run, Error, Result Link |

Linked views on the Command Center page: New Leads, Needs Qualification, Technical Review, Funding Matches, Human Approvals, Failed Agent Runs.

**Task states:** Queued → Running → Needs Review → Approved, plus Failed / Blocked / Rejected.

Queue work by creating a row in **Agent Tasks & Runs** with Status = `Queued`. GitHub Actions picks it up on the next schedule (four times daily UTC) or via workflow_dispatch.

### API version

This client uses `Notion-Version: 2025-09-03` and queries via:

`POST /v1/data_sources/{data_source_id}/query`

Copy each database’s **data source id** into the secrets listed in `.env.example`.

### Free-plan block limit

Workspaces with two or more members on Notion Free are limited to 1,000 blocks. V1 writes **compact structured records** only; detailed logs stay in GitHub Actions.

## Agents

| Agent | Receives | Produces | Human review |
| --- | --- | --- | --- |
| Lead Extraction | Company sources | Company / contact records | Uncertain identity |
| Research Extraction | Sources + questions | Source-linked findings | Material claims |
| Evidence Verification | Extracted claims | Supported / unsupported / conflicting | High-impact contradictions |
| Opportunity Qualification | Lead + verified research | Fit + discovery questions | Before sales priority |
| Technical Feasibility | Qualified project | Gaps, risks, prerequisites | Engineer approval |
| Funding & Partnership | Feasible project | Programme matches + gaps | Before applications |

Funding eligibility outcomes are only **Eligible**, **Not eligible**, or **Unverified**. A high match score never overrides a failed eligibility requirement. Autonomous external communication is disabled.

## Secrets (GitHub Actions)

| Secret | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Internal connection API token ([developers/connections](https://app.notion.com/developers/connections)) |
| `NOTION_TASKS_DATA_SOURCE_ID` | Agent Tasks & Runs data source |
| `NOTION_COMPANIES_DATA_SOURCE_ID` | Companies & Leads |
| `NOTION_RESEARCH_DATA_SOURCE_ID` | Research & Evidence |
| `NOTION_PROJECTS_DATA_SOURCE_ID` | Projects & Pilots |
| `NOTION_FUNDING_DATA_SOURCE_ID` | Funding & Partnerships |
| `OPENROUTER_API_KEY` | LLM provider (optional for rule-based V1 path) |

Also set provider-side spending caps. Per-task token budgets can be stored as **Approved Token Budget** on a task when you add that property.

## Cost controls (V1)

- Four scheduled runs/day, max 10 tasks/run, 15-minute job timeout
- One LLM retry for malformed JSON
- No extra infrastructure subscriptions (€0)
- Compact Notion writes; no long per-run documents

## Design rules

- Every agent shares the same `AgentInput` / `AgentResult` interface
- External APIs go through `integrations/`
- Agents cannot mutate another agent’s results
- Business logic is unit-tested without Notion or an LLM
- Model outputs are validated (Pydantic) before Notion writes
- Each run has a unique `run_id`; companies use stable `external_id`
- Failed runs are retryable without creating duplicates

## Integrating existing extractors

Point legacy Lead / Research scripts at:

- `aether_agents.agents.lead_extraction.extract_company_from_payload`
- `aether_agents.agents.research_extraction.extract_findings`

or set plugin env vars (callable must return `CompanyRecord` / `list[ResearchFinding]` or compatible dicts):

```bash
export AETHER_LEAD_EXTRACTOR=my_package.leads:extract_company
export AETHER_RESEARCH_EXTRACTOR=my_package.research:extract_findings
```

Existing Modamer Jobs worker (`scripts/notion_worker.py` under Outreach Control) can keep its pipeline; this platform is the BMS opportunity track. Do not rewrite working extraction logic—wrap it.

## Nordic angel outreach pack

Formatted FI + SE business angel pack (€20–30k / 3–4 weeks):

| File | Use |
| --- | --- |
| `docs/Nordic_Business_Angels_FI_SE.docx` | Upload to Drive → Open with Google Docs (best formatting) |
| `docs/Nordic_Business_Angels_FI_SE.html` | Open in browser → Select all → Paste into an existing Doc |
| `docs/nordic-angels-direct-contacts.md` | Public emails/phones found + LinkedIn + sources |
| `docs/nordic-angels-malls-format.txt` | Plain `#1–110` contact lines |
| `scripts/build_nordic_angels_doc.py` | Rebuild DOCX/HTML from the malls-format list |

**Replace an existing Google Doc:** File → Open → Upload → choose the `.docx`, then copy the new Doc into your target (or File → Make a copy). Paste from the HTML file preserves headings and tables reasonably well.

## Production checklist

See the Go/No-Go list in the project plan: existing agents wrapped, E2E demo, quality gates green, no duplicates on replay, source-backed recommendations, human review for technical/funding, failure recovery, token safeguards, Notion usable by the team.
