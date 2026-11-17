"""Projection of live Deep Agent turns into chat page events."""

import copy
import logging
from collections.abc import Callable, Generator
from typing import Any, cast

from celery.exceptions import SoftTimeLimitExceeded
from langchain_core.runnables import RunnableConfig

from nautobot_agent.chat.errors import safe_error_diagnostic
from nautobot_agent.chat.history import ToolEntry, context_usage, project_tool_entry, tool_entries
from nautobot_agent.runtime.agent import read_state
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import RunDraining
from nautobot_agent.runtime.errors import model_failure

logger = logging.getLogger("nautobot_agent.chat_stream")


MESSAGE_START = "message_start"
# Keep the public event name distinct from model/tool payload fields.
TOKEN = "token"  # noqa: S105
CONTEXT = "context"
TOOL_CALL = "tool_call"
TODOS = "todos"
DONE = "done"
ERROR = "error"

Event = tuple[str, dict[str, Any]]


def refusal_events(text: str) -> Generator[Event, None, None]:
    """Yield the fixed denied-turn sequence; no graph, checkpoint, or session access."""
    yield MESSAGE_START, {}
    yield TOKEN, {"text": text}
    yield DONE, {}


class _TurnProjection:
    """Project one send's graph stream items into page events, emitting each tool status change once."""

    def __init__(self, control: Any, historical: set[str], limit: int) -> None:
        self._control = control
        self._historical = historical
        self._limit = limit
        self._todos: list[dict[str, Any]] | None = None
        self._emitted: dict[str, ToolEntry] = {}

    def check(self) -> None:
        """Stop projection once worker shutdown asks running turns to drain."""
        if self._control.drain_requested:
            raise RunDraining("This execution is shutting down.")

    def values(self, item: dict[str, Any]) -> Generator[Event, None, None]:
        """Yield changed todos, then each current-send tool call whose status advanced."""
        todos = item.get("todos", [])
        if self._todos is None or todos != self._todos:
            self._todos = [dict(todo) for todo in todos]
            yield TODOS, {"todos": todos}
        for _, entry in tool_entries(item.get("messages", []), "running"):
            sent = self._advance(entry)
            if sent is None:
                continue
            self.check()
            yield TOOL_CALL, cast(dict[str, Any], sent)

    def _advance(self, entry: ToolEntry) -> ToolEntry | None:
        """Record a newly seen or newly settled call; None when the page already shows its status."""
        if entry["id"] in self._historical:
            return None
        sent = self._emitted.get(entry["id"])
        if sent is not None and (sent["status"] != "running" or entry["status"] == "running"):
            return None
        if sent is None:
            sent = self._emitted[entry["id"]] = copy.deepcopy(entry)
        sent["status"] = entry["status"]
        if sent["status"] == "completed":
            sent.update(project_tool_entry(sent))
        return sent

    def model(self, item: Any) -> Generator[Event, None, None]:
        """Yield one model message's text deltas and its context usage."""
        started = False
        for delta in item.text:
            self.check()
            if not delta:
                continue
            if not started:
                started = True
                yield MESSAGE_START, {}
            yield TOKEN, {"text": delta}
        output = getattr(item, "output", None)
        usage = getattr(output, "usage_metadata", None)
        yield CONTEXT, context_usage(usage, self._limit)


def stream_chat_events(  # noqa: PLR0913  # one turn's inputs plus keyword-only lifecycle hooks
    agent: Any,
    text: str,
    config: RunnableConfig,
    context: AgentContext,
    limit: int,
    *,
    control: Any,
    on_cleanup: Callable[[], object],
) -> Generator[Event, None, None]:
    """Yield the page's live event sequence for one agent turn."""
    stream = None
    try:
        # Calls already retained in the checkpoint belong to earlier sends and are never replayed.
        retained = read_state(agent, config)["messages"]
        projection = _TurnProjection(control, {entry["id"] for _, entry in tool_entries(retained, "unknown")}, limit)
        stream = agent.stream_events(
            {
                "messages": [{"role": "user", "content": text}],
                "skills_metadata": None,
            },
            config,
            context=context,
            version="v3",
            durability="sync",
            control=control,
        )
        for kind, item in stream.interleave("messages", "values"):
            projection.check()
            if kind == "values":
                yield from projection.values(item)
            elif item.node == "model":
                yield from projection.model(item)
            projection.check()
        yield DONE, {}
    except (RunDraining, SoftTimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001  # any turn failure ends the page stream with a safe error event
        logger.error("Error streaming Agent response: %s", safe_error_diagnostic(exc))
        failure = model_failure(exc)
        yield ERROR, {"error_code": failure.code if failure is not None else type(exc).__name__}
    finally:
        if stream is not None:
            on_cleanup()
            # Public, idempotent abort; it suppresses ordinary iterator/mux close failures.
            stream.abort()
