"""Minimal Telegram Bot API client (long polling via getUpdates)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from aether_agents.config import Settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelegramClient:
    """Thin httpx wrapper around Bot API methods we need."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> None:
        token = settings.telegram_bot_token.strip()
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=f"{TELEGRAM_API_BASE}/bot{token}",
            timeout=httpx.Timeout(70.0, connect=10.0),
        )
        self._offset: int | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(f"/{method}", json=payload or {})
        if response.status_code >= 400:
            raise TelegramError(
                f"Telegram API HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram API error: {data.get('description', data)}")
        result = data.get("result")
        if isinstance(result, dict):
            return result
        # getUpdates returns a list in result — wrap for callers that need raw
        return data

    def get_updates(self, *, timeout: int = 25) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if self._offset is not None:
            body["offset"] = self._offset
        response = self._client.post("/getUpdates", json=body)
        if response.status_code >= 400:
            raise TelegramError(
                f"Telegram getUpdates HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram getUpdates error: {data.get('description', data)}")
        updates = data.get("result") or []
        if not isinstance(updates, list):
            return []
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        response = self._client.post("/sendMessage", json=payload)
        if response.status_code >= 400:
            raise TelegramError(
                f"Telegram sendMessage HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram sendMessage error: {data.get('description', data)}")
        result = data.get("result")
        return result if isinstance(result, dict) else data
