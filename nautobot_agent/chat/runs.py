"""Database-owned admission, fencing, and every persisted durable-run transition."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connections, router, transaction
from django.db.models import Value
from django.utils import timezone
from nautobot.core.celery import app as celery_app

from nautobot_agent import app_settings
from nautobot_agent.models import (
    AgentConversation,
    AgentConversationQuerySet,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    ExecutionStatus,
    HeartbeatNow,
    TraceCaptureState,
)

logger = logging.getLogger(__name__)


class RunPublisher(Protocol):  # pylint: disable=too-few-public-methods
    """Publish an admitted run with explicitly named resource limits."""

    def __call__(  # noqa: PLR0913  # publisher contract names each time limit explicitly
        self,
        run_id: UUID,
        user_id: UUID,
        queue: str | None,
        *,
        soft: int,
        hard: int,
        cleanup: int,
    ) -> object:
        """Publish a committed run using its queue and explicitly named time limits."""
        ...


# The only allowed terminal transitions, keyed by the run's current status.
_TERMINAL_TRANSITIONS = {
    AgentRunStatus.QUEUED: (AgentRunStatus.CANCELLED, AgentRunStatus.DISPATCH_FAILED, AgentRunStatus.FAILED),
    AgentRunStatus.RUNNING: (
        AgentRunStatus.COMPLETED,
        AgentRunStatus.REFUSED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
        AgentRunStatus.INTERRUPTED,
    ),
}
_CLAIM_FIELDS = ("status", "heartbeat_at")
_FINISH_FIELDS = ("status", "prompt", "error_code")
_MAX_ACTIVE_RUNS_PER_USER = 2


class RunReservationConflict(Exception):
    """A worker's persisted run reservation no longer matches its authority."""


class AdmissionKind(StrEnum):
    """Expected admission outcomes."""

    ADMITTED = "admitted"
    DUPLICATE = "duplicate"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    BUSY = "busy"
    USER_LIMIT = "user_limit"


@dataclass(frozen=True)
class AdmissionResult:
    """Admission decision and the row captured under its serialization locks."""

    kind: AdmissionKind
    run: AgentRun | None = None
    blocker: AgentRun | None = None


@dataclass(frozen=True)
class RunClaim:
    """Immutable authority and cooperative execution budgets for one worker claim."""

    run_id: UUID
    conversation_id: UUID
    user_id: UUID
    soft_time_limit: int
    cleanup_timeout: int


@dataclass
class LockedRun:
    """The user, conversation and run rows locked in authoritative order; owns lifecycle transitions."""

    owner: Any
    conversation: AgentConversation
    run: AgentRun
    using: str

    def claim(self) -> None:
        """Persist a queued run's worker authority."""
        run = cast(Any, self.run)
        run.status = AgentRunStatus.RUNNING
        run.heartbeat_at = HeartbeatNow()
        run.save(using=self.using, update_fields=_CLAIM_FIELDS)

    def finish(self, status: str, *, error_code: str | None = None) -> None:
        """Persist a terminal transition, retain only display-refusal prompts, and release the reservation."""
        run = cast(Any, self.run)
        if status not in _TERMINAL_TRANSITIONS.get(run.status, ()):
            raise ValueError("Invalid terminal run transition.")
        was_running = run.status == AgentRunStatus.RUNNING
        run.status = status
        if run.refusal_anchor_id is None:
            run.prompt = ""
        if error_code is not None:
            run.error_code = error_code[:64]
        fields = list(_FINISH_FIELDS)
        if was_running and run.error_code != "stale_heartbeat":
            # Retain the completion time even if the optional trace write fails.
            run.heartbeat_at = timezone.now()
            fields.append("heartbeat_at")
        run.save(using=self.using, update_fields=fields)
        if was_running:
            _set_trace_outcome(run, using=self.using)

    def request_cancellation(self) -> None:
        """Record a cooperative stop request without revoking the running owner's fence."""
        if not self.run.cancel_requested:
            cast(Any, self.run).cancel_requested = True
            self.run.save(using=self.using, update_fields=("cancel_requested",))

    def heartbeat(self) -> bool:
        """Persist a fresh heartbeat and return cancellation from this locked snapshot."""
        run = cast(Any, self.run)
        run.heartbeat_at = HeartbeatNow()
        run.save(using=self.using, update_fields=("heartbeat_at",))
        return run.cancel_requested

    def mark_live_events_incomplete(self) -> None:
        """Persist that live delivery may be incomplete."""
        cast(Any, self.run).live_events_incomplete = True
        self.run.save(using=self.using, update_fields=("live_events_incomplete",))

    def store_display(
        self, model: str | None, context_limit: int | None, mcp_unavailable_servers: list[dict[str, str]]
    ) -> None:
        """Persist model and captured MCP availability display values under this run's fence."""
        run = cast(Any, self.run)
        run.display_model = model or ""
        run.display_context_limit = context_limit
        run.mcp_unavailable_servers = mcp_unavailable_servers
        self.run.save(
            using=self.using,
            update_fields=("display_model", "display_context_limit", "mcp_unavailable_servers"),
        )

    def link_trace(self, trace_id: UUID) -> None:
        """Associate an existing trace with this claimed run."""
        self.run.trace_id = trace_id
        self.run.save(using=self.using, update_fields=("trace",))


def _lock_owner(user_id, *, using: str):
    return owner_lock_queryset(using).filter(pk=user_id).first()


def owner_lock_queryset(using: str):
    """Lock user rows without blocking unrelated inserts that reference them (PostgreSQL ``FOR NO KEY UPDATE``)."""
    no_key = connections[using].features.has_select_for_no_key_update
    return get_user_model().objects.using(using).select_for_update(no_key=no_key)


def _lock_run(run_id: UUID, *, using: str) -> LockedRun | None:
    ref = AgentRun.objects.using(using).filter(pk=run_id).values("user_id", "conversation_id").first()
    if ref is None:
        return None
    owner = _lock_owner(ref["user_id"], using=using)
    conversation = AgentConversation.objects.using(using).select_for_update().filter(pk=ref["conversation_id"]).first()
    run = AgentRun.objects.using(using).with_queue_warning().select_for_update().filter(pk=run_id).first()
    if run is None or owner is None or conversation is None:
        return None
    if run.user_id != owner.pk or run.conversation_id != conversation.pk or conversation.user_id != owner.pk:
        return None
    return LockedRun(owner, conversation, run, using)


@contextmanager
def _locked(run_id: UUID, *statuses: str):
    """Lock user -> conversation -> run; yield None when rows or requested authority do not match."""
    using = router.db_for_write(AgentRun)
    with transaction.atomic(using=using):
        locked = _lock_run(run_id, using=using)
        if locked is not None and statuses and locked.run.status not in statuses:
            locked = None
        yield locked


def lock_conversation_reservations(
    owner_by_id: dict[UUID, UUID],
    conversations: AgentConversationQuerySet,
    *,
    using: str,
) -> list[tuple[AgentConversation, bool]]:
    """Lock owner -> conversation -> run groups in stable PK order and classify reservations.

    The supplied owner mapping is the authorized initial snapshot. Changed owners are rejected;
    this method deliberately never chases them by acquiring a late owner lock.
    """
    owner_ids = sorted(set(owner_by_id.values()))
    locked_owners = list(owner_lock_queryset(using).filter(pk__in=owner_ids).order_by("pk"))
    locked_owner_ids = {owner.pk for owner in locked_owners}
    candidate_ids = [
        conversation_id for conversation_id, owner_id in owner_by_id.items() if owner_id in locked_owner_ids
    ]
    locked_conversations = list(
        conversations.using(using)
        .filter(pk__in=candidate_ids, user_id__in=locked_owner_ids)
        .select_for_update()
        .order_by("pk")
    )
    authorized = [
        conversation
        for conversation in locked_conversations
        if owner_by_id.get(conversation.pk) == conversation.user_id
    ]
    locked_runs = list(
        AgentRun.objects.using(using)
        .active()
        .filter(conversation_id__in=[conversation.pk for conversation in authorized])
        .select_for_update()
        .order_by("pk")
    )
    owners = {owner.pk: owner for owner in locked_owners}
    conversations_by_id = {conversation.pk: conversation for conversation in authorized}
    reserved_ids = set()
    for run in locked_runs:
        conversation = conversations_by_id[run.conversation_id]
        _reconcile_locked(LockedRun(owners[conversation.user_id], conversation, run, using))
        if not run.is_terminal:
            reserved_ids.add(run.conversation_id)
    return [(conversation, conversation.pk in reserved_ids) for conversation in authorized]


def submit_run(
    user,
    client_request_id: UUID,
    prompt: str,
    conversation_id: UUID | None,
    publish: RunPublisher,
) -> AdmissionResult:
    """Return one admission decision, committing before bounded injected publication.

    Input is expected to be form-normalized. `publish` receives run UUID, user id, queue, soft
    limit, hard limit, and cleanup timeout; its broker/task implementation belongs to the caller.
    """
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    using = router.db_for_write(AgentRun)
    connection = transaction.get_connection(using)
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError("Run admission requires an outermost autocommit connection.")
    policy = app_settings.chat_run_settings()
    request_id = UUID(str(client_request_id))
    queue = policy.celery_queue or celery_app.conf.task_default_queue

    with transaction.atomic(using=using, durable=True):
        admission = _admit(user.pk, request_id, prompt, conversation_id, policy, using=using)
    if admission.kind != AdmissionKind.ADMITTED:
        return admission
    # The durable block has committed: workers can only see the run after this point.
    run_id = cast(AgentRun, admission.run).pk
    try:
        publish(
            run_id,  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.
            user.pk,
            queue,
            soft=policy.soft_time_limit,
            hard=policy.hard_time_limit,
            cleanup=policy.cleanup_timeout,
        )
    except Exception as error:  # noqa: BLE001  # any broker failure finishes the committed run as dispatch-failed
        _resolve_dispatch_failure(
            run_id,  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.
            type(error).__name__,
        )
    return AdmissionResult(
        AdmissionKind.ADMITTED, run=AgentRun.objects.using(using).with_queue_warning().get(pk=run_id)
    )


def _expire_if_due(locked: LockedRun) -> bool:
    """Finish a locked queued run whose admission deadline has reached database statement time."""
    expired = (
        AgentRun.objects.using(locked.using)
        # Locking current read: MySQL repeatable-read must not judge a pre-lock snapshot.
        .select_for_update()
        .filter(pk=locked.run.pk, status=AgentRunStatus.QUEUED, queue_expires_at__lte=HeartbeatNow())
        .exists()
    )
    if expired:
        locked.finish(AgentRunStatus.DISPATCH_FAILED, error_code="queue_wait_expired")
    return expired


def _interrupt_if_heartbeat_stale(locked: LockedRun) -> bool:
    """Terminally interrupt a locked running owner whose database-clock heartbeat is stale."""
    stale = (
        AgentRun.objects.using(locked.using)
        # MySQL repeatable-read needs this locking read to see heartbeats committed after
        # the transaction snapshot; the annotated predicate alone is not a current read.
        .select_for_update()
        .with_heartbeat_stale()
        .filter(pk=locked.run.pk, status=AgentRunStatus.RUNNING, heartbeat_is_stale=True)
        .exists()
    )
    if stale:
        locked.finish(AgentRunStatus.INTERRUPTED, error_code="stale_heartbeat")
    return stale


def _reconcile_locked(locked: LockedRun) -> None:
    """Apply any due lazy lifecycle transition to an already locked run."""
    if locked.run.status == AgentRunStatus.QUEUED:
        _expire_if_due(locked)
    elif locked.run.status == AgentRunStatus.RUNNING:
        _interrupt_if_heartbeat_stale(locked)


def reconcile_run(run_id: UUID) -> AgentRun | None:
    """Reconcile due lifecycle transitions or a missed terminal trace outcome under run locks.

    Returns the authoritative row, or None when it no longer exists. Callers own authorization.
    """
    with _locked(run_id) as locked:
        if locked is None:
            return None
        if locked.run.is_terminal:
            _set_trace_outcome(locked.run, using=locked.using)
        else:
            _reconcile_locked(locked)
        return locked.run


def _admit(  # noqa: PLR0913  # admission decision inputs resolved under one lock sequence
    user_id, request_id: UUID, prompt: str, conversation_id: UUID | None, policy, *, using: str
) -> AdmissionResult:
    """Decide admission under user -> conversation -> run locks; create the reserved run if admitted."""
    owner = _lock_owner(user_id, using=using)
    if owner is None or not owner.is_active:
        raise PermissionDenied
    duplicate = AgentRun.objects.using(using).filter(user_id=owner.pk, client_request_id=request_id).first()
    if duplicate is not None:
        # Reconcile the original only; a duplicate never republishes or creates a replacement.
        locked = _lock_run(duplicate.pk, using=using)
        if locked is None or locked.owner.pk != owner.pk:
            return AdmissionResult(AdmissionKind.CONVERSATION_NOT_FOUND)
        _reconcile_locked(locked)
        return AdmissionResult(AdmissionKind.DUPLICATE, run=locked.run)
    conversation = None
    if conversation_id is not None:
        conversation = (
            AgentConversation.objects.using(using)
            .select_for_update()
            .filter(pk=conversation_id, user_id=owner.pk)
            .first()
        )
        if conversation is None:
            return AdmissionResult(AdmissionKind.CONVERSATION_NOT_FOUND)
        blocker = _active_blocker(owner, conversation, using=using)
        if blocker is not None:
            return AdmissionResult(AdmissionKind.BUSY, blocker=blocker)
    if _active_run_count(owner, using=using) >= _MAX_ACTIVE_RUNS_PER_USER:
        return AdmissionResult(AdmissionKind.USER_LIMIT)
    if conversation is None:
        now = timezone.now()
        conversation = AgentConversation.objects.using(using).create(
            user=owner,
            title=" ".join(prompt.split())[:255],
            created_at=now,
            last_activity_at=now,
        )
    # One INSERT statement evaluates both database-clock values; the deadline is never recomputed.
    run = AgentRun.objects.using(using).create(
        user_id=owner.pk,
        conversation=conversation,
        id=uuid4(),
        client_request_id=request_id,
        prompt=prompt,
        status=AgentRunStatus.QUEUED,
        submitted_at=HeartbeatNow(),
        queue_expires_at=HeartbeatNow() + Value(timedelta(seconds=policy.queue_wait_timeout)),
        soft_time_limit=policy.soft_time_limit,
        cleanup_timeout=policy.cleanup_timeout,
    )
    run.refresh_from_db(using=using, fields=("submitted_at", "queue_expires_at"))
    return AdmissionResult(AdmissionKind.ADMITTED, run=run)


def _active_blocker(owner, conversation: AgentConversation, *, using: str) -> AgentRun | None:
    """Lock and reconcile the conversation's active runs in key order; return the first still active."""
    for blocker in (
        AgentRun.objects.using(using)
        .with_queue_warning()
        .active()
        .select_for_update()
        .filter(conversation_id=conversation.pk)
        .order_by("pk")
    ):
        _reconcile_locked(LockedRun(owner, conversation, blocker, using))
        if not blocker.is_terminal:
            return blocker
    return None


def _active_run_count(owner, *, using: str) -> int:
    """Lock and reconcile each of the owner's active runs in key order; count those still active."""
    count = 0
    for run_id in (
        AgentRun.objects.using(using).active().filter(user_id=owner.pk).order_by("pk").values_list("pk", flat=True)
    ):
        locked = _lock_run(run_id, using=using)
        if locked is not None:
            _reconcile_locked(locked)
            count += not locked.run.is_terminal
    return count


def _resolve_dispatch_failure(run_id: UUID, code: str) -> None:
    with _locked(run_id, AgentRunStatus.QUEUED) as locked:
        if locked is None:
            return
        locked.finish(AgentRunStatus.DISPATCH_FAILED, error_code=code)


def claim_run(run_id: UUID) -> RunClaim | None:
    """Claim queued work exactly once, before its deadline, under the locked lifecycle status."""
    with _locked(run_id, AgentRunStatus.QUEUED) as locked:
        if locked is None:
            return None
        # Exact-deadline delivery loses; expiry wins before any principal or execution work.
        if _expire_if_due(locked):
            return None
        if not locked.owner.is_active:
            locked.finish(AgentRunStatus.FAILED, error_code="principal_inactive")
            return None
        locked.claim()
        run = locked.run
        return RunClaim(
            run.pk,
            locked.conversation.pk,
            run.user_id,
            run.soft_time_limit,
            run.cleanup_timeout,
        )


def request_cancel(run_id: UUID, user) -> AgentRun | None:
    """Cancel queued work or request cooperative cancellation from its running owner."""
    with _locked(run_id) as locked:
        if locked is None or locked.owner.pk != user.pk or not locked.owner.is_active:
            return None
        _reconcile_locked(locked)
        run = locked.run
        if run.status == AgentRunStatus.QUEUED:
            locked.finish(AgentRunStatus.CANCELLED)
        elif run.status == AgentRunStatus.RUNNING:
            locked.request_cancellation()
        return run


def record_run_diagnostics(
    run_id: UUID,
    *,
    user_id,
    conversation_id: UUID,
    external_outcome_unknown: bool = False,
    cleanup_incomplete: bool = False,
) -> None:
    """Best-effort monotonic enrichment of the original owned run, including terminal rows.

    Only true flags are written, by one guarded UPDATE that preserves every other field. A deleted or
    differently owned run is a no-op and is never recreated; storage errors propagate to the caller.
    """
    flags = {
        name: True
        for name, observed in (
            ("external_outcome_unknown", external_outcome_unknown),
            ("cleanup_incomplete", cleanup_incomplete),
        )
        if observed
    }
    if not flags:
        return
    using = router.db_for_write(AgentRun)
    AgentRun.objects.using(using).filter(pk=run_id, user_id=user_id, conversation_id=conversation_id).update(**flags)


def _set_trace_outcome(run: AgentRun, *, using: str) -> None:
    """Persist outcome, failing pending capture only when a stale worker loses authority."""
    if run.trace_id is None:
        return
    try:
        with transaction.atomic(using=using):
            trace = AgentExecutionTrace.objects.using(using).select_for_update().get(pk=run.trace_id)
            if trace.status != ExecutionStatus.RUNNING:
                return
            trace.status = {
                AgentRunStatus.COMPLETED: ExecutionStatus.OK,
                AgentRunStatus.REFUSED: ExecutionStatus.REFUSED,
                AgentRunStatus.FAILED: ExecutionStatus.ERROR,
                AgentRunStatus.CANCELLED: ExecutionStatus.CANCELLED,
            }.get(run.status, ExecutionStatus.INCOMPLETE)
            trace.ended_at = run.heartbeat_at or timezone.now()
            fields = ["status", "ended_at"]
            if (
                run.status == AgentRunStatus.INTERRUPTED
                and run.error_code == "stale_heartbeat"
                and trace.capture_state == TraceCaptureState.PENDING
            ):
                trace.capture_state = TraceCaptureState.FAILED
                fields.append("capture_state")
            trace.save(using=using, update_fields=fields)
    except Exception as error:  # noqa: BLE001  # optional trace outcome must not block run reconciliation
        logger.warning("Optional run trace outcome failed (%s).", type(error).__name__)


@contextmanager
def locked_run_write(run_id: UUID):
    """Yield the locked rows only while this once-claimed run retains its running reservation."""
    with _locked(run_id, AgentRunStatus.RUNNING) as locked:
        if locked is None:
            raise RunReservationConflict("Run authority has been revoked.")
        yield locked


def finish_run(
    run_id: UUID,
    status: str,
    *,
    error_code: str = "",
) -> None:
    """Safely finish only the current owner and release only its matching reservation."""
    with locked_run_write(run_id) as locked:
        locked.finish(status, error_code=error_code)
