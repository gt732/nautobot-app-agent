"""Trusted invocation of the query tool on behalf of sandbox code.

Adapted from ``langchain-monty`` (``middleware/_bridge.py``), MIT License,
Copyright (c) 2026 Shane Smithrand; see the ``LICENSE`` file in this directory.

The sandbox supplies only the model-visible arguments. Anything it passes under
an injected name (``runtime``, ``config``, ``tool_call_id``...) is discarded; the
trusted ``ToolRuntime`` and its configuration are attached here, so identity and
run context can never originate from sandbox input. Every ordinary outcome maps
to a resume payload; run-control exceptions propagate to the caller.
"""

import json
import uuid
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from pydantic_monty import ExternalException, ExternalReturnValue

from nautobot_agent.tools.monty.rendering import INJECTED_PARAM_NAMES, visible_fields


def _normalize_call_args(tool: BaseTool, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Merge positional and keyword sandbox arguments into one keyword mapping.

    Positional values fill the tool's visible fields in declaration order. Sandbox
    values for injected names are dropped, never forwarded.
    """
    fields = visible_fields(tool)
    if len(args) > len(fields):
        raise TypeError(f"{tool.name}() takes at most {len(fields)} positional arguments ({len(args)} given)")
    merged = dict(zip(fields, args, strict=False))
    for name, value in kwargs.items():
        if name in INJECTED_PARAM_NAMES:
            continue
        if name in merged:
            raise TypeError(f"{tool.name}() got multiple values for argument {name!r}")
        merged[name] = value
    return merged


def _tool_value(raw: Any) -> Any:
    """Convert a tool result into the value the sandbox call returns.

    A handled-error ``ToolMessage`` raises so the caller maps it to a sandbox
    exception. A ``Command`` cannot be applied from inside a tool and is rejected.
    """
    if isinstance(raw, Command):
        raise RuntimeError("Graph-state updates cannot be returned to sandbox code; call the tool directly instead")
    if isinstance(raw, ToolMessage):
        text = raw.content if isinstance(raw.content, str) else raw.text
        if raw.status == "error":
            raise RuntimeError(text)
        if raw.artifact is not None:
            return raw.artifact
        raw = text
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def invoke_query_tool(
    tool: BaseTool, args: tuple[Any, ...], kwargs: dict[str, Any], runtime: ToolRuntime
) -> ExternalReturnValue | ExternalException:
    """Run the query tool once and return the payload that resumes the sandbox call.

    Returns ``{"return_value": value}`` on success or ``{"exception": exc}`` for
    handled failures and ordinary errors. ``GraphBubbleUp`` (including run
    draining and interrupts) and Celery's ``SoftTimeLimitExceeded`` propagate.
    """
    try:
        call = {
            "type": "tool_call",
            "name": tool.name,
            "id": f"eval_python:{uuid.uuid4().hex[:12]}",
            "args": {**_normalize_call_args(tool, args, kwargs), "runtime": runtime},
        }
        return {"return_value": _tool_value(tool.invoke(call, config=runtime.config or None))}
    except (GraphBubbleUp, SoftTimeLimitExceeded):
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"exception": exc}
