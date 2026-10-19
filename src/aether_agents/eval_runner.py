"""Fixed evaluation set for pre-production review.

Runs historical-style Aether BMS cases locally (no Notion required) and
prints a compact scorecard by error category.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aether_agents.models import Decision
from aether_agents.workflows.opportunity_pipeline import run_opportunity_pipeline

EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval"


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(EVAL_DIR.glob("*.json")):
        cases.append(json.loads(path.read_text(encoding="utf-8")))
    return cases


def run_eval() -> dict[str, Any]:
    cases = load_cases()
    if not cases:
        raise SystemExit(f"No eval fixtures found in {EVAL_DIR}")

    rows: list[dict[str, Any]] = []
    categories: dict[str, int] = {
        "qualified": 0,
        "needs_review": 0,
        "blocked": 0,
        "missing_sources": 0,
        "pipeline_incomplete": 0,
    }

    for case in cases:
        outcome = run_opportunity_pipeline(
            opportunity_id=str(case["opportunity_id"]),
            task_payload=case,
            dry_run=True,
        )
        sourced = any(r.evidence for r in outcome.results)
        incomplete = len(outcome.results) < 6 and outcome.final_decision != Decision.BLOCKED
        categories[outcome.final_decision.value] += 1
        if not sourced:
            categories["missing_sources"] += 1
        if incomplete:
            categories["pipeline_incomplete"] += 1
        rows.append(
            {
                "opportunity_id": outcome.opportunity_id,
                "label": case.get("label"),
                "final_decision": outcome.final_decision.value,
                "steps": len(outcome.results),
                "has_sources": sourced,
                "next_actions": outcome.next_actions[-2:],
                "summary": outcome.consolidated_summary[:240],
            }
        )

    return {"total": len(rows), "categories": categories, "cases": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Aether fixed evaluation set")
    parser.add_argument("--json", action="store_true", help="Print full JSON scorecard")
    args = parser.parse_args(argv)
    report = run_eval()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Evaluated {report['total']} cases")
        for key, value in report["categories"].items():
            print(f"  {key}: {value}")
        for row in report["cases"]:
            print(
                f"- {row['opportunity_id']}: {row['final_decision']} "
                f"(steps={row['steps']}, sources={row['has_sources']})"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
