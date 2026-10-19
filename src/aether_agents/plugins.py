"""Optional hooks for existing Lead / Research extraction implementations.

Set environment variables to import paths of callables that return the shared
models (or dicts that validate into them):

  AETHER_LEAD_EXTRACTOR=package.module:extract_company
  AETHER_RESEARCH_EXTRACTOR=package.module:extract_findings

If unset, the built-in deterministic extractors are used.
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Callable
from typing import Any

from aether_agents.models import CompanyRecord, ResearchFinding

logger = logging.getLogger(__name__)


def _load_callable(spec: str) -> Callable[..., Any]:
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"Invalid plugin spec '{spec}'. Expected 'package.module:callable'")
    module = importlib.import_module(module_name)
    fn = getattr(module, attr)
    if not callable(fn):
        raise TypeError(f"Plugin '{spec}' is not callable")
    return fn  # type: ignore[no-any-return]


def resolve_lead_extractor() -> Callable[[dict[str, Any]], CompanyRecord] | None:
    spec = os.getenv("AETHER_LEAD_EXTRACTOR", "").strip()
    if not spec:
        return None
    logger.info("Using external lead extractor: %s", spec)
    fn = _load_callable(spec)

    def _wrapped(payload: dict[str, Any]) -> CompanyRecord:
        result = fn(payload)
        if isinstance(result, CompanyRecord):
            return result
        return CompanyRecord.model_validate(result)

    return _wrapped


def resolve_research_extractor() -> Callable[..., list[ResearchFinding]] | None:
    spec = os.getenv("AETHER_RESEARCH_EXTRACTOR", "").strip()
    if not spec:
        return None
    logger.info("Using external research extractor: %s", spec)
    fn = _load_callable(spec)

    def _wrapped(
        *,
        company_id: str,
        sources: list[dict[str, Any]],
        questions: list[str],
    ) -> list[ResearchFinding]:
        result = fn(company_id=company_id, sources=sources, questions=questions)
        findings: list[ResearchFinding] = []
        for item in result:
            if isinstance(item, ResearchFinding):
                findings.append(item)
            else:
                findings.append(ResearchFinding.model_validate(item))
        return findings

    return _wrapped
