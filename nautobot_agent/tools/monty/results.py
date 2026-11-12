"""Structured ``eval_python`` results and JSON-safe sandbox value conversion.

Adapted from ``langchain-monty`` (``models/eval_result.py`` and
``helpers/__init__.py``), MIT License, Copyright (c) 2026 Shane Smithrand; see
the ``LICENSE`` file in this directory.
"""

import base64
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalError(BaseModel):
    """Structured error payload returned in ``EvalPythonResult.error``.

    ``type`` is the real sandbox exception class for runtime errors (including
    resource exhaustion), ``SyntaxError`` for parse failures, ``TypeCheckError``
    for static type-check failures, or a host-enforced name such as
    ``IterationBudgetExceeded``. ``traceback`` carries richer diagnostics when
    available and is ``None`` otherwise.
    """

    type: str = Field(description="Exception/error class name.")
    message: str = Field(description="Human-readable error message.")
    traceback: str | None = Field(
        default=None,
        description="Formatted diagnostics when available, otherwise ``None``.",
    )

    model_config = ConfigDict(extra="forbid")


class EvalPythonResult(BaseModel):
    """Four-field return shape for the ``eval_python`` tool."""

    result: Any = Field(
        default=None,
        description="Value of the final expression; ``None`` without one or when ``error`` is set.",
    )
    stdout: str = Field(default="", description="Captured stdout from the sandboxed run.")
    error: EvalError | None = Field(default=None, description="Structured error payload, or ``None`` on success.")
    attempted_code: str | None = Field(
        default=None,
        description="Submitted code; populated only when ``error`` is set.",
    )

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


def success_result(value: Any, stdout: str) -> dict[str, Any]:
    """Return the envelope for a completed run; ``value`` must already be read from the live session."""
    return EvalPythonResult(result=jsonable_output(value), stdout=stdout).model_dump()


def error_result(error_type: str, message: str, *, traceback: str | None = None, code: str = "") -> dict[str, Any]:
    """Return the envelope for a failed run, echoing the submitted ``code`` as ``attempted_code``."""
    return EvalPythonResult(
        error=EvalError(type=error_type, message=message, traceback=traceback),
        attempted_code=code or None,
    ).model_dump()


def jsonable_output(output: Any) -> Any:
    """Return a sandbox final value in JSON-safe form.

    Values ``json.dumps`` accepts are returned untouched. Otherwise they are
    re-encoded into a tagged form (``{"$tuple": [...]}``, ``{"$set": [...]}``,
    ...) so rich values stay meaningful instead of failing later in message
    serialization. The value must be read from the VM while its session is live.
    """
    try:
        json.dumps(output)
    except (TypeError, ValueError):
        return _tagged(output)
    return output


def _tagged(value: Any) -> Any:  # pylint: disable=too-many-return-statements
    """Recursively re-encode values JSON cannot express; unknown values degrade to ``repr``."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        return {"$tuple": [_tagged(item) for item in value]}
    if isinstance(value, frozenset):
        return {"$frozenset": [_tagged(item) for item in value]}
    if isinstance(value, set):
        return {"$set": [_tagged(item) for item in value]}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, list):
        return [_tagged(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: _tagged(item) for key, item in value.items()}
        return {"$dict": [[_tagged(key), _tagged(item)] for key, item in value.items()]}

    # Dataclass instances arrive as objects exposing a name/attributes pair.
    name = getattr(value, "name", None)
    attributes = getattr(value, "attributes", None)
    if isinstance(name, str) and isinstance(attributes, dict):
        return {"$object": {"name": name, "attributes": {key: _tagged(item) for key, item in attributes.items()}}}
    return repr(value)
