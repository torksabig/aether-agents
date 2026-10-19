"""Opportunity Qualification — fit assessment and discovery questions."""

from __future__ import annotations

from typing import ClassVar

from aether_agents.agents.base import BaseAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    Decision,
    Evidence,
    VerificationStatus,
)

DISCOVERY_QUESTIONS = [
    "Who owns building energy / BMS decisions?",
    "Is there a defined pilot building or campus?",
    "What data can be shared (meters, BMS points, EMS)?",
    "Is there budget or a funding path for a pilot?",
    "What success metrics would justify expansion?",
]


def assess_fit(payload: AgentInput) -> tuple[Decision, list[str], str, list[str]]:
    """Pure qualification logic over verified research + lead."""
    missing: list[str] = []
    company = payload.context.company
    if company is None:
        missing.append("company_record")
    supported = [
        e
        for e in payload.context.verified_evidence
        if e.verification == VerificationStatus.SUPPORTED
    ]
    # Also accept evidence from the latest verification result
    for prior in reversed(payload.context.prior_results):
        if prior.agent == AgentName.EVIDENCE_VERIFICATION:
            supported = [
                e for e in prior.evidence if e.verification == VerificationStatus.SUPPORTED
            ]
            break

    if not supported:
        missing.append("supported_evidence")

    energy_signals = 0
    keywords = ("energy", "bms", "building", "hvac", "meter", "efficiency")
    for item in supported:
        if any(k in item.claim.lower() for k in keywords):
            energy_signals += 1

    if energy_signals == 0:
        missing.append("building_energy_signal")

    if missing:
        return (
            Decision.NEEDS_REVIEW,
            missing,
            (
                "Opportunity is not ready for sales prioritization. "
                f"Missing: {', '.join(missing)}."
            ),
            DISCOVERY_QUESTIONS,
        )

    return (
        Decision.QUALIFIED,
        [],
        (
            f"Lead {company.name if company else payload.context.opportunity_id} "
            f"shows {energy_signals} building-energy signal(s) with supported evidence. "
            "Ready for technical feasibility review."
        ),
        DISCOVERY_QUESTIONS[:2],
    )


class QualificationAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.QUALIFICATION

    def run(self, payload: AgentInput) -> AgentResult:
        decision, missing, summary, questions = assess_fit(payload)

        evidence: list[Evidence] = []
        for prior in payload.context.prior_results:
            if prior.agent == AgentName.EVIDENCE_VERIFICATION:
                evidence = [
                    e for e in prior.evidence if e.verification == VerificationStatus.SUPPORTED
                ]
                break
        if not evidence:
            evidence = [
                e
                for e in payload.context.verified_evidence
                if e.verification == VerificationStatus.SUPPORTED
            ]

        # Qualified path requires evidence (enforced by AgentResult validator)
        if decision == Decision.QUALIFIED and not evidence:
            decision = Decision.NEEDS_REVIEW
            missing = list({*missing, "supported_evidence"})
            summary = (
                "Qualification withheld: no retained source evidence survived "
                "verification. Do not treat peer conclusions as verified."
            )

        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=decision,
            summary=summary if len(summary) >= 20 else summary + " Review required.",
            evidence=evidence,
            unknowns=missing + questions,
            next_action=(
                "Engineer technical feasibility review."
                if decision == Decision.QUALIFIED
                else "Answer discovery questions and gather missing evidence."
            ),
            metadata={"discovery_questions": questions, "missing_fields": missing},
        )
