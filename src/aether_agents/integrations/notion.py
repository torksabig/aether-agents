"""Notion API client (Notion-Version 2025-09-03).

Uses /v1/data_sources/{data_source_id}/query for record queries.
Throttles to ~Notion's documented ~3 rps average limit.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from aether_agents.config import Settings
from aether_agents.models import (
    AgentName,
    AgentResult,
    AgentTask,
    CompanyRecord,
    Decision,
    FundingMatch,
    TaskStatus,
)

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"


class NotionError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotionRateLimiter:
    """Simple token-bucket style delay between requests."""

    def __init__(self, max_rps: float = 2.5) -> None:
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.4
        self._last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


def _rich_text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": value[:2000]}}]


def _title(value: str) -> dict[str, Any]:
    return {"title": _rich_text(value)}


def _rich(value: str) -> dict[str, Any]:
    return {"rich_text": _rich_text(value)}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


def _number(value: int | float) -> dict[str, Any]:
    return {"number": value}


def _url(value: str | None) -> dict[str, Any]:
    return {"url": value}


def _date(value: datetime | None) -> dict[str, Any]:
    if value is None:
        return {"date": None}
    return {"date": {"start": value.astimezone(UTC).date().isoformat()}}


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "select" and prop.get("select"):
        return str(prop["select"].get("name", ""))
    if ptype == "status" and prop.get("status"):
        return str(prop["status"].get("name", ""))
    if ptype == "number":
        return str(prop.get("number") or "")
    if ptype == "url":
        return str(prop.get("url") or "")
    if ptype == "date" and prop.get("date"):
        return str(prop["date"].get("start") or "")
    return ""


def _external_id_from_domain(domain: str | None, name: str) -> str:
    if domain:
        host = urlparse(domain if "://" in domain else f"https://{domain}").netloc
        host = host.lower().removeprefix("www.")
        if host:
            return f"co_{host}"
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    return f"co_{slug[:48] or 'unknown'}"


class NotionClient:
    """Reusable Notion client with retries, pagination, and upsert helpers."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        *,
        max_retries: int = 3,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=NOTION_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.notion_token}",
                "Notion-Version": settings.notion_version,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._limiter = NotionRateLimiter(settings.notion_max_rps)
        self._max_retries = max_retries
        self._company_index: dict[str, str] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            self._limiter.wait()
            response = self._client.request(method, path, json=json, params=params)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                logger.warning("Notion rate limited; sleeping %.1fs", retry_after)
                time.sleep(max(retry_after, 1.0))
                if attempt <= self._max_retries:
                    continue
            if response.status_code >= 500 and attempt <= self._max_retries:
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code >= 400:
                raise NotionError(
                    f"Notion API error {response.status_code}: {response.text[:300]}",
                    status_code=response.status_code,
                )
            payload: dict[str, Any] = response.json()
            return payload

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict[str, Any] | None = None,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Query via /v1/data_sources/{id}/query (Notion-Version 2025-09-03)."""
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter is not None:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{data_source_id}/query", json=body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return results

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def create_page(
        self,
        parent_data_source_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            json={
                "parent": {"data_source_id": parent_data_source_id},
                "properties": properties,
            },
        )

    def create_child_page(
        self,
        parent_page_id: str,
        title: str,
        *,
        paragraphs: list[str] | None = None,
        headings: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Create a page nested under another page (not a database row)."""
        children: list[dict[str, Any]] = []
        for level, text in headings or []:
            block_type = "heading_2" if level == "h2" else "heading_3"
            children.append(
                {
                    "object": "block",
                    "type": block_type,
                    block_type: {"rich_text": _rich_text(text)},
                }
            )
        for text in paragraphs or []:
            cleaned = text.strip()
            if not cleaned:
                continue
            for start in range(0, len(cleaned), 2000):
                chunk = cleaned[start : start + 2000]
                children.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": _rich_text(chunk)},
                    }
                )
        body: dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": _title(title[:200])},
        }
        if children:
            body["children"] = children[:100]
        return self.request("POST", "/pages", json=body)

    def ensure_outputs_parent(self) -> str:
        """Return NOTION_OUTPUTS_PARENT_PAGE_ID, creating under Command Center if needed."""
        existing = self.settings.notion_outputs_parent_page_id.strip()
        if existing:
            return existing
        parent = self.settings.notion_command_center_page_id.strip()
        if not parent:
            raise NotionError(
                "Set NOTION_OUTPUTS_PARENT_PAGE_ID or NOTION_COMMAND_CENTER_PAGE_ID "
                "so Telegram runs can write Notion subpages."
            )
        created = self.create_child_page(
            parent,
            "Telegram Agent Outputs",
            paragraphs=[
                "Each Telegram agent run creates a child page here with the full "
                "result summary and a link back from the bot notification.",
            ],
        )
        page_id = str(created["id"])
        logger.info("Created Telegram Agent Outputs parent page_id=%s", page_id)
        return page_id

    @staticmethod
    def page_url(page: dict[str, Any] | str) -> str:
        if isinstance(page, dict):
            url = page.get("url")
            if isinstance(url, str) and url:
                return url
            page_id = str(page.get("id", "")).replace("-", "")
        else:
            page_id = page.replace("-", "")
        return f"https://www.notion.so/{page_id}"

    def find_company_by_external_id(self, external_id: str) -> CompanyRecord | None:
        ds = self.settings.notion_companies_data_source_id
        if not ds:
            return None
        pages = self.query_data_source(
            ds,
            filter={
                "property": "External ID",
                "rich_text": {"equals": external_id},
            },
        )
        if not pages:
            return None
        props = pages[0].get("properties", {})
        raw: dict[str, Any] = {
            "name": _plain(props.get("Name")) or external_id,
            "domain": _plain(props.get("Domain")) or None,
            "external_id": _plain(props.get("External ID")) or external_id,
            "stage": _plain(props.get("Stage")) or "New",
            "owner": _plain(props.get("Owner")) or None,
        }
        source = _plain(props.get("Source URL"))
        if source:
            raw["source_url"] = source
        return CompanyRecord.model_validate(raw)

    def find_project_by_company_id(self, company_id: str) -> dict[str, Any] | None:
        ds = self.settings.notion_projects_data_source_id
        if not ds:
            return None
        pages = self.query_data_source(
            ds,
            filter={
                "property": "Company",
                "rich_text": {"equals": company_id},
            },
        )
        if not pages:
            return None
        props = pages[0].get("properties", {})
        partners_raw = _plain(props.get("Partners"))
        partners = [p.strip() for p in partners_raw.split(",") if p.strip()]
        return {
            "project": _plain(props.get("Project")) or "Untitled project",
            "company_id": company_id,
            "use_case": _plain(props.get("Use Case")) or "building energy",
            "technical_status": _plain(props.get("Technical Status")) or "Unknown",
            "data_access": _plain(props.get("Data Access")) or "unconfirmed",
            "owner": _plain(props.get("Owner")) or None,
            "decision": _plain(props.get("Decision")) or None,
            "region": _plain(props.get("Region")) or None,
            "partners": partners,
            "co_financing_capacity": _plain(props.get("Co-financing")) or "unknown",
        }

    def create_queued_task(
        self,
        *,
        task: str,
        agent: AgentName,
        target_id: str,
        approved_token_budget: int | None = None,
    ) -> str:
        props: dict[str, Any] = {
            "Task": _title(task),
            "Agent": _select(agent.value),
            "Target ID": _rich(target_id),
            "Status": _select(TaskStatus.QUEUED.value),
            "Attempts": _number(0),
            "Error": _rich(""),
        }
        if approved_token_budget is not None:
            props["Approved Token Budget"] = _number(approved_token_budget)
        created = self.create_page(self.settings.notion_tasks_data_source_id, props)
        return str(created["id"])

    def list_queued_tasks(self, *, limit: int = 10) -> list[AgentTask]:
        pages = self.query_data_source(
            self.settings.notion_tasks_data_source_id,
            filter={
                "property": "Status",
                "select": {"equals": TaskStatus.QUEUED.value},
            },
        )
        tasks = [self._parse_task(page) for page in pages]
        return tasks[:limit]

    def mark_task_running(self, task: AgentTask) -> AgentTask:
        now = datetime.now(UTC)
        self.update_page(
            task.page_id,
            {
                "Status": _select(TaskStatus.RUNNING.value),
                "Attempts": _number(task.attempts + 1),
                "Last Run": _date(now),
                "Error": _rich(""),
            },
        )
        return task.model_copy(
            update={
                "status": TaskStatus.RUNNING,
                "attempts": task.attempts + 1,
                "last_run": now,
                "error": None,
            }
        )

    def mark_task_result(
        self,
        task: AgentTask,
        result: AgentResult,
        *,
        result_link: str | None = None,
    ) -> AgentTask:
        status = {
            Decision.QUALIFIED: TaskStatus.NEEDS_REVIEW,
            Decision.NEEDS_REVIEW: TaskStatus.NEEDS_REVIEW,
            Decision.BLOCKED: TaskStatus.BLOCKED,
        }[result.decision]
        props: dict[str, Any] = {
            "Status": _select(status.value),
            "Error": _rich(""),
            "Result Link": _url(result_link),
        }
        self.update_page(task.page_id, props)
        return task.model_copy(update={"status": status, "result_link": result_link, "error": None})

    def mark_task_failed(self, task: AgentTask, error: str) -> AgentTask:
        self.update_page(
            task.page_id,
            {
                "Status": _select(TaskStatus.FAILED.value),
                "Error": _rich(error[:1900]),
            },
        )
        return task.model_copy(update={"status": TaskStatus.FAILED, "error": error})

    def upsert_company(self, company: CompanyRecord) -> str:
        ds = self.settings.notion_companies_data_source_id
        if not ds:
            raise NotionError("NOTION_COMPANIES_DATA_SOURCE_ID is not configured")
        existing = self.query_data_source(
            ds,
            filter={
                "property": "External ID",
                "rich_text": {"equals": company.external_id},
            },
        )
        props = {
            "Name": _title(company.name),
            "Domain": _rich(company.domain or ""),
            "External ID": _rich(company.external_id),
            "Stage": _select(company.stage),
            "Owner": _rich(company.owner or ""),
            "Source URL": _url(str(company.source_url) if company.source_url else None),
            "Last Updated": _date(company.last_updated),
        }
        if existing:
            page_id = str(existing[0]["id"])
            self.update_page(page_id, props)
            self._company_index[company.external_id] = page_id
            return page_id
        created = self.create_page(ds, props)
        page_id = str(created["id"])
        self._company_index[company.external_id] = page_id
        return page_id

    def write_funding_match(self, match: FundingMatch, *, project_name: str) -> str:
        ds = self.settings.notion_funding_data_source_id
        if not ds:
            raise NotionError("NOTION_FUNDING_DATA_SOURCE_ID is not configured")
        existing = self.query_data_source(
            ds,
            filter={
                "and": [
                    {"property": "Programme", "title": {"equals": match.programme}},
                    {
                        "property": "Project Match",
                        "rich_text": {"equals": project_name},
                    },
                ]
            },
        )
        props = {
            "Programme": _title(match.programme),
            "Organization": _rich(match.organization),
            "Region": _rich(match.region),
            "Deadline": (
                {"date": {"start": match.deadline.isoformat()}}
                if match.deadline
                else {"date": None}
            ),
            "Eligibility": _select(match.eligibility.value),
            "Project Match": _rich(project_name),
            "Status": _select(match.status),
            "Source URL": _url(str(match.source_url)),
        }
        if existing:
            page_id = str(existing[0]["id"])
            self.update_page(page_id, props)
            return page_id
        created = self.create_page(ds, props)
        return str(created["id"])

    def write_agent_result_summary(self, task: AgentTask, result: AgentResult) -> str | None:
        updated = self.mark_task_result(task, result, result_link=None)
        return updated.page_id

    def _parse_task(self, page: dict[str, Any]) -> AgentTask:
        props = page.get("properties", {})
        agent_raw = _plain(props.get("Agent")) or AgentName.LEAD_EXTRACTION.value
        status_raw = _plain(props.get("Status")) or TaskStatus.QUEUED.value
        attempts_raw = props.get("Attempts", {}).get("number")
        return AgentTask(
            page_id=page["id"],
            task=_plain(props.get("Task")) or "Untitled task",
            agent=AgentName(agent_raw),
            target_id=_plain(props.get("Target ID")) or page["id"],
            status=TaskStatus(status_raw),
            attempts=int(attempts_raw or 0),
            error=_plain(props.get("Error")) or None,
            result_link=_plain(props.get("Result Link")) or None,
            payload={"raw_properties": {k: _plain(v) for k, v in props.items()}},
        )


def ensure_external_id(name: str, domain: str | None = None) -> str:
    return _external_id_from_domain(domain, name)
