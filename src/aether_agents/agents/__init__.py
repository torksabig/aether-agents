"""Agent registry — every agent shares the BaseAgent interface."""

from __future__ import annotations

from aether_agents.agents.base import BaseAgent
from aether_agents.agents.evidence_verification import EvidenceVerificationAgent
from aether_agents.agents.funding_partnership import FundingPartnershipAgent
from aether_agents.agents.lead_extraction import LeadExtractionAgent
from aether_agents.agents.qualification import QualificationAgent
from aether_agents.agents.research_extraction import ResearchExtractionAgent
from aether_agents.agents.technical_feasibility import TechnicalFeasibilityAgent
from aether_agents.models import AgentName

AGENT_REGISTRY: dict[AgentName, type[BaseAgent]] = {
    AgentName.LEAD_EXTRACTION: LeadExtractionAgent,
    AgentName.RESEARCH_EXTRACTION: ResearchExtractionAgent,
    AgentName.EVIDENCE_VERIFICATION: EvidenceVerificationAgent,
    AgentName.QUALIFICATION: QualificationAgent,
    AgentName.TECHNICAL_FEASIBILITY: TechnicalFeasibilityAgent,
    AgentName.FUNDING_PARTNERSHIP: FundingPartnershipAgent,
}


def get_agent(name: AgentName) -> BaseAgent:
    try:
        cls = AGENT_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown agent: {name}") from exc
    return cls()


__all__ = [
    "AGENT_REGISTRY",
    "BaseAgent",
    "EvidenceVerificationAgent",
    "FundingPartnershipAgent",
    "LeadExtractionAgent",
    "QualificationAgent",
    "ResearchExtractionAgent",
    "TechnicalFeasibilityAgent",
    "get_agent",
]
