"""Technical feasibility gaps and engineer approval flag."""

from __future__ import annotations

from datetime import UTC, datetime

from aether_agents.agents.technical_feasibility import TechnicalFeasibilityAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    AgentTask,
    Decision,
    Evidence,
    OpportunityContext,
    ProjectRecord,
    TaskStatus,
)


def test_missing_data_access_needs_review() -> None:
    agent = TechnicalFeasibilityAgent()
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Feasibility",
                agent=AgentName.TECHNICAL_FEASIBILITY,
                target_id="co_x",
                status=TaskStatus.QUEUED,
            ),
            context=OpportunityContext(
                opportunity_id="co_x",
                project=ProjectRecord(
                    project="Pilot",
                    company_id="co_x",
                    use_case="building energy BMS",
                    data_access="unconfirmed",
                    technical_status="draft",
                ),
                prior_results=[
                    AgentResult(
                        opportunity_id="co_x",
                        agent=AgentName.QUALIFICATION,
                        decision=Decision.QUALIFIED,
                        summary="Qualified with supported building energy evidence present.",
                        evidence=[
                            Evidence(
                                source_url="https://example.com/energy",  # type: ignore[arg-type]
                                claim="Building energy BMS opportunity documented on site.",
                                retrieved_at=datetime.now(UTC),
                            )
                        ],
                        next_action="Feasibility",
                    )
                ],
            ),
        )
    )
    assert result.decision == Decision.NEEDS_REVIEW
    assert "confirmed_data_access" in result.unknowns
    assert result.metadata["requires_engineer_approval"] is True


def test_denied_data_access_blocks() -> None:
    agent = TechnicalFeasibilityAgent()
    result = agent(
        AgentInput(
            task=AgentTask(
                page_id="p",
                task="Feasibility",
                agent=AgentName.TECHNICAL_FEASIBILITY,
                target_id="co_x",
            ),
            context=OpportunityContext(
                opportunity_id="co_x",
                project=ProjectRecord(
                    project="Pilot",
                    company_id="co_x",
                    use_case="building energy",
                    data_access="denied",
                    technical_status="blocked",
                ),
            ),
        )
    )
    assert result.decision == Decision.BLOCKED
