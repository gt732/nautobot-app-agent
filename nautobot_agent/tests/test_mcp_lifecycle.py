"""MCP operation policy and bounded resource lifecycle regressions."""

import asyncio
import concurrent.futures
import threading
import time
from typing import Any, cast
from unittest.mock import patch

from django.test import SimpleTestCase
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult
from langchain_core.tools import ToolException
from mcp.types import AudioContent, Tool

from nautobot_agent.mcp import launch as mcp_launch
from nautobot_agent.mcp import turn as mcp_turn
from nautobot_agent.mcp.launch import ClientSession
from nautobot_agent.mcp.turn import MCPCleanupResult, MCPTurn
from nautobot_agent.tests.support.mcp import FakeLaunch, Host, InProcessLaunch

CONFIRMED = MCPCleanupResult(cleanup_confirmed=True, external_outcome_unknown=False)
UNCONFIRMED = MCPCleanupResult(cleanup_confirmed=False, external_outcome_unknown=False)
CONFIRMED_UNKNOWN = MCPCleanupResult(cleanup_confirmed=True, external_outcome_unknown=True)


def _without_running_loop():
    """Report whether the caller is outside any running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


class ClientSessionEvidenceTests(SimpleTestCase):
    """Exercise cleanup evidence through owner behavior and observable clients."""

    def test_pending_close_attempt_is_reused_across_retry(self):
        async def exercise():
            started = asyncio.Event()
            release = asyncio.Event()
            retry_started = asyncio.Event()

            class PendingCloseClient:  # pylint: disable=too-few-public-methods
                """Keep the first close alive after its waiter deadline expires."""

                def __init__(self):
                    self.active = 0
                    self.maximum = 0
                    self.attempts = 0

                async def close(self):
                    """Hold the owned close attempt until the test releases it."""
                    self.attempts += 1
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    started.set()
                    try:
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            await release.wait()
                    finally:
                        self.active -= 1

            client = PendingCloseClient()
            owner = ClientSession(cast(Client, client))
            retry = None
            try:
                self.assertFalse(await owner.close(asyncio.get_running_loop().time() + 0.01))
                await started.wait()

                async def retry_close():
                    retry_started.set()
                    return await owner.close(asyncio.get_running_loop().time() + 1)

                retry = asyncio.create_task(retry_close())
                await retry_started.wait()
                self.assertEqual(client.attempts, 1)
                self.assertEqual(client.maximum, 1)
                release.set()
                self.assertTrue(await retry)
            finally:
                release.set()
                if retry is not None:
                    await asyncio.gather(retry, return_exceptions=True)

        asyncio.run(exercise())

    def test_discovery_cleanup_failure_is_a_diagnostic_that_keeps_the_turn_usable(self):
        clients = []

        class PartialGroupClient:
            """Control entry and cleanup around FastMCP's native ClientGroup."""

            def __init__(self, *, fail_entry):
                self.fail_entry = fail_entry
                self.release = asyncio.Event()
                self.task = None
                clients.append(self)

            async def __aenter__(self):
                self.task = asyncio.create_task(self.release.wait())
                if self.fail_entry:
                    raise ConnectionError("original group entry failure")

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            def is_connected(self):
                """Report the successfully entered fixture as connected."""
                return not self.fail_entry

            async def list_tools(self, **_kwargs):
                """Expose a healthy empty catalog for the successful entry."""
                return []

            async def close(self):
                """Release the task but report unsuccessful cleanup."""
                self.release.set()
                raise OSError("cleanup failure")

        self.enterContext(patch.object(mcp_turn, "DISCOVERY_TIMEOUT_SECONDS", 0.05))
        signals = []
        persisted = threading.Event()

        def persist():
            signals.append(("persist", _without_running_loop()))
            persisted.set()

        turn = MCPTurn(
            host=Host(
                on_uncertain=lambda: signals.append(("uncertain", None)),
                on_cleanup_incomplete=lambda: signals.append(("cleanup", None)),
                on_persist=persist,
            )
        )
        self.addCleanup(turn.close)

        result = turn.discover(
            {
                "first": FakeLaunch(lambda _handler: PartialGroupClient(fail_entry=False)),
                "second": FakeLaunch(lambda _handler: PartialGroupClient(fail_entry=True)),
            }
        )
        self.assertEqual(result.unavailable_aliases, ("second",))
        self.assertEqual(result.entries, ())
        self.assertTrue(persisted.wait(5))
        self.assertEqual(signals, [("cleanup", None), ("persist", True)])
        turn.require_open()
        self.assertEqual(turn.close(), CONFIRMED)


class MCPLaunchSeamTests(SimpleTestCase):
    """Exercise MCPTurn's launch abstraction without a subprocess."""

    def test_shutdown_closes_servers_concurrently_and_reports_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                remote = FastMCP("concurrent-shutdown")
                remote.tool(name="ping")(lambda: "pong")
                started = []
                all_started = asyncio.Event()

                class GatedClient(Client):
                    """Wait for all server closes before completing SDK cleanup."""

                    fail_close = fail
                    close_starts = started
                    close_gate = all_started

                    async def close(self):
                        self.close_starts.append(self)
                        if len(self.close_starts) == 3:
                            self.close_gate.set()
                        await asyncio.wait_for(self.close_gate.wait(), timeout=2)
                        await super().close()
                        if self.fail_close and self is self.close_starts[0]:
                            raise OSError("cleanup failed")

                turn = MCPTurn(host=Host())
                self.addCleanup(turn.close)
                for alias in ("one", "two", "three"):
                    turn.call_sync(
                        alias,
                        FakeLaunch(
                            lambda handler, client_type=GatedClient, server=remote: client_type(
                                server, elicitation_handler=handler
                            ),
                            stdio=True,
                        ),
                        Tool(name="ping", input_schema={"type": "object", "properties": {}}),
                        {},
                    )
                self.assertEqual(turn.close(timeout=5), UNCONFIRMED if fail else CONFIRMED)
                self.assertEqual(len(set(started)), 3)

    def test_unconfirmed_close_eventually_releases_thread(self):
        for mode in ("blocked", "error"):
            with self.subTest(mode=mode):
                remote = FastMCP("late-thread-cleanup")
                remote.tool(name="ping")(lambda: "pong")
                release: concurrent.futures.Future[None] = concurrent.futures.Future()
                closed = threading.Event()
                owners = []

                class LateCloseClient(Client):
                    """Finish real SDK cleanup after a delay or with a reported error."""

                    close_mode = mode
                    close_release = release
                    closed_signal = closed

                    async def close(self):
                        if self.close_mode == "blocked":
                            await asyncio.wrap_future(self.close_release)
                        await super().close()
                        self.closed_signal.set()
                        if self.close_mode == "error":
                            raise OSError("cleanup failed")

                def client_factory(handler, record_owner=owners.append, server=remote, client_type=LateCloseClient):
                    record_owner((asyncio.get_running_loop(), threading.current_thread()))
                    return client_type(server, elicitation_handler=handler)

                turn = MCPTurn(host=Host())
                launch = FakeLaunch(client_factory, stdio=True)
                definition = Tool(name="ping", input_schema={"type": "object", "properties": {}})
                turn.call_sync("late-close", launch, definition, {})
                loop, thread = owners[0]
                try:
                    self.assertEqual(turn.close(timeout=0.05), UNCONFIRMED)
                    release.set_result(None)
                    self.assertTrue(closed.wait(5))
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                    self.assertTrue(loop.is_closed())
                finally:
                    if not release.done():
                        release.set_result(None)
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(loop.stop)
                    thread.join(timeout=5)

    def test_close_during_loop_start_stops_owned_thread(self):
        entered, release = threading.Event(), threading.Event()
        owners = []

        class DelayedTurn(MCPTurn):
            """Hold loop startup until shutdown has been requested."""

            def _run_loop(self, loop):
                owners.append((loop, threading.current_thread()))
                entered.set()
                release.wait(5)
                super()._run_loop(loop)

        remote = FastMCP("delayed-loop")
        remote.tool(name="ping")(lambda: "pong")
        turn = DelayedTurn(host=Host())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                turn.call_sync,
                "delayed",
                InProcessLaunch(remote),
                Tool(name="ping", input_schema={"type": "object", "properties": {}}),
                {},
            )
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(turn.close(timeout=0.05), UNCONFIRMED)
                release.set()
                with self.assertRaises(concurrent.futures.CancelledError):
                    pending.result(timeout=5)
                loop, thread = owners[0]
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(loop.is_closed())
            finally:
                release.set()
                if owners:
                    loop, thread = owners[0]
                    if not loop.is_closed():
                        loop.call_soon_threadsafe(loop.stop)
                    thread.join(timeout=5)

    def test_unsupported_audio_is_not_an_unknown_external_outcome(self):
        remote = FastMCP("audio-outcome")
        calls, signals = [], []

        @remote.tool
        def audio() -> ToolResult:
            """Return a definitive audio response after an observable effect."""
            calls.append("completed")
            return ToolResult(content=[AudioContent(type="audio", data="UklGRg==", mime_type="audio/wav")])

        @remote.tool
        def healthy() -> str:
            """Confirm the same server is still callable."""
            return "healthy result"

        for retained in (False, True):
            with self.subTest(retained=retained):
                turn = MCPTurn(host=Host(on_uncertain=lambda: signals.append("unknown")))
                launch = InProcessLaunch(remote, stdio=retained)
                try:
                    with self.assertRaises(mcp_launch.MCPUnavailableError):
                        turn.call_sync(
                            "audio",
                            launch,
                            Tool(name="audio", input_schema={"type": "object", "properties": {}}),
                            {},
                        )
                    turn.require_open()
                    self.assertEqual(signals, [])
                    self.assertIn(
                        "healthy result",
                        str(
                            turn.call_sync(
                                "audio",
                                launch,
                                Tool(name="healthy", input_schema={"type": "object", "properties": {}}),
                                {},
                            )
                        ),
                    )
                finally:
                    self.assertEqual(turn.close(), CONFIRMED)
        self.assertEqual(calls, ["completed", "completed"])

    def test_retained_policy_serializes_concurrent_calls_and_rechecks_admission_after_lock(self):
        async def exercise():
            entered = asyncio.Event()
            release = asyncio.Event()
            allowed = True
            calls = []
            remote = FastMCP("retained-serialization")

            @remote.tool
            async def execute() -> str:
                """Block first real invocation while another waits for the session lock."""
                calls.append("called")
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                return "done"

            def require_open():
                if not allowed:
                    raise RuntimeError("turn closed")

            policy = mcp_launch.RetainedOperations(
                lambda handler: Client(remote, elicitation_handler=handler), require_open
            )
            definition = Tool(name="execute", input_schema={"type": "object", "properties": {}})
            first = asyncio.create_task(policy.invoke(definition, {}, 5, lambda: None))
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = asyncio.create_task(policy.invoke(definition, {}, 5, lambda: None))
            await asyncio.sleep(0)
            allowed = False
            release.set()
            self.assertIn("done", str(await first))
            with self.assertRaisesRegex(RuntimeError, "turn closed"):
                await second
            self.assertEqual(calls, ["called"])
            self.assertTrue(await policy.close(asyncio.get_running_loop().time() + 5))

        asyncio.run(exercise())

    def test_queued_stdio_calls_each_get_a_full_operation_budget(self):
        remote = FastMCP("operation-budget")

        @remote.tool
        async def execute() -> str:
            """Take less than one operation budget after acquiring the retained session lock."""
            await asyncio.sleep(0.12)
            return "done"

        turn = MCPTurn(host=Host())
        launch = FakeLaunch(lambda handler: Client(remote, elicitation_handler=handler), stdio=True)
        definition = Tool(name="execute", input_schema={"type": "object", "properties": {}})
        with (
            patch.object(mcp_turn, "OPERATION_TIMEOUT_SECONDS", 0.2),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
        ):
            outcomes = list(executor.map(lambda _: turn.call_sync("budget", launch, definition, {}), range(2)))
        self.assertTrue(all("done" in str(outcome) for outcome in outcomes))
        self.assertEqual(turn.close(), CONFIRMED)

    def test_partial_client_entry_is_cleaned_up(self):
        class PartialClient:
            """Client that allocates before failing its context entry."""

            def __init__(self):
                self.closed = False

            async def __aenter__(self):
                raise ConnectionError("entry failed")

            async def close(self):
                """Confirm disposal of partially entered resources."""
                self.closed = True

        clients = []

        def factory(_handler):
            client = PartialClient()
            clients.append(client)
            return client

        launch = FakeLaunch(factory)
        turn = MCPTurn(host=Host())
        self.addCleanup(turn.close)
        definition = Tool(name="operation", input_schema={"type": "object", "properties": {}})
        with self.assertRaises(ConnectionError):
            turn.call_sync("partial", launch, definition, {})
        self.assertTrue(clients[0].closed)
        self.assertEqual(turn.close(), CONFIRMED)

    def test_admission_closed_during_conversion_prevents_remote_invocation(self):
        remote = FastMCP("conversion-admission")
        calls = []

        @remote.tool
        def execute() -> str:
            calls.append("invoked")
            return "done"

        original_conversion = mcp_launch.as_langchain_tool
        turn = MCPTurn(host=Host())
        self.addCleanup(turn.close)

        async def close_admission(definition, client):
            converted = await original_conversion(definition, client)
            turn.cancel()
            return converted

        with (
            patch.object(mcp_launch, "as_langchain_tool", side_effect=close_admission),
            self.assertRaisesRegex(RuntimeError, "no longer available"),
        ):
            turn.call_sync(
                "closing",
                InProcessLaunch(remote),
                Tool(name="execute", input_schema={"type": "object", "properties": {}}),
                {},
            )
        self.assertEqual(calls, [])
        self.assertEqual(turn.close(), CONFIRMED)

    def test_retained_entry_failure_poison_prevents_relaunch_without_remote_uncertainty(self):
        clients = []

        class FailedEntry:
            """Fail entry before any remote invocation."""

            async def __aenter__(self):
                raise ConnectionError("entry failed")

            async def close(self):
                """Confirm cleanup after partial entry."""
                return

        def create_client(_handler):
            client = FailedEntry()
            clients.append(client)
            return client

        turn = MCPTurn(host=Host())
        self.addCleanup(turn.close)
        launch = FakeLaunch(create_client, stdio=True)
        definition = Tool(name="operation", input_schema={"type": "object", "properties": {}})
        with self.assertRaisesRegex(ConnectionError, "entry failed"):
            turn.call_sync("poisoned", launch, definition, {})
        with self.assertRaisesRegex(RuntimeError, "session is unavailable"):
            turn.call_sync("poisoned", launch, definition, {})
        self.assertEqual(len(clients), 1)
        self.assertEqual(turn.close(), CONFIRMED)

    def test_fresh_cleanup_errors_preserve_result_and_report_unconfirmed_shutdown(self):  # pylint: disable=too-many-locals
        remote = FastMCP("fresh-cleanup-error")
        remote.tool(name="execute")(lambda: "operation result")

        @remote.tool
        def fail() -> str:
            """Raise a definitive operation error."""
            raise ValueError("operation failed")

        class RaisingCloseClient(Client):
            """Raise after the real client cleanup has run."""

            async def close(self):
                await super().close()
                raise OSError("cleanup failed")

        turn = MCPTurn(host=Host())
        self.addCleanup(turn.close)
        launch = FakeLaunch(lambda handler: RaisingCloseClient(remote, elicitation_handler=handler))
        definition = Tool(name="execute", input_schema={"type": "object", "properties": {}})
        error_definition = Tool(name="fail", input_schema={"type": "object", "properties": {}})
        with self.assertLogs(mcp_launch.logger, level="ERROR") as logs:
            self.assertIn("operation result", str(turn.call_sync("fresh-error", launch, definition, {})))
            with self.assertRaises(ToolException):
                turn.call_sync("fresh-error", launch, error_definition, {})
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(turn.close(), UNCONFIRMED)

        class RaisingSession:  # pylint: disable=too-few-public-methods
            """Raise from the session boundary during FreshOperations shutdown."""

            async def close(self, _deadline):
                """Reject shutdown cleanup."""
                raise OSError("shutdown cleanup failed")

        class ClosedSession:  # pylint: disable=too-few-public-methods
            """Confirm cleanup during FreshOperations shutdown."""

            async def close(self, _deadline):
                """Confirm shutdown cleanup."""
                return True

        operations = mcp_launch.FreshOperations(lambda _handler: None, lambda: None)
        sessions = cast(Any, operations._sessions)  # pylint: disable=protected-access
        sessions.update(raising=RaisingSession(), closed=ClosedSession())
        with self.assertLogs(mcp_launch.logger, level="ERROR") as logs:
            self.assertFalse(asyncio.run(operations.close(0)))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(list(sessions), ["raising"])

    def test_lost_response_is_reported_before_blocked_fresh_cleanup_returns(self):  # pylint: disable=too-many-locals
        remote = FastMCP("lost-response-blocked-close")
        calls = []

        @remote.tool
        def execute() -> str:
            calls.append("invoked")
            return "remote side effect completed"

        signalled, entered_close, release_close = threading.Event(), threading.Event(), threading.Event()
        persist_started, release_persist = threading.Event(), threading.Event()
        self.addCleanup(release_close.set)
        self.addCleanup(release_persist.set)

        class LostResponseBlockedClose(Client):
            """Lose the real response, then block the fresh client's cleanup."""

            async def call_tool(self, *args, **kwargs):
                await super().call_tool(*args, **kwargs)
                raise ConnectionError("response lost")

            async def close(self):
                entered_close.set()
                await asyncio.to_thread(release_close.wait, 5)
                await super().close()

        def persist():
            persist_started.set()
            release_persist.wait(5)

        turn = MCPTurn(host=Host(on_uncertain=signalled.set, on_persist=persist))
        self.addCleanup(turn.close)
        launch = FakeLaunch(lambda handler: LostResponseBlockedClose(remote, elicitation_handler=handler))
        definition = Tool(name="execute", input_schema={"type": "object", "properties": {}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(turn.call_sync, "lost-blocked", launch, definition, {})
            self.assertTrue(entered_close.wait(5))
            self.assertTrue(signalled.wait(5))
            self.assertFalse(pending.done())
            release_close.set()
            with self.assertRaisesRegex(ConnectionError, "response lost"):
                pending.result(timeout=5)
        self.assertEqual(calls, ["invoked"])
        self.assertTrue(persist_started.wait(5))
        self.assertEqual(turn.close(timeout=0.05), CONFIRMED_UNKNOWN)
        thread = turn._thread  # pylint: disable=protected-access
        assert thread is not None
        self.assertTrue(thread.is_alive())
        release_persist.set()
        self.assertEqual(turn.close(timeout=5), CONFIRMED_UNKNOWN)

    def test_close_rejects_unresolved_invocation_at_deadline_then_confirms_settled_result(self):
        remote = FastMCP("unresolved-invocation-deadline")
        entered = threading.Event()
        completed = threading.Event()
        release = threading.Event()
        uncertainty_signals = []

        @remote.tool
        def execute() -> str:
            """Return a real response before the client can record definitive evidence."""
            return "done"

        def hold_response(client):
            call_tool = client.call_tool

            async def call(*args, **kwargs):
                result = await call_tool(*args, **kwargs)
                entered.set()
                while not release.is_set():
                    try:
                        await asyncio.to_thread(release.wait)
                    except asyncio.CancelledError:
                        continue
                completed.set()
                return result

            client.call_tool = call

        turn = MCPTurn(host=Host(on_uncertain=lambda: uncertainty_signals.append(True)))
        launch = InProcessLaunch(remote, stdio=True, on_client=hold_response)
        definition = Tool(name="execute", input_schema={"type": "object", "properties": {}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(turn.call_sync, "unresolved", launch, definition, {})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(turn.close(timeout=0.05), UNCONFIRMED)
                self.assertEqual(uncertainty_signals, [])
            finally:
                release.set()
            self.assertTrue(completed.wait(2))
        self.assertEqual(turn.close(timeout=2), CONFIRMED)

    def test_close_obeys_one_deadline_and_does_not_confirm_blocked_client_cleanup(self):
        release: concurrent.futures.Future[None] = concurrent.futures.Future()
        entered_close = threading.Event()

        remote = FastMCP("bounded-cleanup")
        remote.tool(name="ping")(lambda: "pong")

        class BlockingClient(Client):
            """Keep one owned close attempt alive until explicitly released."""

            closed = False

            async def close(self):
                """Model a client whose cleanup cannot finish merely by cancellation."""
                entered_close.set()
                try:
                    await asyncio.shield(asyncio.wrap_future(release))
                except asyncio.CancelledError:
                    await asyncio.wrap_future(release)
                self.closed = True
                await super().close()

        client = BlockingClient(remote)
        turn = MCPTurn(host=Host())
        self.addCleanup(turn.close)
        launch = FakeLaunch(lambda _handler: client, stdio=True)
        definition = Tool(name="ping", input_schema={"type": "object", "properties": {}})
        self.assertIn("pong", str(turn.call_sync("bounded", launch, definition, {})))
        try:
            started = time.monotonic()
            cleanup = turn.close(timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(cleanup, UNCONFIRMED)
            self.assertTrue(entered_close.is_set())
            self.assertFalse(client.closed)
        finally:
            release.set_result(None)
            cleanup = turn.close(timeout=1)
        self.assertEqual(cleanup, CONFIRMED)
        self.assertTrue(client.closed)
