"""Evaluation harness smoke test."""

from __future__ import annotations

from aether_agents.eval_runner import load_cases, run_eval


def test_eval_fixtures_present() -> None:
    cases = load_cases()
    assert len(cases) >= 5


def test_eval_scorecard_runs() -> None:
    report = run_eval()
    assert report["total"] >= 5
    assert "categories" in report
    assert all("opportunity_id" in row for row in report["cases"])
