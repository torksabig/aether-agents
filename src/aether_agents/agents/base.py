"""Common agent interface. Agents never mutate peer results directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from aether_agents.models import AgentInput, AgentName, AgentResult


class BaseAgent(ABC):
    """Every agent implements the same input/output contract."""

    name: ClassVar[AgentName]

    @abstractmethod
    def run(self, payload: AgentInput) -> AgentResult:
        """Execute business logic and return a validated AgentResult."""

    def __call__(self, payload: AgentInput) -> AgentResult:
        result = self.run(payload)
        if result.agent != self.name:
            raise ValueError(f"Agent {self.name} produced result for agent {result.agent}")
        if result.opportunity_id != payload.context.opportunity_id:
            raise ValueError("Result opportunity_id must match input context")
        return result
