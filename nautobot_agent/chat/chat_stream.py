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


# Keep event projection and its two finalization branches together so cleanup ordering stays explicit.
def stream_chat_events(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-statements
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
    previous = None
    emitted: dict[str, ToolEntry] = {}
    stream = None

    def check() -> None:
        if control.drain_requested:
            raise RunDraining("This execution is shutting down.")

    try:
        # Calls already retained in the checkpoint belong to earlier sends and are never replayed.
        retained = read_state(agent, config)["messages"]
        historical = {entry["id"] for _, entry in tool_entries(retained, "unknown")}
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
        for projection, item in stream.interleave("messages", "values"):
            check()
            if projection == "values":
                todos = item.get("todos", [])
                if previous is None or todos != previous:
                    previous = [dict(todo) for todo in todos]
                    yield TODOS, {"todos": todos}
                for _, entry in tool_entries(item.get("messages", []), "running"):
                    if entry["id"] in historical:
                        continue
                    sent = emitted.get(entry["id"])
                    if sent is not None and (sent["status"] != "running" or entry["status"] == "running"):
                        continue
                    if sent is None:
                        sent = emitted[entry["id"]] = copy.deepcopy(entry)
                    sent["status"] = entry["status"]
                    if sent["status"] == "completed":
                        sent.update(project_tool_entry(sent))
                    check()
                    yield TOOL_CALL, cast(dict[str, Any], sent)
            elif item.node == "model":
                started = False
                for delta in item.text:
                    check()
                    if not delta:
                        continue
                    if not started:
                        started = True
                        yield MESSAGE_START, {}
                    yield TOKEN, {"text": delta}
                output = getattr(item, "output", None)
                usage = getattr(output, "usage_metadata", None)
                yield CONTEXT, context_usage(usage, limit)
            check()
        yield DONE, {}
    except (RunDraining, SoftTimeLimitExceeded):
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("Error streaming Agent response: %s", safe_error_diagnostic(exc))
        failure = model_failure(exc)
        yield ERROR, {"error_code": failure.code if failure is not None else type(exc).__name__}
    finally:
        if stream is not None:
            on_cleanup()
            # Public, idempotent abort; it suppresses ordinary iterator/mux close failures.
            stream.abort()
