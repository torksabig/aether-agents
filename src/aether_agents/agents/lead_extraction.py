"""Lead Extraction agent.

Wraps existing lead-extraction logic behind the shared Agent interface.
V1 ships a deterministic extractor for company identity from payload/sources
so the pipeline is testable without rewriting prior scripts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

from aether_agents.agents.base import BaseAgent
from aether_agents.integrations.notion import ensure_external_id
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    CompanyRecord,
    Decision,
    Evidence,
    EvidenceKind,
)
from aether_agents.plugins import resolve_lead_extractor


def extract_company_from_payload(payload: dict[str, Any]) -> CompanyRecord:
    """Pure lead extraction — no Notion/LLM. Reuse point for legacy scripts."""
    name = str(payload.get("company_name") or payload.get("name") or "").strip()
    if not name:
        raise ValueError("company_name is required for lead extraction")

    domain = payload.get("domain")
    source = payload.get("source_url") or payload.get("url")
    if isinstance(domain, str) and domain and "://" not in domain:
        domain = domain.lower().removeprefix("www.")
    elif source and not domain:
        host = urlparse(str(source)).netloc.lower().removeprefix("www.")
        domain = host or None

    external_id = str(payload.get("external_id") or ensure_external_id(name, domain))
    contacts = payload.get("contacts") or []
    if isinstance(contacts, str):
        contacts = [c.strip() for c in contacts.split(",") if c.strip()]

    return CompanyRecord(
        name=name,
        domain=domain if isinstance(domain, str) else None,
        external_id=external_id,
        stage=str(payload.get("stage") or "New"),
        owner=payload.get("owner"),
        source_url=source,
        last_updated=datetime.now(UTC),
        contacts=[str(c) for c in contacts],
    )


class LeadExtractionAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.LEAD_EXTRACTION

    def run(self, payload: AgentInput) -> AgentResult:
        raw = {
            **payload.task.payload,
            "external_id": payload.task.target_id
            if payload.task.target_id.startswith("co_")
            else payload.task.payload.get("external_id"),
        }
        # Prefer structured payload fields; fall back to task title as company name
        if not raw.get("company_name") and not raw.get("name"):
            raw["company_name"] = payload.task.task

        try:
            external = resolve_lead_extractor()
            company = external(raw) if external is not None else extract_company_from_payload(raw)
        except ValueError as exc:
            return AgentResult(
                opportunity_id=payload.context.opportunity_id,
                agent=self.name,
                decision=Decision.NEEDS_REVIEW,
                summary=(
                    "Lead extraction could not resolve a stable company identity. "
                    f"Missing required fields: {exc}."
                ),
                unknowns=["company_name", "source_url"],
                next_action="Provide company name and at least one source URL.",
                metadata={"error_category": "missing_data"},
            )

        evidence: list[Evidence] = []
        if company.source_url:
            evidence.append(
                Evidence(
                    source_url=company.source_url,
                    claim=f"Organization identified as {company.name}",
                    retrieved_at=datetime.now(UTC),
                    kind=EvidenceKind.FACT,
                    confidence=0.8 if company.domain else 0.55,
                )
            )

        uncertain = not company.domain or not company.contacts
        decision = Decision.NEEDS_REVIEW if uncertain else Decision.QUALIFIED
        unknowns: list[str] = []
        if not company.domain:
            unknowns.append("domain")
        if not company.contacts:
            unknowns.append("contacts")

        # Attach company onto context copy is caller's responsibility;
        # we return it via metadata for the pipeline to merge.
        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=decision,
            summary=(
                f"Extracted lead {company.name}"
                + (f" ({company.domain})" if company.domain else "")
                + f" with external_id={company.external_id}."
            ),
            evidence=evidence,
            unknowns=unknowns,
            next_action=(
                "Confirm company identity and contacts before research."
                if uncertain
                else "Proceed to research extraction."
            ),
            metadata={"company": company.model_dump(mode="json")},
        )
