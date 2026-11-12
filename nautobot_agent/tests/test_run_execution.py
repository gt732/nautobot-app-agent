"""Worker execution outcomes use the durable run claim and write fence."""

# Exercise owner/monitor boundaries deterministically without provider or worker services.
# pylint: disable=protected-access,too-many-lines

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connections
from django.db.models.query import QuerySet
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from fastmcp import FastMCP
from langchain_core.messages import AIMessage
from langgraph.checkpoint.conformance.test_utils import generate_checkpoint, generate_config, generate_metadata
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import RunControl

from nautobot_agent.app_settings import CHAT_RUN_SHUTDOWN_MARGIN
from nautobot_agent.chat.chat_stream import CONTEXT, DONE, MESSAGE_START, TOKEN
from nautobot_agent.chat.events import EventStreamError
from nautobot_agent.chat.execution import (
    AgentExecution,
    RunWriteGuard,
    TerminationKind,
    execute_run,
)
from nautobot_agent.chat.history import with_refusal_history
from nautobot_agent.chat.run_status import run_summary
from nautobot_agent.chat.runs import (
    AdmissionKind,
    LockedRun,
    RunReservationConflict,
    claim_run,
    finish_run,
    locked_run_write,
    request_cancel,
    submit_run,
)
from nautobot_agent.mcp.tools import MCPToolCache
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    ExecutionStatus,
    MCPServer,
    TraceCaptureState,
)
from nautobot_agent.runtime import tracing as tracing_module
from nautobot_agent.runtime.agent import AgentGraph, current_runtime
from nautobot_agent.runtime.checkpointer import DjangoCheckpointSaver, attach_run_guard
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import ConversationUnavailable, RunDraining
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE, ScopeDecision
from nautobot_agent.runtime.tracing import start_trace
from nautobot_agent.runtime.turn import AgentTurn
from nautobot_agent.tests.support.chat import (
    NORMALIZED_MODEL_FAILURES,
    BlockingAgent,
    FakeAgent,
    ScriptedToolModel,
    TestRunWriteGuard,
    claimed_run,
    create_configuration,
    create_conversation,
    enable_agent_settings,
    model_message,
    override_agent_settings,
    recorded_run_events,
    run_dependencies,
    use_runtime,
)
from nautobot_agent.tests.support.db import CommittedTestCase
from nautobot_agent.tests.support.graph import DurableSourceGraphTestCase
from nautobot_agent.tests.support.mcp import InProcessLaunch, patched_launches
from nautobot_agent.tests.support.sources import fixture_catalog


class CloseObservedAgent(FakeAgent):
    """Record graph iterator closure performed by the stream's public abort."""

    def __init__(self, closed, label, **kwargs):
        super().__init__(**kwargs)
        iterator = iter(self.parts)

        class CloseObservedIterator:
            """Observe the graph resource boundary."""

            def __iter__(self):
                return self

            def __next__(self):
                return next(iterator)

            def close(self):
                """Record closure and release the underlying iterator."""
                closed.append(label)
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()

        # FakeAgent streams this iterator directly, so the stream's public abort closes it.
        self.parts = CloseObservedIterator()


@dataclass
class OperationState:
    """State for one real MCP dispatch in an execution fixture."""

    done: bool = False


class MCPOperationAgent(FakeAgent):
    """Discover and dispatch MCP through the production graph runtime context."""

    def __init__(self, server, *, before_call=None, on_failure=None):
        super().__init__(parts=[("messages", model_message("model", "completed"))])
        self.server = server
        self.before_call = before_call
        self.on_failure = on_failure

    def stream_events(self, _input, config, *, context=None, **_kwargs):
        assert isinstance(context, AgentContext)
        tools = MCPToolCache().get((self.server.pk,), [self.server], context.mcp_turn).tools

        def invoke(state: OperationState):
            if self.before_call is not None:
                self.before_call()
            try:
                tools[0].invoke({"name": tools[0].name, "args": {}, "id": "operation", "type": "tool_call"})
            except RuntimeError:
                if self.on_failure is None:
                    raise
                self.on_failure(context)
            return {"done": not state.done}

        graph = StateGraph(OperationState, context_schema=AgentContext)
        graph.add_node("invoke", invoke)
        graph.add_edge(START, "invoke")
        graph.add_edge("invoke", END)
        graph.compile().invoke(OperationState(), context=context)
        return super().stream_events(_input, config)


def _mcp_close_observer(closed, original_close):
    """Create a close callback bound to one test's observed event list and original implementation."""

    def close_mcp(turn, *args, **kwargs):
        closed.append("mcp")
        return original_close(turn, *args, **kwargs)

    return close_mcp


def _drain_observer(run, user, execution, testcase):
    """Create a publish callback bound to one run's cancellation observation."""

    def drain_after_token(_run_id, event, _payload):
        if event == TOKEN:
            request_cancel(run.pk, user)
            with testcase.assertRaises(RunDraining):
                execution.guard.require_new_work()

    return drain_after_token


class RunExecutionTestCase(CommittedTestCase):
    """Minimal committed fixtures shared by execution and finalization scenarios."""

    agent_scope_mode = "off"

    def setUp(self):
        self.user = get_user_model().objects.create_user(username=f"execution-{uuid.uuid4()}")
        enable_agent_settings(self)

    def submit(self, prompt="A request"):
        """Admit an isolated worker fixture without publishing to the shared broker."""
        result = submit_run(self.user, uuid.uuid4(), prompt, None, lambda *args, **kwargs: None)
        assert result.run is not None
        return result.run

    def start_execution(self, prompt="A request", *, heartbeat_interval=0.02, **claim_overrides):
        """Admit and claim one worker fixture with optional deadline overrides."""
        run = self.submit(prompt)
        claim = replace(claimed_run(run.pk), **claim_overrides)
        return run, AgentExecution(claim, heartbeat_interval=heartbeat_interval)

    @staticmethod
    def _run_and_close(execution):
        try:
            execution.run()
        finally:
            connections.close_all()

    @contextmanager
    def _observe_drain(self, execution):
        """Observe the real public drain operation without replacing its behavior."""
        observed = threading.Event()
        request_drain = RunControl.request_drain

        def observe(control, *args, **kwargs):
            request_drain(control, *args, **kwargs)
            if control is execution.control:
                observed.set()

        with patch.object(RunControl, "request_drain", new=observe):
            yield observed

    def _execute(self, run, *, allowed=True, parts=None, mode="off", start_error=None):
        graph = AgentGraph(FakeAgent(parts=parts), "test:model", 1000)

        def classifier(*_args, **_kwargs):
            return ScopeDecision(allowed=allowed)

        with (
            override_agent_settings(scope_mode=mode),
            run_dependencies(graph_source=lambda _user: graph, scope_classifier=classifier),
            recorded_run_events(start_error=start_error),
        ):
            execute_run(str(run.pk))


class RunWriteGuardTests(TestCase):
    """Verify guarded authority and execution finalization against persisted run state."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username=f"guard-{uuid.uuid4()}")
        self.conversation = create_conversation(self.user)
        self.clock = self.enterContext(patch("nautobot_agent.chat.execution.time.monotonic", return_value=100.0))
        self.drain_events = []
        self.guard = TestRunWriteGuard(
            self.conversation,
            on_drain=lambda: self.drain_events.append(("drain", AgentRun.objects.get(pk=self.run_row.pk).status)),
        )
        self.run_row = AgentRun.objects.get(pk=self.guard.claim.run_id)
        self.execution = AgentExecution(self.guard.claim)
        self.execution.guard = self.guard

    def test_revoked_admission_fence_denies_inactive_owner_after_terminal_interruption(self):
        work = []
        self.user.is_active = False
        self.user.save(update_fields=("is_active",))
        finish_run(self.run_row.pk, AgentRunStatus.INTERRUPTED, error_code="stale_heartbeat")

        def admit_work():
            self.guard.require_new_work()
            work.append("model-or-tool")

        with self.assertRaises(ConversationUnavailable):
            admit_work()

        self.run_row.refresh_from_db()
        self.assertEqual(work, [])
        self.assertEqual(self.run_row.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(self.run_row.error_code, "stale_heartbeat")

    def test_heartbeat_storage_failure_exhaustion_interrupts(self):
        baseline = self.clock.return_value
        with (
            patch("nautobot_agent.chat.execution.time.monotonic", return_value=baseline + 20),
            patch.object(LockedRun, "heartbeat", autospec=True, side_effect=DatabaseError("exhausted")),
        ):
            with self.assertRaises(ConversationUnavailable):
                self.guard._heartbeat()
        self.assertTrue(self.guard.invalid)

    def test_inactive_owner_and_cancellation_deny_new_work(self):
        self.user.is_active = False
        self.user.save(update_fields=("is_active",))
        with self.assertRaises(ConversationUnavailable):
            self.guard.require_new_work()
        self.run_row.refresh_from_db()
        self.assertEqual(
            (self.run_row.status, self.run_row.error_code), (AgentRunStatus.INTERRUPTED, "principal_inactive")
        )

        self.user.is_active = True
        self.user.save(update_fields=("is_active",))
        cancelled = TestRunWriteGuard(create_conversation(self.user))
        request_cancel(cancelled.claim.run_id, self.user)
        with self.assertRaises(RunDraining):
            cancelled.require_new_work()
        with self.assertRaises(RunDraining):
            cancelled.require_new_work()
        cancelled.invalidate()
        with self.assertRaises(ConversationUnavailable):
            cancelled.require_new_work()


class RunExecutionTests(RunExecutionTestCase):
    """Observe normal execution, projection, and live delivery behavior."""

    def test_normalized_model_failures_are_persisted_and_published_safely(self):
        """Normalize provider failures while retaining safe user-facing errors."""
        secret = "PROVIDER-SECRET-SENTINEL"
        for error_type, code in NORMALIZED_MODEL_FAILURES:
            with self.subTest(code=code):
                run, execution = self.start_execution()

                def failing_parts(error_type=error_type):
                    yield "messages", model_message("model", "partial answer")
                    raise error_type(secret)

                closed = []
                graph = AgentGraph(CloseObservedAgent(closed, "graph", parts=failing_parts()), "test:model", 1000)

                def observe_error(closed, _run_id, name, _payload):
                    if name == "error":
                        self.assertEqual(closed, ["graph"])

                with (
                    run_dependencies(graph_source=lambda _user, graph=graph: graph),
                    recorded_run_events(execution=execution, publish=partial(observe_error, closed)) as published,
                    self.assertLogs("nautobot_agent", level="WARNING") as logs,
                ):
                    execution.run()

                run.refresh_from_db()
                summary = run_summary(run)
                error_events = [payload for name, payload in published if name == "error"]
                self.assertEqual(run.status, AgentRunStatus.FAILED)
                self.assertEqual(run.error_code, code)
                self.assertEqual(len(error_events), 1)
                self.assertEqual(error_events[0]["error"], summary["error_message"])
                self.assertTrue(any(name == TOKEN for name, _payload in published))
                self.assertNotIn(DONE, [name for name, _payload in published])
                self.assertFalse(AgentRun.objects.active().filter(conversation=run.conversation).exists())
                self.assertNotIn(secret, str(published) + str(summary))
                self.assertEqual(len(logs.records), 1)
                self.assertIn("failing_parts", logs.output[0])
                self.assertNotIn(secret, logs.output[0])

    def test_invalidation_inside_write_does_not_deadlock_with_finalization(self):  # pylint: disable=too-many-locals,too-many-statements
        run, execution = self.start_execution()
        claim = execution.claim
        write_entered, invalidate_write, finish_entered, invalidated = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        outcomes = []
        errors = []
        original_finish = finish_run

        def observe_finish(*args, **kwargs):
            finish_entered.set()
            return original_finish(*args, **kwargs)

        def write_bookkeeping():
            try:
                with execution.guard.write(str(claim.conversation_id)) as write:
                    write.store_display("tentative", None, [])
                    write.mark_live_events_incomplete()
                    write_entered.set()
                    if not invalidate_write.wait(4):
                        raise TimeoutError("write invalidation was not released")
                    execution.guard.invalidate()
                    invalidated.set()
            except ConversationUnavailable:
                outcomes.append("rolled_back")
            except Exception as error:  # pylint: disable=broad-exception-caught
                errors.append(error)
            finally:
                connections.close_all()

        def finalize():
            try:
                outcomes.append(execution._commit_outcome(TerminationKind.COMPLETED, "", ""))
            except Exception as error:  # pylint: disable=broad-exception-caught
                errors.append(error)
            finally:
                connections.close_all()

        writer = threading.Thread(target=write_bookkeeping, daemon=True)
        finalizer = threading.Thread(target=finalize, daemon=True)
        with patch("nautobot_agent.chat.execution.finish_run", side_effect=observe_finish):
            writer.start()
            try:
                self.assertTrue(write_entered.wait(3))
                finalizer.start()
                self.assertTrue(finish_entered.wait(3))
                invalidate_write.set()
                self.assertTrue(invalidated.wait(3))
            finally:
                invalidate_write.set()
                writer.join(timeout=4)
                if finalizer.ident is not None:
                    finalizer.join(timeout=4)

        self.assertFalse(writer.is_alive())
        self.assertFalse(finalizer.is_alive())
        self.assertEqual(errors, [])
        self.assertIn("rolled_back", outcomes)
        run.refresh_from_db()
        self.assertEqual(run.display_model, "")
        self.assertIsNone(run.display_context_limit)
        self.assertFalse(run.live_events_incomplete)
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)

    def test_guard_rejects_conversation_mismatch_and_distinguishes_storage_failure(self):
        run, execution = self.start_execution()
        claim = execution.claim
        with self.assertRaises(ConversationUnavailable):
            with execution.guard.write(str(uuid.uuid4())) as write:
                write.store_display("forbidden", 1, [])
        self.assertFalse(execution.guard.invalid)
        with patch("nautobot_agent.chat.execution.locked_run_write", side_effect=DatabaseError("unavailable")):
            with self.assertRaises(ConversationUnavailable):
                with execution.guard.write(str(claim.conversation_id)):
                    self.fail("Unavailable storage admitted a write.")
        self.assertTrue(execution.guard.invalid)
        with self.assertRaises(ConversationUnavailable):
            with execution.guard.write(str(claim.conversation_id)) as write:
                write.store_display("forbidden", 1, [])
        run.refresh_from_db()
        self.assertEqual(run.display_model, "")

    def test_optional_storage_failure_and_drain_notification_retry(self):
        _run, execution = self.start_execution()
        with patch("nautobot_agent.chat.execution.locked_run_write", side_effect=DatabaseError("unavailable")):
            self.assertFalse(execution.guard.mark_live_events_incomplete())
        self.assertFalse(execution.guard.invalid)

        notified = []

        def notify():
            notified.append(True)
            if len(notified) == 1:
                raise RuntimeError("transient notification failure")

        execution.guard._drain_handler = notify
        execution.guard.request_drain("cancelled")
        execution.guard.request_drain("cancelled")
        self.assertEqual(notified, [True, True])

    def test_graph_projection_error_fails_the_run(self):
        run = self.submit()

        def broken_parts():
            yield from ()
            raise RuntimeError("EXECUTION-SECRET-SENTINEL at C:\\private\\project\\source.py")

        with self.assertLogs("nautobot_agent.chat_stream", level="ERROR") as logs:
            self._execute(run, parts=broken_parts())
        run.refresh_from_db()

        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertEqual(run.error_code, "RuntimeError")
        diagnostic = "\n".join(logs.output)
        self.assertIn("RuntimeError", diagnostic)
        self.assertIn("broken_parts", diagnostic)
        self.assertNotIn("EXECUTION-SECRET-SENTINEL", diagnostic)
        self.assertNotIn("C:\\private\\project\\source.py", diagnostic)
        self.assertNotIn(__file__, diagnostic)

        timed_out = self.submit("soft timeout")

        def timed_out_parts():
            yield from ()
            raise SoftTimeLimitExceeded()

        self._execute(timed_out, parts=timed_out_parts())
        timed_out.refresh_from_db()
        self.assertEqual((timed_out.status, timed_out.error_code), (AgentRunStatus.FAILED, "soft_time_limit"))

        draining = self.submit("unexpected drain")

        def unexpected_drain_parts():
            yield from ()
            raise RunDraining("unexpected graph drain")

        self._execute(draining, parts=unexpected_drain_parts())
        draining.refresh_from_db()
        self.assertEqual((draining.status, draining.error_code), (AgentRunStatus.FAILED, "execution_unavailable"))

    def test_event_start_failure_resolves_the_run(self):
        run, execution = self.start_execution()
        with recorded_run_events(execution=execution, start_error=EventStreamError("redis unavailable")):
            execution.run()
        run.refresh_from_db()
        self.assertTrue(run.is_terminal)
        self.assertFalse(AgentRun.objects.active().filter(conversation=run.conversation).exists())

    def test_mcp_close_facts_preserve_cleanup_and_unknown_outcomes(self):
        cases = (
            (RuntimeError("close failed"), "cleanup_unconfirmed", ""),
            (SoftTimeLimitExceeded(), "cleanup_unconfirmed", "soft_time_limit"),
            (SimpleNamespace(cleanup_confirmed=True, external_outcome_unknown=True), "mcp_outcome_unknown", ""),
        )
        for close_result, reason, drain in cases:
            with self.subTest(reason=reason, drain=drain):
                _run, execution = self.start_execution()
                execution._close_mcp()

                def close(*_args, result=close_result, **_kwargs):
                    if isinstance(result, BaseException):
                        raise result
                    return result

                execution.mcp = SimpleNamespace(close=close)
                execution._close_mcp()
                self.assertEqual(execution.guard._interruption_reason, reason)
                self.assertEqual(execution.guard.drain_reason, drain)
                self.assertEqual(execution.guard._cleanup_incomplete, reason == "cleanup_unconfirmed")
                finish_run(_run.pk, AgentRunStatus.FAILED, error_code=reason)

    def test_trace_tool_outcome_and_failed_capture(self):
        run = self.submit()
        with override_agent_settings(tracing=True):
            trace = start_trace(self.user, run.conversation, "trace callback")
            callbacks = trace.config()["callbacks"]
            assert isinstance(callbacks, list)
            recorder = callbacks[0]
            tool_id = uuid.uuid4()
            recorder.on_tool_start({"name": "read_file"}, "{}", run_id=tool_id)
            recorder.on_tool_end(SimpleNamespace(status="error"), run_id=tool_id)
            recorder.on_tool_end(SimpleNamespace(status="success"), run_id=uuid.uuid4())
            success_id = uuid.uuid4()
            recorder.on_tool_start({"name": "read_file"}, "{}", run_id=success_id)
            recorder.on_tool_end(SimpleNamespace(status="success"), run_id=success_id)
            error_id = uuid.uuid4()
            recorder.on_tool_start({"name": "read_file"}, "{}", run_id=error_id)
            recorder.on_tool_error(RuntimeError("tool failed"), run_id=error_id)
            AgentExecutionTrace.objects.filter(pk=trace.trace_id).update(
                status=ExecutionStatus.OK, ended_at=timezone.now()
            )
            self.assertEqual(trace.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.COMPLETE)
            self.assertEqual(AgentExecutionSpan.objects.get(run_id=tool_id).status, ExecutionStatus.ERROR)
            self.assertEqual(AgentExecutionSpan.objects.get(run_id=success_id).status, ExecutionStatus.OK)
            self.assertEqual(AgentExecutionSpan.objects.get(run_id=error_id).status, ExecutionStatus.ERROR)

            broken = start_trace(self.user, run.conversation, "failed trace capture")
            AgentExecutionTrace.objects.filter(pk=broken.trace_id).update(
                status=ExecutionStatus.OK, ended_at=timezone.now()
            )
            with patch.object(tracing_module._Recorder, "_flush", side_effect=RuntimeError("trace persistence failed")):
                self.assertEqual(broken.finish_capture(AgentRunStatus.COMPLETED), TraceCaptureState.FAILED)

            abandoned = start_trace(self.user, run.conversation, "unassociated trace")
            abandoned_id = abandoned.trace_id
            abandoned.abandon()
            abandoned_row = AgentExecutionTrace.objects.get(pk=abandoned_id)
            self.assertEqual(
                (abandoned_row.status, abandoned_row.capture_state),
                (ExecutionStatus.INCOMPLETE, TraceCaptureState.FAILED),
            )

    def test_trace_link_failure_abandons_the_unlinked_trace(self):
        for link_failure in (False, DatabaseError("trace link failed")):
            with self.subTest(link_failure=type(link_failure).__name__):
                run, execution = self.start_execution()
                link_patch = (
                    patch.object(RunWriteGuard, "link_trace", return_value=False)
                    if link_failure is False
                    else patch.object(RunWriteGuard, "link_trace", side_effect=link_failure)
                )
                with (
                    override_agent_settings(tracing=True),
                    run_dependencies(graph_source=lambda _user: AgentGraph(FakeAgent(), "test:model", 1000)),
                    recorded_run_events(execution=execution),
                    link_patch,
                ):
                    execution.run()
                trace = AgentExecutionTrace.objects.get(conversation=run.conversation)
                self.assertEqual(
                    (trace.status, trace.capture_state), (ExecutionStatus.INCOMPLETE, TraceCaptureState.FAILED)
                )

    def test_finalization_exception_revokes_authority_without_claiming_success(self):
        run, execution = self.start_execution()
        with (
            override_agent_settings(tracing=True),
            run_dependencies(graph_source=lambda _user: AgentGraph(FakeAgent(), "test:model", 1000)),
            recorded_run_events(execution=execution) as published,
            patch.object(tracing_module.ExecutionTrace, "freeze", side_effect=RuntimeError("trace freeze failed")),
        ):
            execution.run()
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.RUNNING)
        self.assertTrue(execution.guard.invalid)
        self.assertNotIn(DONE, [name for name, _payload in published])

    def test_diagnostic_handoff_is_joined_before_mcp_shutdown(self):
        started, release = threading.Event(), threading.Event()

        def persist():
            started.set()
            if not release.wait(2):
                raise TimeoutError("diagnostic persistence was not released")

        turn = MCPTurn(host=cast(Any, SimpleNamespace(persist_diagnostics=persist)))
        self.addCleanup(turn.close, timeout=2)
        self.addCleanup(turn.cancel)

        async def persist_and_wait():
            turn._handoff_diagnostics()
            await turn._await_handoffs(asyncio.get_running_loop().time() + 2)

        future = turn._submit(persist_and_wait())
        self.assertTrue(started.wait(2))
        release.set()
        future.result(timeout=3)
        turn.close(timeout=2)
        self.assertTrue(release.is_set())


class RunCleanupTests(RunExecutionTestCase):
    """Observe timeout cleanup evidence and disposal across execution unwind paths."""

    def test_late_cooperative_drain_keeps_one_bounded_cleanup_deadline(self):  # pylint: disable=too-many-locals
        run = self.submit()
        claim = claimed_run(run.pk)
        clock = [0.0]
        execution = AgentExecution(claim)
        execution.started = 0.0
        close_observations = []
        original_close = MCPTurn.close

        class LateDrainAgent(FakeAgent):
            """Request cooperative drain at a controlled late point in graph execution."""

            def stream_events(self, _input, config, **kwargs):
                clock[0] = claim.soft_time_limit + 2.0
                execution.guard.request_drain("soft_deadline")
                return super().stream_events(_input, config, **kwargs)

        def slow_confirmed_close(turn, timeout=20):
            deadline = execution.cleanup_deadline
            close_observations.append((clock[0], timeout, deadline))
            clock[0] += 19.0
            return original_close(turn, timeout=timeout)

        with (
            patch("nautobot_agent.chat.execution.time", SimpleNamespace(monotonic=lambda: clock[0])),
            patch.object(threading.Thread, "start"),
            run_dependencies(graph_source=lambda _user: AgentGraph(LateDrainAgent(), "test:model", 1000)),
            recorded_run_events(execution=execution) as published,
            patch.object(MCPTurn, "close", new=slow_confirmed_close),
        ):
            execution.run()
        run.refresh_from_db()
        drain_time, close_budget, latched_deadline = close_observations[0]
        completion_time = clock[0]

        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertEqual(run.error_code, "soft_deadline")

        self.assertEqual(run.prompt, "")
        self.assertEqual(drain_time, claim.soft_time_limit + 2.0)
        self.assertEqual(close_budget, claim.cleanup_timeout)
        self.assertEqual(latched_deadline, drain_time + claim.cleanup_timeout)
        self.assertEqual(completion_time, drain_time + 19.0)
        self.assertGreater(completion_time, claim.soft_time_limit + claim.cleanup_timeout)
        self.assertLess(
            completion_time,
            claim.soft_time_limit + claim.cleanup_timeout + CHAT_RUN_SHUTDOWN_MARGIN,
        )
        self.assertNotIn(DONE, [name for name, _payload in published])


class RunFinalizationTests(RunExecutionTestCase):
    """Exercise confirmed transitions and concurrent monitor/trace handoffs."""

    def test_refusal_survives_cooperative_drain_during_confirmed_cleanup(self):
        for reason in ("cancelled", "soft_deadline"):
            with self.subTest(reason=reason):
                self._exercise_refusal_cleanup_drain(reason)

    def _exercise_refusal_cleanup_drain(self, reason):
        run, execution = self.start_execution()
        original_close = MCPTurn.close
        done_statuses = []

        def drain_during_cleanup(turn, timeout=20):
            self.assertTrue(execution.done_pending)
            self.assertEqual(done_statuses, [])
            execution.guard.request_drain(reason)
            return original_close(turn, timeout=timeout)

        def publish(_run_id, name, _payload):
            if name == DONE:
                done_statuses.append(AgentRun.objects.get(pk=run.pk).status)

        with (
            override_agent_settings(tracing=True, scope_mode="strict"),
            run_dependencies(scope_classifier=lambda *a, **k: ScopeDecision(allowed=False)),
            recorded_run_events(execution=execution, publish=publish),
            patch.object(MCPTurn, "close", autospec=True, side_effect=drain_during_cleanup),
        ):
            execution.run()
        run.refresh_from_db()

        self.assertEqual(run.status, AgentRunStatus.REFUSED)
        self.assertEqual(run.error_code, "")
        self.assertEqual(
            [message.text for message in with_refusal_history(run.conversation, [])],
            ["A request", REFUSAL_MESSAGE],
        )

        self.assertEqual(AgentExecutionTrace.objects.get(pk=run.trace_id).status, ExecutionStatus.REFUSED)
        self.assertEqual(done_statuses, [AgentRunStatus.REFUSED])
        execution.guard.invalidate()
        execution.guard.record_interruption("late_reason", drain_reason="late_cancel")
        execution.guard.interrupt("late_reason")
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.REFUSED)

    def test_midstream_coverage_conflict_drains_before_graph_close_and_preserves_cleanup_reason(self):
        run, execution = self.start_execution(heartbeat_interval=0.02)
        order = []
        published = []
        original_cancel = MCPTurn.cancel

        def fail_midstream(_run_id, name, _payload):
            published.append(name)
            if name == MESSAGE_START:
                raise EventStreamError("event stream failed")

        def observe_cancel(turn):
            order.append("mcp_cancel")
            return original_cancel(turn)

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(
                    CloseObservedAgent(order, "graph_close", parts=[("messages", model_message("model", "partial"))]),
                    "test:model",
                    1000,
                )
            ),
            recorded_run_events(execution=execution, publish=fail_midstream),
            patch.object(
                LockedRun, "mark_live_events_incomplete", side_effect=RunReservationConflict("coverage rejected")
            ),
            patch.object(MCPTurn, "cancel", new=observe_cancel),
        ):
            execution.run()
        run.refresh_from_db()

        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(run.error_code, "graph_cleanup_unconfirmed")
        self.assertEqual(run.prompt, "")

        self.assertLess(order.index("mcp_cancel"), order.index("graph_close"))
        self.assertEqual(execution.control.drain_reason, "ownership")
        self.assertTrue(execution.guard.invalid)
        self.assertNotIn(DONE, published)

    def test_finish_wins_before_monitor_reports_failure(self):
        for monitor_failure in (None, DatabaseError("delayed monitor storage error")):
            with self.subTest(monitor_failure=monitor_failure):
                self._exercise_finish_before_monitor(monitor_failure)

    def _exercise_finish_before_monitor(self, monitor_failure):
        run, execution = self.start_execution()
        graph_entered, release_graph = threading.Event(), threading.Event()
        monitor_entered, release_monitor = threading.Event(), threading.Event()
        self.addCleanup(release_graph.set)
        self.addCleanup(release_monitor.set)
        finished = threading.Event()
        original_write = locked_run_write

        def gate_monitor_write(*args, **kwargs):
            if threading.current_thread().name.startswith("chat-run-"):
                monitor_entered.set()
                if not release_monitor.wait(5):
                    raise TimeoutError("monitor fence check was not released")
                if monitor_failure is not None:
                    raise monitor_failure
            return original_write(*args, **kwargs)

        def observe_finish(*args, **kwargs):
            finish_run(*args, **kwargs)
            finished.set()

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(graph_entered, release_graph), "test:model", 1000)
            ),
            recorded_run_events(execution=execution),
            patch("nautobot_agent.chat.execution.locked_run_write", side_effect=gate_monitor_write),
            patch("nautobot_agent.chat.execution.finish_run", side_effect=observe_finish),
        ):
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(self._run_and_close, execution)
                self.assertTrue(graph_entered.wait(3))
                self.assertTrue(monitor_entered.wait(3))
                release_graph.set()
                self.assertTrue(finished.wait(3))
                release_monitor.set()
                result.result(timeout=4)
            finally:
                release_graph.set()
                release_monitor.set()
                executor.shutdown(wait=True)
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(run.error_code, "")
        self.assertFalse(execution.guard.invalid)
        self.assertFalse(execution.control.drain_requested)

    def test_blocked_trace_flush_releases_reservation_for_successor_checkpoint(self):  # pylint: disable=too-many-locals
        run, execution = self.start_execution(cleanup_timeout=0.05, heartbeat_interval=0.02)
        flushing, release_flush = threading.Event(), threading.Event()
        self.addCleanup(release_flush.set)
        original_flush = tracing_module._Recorder.flush

        def blocked_flush(recorder, trace_id, run_status):
            flushing.set()
            if not release_flush.wait(5):
                raise TimeoutError("trace flush was not released")
            return original_flush(recorder, trace_id, run_status)

        executor = ThreadPoolExecutor(max_workers=1)
        with (
            override_agent_settings(tracing=True),
            run_dependencies(graph_source=lambda _user: AgentGraph(FakeAgent(), "test:model", 1000)),
            recorded_run_events(execution=execution) as published,
            patch.object(tracing_module._Recorder, "flush", autospec=True, side_effect=blocked_flush),
        ):
            try:
                future = executor.submit(self._run_and_close, execution)
                self.assertTrue(flushing.wait(5))
                assert execution.monitor is not None
                self.assertFalse(execution.monitor.is_alive())
                self.assertFalse(release_flush.wait(0.2))  # Exceeds the cleanup deadline while capture is blocked.
                run.refresh_from_db()
                trace = AgentExecutionTrace.objects.get(pk=run.trace_id)
                self.assertEqual(run.status, AgentRunStatus.COMPLETED)
                self.assertEqual((trace.status, trace.capture_state), (ExecutionStatus.OK, TraceCaptureState.PENDING))
                self.assertIsNotNone(trace.ended_at)
                self.assertIn(DONE, [name for name, _payload in published])

                successor_result = submit_run(
                    self.user, uuid.uuid4(), "successor", run.conversation_id, lambda *args, **kwargs: None
                )
                assert successor_result.run is not None
                successor_guard = RunWriteGuard(claimed_run(successor_result.run.pk))
                DjangoCheckpointSaver().put(
                    attach_run_guard(generate_config(str(run.conversation_id)), successor_guard),
                    generate_checkpoint(),
                    generate_metadata(),
                    {},
                )
            finally:
                release_flush.set()
                executor.shutdown(wait=True)
        future.result(timeout=5)
        run.refresh_from_db()
        successor = AgentRun.objects.get(pk=successor_result.run.pk)
        trace = AgentExecutionTrace.objects.get(pk=run.trace_id)

        self.assertEqual((run.status, run.error_code), (AgentRunStatus.COMPLETED, ""))
        self.assertEqual((trace.status, trace.capture_state), (ExecutionStatus.OK, TraceCaptureState.COMPLETE))
        self.assertEqual((successor.status, successor.trace_id), (AgentRunStatus.RUNNING, None))
        self.assertFalse(successor_guard.invalid)
        request_cancel(successor.pk, self.user)

    def test_terminal_status_failure_is_unconfirmed_and_capture_stays_pending(self):
        run, execution = self.start_execution()
        with (
            override_agent_settings(tracing=True),
            run_dependencies(graph_source=lambda _user: AgentGraph(FakeAgent(), "test:model", 1000)),
            recorded_run_events(execution=execution) as published,
            patch("nautobot_agent.chat.execution.finish_run", side_effect=DatabaseError("finish failed")),
        ):
            execution.run()
        run.refresh_from_db()
        trace = AgentExecutionTrace.objects.get(pk=run.trace_id)

        self.assertEqual(run.status, AgentRunStatus.RUNNING)
        self.assertNotIn(DONE, [name for name, _payload in published])
        self.assertTrue(execution.guard.invalid)
        self.assertEqual((trace.status, trace.capture_state), (ExecutionStatus.RUNNING, TraceCaptureState.PENDING))
        self.assertIsNone(trace.ended_at)


class RunResourceLifecycleTests(RunExecutionTestCase):
    """Keep resource cleanup, blocked owners, and successor fencing observable."""

    def test_definitive_mcp_result_with_failed_final_cleanup_interrupts(self):  # pylint: disable=too-many-locals
        remote = FastMCP("failed-final-mcp-cleanup")

        @remote.tool
        def operation() -> str:
            """Return a definitive result before cleanup fails."""
            return "remote result"

        server = MCPServer.objects.create(
            name=f"Failed final MCP {uuid.uuid4()}",
            url="http://127.0.0.1:1/mcp",
            enabled=True,
        )
        invoked = []

        skip_discovery_client = [True]

        def fail_close(client):
            if skip_discovery_client[0]:
                skip_discovery_client[0] = False
                return
            original_close = client.close

            async def close():
                await original_close()
                raise OSError("final MCP cleanup failed")

            client.close = close

        def launch_factory(_spec):
            return InProcessLaunch(remote, on_client=fail_close)

        run, execution = self.start_execution()
        events = []
        with (
            patched_launches(launch_factory),
            run_dependencies(
                graph_source=lambda _user: AgentGraph(
                    MCPOperationAgent(server, before_call=lambda: invoked.append("provider")), "test:model", 1000
                )
            ),
            recorded_run_events(execution=execution, publish=lambda _run, name, *_a, **_k: events.append(name)),
        ):
            execution.run()
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(run.error_code, "cleanup_unconfirmed")
        self.assertEqual(invoked, ["provider"])
        self.assertEqual(run.prompt, "")
        self.assertTrue(run.cleanup_incomplete)
        self.assertFalse(run.external_outcome_unknown)

        self.assertNotIn(DONE, events)

    def test_late_classification_cannot_write_or_release_successor_after_interruption(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                run = self.submit("late classification")
                entered, release = threading.Event(), threading.Event()
                outcome = ScopeDecision(allowed=allowed)

                def delayed_classifier(*args, _entered=entered, _release=release, _outcome=outcome, **kwargs):
                    _entered.set()
                    if not _release.wait(10):
                        raise TimeoutError("classifier release timed out")
                    return _outcome

                with (
                    override_agent_settings(scope_mode="strict"),
                    run_dependencies(
                        graph_source=lambda _user: AgentGraph(FakeAgent(), "test:model", 1000),
                        scope_classifier=delayed_classifier,
                    ),
                    recorded_run_events(),
                ):
                    executor = ThreadPoolExecutor(max_workers=1)

                    def execute_and_close(_run=run):
                        try:
                            execute_run(str(_run.pk))
                        finally:
                            connections.close_all()

                    try:
                        pending = executor.submit(execute_and_close)
                        self.assertTrue(entered.wait(5))
                        run.refresh_from_db()
                        # Revoke the actual owner deterministically; stale-heartbeat
                        # detection is independently covered by the runs suite.
                        finish_run(run.pk, AgentRunStatus.INTERRUPTED, error_code="stale_heartbeat")
                        successor = submit_run(
                            self.user,
                            uuid.uuid4(),
                            "successor",
                            run.conversation_id,
                            lambda *args, **kwargs: None,
                        )
                        assert successor.run is not None
                        successor = successor.run
                        release.set()
                        self.assertIsNone(pending.result(timeout=10))
                    finally:
                        release.set()
                        executor.shutdown(wait=True)
                run.refresh_from_db()
                successor.refresh_from_db()
                conversation = AgentConversation.objects.get(pk=run.conversation_id)
                self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
                self.assertEqual(successor.status, AgentRunStatus.QUEUED)

                self.assertFalse(conversation.runs.filter(refusal_anchor_id__isnull=False).exists())
                request_cancel(successor.pk, self.user)  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.

    def test_cancelled_graph_aborts_before_mcp_close_without_cleanup_evidence(self):
        # Cancel after the first token so the public abort happens during drain.
        closed = []
        run, execution = self.start_execution(heartbeat_interval=0.02)
        original_mcp_close = MCPTurn.close
        agent = CloseObservedAgent(closed, "graph", parts=[("messages", model_message("model", "partial"))])

        with (
            run_dependencies(graph_source=lambda _user: AgentGraph(agent, "test:model", 1000)),
            recorded_run_events(execution=execution, publish=_drain_observer(run, self.user, execution, self)),
            patch.object(MCPTurn, "close", new=_mcp_close_observer(closed, original_mcp_close)),
        ):
            execution.run()
        run.refresh_from_db()

        self.assertEqual(closed, ["graph", "mcp"])
        self.assertEqual(execution.control.drain_reason, "cancelled")
        self.assertEqual((run.status, run.error_code), (AgentRunStatus.CANCELLED, ""))

    def test_monitor_heartbeats_and_observes_cancellation_during_blocked_provider(self):
        run = self.submit()
        claim = claimed_run(run.pk)
        run.heartbeat_at = timezone.now() - timedelta(minutes=1)
        run.save(update_fields=("heartbeat_at",))
        execution = AgentExecution(claim, heartbeat_interval=0.05)
        entered, release = threading.Event(), threading.Event()
        heartbeated, monitor_closed = threading.Event(), threading.Event()
        close_all = connections.close_all

        original_heartbeat = RunWriteGuard.heartbeat

        def observe_heartbeat(guard):
            completed = original_heartbeat(guard)
            if threading.current_thread().name.startswith("chat-run-") and completed:
                heartbeated.set()
            return completed

        def close_monitor_connection():
            close_all()
            if threading.current_thread().name.startswith("chat-run-"):
                monitor_closed.set()

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(entered, release), "test:model", 1000)
            ),
            self._observe_drain(execution) as drained,
            recorded_run_events(execution=execution),
            patch.object(connections, "close_all", side_effect=close_monitor_connection),
            patch.object(RunWriteGuard, "heartbeat", new=observe_heartbeat),
        ):
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(self._run_and_close, execution)
                self.assertTrue(entered.wait(2))
                self.assertTrue(heartbeated.wait(2))
                run.refresh_from_db()
                self.assertGreater(run.heartbeat_at, timezone.now() - timedelta(seconds=2))
                AgentRun.objects.filter(pk=run.pk).update(cancel_requested=True)
                self.assertTrue(drained.wait(2))
            finally:
                release.set()
                executor.shutdown(wait=True)
            result.result(timeout=2)
        self.assertTrue(monitor_closed.is_set())
        run.refresh_from_db()
        self.assertEqual(run.status, AgentRunStatus.CANCELLED)
        self.assertEqual(run.error_code, "")
        self.assertTrue(run.cancel_requested)
        self.assertTrue(claim_run(run.pk) is None)

    def test_monitor_soft_deadline_continues_during_heartbeat_database_failures(self):  # pylint: disable=too-many-locals
        run, execution = self.start_execution(soft_time_limit=0.1, heartbeat_interval=0.02)
        entered, release = threading.Event(), threading.Event()
        monitor_closed = threading.Event()
        close_all = connections.close_all
        original_heartbeat = LockedRun.heartbeat

        def fail_heartbeat(write):
            if entered.is_set():
                raise DatabaseError("temporary heartbeat failure")
            return original_heartbeat(write)

        def close_monitor_connection():
            close_all()
            if threading.current_thread().name.startswith("chat-run-"):
                monitor_closed.set()

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(entered, release), "test:model", 1000)
            ),
            self._observe_drain(execution) as observed_drain,
            recorded_run_events(execution=execution),
            patch.object(LockedRun, "heartbeat", autospec=True, side_effect=fail_heartbeat),
            patch.object(connections, "close_all", side_effect=close_monitor_connection),
        ):
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(self._run_and_close, execution)
                self.assertTrue(entered.wait(2))
                self.assertTrue(observed_drain.wait(2))
                self.assertEqual(execution.control.drain_reason, "soft_deadline")
                self.assertFalse(execution.guard.invalid)
                self.assertFalse(execution.guard.finalized)
            finally:
                release.set()
                executor.shutdown(wait=True)
            result.result(timeout=2)

        self.assertTrue(monitor_closed.is_set())
        run.refresh_from_db()

        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertEqual(run.error_code, "soft_deadline")

    def test_monitor_database_failure_exhaustion_interrupts_and_releases_reservation(self):  # pylint: disable=too-many-locals
        run, execution = self.start_execution(heartbeat_interval=0.02)
        entered, release = threading.Event(), threading.Event()
        interrupted, monitor_closed = threading.Event(), threading.Event()
        close_all = connections.close_all
        original_heartbeat = LockedRun.heartbeat

        def fail_heartbeat(write):
            if entered.is_set():
                raise DatabaseError("heartbeat storage unavailable")
            return original_heartbeat(write)

        def observe_finish(*args, **kwargs):
            finish_run(*args, **kwargs)
            if args[1] == AgentRunStatus.INTERRUPTED:
                interrupted.set()

        def close_monitor_connection():
            close_all()
            if threading.current_thread().name.startswith("chat-run-"):
                monitor_closed.set()

        with (
            patch("nautobot_agent.chat.execution.CHAT_RUN_HEARTBEAT_FAILURE_AFTER", 0),
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(entered, release), "test:model", 1000)
            ),
            recorded_run_events(execution=execution),
            patch.object(LockedRun, "heartbeat", autospec=True, side_effect=fail_heartbeat),
            patch("nautobot_agent.chat.execution.finish_run", side_effect=observe_finish),
            patch.object(connections, "close_all", side_effect=close_monitor_connection),
        ):
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(self._run_and_close, execution)
                self.assertTrue(entered.wait(2))
                self.assertTrue(interrupted.wait(2))
            finally:
                release.set()
                executor.shutdown(wait=True)
            result.result(timeout=2)

        self.assertTrue(monitor_closed.is_set())
        run.refresh_from_db()

        self.assertEqual(
            (run.status, run.error_code, run.prompt), (AgentRunStatus.INTERRUPTED, "ownership_unavailable", "")
        )

        successor = submit_run(
            self.user, uuid.uuid4(), "reservation check", run.conversation_id, lambda *args, **kwargs: None
        )
        self.assertEqual(successor.kind, AdmissionKind.ADMITTED)
        assert successor.run is not None
        request_cancel(successor.run.pk, self.user)  # ty: ignore[invalid-argument-type] Nautobot UUIDField descriptor is typed as the field.

    def test_cleanup_timeout_interruption_failures_use_only_bounded_retries(self):  # pylint: disable=too-many-locals
        run, execution = self.start_execution(cleanup_timeout=0.1)
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        attempts = []
        cancelled = threading.Event()
        original_cancel = MCPTurn.cancel

        def fail_cleanup_interruption(*args, **kwargs):
            attempts.append((kwargs["error_code"], threading.current_thread().name))
            try:
                if len(attempts) <= 2:
                    raise DatabaseError("cleanup interruption storage unavailable")
                return finish_run(*args, **kwargs)
            finally:
                if len(attempts) == 1:
                    attempted.set()

        def observe_cancel(turn):
            original_cancel(turn)
            cancelled.set()

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(entered, release), "test:model", 1000)
            ),
            self._observe_drain(execution) as drained,
            recorded_run_events(execution=execution),
            patch("nautobot_agent.chat.execution.finish_run", side_effect=fail_cleanup_interruption),
            patch.object(MCPTurn, "cancel", new=observe_cancel),
        ):
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(self._run_and_close, execution)
                self.assertTrue(entered.wait(3))
                self.assertFalse(execution.control.drain_requested)
                with execution._cleanup_lock:
                    execution.cleanup_deadline = execution.started - 1
                self.assertTrue(attempted.wait(3))
                self.assertTrue(drained.wait(3))
                self.assertTrue(cancelled.wait(3))
                assert execution.monitor is not None
                execution.monitor.join(timeout=3)
                self.assertFalse(execution.monitor.is_alive())
                self.assertEqual(execution.control.drain_reason, "ownership")
            finally:
                release.set()
                executor.shutdown(wait=True)
            result.result(timeout=5)
        run.refresh_from_db()
        self.assertEqual([reason for reason, _thread in attempts], ["cleanup_unconfirmed"] * 3)
        self.assertTrue(all(thread.startswith("chat-run-") for _reason, thread in attempts[:2]))
        self.assertFalse(attempts[2][1].startswith("chat-run-"))
        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(run.error_code, "cleanup_unconfirmed")
        self.assertEqual(run.prompt, "")

    def test_cleanup_deadline_interrupts_blocked_owner_and_late_finalizer_cannot_overwrite(self):
        run, execution = self.start_execution(cleanup_timeout=0.12, heartbeat_interval=0.03)
        entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()
        original_heartbeat = LockedRun.heartbeat

        def fail_heartbeat(write):
            if entered.is_set():
                raise DatabaseError("temporary heartbeat failure")
            return original_heartbeat(write)

        executor = ThreadPoolExecutor(max_workers=1)

        def execute_and_close():
            self._run_and_close(execution)

        def observe_finish(*args, **kwargs):
            finish_run(*args, **kwargs)
            if args[1] == AgentRunStatus.INTERRUPTED:
                interrupted.set()

        with (
            run_dependencies(
                graph_source=lambda _user: AgentGraph(BlockingAgent(entered, release), "test:model", 1000)
            ),
            recorded_run_events(execution=execution),
            patch.object(LockedRun, "heartbeat", autospec=True, side_effect=fail_heartbeat),
            patch("nautobot_agent.chat.execution.finish_run", side_effect=observe_finish),
        ):
            try:
                future = executor.submit(execute_and_close)
                self.assertTrue(entered.wait(5))
                execution.guard.request_drain("cancelled")
                self.assertTrue(interrupted.wait(3))
                run.refresh_from_db()
                self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
                with self.assertRaises(ConversationUnavailable):
                    DjangoCheckpointSaver().put(
                        attach_run_guard(generate_config(str(run.conversation_id)), execution.guard),
                        generate_checkpoint(),
                        generate_metadata(),
                        {},
                    )
            finally:
                release.set()
                executor.shutdown(wait=True)
        future.result(timeout=5)
        run.refresh_from_db()
        self.assertEqual((run.status, run.error_code), (AgentRunStatus.INTERRUPTED, "cleanup_unconfirmed"))
        self.assertEqual(run.prompt, "")


class DurableRunCheckpointTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Outermost admission and graph executor connections require committed guarded checkpoints."""

    def setUp(self):
        super().setUp()
        use_runtime(self, saver=DjangoCheckpointSaver())

    def submit(self, prompt):
        """Admit committed graph work without publishing to the shared broker."""
        result = submit_run(self.user, uuid.uuid4(), prompt, None, lambda *args, **kwargs: None)
        assert result.run is not None
        return result.run

    def test_authority_loss_in_middleware_interrupts_durable_run(self):
        self.start_graph([AIMessage(content="unreached")])
        run = self.submit("retained request")
        armed = False
        original_invocation = AgentTurn.invocation
        original_write = locked_run_write

        def fail_fence_after_invocation(*args, **kwargs):
            if armed:
                raise DatabaseError("authority storage unavailable")
            return original_write(*args, **kwargs)

        def arm_after_invocation(turn):
            nonlocal armed
            result = original_invocation(turn)
            armed = True
            return result

        with (
            recorded_run_events() as events,
            patch(
                "nautobot_agent.chat.execution.locked_run_write",
                side_effect=fail_fence_after_invocation,
            ),
            patch.object(AgentTurn, "invocation", autospec=True, side_effect=arm_after_invocation),
        ):
            execute_run(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(self.scripted_model.requests, [])

        self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
        self.assertEqual(run.error_code, "ownership_unavailable")
        self.assertEqual(run.prompt, "")
        self.assertFalse(AgentRun.objects.active().filter(conversation_id=run.conversation_id).exists())

        published = [name for name, _payload in events]
        self.assertNotIn("error", published)
        self.assertNotIn(DONE, published)

    def test_missing_agent_context_profile_fails_durably_before_model_or_tools(self):
        """A real graph-preparation failure publishes only safe guidance and releases the run reservation."""
        model = ScriptedToolModel(responses=[AIMessage(content="must not execute")])
        # The test intentionally exercises an invalid constructed profile.
        model.profile = {"provider-private": "PROVIDER-PRIVATE-SENTINEL"}  # ty: ignore[invalid-assignment]
        create_configuration()
        use_runtime(
            self,
            saver=DjangoCheckpointSaver(),
            model_factory=lambda _configuration: model,
            sources=fixture_catalog(self, self.tree),
        )
        run = self.submit("CONSUMER-PROMPT-SENTINEL")
        with recorded_run_events() as published:
            execute_run(str(run.pk))

        run.refresh_from_db()
        conversation = AgentConversation.objects.get(pk=run.conversation_id)
        summary = run_summary(run)
        guidance = summary["error_message"]
        error_events = [payload for name, payload in published if name == "error"]
        restored = current_runtime().read_conversation_values(conversation)["messages"]
        polled = self.client.get(
            reverse("plugins:nautobot_agent:agent_run_events", args=(run.pk,)), {"status_only": "1"}
        )
        reopened = self.client.get(
            reverse("plugins:nautobot_agent:agent_state"), {"conversation": str(conversation.pk)}
        )
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(polled.json()["error_message"], guidance)
        self.assertEqual(reopened.json()["latest_run"]["error_message"], guidance)
        self.assertEqual(reopened.json()["history"], [])

        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertEqual(run.error_code, "model_context_limit_missing")
        self.assertEqual(error_events, [{"error": guidance}])
        self.assertEqual(run.prompt, "")
        self.assertIsNone(run.display_context_limit)
        self.assertEqual(restored, [])
        self.assertEqual(model.requests, [])
        self.assertEqual(model.tool_sets, [])
        self.assertFalse(AgentRun.objects.active().filter(conversation=conversation).exists())
        self.assertIn("profile.max_input_tokens", guidance)
        self.assertNotIn("CONSUMER-PROMPT-SENTINEL", str(published) + str(summary))
        self.assertNotIn("PROVIDER-PRIVATE-SENTINEL", str(published) + str(summary) + reopened.content.decode())
        self.assertNotIn(DONE, [name for name, _payload in published])
        self.assertNotIn(TOKEN, [name for name, _payload in published])

    def test_cleanup_drains_keep_durable_answer_and_done_order(self):  # pylint: disable=too-many-locals
        """Observed pre-seal drains preserve accepted completion and canonical history."""
        for reason in ("cancelled", "soft_deadline"):
            with self.subTest(reason=reason):
                self._exercise_durable_cleanup_drain(reason)

    def _exercise_durable_cleanup_drain(self, reason):  # pylint: disable=too-many-locals
        answer = "The canonical answer survives cleanup cancellation."
        self.start_graph([AIMessage(content=answer)])
        run = self.submit("accepted request")
        claim = claimed_run(run.pk)
        execution = AgentExecution(claim, heartbeat_interval=0.02)
        cleanup_entered, release_cleanup, drain_observed = (threading.Event() for _ in range(3))
        done_statuses = []
        original_close = MCPTurn.close
        original_request_drain = RunWriteGuard.request_drain

        def hold_cleanup(turn, timeout=20):
            cleanup_entered.set()
            if not release_cleanup.wait(10):
                raise TimeoutError("MCP cleanup barrier was not released")
            return original_close(turn, timeout=timeout)

        def observe_drain(guard, drain_reason):
            original_request_drain(guard, drain_reason)
            if drain_reason == reason:
                drain_observed.set()

        def publish(_run_id, name, _payload):
            if name == DONE:
                done_statuses.append(AgentRun.objects.get(pk=run.pk).status)

        def execute_and_close():
            try:
                execution.run()
            finally:
                connections.close_all()

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with (
                override_agent_settings(tracing=True),
                patch.object(MCPTurn, "close", autospec=True, side_effect=hold_cleanup),
                patch.object(RunWriteGuard, "request_drain", autospec=True, side_effect=observe_drain),
                recorded_run_events(execution=execution, publish=publish),
            ):
                future = executor.submit(execute_and_close)
                self.assertTrue(cleanup_entered.wait(10))
                self.assertTrue(execution.done_pending)
                self.assertEqual(done_statuses, [])
                if reason == "cancelled":
                    self.assertIsNotNone(request_cancel(run.pk, self.user))
                else:
                    execution.started -= claim.soft_time_limit
                self.assertTrue(drain_observed.wait(5))
                release_cleanup.set()
                future.result(timeout=10)
        finally:
            release_cleanup.set()
            executor.shutdown(wait=True)

        run.refresh_from_db()
        conversation = AgentConversation.objects.get(pk=run.conversation_id)
        trace = AgentExecutionTrace.objects.get(pk=run.trace_id)
        messages = current_runtime().read_conversation_values(conversation)["messages"]

        self.assertEqual(run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(run.error_code, "")
        self.assertEqual(run.prompt, "")

        self.assertEqual(set(trace.spans.values_list("status", flat=True)), {ExecutionStatus.OK})
        self.assertEqual(messages[-1].content, answer)
        self.assertEqual(done_statuses, [AgentRunStatus.COMPLETED])

    def test_failures_keep_completed_checkpoint(self):
        original_bulk_create = QuerySet.bulk_create

        def fail_trace_persistence(queryset, *args, **kwargs):
            if queryset.model is AgentExecutionSpan:
                raise RuntimeError("diagnostic storage")
            return original_bulk_create(queryset, *args, **kwargs)

        def lose_delivery(_run_id, name, _payload):
            if name == CONTEXT:
                raise EventStreamError("redis unavailable")

        rows = (
            (
                "span persistence",
                "Exact durable answer survives trace persistence failure. " * 2048,
                True,
                True,
                TraceCaptureState.FAILED,
            ),
            ("live delivery", "The durable answer survives live delivery loss.", False, False, None),
        )
        for case, answer, tracing, fail_persistence, expected_capture in rows:
            with self.subTest(case=case):
                self.start_graph([AIMessage(content=answer)])
                run = self.submit("accepted request")
                with ExitStack() as stack:
                    if tracing:
                        stack.enter_context(override_agent_settings(tracing=True))
                    if fail_persistence:
                        stack.enter_context(
                            patch.object(
                                QuerySet,
                                "bulk_create",
                                autospec=True,
                                side_effect=fail_trace_persistence,
                            )
                        )
                    published = stack.enter_context(recorded_run_events(publish=lose_delivery if not tracing else None))
                    execute_run(str(run.pk))
                run.refresh_from_db()

                self.assertEqual(run.status, AgentRunStatus.COMPLETED)
                self.assertEqual(run.prompt, "")
                self.assertFalse(AgentRun.objects.active().filter(conversation=run.conversation).exists())
                self.assertEqual(
                    current_runtime().read_conversation_values(run.conversation)["messages"][-1].content, answer
                )
                if tracing:
                    self.assertEqual(
                        AgentExecutionTrace.objects.values_list("status", "capture_state").get(pk=run.trace_id),
                        (ExecutionStatus.OK, expected_capture),
                    )
                    self.assertIn(DONE, [name for name, _payload in published])
                else:
                    self.assertTrue(run.live_events_incomplete)
                    names = [name for name, _payload in published]
                    self.assertEqual(names[-1], CONTEXT)
                    self.assertEqual(names.count(CONTEXT), 1)
