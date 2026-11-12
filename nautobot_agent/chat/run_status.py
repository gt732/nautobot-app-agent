"""Client-visible state and event delivery for owned durable runs.

Read-side lazy reconciliation, the single run summary shape and one event-page
delivery decision live here; write-side transitions stay in ``chat.runs``.
"""

from collections.abc import Sequence
from typing import Any

from django.db.models import Q
from django.urls import reverse

from nautobot_agent.chat import runs
from nautobot_agent.chat.errors import RUN_ERROR_MESSAGES
from nautobot_agent.chat.events import EventPage
from nautobot_agent.models import AgentConversation, AgentExecutionTrace, AgentRun, AgentRunStatus, ExecutionStatus


def _reconcile(run: AgentRun | None) -> AgentRun | None:
    """Return the authoritative locked result for a SQL-marked expired or stale candidate."""
    if run is None or not (
        (run.status == AgentRunStatus.RUNNING and run.heartbeat_is_stale)
        or (run.status == AgentRunStatus.QUEUED and run.queue_is_expired)
    ):
        return run
    return runs.reconcile_run(run.pk)  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.


def _candidates():
    """Project the database-clock lifecycle predicates and client queue warning."""
    return AgentRun.objects.with_heartbeat_stale().with_queue_expired().with_queue_warning()


def reconcile_owned_run(user: Any, **lookup: Any) -> AgentRun | None:
    """Read a run constrained to its owner by ``lookup``; due queued expiry or stale interruption is persisted."""
    return _reconcile(_candidates().filter(user=user, **lookup).first())


def reconcile_latest_run(conversation: AgentConversation) -> AgentRun | None:
    """Read and reconcile one latest run, the only possible active reservation."""
    run = (
        _candidates()
        .filter(
            conversation_id=conversation.pk,
            user_id=conversation.user_id,
        )
        .order_by("-submitted_at")
        .first()
    )
    return _reconcile(run)


def reconcile_trace_runs(traces: Sequence[AgentExecutionTrace]) -> None:
    """Repair stale runs and missed terminal outcomes for authorized, displayed traces."""
    running = {trace.pk: trace for trace in traces if trace.status == ExecutionStatus.RUNNING}
    if not running:
        return
    candidates = (
        _candidates()
        .filter(trace_id__in=running)
        .filter(
            Q(status=AgentRunStatus.RUNNING, heartbeat_is_stale=True)
            | ~Q(status__in=(AgentRunStatus.QUEUED, AgentRunStatus.RUNNING))
        )
        .order_by("pk")
    )
    repaired = []
    for candidate in candidates:
        runs.reconcile_run(candidate.pk)
        repaired.append(candidate.trace_id)
    for trace in AgentExecutionTrace.objects.filter(pk__in=repaired).only("status", "ended_at", "capture_state"):
        displayed = running[trace.pk]
        displayed.status = trace.status
        displayed.ended_at = trace.ended_at
        displayed.capture_state = trace.capture_state


def run_summary(run: AgentRun) -> dict[str, Any]:
    """Serialize a run projection; use an owned read for current client-facing state."""
    return {
        "run_id": str(run.pk),
        "conversation_id": str(run.conversation_id),
        "status": run.status,
        # Claim writes the first heartbeat atomically; terminal transitions retain it.
        "execution_started": run.heartbeat_at is not None,
        "events_url": reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)),
        "cancel_url": reverse("plugins:nautobot_agent:agent_run_cancel", args=(run.pk,)),
        "cancel_requested": run.cancel_requested,
        "live_events_incomplete": run.live_events_incomplete,
        "external_outcome_unknown": run.external_outcome_unknown,
        "cleanup_incomplete": run.cleanup_incomplete,
        "error_code": run.error_code or None,
        "error_message": RUN_ERROR_MESSAGES.get(run.error_code),
        "queued_warning": run.queued_warning_due,
        "terminal": run.is_terminal,
        "cancellable": run.cancellable,
        "mcp_unavailable_servers": run.mcp_unavailable_servers,
    }


def delivery_payload(run: AgentRun, page: EventPage) -> dict[str, Any]:
    """Combine an event page with current owned run status and the delivery safety decision."""
    resync = page.resync_required or run.live_events_incomplete or (page.queued and run.is_terminal)
    deliver_events = not resync
    return {
        **run_summary(run),
        "events": page.events if deliver_events else [],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more if deliver_events else False,
        "resync_required": resync,
    }
