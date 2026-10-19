"""Chat CLI command parsing (no Notion required)."""

from __future__ import annotations

from aether_agents.chat import AetherChat, _company_payload
from aether_agents.config import Settings


def test_company_payload_parses_name_domain_url() -> None:
    payload = _company_payload("Acme Oy acme.fi https://acme.fi/about")
    assert payload["company_name"] == "Acme Oy"
    assert payload["domain"] == "acme.fi"
    assert payload["external_id"] == "co_acme.fi"
    assert payload["source_url"] == "https://acme.fi/about"


def test_company_payload_accepts_external_id() -> None:
    payload = _company_payload("co_demo-building.fi")
    assert payload["external_id"] == "co_demo-building.fi"


def test_help_and_ask_offline() -> None:
    chat = AetherChat(Settings(AGENT_DRY_RUN=True))
    help_reply = chat.handle("help")
    assert "ask <company>" in help_reply.text

    ask = chat.handle("ask Demo Building Owner Oy demo-building.fi")
    assert ask.ok
    assert "Decision:" in ask.text
    assert "co_demo-building.fi" in ask.text
    chat.close()


def test_unknown_command_is_friendly() -> None:
    chat = AetherChat(Settings())
    reply = chat.handle("make coffee")
    assert "didn't catch" in reply.text.lower()
    chat.close()


def test_lead_offline() -> None:
    chat = AetherChat(Settings())
    reply = chat.handle("lead Test Co testco.fi https://testco.fi")
    assert reply.ok
    assert "co_testco.fi" in reply.text
    chat.close()
