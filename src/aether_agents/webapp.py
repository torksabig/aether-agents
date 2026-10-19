"""Hosted browser chat for Aether agents.

Run:
  aether-web
  # → http://127.0.0.1:3847
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aether_agents.chat import AetherChat
from aether_agents.config import get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    ok: bool
    notion_ready: bool


class StatusResponse(BaseModel):
    notion_ready: bool
    dry_run: bool
    queued: int | None
    brand: str = "Aether Applied"


class AgentRunRequest(BaseModel):
    """Trigger one pipeline agent via AWS route (Notion control plane)."""

    target: str = Field(min_length=1, max_length=500)
    domain: str | None = None
    source_url: str | None = None
    questions: list[str] | None = None


class AgentRunResponse(BaseModel):
    ok: bool
    agent: str
    target: str
    reply: str
    notion_ready: bool


# Maps public AWS route keys → chat CLI verbs that queue/run that agent.
_AGENT_ROUTE_COMMANDS: dict[str, str] = {
    "lead": "lead",
    "research": "research",
    "qualify": "qualify",
    "feasibility": "feasibility",
    "funding": "funding",
}


def create_app() -> FastAPI:
    app = FastAPI(title="Aether Agents Chat", version="0.1.0")
    chat = AetherChat(get_settings())

    @app.on_event("shutdown")
    def _shutdown() -> None:
        chat.close()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status", response_model=StatusResponse)
    def status() -> StatusResponse:
        queued: int | None = None
        if chat.notion_ready:
            try:
                reply = chat.handle("status")
                # status text includes "Queued tasks: N"
                for line in reply.text.splitlines():
                    if "Queued tasks:" in line:
                        try:
                            queued = int(line.split(":")[-1].strip())
                        except ValueError:
                            queued = None
            except Exception:
                queued = None
        return StatusResponse(
            notion_ready=chat.notion_ready,
            dry_run=bool(chat.settings.agent_dry_run),
            queued=queued,
        )

    @app.post("/api/chat", response_model=ChatResponse)
    async def api_chat(body: ChatRequest) -> ChatResponse:
        message = body.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="Empty message")
        # Agent + Notion calls are sync/blocking — run off the event loop
        reply = await asyncio.to_thread(chat.handle, message)
        return ChatResponse(
            reply=reply.text,
            ok=reply.ok,
            notion_ready=chat.notion_ready,
        )

    @app.post("/api/agents/{agent_key}/run", response_model=AgentRunResponse)
    async def run_agent(agent_key: str, body: AgentRunRequest) -> AgentRunResponse:
        """Select one agent module inside this ECS service (not a separate AWS service)."""
        key = agent_key.strip().lower()
        if key not in _AGENT_ROUTE_COMMANDS:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown agent '{agent_key}'. Use: {', '.join(sorted(_AGENT_ROUTE_COMMANDS))}",
            )
        verb = _AGENT_ROUTE_COMMANDS[key]
        parts = [verb, body.target.strip()]
        if body.domain:
            parts.append(body.domain.strip())
        if body.source_url:
            parts.append(body.source_url.strip())
        message = " ".join(parts)
        reply = await asyncio.to_thread(chat.handle, message)
        # Drain one Queued batch so AWS execution actually runs after enqueue
        if reply.ok and chat.notion_ready:
            process_reply = await asyncio.to_thread(chat.handle, "process")
            text = f"{reply.text}\n\n{process_reply.text}"
            ok = reply.ok and process_reply.ok
        else:
            text = reply.text
            ok = reply.ok
        return AgentRunResponse(
            ok=ok,
            agent=key,
            target=body.target,
            reply=text,
            notion_ready=chat.notion_ready,
        )

    @app.get("/api/agents")
    def list_agents() -> dict[str, object]:
        base = "http://aether-agents-alb-1745455692.eu-north-1.elb.amazonaws.com"
        return {
            "service": "aether-agents-chat",
            "note": "One ECS service; agents selected by route",
            "agents": [
                {"key": k, "route": f"{base}/api/agents/{k}/run"}
                for k in sorted(_AGENT_ROUTE_COMMANDS)
            ],
        }

    @app.get("/")
    def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="Chat UI missing")
        return FileResponse(index_path)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "aether_agents.webapp:app",
        host="0.0.0.0",
        port=3847,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
