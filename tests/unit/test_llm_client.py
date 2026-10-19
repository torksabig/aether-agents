"""LLM client rejects invalid JSON before Notion write path."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from aether_agents.config import Settings
from aether_agents.integrations.llm import LLMClient, LLMError


class TinySchema(BaseModel):
    summary: str = Field(min_length=5)


def test_invalid_json_retries_then_fails() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json-at-all"}}]},
        )

    settings = Settings(
        OPENROUTER_API_KEY="test-key",
        AGENT_LLM_RETRY_LIMIT=1,
    )
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    llm = LLMClient(settings, client=http)
    with pytest.raises(LLMError, match="invalid JSON"):
        llm.complete_json(
            system="Return JSON",
            user="Hello",
            schema=TinySchema,
        )
    assert calls["n"] == 2  # initial + 1 retry
    llm.close()


def test_valid_json_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"summary": "Valid summary text"})}}]
            },
        )

    settings = Settings(OPENROUTER_API_KEY="test-key")
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    llm = LLMClient(settings, client=http)
    result = llm.complete_json(system="sys", user="user", schema=TinySchema)
    assert result.summary.startswith("Valid")
    llm.close()
