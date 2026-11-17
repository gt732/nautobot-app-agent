"""Run-owned MCP operations and bounded transport cleanup."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from asgiref.sync import async_to_sync, sync_to_async
from fastmcp import ClientGroup
from langchain.mcp import as_langchain_tool
from langchain_core.tools import BaseTool
from mcp.types import Tool

from nautobot_agent.mcp.launch import ClientSession, Elicitation, Launch, LaunchOperations, MCPUnavailableError

logger = logging.getLogger("nautobot_agent.mcp")


DISCOVERY_TIMEOUT_SECONDS = 10.0
OPERATION_TIMEOUT_SECONDS = 60.0
_TURN_UNAVAILABLE = "The MCP turn is no longer available."


@dataclass(frozen=True)
class MCPDiscoveryResult:
    """Immutable successful tool entries and unavailable configured server aliases."""

    entries: tuple[tuple[str, Tool, BaseTool], ...]
    unavailable_aliases: tuple[str, ...]


@dataclass(frozen=True)
class MCPCleanupResult:
    """Independent final-close facts; neither dimension ever substitutes for the other.

    ``cleanup_confirmed`` covers operation/execution cleanup only: not discovery client closure (reported through
    ``TurnHost.mark_mcp_cleanup_incomplete``) and not pending optional diagnostic handoffs. ``external_outcome_unknown``
    is monotonic: an invoked operation lacked a definitive result, whatever cleanup later achieved.
    """

    cleanup_confirmed: bool
    external_outcome_unknown: bool


class TurnHost(Protocol):
    """The run that owns an MCP turn: gates new work and receives lifecycle signals.

    Extends the ``runtime.context.RunAuthority`` contract (``require_new_work``) with two loop-thread evidence
    hooks that only latch facts, plus the synchronous persistence handoff run off the loop.
    """

    def require_new_work(self) -> None:
        """Raise when the run may no longer start sensitive work."""

    def mark_mcp_uncertain(self) -> None:
        """Record that an invoked remote operation may have taken effect without a definitive result."""

    def mark_mcp_cleanup_incomplete(self) -> None:
        """Record nonfatal discovery cleanup failure; it neither stops the turn nor implies remote effects."""

    def persist_diagnostics(self) -> None:
        """Synchronously persist latched evidence; runs on a detached executor thread, never the loop."""


class MCPTurn:  # pylint: disable=too-many-instance-attributes
    """Own transport-neutral discovery, operation handles, evidence, and shutdown."""

    def __init__(self, *, host: TurnHost) -> None:
        """Allocate bookkeeping without starting a thread or subprocess."""
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._cleanup: concurrent.futures.Future[bool] | None = None
        self._closing = False
        self._operations: dict[str, LaunchOperations] = {}
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._handoffs: set[asyncio.Task[Any]] = set()
        self._discovery: asyncio.Task[Any] | None = None
        self._unknown_external = False
        self._host = host

    def check_admission(self) -> None:
        """Recheck run authority at MCP catalog admission boundaries."""
        self._host.require_new_work()

    def discover(self, launches: dict[str, Launch]) -> MCPDiscoveryResult:
        """Discover independent server outcomes on the calling thread.

        Running here keeps turn-loop threads from ever using Django connections.
        """
        try:
            return async_to_sync(self._discover)(launches)
        except asyncio.CancelledError:
            raise MCPUnavailableError(_TURN_UNAVAILABLE) from None

    async def _discover(self, launches: dict[str, Launch]) -> MCPDiscoveryResult:
        """Discover each catalog through its own native, single-member FastMCP group."""
        with self._state_lock:
            if self._closing:
                raise MCPUnavailableError(_TURN_UNAVAILABLE)
            self._discovery = asyncio.current_task()
        sessions: dict[str, ClientSession] = {}
        tasks: dict[str, asyncio.Task[Any]] = {}
        try:
            tasks = {
                alias: asyncio.create_task(self._discover_one(alias, launch, sessions))
                for alias, launch in launches.items()
            }
            done, pending = await asyncio.wait(
                tasks.values(), timeout=DISCOVERY_TIMEOUT_SECONDS, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            finished = await asyncio.gather(*done, return_exceptions=True)
            abandoned = await asyncio.gather(*pending, return_exceptions=True)
            fatal = _fatal_discovery_failure(finished, abandoned)
            if fatal is not None:
                raise fatal
            # Abandoned servers that ignored cancellation and still returned stay unavailable.
            # Any finished exception was raised above; the filter only narrows the result type.
            outcomes = dict(result for result in finished if not isinstance(result, BaseException))
            return MCPDiscoveryResult(
                entries=tuple(entry for alias in launches for entry in (outcomes.get(alias) or ())),
                unavailable_aliases=tuple(alias for alias in launches if outcomes.get(alias) is None),
            )
        finally:
            with self._state_lock:
                self._discovery = None
            unsettled = {task for task in tasks.values() if not task.done()}
            for task in unsettled:
                task.cancel()
            await asyncio.gather(*unsettled, return_exceptions=True)
            await self._close_discovery(list(sessions.values()))

    async def _discover_one(
        self, alias: str, launch: Launch, sessions: dict[str, ClientSession]
    ) -> tuple[str, tuple[tuple[str, Tool, BaseTool], ...] | None]:
        """Isolate startup, listing, routing, and conversion failures to one server."""
        try:
            session = ClientSession(launch.client(Elicitation().decline))
            sessions[alias] = session
            await session.enter()
            group = ClientGroup({alias: session.client})
            public_tools = await group.list_tools()
            entries = []
            for public_tool in public_tools:
                route = await group.resolve_tool(public_tool.name)
                definition = public_tool.model_copy(update={"name": route.upstream_name})
                converted = await as_langchain_tool(public_tool, group)
                entries.append((route.server_name, definition, converted))
            return alias, tuple(entries)
        except Exception as exc:  # noqa: BLE001  # one server's failure only makes that server unavailable
            logger.error("MCP server discovery failed (alias=%s, error=%s)", alias, type(exc).__name__)
            return alias, None

    async def _close_discovery(self, sessions: list[ClientSession]) -> None:
        """Close discovery clients on their owning loop; failure is a diagnostic, never a catalog or turn failure."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DISCOVERY_TIMEOUT_SECONDS
        results = await asyncio.gather(*(session.close(deadline) for session in sessions))
        if not all(results):
            logger.error("MCP discovery cleanup exceeded its deadline")
            self._mark_discovery_cleanup_incomplete()
            await self._await_handoffs(deadline)

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _submit(self, coroutine):
        with self._state_lock:
            if self._closing:
                coroutine.close()
                raise MCPUnavailableError(_TURN_UNAVAILABLE)
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._run_loop, args=(self._loop,), name="nautobot-mcp-turn", daemon=True
                )
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            self._futures.add(future)
        # Outside the lock: an already-finished future runs the callback inline.
        future.add_done_callback(self._forget_future)
        return future

    def _forget_future(self, future) -> None:
        with self._state_lock:
            self._futures.discard(future)

    def call_sync(self, alias: str, launch: Launch, definition: Tool, arguments: dict[str, Any]) -> Any:
        """Perform one admitted operation through this turn's adapter-owned handle."""
        timeout = OPERATION_TIMEOUT_SECONDS
        return self._submit(self._call(alias, launch, definition, arguments, timeout)).result()

    def require_open(self) -> None:
        """Reject closed turns using only local evidence, without consulting the host."""
        with self._state_lock:
            closing = self._closing
        if closing:
            raise MCPUnavailableError(_TURN_UNAVAILABLE)

    def _operation_handle(self, alias: str, launch: Launch) -> LaunchOperations:
        handle = self._operations.get(alias)
        if handle is None:
            handle = launch.open_operations(self.require_open)
            self._operations[alias] = handle
        return handle

    def _mark_discovery_cleanup_incomplete(self) -> None:
        try:
            self._host.mark_mcp_cleanup_incomplete()
        except Exception as exc:  # noqa: BLE001  # host callback failure must not stop discovery cleanup
            logger.error("MCP discovery cleanup callback failed (%s)", type(exc).__name__)
        self._handoff_diagnostics()

    def _handoff_diagnostics(self) -> None:
        """Start one owned best-effort persistence of host evidence on a non-loop executor thread.

        A separate task, so caller cancellation of the failing operation never cancels it; shutdown waits for it.
        """
        try:
            task = asyncio.ensure_future(sync_to_async(self._host.persist_diagnostics, thread_sensitive=False)())
        except Exception as exc:  # noqa: BLE001  # best-effort diagnostics must not fail the reporting operation
            logger.warning("MCP diagnostic handoff failed (%s)", type(exc).__name__)
            return
        with self._state_lock:
            self._handoffs.add(task)
        task.add_done_callback(self._settle_handoff)

    def _settle_handoff(self, task: asyncio.Task[Any]) -> None:
        with self._state_lock:
            self._handoffs.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("MCP diagnostic handoff failed (%s)", type(task.exception()).__name__)

    async def _await_handoffs(self, deadline: float) -> None:
        """Let started handoffs reach their executor, then wait only within the remaining allowance."""
        await asyncio.sleep(0)
        with self._state_lock:
            handoffs = [task for task in self._handoffs if not task.done()]
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        if handoffs and remaining:
            await asyncio.wait(handoffs, timeout=remaining)

    def _stop_after_handoffs(self) -> None:
        """Keep the owned loop alive for already-submitted diagnostic writers only."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            pending = tuple(self._handoffs)
        if pending:
            settled = asyncio.gather(*pending, return_exceptions=True)
            settled.add_done_callback(lambda _result: loop.stop())
        else:
            loop.stop()

    async def _call(
        self, alias: str, launch: Launch, definition: Tool, arguments: dict[str, Any], timeout_seconds: float
    ):
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("No MCP operation task is available.")
        self._tasks.add(task)
        try:
            handle = self._operation_handle(alias, launch)
            return await handle.invoke(definition, arguments, timeout_seconds, self._mark_unknown)
        finally:
            self._tasks.discard(task)

    def _mark_unknown(self) -> None:
        notify = False
        with self._state_lock:
            if not self._unknown_external:
                self._unknown_external = True
                self._closing = True
                notify = True
        if notify:
            try:
                self._host.mark_mcp_uncertain()
            except Exception as exc:  # noqa: BLE001  # host callback failure must not mask the operation outcome
                logger.error("MCP uncertainty callback failed (%s)", type(exc).__name__)
            self._handoff_diagnostics()

    def cancel(self) -> None:
        """Close admission and signal active discovery and every submitted operation."""
        with self._state_lock:
            self._closing = True
            futures = tuple(self._futures)
            discovery = self._discovery
            self._discovery = None
            if discovery is not None:
                discovery.get_loop().call_soon_threadsafe(discovery.cancel)
        if threading.current_thread() is self._thread:
            current = asyncio.current_task()
            for task in tuple(self._tasks):
                if task is not current:
                    task.cancel()
            return
        for future in futures:
            future.cancel()

    async def _shutdown(self) -> bool:
        """Finish owned operations and clients, even after the caller's wait budget expires."""
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        results = await asyncio.gather(
            *(handle.close(float("inf")) for handle in self._operations.values()), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error("MCP operation cleanup failed (%s)", type(result).__name__)
        # Final shutdown evidence is returned; only discovery cleanup notifies the host.
        return all(result is True for result in results)

    def close(self, timeout: float = 20) -> MCPCleanupResult:
        """Bound local operation cleanup and report it separately from unknown external effects."""
        deadline = time.monotonic() + max(0, timeout)
        self.cancel()
        with self._state_lock:
            loop, thread = self._loop, self._thread
        cleanup_confirmed = True
        if loop is not None and thread is not None:
            with self._state_lock:
                if self._cleanup is None and not loop.is_closed():
                    self._cleanup = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                    self._cleanup.add_done_callback(
                        lambda _result: loop.call_soon_threadsafe(self._stop_after_handoffs)
                    )
                cleanup = self._cleanup
            if cleanup is not None:
                try:
                    cleanup_confirmed = bool(cleanup.result(timeout=max(0, deadline - time.monotonic())))
                except Exception as exc:  # noqa: BLE001  # close reports unconfirmed cleanup instead of raising
                    cleanup_confirmed = False
                    logger.error("MCP turn cleanup failed (%s)", type(exc).__name__)
            thread.join(timeout=max(0, deadline - time.monotonic()))
            # The owned shutdown stops the loop after cleanup and optional diagnostic writers finish.
        with self._state_lock:
            unknown = self._unknown_external
        return MCPCleanupResult(cleanup_confirmed=cleanup_confirmed, external_outcome_unknown=unknown)


def _fatal_discovery_failure(finished: list[Any], abandoned: list[Any]) -> BaseException | None:
    """Choose the failure that aborts discovery; per-server errors were already isolated as results.

    Cancellation of a finished server wins; abandoned servers count only when not cancelled by the timeout.
    """
    failures = [result for result in finished if isinstance(result, BaseException)]
    cancelled = [failure for failure in failures if isinstance(failure, asyncio.CancelledError)]
    late = [
        result
        for result in abandoned
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
    ]
    return next(iter(cancelled or failures or late), None)
