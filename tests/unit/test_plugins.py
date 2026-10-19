"""External extractor plugin loading."""

from __future__ import annotations

import os

from aether_agents.agents.lead_extraction import extract_company_from_payload
from aether_agents.plugins import resolve_lead_extractor


def _fake_lead(payload: dict[str, object]) -> dict[str, object]:
    return {
        "name": "Plugin Co",
        "domain": "plugin.fi",
        "external_id": "co_plugin.fi",
        "source_url": "https://plugin.fi",
        "contacts": ["a@plugin.fi"],
    }


def test_builtin_lead_still_works() -> None:
    company = extract_company_from_payload(
        {
            "company_name": "Acme",
            "domain": "acme.fi",
            "source_url": "https://acme.fi",
        }
    )
    assert company.external_id == "co_acme.fi"


def test_external_lead_plugin(monkeypatch: object) -> None:
    monkeypatch.setenv(  # type: ignore[attr-defined]
        "AETHER_LEAD_EXTRACTOR",
        "tests.unit.test_plugins:_fake_lead",
    )
    # Clear any cached import side effects by reading env fresh
    fn = resolve_lead_extractor()
    assert fn is not None
    company = fn({"ignored": True})
    assert company.name == "Plugin Co"
    assert company.external_id == "co_plugin.fi"
    monkeypatch.delenv("AETHER_LEAD_EXTRACTOR", raising=False)  # type: ignore[attr-defined]
    assert resolve_lead_extractor() is None
    assert "AETHER_LEAD_EXTRACTOR" not in os.environ
