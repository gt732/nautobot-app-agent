"""Conversation and run errors, safe messages, and traceback diagnostics."""

from types import TracebackType

from nautobot_agent.runtime.errors import MODEL_ERROR_MESSAGES

BUSY_MESSAGE = "A reply is still running in this chat. Try again when it finishes."
UNAVAILABLE_MESSAGE = "Chat is temporarily unavailable. Please try again."
RESPONSE_ERROR_MESSAGE = "The Agent could not complete the response."
RUN_STORAGE_UNAVAILABLE_MESSAGE = "Run storage is unavailable."

_TIME_LIMIT_MESSAGE = (
    "The run reached its time limit and was stopped before finishing. Earlier checkpoints may contain partial progress."
)

RUN_ERROR_MESSAGES = {
    **MODEL_ERROR_MESSAGES,
    "llm_not_configured": (
        "Chat is not configured yet. Ask an administrator to select a default Agent model "
        "under Nautobot Agent → LLM Configurations."
    ),
    "queue_wait_expired": (
        "The request waited too long for a worker and was not started. Send it again when workers are available."
    ),
    "soft_deadline": _TIME_LIMIT_MESSAGE,
    "soft_time_limit": _TIME_LIMIT_MESSAGE,
}


def safe_error_diagnostic(exc: BaseException) -> str:
    """Describe exception type and traceback locations without values or filesystem paths."""
    frames: list[str] = []
    traceback: TracebackType | None = exc.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "<unknown>")
        frames.append(f"{module}.{frame.f_code.co_name}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return f"{type(exc).__name__} ({', '.join(frames)})"


class ConversationBusy(Exception):
    """The conversation is reserved by active work."""
