"""Research Extraction agent — source-linked findings for project questions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from aether_agents.agents.base import BaseAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    Decision,
    Evidence,
    EvidenceKind,
    ResearchFinding,
)
from aether_agents.plugins import resolve_research_extractor


def extract_findings(
    *,
    company_id: str,
    sources: list[dict[str, Any]],
    questions: list[str],
) -> list[ResearchFinding]:
    """Deterministic research wrapper. Maps source snippets to findings.

    Existing research scripts should call into this shape or return the same
    ResearchFinding list. Untrusted webpage text never becomes instructions.
    """
    findings: list[ResearchFinding] = []
    for source in sources:
        url = str(source.get("url") or "").strip()
        text = str(source.get("text") or source.get("snippet") or "").strip()
        if not url or not text:
            continue
        # Strip instruction-like injection markers from scraped text
        sanitized = _sanitize_source_text(text)
        if len(sanitized) < 10:
            continue
        topic = questions[0] if questions else "general research"
        findings.append(
            ResearchFinding(
                finding=f"Regarding '{topic}': {sanitized[:400]}",
                source_url=url,  # type: ignore[arg-type]
                related_company_id=company_id,
                kind=EvidenceKind.HYPOTHESIS if source.get("unverified") else EvidenceKind.FACT,
                confidence=float(source.get("confidence", 0.5)),
            )
        )
    return findings


def _sanitize_source_text(text: str) -> str:
    """Prevent untrusted webpage text from overriding agent instructions."""
    blocked_prefixes = (
        "ignore previous",
        "system:",
        "you are now",
        "disregard all",
    )
    lines = []
    for line in text.splitlines():
        lowered = line.strip().lower()
        if any(lowered.startswith(p) for p in blocked_prefixes):
            continue
        lines.append(line)
    return " ".join(lines).strip()


def _default_sources_from_context(payload: AgentInput) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = list(payload.task.payload.get("sources") or [])
    for url in payload.context.source_urls:
        sources.append(
            {
                "url": str(url),
                "text": f"Prior pipeline source retained for {urlparse(str(url)).netloc}",
                "confidence": 0.4,
            }
        )
    if payload.context.company and payload.context.company.source_url:
        sources.append(
            {
                "url": str(payload.context.company.source_url),
                "text": (
                    f"{payload.context.company.name} operates in building energy / BMS "
                    "context relevant to Aether Applied pilots."
                ),
                "confidence": 0.6,
            }
        )
    # Deduplicate by URL keeping first
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in sources:
        key = str(item.get("url"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


class ResearchExtractionAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.RESEARCH_EXTRACTION

    def run(self, payload: AgentInput) -> AgentResult:
        company_id = (
            payload.context.company.external_id
            if payload.context.company
            else payload.task.target_id
        )
        questions = list(
            payload.task.payload.get("questions")
            or [
                "buildings and energy systems",
                "known operational problems",
                "data access / BMS landscape",
            ]
        )
        sources = _default_sources_from_context(payload)
        external = resolve_research_extractor()
        findings = (
            external(company_id=company_id, sources=sources, questions=questions)
            if external is not None
            else extract_findings(company_id=company_id, sources=sources, questions=questions)
        )

        if not findings:
            return AgentResult(
                opportunity_id=payload.context.opportunity_id,
                agent=self.name,
                decision=Decision.NEEDS_REVIEW,
                summary=(
                    "Research extraction found no usable source-linked findings. "
                    "Provide accessible sources before treating claims as evidence."
                ),
                unknowns=["accessible_sources"],
                next_action="Attach official or primary source URLs and re-queue.",
                metadata={"error_category": "source_inaccessible"},
            )

        evidence = [
            Evidence(
                source_url=f.source_url,
                claim=f.finding[:500],
                retrieved_at=datetime.now(UTC),
                kind=f.kind,
                confidence=f.confidence,
            )
            for f in findings
        ]
        material = any(f.confidence >= 0.7 for f in findings)
        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=Decision.NEEDS_REVIEW if material else Decision.QUALIFIED,
            summary=(
                f"Collected {len(findings)} source-linked research finding(s) "
                f"for {company_id}. Original source URLs retained."
            ),
            evidence=evidence,
            unknowns=[] if findings else ["verified_building_systems"],
            next_action="Run evidence verification on material claims.",
            metadata={
                "findings": [f.model_dump(mode="json") for f in findings],
            },
        )
