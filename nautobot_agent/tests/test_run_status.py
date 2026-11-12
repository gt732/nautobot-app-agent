"""Owned projections and delivery decisions for durable run status."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.db.models import Value
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from nautobot_agent.chat import conversation as chat_conversation
from nautobot_agent.chat import run_status, runs
from nautobot_agent.models import AgentExecutionTrace, AgentRun, AgentRunStatus, ExecutionStatus, HeartbeatNow
from nautobot_agent.tests.support.chat import claimed_run, create_conversation, create_reserved_run


class RunStatusDatabaseTests(TestCase):
    """Verify owned stale reconciliation against durable rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username=f"run-status-{uuid.uuid4()}")

    def create_run(self, conversation):
        """Persist and claim one queued run."""
        run = create_reserved_run(conversation, prompt="request", client_request_id=uuid.uuid4())
        claimed_run(run.pk)
        run.refresh_from_db()
        return run

    def test_terminal_trace_write_failure_is_repaired_with_original_end_time(self):
        run = self.create_run(create_conversation(self.user))
        trace = AgentExecutionTrace.objects.create(
            conversation_id=run.conversation_id,
            request="missed outcome",
            started_at=timezone.now() - timedelta(minutes=1),
        )
        AgentRun.objects.filter(pk=run.pk).update(trace=trace)
        original_get = QuerySet.get

        def fail_trace_lock(queryset, *args, **kwargs):
            if queryset.model is AgentExecutionTrace:
                raise DatabaseError("trace lock timeout")
            return original_get(queryset, *args, **kwargs)

        ended_at = timezone.now()
        with (
            patch.object(QuerySet, "get", fail_trace_lock),
            patch("nautobot_agent.chat.runs.timezone.now", return_value=ended_at),
            self.assertLogs("nautobot_agent.chat.runs", level="WARNING"),
        ):
            runs.finish_run(run.pk, AgentRunStatus.COMPLETED)
        run.refresh_from_db()
        trace.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)
        self.assertEqual((trace.status, trace.ended_at), (ExecutionStatus.RUNNING, None))

        with patch("nautobot_agent.chat.runs.timezone.now", return_value=ended_at + timedelta(days=2)):
            run_status.reconcile_trace_runs([trace])
        self.assertEqual((trace.status, trace.ended_at), (ExecutionStatus.OK, ended_at))
        trace.refresh_from_db()
        self.assertEqual((trace.status, trace.ended_at), (ExecutionStatus.OK, ended_at))

    def test_owned_reads_reconcile_only_stale_running(self):
        stale = self.create_run(create_conversation(self.user))
        AgentRun.objects.filter(pk=stale.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        reconciled = run_status.reconcile_owned_run(self.user, pk=stale.pk)
        assert reconciled is not None
        self.assertEqual(
            (reconciled.status, reconciled.error_code, reconciled.prompt),
            (AgentRunStatus.INTERRUPTED, "stale_heartbeat", ""),
        )
        latest = run_status.reconcile_latest_run(stale.conversation)
        assert latest is not None
        self.assertEqual(latest.status, AgentRunStatus.INTERRUPTED)

        by_request = self.create_run(create_conversation(self.user))
        AgentRun.objects.filter(pk=by_request.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        reconciled = run_status.reconcile_owned_run(self.user, client_request_id=by_request.client_request_id)
        assert reconciled is not None
        self.assertEqual(reconciled.status, AgentRunStatus.INTERRUPTED)

        fresh = self.create_run(create_conversation(self.user))
        refreshed = run_status.reconcile_owned_run(self.user, pk=fresh.pk)
        assert refreshed is not None
        self.assertEqual(refreshed.status, AgentRunStatus.RUNNING)

        for status in (AgentRunStatus.QUEUED, AgentRunStatus.COMPLETED, AgentRunStatus.INTERRUPTED):
            with self.subTest(status=status):
                conversation = create_conversation(self.user)
                run = create_reserved_run(conversation, prompt="request", client_request_id=uuid.uuid4())
                AgentRun.objects.filter(pk=run.pk).update(status=status)
                conversation.refresh_from_db()
                for read in (
                    run_status.reconcile_owned_run(self.user, pk=run.pk),
                    run_status.reconcile_owned_run(self.user, client_request_id=run.client_request_id),
                ):
                    assert read is not None
                    self.assertEqual(read.status, status)
                latest = run_status.reconcile_latest_run(conversation)
                assert latest is not None
                self.assertEqual(latest.pk, run.pk)
                self.assertEqual(latest.status, status)

        deleted = create_reserved_run(create_conversation(self.user), prompt="request")
        AgentRun.objects.filter(pk=deleted.pk).update(queue_expires_at=HeartbeatNow() - Value(timedelta(seconds=1)))
        snapshot = AgentRun.objects.with_queue_expired().get(pk=deleted.pk)
        AgentRun.objects.filter(pk=deleted.pk).delete()
        self.assertIsNone(run_status._reconcile(snapshot))  # pylint: disable=protected-access

    def test_stale_snapshot_rechecks_renewed_heartbeat_and_completed_state(self):
        # Pass annotated snapshots directly to exercise the race after the ownership-filtered read.
        # pylint: disable=protected-access
        renewed = self.create_run(create_conversation(self.user))
        AgentRun.objects.filter(pk=renewed.pk).update(
            heartbeat_at=HeartbeatNow() - Value(AgentRun.STALE_HEARTBEAT_AFTER + timedelta(seconds=1))
        )
        stale_snapshot = AgentRun.objects.with_heartbeat_stale().get(pk=renewed.pk)
        stale_heartbeat = stale_snapshot.heartbeat_at
        AgentRun.objects.filter(pk=renewed.pk).update(heartbeat_at=HeartbeatNow())

        current = run_status._reconcile(stale_snapshot)
        assert current is not None
        self.assertEqual(current.status, AgentRunStatus.RUNNING)
        self.assertGreater(current.heartbeat_at, stale_heartbeat)

        completed = self.create_run(create_conversation(self.user))
        AgentRun.objects.filter(pk=completed.pk).update(
            heartbeat_at=HeartbeatNow() - Value(AgentRun.STALE_HEARTBEAT_AFTER + timedelta(seconds=1))
        )
        stale_snapshot = AgentRun.objects.with_heartbeat_stale().get(pk=completed.pk)
        AgentRun.objects.filter(pk=completed.pk).update(status=AgentRunStatus.COMPLETED)

        current = run_status._reconcile(stale_snapshot)
        assert current is not None
        self.assertEqual(current.status, AgentRunStatus.COMPLETED)

        conversation = create_conversation(self.user)
        run = self.create_run(conversation)

        def complete_after_read(snapshot):
            AgentRun.objects.filter(pk=run.pk).update(status=AgentRunStatus.COMPLETED)
            return snapshot

        with (
            patch.object(run_status, "_reconcile", side_effect=complete_after_read),
            patch.object(chat_conversation.chat_history, "read_chat_state", return_value=([], [], {})),
        ):
            state, notice = chat_conversation.restore_state(conversation)
        self.assertIsNone(notice)
        self.assertEqual(state["latest_run"]["status"], AgentRunStatus.RUNNING)
        self.assertEqual(state["active_run"], state["latest_run"])
        with patch.object(chat_conversation.chat_history, "read_chat_state", return_value=([], [], {})):
            state, notice = chat_conversation.restore_state(conversation)
        self.assertIsNone(notice)
        self.assertEqual(state["latest_run"]["status"], AgentRunStatus.COMPLETED)
        self.assertTrue(state["latest_run"]["terminal"])
        self.assertIsNone(state["active_run"])

    def test_reads_are_owner_and_conversation_scoped(self):
        self.assertIsNone(run_status.reconcile_owned_run(self.user, pk=uuid.uuid4()))
        self.assertIsNone(run_status.reconcile_owned_run(self.user, client_request_id=uuid.uuid4()))
        self.assertIsNone(run_status.reconcile_latest_run(create_conversation(self.user)))

        owner = get_user_model().objects.create_user(username=f"run-status-other-{uuid.uuid4()}")
        foreign_run = self.create_run(create_conversation(owner))
        AgentRun.objects.filter(pk=foreign_run.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        self.assertIsNone(run_status.reconcile_owned_run(self.user, pk=foreign_run.pk))
        self.assertIsNone(run_status.reconcile_owned_run(self.user, client_request_id=foreign_run.client_request_id))

        owned_conversation = create_conversation(self.user)
        owned_run = self.create_run(owned_conversation)
        AgentRun.objects.filter(pk=owned_run.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        active = run_status.reconcile_latest_run(owned_conversation)
        assert active is not None
        self.assertEqual(active.status, AgentRunStatus.INTERRUPTED)

        conversation = create_conversation(self.user)
        mismatched_run = self.create_run(create_conversation(self.user))
        AgentRun.objects.filter(pk=mismatched_run.pk).update(heartbeat_at=HeartbeatNow() - Value(timedelta(seconds=31)))
        for unmatched in (mismatched_run, foreign_run):
            with self.subTest(unmatched=unmatched.pk):
                self.assertIsNone(run_status.reconcile_latest_run(conversation))
                unmatched.refresh_from_db()
                self.assertEqual(unmatched.status, AgentRunStatus.RUNNING)
