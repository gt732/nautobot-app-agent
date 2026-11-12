"""Retained-conversation reads and their projections into chat page data."""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from nautobot_agent.models import AgentConversation
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE
from nautobot_agent.tools.render_chart import build_chart_config, render_chart


def with_refusal_history(conversation: AgentConversation, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Merge durable refusals into display history, never into executable graph state."""
    retained_ids = {message.id for message in messages if message.id}
    by_anchor: dict[str, list[BaseMessage]] = {}
    for refusal in conversation.runs.filter(refusal_anchor_id__isnull=False).order_by("submitted_at", "pk"):
        anchor = refusal.refusal_anchor_id if refusal.refusal_anchor_id in retained_ids else ""
        by_anchor.setdefault(anchor, []).extend(
            [HumanMessage(content=refusal.prompt), AIMessage(content=REFUSAL_MESSAGE)]
        )
    # Summarized-away anchors precede the retained graph history.
    history = by_anchor.pop("", [])
    for message in messages:
        history.append(message)
        history.extend(by_anchor.pop(message.id, []))
    return history


def text_entry(message: BaseMessage) -> dict[str, str] | None:
    """Project one retained user or assistant text message."""
    if isinstance(message, HumanMessage) and message.additional_kwargs.get("lc_source") != "summarization":
        return {"role": "user", "content": message.text}
    if isinstance(message, AIMessage) and message.text:
        return {"role": "assistant", "content": message.text}
    return None


def transcript_entries(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    """Project retained user and assistant text without exposing tool activity."""
    return [entry for message in messages if (entry := text_entry(message)) is not None]


ToolStatus = Literal["running", "completed", "failed", "unknown"]


class ToolEntry(TypedDict):
    """One projected tool call; ``chart`` is present only on rebuilt successful chart calls."""

    id: str
    name: str
    args: Any
    status: ToolStatus
    chart: NotRequired[dict[str, Any]]


def _display_id(message_id: str | None, category: str, local_id: str | int) -> str:
    """Compact-JSON encode the message/category/local-ID tuple without delimiter ambiguity."""
    return json.dumps([message_id, category, local_id], separators=(",", ":"))


def _is_interrupted_repair(content: object, name: str, call_id: str) -> bool:
    """Recognize the pinned SDK's synthetic repair, not a recorded tool failure."""
    return content == (
        f"Tool call {name} with id {call_id} did not complete - no result was recorded. "
        "It may have been cancelled or interrupted."
    )


def tool_entries(messages: Sequence[BaseMessage], pending_status: ToolStatus) -> list[tuple[AIMessage, ToolEntry]]:
    """Associate each result with its latest preceding, unconsumed native-ID request.

    Result text is never exposed; it is inspected only for the pinned SDK's synthetic interrupted-call repair.
    """
    entries = []
    unresolved: dict[str, tuple[ToolEntry, ToolCall]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            counts = Counter(call["id"] for call in message.tool_calls)
            for index, call in enumerate(message.tool_calls):
                has_native_id = bool(call["id"]) and counts[call["id"]] == 1
                entry: ToolEntry = {
                    "id": _display_id(
                        message.id,
                        "tool" if has_native_id else "tool-index",
                        call["id"] if has_native_id else index,
                    ),
                    "name": call["name"],
                    "args": call["args"],
                    "status": pending_status,
                }
                entries.append((message, entry))
                if has_native_id:
                    unresolved[call["id"]] = (entry, call)
            for index, invalid in enumerate(message.invalid_tool_calls):
                invalid_entry: ToolEntry = {
                    "id": _display_id(message.id, "invalid", index),
                    "name": invalid["name"] or "unknown",
                    "args": invalid["args"],
                    "status": "failed",
                }
                entries.append((message, invalid_entry))
        elif isinstance(message, ToolMessage):
            associated = unresolved.pop(message.tool_call_id, None)
            if associated is None:
                continue
            entry, call = associated
            if message.status != "error":
                entry["status"] = "completed"
            elif _is_interrupted_repair(message.content, call["name"], message.tool_call_id):
                entry["status"] = "unknown"
            else:
                entry["status"] = "failed"
    return entries


def project_tool_entry(entry: ToolEntry) -> ToolEntry:
    """Add a chart projection to successful native chart calls."""
    if entry["status"] != "completed" or entry["name"] != render_chart.name:
        return entry
    return {**entry, "chart": build_chart_config(entry["args"])}


def project_history(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """Convert stored user messages, assistant prose, and tool calls to the page's history shape."""
    tools: dict[str | None, list[dict[str, Any]]] = {}
    for message, entry in tool_entries(messages, "unknown"):
        tools.setdefault(message.id, []).append({"role": "tool", **project_tool_entry(entry)})
    history: list[dict[str, Any]] = []
    for message in messages:
        entry = text_entry(message)
        if entry is not None:
            history.append(entry)
        if isinstance(message, AIMessage):
            history.extend(tools.get(message.id, []))
    return history


def _token_count(usage: object, key: str) -> int | None:
    """Return one provider token count when it is an exact non-negative int (bool is rejected)."""
    value = usage.get(key) if isinstance(usage, Mapping) else None
    return value if type(value) is int and value >= 0 else None  # pylint: disable=unidiomatic-typecheck


def context_usage(usage: object, limit: int | None) -> dict[str, int | float | None]:
    """Normalize provider usage using the already-sanitized graph limit."""
    input_tokens = _token_count(usage, "input_tokens")
    output_tokens = _token_count(usage, "output_tokens")
    if input_tokens is None or output_tokens is None:
        return {"used": None, "limit": limit, "percent": None}
    used = input_tokens + output_tokens
    return {"used": used, "limit": limit, "percent": used / limit * 100 if limit is not None else None}


def _history_context(messages: Sequence[BaseMessage], limit: int | None) -> dict[str, int | float | None]:
    """Restore usage from only the latest retained root assistant message."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return context_usage(message.usage_metadata, limit)
    return context_usage(None, limit)


EMPTY_CONTEXT_USAGE = context_usage(None, None)


def _read_display_values(conversation: AgentConversation) -> tuple[Mapping[str, Any], list[BaseMessage]]:
    """Read validated checkpoint values and merge durable refusals for display."""
    values = current_runtime().read_conversation_values(conversation)
    return values, with_refusal_history(conversation, values["messages"])


def read_chat_state(
    conversation: AgentConversation, *, limit: int | None = None
) -> tuple[list[dict[str, Any]], Any, dict[str, int | float | None]]:
    """Read and project one conversation's rich page state."""
    values, messages = _read_display_values(conversation)
    return project_history(messages), values.get("todos", []), _history_context(values["messages"], limit)


def read_transcript(conversation: AgentConversation) -> list[dict[str, str]]:
    """Read one conversation's refusal-merged text-only transcript."""
    _, messages = _read_display_values(conversation)
    return transcript_entries(messages)
