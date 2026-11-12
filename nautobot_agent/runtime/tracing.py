"""Opt-in, fail-open capture of per-send LangChain execution spans."""

from __future__ import annotations

import logging
import threading
from asyncio import CancelledError
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from django.db import router, transaction
from django.utils import timezone
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs

from nautobot_agent import app_settings
from nautobot_agent.chat.runs import owner_lock_queryset
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRunStatus,
    ExecutionStatus,
    TraceCaptureState,
)
from nautobot_agent.runtime.control import RunDraining
from nautobot_agent.runtime.trace_payloads import (
    callback_envelope,
    event_entry,
    event_log_details,
    model_call_summaries,
    snapshot,
    span_metadata,
)

logger = logging.getLogger(__name__)


def _prepared_values(trace_id, span):
    """Build one complete persisted span row."""
    return {
        "trace_id": trace_id,
        "run_id": span.run_id,
        "parent_run_id": span.parent_run_id,
        "kind": span.kind,
        "name": span.name[:512],
        "status": span.status,
        "started_at": span.started_at,
        "ended_at": span.ended_at,
        "input_data": span.input_data,
        "output_data": span.output_data,
        "error_data": span.error_data,
        "details": dict(span.details),
        "event_counts": dict(span.event_counts),
    }


_BATCH_MAX_ROWS = 50


@dataclass
class _Span:  # pylint: disable=too-many-instance-attributes
    run_id: UUID
    parent_run_id: UUID | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    status: str = ExecutionStatus.RUNNING
    input_data: dict | None = None
    output_data: dict | None = None
    error_data: dict | None = None
    details: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    event_counts: Counter = field(default_factory=Counter)


class _Recorder(BaseCallbackHandler):  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """Thread-safe callback recorder shared by classifier and graph invocations."""

    raise_error = False
    run_inline = True

    def __init__(self):
        super().__init__()
        self.origin_at = timezone.now()
        self.origin = perf_counter()
        self.spans: dict[UUID, _Span] = {}
        self.event_sequence = 0
        self.lock = threading.Lock()
        self.frozen = False
        self._frozen_at = None
        self._failed = False
        self._committed = False

    def _wall(self, elapsed):
        """Convert a monotonic timestamp using this recorder's fixed wall-clock origin."""
        return self.origin_at + timedelta(seconds=elapsed - self.origin)

    def freeze(self) -> None:
        """Stop accepting callbacks and fix the end time used for unfinished spans."""
        with self.lock:
            if not self.frozen:
                self.frozen = True
                self._frozen_at = self._wall(perf_counter())

    def flush(self, trace_id, run_status) -> TraceCaptureState:
        """Persist captured spans and classify capture health."""
        try:
            return self._flush(trace_id, run_status)
        except SoftTimeLimitExceeded:
            self._failed = True
            return self._state()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._failed = True
            logger.warning("Execution trace preparation failed (%s)", type(exc).__name__)
            return self._state()

    def _state(self):
        if not self._failed:
            return TraceCaptureState.COMPLETE
        if self._committed:
            return TraceCaptureState.PARTIAL
        return TraceCaptureState.FAILED

    def _flush(self, trace_id, run_status) -> TraceCaptureState:
        """Classify unfinished spans by the committed run status and persist rows in fixed batches."""
        self.freeze()
        incomplete_count = 0
        with self.lock:
            for span in self.spans.values():
                if span.ended_at is None:
                    span.ended_at = self._frozen_at
                    span.status = (
                        ExecutionStatus.CANCELLED
                        if run_status == AgentRunStatus.CANCELLED
                        else ExecutionStatus.INCOMPLETE
                    )
                    if span.status == ExecutionStatus.INCOMPLETE:
                        self._failed = True
                        incomplete_count += 1
                span.details.update(event_log_details(span.events))
            spans = tuple(self.spans.values())
        if incomplete_count:
            logger.warning("Execution trace has incomplete spans trace=%s count=%s", trace_id, incomplete_count)
        using = router.db_for_write(AgentExecutionSpan)
        conversation_id = (
            AgentExecutionTrace.objects.using(using)
            .filter(pk=trace_id)
            .values_list("conversation_id", flat=True)
            .first()
        )
        if conversation_id is None:
            self._failed = True
            return self._state()
        pending = []

        def write_batch(rows):
            with transaction.atomic(using=using):
                # Guarded conversation deletion takes this lock before removing any spans.
                # A trace lock alone would be too late to protect the child-deletion phase.
                if not AgentConversation.objects.using(using).select_for_update().filter(pk=conversation_id).exists():
                    return False
                if not AgentExecutionTrace.objects.using(using).select_for_update().filter(pk=trace_id).exists():
                    return False
                AgentExecutionSpan.objects.using(using).bulk_create(rows)
            self._committed = True
            return True

        def submit_batch():
            nonlocal pending
            if not pending:
                return True
            rows, pending = pending, []
            try:
                if not write_batch(rows):
                    self._failed = True
                    return False
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._failed = True
                logger.warning("Execution trace batch failed trace=%s (%s)", trace_id, type(exc).__name__)
                for row in rows:
                    try:
                        if not write_batch([row]):
                            return False
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception as row_exc:  # pylint: disable=broad-exception-caught
                        logger.warning(
                            "Execution trace row failed trace=%s run=%s (%s)",
                            trace_id,
                            row.run_id,
                            type(row_exc).__name__,
                        )
            return True

        for span in spans:
            pending.append(AgentExecutionSpan(**_prepared_values(trace_id, span)))
            if len(pending) >= _BATCH_MAX_ROWS and not submit_batch():
                return self._state()
        submit_batch()
        return self._state()

    def _guard(self, callback):
        with self.lock:
            if self.frozen:
                return
            try:
                callback()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._failed = True
                logger.warning("Execution trace callback failed (%s)", type(exc).__name__)

    def _open(self, kind, serialized, run_id, parent_run_id, input_data=None, **kwargs):
        now = perf_counter()
        serialized_name = serialized.get("name") if isinstance(serialized, Mapping) else None
        name = kwargs.get("name") or serialized_name or kind
        details = span_metadata(kind, kwargs)
        self.spans[run_id] = _Span(
            run_id,
            parent_run_id,
            kind,
            str(name),
            self._wall(now),
            input_data=input_data,
            details=details,
        )

    def _record_event(self, span, kind, payload, *, include_payload=True):
        """Append a complete detached event while the recorder lock is held."""
        self.event_sequence += 1
        now = perf_counter()
        span.events.append(
            event_entry(self.event_sequence, self._wall(now), kind, payload, include_payload=include_payload)
        )

    def _close(self, run_id, status=ExecutionStatus.OK, *, output_data=None, error_data=None, **details):
        span = self.spans.get(run_id)
        if span is None:
            return
        now = perf_counter()
        if error_data is not None or status == ExecutionStatus.ERROR:
            self._record_event(span, "error", None, include_payload=False)
        span.ended_at = self._wall(now)
        span.status = status
        span.output_data = output_data
        span.error_data = error_data
        span.details.update(details)

    def _error(self, run_id, error, data):
        status = (
            ExecutionStatus.CANCELLED
            if isinstance(error, CancelledError | KeyboardInterrupt | GeneratorExit | RunDraining)
            else ExecutionStatus.ERROR
        )
        self._close(run_id, status, error_data=data, error_type=type(error).__name__)

    def _capture_error(self, run_id, error, kwargs):
        if run_id in self.spans:
            self._error(run_id, error, callback_envelope(run_id, {"error": error}, kwargs))

    def _begin(self, kind, serialized, arguments, run_id, parent_run_id, kwargs):  # pylint: disable=too-many-arguments
        payload = callback_envelope(run_id, {**arguments, "parent_run_id": parent_run_id}, kwargs)
        self._open(kind, serialized, run_id, parent_run_id, payload, **kwargs)

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self._guard(lambda: self._open("chain", serialized, run_id, parent_run_id, **kwargs))

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._guard(lambda: self._close(run_id))

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._guard(lambda: self._capture_error(run_id, error, kwargs))

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._guard(
            lambda: self._begin(
                "chat_model",
                serialized,
                {"messages": messages},
                run_id,
                parent_run_id,
                {key: value for key, value in kwargs.items() if key != "invocation_params"},
            )
        )

    def discard_model_start(self, run_id):
        """Remove a provisional start when admission rejects before generation."""

        def discard():
            span = self.spans.get(run_id)
            if span is not None and span.kind == "chat_model" and span.ended_at is None:
                del self.spans[run_id]

        self._guard(discard)

    def on_llm_end(self, response, *, run_id, **kwargs):
        def record():
            if run_id not in self.spans:
                return
            payload = callback_envelope(run_id, {"response": response}, kwargs)
            summaries = {}
            try:
                summaries = model_call_summaries(payload)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Execution trace summary failed (%s)", type(exc).__name__)
            self._close(run_id, output_data=payload, **summaries)

        self._guard(record)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._guard(lambda: self._capture_error(run_id, error, kwargs))

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._guard(lambda: self._begin("tool", serialized, {"input_str": input_str}, run_id, parent_run_id, kwargs))

    def on_tool_end(self, output, *, run_id, **kwargs):
        def record():
            if run_id not in self.spans:
                return
            payload = callback_envelope(run_id, {"output": output}, kwargs)
            status = ExecutionStatus.ERROR if getattr(output, "status", None) == "error" else ExecutionStatus.OK
            self._close(run_id, status, output_data=payload)

        self._guard(record)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._guard(lambda: self._capture_error(run_id, error, kwargs))

    def _event(self, event, run_id):
        span = self.spans.get(run_id)
        if span is not None:
            span.event_counts[event] += 1

    def _captured_event(self, event, run_id, payload):
        span = self.spans.get(run_id)
        if span is not None:
            self._record_event(span, event, snapshot(payload))

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        self._guard(lambda: self._event("token", run_id))

    def on_stream_event(self, event, *, run_id, **kwargs):
        self._guard(lambda: self._event("stream", run_id))

    def on_retry(self, retry_state, *, run_id, **kwargs):
        self._guard(lambda: self._captured_event("on_retry", run_id, {"retry_state": retry_state, "kwargs": kwargs}))


class ExecutionTrace:
    """Worker-owned handle for freezing and persisting trace capture."""

    def __init__(self, recorder=None, trace_id=None):
        """Create an inert handle unless a recorder and persisted row are supplied."""
        self._recorder = recorder
        self._trace_id = trace_id

    def config(self, base: RunnableConfig | None = None) -> RunnableConfig:
        """Merge this trace callback into an optional existing runnable config."""
        if self._recorder is None:
            return {} if base is None else base
        return merge_configs(base, {"callbacks": [self._recorder]})

    def discard_model_start(self, run_id):
        """Undo only a denied model start if native callback merging reordered handlers."""
        if self._recorder is not None:
            self._recorder.discard_model_start(run_id)

    @property
    def trace_id(self):
        """Return the associated persistent trace identity, if capture is enabled."""
        return self._trace_id

    def freeze(self) -> None:
        """Stop recording callbacks before the execution outcome is persisted."""
        if self._recorder is not None:
            self._recorder.freeze()

    def finish_capture(self, run_status) -> TraceCaptureState | None:
        """Persist spans once, then record capture health on the original ended, still-pending trace.

        ``run_status`` is the committed run outcome, or None when no terminal outcome is confirmed.
        The health update is best effort in its own transaction: it never recreates a deleted trace,
        overwrites observer-established health, or revises the execution outcome or end time.
        """
        recorder = self._recorder
        if recorder is None:
            return None
        state = recorder.flush(self._trace_id, run_status)
        try:
            using = router.db_for_write(AgentExecutionTrace)
            with transaction.atomic(using=using):
                AgentExecutionTrace.objects.using(using).filter(
                    pk=self._trace_id, capture_state=TraceCaptureState.PENDING, ended_at__isnull=False
                ).update(capture_state=state)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Execution trace capture health failed trace=%s (%s)", self._trace_id, type(exc).__name__)
        return state

    def abandon(self) -> None:
        """Stop capture after unconfirmed association and mark only the original pending trace incomplete."""
        recorder, trace_id = self._recorder, self._trace_id
        self._recorder = None
        self._trace_id = None
        if recorder is None:
            return
        recorder.freeze()
        try:
            using = router.db_for_write(AgentExecutionTrace)
            AgentExecutionTrace.objects.using(using).filter(
                pk=trace_id, capture_state=TraceCaptureState.PENDING
            ).update(
                status=ExecutionStatus.INCOMPLETE,
                capture_state=TraceCaptureState.FAILED,
                ended_at=timezone.now(),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Execution trace abandonment failed trace=%s (%s)", trace_id, type(exc).__name__)


def start_trace(user, conversation, request) -> ExecutionTrace:
    """Start a trace when enabled; failures return a no-op handle."""
    try:
        if not app_settings.tracing_enabled():
            return ExecutionTrace()
        recorder = _Recorder()
        using = router.db_for_write(AgentExecutionTrace)
        with transaction.atomic(using=using):
            # Match heartbeat's owner-first order before INSERT acquires conversation FK locks.
            if user is not None:
                owner_lock_queryset(using).filter(pk=user.pk).first()
            row = AgentExecutionTrace.objects.using(using).create(
                conversation=conversation,
                request=request,
                status=ExecutionStatus.RUNNING,
                capture_state=TraceCaptureState.PENDING,
                started_at=recorder.origin_at,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Execution trace start failed (%s)", type(exc).__name__)
        return ExecutionTrace()
    return ExecutionTrace(recorder, row.pk)
