"""End-to-end opportunity pipeline with source retention.

Sequence: Extract → Verify → Qualify → Assess Feasibility → Match Funding
→ Human Review → Next Action

Agents never treat another agent's unsupported conclusion as verified evidence.
"""

from __future__ import annotations

from aether_agents.agents import get_agent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    AgentTask,
    CompanyRecord,
    Decision,
    OpportunityContext,
    PipelineOutcome,
    ProjectRecord,
    ResearchFinding,
    TaskStatus,
    new_run_id,
)

PIPELINE_ORDER: list[AgentName] = [
    AgentName.LEAD_EXTRACTION,
    AgentName.RESEARCH_EXTRACTION,
    AgentName.EVIDENCE_VERIFICATION,
    AgentName.QUALIFICATION,
    AgentName.TECHNICAL_FEASIBILITY,
    AgentName.FUNDING_PARTNERSHIP,
]


def _merge_context(context: OpportunityContext, result: AgentResult) -> None:
    """Retain sources and merge structured side-effects. Never mutate peer results."""
    context.retain_sources_from_result(result)

    company_data = result.metadata.get("company")
    if company_data and context.company is None:
        context.company = CompanyRecord.model_validate(company_data)

    findings = result.metadata.get("findings")
    if findings:
        context.research.extend(ResearchFinding.model_validate(f) for f in findings)

    if result.agent == AgentName.EVIDENCE_VERIFICATION:
        context.verified_evidence = list(result.evidence)

    project_data = result.metadata.get("project")
    if project_data and context.project is None:
        context.project = ProjectRecord.model_validate(project_data)

    funding = result.metadata.get("funding_matches")
    if funding:
        from aether_agents.models import FundingMatch

        context.funding_matches = [FundingMatch.model_validate(m) for m in funding]


def _consolidate(results: list[AgentResult], opportunity_id: str, run_id: str) -> PipelineOutcome:
    blockers: list[str] = []
    next_actions: list[str] = []
    final = Decision.QUALIFIED

    for result in results:
        next_actions.append(f"[{result.agent.value}] {result.next_action}")
        blockers.extend(result.unknowns)
        if result.decision == Decision.BLOCKED:
            final = Decision.BLOCKED
        elif result.decision == Decision.NEEDS_REVIEW and final != Decision.BLOCKED:
            final = Decision.NEEDS_REVIEW

    # Build one consolidated answer set (not six separate AI reports)
    worth = "yes" if final == Decision.QUALIFIED else "not yet / needs review"
    if final == Decision.BLOCKED:
        worth = "no — blocked"

    deliverable = next(
        (r for r in results if r.agent == AgentName.TECHNICAL_FEASIBILITY),
        None,
    )
    funding = next(
        (r for r in results if r.agent == AgentName.FUNDING_PARTNERSHIP),
        None,
    )

    summary = (
        f"Opportunity {opportunity_id}: worth pursuing? {worth}. "
        f"Delivery readiness: "
        f"{deliverable.summary if deliverable else 'feasibility not run'}. "
        f"Funding: {funding.summary if funding else 'funding not run'}. "
        f"Sources retained across {len(results)} agent step(s)."
    )

    return PipelineOutcome(
        opportunity_id=opportunity_id,
        run_id=run_id,
        results=results,
        final_decision=final,
        consolidated_summary=summary,
        next_actions=next_actions,
        blockers=list(dict.fromkeys(blockers)),
    )


def run_opportunity_pipeline(
    *,
    opportunity_id: str,
    task_payload: dict[str, object] | None = None,
    context: OpportunityContext | None = None,
    stop_on_blocked: bool = True,
    token_budget: int = 8000,
    dry_run: bool = False,
    agents: list[AgentName] | None = None,
) -> PipelineOutcome:
    """Run the defined agent sequence for one opportunity."""
    run_id = new_run_id()
    ctx = context or OpportunityContext(opportunity_id=opportunity_id)
    payload_data = dict(task_payload or {})
    results: list[AgentResult] = []
    sequence = agents or PIPELINE_ORDER

    for agent_name in sequence:
        task = AgentTask(
            page_id=f"local-{agent_name.value}",
            task=f"{agent_name.value} for {opportunity_id}",
            agent=agent_name,
            target_id=opportunity_id,
            status=TaskStatus.QUEUED,
            payload=payload_data,
        )
        agent = get_agent(agent_name)
        result = agent(
            AgentInput(
                task=task,
                context=ctx,
                token_budget=token_budget,
                dry_run=dry_run,
            )
        )
        # Freeze prior results: store copies; agents must not edit peers
        results.append(result.model_copy(deep=True))
        _merge_context(ctx, result)
        if stop_on_blocked and result.decision == Decision.BLOCKED:
            break

    return _consolidate(results, opportunity_id, run_id)


def run_single_task(
    task: AgentTask,
    *,
    context: OpportunityContext | None = None,
    token_budget: int = 8000,
    dry_run: bool = False,
) -> AgentResult:
    """Execute one queued Notion task without advancing the full pipeline."""
    ctx = context or OpportunityContext(opportunity_id=task.target_id)
    agent = get_agent(task.agent)
    return agent(
        AgentInput(
            task=task,
            context=ctx,
            token_budget=token_budget,
            dry_run=dry_run,
        )
    )
