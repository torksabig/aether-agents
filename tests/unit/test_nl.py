"""NL → chat command mapping."""

from __future__ import annotations

from aether_agents.config import Settings
from aether_agents.nl import looks_like_command, map_message_to_command


def test_looks_like_command() -> None:
    assert looks_like_command("lead Acme Oy")
    assert looks_like_command("RUN Demo Building")
    assert looks_like_command("help")
    assert not looks_like_command("look into Helsingin Energia")


def test_passthrough_known_command() -> None:
    result = map_message_to_command("research Acme Oy", Settings())
    assert result.source == "passthrough"
    assert result.command == "research Acme Oy"


def test_free_text_without_llm_falls_back_to_help() -> None:
    result = map_message_to_command(
        "look into Helsingin Energia BMS opportunity",
        Settings(OPENROUTER_API_KEY=""),
    )
    assert result.source == "fallback_help"
    assert result.command == "help"
