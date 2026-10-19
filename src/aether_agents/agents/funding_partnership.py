"""Funding & Partnership — official-source matching with hard eligibility rules."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import ClassVar, TypedDict

from pydantic import HttpUrl

from aether_agents.agents.base import BaseAgent
from aether_agents.models import (
    AgentInput,
    AgentName,
    AgentResult,
    Decision,
    EligibilityOutcome,
    Evidence,
    FundingMatch,
    ProjectRecord,
)


class ProgrammeSpec(TypedDict):
    programme: str
    organization: str
    region: str
    deadline: date
    source_url: str
    applicant_types: set[str]
    geographies: set[str]
    min_maturity: str
    requires_consortium: bool
    requires_co_financing: bool
    themes: set[str]


# Compact catalogue of official programmes (URLs are checkable).
# Agents must not invent programmes beyond this catalogue in V1 offline mode.
PROGRAMME_CATALOGUE: list[ProgrammeSpec] = [
    {
        "programme": "Business Finland — Energy Aid",
        "organization": "Business Finland",
        "region": "Finland",
        "deadline": date(2099, 12, 31),  # rolling; treat as open
        "source_url": ("https://www.businessfinland.fi/en/for-finnish-customers/services/funding/"),
        "applicant_types": {"company", "sme"},
        "geographies": {"finland", "fi", "eu"},
        "min_maturity": "pilot",
        "requires_consortium": False,
        "requires_co_financing": True,
        "themes": {"energy", "bms", "buildings", "efficiency"},
    },
    {
        "programme": "Horizon Europe — Climate, Energy and Mobility",
        "organization": "European Commission",
        "region": "EU",
        "deadline": date(2027, 9, 15),
        "source_url": (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home"
        ),
        "applicant_types": {"company", "research", "consortium"},
        "geographies": {"eu", "eea", "finland", "fi"},
        "min_maturity": "research",
        "requires_consortium": True,
        "requires_co_financing": True,
        "themes": {"energy", "buildings", "digital", "bms"},
    },
    {
        "programme": "EIC Accelerator",
        "organization": "European Innovation Council",
        "region": "EU",
        "deadline": date(2027, 3, 11),
        "source_url": ("https://eic.ec.europa.eu/eic-funding-opportunities/eic-accelerator_en"),
        "applicant_types": {"sme", "startup"},
        "geographies": {"eu", "eea", "finland", "fi"},
        "min_maturity": "scaleup",
        "requires_consortium": False,
        "requires_co_financing": False,
        "themes": {"deeptech", "energy", "digital"},
    },
    {
        "programme": "Expired Demo Grant (fixture)",
        "organization": "Demo Authority",
        "region": "EU",
        "deadline": date(2020, 1, 1),
        "source_url": (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home"
        ),
        "applicant_types": {"company"},
        "geographies": {"eu"},
        "min_maturity": "pilot",
        "requires_consortium": False,
        "requires_co_financing": False,
        "themes": {"energy"},
    },
]


def _theme_score(project: ProjectRecord, themes: set[str]) -> float:
    hay = f"{project.project} {project.use_case}".lower()
    hits = sum(1 for t in themes if t in hay)
    return min(1.0, hits / max(len(themes), 1) * 2)


def evaluate_eligibility(
    project: ProjectRecord,
    programme: ProgrammeSpec,
) -> tuple[EligibilityOutcome, list[str]]:
    """Three explicit outcomes: Eligible, Not eligible, Unverified."""
    gaps: list[str] = []
    region = (project.region or "").lower()
    geos = {g.lower() for g in programme["geographies"]}
    if region and region not in geos and "eu" not in geos:
        return EligibilityOutcome.NOT_ELIGIBLE, ["geography"]

    if programme["requires_consortium"] and len(project.partners) < 1:
        gaps.append("research_or_consortium_partner")

    if programme["requires_co_financing"] and project.co_financing_capacity in {
        "none",
        "unknown",
    }:
        gaps.append("co_financing_commitment")

    if project.data_access != "confirmed":
        gaps.append("confirmed_data_access")

    deadline = programme["deadline"]
    if deadline < date.today():
        return EligibilityOutcome.NOT_ELIGIBLE, ["deadline_passed"]

    # Missing region → Unverified, never fabricate Eligible
    if not project.region:
        gaps.append("applicant_geography")
        return EligibilityOutcome.UNVERIFIED, gaps

    if gaps:
        if programme["requires_consortium"] and "research_or_consortium_partner" in gaps:
            return EligibilityOutcome.NOT_ELIGIBLE, gaps
        if "co_financing_commitment" in gaps and programme["requires_co_financing"]:
            return EligibilityOutcome.UNVERIFIED, gaps
        return EligibilityOutcome.UNVERIFIED, gaps

    return EligibilityOutcome.ELIGIBLE, []


def match_funding(project: ProjectRecord) -> list[FundingMatch]:
    matches: list[FundingMatch] = []
    for programme in PROGRAMME_CATALOGUE:
        deadline = programme["deadline"]
        eligibility, gaps = evaluate_eligibility(project, programme)
        score = _theme_score(project, programme["themes"])
        match = FundingMatch(
            programme=programme["programme"],
            organization=programme["organization"],
            region=programme["region"],
            deadline=deadline,
            eligibility=eligibility,
            project_match_score=score,
            source_url=HttpUrl(programme["source_url"]),
            eligibility_gaps=gaps,
            recommended_next_step=_next_step(eligibility, gaps),
            fit_assessment=(
                f"Theme score {score:.2f} for {project.use_case}. "
                f"Eligibility={eligibility.value}."
            ),
        )
        # Expired grants must not appear as open opportunities
        if match.status == "Expired":
            continue
        if eligibility == EligibilityOutcome.NOT_ELIGIBLE:
            match.status = "Not eligible"
        matches.append(match)

    matches.sort(
        key=lambda m: (
            0 if m.eligibility == EligibilityOutcome.ELIGIBLE else 1,
            0 if m.eligibility == EligibilityOutcome.UNVERIFIED else 1,
            -m.project_match_score,
        )
    )
    return matches


def _next_step(eligibility: EligibilityOutcome, gaps: list[str]) -> str:
    if eligibility == EligibilityOutcome.ELIGIBLE:
        return "Prepare short concept note and confirm human approval before outreach."
    if eligibility == EligibilityOutcome.NOT_ELIGIBLE:
        return "Do not apply; document exclusion reason and consider alternatives."
    if gaps:
        return f"Close gaps before applying: {', '.join(gaps)}."
    return "Verify eligibility requirements against the official programme page."


class FundingPartnershipAgent(BaseAgent):
    name: ClassVar[AgentName] = AgentName.FUNDING_PARTNERSHIP

    def run(self, payload: AgentInput) -> AgentResult:
        project = payload.context.project
        if project is None and isinstance(payload.task.payload.get("project"), dict):
            project = ProjectRecord.model_validate(payload.task.payload["project"])

        if project is None:
            return AgentResult(
                opportunity_id=payload.context.opportunity_id,
                agent=self.name,
                decision=Decision.NEEDS_REVIEW,
                summary=(
                    "Funding match requires a technically reviewed project with "
                    "scope, region, and data-access status."
                ),
                unknowns=["project_record"],
                next_action="Complete technical feasibility before funding research.",
            )

        if project.data_access != "confirmed":
            return AgentResult(
                opportunity_id=payload.context.opportunity_id,
                agent=self.name,
                decision=Decision.BLOCKED,
                summary=(
                    f"Funding recommendations blocked for '{project.project}': "
                    "confirmed data access is a prerequisite for BMS pilots."
                ),
                unknowns=["confirmed_data_access"],
                next_action="Secure data-access agreement before programme matching.",
                metadata={"funding_matches": []},
            )

        matches = match_funding(project)
        actionable = [
            m
            for m in matches
            if m.eligibility in {EligibilityOutcome.ELIGIBLE, EligibilityOutcome.UNVERIFIED}
        ]

        evidence = [
            Evidence(
                source_url=m.source_url,
                claim=(
                    f"{m.programme}: eligibility {m.eligibility.value}; "
                    f"gaps={', '.join(m.eligibility_gaps) or 'none'}"
                ),
                retrieved_at=datetime.now(UTC),
            )
            for m in actionable[:5]
        ]

        if not actionable:
            decision = Decision.BLOCKED
            summary = (
                "No open eligible funding programmes matched this project under "
                "current constraints. Review catalogue and project prerequisites."
            )
            next_action = "Revisit eligibility gaps or expand official programme sources."
        elif any(m.eligibility == EligibilityOutcome.ELIGIBLE for m in actionable):
            decision = Decision.QUALIFIED
            summary = (
                f"Identified {len(actionable)} funding candidate(s) for "
                f"'{project.project}'. Eligibility never overridden by match score."
            )
            next_action = "Human review before applications or partner outreach."
        else:
            decision = Decision.NEEDS_REVIEW
            summary = (
                f"Found {len(actionable)} unverified funding candidate(s). "
                "Close eligibility gaps using official programme requirements."
            )
            next_action = "Verify geography, consortium, and co-financing on official pages."

        return AgentResult(
            opportunity_id=payload.context.opportunity_id,
            agent=self.name,
            decision=decision,
            summary=summary,
            evidence=evidence,
            unknowns=[g for m in matches for g in m.eligibility_gaps],
            next_action=next_action,
            metadata={
                "funding_matches": [m.model_dump(mode="json") for m in matches],
                "autonomous_outreach_disabled": True,
            },
        )
