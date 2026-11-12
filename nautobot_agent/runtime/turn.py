"""Runtime-owned operations for one guarded chat send."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from langchain_core.runnables import RunnableConfig

from nautobot_agent import app_settings
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.runtime.budgets import ModelCallBudgetCallback, RunBudget
from nautobot_agent.runtime.checkpointer import ConversationWriteGuard, attach_run_guard, thread_config
from nautobot_agent.runtime.context import AgentContext, RunAuthority
from nautobot_agent.runtime.tracing import ExecutionTrace

if TYPE_CHECKING:
    from nautobot_agent.models import AgentConversation
    from nautobot_agent.runtime.agent import AgentGraph, AgentRuntime


class TurnGuard(RunAuthority, ConversationWriteGuard, Protocol):
    """One run owner admitting work and fencing durable conversation writes."""


class ScopeScreen(NamedTuple):
    """Scope decision and the actual retained-history refusal anchor."""

    allowed: bool
    anchor_id: str


@dataclass(frozen=True)
class AgentTurn:  # pylint: disable=too-many-instance-attributes
    """Screen scope, prepare a graph, and bind invocation inputs for one run."""

    runtime: AgentRuntime
    user: Any
    conversation: AgentConversation
    prompt: str
    guard: TurnGuard
    mcp_turn: MCPTurn
    trace: ExecutionTrace
    budget: RunBudget = dataclass_field(init=False)
    model_callback: ModelCallBudgetCallback = dataclass_field(init=False)

    def __post_init__(self) -> None:
        """Snapshot fresh work ceilings and callback before screening or graph work."""
        budget = RunBudget(
            tool_limit=app_settings.chat_max_tool_calls(),
            model_limit=app_settings.chat_max_model_calls(),
        )
        object.__setattr__(self, "budget", budget)
        object.__setattr__(
            self, "model_callback", ModelCallBudgetCallback(budget, on_rejected=self.trace.discard_model_start)
        )

    def model_config(self) -> RunnableConfig:
        """Compose the run-local model reservation before optional trace callbacks."""
        return self.trace.config({"callbacks": [self.model_callback]})

    def screen(self) -> ScopeScreen:
        """Apply the configured scope policy with authority checks around classification."""
        mode = app_settings.scope_mode()
        if mode is app_settings.ScopeMode.OFF:
            return ScopeScreen(True, "")
        messages = self.runtime.read_conversation_values(self.conversation)["messages"]
        self.guard.require_new_work()
        decision = self.runtime.classify_scope(
            self.prompt,
            messages,
            topic_switch_only=mode is app_settings.ScopeMode.CONVERSATION,
            config=self.model_config(),
        )
        self.guard.require_new_work()
        anchor_id = (messages[-1].id or "") if messages else ""
        return ScopeScreen(decision.allowed, anchor_id)

    def prepare(self) -> AgentGraph:
        """Look up the run's graph with authority checks around graph construction."""
        self.guard.require_new_work()
        agent_graph = self.runtime.graph(self.user, mcp_turn=self.mcp_turn)
        self.guard.require_new_work()
        return agent_graph

    def invocation(self) -> tuple[RunnableConfig, AgentContext]:
        """Return the guarded graph config and trusted context for this run's stream."""
        config = self.trace.config({"callbacks": [self.model_callback], **thread_config(self.conversation.pk)})
        config = attach_run_guard(config, self.guard)
        context = AgentContext(
            user_id=str(self.user.pk),
            mcp_turn=self.mcp_turn,
            authority=self.guard,
            budget=self.budget,
        )
        return config, context
