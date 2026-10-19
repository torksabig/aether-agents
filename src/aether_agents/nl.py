"""Natural-language → Aether chat command mapper.

Uses existing chat regex verbs when the message already looks like a command.
Falls back to OpenRouter structured JSON when OPENROUTER_API_KEY is set.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from aether_agents.config import Settings
from aether_agents.integrations.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

KNOWN_VERBS = (
    "help",
    "status",
    "queue",
    "process",
    "companies",
    "ask",
    "lead",
    "research",
    "verify",
    "qualify",
    "feasibility",
    "funding",
    "fund",
    "tech",
    "task",
    "run",
)

_COMMAND_PREFIX = re.compile(
    rf"^(?:{'|'.join(KNOWN_VERBS)})(?:\s|$)",
    re.IGNORECASE,
)


class MappedIntent(BaseModel):
    """LLM output schema for free-text Telegram messages."""

    command: str = Field(
        description=(
            "A single Aether chat command line, e.g. "
            "'lead Acme Oy acme.fi' or 'run Demo Building Owner Oy' or 'help'."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    rationale: str = Field(default="")


IntentSource = Literal["passthrough", "llm", "fallback_help"]


class NLMapResult(BaseModel):
    command: str
    source: IntentSource
    confidence: float = 1.0


def looks_like_command(text: str) -> bool:
    return bool(_COMMAND_PREFIX.match(text.strip()))


def map_message_to_command(text: str, settings: Settings) -> NLMapResult:
    """Map user text to a chat command string for AetherChat.handle."""
    cleaned = text.strip()
    if not cleaned:
        return NLMapResult(command="help", source="fallback_help", confidence=1.0)

    if looks_like_command(cleaned):
        return NLMapResult(command=cleaned, source="passthrough", confidence=1.0)

    if not settings.openrouter_api_key:
        logger.info("No OPENROUTER_API_KEY; free text falls back to help")
        return NLMapResult(command="help", source="fallback_help", confidence=0.0)

    system = (
        "You map user messages to Aether Applied agent chat commands.\n"
        "Allowed command forms:\n"
        "  help | status | queue | process | companies\n"
        "  ask <company>\n"
        "  lead <company> [domain] [url]\n"
        "  research <company|target_id>\n"
        "  qualify <company|target_id>\n"
        "  feasibility <company|target_id>\n"
        "  funding <company|target_id>\n"
        "  run <company>   (full pipeline)\n"
        "  task <agent> <company>\n"
        "Prefer 'run <company>' when the user wants a full opportunity review.\n"
        "Prefer 'lead <company>' when they only want to capture a new lead.\n"
        "If unclear, return command 'help'. Never invent URLs."
    )
    llm = LLMClient(settings)
    try:
        mapped = llm.complete_json(
            system=system,
            user=cleaned,
            schema=MappedIntent,
            max_tokens=400,
        )
    except LLMError as exc:
        logger.warning("NL LLM mapping failed: %s", exc)
        return NLMapResult(command="help", source="fallback_help", confidence=0.0)
    finally:
        llm.close()

    command = mapped.command.strip() or "help"
    if mapped.confidence < 0.35:
        return NLMapResult(command="help", source="fallback_help", confidence=mapped.confidence)
    if not looks_like_command(command) and command.lower() not in KNOWN_VERBS:
        return NLMapResult(command="help", source="fallback_help", confidence=mapped.confidence)
    return NLMapResult(command=command, source="llm", confidence=mapped.confidence)
