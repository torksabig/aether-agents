"""Schema and validation rules."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from aether_agents.models import (
    AgentName,
    AgentResult,
    Decision,
    EligibilityOutcome,
    Evidence,
    FundingMatch,
)


def test_qualified_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="supporting evidence"):
        AgentResult(
            opportunity_id="co_test",
            agent=AgentName.QUALIFICATION,
            decision=Decision.QUALIFIED,
            summary="This would be a qualified opportunity without sources.",
            evidence=[],
            next_action="Ship it",
        )


def test_needs_review_allows_empty_evidence() -> None:
    result = AgentResult(
        opportunity_id="co_test",
        agent=AgentName.QUALIFICATION,
        decision=Decision.NEEDS_REVIEW,
        summary="Missing fields prevent a qualified recommendation today.",
        evidence=[],
        unknowns=["contacts"],
        next_action="Collect contacts",
    )
    assert result.decision == Decision.NEEDS_REVIEW


def test_eligibility_high_score_cannot_override_not_eligible() -> None:
    match = FundingMatch(
        programme="Test Programme",
        organization="Org",
        region="EU",
        deadline=date(2099, 1, 1),
        eligibility=EligibilityOutcome.NOT_ELIGIBLE,
        project_match_score=0.99,
        source_url="https://ec.europa.eu/example",
        recommended_next_step="Do not apply",
        fit_assessment="High theme overlap but geography fails.",
    )
    assert match.status == "Not eligible"


def test_expired_grant_marked_expired() -> None:
    match = FundingMatch(
        programme="Old Grant",
        organization="Org",
        region="EU",
        deadline=date(2020, 1, 1),
        eligibility=EligibilityOutcome.ELIGIBLE,
        project_match_score=0.9,
        source_url="https://ec.europa.eu/example",
        recommended_next_step="Skip expired grant",
        fit_assessment="Would have been a strong thematic match.",
    )
    assert match.status == "Expired"


def test_evidence_min_claim_length() -> None:
    with pytest.raises(ValidationError):
        Evidence(
            source_url="https://example.com/a",
            claim="too short",
            retrieved_at=datetime.now(UTC),
        )
