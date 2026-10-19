"""Evidence Verification — supported / unsupported / conflicting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlparse

from aether_agents.agents.base import BaseAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    Decision,
    Evidence,
    EvidenceKind,
    VerificationStatus,
)

TRUSTED_HOST_HINTS = (
    "europa.eu",
    "businessfinland.fi",
    "gov",
    "wikipedia.org",
    "linkedin.com",
)

# First-party / prospect domains with usable excerpts count as provisional support
# when confidence is high — still distinct from official programme sources.
PROVISIONAL_CONFIDENCE = 0.7


def classify_evidence(item: Evidence) -> Evidence:
    """Rule-based verification. Never treats peer conclusions as primary evidence."""
    host = urlparse(str(item.source_url)).netloc.lower()
    if not host:
        return item.model_copy(update={"verification": VerificationStatus.UNSUPPORTED})

    trusted = any(hint in host for hint in TRUSTED_HOST_HINTS)
    claim_lower = item.claim.lower()

    # Peer agent conclusions without a real source host are unsupported
    if "agent concluded" in claim_lower or "according to our model" in claim_lower:
        return item.model_copy(
            update={
                "verification": VerificationStatus.UNSUPPORTED,
                "confidence": min(item.confidence, 0.2),
            }
        )

    if item.kind == EvidenceKind.HYPOTHESIS and not trusted:
        return item.model_copy(update={"verification": VerificationStatus.UNVERIFIED})

    if trusted and item.confidence >= 0.5:
        return item.model_copy(update={"verification": VerificationStatus.SUPPORTED})

    # High-confidence primary source excerpts (company site) are provisionally supported
    if item.confidence >= PROVISIONAL_CONFIDENCE and item.kind == EvidenceKind.FACT:
        return item.model_copy(update={"verification": VerificationStatus.SUPPORTED})

    if item.confidence < 0.3:
        return item.model_copy(update={"verification": VerificationStatus.UNSUPPORTED})

    return item.model_copy(update={"verification": VerificationStatus.UNVERIFIED})


def detect_conflicts(items: list[Evidence]) -> list[Evidence]:
    """Mark pairs that assert opposite polarity on the same host/topic."""
    negatives = ("no data access", "not eligible", "cancelled", "denied")
    positives = ("data access confirmed", "eligible", "awarded", "approved")
    has_neg = any(any(n in e.claim.lower() for n in negatives) for e in items)
    has_pos = any(any(p in e.claim.lower() for p in positives) for e in items)
    if has_neg and has_pos:
        return [
            e.model_copy(update={"verification": VerificationStatus.CONFLICTING}) for e in items
        ]
    return items


class EvidenceVerificationAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.EVIDENCE_VERIFICATION

    def run(self, payload: AgentInput) -> AgentResult:
        # Only verify original evidence — never promote unsupported peer conclusions
        candidates = list(payload.context.verified_evidence)
        for prior in payload.context.prior_results:
            if prior.agent == AgentName.EVIDENCE_VERIFICATION:
                continue
            candidates.extend(prior.evidence)

        if not candidates:
            return AgentResult(
                opportunity_id=payload.context.opportunity_id,
                agent=self.name,
                decision=Decision.NEEDS_REVIEW,
                summary=(
                    "No source-linked claims available to verify. "
                    "Upstream extraction must retain original URLs."
                ),
                unknowns=["claims_with_sources"],
                next_action="Re-run research extraction with accessible sources.",
            )

        verified = [classify_evidence(e) for e in candidates]
        verified = detect_conflicts(verified)

        supported = [e for e in verified if e.verification == VerificationStatus.SUPPORTED]
        conflicting = [e for e in verified if e.verification == VerificationStatus.CONFLICTING]
        unsupported = [e for e in verified if e.verification == VerificationStatus.UNSUPPORTED]

        if conflicting:
            decision = Decision.NEEDS_REVIEW
            next_action = "Human review required for high-impact contradictions."
        elif not supported:
            decision = Decision.BLOCKED
            next_action = "Gather primary sources before qualification."
        else:
            decision = Decision.QUALIFIED
            next_action = "Proceed to opportunity qualification."

        # Stamp retrieval time for audit trail
        stamped = [
            e.model_copy(update={"retrieved_at": e.retrieved_at or datetime.now(UTC)})
            for e in verified
        ]

        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=decision,
            summary=(
                f"Verified {len(stamped)} claim(s): "
                f"{len(supported)} supported, {len(unsupported)} unsupported, "
                f"{len(conflicting)} conflicting. Peer conclusions were not "
                "treated as primary evidence."
            ),
            evidence=stamped if supported or conflicting else stamped[:1],
            unknowns=[
                e.claim[:80] for e in stamped if e.verification == VerificationStatus.UNVERIFIED
            ][:5],
            next_action=next_action,
            metadata={
                "supported_count": len(supported),
                "unsupported_count": len(unsupported),
                "conflicting_count": len(conflicting),
            },
        )
