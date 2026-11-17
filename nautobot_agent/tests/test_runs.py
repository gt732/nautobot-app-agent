"""Durable run admission, publication, and ownership-domain tests."""

# Keep durable lifecycle and its committed-row concurrency regressions together.
# pylint: disable=too-many-lines

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, connections, router, transaction
from django.db.models import DateTimeField, QuerySet, Value
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from kombu import Connection
from nautobot.users.models import Token

from nautobot_agent import tasks
from nautobot_agent.chat import runs
from nautobot_agent.chat.conversation import delete, delete_many
from nautobot_agent.chat.errors import BUSY_MESSAGE, ConversationBusy
from nautobot_agent.chat.execution import RunWriteGuard
from nautobot_agent.chat.runs import (
    AdmissionKind,
    RunReservationConflict,
    claim_run,
    finish_run,
    lock_conversation_reservations,
    locked_run_write,
    record_run_diagnostics,
    request_cancel,
    submit_run,
)
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    ExecutionStatus,
    HeartbeatNow,
    TraceCaptureState,
)
from nautobot_agent.tests.support.chat import claimed_run, create_reserved_run
from nautobot_agent.tests.support.db import CommittedTestCase


class RunPublicationTests(SimpleTestCase):
    """Exercise real Celery publication through an isolated in-memory broker."""

    @staticmethod
    def _invalidate_amqp_policy():
        """Drop Celery's cached protocol builder and retry-policy sender."""
        amqp = tasks.app.amqp
        for name in ("create_task_message", "send_task_message"):
            if name in amqp.__dict__:
                delattr(amqp, name)

    def _configure_publication(self, **extra):
        """Scope namespaced Django Celery settings and reset cached policy both ways."""
        # Resolve Celery's lazy Django configuration before patching the backing dictionary.
        tasks.app.conf.get("task_protocol")
        patcher = patch.dict(
            tasks.app.conf.changes,
            {
                "CELERY_TASK_ALWAYS_EAGER": False,
                "CELERY_TASK_PROTOCOL": 2,
                **extra,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._invalidate_amqp_policy)
        self._invalidate_amqp_policy()
        self.assertFalse(tasks.app.conf.task_always_eager)
        self.assertEqual(tasks.app.conf.task_protocol, 2)

    def _register_queue_cleanup(self, queue_name):
        def cleanup():
            try:
                with Connection("memory://") as cleanup_connection, cleanup_connection.channel() as channel:
                    channel.exchange_delete(exchange=queue_name)
                    channel.queue_delete(queue=queue_name)
            finally:
                tasks.app.amqp.queues.pop(queue_name, None)

        self.addCleanup(cleanup)

    def test_publish_run_delivers_consumer_visible_envelope(self):
        queue_name = f"agent-run-publication-{uuid.uuid4().hex}"
        run_id = uuid.uuid4()
        self._configure_publication()
        self._register_queue_cleanup(queue_name)
        memory_connection = Connection("memory://")
        with patch("nautobot_agent.tasks.app.connection_for_write", return_value=memory_connection):
            tasks.publish_run(run_id, uuid.uuid4(), queue_name, soft=30, cleanup=5, hard=46)

        with Connection("memory://") as consumer_connection, consumer_connection.SimpleQueue(queue_name) as consumer:
            message = None
            try:
                message = consumer.get(block=False)
                self.assertEqual(message.headers["task"], "nautobot_agent.execute_chat_run")
                self.assertEqual(message.payload[0], [str(run_id)])
                self.assertEqual(message.delivery_info["routing_key"], queue_name)
                self.assertEqual(message.headers["timelimit"], [46, 45])
            finally:
                if message is not None:
                    message.ack()


class ReservationTransitionTests(TestCase):
    """Persist transitions without admission or broker work."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="reservation-transitions")
        cls.conversation = AgentConversation.objects.create(user=cls.user)
        cls.reserved_run = create_reserved_run(cls.conversation, prompt="retained request")

    def test_terminal_transitions_retain_only_recorded_refusal_prompts(self):
        for anchor in (None, "", "retained-answer"):
            for status in (AgentRunStatus.REFUSED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED):
                with self.subTest(anchor=anchor, status=status):
                    run = create_reserved_run(self.conversation, prompt="retained request")
                    claimed_run(run.pk)
                    AgentRun.objects.filter(pk=run.pk).update(refusal_anchor_id=anchor)
                    finish_run(run.pk, status)
                    run.refresh_from_db()
                    self.assertEqual(run.prompt, "" if anchor is None else "retained request")
                    self.assertEqual(run.refusal_anchor_id, anchor)

    def test_claimed_bookkeeping_rejects_invalid_authority(self):
        claimed_run(self.reserved_run.pk)
        for case in ("interrupted", "not_running", "inconsistent_owner", "missing_run"):
            with self.subTest(case=case), transaction.atomic():
                run_id = self.reserved_run.pk
                if case == "interrupted":
                    finish_run(run_id, AgentRunStatus.INTERRUPTED, error_code="ownership_unavailable")
                elif case == "not_running":
                    AgentRun.objects.filter(pk=run_id).update(status=AgentRunStatus.COMPLETED)
                elif case == "inconsistent_owner":
                    other = get_user_model().objects.create_user(username="inconsistent-owner")
                    AgentConversation.objects.filter(pk=self.conversation.pk).update(user=other)
                else:
                    run_id = uuid.uuid4()
                with self.assertRaises(RunReservationConflict), locked_run_write(run_id) as write:
                    write.store_display("forbidden", 1, [])
                self.reserved_run.refresh_from_db()
                self.assertEqual(self.reserved_run.display_model, "")
                transaction.set_rollback(True)

    def test_claimed_run_write_persists_bookkeeping_and_cancellation_snapshot(self):
        run = self.reserved_run
        claimed_run(run.pk)
        trace = AgentExecutionTrace.objects.create(
            conversation=run.conversation,
            request="trace association",
            status=ExecutionStatus.OK,
            started_at=timezone.now(),
        )
        previous_heartbeat = timezone.now() - timedelta(seconds=10)
        AgentRun.objects.filter(pk=run.pk).update(cancel_requested=True, heartbeat_at=previous_heartbeat)
        model_spec = "openai:" + "x" * 505
        with locked_run_write(run.pk) as write:
            write.store_display(model_spec, 8192, [{"id": "server-id", "name": "Integration"}])
        run.refresh_from_db()
        self.assertEqual((run.display_model, run.display_context_limit), (model_spec, 8192))
        self.assertEqual(run.mcp_unavailable_servers, [{"id": "server-id", "name": "Integration"}])
        with locked_run_write(run.pk) as write:
            self.assertTrue(write.heartbeat())
            self.assertTrue(write.run.cancel_requested)
            write.mark_live_events_incomplete()
            write.store_display(None, None, [])
            write.link_trace(trace.pk)
        run.refresh_from_db()
        self.assertTrue(run.cancel_requested)
        self.assertGreater(run.heartbeat_at, previous_heartbeat)
        self.assertTrue(run.live_events_incomplete)
        self.assertEqual(run.display_model, "")
        self.assertIsNone(run.display_context_limit)
        self.assertEqual(run.mcp_unavailable_servers, [])
        self.assertEqual(run.trace_id, trace.pk)

    def test_interruption_is_terminal_releases_reservation_and_fences_late_finish(self):
        claimed_run(self.reserved_run.pk)
        trace = AgentExecutionTrace.objects.create(
            conversation=self.conversation,
            request="request",
            status=ExecutionStatus.RUNNING,
            started_at=timezone.now(),
        )
        self.reserved_run.trace = trace
        self.reserved_run.save(update_fields=("trace",))
        finish_run(
            self.reserved_run.pk,
            AgentRunStatus.INTERRUPTED,
            error_code="uncertain",
        )
        trace.refresh_from_db()
        self.assertEqual(trace.status, ExecutionStatus.INCOMPLETE)
        self.assertEqual(trace.capture_state, TraceCaptureState.PENDING)
        interrupted_at = trace.ended_at
        with self.assertRaises(RunReservationConflict):
            finish_run(
                self.reserved_run.pk,
                AgentRunStatus.COMPLETED,
            )
        trace.refresh_from_db()
        self.assertEqual((trace.status, trace.ended_at), (ExecutionStatus.INCOMPLETE, interrupted_at))
        self.assertEqual(trace.capture_state, TraceCaptureState.PENDING)
        self.reserved_run.refresh_from_db()
        self.assertEqual(self.reserved_run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(self.reserved_run.error_code, "uncertain")
        self.assertEqual(self.reserved_run.prompt, "")
        self.assertFalse(AgentRun.objects.active().filter(conversation=self.conversation).exists())
        for status in AgentRunStatus.values:
            with self.subTest(status=status), self.assertRaises((RunReservationConflict, ValueError)):
                finish_run(self.reserved_run.pk, status)

    def test_inactive_owner_claim_preserves_foreign_or_terminal_runs(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        for case in ("inactive_owner", "inconsistent_owner", "nonqueued"):
            with self.subTest(case=case), transaction.atomic():
                if case == "inconsistent_owner":
                    other = get_user_model().objects.create_user(username="inactive-reservation-owner")
                    AgentConversation.objects.filter(pk=self.conversation.pk).update(user=other)
                elif case == "nonqueued":
                    AgentRun.objects.filter(pk=self.reserved_run.pk).update(status=AgentRunStatus.COMPLETED)

                self.conversation.refresh_from_db()
                self.assertIsNone(claim_run(self.reserved_run.pk))
                self.reserved_run.refresh_from_db()
                self.conversation.refresh_from_db()
                expected_status = {
                    "inactive_owner": AgentRunStatus.FAILED,
                    "inconsistent_owner": AgentRunStatus.QUEUED,
                    "nonqueued": AgentRunStatus.COMPLETED,
                }[case]
                self.assertEqual(self.reserved_run.status, expected_status)
                self.assertEqual(
                    self.reserved_run.prompt,
                    "" if case == "inactive_owner" else "retained request",
                )
                if case == "inactive_owner":
                    self.assertEqual(self.reserved_run.error_code, "principal_inactive")
                    self.assertFalse(AgentRun.objects.active().filter(conversation=self.conversation).exists())

                transaction.set_rollback(True)

    def test_trace_capture_is_fenced_and_isolated(self):
        original_save = AgentExecutionTrace.save

        def duplicate_trace(instance, *args, **kwargs):
            # A real constraint error poisons the savepoint unless isolated correctly.
            kwargs.pop("update_fields", None)
            return original_save(instance, *args, force_insert=True, **kwargs)

        conversation = AgentConversation.objects.create(user=self.user)
        run = create_reserved_run(conversation, prompt="request")
        claimed_run(run.pk)
        trace = AgentExecutionTrace.objects.create(
            conversation=conversation,
            request="request",
            status=ExecutionStatus.RUNNING,
            started_at=timezone.now() - timedelta(seconds=2),
        )
        run.trace = trace
        run.save(update_fields=("trace",))
        with patch.object(AgentExecutionTrace, "save", new=duplicate_trace):
            finish_run(run.pk, AgentRunStatus.COMPLETED)
        run.refresh_from_db()
        trace.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(run.prompt, "")
        self.assertEqual(trace.status, ExecutionStatus.RUNNING)
        self.assertIsNone(trace.ended_at)

    def test_trace_execution_outcome_preserves_capture_health(self):
        for status, expected in (
            (AgentRunStatus.COMPLETED, ExecutionStatus.OK),
            (AgentRunStatus.REFUSED, ExecutionStatus.REFUSED),
            (AgentRunStatus.FAILED, ExecutionStatus.ERROR),
            (AgentRunStatus.CANCELLED, ExecutionStatus.CANCELLED),
            (AgentRunStatus.INTERRUPTED, ExecutionStatus.INCOMPLETE),
        ):
            for capture_state in TraceCaptureState.values:
                with self.subTest(status=status, capture_state=capture_state):
                    self.assert_trace_outcome_preserves_capture(status, expected, capture_state)

    def assert_trace_outcome_preserves_capture(self, status, expected, capture_state):
        """Verify terminal run persistence does not take ownership of trace capture health."""
        conversation = AgentConversation.objects.create(user=self.user)
        run = create_reserved_run(conversation, prompt="request")
        claimed_run(run.pk)
        trace = AgentExecutionTrace.objects.create(
            conversation=conversation,
            request="request",
            status=ExecutionStatus.RUNNING,
            capture_state=capture_state,
            started_at=timezone.now(),
        )
        run.trace = trace
        run.save(update_fields=("trace",))
        finish_run(run.pk, status)
        trace.refresh_from_db()
        self.assertEqual(trace.status, expected)
        self.assertEqual(trace.capture_state, capture_state)
        self.assertIsNotNone(trace.ended_at)
        self.assertGreaterEqual(trace.duration_seconds, 0)


class DurableRunTests(CommittedTestCase):  # pylint: disable=too-many-public-methods
    """Exercise real commit callbacks and the database run fence."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username=f"run-{uuid.uuid4()}")
        self.client_request_id = uuid.uuid4()
        self.published = []

    def test_claim_and_heartbeat_use_database_clock_despite_worker_skew(self):
        for offset in (None, -365, 365):
            with self.subTest(offset=offset):
                run = self.admit(request_id=uuid.uuid4())
                before = timezone.now()
                clock = (
                    nullcontext()
                    if offset is None
                    else patch("nautobot_agent.chat.runs.timezone.now", return_value=before + timedelta(days=offset))
                )
                with clock:
                    claimed_run(run.pk)
                    with locked_run_write(run.pk) as write:
                        self.assertFalse(write.heartbeat())
                after = timezone.now()
                run.refresh_from_db()
                tolerance = timedelta(seconds=1)
                self.assertGreaterEqual(run.heartbeat_at, before - tolerance)
                self.assertLessEqual(run.heartbeat_at, after + tolerance)
                finish_run(run.pk, AgentRunStatus.COMPLETED)

    def publish(self, run_id, user_id, queue, **limits):
        """Capture the single publication attempt without delivering to a worker."""
        self.published.append((run_id, user_id, queue, limits))

    def submit(self, *, conversation_id=None, request_id=None, prompt="normalized prompt", publish=None):
        """Submit form-normalized input through the real admission transaction."""
        return submit_run(
            self.user,
            request_id or self.client_request_id,
            prompt,
            conversation_id,
            publish or self.publish,
        )

    def admit(self, **kwargs):
        """Return the run from an expected admitted result."""
        result = self.submit(**kwargs)
        assert result.kind == AdmissionKind.ADMITTED
        assert result.run is not None
        return result.run

    def test_admission_rejects_disabled_autocommit_without_publication(self):
        transaction.set_autocommit(False)
        try:
            with self.assertRaises(RuntimeError):
                self.submit()
            self.assertFalse(AgentRun.objects.filter(user=self.user).exists())
            self.assertEqual(self.published, [])
        finally:
            connection.rollback()
            transaction.set_autocommit(True)

    def test_concurrent_admission_serializes(self):  # pylint: disable=too-many-locals
        conversation = AgentConversation.objects.create(user=self.user)
        barrier = threading.Barrier(3)
        request_ids = (uuid.uuid4(), uuid.uuid4())

        def submit_concurrently(request_id):
            try:
                barrier.wait(timeout=5)
                result = self.submit(
                    conversation_id=conversation.pk,
                    request_id=request_id,
                    prompt=f"prompt-{request_id}",
                )
                return ("accepted" if result.kind == AdmissionKind.ADMITTED else "blocked"), result, request_id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit_concurrently, request_id) for request_id in request_ids]
            barrier.wait(timeout=5)
            outcomes = [future.result(timeout=10) for future in futures]

        accepted = [outcome for outcome in outcomes if outcome[0] == "accepted"]
        blocked = [outcome for outcome in outcomes if outcome[0] == "blocked"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0][1].kind, AdmissionKind.BUSY)
        result = accepted[0][1]
        assert result.run is not None
        run = result.run
        self.assertEqual(result.kind, AdmissionKind.ADMITTED)
        self.assertEqual(run.conversation_id, conversation.pk)

        self.assertEqual(len(self.published), 1)

        duplicate_user = get_user_model().objects.create_user(username=f"duplicate-{uuid.uuid4()}")
        duplicate_request_id = uuid.uuid4()
        duplicate_barrier = threading.Barrier(3)
        duplicate_publications = []

        def duplicate_submit(prompt):
            try:
                duplicate_barrier.wait(timeout=5)
                return submit_run(
                    duplicate_user,
                    duplicate_request_id,
                    prompt,
                    None,
                    lambda *args, **kwargs: duplicate_publications.append((args, kwargs)),
                )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            duplicate_futures = [
                executor.submit(duplicate_submit, "first duplicate body"),
                executor.submit(duplicate_submit, "second duplicate body"),
            ]
            duplicate_barrier.wait(timeout=5)
            duplicate_results = [future.result(timeout=10) for future in duplicate_futures]
        self.assertEqual(len(duplicate_publications), 1)
        duplicate_run_ids = []
        for result in duplicate_results:
            assert result.run is not None
            duplicate_run_ids.append(result.run.pk)
        self.assertEqual(len(set(duplicate_run_ids)), 1)
        self.assertCountEqual(
            [result.kind for result in duplicate_results],
            [AdmissionKind.ADMITTED, AdmissionKind.DUPLICATE],
        )
        duplicate_run = duplicate_results[0].run
        assert duplicate_run is not None

    def test_claim_is_single_use_and_finish_clears_prompt(self):
        run = self.admit()
        claimed_run(run.pk)
        self.assertIsNone(claim_run(run.pk))
        finish_run(run.pk, AgentRunStatus.COMPLETED)
        run.refresh_from_db()
        self.assertEqual(run.prompt, "")

        with self.assertRaises(RunReservationConflict):
            finish_run(run.pk, AgentRunStatus.FAILED)

    def test_interrupted_run_cannot_write_or_finish_successor_reservation(self):
        run = self.admit()
        claimed_run(run.pk)
        finish_run(run.pk, AgentRunStatus.INTERRUPTED, error_code="stale_heartbeat")
        successor = self.admit(conversation_id=run.conversation_id, request_id=uuid.uuid4())
        claimed_run(successor.pk)
        with self.assertRaises(RunReservationConflict), locked_run_write(run.pk):
            pass
        for status in (AgentRunStatus.COMPLETED, AgentRunStatus.INTERRUPTED):
            with self.subTest(status=status), self.assertRaises(RunReservationConflict):
                finish_run(run.pk, status)
        run.refresh_from_db()
        successor.refresh_from_db()
        self.assertEqual((run.status, run.error_code), (AgentRunStatus.INTERRUPTED, "stale_heartbeat"))
        self.assertEqual(successor.status, AgentRunStatus.RUNNING)

    def assert_queue_expired(self, run):
        """Assert the durable expiry outcome: released, prompt erased, never claimed or traced."""
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.DISPATCH_FAILED)
        self.assertEqual(run.error_code, "queue_wait_expired")
        self.assertEqual(run.prompt, "")
        self.assertIsNone(run.heartbeat_at)
        self.assertIsNone(run.trace_id)
        self.assertFalse(AgentRun.objects.active().filter(conversation_id=run.conversation_id).exists())

    def test_claim_expires_at_exact_deadline_and_starts_once_before_it(self):
        deadline = timezone.now()
        expired = self.admit(prompt="exact deadline")
        AgentRun.objects.filter(pk=expired.pk).update(queue_expires_at=deadline)
        # Freeze the SQL clock expression, not the transition: real queries and locks still execute.
        with patch.object(runs, "HeartbeatNow", return_value=Value(deadline, output_field=DateTimeField())):
            self.assertIsNone(claim_run(expired.pk))
        self.assert_queue_expired(expired)
        self.assertIsNone(claim_run(expired.pk))
        self.assert_queue_expired(expired)

        eligible = self.admit(request_id=uuid.uuid4(), prompt="before deadline")
        AgentRun.objects.filter(pk=eligible.pk).update(queue_expires_at=deadline)
        before = Value(deadline - timedelta(microseconds=1), output_field=DateTimeField())
        with patch.object(runs, "HeartbeatNow", return_value=before):
            self.assertIsNotNone(claim_run(eligible.pk))
            self.assertIsNone(claim_run(eligible.pk))
        eligible.refresh_from_db()
        self.assertEqual(eligible.status, AgentRunStatus.RUNNING)
        self.assertEqual(eligible.error_code, "")
        # A claim consumes queue eligibility; its later deadline cannot terminate active execution.
        reconciled = runs.reconcile_run(eligible.pk)
        assert reconciled is not None
        self.assertEqual(reconciled.status, AgentRunStatus.RUNNING)
        finish_run(eligible.pk, AgentRunStatus.COMPLETED)

    def test_delayed_task_delivery_after_expiry_initializes_nothing(self):
        run = self.admit()
        AgentRun.objects.filter(pk=run.pk).update(queue_expires_at=HeartbeatNow())
        with patch("nautobot_agent.chat.execution.AgentExecution") as execution:
            tasks.execute_chat_run(str(run.pk))
        execution.assert_not_called()
        self.assert_queue_expired(run)
        self.assertFalse(AgentExecutionTrace.objects.filter(conversation_id=run.conversation_id).exists())

    def test_admission_expires_owner_reservations_before_counting(self):
        blocker = self.admit(prompt="expired blocker")
        elsewhere = self.admit(request_id=uuid.uuid4(), prompt="other conversation")
        AgentRun.objects.filter(pk__in=(blocker.pk, elsewhere.pk)).update(queue_expires_at=HeartbeatNow())

        successor = self.admit(conversation_id=blocker.conversation_id, request_id=uuid.uuid4())
        self.assertEqual(successor.status, AgentRunStatus.QUEUED)
        self.assertEqual(successor.conversation_id, blocker.conversation_id)
        blocker.refresh_from_db()
        self.assertEqual((blocker.status, blocker.error_code), (AgentRunStatus.DISPATCH_FAILED, "queue_wait_expired"))
        elsewhere.refresh_from_db()
        self.assertEqual(
            (elsewhere.status, elsewhere.error_code), (AgentRunStatus.DISPATCH_FAILED, "queue_wait_expired")
        )
        self.assertEqual(len(self.published), 3)

    def test_cancellation_rechecks_expiry_committed_while_waiting_for_locks(self):
        run = self.admit()
        locked, release = threading.Event(), threading.Event()

        def expire_while_holding_locks():
            try:
                with runs._locked(run.pk):  # pylint: disable=protected-access
                    AgentRun.objects.filter(pk=run.pk).update(queue_expires_at=HeartbeatNow())
                    locked.set()
                    if not release.wait(5):
                        raise TimeoutError("lock holder was not released")
            finally:
                connections.close_all()

        def cancel():
            try:
                return request_cancel(run.pk, self.user)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(expire_while_holding_locks)
            self.assertTrue(locked.wait(5))
            cancelled = executor.submit(cancel)
            release.set()
            holder.result(timeout=10)
            result = cancelled.result(timeout=10)
        assert result is not None
        self.assertEqual((result.status, result.error_code), (AgentRunStatus.DISPATCH_FAILED, "queue_wait_expired"))
        self.assert_queue_expired(run)
        self.assertIsNone(claim_run(run.pk))

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL FOR NO KEY UPDATE semantics")
    def test_run_lock_does_not_block_unrelated_inserts_referencing_owner(self):
        run = self.admit()
        locked, release = threading.Event(), threading.Event()

        def hold_run_locks():
            try:
                with runs._locked(run.pk):  # pylint: disable=protected-access
                    locked.set()
                    if not release.wait(10):
                        raise TimeoutError("lock holder was not released")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold_run_locks)
            try:
                self.assertTrue(locked.wait(5))
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '2s'")
                    Token.objects.create(user=self.user)
            finally:
                release.set()
                holder.result(timeout=10)
        self.assertTrue(Token.objects.filter(user=self.user).exists())

    def test_publication_failure_never_overwrites_expiry_or_claim(self):
        def expire_then_fail(run_id, *_args, **_kwargs):
            AgentRun.objects.filter(pk=run_id).update(queue_expires_at=HeartbeatNow())
            self.assertIsNone(claim_run(run_id))
            raise RuntimeError("broker unavailable")

        expired = self.admit(publish=expire_then_fail)
        self.assert_queue_expired(expired)

        def claim_then_fail(run_id, *_args, **_kwargs):
            self.assertIsNotNone(claim_run(run_id))
            raise RuntimeError("broker unavailable")

        claimed = self.admit(request_id=uuid.uuid4(), publish=claim_then_fail)
        claimed.refresh_from_db()
        self.assertEqual((claimed.status, claimed.error_code), (AgentRunStatus.RUNNING, ""))
        finish_run(claimed.pk, AgentRunStatus.COMPLETED)

    def test_stale_reconciliation_fails_only_pending_trace_capture(self):
        for capture_state in TraceCaptureState.values:
            with self.subTest(capture_state=capture_state):
                run = self.admit(request_id=uuid.uuid4())
                claimed_run(run.pk)
                trace = AgentExecutionTrace.objects.create(
                    conversation_id=run.conversation_id,
                    request="stale capture",
                    status=ExecutionStatus.RUNNING,
                    capture_state=capture_state,
                    started_at=timezone.now(),
                )
                AgentRun.objects.filter(pk=run.pk).update(
                    trace=trace,
                    heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)),
                )
                run.refresh_from_db()
                last_heartbeat = run.heartbeat_at
                runs.reconcile_run(run.pk)
                trace.refresh_from_db()
                self.assertEqual(trace.status, ExecutionStatus.INCOMPLETE)
                self.assertEqual(trace.ended_at, last_heartbeat)
                self.assertEqual(
                    trace.capture_state,
                    TraceCaptureState.FAILED if capture_state == TraceCaptureState.PENDING else capture_state,
                )

    def test_stale_interruption_admits_successor_that_late_worker_cannot_touch(self):
        run = self.admit()
        claimed_run(run.pk)
        AgentRun.objects.filter(pk=run.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        admission = self.submit(conversation_id=run.conversation_id, request_id=uuid.uuid4())
        self.assertEqual(admission.kind, AdmissionKind.ADMITTED)
        successor = cast(AgentRun, admission.run)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(run.prompt, "")
        claimed_run(successor.pk)
        with self.assertRaises(RunReservationConflict):
            finish_run(run.pk, AgentRunStatus.COMPLETED)
        with self.assertRaises(RunReservationConflict), locked_run_write(run.pk):
            pass
        successor.refresh_from_db()
        self.assertEqual(successor.status, AgentRunStatus.RUNNING)

    def test_admission_reconciles_owner_stale_runs_without_touching_other_users(self):
        stale = self.admit(request_id=uuid.uuid4())
        other_stale = self.admit(request_id=uuid.uuid4())
        foreign_user = get_user_model().objects.create_user(username=f"stale-foreign-{uuid.uuid4()}")
        foreign = create_reserved_run(AgentConversation.objects.create(user=foreign_user), prompt="foreign stale")
        for run in (stale, other_stale, foreign):
            claimed_run(run.pk)
        AgentRun.objects.filter(pk__in=(stale.pk, other_stale.pk, foreign.pk)).update(
            heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31))
        )
        admission = self.submit(request_id=uuid.uuid4())

        self.assertEqual(admission.kind, AdmissionKind.ADMITTED)
        self.assertNotIn(admission.run.conversation_id, (stale.conversation_id, other_stale.conversation_id))
        stale.refresh_from_db()
        other_stale.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(stale.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(other_stale.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(foreign.status, AgentRunStatus.RUNNING)

    def test_renewed_heartbeat_wins_against_stale_snapshot(self):  # pylint: disable=too-many-locals
        run = self.admit()
        claimed_run(run.pk)
        AgentRun.objects.filter(pk=run.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        heartbeat_locked, allow_heartbeat_commit, stale_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def set_repeatable_read():
            if connection.vendor != "mysql":
                return None
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT @@transaction_isolation")
                isolation = cursor.fetchone()[0]
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return isolation

        def restore_isolation(isolation):
            if isolation is not None:
                normalized = isolation.replace("-", " ").upper()
                with connections["default"].cursor() as cursor:
                    cursor.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {normalized}")

        def commit_new_heartbeat():
            isolation = set_repeatable_read()
            try:
                with locked_run_write(run.pk) as write:
                    heartbeat_locked.set()
                    if not allow_heartbeat_commit.wait(5):
                        raise TimeoutError("heartbeat transaction was not released")
                    write.heartbeat()
            finally:
                restore_isolation(isolation)
                connections.close_all()

        def recheck_stale():
            isolation = set_repeatable_read()
            try:
                with transaction.atomic():
                    AgentRun.objects.get(pk=run.pk)
                    stale_started.set()
                    return runs.reconcile_run(run.pk)
            finally:
                restore_isolation(isolation)
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            heartbeat = executor.submit(commit_new_heartbeat)
            self.assertTrue(heartbeat_locked.wait(5))
            stale = executor.submit(recheck_stale)
            self.assertTrue(stale_started.wait(5))
            allow_heartbeat_commit.set()
            heartbeat.result(timeout=10)
            reconciled = stale.result(timeout=10)
            assert reconciled is not None
            self.assertEqual(reconciled.status, AgentRunStatus.RUNNING)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.RUNNING)
        self.assertGreater(run.heartbeat_at, timezone.now() - timedelta(seconds=5))


class RunTransitionTests(TestCase):
    """Persisted transitions reject replay and revoke authority without discarding uncertain work."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="run-transitions")

    def test_state_machine_transitions(self):
        conversation = AgentConversation.objects.create(user=self.user)
        run = create_reserved_run(conversation, prompt="retained uncertain request")
        runs.reconcile_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.QUEUED)
        self.assertEqual(run.prompt, "retained uncertain request")

        claimed_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.RUNNING)
        self.assertIsNone(claim_run(run.pk))
        with self.assertRaises(ValueError):
            finish_run(run.pk, AgentRunStatus.DISPATCH_FAILED)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.RUNNING)

        max_length = cast(Any, AgentRun)._meta.get_field("error_code").max_length
        finish_run(run.pk, AgentRunStatus.INTERRUPTED, error_code="x" * (max_length + 16))
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertIsNone(claim_run(run.pk))
        with self.assertRaises(RunReservationConflict):
            finish_run(run.pk, AgentRunStatus.COMPLETED)
        run.refresh_from_db()
        self.assertTrue(run.is_terminal)
        self.assertEqual(run.prompt, "")
        self.assertLessEqual(len(run.error_code), max_length)
        self.assertTrue(run.error_code.startswith("x"))

        failed = create_reserved_run(AgentConversation.objects.create(user=self.user), prompt="request")
        claimed_run(failed.pk)
        finish_run(failed.pk, AgentRunStatus.FAILED, error_code="y" * (max_length + 16))
        failed.refresh_from_db()
        self.assertLessEqual(len(failed.error_code), max_length)
        self.assertTrue(failed.error_code.startswith("y"))


class RunDiagnosticsTests(TestCase):
    """Monotonic diagnostics enrich only the original owned row and never alter its lifecycle."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="run-diagnostics")

    def finished_run(self, status=AgentRunStatus.COMPLETED):
        """Return a guard and its run, already terminal with a linked trace."""
        conversation = AgentConversation.objects.create(user=self.user)
        run = create_reserved_run(conversation, prompt="request")
        guard = RunWriteGuard(claimed_run(run.pk))
        trace = AgentExecutionTrace.objects.create(
            conversation=conversation, request="request", status="running", started_at=timezone.now()
        )
        guard.link_trace(trace.pk)
        finish_run(run.pk, status, error_code="kept")
        run.refresh_from_db()
        return guard, run

    def test_late_evidence_enriches_only_the_original_terminal_row(self):
        guard, run = self.finished_run()
        _, other = self.finished_run()
        conversation = run.conversation
        conversation.refresh_from_db()
        trace = AgentExecutionTrace.objects.get(pk=run.trace_id)
        self.assertFalse(run.external_outcome_unknown or run.cleanup_incomplete)

        guard.mark_mcp_uncertain()
        guard.merge_diagnostics()
        run.refresh_from_db()
        self.assertTrue(run.external_outcome_unknown)
        self.assertFalse(run.cleanup_incomplete)
        guard.mark_mcp_cleanup_incomplete()
        guard.merge_diagnostics()

        current = AgentRun.objects.get(pk=run.pk)
        self.assertTrue(current.external_outcome_unknown and current.cleanup_incomplete)
        self.assertEqual(
            (current.status, current.prompt, current.error_code, current.trace_id),
            (AgentRunStatus.COMPLETED, "", "kept", run.trace_id),
        )
        refreshed_trace = AgentExecutionTrace.objects.get(pk=trace.pk)
        self.assertEqual((refreshed_trace.status, refreshed_trace.capture_state), (trace.status, trace.capture_state))
        self.assertEqual(
            AgentConversation.objects.get(pk=conversation.pk).last_activity_at, conversation.last_activity_at
        )
        other.refresh_from_db()
        self.assertFalse(other.external_outcome_unknown or other.cleanup_incomplete)

        record_run_diagnostics(run.pk, user_id=run.user_id, conversation_id=run.conversation_id)
        current.refresh_from_db()
        self.assertTrue(current.external_outcome_unknown and current.cleanup_incomplete)

    def test_unowned_or_deleted_rows_are_never_written_or_recreated(self):
        guard, run = self.finished_run(AgentRunStatus.FAILED)
        stranger = get_user_model().objects.create_user(username="run-diagnostics-stranger")
        for user_id, conversation_id in ((stranger.pk, run.conversation_id), (run.user_id, uuid.uuid4())):
            record_run_diagnostics(
                run.pk, user_id=user_id, conversation_id=conversation_id, external_outcome_unknown=True
            )
        run.refresh_from_db()
        self.assertFalse(run.external_outcome_unknown)

        run.conversation.delete()
        guard.mark_mcp_uncertain()
        guard.merge_diagnostics()
        self.assertFalse(AgentRun.objects.filter(pk=run.pk).exists())

    def test_storage_failure_logs_without_touching_lifecycle(self):
        guard, run = self.finished_run(AgentRunStatus.CANCELLED)
        guard.mark_mcp_uncertain()
        with (
            patch.object(QuerySet, "update", side_effect=DatabaseError("secret detail")),
            self.assertLogs("nautobot_agent.chat.execution", level="WARNING") as captured,
        ):
            guard.merge_diagnostics()
        self.assertIn("Run diagnostic persistence failed (DatabaseError)", captured.output[0])
        self.assertNotIn("secret", captured.output[0])
        current = AgentRun.objects.get(pk=run.pk)
        self.assertEqual((current.status, current.external_outcome_unknown), (AgentRunStatus.CANCELLED, False))


class GroupedReservationTests(TestCase):
    """Reservation classification for the grouped deletion lock interface."""

    def test_grouped_locks_reject_changed_owner_without_chasing_it(self):
        owner = get_user_model().objects.create_user(username=f"grouped-owner-{uuid.uuid4()}")
        changed_owner = get_user_model().objects.create_user(username=f"grouped-changed-{uuid.uuid4()}")
        conversation = AgentConversation.objects.create(user=owner)
        AgentConversation.objects.filter(pk=conversation.pk).update(user=changed_owner)
        result = lock_conversation_reservations(
            {conversation.pk: owner.pk},
            AgentConversation.objects.all(),
            using=router.db_for_write(AgentConversation),
        )
        self.assertEqual(result, [])


class BulkDeletionTransactionTests(CommittedTestCase):
    """Exercise guarded deletion commits, rollback, and admission races."""

    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username=f"conversation-delete-{uuid.uuid4()}",
            email="",
            password="admin",
        )
        self.using = router.db_for_write(AgentConversation)
        self.connection = connections[self.using]

    def request(self):
        """Build an orchestration request without HTTP session persistence."""
        return SimpleNamespace(user=self.owner, session={})

    def test_admission_and_deletion_serialize_without_orphan_runs(self):
        for entrypoint in ("single", "bulk"):
            for winner in ("admission", "deletion"):
                with self.subTest(entrypoint=entrypoint, winner=winner):
                    self._assert_admission_deletion_race(entrypoint, winner)

    def _assert_admission_deletion_race(self, entrypoint, winner):
        conversation = AgentConversation.objects.create(user=self.owner)
        first_locked = threading.Event()
        second_attempted = threading.Event()
        release_first = threading.Event()

        def contend(role, original):
            """The winner holds its first lock until released; the loser signals before blocking on it."""

            def lock(*args, **kwargs):
                if role != winner:
                    second_attempted.set()
                    return original(*args, **kwargs)
                locked = original(*args, **kwargs)
                first_locked.set()
                if not release_first.wait(10):
                    raise TimeoutError("second contender did not reach its lock boundary")
                return locked

            return lock

        def delete_contender():
            try:
                if entrypoint == "bulk":
                    return delete_many(self.request(), [conversation.pk])
                return delete(self.request(), conversation.pk)
            except ConversationBusy as exc:
                return exc
            finally:
                connections.close_all()

        def submit():
            try:
                return submit_run(
                    self.owner,
                    uuid.uuid4(),
                    "race without providers",
                    conversation.pk,
                    lambda *args, **kwargs: None,
                )
            finally:
                connections.close_all()

        with (
            patch.object(
                runs,
                "lock_conversation_reservations",
                side_effect=contend("deletion", runs.lock_conversation_reservations),
            ),
            patch.object(
                runs,
                "_lock_owner",
                side_effect=contend("admission", runs._lock_owner),  # pylint: disable=protected-access
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first = executor.submit(submit if winner == "admission" else delete_contender)
            try:
                self.assertTrue(first_locked.wait(5))
                second = executor.submit(delete_contender if winner == "admission" else submit)
                self.assertTrue(second_attempted.wait(5))
            finally:
                release_first.set()
            first_result, second_result = first.result(timeout=10), second.result(timeout=10)
        if winner == "admission":
            self._assert_admission_won(conversation, entrypoint, first_result, second_result)
        else:
            self._assert_deletion_won(conversation, entrypoint, second_result, first_result)

    def _assert_admission_won(self, conversation, entrypoint, admission, deletion_result):
        self.assertEqual(admission.kind, AdmissionKind.ADMITTED)
        if entrypoint == "single":
            self.assertIsInstance(deletion_result, ConversationBusy)
            self.assertEqual(str(deletion_result), BUSY_MESSAGE)
        else:
            self.assertEqual(deletion_result, (0, [BUSY_MESSAGE]))
        conversation.refresh_from_db()

        self.assertTrue(AgentRun.objects.filter(pk=admission.run.pk, conversation=conversation).exists())
        # Release this case's capacity only after checking the durable reservation.
        AgentConversation.objects.filter(pk=conversation.pk).delete()

    def _assert_deletion_won(self, conversation, entrypoint, admission, deletion_result):
        self.assertEqual(admission.kind, AdmissionKind.CONVERSATION_NOT_FOUND)
        self.assertEqual(deletion_result, None if entrypoint == "single" else (1, []))
        self.assertFalse(AgentConversation.objects.filter(pk=conversation.pk).exists())
        self.assertFalse(AgentRun.objects.filter(conversation_id=conversation.pk).exists())
