"""Sole stateless Monty interpreter middleware for the main agent.

The execution driver, trusted query bridge, schema rendering and result envelopes
live beside this module; this class wires them to the agent graph, owns the
``eval_python`` tool and prompt, and bounds the tool result.

Tool registration and model-request hooks adapted from ``langchain-monty``
(``middleware/monty_code_interpreter_middleware.py``), MIT License, Copyright
(c) 2026 Shane Smithrand; see the ``LICENSE`` file in this directory.
"""

import json
from collections.abc import Callable
from typing import Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from nautobot_agent.app_settings import interpreter_limits
from nautobot_agent.data.query import DEFAULT_LIMIT
from nautobot_agent.prompting.definitions import interpreter_prompt, interpreter_tool_description
from nautobot_agent.tools.monty.driver import run_eval_python
from nautobot_agent.tools.monty.rendering import append_to_system_message, format_description, format_tool_schema
from nautobot_agent.tools.query_records import (
    INTERPRETER_MAX_LIMIT,
    MAX_LIMIT,
    interpreter_query_records,
)

_DIAGNOSTIC_TRUNCATION_MARKER = "\n...[diagnostics truncated]"


def _failure_payload(error: dict[str, Any]) -> dict[str, Any]:
    return {"result": None, "stdout": "", "error": error, "attempted_code": None}


def _bounded_failure(content: str, limit: int) -> str | None:
    """Keep the original interpreter error while removing bulky code/output and bounding diagnostics."""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("type"), str):
        return None
    error = {
        "type": error["type"][:128],
        "message": error.get("message", ""),
        "traceback": error.get("traceback"),
    }
    payload = _failure_payload(error)
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    texts = {key: value for key, value in error.items() if key != "type" and isinstance(value, str)}
    low, high = 0, max((len(value) for value in texts.values()), default=0)
    best = None
    while low <= high:
        middle = (low + high) // 2
        for key, value in texts.items():
            error[key] = value[:middle] + _DIAGNOSTIC_TRUNCATION_MARKER if len(value) > middle else value
        candidate = json.dumps(payload, separators=(",", ":"))
        if len(candidate) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


class InterpreterMiddleware(AgentMiddleware):
    """Configure the read-only Python interpreter and bound its tool results."""

    def __init__(self):
        """Apply configured limits, query access, and interpreter prompts."""
        super().__init__()
        limits = interpreter_limits()
        prompt_args = {
            "default_limit": DEFAULT_LIMIT,
            "direct_max_limit": MAX_LIMIT,
            "interpreter_max_limit": INTERPRETER_MAX_LIMIT,
            "max_host_calls": limits.max_host_calls,
        }
        query_tool = interpreter_query_records
        self._system_prompt = (
            interpreter_prompt(**prompt_args)
            + "\n\nHost functions exposed to the interpreter:\n\n"
            + format_tool_schema(query_tool)
        )

        def eval_python(
            code: Annotated[
                str,
                "Python source to execute in the Monty sandbox. The value of the final expression is "
                "returned as ``result``. Stdout is captured. Exceptions return as a structured ``error``.",
            ],
            runtime: ToolRuntime,
        ) -> dict[str, Any]:
            return run_eval_python(code, limits, runtime, query_tool)

        self.tools = [
            StructuredTool.from_function(
                name="eval_python",
                func=eval_python,
                description=format_description(interpreter_tool_description(**prompt_args), query_tool, limits),
            )
        ]
        self._max_output_chars = limits.max_output_chars

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Append the interpreter prompt and full host-function schemas to the system message."""
        system_message = append_to_system_message(request.system_message, self._system_prompt)
        return handler(request.override(system_message=system_message))

    def _cap_result(self, request: ToolCallRequest, result: ToolMessage | Command[Any]) -> ToolMessage | Command[Any]:
        if (
            request.tool_call.get("name") != "eval_python"
            or not isinstance(result, ToolMessage)
            or not isinstance(result.content, str)
            or len(result.content) <= self._max_output_chars
        ):
            return result
        failure = _bounded_failure(result.content, self._max_output_chars)
        if failure is not None:
            return result.model_copy(update={"content": failure})
        size = len(result.content)
        payload = _failure_payload(
            {
                "type": "OutputLimitExceeded",
                "message": (
                    f"Interpreter output was {size} characters, exceeding the "
                    f"{self._max_output_chars}-character limit. Return a smaller summary."
                ),
            }
        )
        content = json.dumps(payload, separators=(",", ":"))
        return result.model_copy(update={"content": content})

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Cap oversized synchronous interpreter results."""
        return self._cap_result(request, handler(request))
