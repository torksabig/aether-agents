"""Telegram allowlist + Notion output page payload."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx

from aether_agents.chat import ChatReply
from aether_agents.config import Settings
from aether_agents.integrations.notion import NotionClient
from aether_agents.telegram_bot import handle_telegram_text, write_run_output_page


def test_settings_allowlist_parsing() -> None:
    settings = Settings(TELEGRAM_ALLOWED_CHAT_IDS="111, 222,bad,333")
    assert settings.telegram_allowed_chat_id_set() == {111, 222, 333}
    assert not Settings(TELEGRAM_ALLOWED_CHAT_IDS="").telegram_ready()
    assert Settings(
        TELEGRAM_ENABLED=True,
        TELEGRAM_BOT_TOKEN="tok",
        TELEGRAM_ALLOWED_CHAT_IDS="1",
    ).telegram_ready()


def test_allowlist_rejects_unknown_chat() -> None:
    settings = Settings(
        TELEGRAM_ENABLED=True,
        TELEGRAM_BOT_TOKEN="tok",
        TELEGRAM_ALLOWED_CHAT_IDS="99",
    )
    telegram = MagicMock()
    chat = MagicMock()
    handle_telegram_text(
        settings=settings,
        chat=chat,
        telegram=telegram,
        text="lead Acme",
        chat_id=1,
        message_id=10,
    )
    chat.handle.assert_not_called()
    telegram.send_message.assert_called_once()
    assert "allowlist" in telegram.send_message.call_args.args[1].lower()


def test_create_child_page_payload_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "url": "https://www.notion.so/aaaaaaaabbbbccccddddeeeeeeeeeeee",
            },
        )

    client = httpx.Client(
        base_url="https://api.notion.com/v1",
        transport=httpx.MockTransport(handler),
    )
    notion = NotionClient(
        Settings(NOTION_TOKEN="t", NOTION_TASKS_DATA_SOURCE_ID="ds", NOTION_MAX_RPS=1000),
        client=client,
    )
    page = notion.create_child_page(
        "parent-page-id",
        "2026-10-19 Lead - Acme",
        headings=[("h2", "Telegram agent run")],
        paragraphs=["Status: ok", "Result: done"],
    )
    body = captured["json"]
    assert body["parent"] == {"page_id": "parent-page-id"}
    assert body["properties"]["title"]["title"][0]["text"]["content"].startswith("2026-10-19")
    assert len(body["children"]) >= 2
    assert NotionClient.page_url(page).startswith("https://www.notion.so/")
    notion.close()


def test_write_run_output_page_uses_parent() -> None:
    notion = MagicMock()
    notion.ensure_outputs_parent.return_value = "parent-id"
    notion.create_child_page.return_value = {
        "id": "child-id",
        "url": "https://www.notion.so/child",
    }
    page_id, url = write_run_output_page(
        notion,
        command="lead Acme Oy",
        reply=ChatReply("Lead saved", ok=True),
        chat_id=42,
        message_id=7,
        nl_source="passthrough",
    )
    assert page_id == "child-id"
    assert "notion" in url
    kwargs = notion.create_child_page.call_args
    assert kwargs.args[0] == "parent-id"
    assert "Lead" in kwargs.args[1]
