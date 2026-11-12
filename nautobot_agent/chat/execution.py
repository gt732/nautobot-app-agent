"""Worker-owned execution of one database-claimed chat run."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, close_old_connections, connections
from langgraph.runtime import RunControl

from nautobot_agent.app_settings import CHAT_RUN_HEARTBEAT_FAILURE_AFTER, CHAT_RUN_HEARTBEAT_INTERVAL
from nautobot_agent.chat import events
from nautobot_agent.chat.chat_stream import DONE, ERROR, refusal_events, stream_chat_events
from nautobot_agent.chat.errors import RESPONSE_ERROR_MESSAGE, RUN_ERROR_MESSAGES, safe_error_diagnostic
from nautobot_agent.chat.runs import (
    LockedRun,
    RunClaim,
    RunReservationConflict,
    claim_run,
    finish_run,
    locked_run_write,
    record_run_diagnostics,
)
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import AgentConversation, AgentRun, AgentRunStatus
from nautobot_agent.runtime import tracing
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.checkpointer import DjangoCheckpointSaver
from nautobot_agent.runtime.control import ConversationUnavailable, RunDraining
from nautobot_agent.runtime.errors import model_failure
from nautobot_agent.runtime.llm import DefaultLLMNotConfigured
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE
from nautobot_agent.runtime.turn import AgentTurn

logger = logging.getLogger(__name__)


class TerminationKind(StrEnum):
    """Factual execution termination, without retaining an exception."""

    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


_TIMEOUT_REASONS = frozenset({"soft_deadline", "soft_time_limit"})
_USER_STOP_REASON = "cancelled"


@dataclass(frozen=True)
class RunObservations:
    """Immutable facts collected while executing and cleaning up one run."""

    termination: TerminationKind
    termination_error: str = ""
    drain_reason: str = ""
    interruption_reason: str = ""
    ownership_invalid: bool = False


@dataclass(frozen=True)
class RunOutcome:
    """The decided persisted status and safe error code."""

    status: AgentRunStatus
    error_code: str


def decide_run_outcome(observations: RunObservations) -> RunOutcome:
    """Apply the run finalization precedence table without side effects.

    Interruption reason > ownership loss (both interrupted) > completed/refused
    > timeout (failed with its reason) > explicit user stop (cancelled, no error code)
    > failed with the recorded termination error.
    """
    drain_reason = observations.drain_reason
    if observations.interruption_reason:
        status, error_code = AgentRunStatus.INTERRUPTED, observations.interruption_reason
    elif observations.ownership_invalid:
        status, error_code = AgentRunStatus.INTERRUPTED, "ownership_unavailable"
    elif observations.termination in (TerminationKind.COMPLETED, TerminationKind.REFUSED):
        status, error_code = AgentRunStatus(observations.termination), ""
    elif drain_reason in _TIMEOUT_REASONS:
        status, error_code = AgentRunStatus.FAILED, drain_reason
    elif observations.termination_error in _TIMEOUT_REASONS:
        status, error_code = AgentRunStatus.FAILED, observations.termination_error
    elif drain_reason == _USER_STOP_REASON:
        status, error_code = AgentRunStatus.CANCELLED, ""
    else:
        status, error_code = AgentRunStatus.FAILED, observations.termination_error
    return RunOutcome(status=status, error_code=error_code)


class RunWriteGuard:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Fence run writes and own run authority, the graph stop signal and the committed outcome latch."""

    def __init__(self, claim: RunClaim, *, on_drain: Callable[[], None] | None = None):
        """Bind claim authority and local outcome synchronization."""
        self.claim = claim
        self._invalid = threading.Event()
        self._outcome_lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._operation_state = threading.local()
        self._control = RunControl()
        self._drain_handler = on_drain
        self._notification_pending = False
        self._notification_in_progress = False
        self._interruption_reason = ""
        # Leaf lock: loop-thread callbacks only latch these monotonic diagnostic facts.
        self._diagnostic_lock = threading.Lock()
        self._external_outcome_unknown = False
        self._cleanup_incomplete = False
        self._heartbeat_baseline = time.monotonic()
        # Committed outcome; writers hold both locks, drain-only readers never need the outcome lock.
        self._finalization: RunOutcome | None = None

    @property
    def control(self) -> RunControl:
        """Expose the one graph control signal owned by this guard."""
        return self._control

    @property
    def invalid(self) -> bool:
        """Read the monotonic authority latch."""
        return self._invalid.is_set()

    @contextmanager
    def _operation(self) -> Generator[None, None, None]:
        """Defer drain notification until this thread unwinds guard critical sections."""
        depth = getattr(self._operation_state, "depth", 0)
        self._operation_state.depth = depth + 1
        try:
            yield
        finally:
            self._operation_state.depth -= 1
            if self._operation_state.depth == 0:
                self._dispatch_notification()

    def _dispatch_notification(self) -> None:
        with self._drain_lock:
            if not self._notification_pending or self._notification_in_progress or self._drain_handler is None:
                return
            self._notification_pending = False
            self._notification_in_progress = True
            handler = self._drain_handler
        try:
            handler()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            with self._drain_lock:
                self._notification_pending = True
            logger.warning("Run drain resource notification failed (%s)", type(exc).__name__)
        finally:
            with self._drain_lock:
                self._notification_in_progress = False

    def _latch_drain(self, reason: str) -> None:
        """Latch while holding the leaf drain-state lock."""
        if self._control.drain_requested:
            return
        self._control.request_drain(reason)
        self._notification_pending = True

    def request_drain(self, reason: str) -> None:
        """Latch the first drain reason and notify execution after guard work unwinds."""
        with self._operation(), self._drain_lock:
            if self._finalization is None:
                self._latch_drain(reason)

    @property
    def drain_reason(self) -> str:
        """Read the first guard-owned drain reason."""
        with self._drain_lock:
            return self._control.drain_reason or ""

    def invalidate(self) -> None:
        """Deny new sensitive work immediately and request ownership drain."""
        with self._operation(), self._drain_lock:
            # A committed interruption still revokes local authority; any other committed outcome is final.
            if self._finalization is not None and self._finalization.status != AgentRunStatus.INTERRUPTED:
                return
            self._invalid.set()
            self._latch_drain("ownership")

    @property
    def finalized(self) -> bool:
        """Read locally confirmed lifecycle persistence."""
        with self._outcome_lock:
            return self._finalization is not None

    def publication_allowed(self) -> bool:
        """Check current publication safety under finalization synchronization."""
        with self._outcome_lock:
            return not self.invalid and not self._interruption_reason

    def record_interruption(self, reason: str, *, drain_reason: str = "") -> None:
        """Latch the first interruption cause and an explicitly paired drain."""
        with self._operation():
            with self._outcome_lock:
                if self._finalization is not None:
                    return
                if not self._interruption_reason:
                    self._interruption_reason = reason or "cleanup_unconfirmed"
                if drain_reason:
                    with self._drain_lock:
                        self._latch_drain(drain_reason)

    def observe(self, termination: TerminationKind, termination_error: str, drain_reason: str) -> RunObservations:
        """Snapshot guard-owned safety facts alongside the execution's termination evidence."""
        with self._outcome_lock:
            return RunObservations(
                termination=termination,
                termination_error=termination_error,
                drain_reason=drain_reason,
                interruption_reason=self._interruption_reason,
                ownership_invalid=self.invalid,
            )

    def _authority_failure(self) -> ConversationUnavailable:
        self.invalidate()
        return ConversationUnavailable("Conversation ownership is unavailable.")

    @contextmanager
    def _fenced(self, *, ordinary=False, optional=False) -> Generator[LockedRun]:
        with self._operation():
            if self.invalid:
                raise ConversationUnavailable("Conversation ownership is unavailable.")
            entered = False
            try:
                with locked_run_write(self.claim.run_id) as write:
                    entered = True
                    yield write
                    if self.invalid:
                        raise ConversationUnavailable("Conversation ownership is unavailable.")
            except RunReservationConflict:
                raise self._authority_failure() from None
            except DatabaseError:
                if optional or (ordinary and entered):
                    raise
                raise self._authority_failure() from None

    def write(self, conversation_id: str) -> AbstractContextManager[LockedRun]:
        """Fence ordinary writes without invalidating on admitted storage errors."""
        if str(conversation_id) != str(self.claim.conversation_id):
            raise ConversationUnavailable("Conversation ownership is unavailable.")
        return self._fenced(ordinary=True)

    def _optional_write(self, label: str, action: Callable[[LockedRun], None]) -> bool:
        """Persist optional bookkeeping; storage errors degrade, definite ownership conflicts still fence."""
        try:
            with self._fenced(optional=True) as write:
                action(write)
        except DatabaseError as exc:
            logger.warning("Optional run %s persistence failed (%s)", label, type(exc).__name__)
            return False
        return True

    def link_trace(self, trace_id: UUID) -> bool:
        """Associate a trace; return False when only the optional association could not be stored."""
        return self._optional_write("trace link", lambda write: write.link_trace(trace_id))

    def mark_live_events_incomplete(self) -> bool:
        """Record delivery uncertainty through the current claim, best effort on storage errors."""
        return self._optional_write("event coverage", lambda write: write.mark_live_events_incomplete())

    def store_display(self, model, context_limit, mcp_unavailable_servers) -> bool:
        """Store presentation metadata through the current claim, best effort on storage errors."""
        return self._optional_write(
            "display", lambda write: write.store_display(model, context_limit, mcp_unavailable_servers)
        )

    def require_new_work(self) -> None:
        """Recheck active principal and run ownership before each new sensitive operation."""
        if self.invalid:
            raise ConversationUnavailable("Execution authority is unavailable.") from None
        if self.control.drain_requested:
            raise RunDraining("Execution is shutting down.")
        with self._fenced() as write:
            principal_active = write.owner.is_active
            cancel = write.run.cancel_requested
        if not principal_active:
            self.record_interruption("principal_inactive", drain_reason="principal_inactive")
            self.invalidate()
            error = ConversationUnavailable("Execution authority is unavailable.")
            try:
                self.interrupt("principal_inactive")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Unable to interrupt inactive-principal run (%s)", type(exc).__name__)
            raise error
        if cancel:
            self.request_drain("cancelled")
            raise RunDraining("Execution is shutting down.")

    def mark_mcp_uncertain(self) -> None:
        """Latch remote uncertainty even when the graph catches its tool exception."""
        with self._diagnostic_lock:
            self._external_outcome_unknown = True
        self.record_interruption("mcp_outcome_unknown", drain_reason="mcp_uncertain")

    def mark_mcp_cleanup_incomplete(self) -> None:
        """Latch nonfatal cleanup evidence only; it never drains, interrupts or implies remote effects."""
        with self._diagnostic_lock:
            self._cleanup_incomplete = True

    def merge_diagnostics(self) -> None:
        """Synchronously persist latched diagnostic facts onto the original owned run, best effort.

        Never called on an event-loop thread; never fences, revokes or retries anything.
        """
        with self._diagnostic_lock:
            external_outcome_unknown = self._external_outcome_unknown
            cleanup_incomplete = self._cleanup_incomplete
        try:
            record_run_diagnostics(
                self.claim.run_id,
                user_id=self.claim.user_id,
                conversation_id=self.claim.conversation_id,
                external_outcome_unknown=external_outcome_unknown,
                cleanup_incomplete=cleanup_incomplete,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Run diagnostic persistence failed (%s)", type(exc).__name__)

    def persist_diagnostics(self) -> None:
        """Persist latched facts from a detached executor thread, then release its connections."""
        try:
            self.merge_diagnostics()
        finally:
            connections.close_all()

    def _ownership_loss_reaction(self) -> None:
        """Best-effort terminal interruption after authority loss, outside the failed operation scope."""
        close_old_connections()
        if self.finalized:
            return
        try:
            self.interrupt("ownership_unavailable")
        except Exception:  # pylint: disable=broad-exception-caught
            close_old_connections()

    def heartbeat(self) -> bool:
        """Persist heartbeat, tolerating brief database failures; return whether to continue."""
        try:
            return self._heartbeat()
        except ConversationUnavailable:
            self._ownership_loss_reaction()
            return False

    def _heartbeat(self) -> bool:
        """Perform a heartbeat operation, leaving ownership-loss reaction to the caller."""
        with self._operation():
            failure = None
            cancel = False
            attempt_started = time.monotonic()
            try:
                with locked_run_write(self.claim.run_id) as write:
                    cancel = write.heartbeat()
            except (RunReservationConflict, DatabaseError) as error:
                failure = error
            # The transaction has unwound before outcome synchronization.
            with self._outcome_lock:
                if self._finalization is not None:
                    return False
                if isinstance(failure, DatabaseError):
                    if time.monotonic() - self._heartbeat_baseline < CHAT_RUN_HEARTBEAT_FAILURE_AFTER:
                        return True
                    raise self._authority_failure() from None
                if failure is not None:
                    raise self._authority_failure() from None
                self._heartbeat_baseline = attempt_started
            if cancel:
                self.request_drain("cancelled")
            return True

    def finish(self, status: str, *, error_code: str) -> None:
        """Confirm finalization only after the fenced finish commits."""
        with self._operation(), self._outcome_lock:
            try:
                finish_run(
                    self.claim.run_id,
                    status,
                    error_code=error_code,
                )
            except (RunReservationConflict, DatabaseError):
                raise self._authority_failure() from None
            with self._drain_lock:
                self._finalization = RunOutcome(AgentRunStatus(status), error_code)

    def interrupt(self, error_code: str) -> None:
        """Terminally interrupt the still-owned run once, releasing its reservation and fencing writes.

        The first latched interruption cause is persisted; trace capture health is left unchanged.
        """
        with self._operation(), self._outcome_lock:
            if self._finalization is not None:
                return
            self.record_interruption(error_code)
            self.finish(
                AgentRunStatus.INTERRUPTED,
                error_code=self._interruption_reason,
            )

    def interrupt_cleanup_timeout(self) -> None:
        """Interrupt unconfirmed cleanup, then revoke local authority once the interruption committed."""
        try:
            self.interrupt("cleanup_unconfirmed")
        except ConversationUnavailable:
            self._ownership_loss_reaction()
            return
        self.invalidate()

    @contextmanager
    def finalization(self) -> Generator[RunOutcome | None, None, None]:
        """Serialize the execution's final write with interruption, deferring resource callbacks until exit."""
        with self._operation(), self._outcome_lock:
            yield self._finalization


class AgentExecution:  # pylint: disable=too-many-instance-attributes
    """Own graph, MCP, trace, checkpoint guard, and disposable event publication."""

    def __init__(self, claim: RunClaim, *, heartbeat_interval: float = CHAT_RUN_HEARTBEAT_INTERVAL):
        """Allocate the resources owned by one claimed worker execution."""
        self.claim = claim
        self.heartbeat_interval = heartbeat_interval
        self.publisher = events.RunEvents()
        self.done_pending = False
        self.trace = tracing.ExecutionTrace()
        self.mcp = None
        self.stop_monitor = threading.Event()
        self.monitor = None
        self.started = time.monotonic()
        self.cleanup_deadline = None
        self._cleanup_lock = threading.Lock()
        self.publication_failed = False
        self.guard = RunWriteGuard(claim, on_drain=self._handle_drain)

    @property
    def control(self) -> RunControl:
        """Read the guard's shared graph control."""
        return self.guard.control

    def run(self) -> None:
        """Execute work and persist the outcome under the run guard."""
        termination, termination_error = TerminationKind.FAILED, ""
        try:
            try:
                self._start_monitor()
                termination, termination_error = self._execute()
            finally:
                try:
                    self._begin_cleanup()
                    self._close_mcp()
                finally:
                    self._finalize(termination, termination_error)
        finally:
            try:
                self._stop_monitor()
            finally:
                self.publisher.close()

    def _close_mcp(self) -> None:
        """Close once; only operation cleanup evidence (or a failed close) latches cleanup_incomplete."""
        if self.mcp is None:
            return
        try:
            result = self.mcp.close(timeout=self._cleanup_remaining())
        except SoftTimeLimitExceeded:
            self.guard.request_drain("soft_time_limit")
            self._mcp_cleanup_unconfirmed()
            logger.warning("Chat MCP shutdown failed (SoftTimeLimitExceeded)")
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._mcp_cleanup_unconfirmed()
            logger.warning("Chat MCP shutdown failed (%s)", type(exc).__name__)
            return
        if not result.cleanup_confirmed:
            self._mcp_cleanup_unconfirmed()
        if result.external_outcome_unknown:
            # Confirmed cleanup never clears an unknown remote result: it still interrupts the run.
            self.guard.record_interruption("mcp_outcome_unknown")

    def _mcp_cleanup_unconfirmed(self) -> None:
        self.guard.mark_mcp_cleanup_incomplete()
        self.guard.record_interruption("cleanup_unconfirmed")

    def _execute(self) -> tuple[TerminationKind, str]:  # pylint: disable=too-many-return-statements
        """Run the turn and classify how it ended."""
        try:
            termination, code = self._execute_turn()
            if code:
                self._publish_failure(code)
            return termination, code
        except RunDraining:
            if self.control.drain_requested:
                return TerminationKind.FAILED, ""
            self._report_failure("execution_unavailable")
            return TerminationKind.FAILED, "execution_unavailable"
        except SoftTimeLimitExceeded:
            self.guard.request_drain("soft_time_limit")
            return TerminationKind.FAILED, "soft_time_limit"
        except DefaultLLMNotConfigured:
            self._report_failure("llm_not_configured")
            return TerminationKind.FAILED, "llm_not_configured"
        except (ConversationUnavailable, ImproperlyConfigured) as exc:
            self._report_failure("execution_unavailable", diagnostic=safe_error_diagnostic(exc))
            return TerminationKind.FAILED, "execution_unavailable"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failure = model_failure(exc)
            code = failure.code if failure is not None else type(exc).__name__
            self._report_failure(code, diagnostic=safe_error_diagnostic(exc))
            return TerminationKind.FAILED, code

    def _execute_turn(self) -> tuple[TerminationKind, str]:
        """Bootstrap publisher, principal, MCP and trace, then screen and run the turn."""
        try:
            self.publisher.start(self.claim.run_id)
        except events.EventStreamError as exc:
            self._fail_publication(exc)
        run = AgentRun.objects.get(pk=self.claim.run_id)
        user = get_user_model().objects.get(pk=self.claim.user_id)
        runtime = current_runtime()
        self.mcp = MCPTurn(host=self.guard)
        conversation = self._conversation()
        self.trace = tracing.start_trace(user, conversation, run.prompt)
        if self.trace.trace_id is not None:
            try:
                linked = self.guard.link_trace(self.trace.trace_id)
            except BaseException:
                self.trace.abandon()
                raise
            if not linked:
                self.trace.abandon()
        turn = AgentTurn(
            runtime=runtime,
            user=user,
            conversation=conversation,
            prompt=run.prompt,
            guard=self.guard,
            mcp_turn=self.mcp,
            trace=self.trace,
        )
        screen = turn.screen()
        if screen.allowed:
            return self._run_graph(turn)
        self._write_refusal(screen.anchor_id)
        for name, payload in refusal_events(REFUSAL_MESSAGE):
            self._publish(name, payload)
        return TerminationKind.REFUSED, ""

    def _finalize(self, termination: TerminationKind, termination_error: str) -> None:
        """Decide and persist the final outcome, then publish completion before the optional trace flush.

        The drain reason is sealed when execution ends; later interruption and authority facts still apply.
        """
        self.guard.merge_diagnostics()
        committed = None
        try:
            drain_reason = self.guard.drain_reason
            self.trace.freeze()
            outcome, confirmed = self._commit_outcome(termination, termination_error, drain_reason)
            committed = outcome.status if confirmed else self._committed_status()
            if confirmed and outcome.status != AgentRunStatus.INTERRUPTED and self.done_pending:
                self._publish(DONE, {}, terminal=True)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.guard.invalidate()
            logger.warning("Chat run finalization failed (%s)", type(exc).__name__)
        # Execution is over: trace-only delay must never reach the cleanup-deadline monitor.
        self._stop_monitor()
        self.trace.finish_capture(committed)

    def _commit_outcome(
        self, termination: TerminationKind, termination_error: str, drain_reason: str
    ) -> tuple[RunOutcome, bool]:
        """Select and persist once while serialized with the monitor's authority revocation."""
        with self.guard.finalization() as committed:
            if committed is not None:
                return committed, True
            outcome = decide_run_outcome(self.guard.observe(termination, termination_error, drain_reason))
            try:
                self.guard.finish(outcome.status, error_code=outcome.error_code)
                return outcome, True
            except ConversationUnavailable:
                logger.warning("Chat run finalization unconfirmed: execution authority is unavailable.")
                return outcome, False

    def _committed_status(self) -> str | None:
        """Read an outcome another actor committed; None when no terminal outcome is visible."""
        try:
            status = AgentRun.objects.filter(pk=self.claim.run_id).values_list("status", flat=True).first()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Chat run outcome read failed (%s)", type(exc).__name__)
            return None
        return None if status in (None, AgentRunStatus.QUEUED, AgentRunStatus.RUNNING) else status

    def _report_failure(self, code: str, *, diagnostic: str | None = None) -> None:
        if diagnostic is None:
            logger.warning("Chat run execution failed (%s)", code)
        else:
            logger.warning("Chat run execution failed (%s): %s", code, diagnostic)
        self._publish_failure(code)

    def _publish_failure(self, code: str) -> None:
        payload = {"error": RUN_ERROR_MESSAGES.get(code, RESPONSE_ERROR_MESSAGE)}
        self._publish(ERROR, payload)

    def _publish(self, name: str, payload: dict, *, terminal=False) -> None:
        if self.publication_failed or not self.guard.publication_allowed():
            return
        if name == DONE and not terminal:
            self.done_pending = True
            return
        try:
            self.publisher.publish(self.claim.run_id, name, payload)
        except events.EventStreamError as exc:
            self._fail_publication(exc, terminal=terminal)

    def _fail_publication(self, error: events.EventStreamError, *, terminal=False) -> None:
        self.publication_failed = True
        self.publisher.close()
        logger.warning("Run event publication failed (%s)", str(error)[:80])
        if terminal:
            return
        self.guard.mark_live_events_incomplete()

    def _write_acceptance(self) -> None:
        with self.guard.write(str(self.claim.conversation_id)) as write:
            write.conversation.record_acceptance()

    def _conversation(self):
        """Resolve the currently owned conversation for a fenced write."""
        return AgentConversation.objects.get(pk=self.claim.conversation_id, user_id=self.claim.user_id)

    def _write_refusal(self, anchor_id: str) -> None:
        with self.guard.write(str(self.claim.conversation_id)):
            AgentRun.objects.filter(pk=self.claim.run_id).update(refusal_anchor_id=anchor_id)

    def _run_graph(self, turn) -> tuple[TerminationKind, str]:
        """Execute accepted work and distinguish successful completion from stream failure."""
        agent_graph = turn.prepare()
        self._write_acceptance()
        unavailable = [server.as_dict() for server in agent_graph.mcp_unavailable_servers]
        self.guard.store_display(agent_graph.model_spec, agent_graph.context_limit, unavailable)
        termination = TerminationKind.FAILED
        failure_code = ""
        config, context = turn.invocation()
        stream = stream_chat_events(
            agent_graph.graph,
            turn.prompt,
            config,
            context,
            agent_graph.context_limit,
            control=self.control,
            on_cleanup=self._begin_cleanup,
        )
        try:
            try:
                for name, payload in stream:
                    if self.control.drain_requested:
                        break
                    if name == DONE:
                        termination = TerminationKind.COMPLETED
                    if name == ERROR and payload.get("error_code"):
                        failure_code = payload["error_code"]
                        continue
                    self._publish(name, payload)
            except (RunDraining, SoftTimeLimitExceeded):
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if self.cleanup_deadline is not None:
                    self.guard.record_interruption("graph_cleanup_unconfirmed", drain_reason="graph_cleanup")
                    logger.warning("Chat graph shutdown failed (%s)", type(exc).__name__)
                raise
        finally:
            self._begin_cleanup()
            try:
                stream.close()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.guard.record_interruption("graph_cleanup_unconfirmed", drain_reason="graph_cleanup")
                logger.warning("Chat graph cleanup failed (%s)", type(exc).__name__)
            else:
                if isinstance(turn.runtime.saver, DjangoCheckpointSaver):
                    try:
                        turn.runtime.saver.prune_turn(config, channels=agent_graph.graph.channels)
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        logger.warning("Chat checkpoint pruning failed (%s)", type(exc).__name__)
        return termination, failure_code

    def _handle_drain(self) -> None:
        """Start bounded cleanup and signal MCP without taking the cleanup lock."""
        self._begin_cleanup()
        if self.mcp is not None:
            self.mcp.cancel()

    def _begin_cleanup(self):
        """Latch one cleanup deadline shared by graph, MCP and monitor shutdown."""
        with self._cleanup_lock:
            if self.cleanup_deadline is None:
                self.cleanup_deadline = time.monotonic() + self.claim.cleanup_timeout
            return self.cleanup_deadline

    def _cleanup_remaining(self):
        return max(0, self._begin_cleanup() - time.monotonic())

    def _start_monitor(self):
        self.monitor = threading.Thread(target=self._monitor_loop, name=f"chat-run-{self.claim.run_id}", daemon=True)
        self.monitor.start()

    def _monitor_loop(self):
        try:
            next_heartbeat = self.started
            while not self.stop_monitor.wait(max(0, next_heartbeat - time.monotonic())):
                close_old_connections()
                if not self._heartbeat_tick():
                    return
                next_heartbeat = time.monotonic() + self.heartbeat_interval
                with self._cleanup_lock:
                    if self.cleanup_deadline is not None:
                        next_heartbeat = min(next_heartbeat, self.cleanup_deadline)
        finally:
            connections.close_all()

    def _heartbeat_tick(self) -> bool:
        """Refresh authority once, then enforce local deadlines."""
        if not self.guard.heartbeat():
            return False
        now = time.monotonic()
        if now - self.started >= self.claim.soft_time_limit:
            self.guard.request_drain("soft_deadline")
        with self._cleanup_lock:
            cleanup_deadline = self.cleanup_deadline
        if cleanup_deadline is not None and now >= cleanup_deadline:
            self.guard.interrupt_cleanup_timeout()
            return False
        return True

    def _stop_monitor(self):
        self.stop_monitor.set()
        monitor = self.monitor
        if monitor is not None and monitor is not threading.current_thread() and monitor.ident is not None:
            monitor.join(timeout=1)


def execute_run(run_id: UUID | str) -> None:
    """Claim and execute one queued run, persisting its outcome."""
    claim = claim_run(UUID(str(run_id)))
    if claim is not None:
        AgentExecution(claim).run()
