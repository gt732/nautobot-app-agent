"""In-process MCP launches and client-level fault injection for tests."""

import asyncio
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastmcp import Client

from nautobot_agent.mcp.launch import Launch

_FROM_SERVER = Launch.from_server


@contextmanager
def patched_launches(factory):
    """Replace validated launches while retaining production server identities."""

    def from_server(server):
        specification = _FROM_SERVER(server)
        launch = factory(specification)
        object.__setattr__(launch, "alias", specification.alias)
        return launch

    with patch.object(Launch, "from_server", side_effect=from_server):
        yield


class Host:
    """Recording MCP turn host; optional ``require_new_work`` simulates authority checks."""

    def __init__(self, *, require_new_work=None, on_uncertain=None, on_cleanup_incomplete=None, on_persist=None):
        self._require_new_work = require_new_work
        self._on_uncertain = on_uncertain
        self._on_cleanup_incomplete = on_cleanup_incomplete
        self._on_persist = on_persist

    def require_new_work(self):
        """Delegate to the supplied authority check."""
        if self._require_new_work is not None:
            self._require_new_work()

    def mark_mcp_uncertain(self):
        """Forward the uncertainty signal."""
        if self._on_uncertain is not None:
            self._on_uncertain()

    def mark_mcp_cleanup_incomplete(self):
        """Forward the discovery cleanup diagnostic."""
        if self._on_cleanup_incomplete is not None:
            self._on_cleanup_incomplete()

    def persist_diagnostics(self):
        """Forward the off-loop diagnostic persistence handoff."""
        if self._on_persist is not None:
            self._on_persist()


class ResponseLossClient(Client):
    """Inject a lost response after a successful real client call."""

    def __init__(self, *args, response_loss, **kwargs):
        super().__init__(*args, **kwargs)
        self._response_loss = response_loss

    async def call_tool(self, *args, **kwargs):
        """Run the SDK call before injecting loss, preserving error results."""
        result = await super().call_tool(*args, **kwargs)
        if not result.is_error:
            self._response_loss()
        return result


class UnavailableDiscoveryClient(Client):
    """Fail foreign catalog listing while retaining real SDK entry and cleanup."""

    async def list_tools(self, *args, **kwargs):
        """Reject discovery without exposing transport details."""
        raise ConnectionError("private discovery failure")


class CancellationAwareDiscoveryClient(Client):
    """Pause discovery at list-tools while remaining cancellable by the owning MCP turn."""

    def __init__(self, *args, entered, release, **kwargs):
        super().__init__(*args, **kwargs)
        self._entered = entered
        self._release = release

    async def list_tools(self, *args, **kwargs):
        """Wait cooperatively until released, then perform genuine discovery."""
        self._entered.set()
        while not await asyncio.to_thread(self._release.wait, 0.1):
            pass
        return await super().list_tools(*args, **kwargs)


class CancellationAwareDiscoveryLaunch(Launch):
    """Launch a real in-process client with controllably cancellable discovery."""

    server: Any
    entered: Any
    release: Any

    def __init__(self, server, *, entered, release):
        super().__init__(alias="in-process", transport="http")
        object.__setattr__(self, "server", server)
        object.__setattr__(self, "entered", entered)
        object.__setattr__(self, "release", release)

    def client(self, elicitation_handler):
        """Construct the cancellation-aware SDK client."""
        return CancellationAwareDiscoveryClient(
            self.server, elicitation_handler=elicitation_handler, entered=self.entered, release=self.release
        )


class InProcessLaunch(Launch):
    """Test launch using a real FastMCP client and production session policy."""

    server: Any
    response_loss: Any
    mode: Any
    on_client: Any

    def __init__(self, server, *, stdio=False, response_loss=None, mode=None, on_client=None):
        super().__init__(alias="in-process", transport="stdio" if stdio else "http")
        for name, value in (
            ("server", server),
            ("response_loss", response_loss),
            ("mode", mode),
            ("on_client", on_client),
        ):
            object.__setattr__(self, name, value)

    def client(self, elicitation_handler):
        """Construct a real SDK client against the in-memory server."""
        kwargs: dict[str, Any] = {"elicitation_handler": elicitation_handler}
        if self.mode is not None:
            kwargs["mode"] = self.mode
        if self.response_loss is not None:
            client = ResponseLossClient(self.server, response_loss=self.response_loss, **kwargs)
        else:
            client = Client(self.server, **kwargs)
        if self.on_client is not None:
            self.on_client(client)
        return client


class FakeLaunch(InProcessLaunch):
    """Inject controlled clients while retaining production session policy."""

    def __init__(self, client_factory, *, stdio=False):
        super().__init__(None, stdio=stdio)
        self.client_factory = client_factory

    def client(self, elicitation_handler):
        """Construct a client through the supplied fault-injection factory."""
        return self.client_factory(elicitation_handler)
