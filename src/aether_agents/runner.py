"""CLI entrypoint for scheduled GitHub Actions runs.

Pending work remains in Notion until successfully processed.
Failed tasks do not prevent unrelated queued tasks from running.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from aether_agents.agents import get_agent
from aether_agents.config import Settings, get_settings
from aether_agents.integrations.notion import NotionClient, NotionError
from aether_agents.models import (
    AgentInput,
    AgentResult,
    AgentTask,
    CompanyRecord,
    OpportunityContext,
    ProjectRecord,
    new_run_id,
)
from aether_agents.workflows.opportunity_pipeline import run_opportunity_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("aether_agents.runner")


def _context_from_task(task: AgentTask) -> OpportunityContext:
    ctx = OpportunityContext(opportunity_id=task.target_id)
    company_raw = task.payload.get("company")
    if isinstance(company_raw, dict):
        ctx.company = CompanyRecord.model_validate(company_raw)
    project_raw = task.payload.get("project")
    if isinstance(project_raw, dict):
        ctx.project = ProjectRecord.model_validate(project_raw)
    return ctx


def process_task(
    notion: NotionClient | None,
    task: AgentTask,
    settings: Settings,
) -> AgentResult | None:
    """Process one task. Errors are isolated per task."""
    run_id = new_run_id()
    logger.info(
        "Processing task page=%s agent=%s target=%s run=%s",
        task.page_id,
        task.agent.value,
        task.target_id,
        run_id,
    )
    try:
        if notion is not None and not settings.agent_dry_run:
            task = notion.mark_task_running(task)
            # Hydrate company/project from Notion so queued tasks need minimal fields
            payload = dict(task.payload)
            if "company" not in payload and "company_name" not in payload:
                company = notion.find_company_by_external_id(task.target_id)
                if company is not None:
                    payload["company"] = company.model_dump(mode="json")
                    payload["company_name"] = company.name
                    payload["domain"] = company.domain
                    if company.source_url:
                        payload["source_url"] = str(company.source_url)
                        payload["sources"] = payload.get("sources") or [
                            {
                                "url": str(company.source_url),
                                "text": (
                                    f"{company.name} building-energy / BMS "
                                    "opportunity retained from Companies & Leads."
                                ),
                                "confidence": 0.7,
                            }
                        ]
            if "project" not in payload:
                project = notion.find_project_by_company_id(task.target_id)
                if project is not None:
                    payload["project"] = project
            task = task.model_copy(update={"payload": payload})

        # Full pipeline when requested in payload or task title starts with EVAL: Full
        run_full = bool(task.payload.get("run_full_pipeline")) or task.task.lower().startswith(
            "eval: full pipeline"
        )
        if run_full:
            outcome = run_opportunity_pipeline(
                opportunity_id=task.target_id,
                task_payload=task.payload,
                token_budget=task.approved_token_budget or settings.agent_token_budget,
                dry_run=settings.agent_dry_run,
            )
            result = outcome.results[-1]
            # Attach consolidated summary for Notion
            result = result.model_copy(
                update={
                    "summary": outcome.consolidated_summary[:500],
                    "metadata": {
                        **result.metadata,
                        "pipeline_run_id": outcome.run_id,
                        "final_decision": outcome.final_decision.value,
                        "next_actions": outcome.next_actions,
                    },
                }
            )
        else:
            agent = get_agent(task.agent)
            result = agent(
                AgentInput(
                    task=task,
                    context=_context_from_task(task),
                    token_budget=task.approved_token_budget or settings.agent_token_budget,
                    dry_run=settings.agent_dry_run,
                )
            )

        if notion is not None and not settings.agent_dry_run:
            company_data = result.metadata.get("company")
            if company_data and settings.notion_companies_data_source_id:
                company = CompanyRecord.model_validate(company_data)
                notion.upsert_company(company)

            funding = result.metadata.get("funding_matches")
            if funding and settings.notion_funding_data_source_id:
                from aether_agents.models import FundingMatch

                project_name = task.payload.get("project_name") or task.target_id
                for item in funding:
                    match = FundingMatch.model_validate(item)
                    if match.status != "Expired":
                        notion.write_funding_match(match, project_name=str(project_name))

            notion.mark_task_result(task, result)

        logger.info(
            "Completed task page=%s decision=%s run=%s",
            task.page_id,
            result.decision.value,
            result.run_id,
        )
        return result
    except Exception as exc:  # noqa: BLE001 — isolate per-task failures
        logger.exception("Task failed page=%s: %s", task.page_id, exc)
        if notion is not None and not settings.agent_dry_run:
            try:
                notion.mark_task_failed(task, f"{type(exc).__name__}: {exc}")
            except NotionError:
                logger.exception("Failed to mark task as failed in Notion")
        return None


def process_queue(settings: Settings, *, batch_limit: int) -> dict[str, Any]:
    dry = settings.agent_dry_run or not settings.notion_token
    if dry:
        logger.warning(
            "Running without Notion writes (dry_run=%s, token_set=%s)",
            settings.agent_dry_run,
            bool(settings.notion_token),
        )
        return {"processed": 0, "succeeded": 0, "failed": 0, "dry_run": True}

    settings.require_notion()
    succeeded = 0
    failed = 0
    with NotionClient(settings) as notion:
        tasks = notion.list_queued_tasks(limit=batch_limit)
        logger.info("Fetched %s queued task(s)", len(tasks))
        for task in tasks:
            result = process_task(notion, task, settings)
            if result is None:
                failed += 1
            else:
                succeeded += 1
    return {
        "processed": succeeded + failed,
        "succeeded": succeeded,
        "failed": failed,
        "dry_run": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aether agent pipeline runner")
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=None,
        help="Max queued tasks to process (default: AGENT_BATCH_LIMIT)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Notion writes",
    )
    parser.add_argument(
        "--demo-pipeline",
        metavar="OPPORTUNITY_ID",
        help="Run local end-to-end pipeline demo without Notion",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.dry_run:
        settings = settings.model_copy(update={"agent_dry_run": True})

    if args.demo_pipeline:
        outcome = run_opportunity_pipeline(
            opportunity_id=args.demo_pipeline,
            task_payload={
                "company_name": "Demo Building Owner Oy",
                "domain": "demo-building.fi",
                "source_url": "https://demo-building.fi/about",
                "contacts": ["energy.manager@demo-building.fi"],
                "sources": [
                    {
                        "url": "https://demo-building.fi/sustainability",
                        "text": (
                            "Campus BMS covers HVAC and district heating meters "
                            "across three Helsinki buildings."
                        ),
                        "confidence": 0.75,
                    }
                ],
                "project": {
                    "project": "Helsinki BMS Pilot",
                    "company_id": args.demo_pipeline,
                    "use_case": "building energy BMS optimisation",
                    "technical_status": "ready_for_pilot",
                    "data_access": "confirmed",
                    "region": "finland",
                    "maturity": "pilot",
                    "partners": ["Aalto Energy Lab"],
                    "co_financing_capacity": "confirmed",
                },
            },
            token_budget=settings.agent_token_budget,
            dry_run=True,
        )
        print(outcome.model_dump_json(indent=2))
        return 0

    batch = args.batch_limit or settings.agent_batch_limit
    summary = process_queue(settings, batch_limit=batch)
    logger.info("Run summary: %s", summary)
    # Non-zero only if all tasks failed and some were attempted
    if summary["processed"] and summary["succeeded"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
