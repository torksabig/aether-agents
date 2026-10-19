"""Funding & partnership eligibility rules."""

from __future__ import annotations

from aether_agents.agents.funding_partnership import (
    PROGRAMME_CATALOGUE,
    FundingPartnershipAgent,
    evaluate_eligibility,
    match_funding,
)
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentTask,
    EligibilityOutcome,
    OpportunityContext,
    ProjectRecord,
    TaskStatus,
)


def _project(**overrides: object) -> ProjectRecord:
    base: dict[str, object] = {
        "project": "Helsinki BMS Pilot",
        "company_id": "co_demo",
        "use_case": "building energy BMS optimisation",
        "technical_status": "ready_for_pilot",
        "data_access": "confirmed",
        "region": "finland",
        "partners": ["Aalto"],
        "co_financing_capacity": "confirmed",
    }
    base.update(overrides)
    return ProjectRecord.model_validate(base)


def test_missing_eligibility_becomes_unverified() -> None:
    project = _project(region=None, co_financing_capacity="unknown")
    programme = next(p for p in PROGRAMME_CATALOGUE if "Business Finland" in str(p["programme"]))
    outcome, gaps = evaluate_eligibility(project, programme)
    assert outcome == EligibilityOutcome.UNVERIFIED
    assert gaps


def test_expired_grants_excluded_from_open_matches() -> None:
    matches = match_funding(_project())
    names = [m.programme for m in matches]
    assert not any("Expired Demo" in n for n in names)
    assert all(m.status != "Expired" for m in matches)


def test_high_score_does_not_override_not_eligible() -> None:
    # Horizon requires consortium — empty partners → not eligible
    project = _project(partners=[], co_financing_capacity="confirmed")
    horizon = next(p for p in PROGRAMME_CATALOGUE if "Horizon" in str(p["programme"]))
    outcome, gaps = evaluate_eligibility(project, horizon)
    assert outcome == EligibilityOutcome.NOT_ELIGIBLE
    assert "research_or_consortium_partner" in gaps
    matches = match_funding(project)
    horizon_match = next(m for m in matches if "Horizon" in m.programme)
    assert horizon_match.eligibility == EligibilityOutcome.NOT_ELIGIBLE
    assert horizon_match.status == "Not eligible"


def test_no_data_access_blocks_funding_agent() -> None:
    agent = FundingPartnershipAgent()
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Fund",
                agent=AgentName.FUNDING_PARTNERSHIP,
                target_id="co_demo",
                status=TaskStatus.QUEUED,
            ),
            context=OpportunityContext(
                opportunity_id="co_demo",
                project=_project(data_access="unconfirmed"),
            ),
        )
    )
    assert result.decision.value == "blocked"
    assert result.metadata.get("funding_matches") == []


def test_autonomous_outreach_disabled() -> None:
    agent = FundingPartnershipAgent()
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Fund",
                agent=AgentName.FUNDING_PARTNERSHIP,
                target_id="co_demo",
            ),
            context=OpportunityContext(
                opportunity_id="co_demo",
                project=_project(),
            ),
        )
    )
    assert result.metadata["autonomous_outreach_disabled"] is True
