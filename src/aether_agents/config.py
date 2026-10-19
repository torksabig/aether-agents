"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Secrets never logged."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    notion_token: str = Field(default="", alias="NOTION_TOKEN")
    notion_tasks_data_source_id: str = Field(default="", alias="NOTION_TASKS_DATA_SOURCE_ID")
    notion_companies_data_source_id: str = Field(
        default="", alias="NOTION_COMPANIES_DATA_SOURCE_ID"
    )
    notion_research_data_source_id: str = Field(default="", alias="NOTION_RESEARCH_DATA_SOURCE_ID")
    notion_projects_data_source_id: str = Field(default="", alias="NOTION_PROJECTS_DATA_SOURCE_ID")
    notion_funding_data_source_id: str = Field(default="", alias="NOTION_FUNDING_DATA_SOURCE_ID")
    notion_command_center_page_id: str = Field(default="", alias="NOTION_COMMAND_CENTER_PAGE_ID")
    notion_version: str = Field(default="2025-09-03", alias="NOTION_VERSION")
    notion_max_rps: float = Field(default=2.5, alias="NOTION_MAX_RPS")

    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")

    agent_batch_limit: int = Field(default=10, alias="AGENT_BATCH_LIMIT", ge=1, le=50)
    agent_llm_retry_limit: int = Field(default=1, alias="AGENT_LLM_RETRY_LIMIT", ge=0, le=3)
    agent_token_budget: int = Field(default=8000, alias="AGENT_TOKEN_BUDGET", ge=500, le=100_000)
    agent_dry_run: bool = Field(default=False, alias="AGENT_DRY_RUN")

    def require_notion(self) -> None:
        if not self.notion_token:
            raise ValueError("NOTION_TOKEN is required")
        if not self.notion_tasks_data_source_id:
            raise ValueError("NOTION_TASKS_DATA_SOURCE_ID is required")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
