"""Technical Feasibility — gaps, prerequisites, risks. Engineer must approve."""

from __future__ import annotations

from typing import ClassVar

from aether_agents.agents.base import BaseAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    Decision,
    ProjectRecord,
)


def assess_feasibility(project: ProjectRecord | None) -> tuple[Decision, list[str], list[str], str]:
    """Determine whether a pilot is practically deliverable."""
    gaps: list[str] = []
    risks: list[str] = []

    if project is None:
        return (
            Decision.NEEDS_REVIEW,
            ["project_scope", "data_access", "pilot_site"],
            ["Unknown technical scope"],
            (
                "No project record available. Engineer must define use case, "
                "data access, and pilot site before feasibility can be assessed."
            ),
        )

    if project.data_access != "confirmed":
        gaps.append("confirmed_data_access")
        risks.append("Pilot blocked without BMS/meter data access agreement")

    if not project.use_case or len(project.use_case) < 5:
        gaps.append("use_case")

    if project.technical_status.lower() in {"unknown", "", "blocked"}:
        gaps.append("technical_status")

    if project.co_financing_capacity in {"none", "unknown"}:
        risks.append("Co-financing capacity not confirmed")

    if project.data_access == "denied":
        return (
            Decision.BLOCKED,
            gaps,
            risks,
            (
                f"Project '{project.project}' cannot proceed: data access denied. "
                "Aether cannot deliver BMS value without operational data."
            ),
        )

    if gaps:
        return (
            Decision.NEEDS_REVIEW,
            gaps,
            risks,
            (
                f"Feasibility gaps for '{project.project}': {', '.join(gaps)}. "
                "Engineer approval required before funding outreach."
            ),
        )

    return (
        Decision.QUALIFIED,
        [],
        risks,
        (
            f"Project '{project.project}' appears technically feasible for a "
            f"{project.use_case} pilot with confirmed data access. "
            "Engineer must still approve before applications."
        ),
    )


class TechnicalFeasibilityAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.TECHNICAL_FEASIBILITY

    def run(self, payload: AgentInput) -> AgentResult:
        project = payload.context.project
        if project is None and payload.task.payload.get("project"):
            raw = payload.task.payload["project"]
            if isinstance(raw, dict):
                project = ProjectRecord.model_validate(raw)

        decision, gaps, risks, summary = assess_feasibility(project)

        evidence = []
        for prior in payload.context.prior_results:
            if prior.agent in {
                AgentName.EVIDENCE_VERIFICATION,
                AgentName.QUALIFICATION,
            }:
                evidence.extend(prior.evidence)

        # Deduplicate by claim+url
        seen: set[str] = set()
        unique = []
        for item in evidence:
            key = f"{item.source_url}|{item.claim[:40]}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        if decision == Decision.QUALIFIED and not unique:
            decision = Decision.NEEDS_REVIEW
            gaps = list({*gaps, "supporting_evidence"})
            summary = (
                "Technical feasibility cannot be marked qualified without "
                "retained source evidence from earlier pipeline stages."
            )

        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=decision,
            summary=summary,
            evidence=unique,
            unknowns=gaps,
            next_action=(
                "Human engineer approval, then funding & partnership match."
                if decision != Decision.BLOCKED
                else "Stop pipeline until data access is restored."
            ),
            metadata={
                "gaps": gaps,
                "risks": risks,
                "project": project.model_dump(mode="json") if project else None,
                "requires_engineer_approval": True,
            },
        )
