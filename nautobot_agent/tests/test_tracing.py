"""Behavioral tests for persisted execution traces."""

# Keep recorder persistence and frozen-capture regressions together.
# pylint: disable=too-many-lines

import json
import math
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, router, transaction
from django.db.models.query import QuerySet
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from nautobot.apps.testing import TestCase

from nautobot_agent.chat import conversation as chat_conversation
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRunStatus,
    ExecutionStatus,
    LLMConfiguration,
    TraceCaptureState,
)
from nautobot_agent.runtime import tracing
from nautobot_agent.runtime.budgets import ModelCallBudgetCallback, RunBudget, RunBudgetExceeded
from nautobot_agent.runtime.checkpointer import RUN_GUARD_CONFIG_KEY
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import RunDraining
from nautobot_agent.runtime.trace_inspector import inspect_trace
from nautobot_agent.runtime.tracing import _Recorder, start_trace
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import (
    ScriptedToolModel,
    create_configuration,
    create_conversation,
    parse_run_events,
    post_run_message,
    run_dependencies,
    tool_call_message,
    use_runtime,
)
from nautobot_agent.tests.support.data import grant
from nautobot_agent.tests.support.db import CommittedTestCase
from nautobot_agent.tests.support.graph import SourceGraphTestCase
from nautobot_agent.tests.support.mcp import Host


class TraceBatchAndFailureTests(CommittedTestCase):
    """Test batched trace persistence and failure handling."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="trace-recorder")
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))

    def test_reordered_model_callbacks_preserve_complete_capture_of_admitted_calls(self):
        handle = start_trace(self.user, create_conversation(self.user), "reordered admission")
        callbacks = handle.config()["callbacks"]
        assert isinstance(callbacks, list)
        budget = RunBudget(tool_limit=1, model_limit=1)
        admission = ModelCallBudgetCallback(budget, on_rejected=handle.discard_model_start)
        model = ScriptedToolModel(responses=[AIMessage(content="Admitted answer.")])
        # Native callback-manager merges may place the recorder before admission.
        config: RunnableConfig = {"callbacks": [*callbacks, admission]}
        self.assertEqual(model.invoke("First request", config).text, "Admitted answer.")
        with self.assertRaises(RunBudgetExceeded):
            model.invoke("Denied request", config)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(handle.finish_capture(AgentRunStatus.FAILED), TraceCaptureState.COMPLETE)
        spans = AgentExecutionSpan.objects.filter(trace_id=handle.trace_id, kind="chat_model")
        self.assertEqual(list(spans.values_list("status", flat=True)), [ExecutionStatus.OK])

    def _ended_trace(self, request, capture_state=TraceCaptureState.PENDING):
        """Start a trace whose execution outcome was committed with the given capture health."""
        handle = start_trace(self.user, create_conversation(self.user), request)
        ended_at = timezone.now()
        AgentExecutionTrace.objects.filter(pk=handle.trace_id).update(
            status=ExecutionStatus.OK, ended_at=ended_at, capture_state=capture_state
        )
        return handle, ended_at

    @staticmethod
    def _recorder(handle):
        callbacks = handle.config()["callbacks"]
        assert isinstance(callbacks, list)
        return callbacks[0]

    def test_capture_health_updates_only_ended_pending_original_trace(self):
        handle, ended_at = self._ended_trace("committed")
        callback = self._recorder(handle)
        unfinished = uuid.uuid4()
        callback.on_chain_start({}, {}, run_id=unfinished)
        handle.freeze()
        callback.on_chain_end({}, run_id=unfinished)
        other, _other_ended = self._ended_trace("untouched")

        self.assertEqual(handle.finish_capture(AgentRunStatus.CANCELLED), TraceCaptureState.COMPLETE)

        trace = AgentExecutionTrace.objects.get(pk=handle.trace_id)
        self.assertEqual(
            (trace.status, trace.ended_at, trace.capture_state),
            (ExecutionStatus.OK, ended_at, TraceCaptureState.COMPLETE),
        )
        self.assertEqual(AgentExecutionSpan.objects.get(run_id=unfinished).status, ExecutionStatus.CANCELLED)
        self.assertEqual(AgentExecutionTrace.objects.get(pk=other.trace_id).capture_state, TraceCaptureState.PENDING)

    def test_capture_health_without_committed_outcome_leaves_trace_pending(self):
        handle = start_trace(self.user, create_conversation(self.user), "unconfirmed")
        unfinished = uuid.uuid4()
        self._recorder(handle).on_chain_start({}, {}, run_id=unfinished)

        self.assertEqual(handle.finish_capture(None), TraceCaptureState.PARTIAL)

        trace = AgentExecutionTrace.objects.get(pk=handle.trace_id)
        self.assertEqual((trace.status, trace.capture_state), (ExecutionStatus.RUNNING, TraceCaptureState.PENDING))
        self.assertIsNone(trace.ended_at)
        self.assertEqual(AgentExecutionSpan.objects.get(run_id=unfinished).status, ExecutionStatus.INCOMPLETE)

    def test_capture_health_preserves_observer_state_and_deleted_rows(self):
        observed, ended_at = self._ended_trace("observer", TraceCaptureState.PARTIAL)
        self.assertEqual(observed.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.COMPLETE)
        trace = AgentExecutionTrace.objects.get(pk=observed.trace_id)
        self.assertEqual(
            (trace.status, trace.ended_at, trace.capture_state),
            (ExecutionStatus.OK, ended_at, TraceCaptureState.PARTIAL),
        )

        deleted, _ended_at = self._ended_trace("deleted")
        self._recorder(deleted).on_chain_start({}, {}, run_id=uuid.uuid4())
        AgentExecutionTrace.objects.filter(pk=deleted.trace_id).delete()
        with self.assertLogs("nautobot_agent.runtime.tracing", level="WARNING"):
            self.assertEqual(deleted.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.FAILED)
        self.assertFalse(AgentExecutionTrace.objects.filter(pk=deleted.trace_id).exists())
        self.assertFalse(AgentExecutionSpan.objects.filter(trace_id=deleted.trace_id).exists())

    def test_capture_health_update_failure_is_logged_and_nonfatal(self):
        handle, ended_at = self._ended_trace("health failure")
        with (
            patch.object(QuerySet, "update", side_effect=RuntimeError("health storage")),
            self.assertLogs("nautobot_agent.runtime.tracing", level="WARNING") as logs,
        ):
            self.assertEqual(handle.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.COMPLETE)
        self.assertIn("capture health failed", "".join(logs.output))
        trace = AgentExecutionTrace.objects.get(pk=handle.trace_id)
        self.assertEqual((trace.ended_at, trace.capture_state), (ended_at, TraceCaptureState.PENDING))

    def test_soft_timeout_stops_batches_and_row_fallback(self):
        scenarios = (
            ({1: SoftTimeLimitExceeded}, 1, 0, TraceCaptureState.FAILED),
            ({2: SoftTimeLimitExceeded}, 2, 50, TraceCaptureState.PARTIAL),
            ({1: RuntimeError, 2: SoftTimeLimitExceeded}, 2, 0, TraceCaptureState.FAILED),
            ({2: RuntimeError, 4: SoftTimeLimitExceeded}, 4, 51, TraceCaptureState.PARTIAL),
        )
        original_insert = QuerySet.bulk_create
        for errors, expected_attempts, committed_rows, expected_state in scenarios:
            with self.subTest(errors=errors):
                handle, ended_at = self._ended_trace("soft timeout")
                callback = self._recorder(handle)
                for _ in range(101):
                    run_id = uuid.uuid4()
                    callback.on_chain_start({}, {}, run_id=run_id)
                    callback.on_chain_end({}, run_id=run_id)
                attempts = 0
                error_schedule = tuple(errors.items())

                def insert(queryset, rows, *, errors=error_schedule, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    for attempt, error in errors:
                        if attempts == attempt:
                            raise error()
                    return original_insert(queryset, rows, **kwargs)

                with patch.object(QuerySet, "bulk_create", insert):
                    self.assertEqual(handle.finish_capture(AgentRunStatus.COMPLETED), expected_state)
                self.assertEqual(attempts, expected_attempts)
                self.assertEqual(AgentExecutionSpan.objects.filter(trace_id=handle.trace_id).count(), committed_rows)
                trace = AgentExecutionTrace.objects.get(pk=handle.trace_id)
                self.assertEqual(
                    (trace.status, trace.ended_at, trace.capture_state),
                    (ExecutionStatus.OK, ended_at, expected_state),
                )


class TraceTransactionTests(CommittedTestCase):
    """Test trace persistence behavior across database transactions."""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="trace-transaction")
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))

    def test_conversation_deleted_between_batches_stops_capture(self):
        conversation = create_conversation(self.user)
        handle = start_trace(self.user, conversation, "delete between batches")
        callback = cast(_Recorder, cast(list, handle.config()["callbacks"])[0])
        run_ids = [uuid.uuid4() for _ in range(101)]
        for run_id in run_ids:
            callback.on_chain_start({}, {}, run_id=run_id)
            callback.on_chain_end({}, run_id=run_id)
        grant(self.user, AgentConversation, actions=("delete",))
        original = tracing._prepared_values  # pylint: disable=protected-access

        def prepare(trace_id, span):
            if span.run_id == run_ids[50]:
                self.assertEqual(AgentExecutionSpan.objects.filter(trace_id=trace_id).count(), 50)
                deleted, skipped = chat_conversation.delete_many(
                    SimpleNamespace(user=self.user, session={}), [conversation.pk]
                )
                self.assertEqual((deleted, skipped), (1, []))
            return original(trace_id, span)

        with (
            patch.object(tracing, "_prepared_values", side_effect=prepare),
            self.assertNoLogs("nautobot_agent.runtime.tracing", level="WARNING"),
        ):
            self.assertEqual(handle.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.PARTIAL)
        self.assertFalse(AgentExecutionTrace.objects.filter(pk=handle.trace_id).exists())
        self.assertFalse(AgentExecutionSpan.objects.filter(trace_id=handle.trace_id).exists())

    def test_capture_waits_for_conversation_deletion_before_inserting(self):  # pylint: disable=too-many-locals
        using = router.db_for_write(AgentExecutionSpan)
        if connections[using].vendor != "postgresql":
            self.skipTest("PostgreSQL lock observation is required for this regression.")
        conversation = create_conversation(self.user)
        handle = start_trace(self.user, conversation, "concurrent deletion")
        callback = cast(_Recorder, cast(list, handle.config()["callbacks"])[0])
        run_id = uuid.uuid4()
        callback.on_chain_start({}, {}, run_id=run_id)
        callback.on_chain_end({}, run_id=run_id)
        ready = threading.Event()
        result = {}

        def flush_in_worker():
            connections.close_all()
            try:
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    result["pid"] = cursor.fetchone()[0]
                ready.set()
                result["state"] = handle.finish_capture(AgentRunStatus.COMPLETED)
            except Exception as exc:  # noqa: BLE001  # forward worker-thread failures to the test thread
                result["error"] = exc
            finally:
                connections.close_all()

        worker = threading.Thread(target=flush_in_worker)
        try:
            with transaction.atomic(using=using):
                AgentConversation.objects.using(using).select_for_update().get(pk=conversation.pk)
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    main_pid = cursor.fetchone()[0]
                worker.start()
                self.assertTrue(ready.wait(timeout=5))
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with connections[using].cursor() as cursor:
                        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [main_pid, result["pid"]])
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.01)
                self.assertTrue(blocked, "Capture must wait before inserting spans into a deleting conversation.")
                conversation.delete()
        finally:
            if worker.ident is not None:
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["state"], TraceCaptureState.FAILED)
        self.assertFalse(AgentExecutionSpan.objects.filter(trace_id=handle.trace_id).exists())

    def test_trace_start_waits_for_owner_before_locking_conversation(self):
        """Trace startup cannot take the conversation FK lock before waiting on its owner."""
        using = router.db_for_write(AgentExecutionTrace)
        if connections[using].vendor != "postgresql":
            self.skipTest("PostgreSQL lock observation is required for this regression.")

        worker_ready = threading.Event()
        worker_data = {}
        conversation = create_conversation(self.user)

        def start_trace_in_worker():
            connections.close_all()
            try:
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_data["pid"] = cursor.fetchone()[0]
                worker_ready.set()
                worker_data["handle"] = start_trace(self.user, conversation, "concurrent trace")
            except Exception as exc:  # noqa: BLE001  # forward worker-thread failures to the test thread
                worker_data["error"] = exc
            finally:
                connections.close_all()

        worker = threading.Thread(target=start_trace_in_worker)
        try:
            with transaction.atomic(using=using):
                get_user_model().objects.using(using).select_for_update().get(pk=self.user.pk)
                with connections[using].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    main_pid = cursor.fetchone()[0]

                worker.start()
                self.assertTrue(worker_ready.wait(timeout=5), "Trace worker did not establish its database connection.")

                deadline = time.monotonic() + 5
                blocked_by_main = False
                while time.monotonic() < deadline:
                    with connections[using].cursor() as cursor:
                        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [main_pid, worker_data["pid"]])
                        blocked_by_main = cursor.fetchone()[0]
                    if blocked_by_main:
                        break
                    time.sleep(0.01)
                self.assertTrue(
                    blocked_by_main,
                    "Trace worker did not block on the owner row held by the main transaction.",
                )

                AgentConversation.objects.using(using).select_for_update(nowait=True).get(pk=conversation.pk)
        finally:
            if worker.ident is not None:
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "Trace worker did not finish after the owner lock was released.")
        self.assertNotIn("error", worker_data)
        trace_handle = worker_data["handle"]
        self.assertIsNotNone(trace_handle.trace_id)
        persisted = AgentExecutionTrace.objects.get(pk=trace_handle.trace_id)
        self.assertEqual(persisted.conversation.user_id, self.user.pk)
        self.assertEqual(persisted.conversation_id, conversation.pk)
        self.assertEqual(persisted.request, "concurrent trace")

    def test_real_middle_batch_collision_preserves_other_rows(self):
        trace_handle = start_trace(self.user, create_conversation(self.user), "batch collision")
        callbacks = trace_handle.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        run_ids = [uuid.uuid4() for _ in range(101)]
        for run_id in run_ids:
            callback.on_chain_start({}, {}, run_id=run_id)
            callback.on_chain_end({}, run_id=run_id)

        collision_id = run_ids[50]
        AgentExecutionSpan.objects.create(
            trace_id=trace_handle.trace_id,
            run_id=collision_id,
            parent_run_id=None,
            kind="existing",
            name="committed collision",
            status=ExecutionStatus.OK,
            started_at=timezone.now(),
            ended_at=timezone.now(),
            details={},
            event_counts={},
        )
        state = trace_handle.finish_capture(AgentRunStatus.COMPLETED)
        survivors = set(
            AgentExecutionSpan.objects.filter(trace_id=trace_handle.trace_id).values_list("run_id", flat=True)
        )
        self.assertEqual(survivors, set(run_ids))
        self.assertEqual(state, TraceCaptureState.PARTIAL)
        self.assertEqual(
            AgentExecutionSpan.objects.get(trace_id=trace_handle.trace_id, run_id=collision_id).name,
            "committed collision",
        )


class TraceSnapshotTests(TestCase):
    """Test trace snapshot persistence and payload boundaries."""

    def test_large_payload_is_persisted_without_recorder_size_limit(self):
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        payload = "large " + "é" * 1_500_000
        handle = start_trace(self.user, create_conversation(self.user), "large payload")
        callbacks = handle.config()["callbacks"]
        assert isinstance(callbacks, list)
        run_id = uuid.uuid4()
        callbacks[0].on_tool_start({"name": "large"}, "input", run_id=run_id)
        callbacks[0].on_tool_end(payload, run_id=run_id)

        self.assertEqual(handle.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.COMPLETE)
        span = AgentExecutionSpan.objects.get(trace_id=handle.trace_id, run_id=run_id)
        self.assertEqual(span.output_data["arguments"]["output"], payload)

    def test_jsonb_safe_values_survive_persistence(self):
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        cyclic = []
        cyclic.append(cyclic)
        payload = {
            "nul\x00key": "before\x00after",
            "surrogate_value": "before\ud83d\x00after",
            "surrogate_key": {"\ude00": "kept"},
            "numbers": [math.nan, math.inf, -math.inf],
            "bytes": b"\x00\xff",
            "cycle": cyclic,
            RUN_GUARD_CONFIG_KEY: object(),
            "nested": {RUN_GUARD_CONFIG_KEY: object(), "kept": "value"},
            "nested_nonstring": {RUN_GUARD_CONFIG_KEY: object(), 9: "kept"},
            "execution_context": AgentContext(
                "trusted-principal",
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
                mcp_turn=MCPTurn(host=Host()),
            ),
            7: "non-string key",
        }
        trace = start_trace(self.user, create_conversation(self.user), "snapshot request")
        callbacks = trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        recorder = callbacks[0]
        run_id = uuid.uuid4()
        recorder.on_tool_start({"name": "snapshot"}, "snapshot input", inputs={"payload": payload}, run_id=run_id)
        recorder.on_tool_end(payload, run_id=run_id)
        payload["bytes"] = b"changed after capture"
        self.assertEqual(trace.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.COMPLETE)

        span = AgentExecutionSpan.objects.get(run_id=run_id)
        observed = span.input_data["kwargs"]["inputs"]["payload"]
        self.assertEqual(observed["type"], "dict")
        self.assertIn(
            [{"type": "str_with_nul", "parts": ["nul", "key"]}, {"type": "str_with_nul", "parts": ["before", "after"]}],
            observed["items"],
        )
        self.assertIn(["bytes", {"type": "bytes", "hex": "00ff"}], observed["items"])
        self.assertIn([7, "non-string key"], observed["items"])
        string_items = {key: value for key, value in observed["items"] if isinstance(key, str)}
        self.assertEqual(
            bytes.fromhex(string_items["surrogate_value"]["utf8_hex"]).decode("utf-8", "surrogatepass"),
            "before\ud83d\x00after",
        )
        surrogate_key, surrogate_value = string_items["surrogate_key"]["items"][0]
        self.assertEqual(bytes.fromhex(surrogate_key["utf8_hex"]).decode("utf-8", "surrogatepass"), "\ude00")
        self.assertEqual(surrogate_value, "kept")
        self.assertNotIn(RUN_GUARD_CONFIG_KEY, string_items)
        self.assertEqual(string_items["nested"], {"kept": "value"})
        self.assertEqual(string_items["nested_nonstring"], {"type": "dict", "items": [[9, "kept"]]})
        self.assertEqual(
            string_items["execution_context"]["fields"],
            {"user_id": "trusted-principal"},
        )
        cycle_value = string_items["cycle"][0]
        self.assertTrue(cycle_value.startswith("<circular reference: list id="))
        values = string_items["numbers"]
        self.assertEqual(
            values,
            [{"type": "float", "value": "nan"}, {"type": "float", "value": "inf"}, {"type": "float", "value": "-inf"}],
        )


class TraceHandleTests(TestCase):
    """Test trace handle data capture and privacy behavior."""

    def test_credential_profile_configuration_is_not_persisted_in_spans(self):
        secret = "TRACE-CREDENTIAL-SENTINEL"
        self.enterContext(
            self.settings(
                PLUGINS_CONFIG={
                    "nautobot_agent": {
                        "tracing": True,
                        "credentials": {
                            "gateway": {"api_key": "unused", "default_headers": {"Authorization": f"Bearer {secret}"}}
                        },
                    }
                }
            )
        )
        configuration = LLMConfiguration(model="openai:probe", credential_profile="gateway")
        _, model_name, arguments = configuration.resolve_model_arguments()
        model = ChatOpenAI(model=model_name, **arguments)
        trace = start_trace(self.user, create_conversation(self.user), "request")
        callbacks = trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        run_id, tool_id = uuid.uuid4(), uuid.uuid4()
        callback.on_chat_model_start(
            dict(model.to_json()),
            [[HumanMessage("inspect inventory")]],
            run_id=run_id,
            invocation_params={"client_kwargs": {"token": secret}},
        )
        callback.on_llm_end(LLMResult(generations=[]), run_id=run_id)
        callback.on_tool_start({"name": "inventory", "client_config": {"token": secret}}, "query", run_id=tool_id)
        callback.on_tool_end("inventory result", run_id=tool_id)
        trace.finish_capture(AgentRunStatus.COMPLETED)
        for span in AgentExecutionSpan.objects.filter(run_id__in=(run_id, tool_id)):
            self.assertNotIn(secret, json.dumps(span.input_data))
            self.assertNotIn(secret, json.dumps(span.details))
        model_span = AgentExecutionSpan.objects.get(run_id=run_id)
        self.assertIn("inspect inventory", json.dumps(model_span.input_data))
        tool_span = AgentExecutionSpan.objects.get(run_id=tool_id)
        self.assertEqual(tool_span.name, "inventory")
        self.assertEqual(tool_span.input_data["arguments"]["input_str"], "query")

    def test_frozen_capture_drops_late_callbacks_and_preserves_persisted_spans(self):  # pylint: disable=too-many-locals
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        conversation = create_conversation(self.user)
        trace = start_trace(self.user, conversation, "request")
        run_id = uuid.uuid4()
        late_run_id = uuid.uuid4()
        callbacks = trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        callback.on_tool_start({"name": "tool"}, "args", run_id=run_id)
        callback.on_tool_end("result", run_id=run_id)
        state = trace.finish_capture(AgentRunStatus.COMPLETED)
        self.assertEqual(state, TraceCaptureState.COMPLETE)
        callback.on_chain_end({"late": "output"}, run_id=run_id)
        callback.on_chain_error(RuntimeError("late error"), run_id=run_id)
        callback.on_llm_new_token("late token", run_id=run_id)
        callback.on_tool_start({"name": "late"}, "ignored", run_id=late_run_id)
        callback.on_chain_end({"late": "output"}, run_id=late_run_id)
        callback.on_chain_error(RuntimeError("late error"), run_id=late_run_id)
        callback.on_llm_new_token("late token", run_id=late_run_id)

        row = AgentExecutionTrace.objects.get(conversation=conversation)
        self.assertEqual(row.conversation_id, conversation.pk)
        self.assertEqual(row.spans.count(), 1)
        span = row.spans.get()
        self.assertEqual(span.run_id, run_id)
        self.assertEqual(span.status, ExecutionStatus.OK)
        self.assertEqual(span.output_data["arguments"]["output"], "result")
        self.assertIsNone(span.error_data)
        self.assertEqual(span.event_counts, {})

        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        incomplete_trace = start_trace(self.user, create_conversation(self.user), "unfinished work")
        callbacks = incomplete_trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        unfinished_chain, finished_tool = uuid.uuid4(), uuid.uuid4()
        callback.on_chain_start({}, {}, run_id=unfinished_chain)
        callback.on_tool_start({"name": "read"}, "input", run_id=finished_tool)
        callback.on_tool_end("result", run_id=finished_tool)
        with self.assertLogs("nautobot_agent.runtime.tracing", level="WARNING") as logs:
            state = incomplete_trace.finish_capture(AgentRunStatus.FAILED)
        self.assertIn(f"trace={incomplete_trace.trace_id} count=1", "".join(logs.output))
        self.assertEqual(state, TraceCaptureState.PARTIAL)
        self.assertEqual(
            AgentExecutionSpan.objects.get(run_id=unfinished_chain).status,
            ExecutionStatus.INCOMPLETE,
        )
        self.assertEqual(
            AgentExecutionSpan.objects.get(run_id=finished_tool).status,
            ExecutionStatus.OK,
        )

        cancelled_trace = start_trace(self.user, create_conversation(self.user), "definitive cancellation")
        callbacks = cancelled_trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        cancelled_id = uuid.uuid4()
        callbacks[0].on_chain_start({}, {}, run_id=cancelled_id)
        state = cancelled_trace.finish_capture(AgentRunStatus.CANCELLED)
        self.assertEqual(state, TraceCaptureState.COMPLETE)
        self.assertEqual(AgentExecutionSpan.objects.get(run_id=cancelled_id).status, ExecutionStatus.CANCELLED)

    def test_callback_capture_failure_is_reported_as_partial_without_leaking_data(self):
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        trace = start_trace(self.user, create_conversation(self.user), "callback failure")
        callbacks = trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        failed_id, surviving_id = uuid.uuid4(), uuid.uuid4()

        class BrokenRepresentation:  # pylint: disable=too-few-public-methods
            """Fail during capture of otherwise opaque callback data."""

            def __repr__(self):
                raise RuntimeError("private callback data")

        with self.assertLogs("nautobot_agent.runtime.tracing", level="WARNING") as logs:
            callback.on_tool_start({"name": "failed"}, "input", run_id=failed_id, opaque=BrokenRepresentation())
        callback.on_tool_start({"name": "survivor"}, "input", run_id=surviving_id)
        callback.on_tool_end("result", run_id=surviving_id)
        state = trace.finish_capture(AgentRunStatus.COMPLETED)
        self.assertNotIn("private callback data", "".join(logs.output))
        self.assertIn("RuntimeError", "".join(logs.output))
        self.assertEqual(state, TraceCaptureState.PARTIAL)
        self.assertTrue(AgentExecutionSpan.objects.filter(trace_id=trace.trace_id, run_id=surviving_id).exists())
        self.assertFalse(AgentExecutionSpan.objects.filter(trace_id=trace.trace_id, run_id=failed_id).exists())

    def test_error_events_empty_history_and_summary_failure_are_fail_open(self):  # pylint: disable=too-many-locals
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        trace = start_trace(self.user, create_conversation(self.user), "error events")
        callbacks = trace.config()["callbacks"]
        assert isinstance(callbacks, list)
        callback = callbacks[0]
        tool_id, success_id, exception_id, cancelled_id, llm_id, empty_run_id = (uuid.uuid4() for _ in range(6))
        callback.on_tool_start({}, "input", run_id=tool_id)
        error_output = ToolMessage(content="complete tool error output", tool_call_id="tool-call", status="error")
        callback.on_tool_end(error_output, run_id=tool_id)
        callback.on_tool_start({}, "input", run_id=success_id)
        callback.on_tool_end(ToolMessage(content="successful output", tool_call_id="success-call"), run_id=success_id)
        callback.on_chain_start({}, {}, run_id=exception_id)
        callback.on_chain_error(RuntimeError("raised callback failure"), run_id=exception_id)
        callback.on_chain_start({}, {}, run_id=cancelled_id)
        callback.on_chain_error(KeyboardInterrupt(), run_id=cancelled_id)
        callback.on_chat_model_start({}, [[HumanMessage("prompt")]], run_id=llm_id)
        callback.on_llm_new_token("token", run_id=llm_id)
        callback.on_stream_event(cast(Any, {"event": "message"}), run_id=llm_id)
        callback.on_retry(cast(Any, "retry state"), run_id=llm_id)
        result = LLMResult(generations=[[Generation(text="complete model output")]], llm_output={})
        with patch.object(tracing, "model_call_summaries", side_effect=RuntimeError("summary")):
            callback.on_llm_end(result, run_id=llm_id)
        callback.on_chain_start({}, {}, run_id=empty_run_id)
        callback.on_chain_end({}, run_id=empty_run_id)
        state = trace.finish_capture(AgentRunStatus.COMPLETED)
        self.assertEqual(state, TraceCaptureState.COMPLETE)

        tool = AgentExecutionSpan.objects.get(run_id=tool_id)
        self.assertEqual(tool.status, ExecutionStatus.ERROR)
        self.assertIn("complete tool error output", json.dumps(tool.output_data))
        self.assertIsNone(tool.error_data)
        tool_event = tool.details["events"][0]
        self.assertEqual(tool_event["kind"], "error")
        self.assertNotIn("payload", tool_event)
        successful = AgentExecutionSpan.objects.get(run_id=success_id)
        self.assertEqual(successful.status, ExecutionStatus.OK)
        self.assertIn("successful output", json.dumps(successful.output_data))
        self.assertEqual(successful.details["events"], [])
        exception = AgentExecutionSpan.objects.get(run_id=exception_id)
        self.assertEqual(exception.status, ExecutionStatus.ERROR)
        self.assertIn("raised callback failure", json.dumps(exception.error_data))
        self.assertIsNone(exception.output_data)
        exception_event = exception.details["events"][0]
        self.assertEqual(exception_event["kind"], "error")
        self.assertNotIn("payload", exception_event)
        cancelled = AgentExecutionSpan.objects.get(run_id=cancelled_id)
        self.assertEqual(cancelled.status, ExecutionStatus.CANCELLED)
        cancelled_event = cancelled.details["events"][0]
        self.assertEqual(cancelled_event["kind"], "error")
        self.assertNotIn("payload", cancelled_event)
        model = AgentExecutionSpan.objects.get(run_id=llm_id)
        self.assertIn("complete model output", json.dumps(model.output_data))
        self.assertNotIn("usage_summary", model.details)
        self.assertNotIn("usage", model.details)
        self.assertEqual(model.event_counts, {"token": 1, "stream": 1})
        self.assertEqual(model.details["events"][0]["kind"], "on_retry")
        overview = inspect_trace(AgentExecutionTrace.objects.get(pk=trace.trace_id), None)["trace_overview"]
        self.assertEqual(overview["model_call_count"], 1)
        self.assertEqual(
            AgentExecutionSpan.objects.get(run_id=empty_run_id).details["events"],
            [],
        )

    def test_run_draining_spans_remain_cancelled_for_cancelled_and_failed_outcomes(self):
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))

        for run_status in (AgentRunStatus.CANCELLED, AgentRunStatus.FAILED):
            trace = start_trace(self.user, create_conversation(self.user), "drained run")
            callbacks = trace.config()["callbacks"]
            assert isinstance(callbacks, list)
            callback = callbacks[0]
            parent_id, child_id = uuid.uuid4(), uuid.uuid4()
            callback.on_chain_start({}, {}, run_id=parent_id)
            callback.on_chain_start({}, {}, run_id=child_id, parent_run_id=parent_id)
            error = RunDraining("Execution authority is unavailable.")
            callback.on_chain_error(error, run_id=child_id)
            callback.on_chain_error(error, run_id=parent_id)
            ordinary_id = uuid.uuid4()
            callback.on_chain_start({}, {}, run_id=ordinary_id)
            callback.on_chain_error(RuntimeError("ordinary failure"), run_id=ordinary_id)

            trace.finish_capture(run_status)

            for run_id in (parent_id, child_id):
                span = AgentExecutionSpan.objects.get(trace_id=trace.trace_id, run_id=run_id)
                self.assertEqual(span.status, ExecutionStatus.CANCELLED)
                self.assertEqual(span.details["error_type"], "RunDraining")
                self.assertEqual(
                    span.error_data["arguments"]["error"]["message"], "Execution authority is unavailable."
                )
                event = span.details["events"][0]
                self.assertEqual(event["kind"], "error")
                self.assertNotIn("payload", event)
            ordinary = AgentExecutionSpan.objects.get(trace_id=trace.trace_id, run_id=ordinary_id)
            self.assertEqual(ordinary.status, ExecutionStatus.ERROR)
            self.assertEqual(ordinary.details["error_type"], "RuntimeError")
            self.assertIn("ordinary failure", json.dumps(ordinary.error_data))

    def test_aggregate_cost_precedence_duplicate_zero_and_message_fallback(self):
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        cases = (
            (
                "duplicate",
                {
                    "total_cost": 4.25,
                    "currency": "USD",
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
                {"token_usage": {"total_cost": 4.25, "currency": "USD"}},
                [{"amount": 4.25, "currency": "USD", "source": "llm_output.token_usage.total_cost"}],
            ),
            (
                "multiple",
                {"cost": 2.5, "total_cost": 4.25, "currency": "USD"},
                {"cost": 4.25, "currency": "USD"},
                [
                    {"amount": 2.5, "currency": "USD", "source": "llm_output.token_usage.cost"},
                    {"amount": 4.25, "currency": "USD", "source": "llm_output.token_usage.total_cost"},
                ],
            ),
            (
                "zero",
                {"total_cost": 0, "currency": "GBP"},
                {"total_cost": 9.5, "currency": "EUR"},
                [{"amount": 0, "currency": "GBP", "source": "llm_output.token_usage.total_cost"}],
            ),
            (
                "absent",
                {},
                {"total_cost": 3.5, "currency": "EUR"},
                [
                    {
                        "amount": 3.5,
                        "currency": "EUR",
                        "source": "generations[0][0].message.response_metadata.total_cost",
                    }
                ],
            ),
            (
                "invalid",
                {"total_cost": True},
                {"total_cost": 6.75, "currency": "CAD"},
                [
                    {
                        "amount": 6.75,
                        "currency": "CAD",
                        "source": "generations[0][0].message.response_metadata.total_cost",
                    }
                ],
            ),
        )
        for name, aggregate, message_cost, expected in cases:
            trace = start_trace(self.user, create_conversation(self.user), name)
            callbacks = trace.config()["callbacks"]
            assert isinstance(callbacks, list)
            callback = callbacks[0]
            run_id = uuid.uuid4()
            callback.on_chat_model_start({}, [], run_id=run_id)
            response = LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content="reply",
                                response_metadata=message_cost,
                                usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                            )
                        )
                    ]
                ],
                llm_output={"token_usage": aggregate},
            )
            callback.on_llm_end(response, run_id=run_id)
            trace.finish_capture(AgentRunStatus.COMPLETED)
            span = AgentExecutionSpan.objects.get(trace_id=trace.trace_id, run_id=run_id)
            self.assertEqual(span.details["reported_costs"], expected)
            self.assertEqual(span.details["usage_summary"]["total_tokens"], 5)
            context = inspect_trace(AgentExecutionTrace.objects.get(pk=trace.trace_id), None)
            self.assertEqual(context["trace_overview"]["model_call_count"], 1)
            self.assertEqual(
                json.loads(context["trace_overview"]["reported_costs_preview"]["preview"]),
                expected,
            )


class TraceSendTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Test tracing behavior during source-backed agent runs."""

    agent_scope_mode = None

    def setUp(self):
        super().setUp()
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True}}))
        self.classifier_configuration = create_configuration(
            "trace-classifier", is_scope_classifier=True, is_default=False
        )
        self.classifier = ScriptedToolModel(
            responses=[tool_call_message("ScopeDecision", {"allowed": True}, "decision")]
        )
        graph_factory = self.runtime.model_factory
        use_runtime(
            self,
            model_factory=lambda configuration: (
                self.classifier
                if configuration.pk == self.classifier_configuration.pk
                else graph_factory(configuration)
            ),
        )

    def start_graph(self, script, **kwargs):
        graph = super().start_graph(script, **kwargs)
        self.classifier_configuration = create_configuration(
            "trace-classifier", is_scope_classifier=True, is_default=False
        )
        graph_model = self.model
        use_runtime(
            self,
            model_factory=lambda configuration: (
                self.classifier if configuration.pk == self.classifier_configuration.pk else graph_model
            ),
        )
        return graph

    def send_run(self, text):
        """Submit one durable run using the real configured scope classifier."""
        return post_run_message(self.client, text)

    def test_admitted_graph_persists_classifier_and_complete_graph_parentage(self):
        self.start_graph(
            [
                tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 2}, "trace-read"),
                AIMessage(content="Trace reply."),
            ]
        )
        response = self.send_run("Inspect models")
        events = parse_run_events(response)
        trace = AgentExecutionTrace.objects.get()
        spans = list(trace.spans.all())
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(str(trace.conversation_id), response.json()["conversation_id"])
        self.assertEqual(sum(span.parent_run_id is None for span in spans), 2)
        run_ids = {span.run_id for span in spans}
        self.assertTrue(all(span.parent_run_id is None or span.parent_run_id in run_ids for span in spans))
        self.assertEqual({span.status for span in spans}, {ExecutionStatus.OK})
        models = [span for span in spans if span.kind == "chat_model"]
        self.assertIn("Inspect models", json.dumps([span.input_data for span in models]))
        self.assertIn("Trace reply.", json.dumps([span.output_data for span in models]))
        tool = next(span for span in spans if span.kind == "tool")
        self.assertIn("/nautobot/models.py", json.dumps(tool.input_data))
        self.assertIn("models line 01", json.dumps(tool.output_data))
        self.assertEqual(trace.request, "Inspect models")

    def test_failures_and_refusals_are_traced(self):
        self.classifier.responses = [tool_call_message("ScopeDecision", {"allowed": False}, "decision")]
        denied = self.send_run("Unrelated request")
        self.assertEqual(parse_run_events(denied)[-1][0], "done")
        refused = AgentExecutionTrace.objects.get()
        self.assertEqual(refused.status, ExecutionStatus.REFUSED)
        self.assertEqual(str(refused.conversation_id), denied.json()["conversation_id"])
        self.assertEqual(refused.request, "Unrelated request")
        self.assertEqual(set(refused.spans.values_list("status", flat=True)), {ExecutionStatus.OK})
        self.classifier.on_call = self.fail_model
        failed = self.send_run("Break classifier")
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"], "failed")
        failure = AgentExecutionTrace.objects.get(request="Break classifier")
        self.assertEqual(failure.status, ExecutionStatus.ERROR)
        self.assertEqual(failure.conversation_id, refused.conversation_id)
        self.assertTrue(failure.spans.filter(status=ExecutionStatus.ERROR, error_data__isnull=False).exists())

        create_configuration()

        def unavailable(_user):
            raise ImproperlyConfigured("lookup")

        with run_dependencies(
            graph_source=unavailable,
        ):
            response = post_run_message(self.client, "Lookup failure")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(AgentExecutionTrace.objects.get(request="Lookup failure").status, ExecutionStatus.ERROR)
        self.start_graph([AIMessage(content="Unreachable")])
        self.scripted_model.on_call = self.fail_model
        response = self.send_run("Model failure")
        events = parse_run_events(response)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(AgentExecutionTrace.objects.get(request="Model failure").status, ExecutionStatus.ERROR)

        self.start_graph([AIMessage(content="Unreachable")])
        self.classifier.on_call = None
        self.classifier.responses = [tool_call_message("ScopeDecision", {"allowed": False}, "decision")]
        for mode in ("strict", "off"):
            with (
                self.subTest(mode=mode),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": True, "scope_mode": mode}}),
            ):
                self.send_run(mode)
                trace = AgentExecutionTrace.objects.get(request=mode)
                if mode == "strict":
                    self.assertEqual(trace.status, ExecutionStatus.REFUSED)
                self.assertIsNotNone(trace.conversation_id)
                statuses = set(trace.spans.values_list("status", flat=True))
                self.assertEqual(statuses, {ExecutionStatus.OK})
        self.assertEqual(len(self.scripted_model.requests), 1)

    @staticmethod
    def fail_model(_index):
        """Raise an ordinary provider failure without calling a provider."""
        raise RuntimeError("scripted provider failure")

    def test_trace_persistence_failure_keeps_stream(self):
        self.start_graph([AIMessage(content="Done")])
        with patch.object(QuerySet, "bulk_create", side_effect=RuntimeError("database")):
            response = self.send_run("Persist")
            events = parse_run_events(response)
        trace = AgentExecutionTrace.objects.get(request="Persist")
        self.assertEqual(events[-1], ("done", {}))
        self.assertEqual(trace.status, ExecutionStatus.OK)
        self.assertEqual(trace.capture_state, TraceCaptureState.FAILED)
        self.assertFalse(trace.spans.exists())

        self.start_graph([AIMessage(content="Done")])
        original_create = QuerySet.create

        def fail_trace_creation(queryset, *args, **kwargs):
            if queryset.model is AgentExecutionTrace:
                raise RuntimeError("database")
            return original_create(queryset, *args, **kwargs)

        with patch.object(QuerySet, "create", autospec=True, side_effect=fail_trace_creation):
            events = parse_run_events(self.send_run("Start failure"))
        self.assertEqual(events[-1], ("done", {}))
        self.assertFalse(AgentExecutionTrace.objects.filter(request="Start failure").exists())
