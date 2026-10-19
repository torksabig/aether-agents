"""Shared Pydantic schemas for agent I/O and Notion records."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class TaskStatus(StrEnum):
    QUEUED = "Queued"
    RUNNING = "Running"
    NEEDS_REVIEW = "Needs Review"
    APPROVED = "Approved"
    FAILED = "Failed"
    BLOCKED = "Blocked"
    REJECTED = "Rejected"


class AgentName(StrEnum):
    LEAD_EXTRACTION = "lead_extraction"
    RESEARCH_EXTRACTION = "research_extraction"
    EVIDENCE_VERIFICATION = "evidence_verification"
    QUALIFICATION = "qualification"
    TECHNICAL_FEASIBILITY = "technical_feasibility"
    FUNDING_PARTNERSHIP = "funding_partnership"


class Decision(StrEnum):
    QUALIFIED = "qualified"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class EligibilityOutcome(StrEnum):
    ELIGIBLE = "Eligible"
    NOT_ELIGIBLE = "Not eligible"
    UNVERIFIED = "Unverified"


class EvidenceKind(StrEnum):
    FACT = "Fact"
    HYPOTHESIS = "Hypothesis"


class VerificationStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"
    UNVERIFIED = "unverified"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return f"run_{uuid4().hex[:12]}"


class Evidence(BaseModel):
    """A single source-backed claim. Original URLs must be retained end-to-end."""

    source_url: HttpUrl
    claim: str = Field(min_length=10)
    retrieved_at: datetime
    kind: EvidenceKind = EvidenceKind.FACT
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verification: VerificationStatus = VerificationStatus.UNVERIFIED

    @field_validator("claim")
    @classmethod
    def strip_claim(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise ValueError("claim must be at least 10 characters after strip")
        return cleaned


class AgentResult(BaseModel):
    """Canonical agent output written to Notion after validation."""

    schema_version: Literal["1.0"] = "1.0"
    opportunity_id: str = Field(min_length=1)
    agent: AgentName
    run_id: str = Field(default_factory=new_run_id)
    decision: Decision
    summary: str = Field(min_length=20)
    evidence: list[Evidence] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_action: str = Field(min_length=5)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_evidence(self) -> AgentResult:
        if self.decision == Decision.QUALIFIED and not self.evidence:
            raise ValueError("Qualified outcomes require supporting evidence")
        return self


class CompanyRecord(BaseModel):
    name: str = Field(min_length=1)
    domain: str | None = None
    external_id: str = Field(min_length=1)
    stage: str = "New"
    owner: str | None = None
    source_url: HttpUrl | None = None
    last_updated: datetime = Field(default_factory=utc_now)
    contacts: list[str] = Field(default_factory=list)


class ResearchFinding(BaseModel):
    finding: str = Field(min_length=10)
    source_url: HttpUrl
    related_company_id: str
    kind: EvidenceKind = EvidenceKind.FACT
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified_date: date | None = None


class ProjectRecord(BaseModel):
    project: str = Field(min_length=1)
    company_id: str
    use_case: str
    technical_status: str = "Unknown"
    data_access: Literal["confirmed", "unconfirmed", "denied"] = "unconfirmed"
    owner: str | None = None
    decision: str | None = None
    budget_eur: float | None = None
    region: str | None = None
    maturity: str | None = None
    partners: list[str] = Field(default_factory=list)
    co_financing_capacity: Literal["confirmed", "partial", "unknown", "none"] = "unknown"


class FundingMatch(BaseModel):
    programme: str = Field(min_length=1)
    organization: str
    region: str
    deadline: date | None = None
    eligibility: EligibilityOutcome
    project_match_score: float = Field(ge=0.0, le=1.0)
    status: str = "Candidate"
    source_url: HttpUrl
    eligibility_gaps: list[str] = Field(default_factory=list)
    recommended_next_step: str = Field(min_length=5)
    fit_assessment: str = Field(min_length=10)

    @model_validator(mode="after")
    def eligibility_overrides_score(self) -> FundingMatch:
        if self.eligibility == EligibilityOutcome.NOT_ELIGIBLE and self.project_match_score > 0.5:
            # High match never overrides a failed eligibility requirement.
            self.status = "Not eligible"
        if self.deadline is not None and self.deadline < date.today():
            self.status = "Expired"
        return self


class AgentTask(BaseModel):
    """Queued work item from the Agent Tasks & Runs Notion database."""

    page_id: str
    task: str
    agent: AgentName
    target_id: str
    status: TaskStatus = TaskStatus.QUEUED
    attempts: int = 0
    last_run: datetime | None = None
    error: str | None = None
    result_link: str | None = None
    approved_token_budget: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OpportunityContext(BaseModel):
    """Pipeline context carried between agents. Sources are never dropped."""

    opportunity_id: str
    company: CompanyRecord | None = None
    research: list[ResearchFinding] = Field(default_factory=list)
    verified_evidence: list[Evidence] = Field(default_factory=list)
    project: ProjectRecord | None = None
    funding_matches: list[FundingMatch] = Field(default_factory=list)
    prior_results: list[AgentResult] = Field(default_factory=list)
    source_urls: list[HttpUrl] = Field(default_factory=list)

    def add_evidence_sources(self, evidence: list[Evidence]) -> None:
        existing = {str(url) for url in self.source_urls}
        for item in evidence:
            url = str(item.source_url)
            if url not in existing:
                self.source_urls.append(item.source_url)
                existing.add(url)

    def retain_sources_from_result(self, result: AgentResult) -> None:
        self.prior_results.append(result)
        self.add_evidence_sources(result.evidence)


class AgentInput(BaseModel):
    """Uniform input envelope for every agent."""

    task: AgentTask
    context: OpportunityContext
    token_budget: int = 8000
    dry_run: bool = False


class PipelineOutcome(BaseModel):
    opportunity_id: str
    run_id: str
    results: list[AgentResult]
    final_decision: Decision
    consolidated_summary: str
    next_actions: list[str]
    blockers: list[str] = Field(default_factory=list)
