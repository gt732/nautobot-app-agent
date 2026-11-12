"""Trusted runtime context shared by graph tools and per-run resources."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.runtime.budgets import RunBudget


# Tool schemas validate AgentContext with pydantic, which needs an isinstance-capable annotation.
@runtime_checkable
class RunAuthority(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal invocation-local authority required before new model/tool work."""

    def require_new_work(self) -> None:
        """Raise when this execution may no longer start sensitive work."""
        ...


@dataclass(frozen=True)
class AgentContext:
    """Trusted per-run context; ``user_id`` is the authenticated request user's primary key."""

    user_id: str
    authority: RunAuthority
    budget: RunBudget
    mcp_turn: MCPTurn | None = None
