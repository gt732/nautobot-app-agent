"""Cooperative admission control for model and tool work in a live run."""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langgraph.errors import GraphBubbleUp

from nautobot_agent.db_connections import close_connections


class ConversationUnavailable(Exception):
    """The conversation's run authority or supporting state is unavailable."""


class RunDraining(GraphBubbleUp):
    """Raised when a run has stopped admitting new work."""


_DRAINING = "Execution authority is unavailable."


def _require_new_work(runtime) -> None:
    """Raise RunDraining when the run's authority no longer admits new work."""
    try:
        runtime.context.authority.require_new_work()
    except ConversationUnavailable:
        raise RunDraining(_DRAINING) from None
    runtime.context.budget.require_available()


class RunControlMiddleware(AgentMiddleware):  # pylint: disable=too-few-public-methods
    """Admit synchronous graph model/tool work; asynchronous invocation is unsupported."""

    def wrap_model_call(self, request, handler):
        """Admit a synchronous model invocation under current authority."""
        try:
            _require_new_work(request.runtime)
            return handler(request)
        finally:
            close_connections()

    def wrap_tool_call(self, request, handler):
        """Admit a synchronous tool without repairing a control refusal."""
        try:
            _require_new_work(request.runtime)
            request.runtime.context.budget.reserve_tool_call()
            return handler(request)
        finally:
            close_connections()
