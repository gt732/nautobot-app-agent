"""Invocation-local admission budget shared by all model and tool work in one run."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphBubbleUp


class RunBudgetExceeded(GraphBubbleUp):
    """A static control-flow failure raised when a per-run work ceiling is exhausted."""

    def __init__(self, code: str):
        """Create a payload-free budget refusal."""
        if code not in {"run_tool_call_limit", "run_model_call_limit"}:
            raise ValueError("Unsupported run budget code.")
        self.code = code
        super().__init__(code)


class RunBudget:
    """Thread-safe run-local counters and first-exhaustion latch."""

    def __init__(self, *, tool_limit: int, model_limit: int):
        """Snapshot settings-validated tool and model capacities for one run."""
        self._limits = {"run_tool_call_limit": tool_limit, "run_model_call_limit": model_limit}
        self._calls = dict.fromkeys(self._limits, 0)
        self._exhausted_code: str | None = None
        self._lock = Lock()

    def require_available(self) -> None:
        """Reject new work after any run ceiling has exhausted."""
        with self._lock:
            code = self._exhausted_code
        if code is not None:
            raise RunBudgetExceeded(code)

    def reserve_tool_call(self) -> None:
        """Atomically admit one graph tool invocation, never refunding a reservation."""
        self._reserve("run_tool_call_limit")

    def reserve_model_call(self) -> None:
        """Atomically admit one logical chat-model invocation, never refunding a reservation."""
        self._reserve("run_model_call_limit")

    def _reserve(self, code: str) -> None:
        """Keep both counters and the first-exhaustion latch under one atomic rule."""
        with self._lock:
            if self._exhausted_code is not None:
                code = self._exhausted_code
            elif self._calls[code] >= self._limits[code]:
                self._exhausted_code = code
            else:
                self._calls[code] += 1
                return
        raise RunBudgetExceeded(code)


class ModelCallBudgetCallback(BaseCallbackHandler):
    """Reserve one shared run slot when LangChain starts a chat-model invocation."""

    raise_error = True
    run_inline = True

    def __init__(self, budget: RunBudget, *, on_rejected: Callable[[UUID], None] | None = None):
        """Bind this invocation-local callback to the turn's budget."""
        self.budget = budget
        self._on_rejected = on_rejected

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs) -> None:
        """Reject a model invocation before generation when no slot remains."""
        try:
            self.budget.reserve_model_call()
        except RunBudgetExceeded:
            if self._on_rejected is not None:
                self._on_rejected(run_id)
            raise
