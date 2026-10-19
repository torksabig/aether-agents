"""Telegram long-poll front door for Aether agents.

Flow:
  user message → allowlist → NL map → AetherChat → Notion child page → reply with link
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from aether_agents.chat import AetherChat, ChatReply
from aether_agents.config import Settings, get_settings
from aether_agents.integrations.notion import NotionClient, NotionError
from aether_agents.integrations.telegram import TelegramClient, TelegramError
from aether_agents.nl import map_message_to_command

logger = logging.getLogger(__name__)


def _guess_agent_label(command: str) -> str:
    first = command.strip().split(None, 1)[0].lower() if command.strip() else "run"
    labels = {
        "lead": "Lead",
        "extract": "Lead",
        "research": "Research",
        "verify": "Evidence",
        "qualify": "Qualification",
        "feasibility": "Feasibility",
        "tech": "Feasibility",
        "funding": "Funding",
        "fund": "Funding",
        "run": "Pipeline",
        "ask": "Ask",
        "process": "Process",
        "status": "Status",
        "help": "Help",
    }
    return labels.get(first, first.title())


def _guess_target(command: str) -> str:
    parts = command.strip().split(None, 1)
    if len(parts) < 2:
        return "general"
    return parts[1][:80]


def write_run_output_page(
    notion: NotionClient,
    *,
    command: str,
    reply: ChatReply,
    chat_id: int,
    message_id: int,
    nl_source: str,
) -> tuple[str, str]:
    """Create a Notion child page for this Telegram run. Returns (page_id, url)."""
    parent_id = notion.ensure_outputs_parent()
    today = datetime.now(UTC).date().isoformat()
    agent_label = _guess_agent_label(command)
    target = _guess_target(command)
    title = f"{today} {agent_label} - {target}"[:200]
    status = "ok" if reply.ok else "failed"
    paragraphs = [
        f"Status: {status}",
        f"Command: {command}",
        f"NL source: {nl_source}",
        f"Telegram chat_id: {chat_id}",
        f"Telegram message_id: {message_id}",
        f"Recorded at: {datetime.now(UTC).isoformat()}",
        "",
        "Result:",
        reply.text[:8000],
    ]
    page = notion.create_child_page(
        parent_id,
        title,
        headings=[("h2", "Telegram agent run")],
        paragraphs=paragraphs,
    )
    return str(page["id"]), NotionClient.page_url(page)


def handle_telegram_text(
    *,
    settings: Settings,
    chat: AetherChat,
    telegram: TelegramClient,
    text: str,
    chat_id: int,
    message_id: int,
) -> None:
    """Process one inbound text message end-to-end."""
    allowed = settings.telegram_allowed_chat_id_set()
    if chat_id not in allowed:
        telegram.send_message(
            chat_id,
            "This bot is private. Your chat is not on the allowlist.",
            reply_to_message_id=message_id,
        )
        return

    if text.strip() in {"/start", "/help"}:
        telegram.send_message(
            chat_id,
            "Aether agents via Telegram.\n\n"
            "Examples:\n"
            "• lead Acme Oy acme.fi\n"
            "• research Acme Oy\n"
            "• run Demo Building Owner Oy\n"
            "• qualify co_acme.fi\n\n"
            "Free text works when OPENROUTER_API_KEY is set.\n"
            "Results are saved as Notion subpages; you get the link when done.",
            reply_to_message_id=message_id,
        )
        return

    telegram.send_message(
        chat_id,
        "Working on it…",
        reply_to_message_id=message_id,
    )

    mapped = map_message_to_command(text, settings)
    reply = chat.handle(mapped.command)

    # Drain queue when Notion is ready and command likely enqueued work
    if reply.ok and chat.notion_ready and mapped.command.split()[0].lower() not in {
        "help",
        "status",
        "queue",
        "companies",
        "ask",
        "process",
    }:
        process_reply = chat.handle("process")
        reply = ChatReply(
            text=f"{reply.text}\n\n{process_reply.text}",
            ok=reply.ok and process_reply.ok,
        )

    notion_url: str | None = None
    if chat.notion_ready:
        try:
            _page_id, notion_url = write_run_output_page(
                chat.notion(),
                command=mapped.command,
                reply=reply,
                chat_id=chat_id,
                message_id=message_id,
                nl_source=mapped.source,
            )
        except NotionError as exc:
            logger.exception("Failed to write Notion output page: %s", exc)
            reply = ChatReply(
                text=f"{reply.text}\n\n(Notion output page failed: {exc})",
                ok=False,
            )

    summary = reply.text.strip()
    if len(summary) > 2800:
        summary = summary[:2800] + "…"
    lines = [summary]
    if notion_url:
        lines.append("")
        lines.append(f"Open in Notion: {notion_url}")
    telegram.send_message(
        chat_id,
        "\n".join(lines),
        reply_to_message_id=message_id,
        disable_web_page_preview=False,
    )


def process_update(
    update: dict[str, Any],
    *,
    settings: Settings,
    chat: AetherChat,
    telegram: TelegramClient,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    chat_obj = message.get("chat") or {}
    chat_id = chat_obj.get("id")
    message_id = message.get("message_id")
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return
    try:
        handle_telegram_text(
            settings=settings,
            chat=chat,
            telegram=telegram,
            text=text,
            chat_id=chat_id,
            message_id=message_id,
        )
    except (TelegramError, NotionError, Exception):
        logger.exception("Failed handling Telegram update_id=%s", update.get("update_id"))
        try:
            telegram.send_message(
                chat_id,
                "Something went wrong processing that message. Check ECS logs.",
                reply_to_message_id=message_id,
            )
        except TelegramError:
            logger.exception("Also failed to send error reply")


class TelegramPoller:
    """Background long-poller suitable for the ECS chat service."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._chat: AetherChat | None = None
        self._telegram: TelegramClient | None = None

    def start(self) -> None:
        if not self.settings.telegram_ready():
            logger.info(
                "Telegram poller not started "
                "(need TELEGRAM_ENABLED=true, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS)"
            )
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._chat = AetherChat(self.settings)
        self._telegram = TelegramClient(self.settings)
        self._thread = threading.Thread(
            target=self._run_loop,
            name="aether-telegram-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram poller started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._telegram is not None:
            self._telegram.close()
            self._telegram = None
        if self._chat is not None:
            self._chat.close()
            self._chat = None
        logger.info("Telegram poller stopped")

    def _run_loop(self) -> None:
        assert self._chat is not None and self._telegram is not None
        while not self._stop.is_set():
            try:
                updates = self._telegram.get_updates(timeout=25)
            except TelegramError as exc:
                logger.warning("Telegram getUpdates failed: %s", exc)
                time.sleep(3)
                continue
            except Exception:
                logger.exception("Unexpected Telegram poll error")
                time.sleep(5)
                continue
            for update in updates:
                if self._stop.is_set():
                    break
                process_update(
                    update,
                    settings=self.settings,
                    chat=self._chat,
                    telegram=self._telegram,
                )
