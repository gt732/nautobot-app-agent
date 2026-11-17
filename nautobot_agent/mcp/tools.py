"""MCP catalog validation and run-owned HTTP/stdio tool dispatch."""

import logging
import re
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.runtime import get_runtime

from nautobot_agent.mcp.launch import Launch, MCPUnavailableError, text_tool_content
from nautobot_agent.mcp.turn import MCPDiscoveryResult, MCPTurn
from nautobot_agent.models import MCPServer

logger = logging.getLogger("nautobot_agent.mcp")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FAILED = "The MCP tool call failed or requested unsupported input."
_DISCOVERY_FAILED = "An enabled MCP server catalog is unavailable or invalid."


class MCPCatalogError(ImproperlyConfigured):
    """A sanitized genuine MCP catalog failure, distinct from turn-specific refusal."""


class _MCPToolFailure(ToolException):
    """A sanitized recoverable failure, distinct from the adapter's server-reported errors."""


@dataclass(frozen=True)
class UnavailableMCPServer:
    """Configured server identity safe to retain as an immutable display snapshot."""

    id: str
    name: str

    def as_dict(self) -> dict[str, str]:
        """Return only display identifiers for persistence and model context."""
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class MCPCatalogSnapshot:
    """Validated tools and unavailable configured servers from one catalog generation."""

    tools: tuple[BaseTool, ...]
    unavailable: tuple[UnavailableMCPServer, ...]


_EMPTY_CATALOG = MCPCatalogSnapshot((), ())


def _require_catalog_turn(turn: MCPTurn, *, admit: bool = True) -> None:
    """Check caller authority at boundaries, or only in-memory safety while waiting."""
    if admit:
        turn.check_admission()
    turn.require_open()


@dataclass
class _CatalogGeneration:
    """One snapshot generation and its current discovery attempt or published result."""

    key: tuple[Any, ...]
    event: Event | None = None
    snapshot: MCPCatalogSnapshot | None = None
    failure: str | None = None
    expires_at: float = 0.0


class MCPToolCache:  # pylint: disable=too-few-public-methods
    """Single-flight, runtime-local cache for validated run-neutral MCP tool wrappers."""

    FAILURE_TTL_SECONDS = 60.0
    SUCCESS_TTL_SECONDS = 300.0
    FOLLOWER_WAIT_SECONDS = 0.1

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        """Retain one current snapshot and an injectable monotonic expiry clock."""
        self._lock = Lock()
        self._generation: _CatalogGeneration | None = None
        self._clock = clock

    def get(self, key, rows, turn: MCPTurn) -> MCPCatalogSnapshot:
        """Return a complete or cooldown-cached partial catalog for captured server rows."""
        if not rows:
            return _EMPTY_CATALOG
        _require_catalog_turn(turn)

        generation: _CatalogGeneration | None = None
        authority_checked = True
        while True:
            generation, event, leader, failure = self._attempt(generation, key)

            if not leader and event is None:
                _require_catalog_turn(turn, admit=not authority_checked)
                if generation.snapshot is not None:
                    return generation.snapshot
                raise MCPCatalogError(failure)

            if not leader and event is not None:
                if event.wait(self.FOLLOWER_WAIT_SECONDS):
                    _require_catalog_turn(turn)
                    authority_checked = True
                    continue
                _require_catalog_turn(turn, admit=False)
                authority_checked = False
                continue

            return self._discover(generation, rows, turn, readmit=not authority_checked)

    def _attempt(self, generation, key):
        """Choose one generation attempt while holding only the cache's bookkeeping lock."""
        with self._lock:
            if generation is None:
                generation = self._generation
                if generation is None or generation.key != key:
                    generation = _CatalogGeneration(key)
                    self._generation = generation
            failure = generation.failure
            if generation.snapshot is not None and self._clock() < generation.expires_at:
                return generation, None, False, failure
            if generation.snapshot is not None and generation.event is not None:
                return generation, None, False, failure
            if failure is not None and self._clock() < generation.expires_at:
                return generation, None, False, failure
            if generation.event is None:
                generation.event = Event()
                return generation, generation.event, True, failure
            return generation, generation.event, False, failure

    def _discover(self, generation, rows, turn, *, readmit=False):
        """Publish one admitted candidate to its captured generation and always wake followers."""
        try:
            if readmit:
                _require_catalog_turn(turn)
            try:
                tools, unavailable, discovery_failed = _load_mcp_tools(rows, turn)
            except MCPCatalogError as failure:
                _require_catalog_turn(turn)
                with self._lock:
                    generation.snapshot = None
                    generation.failure = str(failure)
                    generation.expires_at = self._clock() + self.FAILURE_TTL_SECONDS
                raise
            _require_catalog_turn(turn)
            with self._lock:
                generation.snapshot = MCPCatalogSnapshot(tools, unavailable)
                generation.failure = None
                ttl = self.FAILURE_TTL_SECONDS if discovery_failed else self.SUCCESS_TTL_SECONDS
                generation.expires_at = self._clock() + ttl
            return generation.snapshot
        finally:
            with self._lock:
                event = generation.event
                generation.event = None
            if event is not None:
                event.set()


def _runtime_turn() -> MCPTurn:
    """Resolve the trusted per-send MCP session owner from the LangGraph runtime context."""
    try:
        context = get_runtime().context
    except RuntimeError:
        context = None
    turn = getattr(context, "mcp_turn", None)
    if not isinstance(turn, MCPTurn):
        raise MCPUnavailableError(_FAILED)
    return turn


@contextmanager
def _safe_boundary(turn: MCPTurn):
    """Expose only a static failure message; tool errors pass through untouched."""
    try:
        yield
    except ToolException:
        raise
    except Exception as exc:  # noqa: BLE001  # agent sees only a static failure; internals stay in logs
        logger.error("MCP tool call failed (%s)", type(exc).__name__)
        # Cancellation and unresolved remote effects close the turn and must still stop execution.
        turn.require_open()
        raise _MCPToolFailure(_FAILED) from None


def _bind(converted: StructuredTool, alias: str, launch: Launch, definition) -> None:
    """Route converted tools through the invoking run's turn."""
    adapter_error_handler = converted.handle_tool_error

    def handle_error(error: ToolException) -> Any:
        if isinstance(error, _MCPToolFailure):
            return str(error)
        if callable(adapter_error_handler):
            return text_tool_content(adapter_error_handler(error))
        raise error

    def call_sync(**arguments: Any) -> Any:
        turn = _runtime_turn()
        with _safe_boundary(turn):
            return turn.call_sync(alias, launch, definition, arguments)

    converted.coroutine, converted.func = None, call_sync
    converted.handle_tool_error = handle_error


def _load_mcp_tools(
    servers: Iterable[MCPServer], turn: MCPTurn
) -> tuple[tuple[BaseTool, ...], tuple[UnavailableMCPServer, ...], bool]:
    """Validate and discover catalog tools for an admitted cache-owned turn."""
    launches: dict[str, Launch] = {}
    unavailable_by_alias: dict[str, UnavailableMCPServer] = {}
    unavailable: list[UnavailableMCPServer] = []
    for server in servers:
        identity = UnavailableMCPServer(str(server.pk), cast(str, server.name))
        try:
            launch = Launch.from_server(server)
        except Exception as exc:  # noqa: BLE001  # one misconfigured server must not hide the others
            logger.error("MCP server configuration failed (id=%s, error=%s)", server.pk, type(exc).__name__)
            unavailable.append(identity)
            continue
        if launch.alias in launches:
            raise ImproperlyConfigured("The MCP server catalog contains duplicate internal aliases.")
        launches[launch.alias] = launch
        unavailable_by_alias[launch.alias] = identity

    try:
        result: MCPDiscoveryResult = turn.discover(launches) if launches else MCPDiscoveryResult((), ())
    except Exception as exc:  # noqa: BLE001  # discovery failure maps to a static catalog error
        logger.error("MCP discovery failed (%s)", type(exc).__name__)
        raise MCPCatalogError(_DISCOVERY_FAILED) from None
    tools = _validated_tools(result.entries, launches)
    unavailable.extend(unavailable_by_alias[alias] for alias in result.unavailable_aliases)
    return tuple(tools), tuple(unavailable), bool(result.unavailable_aliases)


def _validated_tools(discovered, launches) -> list[BaseTool]:
    """Bind converted definitions with portable tool names."""
    tools: list[BaseTool] = []
    for alias, definition, converted in discovered:
        final_name = converted.name
        if not _TOOL_NAME.fullmatch(final_name):
            logger.warning(
                "Skipping MCP tool with unsupported final name (alias=%r, tool=%r)",
                str(alias)[:64],
                final_name[:64],
            )
            continue
        _bind(converted, alias, launches[alias], definition)
        tools.append(converted)
    return tools
