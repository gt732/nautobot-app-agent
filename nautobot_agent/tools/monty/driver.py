"""Single-pass, synchronous execution of one ``eval_python`` call on a Monty worker.

Adapted from ``langchain-monty`` (``middleware/_driver.py`` and the session
configuration in ``middleware/monty_code_interpreter_middleware.py``), MIT
License, Copyright (c) 2026 Shane Smithrand; see the ``LICENSE`` file in this
directory.

Each call owns one scoped single-worker ``Monty`` pool and one fresh session, so
no globals, workers, or pools exist at import time or survive the call. The
session is driven eagerly: every ``FunctionSnapshot`` for the query tool runs the
trusted bridge immediately and resumes the sandbox with its payload. Ordinary
failures become the structured error envelope; ``GraphBubbleUp`` (run draining,
interrupts) and Celery's ``SoftTimeLimitExceeded`` propagate untouched, and the
``with`` blocks release the session and pool on every exit.
"""

from typing import Any, cast

from celery.exceptions import SoftTimeLimitExceeded
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool
from langgraph.errors import GraphBubbleUp
from pydantic_monty import (
    NOT_HANDLED,
    CollectString,
    FunctionSnapshot,
    Monty,
    MontyComplete,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    NameLookupSnapshot,
    OSAccess,
    OsFunction,
    ResourceLimits,
    SyncSnapshot,
)

from nautobot_agent.app_settings import InterpreterLimits
from nautobot_agent.tools.monty.bridge import invoke_query_tool
from nautobot_agent.tools.monty.rendering import render_type_stubs
from nautobot_agent.tools.monty.results import error_result, success_result

MAX_RECURSION_DEPTH = 256


def _resource_limits(limits: InterpreterLimits) -> ResourceLimits:
    """Map app limits onto Monty's per-feed execution, memory, and recursion ceilings."""
    return ResourceLimits(
        max_feed_duration_secs=limits.max_duration_secs,
        max_memory=limits.max_memory_bytes,
        max_recursion_depth=MAX_RECURSION_DEPTH,
    )


def _budget_error(max_host_calls: int, code: str) -> dict[str, Any]:
    return error_result(
        "IterationBudgetExceeded",
        f"interpreter made more than {max_host_calls} host-tool calls in a single eval_python call",
        code=code,
    )


def _exception_error(exc: Exception, code: str) -> dict[str, Any]:
    """Map a Monty or worker exception to the structured error envelope."""
    error_type = type(exc).__name__
    message = str(exc)
    traceback_text: str | None = None
    if isinstance(exc, MontyRuntimeError):
        inner = exc.exception()
        error_type = type(inner).__name__
        message = str(inner)
        traceback_text = exc.display("traceback")
    elif isinstance(exc, MontyTypingError):
        error_type = "TypeCheckError"
        message = "static type check failed before execution; no code was run"
        traceback_text = exc.display()
    elif isinstance(exc, MontySyntaxError):
        error_type = "SyntaxError"
        message = exc.display("msg")
    return error_result(error_type, message, traceback=traceback_text, code=code)


def _resume_os_call(progress: FunctionSnapshot, os_handler: OSAccess) -> SyncSnapshot:
    """Answer a sandbox OS call from the empty virtual OS and resume the sandbox.

    Monty consults ``os=`` only from ``resume_auto()``, so a manually driven feed
    dispatches OS calls itself. A raising handler is reported to the sandbox.
    """
    try:
        value = os_handler(
            name=cast(OsFunction, progress.function_name),
            args=progress.args,
            kwargs=progress.kwargs,
            is_async=False,
        )
    except (GraphBubbleUp, SoftTimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001  # sandbox OS failures resume as exceptions
        return progress.resume({"exception": exc})
    if value is NOT_HANDLED:
        return progress.resume_not_handled()
    return progress.resume({"return_value": value})


def run_eval_python(code: str, limits: InterpreterLimits, runtime: ToolRuntime, tool: BaseTool) -> dict[str, Any]:
    """Run ``code`` once in a fresh Monty session and return the four-field result envelope.

    ``tool`` is the only host function the sandbox may call, at most
    ``limits.max_host_calls`` times; the call after that is refused before its
    handler runs. ``runtime`` is the trusted runtime the bridge attaches to each
    call. Ordinary failures return the error envelope; ``GraphBubbleUp`` and
    ``SoftTimeLimitExceeded`` propagate.
    """
    try:
        stubs = render_type_stubs(tool)
        with (
            Monty(min_processes=1, max_processes=1) as pool,
            pool.checkout(
                limits=_resource_limits(limits),
                type_check=True,
                type_check_stubs=stubs,
                type_check_format="concise",
                os_policy={"sleep": "zero"},
            ) as session,
        ):
            return _drive_session(session, code, limits, runtime, tool)
    except (GraphBubbleUp, SoftTimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001  # user-code and worker failures become the tool's error envelope
        return _exception_error(exc, code)


def _drive_session(
    session, code: str, limits: InterpreterLimits, runtime: ToolRuntime, tool: BaseTool
) -> dict[str, Any]:
    """Feed ``code`` to a checked-out session, answering each snapshot until it completes."""

    def host_tool(*_args: Any, **_kwargs: Any) -> Any:
        # Only its name crosses into Monty; dispatch must use the guarded bridge below.
        raise RuntimeError("sandbox host-function stand-in must not execute")

    host_tool.__name__ = tool.name
    stdout = CollectString()
    # Empty virtual filesystem and environment; no mounts or real OS access.
    os_handler = OSAccess(max_sleep=0)
    host_calls = 0
    progress = session.feed_start(code, print_callback=stdout, os=os_handler)
    while not isinstance(progress, MontyComplete):
        if isinstance(progress, NameLookupSnapshot):
            is_tool = progress.object_id is None and progress.variable_name == tool.name
            progress = progress.resume(value=host_tool) if is_tool else progress.resume()
        elif not isinstance(progress, FunctionSnapshot):
            return error_result(
                "RuntimeError",
                f"unsupported sandbox progress {type(progress).__name__}; use plain synchronous code",
                code=code,
            )
        elif progress.is_os_function:
            progress = _resume_os_call(progress, os_handler)
        elif progress.function_name != tool.name:
            progress = progress.resume(
                {
                    "exc_type": "RuntimeError",
                    "message": f"host function {progress.function_name!r} is not available; available: [{tool.name!r}]",
                }
            )
        elif host_calls >= limits.max_host_calls:
            return _budget_error(limits.max_host_calls, code)
        else:
            host_calls += 1
            progress = progress.resume(invoke_query_tool(tool, progress.args, progress.kwargs, runtime))
    # The final value converts on access and stdout lives in the session's
    # worker callbacks: read both before the session is released.
    return success_result(progress.output, stdout.output or "")
