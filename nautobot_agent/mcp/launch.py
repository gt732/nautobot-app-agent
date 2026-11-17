"""MCP launch description, operation-session policies, and converted-tool execution."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from django.core.exceptions import ImproperlyConfigured, ValidationError
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from langchain.mcp import as_langchain_tool
from langchain_core.tools import StructuredTool, ToolException
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, Tool

from nautobot_agent.models import MCPServer, MCPTransport

_CLIENT_TIMEOUT = 60
logger = logging.getLogger("nautobot_agent.mcp")


class MCPUnavailableError(RuntimeError):
    """An MCP turn, session, or tool conversion cannot serve the requested operation."""


async def discard_log(*args: Any, **kwargs: Any) -> None:
    """Discard server log payloads, which may contain deployment secrets."""


def _log_cleanup_error(exc: BaseException) -> None:
    logger.error("MCP session cleanup failed (%s)", type(exc).__name__)


def _report_close(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _log_cleanup_error(cast(BaseException, task.exception()))


class ClientSession:
    """Own one client's single public close attempt and its definitive outcome."""

    def __init__(self, client: Client) -> None:
        """Retain an unentered client without acquiring its resources."""
        self.client = client
        self._close_task: asyncio.Task[Any] | None = None

    async def enter(self) -> None:
        """Enter the client once."""
        await self.client.__aenter__()  # pylint: disable=unnecessary-dunder-call

    async def close(self, deadline: float) -> bool:
        """Start or observe one owned public close attempt, waiting no later than the deadline.

        A pending attempt is retained (never overlapped or cancelled) for later observation; a finished attempt's
        result or error is definitive. True confirms only local client closure, never that remote work stopped.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self.client.close())
            self._close_task.add_done_callback(_report_close)
        task = self._close_task
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        if not task.done() and remaining:
            await asyncio.wait((task,), timeout=remaining)
        return task.done() and not task.cancelled() and task.exception() is None


@dataclass
class Elicitation:
    """Track and decline input requests unsupported by chat turns."""

    requested: bool = False

    async def decline(self, *args: Any, **kwargs: Any) -> ElicitResult:
        """Record a remote input request and return a decline response."""
        self.requested = True
        return ElicitResult(action="decline")


class LaunchOperations(Protocol):
    """Resource-owning operations handle belonging to one runtime turn."""

    async def invoke(self, definition: Tool, arguments: dict[str, Any], timeout_seconds: float, on_unresolved):
        """Invoke within the budget; ``on_unresolved`` fires at the failure boundary, before any cleanup."""

    async def close(self, deadline: float) -> bool:
        """Close all owned resources and return confirmed cleanup evidence."""


@dataclass(frozen=True)
class Launch:  # pylint: disable=too-many-instance-attributes
    """Detached, validated connection values for one enabled MCP server.

    HTTP launches use a fresh client per call; stdio launches retain one serialized client per turn.
    """

    alias: str
    transport: str
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Detach launch collections from configuration owners."""
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @property
    def retained(self) -> bool:
        """Whether one serialized client is retained for the turn instead of a fresh client per call."""
        return self.transport == MCPTransport.STDIO

    @classmethod
    def from_server(cls, server: MCPServer) -> Launch:
        """Validate one enabled row and resolve its transport-specific deployment profile."""
        try:
            server.clean_fields()
            profile = server.validate_configuration()
        except (ValidationError, TypeError, ValueError):
            raise ImproperlyConfigured("An enabled MCP server has invalid configuration.") from None
        alias = server.pk.hex[:12]  # ty: ignore[unresolved-attribute] Nautobot UUIDField descriptor is typed as the field.
        transport = cast(str, server.transport)
        if transport == MCPTransport.HTTP:
            return cls(alias, transport, url=cast(str, server.url), headers=profile)
        definition = cast(dict, server.launch)
        env = dict(definition.get("env", {}))
        env.update(profile)
        return cls(
            alias,
            transport,
            command=definition["command"],
            args=definition.get("args", ()),
            cwd=definition.get("cwd") or None,
            env=env,
        )

    def client(self, elicitation_handler) -> Client:
        """Construct an unentered client; stdio subprocess stderr is discarded."""
        if self.retained:
            transport = StdioTransport(
                command=self.command,
                args=list(self.args),
                env=dict(self.env),
                cwd=self.cwd,
                keep_alive=False,
                log_file=Path(os.devnull),
            )
        else:
            transport = StreamableHttpTransport(url=self.url, headers=dict(self.headers))
        return Client(
            transport,
            elicitation_handler=elicitation_handler,
            log_handler=discard_log,
            cache=False,
            timeout=_CLIENT_TIMEOUT,
            init_timeout=_CLIENT_TIMEOUT,
        )

    def open_operations(self, require_open) -> LaunchOperations:
        """Allocate a resource-free per-turn operation handle for this transport's session policy."""
        policy = RetainedOperations if self.retained else FreshOperations
        return policy(self.client, require_open)


def text_tool_content(content):
    """Keep text and replace provider-dependent media blocks with text notices and references."""
    if not isinstance(content, list):
        return content
    return [
        {
            "type": "text",
            "text": f"[MCP {block['type']} content omitted]" + (f" {block['url']}" if block.get("url") else ""),
        }
        if isinstance(block, dict) and block.get("type") in {"image", "file"}
        else block
        for block in content
    ]


async def _converted_call(  # noqa: PLR0913  # one MCP call with its session callbacks
    client,
    definition,
    arguments,
    elicitation,
    on_unresolved,
    require_open,
):
    converted = cast(StructuredTool, await as_langchain_tool(definition, client))
    require_open()
    try:
        coroutine = cast(Callable[..., Awaitable[Any]], converted.coroutine)
        result = await coroutine(**arguments)
    except ToolException:
        if elicitation.requested:
            raise MCPUnavailableError("MCP elicitation is unsupported.") from None
        raise
    except NotImplementedError:
        raise MCPUnavailableError("MCP result content is unsupported.") from None
    except MCPError as exc:
        if exc.code in (REQUEST_TIMEOUT, CONNECTION_CLOSED):
            on_unresolved()
            raise
        raise MCPUnavailableError("The MCP server rejected the request.") from None
    except BaseException:
        # Invoked without a definitive result: report now, never after possibly blocked client cleanup.
        on_unresolved()
        raise
    if elicitation.requested:
        raise MCPUnavailableError("MCP elicitation is unsupported.")
    content, _artifact = result
    return text_tool_content(content), None


class FreshOperations:
    """Create, enter, invoke, and close a fresh client for each concurrent operation."""

    def __init__(self, client_factory, require_open):
        """Bind constructors without acquiring resources."""
        self._client_factory = client_factory
        self._require_open = require_open
        self._sessions: dict[int, ClientSession] = {}

    async def invoke(self, definition, arguments, timeout_seconds, on_unresolved):
        """Invoke on a fresh client, retaining provisional cleanup failures for shutdown."""
        self._require_open()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        elicitation = Elicitation()
        session = ClientSession(self._client_factory(elicitation.decline))
        self._sessions[id(session.client)] = session
        try:
            async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
                await session.enter()
                self._require_open()
                return await _converted_call(
                    session.client,
                    definition,
                    arguments,
                    elicitation,
                    on_unresolved,
                    self._require_open,
                )
        finally:
            try:
                cleanup_confirmed = await session.close(deadline)
            except BaseException as exc:  # noqa: BLE001  # cleanup failure, even cancellation, never replaces the operation outcome
                cleanup_confirmed = False
                _log_cleanup_error(exc)
            if cleanup_confirmed:
                self._sessions.pop(id(session.client), None)

    async def close(self, deadline):
        """Close all outstanding clients under a shared cleanup deadline."""
        sessions = tuple(self._sessions.items())
        results = await asyncio.gather(*(session.close(deadline) for _, session in sessions), return_exceptions=True)
        confirmed = True
        for (key, _), result in zip(sessions, results, strict=True):
            if result is True:
                self._sessions.pop(key, None)
            else:
                confirmed = False
                if isinstance(result, BaseException):
                    _log_cleanup_error(result)
        return confirmed and not self._sessions


class RetainedOperations:  # pylint: disable=too-many-instance-attributes
    """Lazily create one serialized client for the turn."""

    def __init__(self, client_factory, require_open):
        """Bind one lazy serialized session without acquiring resources."""
        self._client_factory = client_factory
        self._require_open = require_open
        self._lock = asyncio.Lock()
        self._session: ClientSession | None = None
        self._failed = False
        self._elicitation = Elicitation()

    async def invoke(self, definition, arguments, timeout_seconds, on_unresolved):
        """Serialize invocation and reject failed or closing sessions."""
        self._require_open()
        async with self._lock:
            self._require_open()
            if self._failed:
                raise MCPUnavailableError("MCP session is unavailable.")
            self._elicitation.requested = False
            try:
                async with asyncio.timeout(timeout_seconds):
                    if self._session is None:
                        session = ClientSession(self._client_factory(self._elicitation.decline))
                        self._session = session
                        await session.enter()
                    self._require_open()
                    return await _converted_call(
                        self._session.client,
                        definition,
                        arguments,
                        self._elicitation,
                        on_unresolved,
                        self._require_open,
                    )
            except (ToolException, MCPUnavailableError):
                raise
            except BaseException:
                self._failed = True
                raise

    async def close(self, deadline):
        """Close the retained session without discarding unconfirmed resources."""
        session = self._session
        if session is None:
            return True
        if not await session.close(deadline):
            return False
        self._session = None
        return True
