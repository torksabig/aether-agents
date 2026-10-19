"""End-to-end pipeline and isolation behaviour."""

from __future__ import annotations

import json
from pathlib import Path

from aether_agents.models import AgentName, AgentTask, Decision, TaskStatus
from aether_agents.workflows.opportunity_pipeline import (
    PIPELINE_ORDER,
    run_opportunity_pipeline,
    run_single_task,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_opportunity.json"


def test_full_pipeline_retains_sources() -> None:
    data = json.loads(FIXTURE.read_text())
    outcome = run_opportunity_pipeline(
        opportunity_id=data["opportunity_id"],
        task_payload=data,
        dry_run=True,
    )
    assert len(outcome.results) == len(PIPELINE_ORDER)
    assert outcome.consolidated_summary
    # Every completed step that produced evidence links to sources
    sourced = [r for r in outcome.results if r.evidence]
    assert sourced
    for result in sourced:
        for ev in result.evidence:
            assert str(ev.source_url).startswith("http")


def test_failed_agent_does_not_block_unrelated_tasks() -> None:
    """Simulate one bad task then a good one via run_single_task isolation."""
    bad = AgentTask(
        page_id="bad",
        task="",
        agent=AgentName.LEAD_EXTRACTION,
        target_id="co_bad",
        status=TaskStatus.QUEUED,
        payload={},  # missing company — needs_review, not exception
    )
    good = AgentTask(
        page_id="good",
        task="Good Co",
        agent=AgentName.LEAD_EXTRACTION,
        target_id="co_good.fi",
        status=TaskStatus.QUEUED,
        payload={
            "company_name": "Good Co",
            "domain": "good.fi",
            "source_url": "https://good.fi",
            "contacts": ["a@good.fi"],
        },
    )
    r1 = run_single_task(bad)
    r2 = run_single_task(good)
    assert r1.decision == Decision.NEEDS_REVIEW
    assert r2.decision in {Decision.QUALIFIED, Decision.NEEDS_REVIEW}
    assert r2.metadata["company"]["external_id"] == "co_good.fi"


def test_pipeline_answers_core_questions() -> None:
    data = json.loads(FIXTURE.read_text())
    outcome = run_opportunity_pipeline(
        opportunity_id=data["opportunity_id"],
        task_payload=data,
    )
    text = outcome.consolidated_summary.lower()
    assert "worth pursuing" in text
    assert outcome.next_actions
    assert outcome.final_decision in {
        Decision.QUALIFIED,
        Decision.NEEDS_REVIEW,
        Decision.BLOCKED,
    }
