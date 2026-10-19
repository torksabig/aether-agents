# Put Aether agents in Notion Agents Library

The Library URL you want:

https://app.notion.com/library/agents?spaceId=edcb22873e668173ad550003d2765456

## Hard limit

Notion **Custom Agents** in that Library are created only in the Notion UI. The public Agent API can query and chat with agents that already exist; it does **not** create them.

Sources: [Custom Agents help](https://www.notion.com/help/custom-agents), [Agent API quickstart](https://developers.notion.com/guides/notion-agent-apis/quickstart).

## What we already set up

| Piece | Where |
|--------|--------|
| Registry DB | [Aether Agents](https://app.notion.com/p/3feb22873e66819592f7fa50988e47f4) under Command Center |
| 5 agent pages | Lead, Research, Qualification, Feasibility, Funding — each with purpose, inputs, AWS route, review rules |
| UI how-to | [Add Aether agents to Notion Agents Library](https://app.notion.com/p/3feb22873e6681d6bec8f94091b9f00b) |
| AWS routes | `POST /api/agents/{lead\|research\|qualify\|feasibility\|funding}/run` on the ALB |

## Create each Library agent (you click once per agent)

1. Open [Library → Agents](https://app.notion.com/library/agents?spaceId=edcb22873e668173ad550003d2765456)
2. **+** → **Create with AI chat**
3. Paste the matching prompt from the how-to page
4. Share agent access with Command Center + DBs + Internal Integration
5. Save

After that they appear in the Library next to Cursor / Prospect Research.

## Execution split

```text
Notion Custom Agent / chat / curl  →  Queued task or /api/agents/<key>/run
                                              ↓
                                    One ECS Fargate service
                                              ↓
                                    Writes back to Notion
```

Not five AWS services — five modules behind one ALB.
