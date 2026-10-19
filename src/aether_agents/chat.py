"""Simple chat CLI for talking to Aether agents and Notion.

Usage:
  aether                  # opens chat
  aether chat
  aether "run pipeline for Demo Building Owner"

Type natural commands. Results go to Notion when credentials are set.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Any

from aether_agents.agents import get_agent
from aether_agents.agents.lead_extraction import extract_company_from_payload
from aether_agents.config import Settings, get_settings
from aether_agents.integrations.notion import NotionClient, NotionError, ensure_external_id
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentTask,
    OpportunityContext,
    TaskStatus,
)
from aether_agents.runner import process_queue, process_task
from aether_agents.workflows.opportunity_pipeline import run_opportunity_pipeline

HELP_TEXT = """
Commands you can type:

  help                         Show this help
  status                       Notion connection + queued task count
  queue                        List queued Notion tasks
  process                      Process up to 10 queued Notion tasks now
  companies                    List recent companies from Notion
  ask <company>                Run full opportunity pipeline (local)
  lead <company>               Extract/upsert a lead
  research <company>           Queue research extraction in Notion
  qualify <company>            Queue opportunity qualification
  feasibility <company>        Queue technical feasibility
  funding <company>            Queue funding & partnership match
  task <agent> <company>       Queue any agent in Notion
  run <company>                Queue + run full pipeline for a company
  exit / quit                  Leave chat

Examples:
  ask Demo Building Owner Oy
  lead Acme Oy acme.fi https://acme.fi
  run co_demo-building.fi
  funding Helsinki BMS Pilot
""".strip()

AGENT_ALIASES: dict[str, AgentName] = {
    "lead": AgentName.LEAD_EXTRACTION,
    "extract": AgentName.LEAD_EXTRACTION,
    "research": AgentName.RESEARCH_EXTRACTION,
    "verify": AgentName.EVIDENCE_VERIFICATION,
    "evidence": AgentName.EVIDENCE_VERIFICATION,
    "qualify": AgentName.QUALIFICATION,
    "qualification": AgentName.QUALIFICATION,
    "feasibility": AgentName.TECHNICAL_FEASIBILITY,
    "tech": AgentName.TECHNICAL_FEASIBILITY,
    "funding": AgentName.FUNDING_PARTNERSHIP,
    "fund": AgentName.FUNDING_PARTNERSHIP,
}


@dataclass
class ChatReply:
    text: str
    ok: bool = True


class AetherChat:
    """Conversational front-door for the agent platform."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._notion: NotionClient | None = None

    @property
    def notion_ready(self) -> bool:
        return bool(self.settings.notion_token and self.settings.notion_tasks_data_source_id)

    def notion(self) -> NotionClient:
        if not self.notion_ready:
            raise NotionError(
                "Notion is not configured. Set NOTION_TOKEN and data source IDs in .env"
            )
        if self._notion is None:
            self._notion = NotionClient(self.settings)
        return self._notion

    def close(self) -> None:
        if self._notion is not None:
            self._notion.close()
            self._notion = None

    def handle(self, line: str) -> ChatReply:
        text = line.strip()
        if not text:
            return ChatReply("Say something, or type help.")

        lowered = text.lower()
        if lowered in {"help", "?", "commands"}:
            return ChatReply(HELP_TEXT)
        if lowered in {"exit", "quit", "q"}:
            return ChatReply("Bye.")
        if lowered in {"status", "ping"}:
            return self._cmd_status()
        if lowered in {"queue", "tasks", "queued"}:
            return self._cmd_queue()
        if lowered in {"process", "process queue", "go", "run queue"}:
            return self._cmd_process()
        if lowered in {"companies", "leads"}:
            return self._cmd_companies()

        # Natural phrases → structured commands
        if match := re.match(r"^(?:ask|analyze|analyse|review|pipeline)\s+(.+)$", text, re.I):
            return self._cmd_ask(match.group(1).strip())
        if match := re.match(
            r"^(?:run(?:\s+full)?(?:\s+pipeline)?(?:\s+for)?)\s+(.+)$", text, re.I
        ):
            return self._cmd_run(match.group(1).strip())
        if match := re.match(r"^(?:lead|extract(?:\s+lead)?)\s+(.+)$", text, re.I):
            return self._cmd_lead(match.group(1).strip())
        if match := re.match(
            r"^(research|verify|qualify|feasibility|funding|fund|tech)\s+(.+)$",
            text,
            re.I,
        ):
            alias = match.group(1).lower()
            target = match.group(2).strip()
            agent = AGENT_ALIASES[alias]
            return self._cmd_queue_agent(agent, target)
        if match := re.match(r"^task\s+(\S+)\s+(.+)$", text, re.I):
            alias = match.group(1).lower()
            if alias not in AGENT_ALIASES and alias not in {a.value for a in AgentName}:
                return ChatReply(
                    f"Unknown agent '{alias}'. Try: lead, research, qualify, "
                    "feasibility, funding",
                    ok=False,
                )
            agent = AGENT_ALIASES.get(alias) or AgentName(alias)
            return self._cmd_queue_agent(agent, match.group(2).strip())

        # Friendly fallback
        return ChatReply(
            "I didn't catch that. Try:\n"
            "  ask <company>\n"
            "  lead <company> [domain] [url]\n"
            "  run <company>\n"
            "  queue / process / help"
        )

    def _cmd_status(self) -> ChatReply:
        bits = [
            f"Notion token: {'yes' if self.settings.notion_token else 'no'}",
            f"Tasks data source: {'yes' if self.settings.notion_tasks_data_source_id else 'no'}",
            f"Dry run: {self.settings.agent_dry_run}",
        ]
        if self.notion_ready:
            try:
                queued = self.notion().list_queued_tasks(limit=50)
                bits.append(f"Queued tasks: {len(queued)}")
            except NotionError as exc:
                bits.append(f"Notion error: {exc}")
        else:
            bits.append("Tip: copy .env.example → .env and add NOTION_TOKEN to write to Notion.")
        return ChatReply("\n".join(bits))

    def _cmd_queue(self) -> ChatReply:
        if not self.notion_ready:
            return ChatReply(
                "Notion not configured — nothing to list. "
                "You can still `ask <company>` locally.",
                ok=False,
            )
        try:
            tasks = self.notion().list_queued_tasks(limit=20)
        except NotionError as exc:
            return ChatReply(f"Could not read queue: {exc}", ok=False)
        if not tasks:
            return ChatReply("Queue is empty. Try: run Demo Building Owner Oy")
        lines = ["Queued tasks:"]
        for task in tasks:
            lines.append(f"  • [{task.agent.value}] {task.task} → {task.target_id}")
        return ChatReply("\n".join(lines))

    def _cmd_process(self) -> ChatReply:
        if not self.notion_ready:
            return ChatReply(
                "Notion not configured. Set NOTION_TOKEN first, or use `ask <company>`.",
                ok=False,
            )
        try:
            summary = process_queue(self.settings, batch_limit=self.settings.agent_batch_limit)
        except (NotionError, ValueError) as exc:
            return ChatReply(f"Process failed: {exc}", ok=False)
        return ChatReply(
            "Processed queue.\n"
            f"  processed={summary['processed']} "
            f"succeeded={summary['succeeded']} failed={summary['failed']}"
        )

    def _cmd_companies(self) -> ChatReply:
        if not self.notion_ready or not self.settings.notion_companies_data_source_id:
            return ChatReply("Companies database not configured.", ok=False)
        try:
            pages = self.notion().query_data_source(
                self.settings.notion_companies_data_source_id, page_size=15
            )
        except NotionError as exc:
            return ChatReply(f"Could not list companies: {exc}", ok=False)
        if not pages:
            return ChatReply("No companies yet. Try: lead Acme Oy acme.fi")
        lines = ["Companies:"]
        for page in pages[:15]:
            props = page.get("properties", {})
            name = _prop_plain(props.get("Name")) or "(untitled)"
            external = _prop_plain(props.get("External ID")) or "?"
            stage = _prop_plain(props.get("Stage")) or "?"
            lines.append(f"  • {name} [{external}] — {stage}")
        return ChatReply("\n".join(lines))

    def _cmd_ask(self, company: str) -> ChatReply:
        """Run full pipeline locally and print a short answer."""
        payload = _company_payload(company)
        opportunity_id = str(payload["external_id"])
        outcome = run_opportunity_pipeline(
            opportunity_id=opportunity_id,
            task_payload=payload,
            dry_run=True,
        )
        actions = "\n".join(f"  • {a}" for a in outcome.next_actions[-3:])
        blockers = (
            "\n".join(f"  • {b}" for b in outcome.blockers[:5]) if outcome.blockers else "  • none"
        )
        return ChatReply(
            f"Opportunity: {opportunity_id}\n"
            f"Decision: {outcome.final_decision.value}\n\n"
            f"{outcome.consolidated_summary}\n\n"
            f"Next actions:\n{actions}\n\n"
            f"Blockers / unknowns:\n{blockers}\n\n"
            "Tip: `run <company>` queues this in Notion for the team."
        )

    def _cmd_lead(self, raw: str) -> ChatReply:
        parts = raw.split()
        name_parts: list[str] = []
        domain: str | None = None
        url: str | None = None
        for part in parts:
            if part.startswith("http://") or part.startswith("https://"):
                url = part
            elif "." in part and " " not in part and not part.endswith("."):
                domain = part
            else:
                name_parts.append(part)
        name = " ".join(name_parts).strip() or raw
        payload: dict[str, Any] = {"company_name": name}
        if domain:
            payload["domain"] = domain
        if url:
            payload["source_url"] = url
        company = extract_company_from_payload(payload)

        if self.notion_ready and self.settings.notion_companies_data_source_id:
            try:
                page_id = self.notion().upsert_company(company)
                task_reply = self._cmd_queue_agent(
                    AgentName.LEAD_EXTRACTION,
                    company.external_id,
                    task_title=f"Lead extract {company.name}",
                )
                return ChatReply(
                    f"Saved lead {company.name} ({company.external_id}) "
                    f"to Notion ({page_id[:8]}…).\n{task_reply.text}"
                )
            except NotionError as exc:
                return ChatReply(
                    f"Lead extracted locally as {company.external_id}, "
                    f"but Notion write failed: {exc}",
                    ok=False,
                )

        return ChatReply(
            f"Extracted lead {company.name}\n"
            f"  external_id: {company.external_id}\n"
            f"  domain: {company.domain or '—'}\n"
            "Notion not connected — result kept local. Add NOTION_TOKEN to save."
        )

    def _cmd_run(self, company: str) -> ChatReply:
        """Queue a full-pipeline task in Notion, or run locally if offline."""
        payload = _company_payload(company)
        opportunity_id = str(payload["external_id"])

        if not self.notion_ready:
            ask = self._cmd_ask(company)
            return ChatReply("Notion offline — ran locally instead.\n\n" + ask.text)

        try:
            # Upsert company if we have enough info
            if payload.get("company_name"):
                company_rec = extract_company_from_payload(payload)
                if self.settings.notion_companies_data_source_id:
                    self.notion().upsert_company(company_rec)
            page_id = self._create_task(
                task=f"Chat: Full pipeline {payload.get('company_name', opportunity_id)}",
                agent=AgentName.LEAD_EXTRACTION,
                target_id=opportunity_id,
                extra_props=None,
            )
            # Run immediately so chat feels responsive
            task = AgentTask(
                page_id=page_id,
                task=f"EVAL: Full pipeline {payload.get('company_name', opportunity_id)}",
                agent=AgentName.LEAD_EXTRACTION,
                target_id=opportunity_id,
                status=TaskStatus.QUEUED,
                payload={**payload, "run_full_pipeline": True},
            )
            result = process_task(self.notion(), task, self.settings)
            if result is None:
                return ChatReply(
                    f"Queued in Notion ({page_id[:8]}…) but the run failed. "
                    "Check Failed Agent Runs view.",
                    ok=False,
                )
            return ChatReply(
                f"Queued + ran full pipeline for {opportunity_id}.\n"
                f"Decision: {result.decision.value}\n"
                f"{result.summary}\n"
                f"Next: {result.next_action}\n"
                "See Human Approvals in Notion."
            )
        except NotionError as exc:
            return ChatReply(f"Notion error: {exc}", ok=False)

    def _cmd_queue_agent(
        self,
        agent: AgentName,
        target: str,
        *,
        task_title: str | None = None,
    ) -> ChatReply:
        payload = _company_payload(target)
        target_id = str(payload["external_id"])
        title = task_title or f"Chat: {agent.value} for {payload.get('company_name', target_id)}"

        if not self.notion_ready:
            # Run the single agent locally as a friendly fallback
            task = AgentTask(
                page_id="local",
                task=title,
                agent=agent,
                target_id=target_id,
                payload=payload,
            )
            ctx = OpportunityContext(opportunity_id=target_id)
            if agent != AgentName.LEAD_EXTRACTION and payload.get("company_name"):
                ctx.company = extract_company_from_payload(payload)
            result = get_agent(agent)(AgentInput(task=task, context=ctx, dry_run=True))
            return ChatReply(
                f"Notion offline — ran {agent.value} locally.\n"
                f"Decision: {result.decision.value}\n"
                f"{result.summary}\n"
                f"Next: {result.next_action}"
            )

        try:
            page_id = self._create_task(task=title, agent=agent, target_id=target_id)
            return ChatReply(
                f"Queued in Notion: [{agent.value}] {title}\n"
                f"  target: {target_id}\n"
                f"  task id: {page_id}\n"
                "Type `process` to run queued tasks now, or wait for the schedule."
            )
        except NotionError as exc:
            return ChatReply(f"Could not queue task: {exc}", ok=False)

    def _create_task(
        self,
        *,
        task: str,
        agent: AgentName,
        target_id: str,
        extra_props: dict[str, Any] | None = None,
    ) -> str:
        del extra_props  # reserved for future chat options
        return self.notion().create_queued_task(task=task, agent=agent, target_id=target_id)


def _company_payload(raw: str) -> dict[str, Any]:
    """Parse 'Name', 'Name domain.fi', 'Name domain url', or 'co_external_id'."""
    text = raw.strip().strip("\"'")
    if text.startswith("co_"):
        return {
            "external_id": text,
            "company_name": text.removeprefix("co_").replace(".", " ").title(),
            "domain": text.removeprefix("co_") if "." in text else None,
        }

    parts = text.split()
    domain: str | None = None
    url: str | None = None
    name_parts: list[str] = []
    for part in parts:
        if part.startswith("http://") or part.startswith("https://"):
            url = part
        elif re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", part):
            domain = part.lower()
        else:
            name_parts.append(part)
    name = " ".join(name_parts).strip() or text
    external_id = ensure_external_id(name, domain)
    payload: dict[str, Any] = {
        "company_name": name,
        "external_id": external_id,
    }
    if domain:
        payload["domain"] = domain
        payload["source_url"] = url or f"https://{domain}"
    elif url:
        payload["source_url"] = url
    # Seed a usable research snippet so ask/run works offline
    if payload.get("source_url"):
        payload["sources"] = [
            {
                "url": payload["source_url"],
                "text": (f"{name} building energy / BMS context for an Aether Applied pilot."),
                "confidence": 0.7,
            }
        ]
    return payload


def _prop_plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "select" and prop.get("select"):
        return str(prop["select"].get("name", ""))
    return ""


def run_repl(one_shot: str | None = None) -> int:
    settings = get_settings()
    chat = AetherChat(settings)
    try:
        if one_shot:
            reply = chat.handle(one_shot)
            print(reply.text)
            return 0 if reply.ok else 1

        print("Aether chat — talk to your agents. Type help. Notion-backed when .env is set.\n")
        while True:
            try:
                line = input("aether> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                return 0
            if not line:
                continue
            reply = chat.handle(line)
            print(reply.text)
            print()
            if line.lower() in {"exit", "quit", "q"}:
                return 0
    finally:
        chat.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aether",
        description="Easy chat CLI for Aether agents + Notion",
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="Optional one-shot message (otherwise opens interactive chat)",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    message = " ".join(args.message).strip()
    # Allow `aether chat` as an explicit alias
    if message.lower() == "chat":
        message = ""
    return run_repl(message or None)


if __name__ == "__main__":
    sys.exit(main())
