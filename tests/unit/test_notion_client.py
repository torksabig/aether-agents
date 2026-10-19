"""Notion client: retries, upserts, no duplicate companies."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aether_agents.config import Settings
from aether_agents.integrations.notion import (
    NotionClient,
    NotionError,
    ensure_external_id,
)
from aether_agents.models import (
    AgentName,
    AgentResult,
    AgentTask,
    CompanyRecord,
    Decision,
    TaskStatus,
)


class MockTransport(httpx.MockTransport):
    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.calls: list[tuple[str, str]] = []


def _settings() -> Settings:
    return Settings(
        NOTION_TOKEN="secret-token",
        NOTION_TASKS_DATA_SOURCE_ID="ds_tasks",
        NOTION_COMPANIES_DATA_SOURCE_ID="ds_companies",
        NOTION_FUNDING_DATA_SOURCE_ID="ds_funding",
        NOTION_MAX_RPS=1000,
    )


def test_ensure_external_id_stable() -> None:
    assert ensure_external_id("Acme", "acme.fi") == ensure_external_id("Acme", "www.acme.fi")


def test_list_queued_tasks_and_mark_running() -> None:
    state = {"attempts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "page-1",
                            "properties": {
                                "Task": {
                                    "type": "title",
                                    "title": [{"plain_text": "Run lead"}],
                                },
                                "Agent": {
                                    "type": "select",
                                    "select": {"name": "lead_extraction"},
                                },
                                "Target ID": {
                                    "type": "rich_text",
                                    "rich_text": [{"plain_text": "co_x"}],
                                },
                                "Status": {
                                    "type": "select",
                                    "select": {"name": "Queued"},
                                },
                                "Attempts": {"type": "number", "number": 0},
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.method == "PATCH":
            state["attempts"] += 1
            body = json.loads(request.content.decode())
            assert body["properties"]["Status"]["select"]["name"] == "Running"
            return httpx.Response(200, json={"id": "page-1"})
        return httpx.Response(500, json={"message": "unexpected"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://api.notion.com/v1")
    notion = NotionClient(_settings(), client=client)
    tasks = notion.list_queued_tasks()
    assert len(tasks) == 1
    assert tasks[0].agent == AgentName.LEAD_EXTRACTION
    updated = notion.mark_task_running(tasks[0])
    assert updated.status == TaskStatus.RUNNING
    assert updated.attempts == 1
    notion.close()


def test_upsert_company_updates_instead_of_duplicate() -> None:
    creates = {"n": 0}
    updates = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            # First call empty, second call finds existing
            if creates["n"] == 0 and updates["n"] == 0:
                return httpx.Response(200, json={"results": [], "has_more": False})
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "company-page"}],
                    "has_more": False,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/pages"):
            creates["n"] += 1
            return httpx.Response(200, json={"id": "company-page"})
        if request.method == "PATCH":
            updates["n"] += 1
            return httpx.Response(200, json={"id": "company-page"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.notion.com/v1")
    notion = NotionClient(_settings(), client=http)
    company = CompanyRecord(name="Acme", domain="acme.fi", external_id="co_acme.fi")
    first = notion.upsert_company(company)
    second = notion.upsert_company(company)
    assert first == second == "company-page"
    assert creates["n"] == 1
    assert updates["n"] == 1
    notion.close()


def test_rate_limit_retries_without_duplicate_write() -> None:
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/pages" in request.url.path:
            posts["n"] += 1
            if posts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={})
            return httpx.Response(200, json={"id": "new-page"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.notion.com/v1")
    notion = NotionClient(_settings(), client=http, max_retries=3)
    page = notion.create_page("ds_companies", {"Name": {"title": [{"text": {"content": "X"}}]}})
    assert page["id"] == "new-page"
    assert posts["n"] == 2
    notion.close()


def test_mark_task_failed_preserves_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["properties"]["Status"]["select"]["name"] == "Failed"
        assert "boom" in body["properties"]["Error"]["rich_text"][0]["text"]["content"]
        return httpx.Response(200, json={"id": "page-1"})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    notion = NotionClient(_settings(), client=http)
    task = AgentTask(
        page_id="page-1",
        task="t",
        agent=AgentName.LEAD_EXTRACTION,
        target_id="co_x",
    )
    failed = notion.mark_task_failed(task, "boom")
    assert failed.status == TaskStatus.FAILED
    notion.close()


def test_invalid_result_never_written_concept() -> None:
    """AgentResult validation blocks unqualified writes before Notion."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentResult(
            opportunity_id="x",
            agent=AgentName.QUALIFICATION,
            decision=Decision.QUALIFIED,
            summary="Qualified without any evidence attached at all.",
            evidence=[],
            next_action="Go",
        )


def test_find_company_and_project_by_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ds_companies" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "c1",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [{"plain_text": "Acme"}],
                                },
                                "Domain": {
                                    "type": "rich_text",
                                    "rich_text": [{"plain_text": "acme.fi"}],
                                },
                                "External ID": {
                                    "type": "rich_text",
                                    "rich_text": [{"plain_text": "co_acme.fi"}],
                                },
                                "Stage": {
                                    "type": "select",
                                    "select": {"name": "New"},
                                },
                                "Owner": {
                                    "type": "rich_text",
                                    "rich_text": [{"plain_text": "Teodor"}],
                                },
                                "Source URL": {
                                    "type": "url",
                                    "url": "https://acme.fi",
                                },
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "p1",
                        "properties": {
                            "Project": {
                                "type": "title",
                                "title": [{"plain_text": "Pilot"}],
                            },
                            "Company": {
                                "type": "rich_text",
                                "rich_text": [{"plain_text": "co_acme.fi"}],
                            },
                            "Use Case": {
                                "type": "rich_text",
                                "rich_text": [{"plain_text": "building energy BMS"}],
                            },
                            "Technical Status": {
                                "type": "select",
                                "select": {"name": "draft"},
                            },
                            "Data Access": {
                                "type": "select",
                                "select": {"name": "confirmed"},
                            },
                            "Region": {
                                "type": "rich_text",
                                "rich_text": [{"plain_text": "finland"}],
                            },
                            "Co-financing": {
                                "type": "select",
                                "select": {"name": "confirmed"},
                            },
                            "Partners": {
                                "type": "rich_text",
                                "rich_text": [{"plain_text": "Aalto"}],
                            },
                        },
                    }
                ],
                "has_more": False,
            },
        )

    settings = Settings(
        NOTION_TOKEN="secret-token",
        NOTION_TASKS_DATA_SOURCE_ID="ds_tasks",
        NOTION_COMPANIES_DATA_SOURCE_ID="ds_companies",
        NOTION_PROJECTS_DATA_SOURCE_ID="ds_projects",
        NOTION_MAX_RPS=1000,
    )
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    notion = NotionClient(settings, client=http)
    company = notion.find_company_by_external_id("co_acme.fi")
    assert company is not None
    assert company.name == "Acme"
    project = notion.find_project_by_company_id("co_acme.fi")
    assert project is not None
    assert project["project"] == "Pilot"
    notion.close()


def test_notion_error_on_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    notion = NotionClient(_settings(), client=http)
    with pytest.raises(NotionError):
        notion.request("GET", "/users/me")
    notion.close()
