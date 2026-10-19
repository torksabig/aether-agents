"""Evidence verification and qualification rules."""

from __future__ import annotations

from datetime import UTC, datetime

from aether_agents.agents.evidence_verification import (
    EvidenceVerificationAgent,
    classify_evidence,
)
from aether_agents.agents.qualification import QualificationAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    AgentTask,
    CompanyRecord,
    Decision,
    Evidence,
    EvidenceKind,
    OpportunityContext,
    TaskStatus,
    VerificationStatus,
)


def _ev(url: str, claim: str, conf: float = 0.7) -> Evidence:
    return Evidence(
        source_url=url,  # type: ignore[arg-type]
        claim=claim,
        retrieved_at=datetime.now(UTC),
        confidence=conf,
    )


def test_peer_conclusion_not_treated_as_evidence() -> None:
    item = classify_evidence(
        _ev("https://example.com/x", "Agent concluded the site is ready for pilot")
    )
    assert item.verification == VerificationStatus.UNSUPPORTED


def test_official_source_supported() -> None:
    item = classify_evidence(
        _ev(
            "https://www.businessfinland.fi/funding",
            "Energy aid supports building efficiency pilots in Finland.",
        )
    )
    assert item.verification == VerificationStatus.SUPPORTED


def test_conflicting_claims_need_review() -> None:
    agent = EvidenceVerificationAgent()
    ctx = OpportunityContext(
        opportunity_id="co_x",
        prior_results=[
            AgentResult(
                opportunity_id="co_x",
                agent=AgentName.RESEARCH_EXTRACTION,
                decision=Decision.QUALIFIED,
                summary="Research gathered opposing statements about data access rights.",
                evidence=[
                    _ev(
                        "https://example.com/a",
                        "Data access confirmed for the Helsinki campus BMS.",
                    ),
                    _ev(
                        "https://example.com/b",
                        "No data access will be granted for external vendors.",
                    ),
                ],
                next_action="Verify",
            )
        ],
    )
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Verify",
                agent=AgentName.EVIDENCE_VERIFICATION,
                target_id="co_x",
                status=TaskStatus.QUEUED,
            ),
            context=ctx,
        )
    )
    assert result.decision == Decision.NEEDS_REVIEW
    assert result.metadata["conflicting_count"] >= 1


def test_qualification_rejects_without_supported_evidence() -> None:
    agent = QualificationAgent()
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Qualify",
                agent=AgentName.QUALIFICATION,
                target_id="co_x",
            ),
            context=OpportunityContext(
                opportunity_id="co_x",
                company=CompanyRecord(
                    name="X",
                    external_id="co_x",
                    domain="x.fi",
                ),
                verified_evidence=[
                    Evidence(
                        source_url="https://x.fi/a",  # type: ignore[arg-type]
                        claim="Vague marketing statement without energy detail.",
                        retrieved_at=datetime.now(UTC),
                        kind=EvidenceKind.HYPOTHESIS,
                        verification=VerificationStatus.UNSUPPORTED,
                    )
                ],
            ),
        )
    )
    assert result.decision == Decision.NEEDS_REVIEW
    assert "supported_evidence" in result.unknowns or "building_energy_signal" in (result.unknowns)
