"""Scripted chat adapters, durable-run helpers, and view-response fixtures."""

# Reservation snapshots intentionally mirror production without exposing a policy bypass.
# pylint: disable=duplicate-code

import json
import re
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, suppress
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.db import router, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from langchain_core.exceptions import (
    ContextOverflowError,
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelRefusalError,
    ModelTimeoutError,
)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.language_models.model_profile import ModelProfile
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.runnables import RunnableConfig

from nautobot_agent import app_settings
from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.chat.events import RunEvents
from nautobot_agent.chat.execution import RunWriteGuard, execute_run
from nautobot_agent.chat.runs import RunClaim, claim_run
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import (
    AgentCheckpoint,
    AgentCheckpointWrite,
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    LLMConfiguration,
)
from nautobot_agent.runtime import agent
from nautobot_agent.runtime.agent import AgentRuntime, current_runtime
from nautobot_agent.runtime.checkpointer import attach_run_guard
from nautobot_agent.runtime.scope import ScopeDecision
from nautobot_agent.tests.support.mcp import Host

NORMALIZED_MODEL_FAILURES = (
    (ModelNotFoundError, "model_not_found"),
    (ModelAuthenticationError, "model_authentication_failed"),
    (ModelPermissionDeniedError, "model_permission_denied"),
    (ModelRateLimitError, "model_rate_limited"),
    (ModelConnectionError, "model_connection_failed"),
    (ModelTimeoutError, "model_timeout"),
    (ModelAPIError, "model_provider_unavailable"),
    (ModelInvalidRequestError, "model_request_invalid"),
    (ContextOverflowError, "model_context_exceeded"),
    (ModelRefusalError, "model_refused"),
)


@contextmanager
def recorded_run_events(*, execution=None, start_error=None, publish=None):
    """Inject a recording event sink without coupling tests to individual Redis methods."""
    recorded = []

    class Sink:
        """Record published events without contacting Redis."""

        def start(self, _run_id):
            """Allow event startup unless the test injected an error."""
            if start_error is not None:
                raise start_error

        def publish(self, run_id, name, payload):
            """Record one event and notify an optional observer."""
            recorded.append((name, payload))
            if publish is not None:
                publish(run_id, name, payload)

        def close(self):
            """The in-memory sink owns no resources."""

    sink = Sink()
    if execution is not None:
        execution.publisher = sink
    with patch("nautobot_agent.chat.events.RunEvents", return_value=sink):
        yield recorded


def provider_usage(input_tokens: int, output_tokens: int, total_tokens: int | None = None) -> UsageMetadata:
    """Return valid provider usage; the total is the sum unless a test needs an inconsistent one."""
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def display_id(message_id, category, local_id):
    """Independently encode the opaque message/category/local-ID display tuple for expectations."""
    return json.dumps([message_id, category, local_id], separators=(",", ":"))


def tool_entry(entry_id, name, args, status):
    """Return the exact restored-history shape of one tool call, keyed by opaque display ID."""
    return {"role": "tool", "id": entry_id, "name": name, "args": args, "status": status}


def client_with_session(case, user, /, **values):
    """Return a client of ``case`` logged in as ``user`` whose stored session already holds ``values``."""
    client = case.client_class()
    client.force_login(user)
    session = client.session
    session.update(values)
    session.save()
    return client


def create_storage_artifacts(conversation, *, label="cascade"):
    """Return payload-bearing trace/span and checkpoint/write rows for one conversation."""
    now = timezone.now()
    trace = AgentExecutionTrace.objects.create(conversation=conversation, request=label, status="ok", started_at=now)
    span = AgentExecutionSpan.objects.create(
        trace=trace,
        run_id=trace.pk,
        kind="chain",
        name=label,
        status="ok",
        started_at=now,
        ended_at=now,
        input_data={"payload": "input"},
        output_data={"payload": "output"},
        error_data={"payload": "error"},
        details={"payload": "details"},
        event_counts={"events": 1},
    )
    checkpoint = AgentCheckpoint.objects.create(
        conversation=conversation, checkpoint_id=label, checkpoint=b"checkpoint", metadata=b"metadata"
    )
    write = AgentCheckpointWrite.objects.create(
        checkpoint=checkpoint, task_id=label, idx=0, channel="messages", value_type="bytes", value=b"write"
    )
    return [trace, span, checkpoint, write]


def stale_conversation_references(user):
    """Return missing, malformed, and deleted selections without retaining the deleted conversation."""
    deleted = create_conversation(user)
    deleted_id = str(deleted.pk)
    deleted.delete()
    return {
        "missing": {},
        "non-string": {CONVERSATION_KEY: 12345},
        "malformed": {CONVERSATION_KEY: "not-a-uuid"},
        "deleted": {CONVERSATION_KEY: deleted_id},
    }


SOURCE_TOOLS = ["glob", "grep", "ls", "read_file"]
METADATA_TOOLS = ["describe_model", "get_nautobot_environment"]
QUERY_TOOLS = ["query_records", "trace_relationships"]
MAIN_TOOLS = sorted(
    [*SOURCE_TOOLS, *METADATA_TOOLS, *QUERY_TOOLS, "eval_python", "render_chart", "task", "write_todos"]
)
DELEGATE_TOOLS = sorted([*SOURCE_TOOLS, *METADATA_TOOLS, *QUERY_TOOLS])
FORBIDDEN_TOOLS = ["delete", "edit_file", "execute", "write_file"]
INTERNAL_SENTINEL = "INTERNAL-STATE-SENTINEL-2b8e"
_UNSET = object()


def tool_name(tool):
    """Return the name of a tool bound to a model, whether it is a tool object, callable, or schema dict."""
    if isinstance(tool, dict):
        return tool.get("name") or tool.get("function", {}).get("name")
    return getattr(tool, "name", None) or tool.__name__


def tool_call_message(name, args, call_id, text=""):
    """Return a scripted assistant message that calls one tool."""
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": call_id}])


# BaseChatModel documents _stream as optional; this fake uses its invoke-based streaming fallback.
class ScriptedToolModel(FakeMessagesListChatModel):  # pylint: disable=abstract-method
    """Scripted chat model that records the tools bound to, and the messages sent with, every call."""

    profile: ModelProfile | None = {"max_input_tokens": 128000}  # noqa: RUF012
    on_call: Callable[[int], None] | None = None
    pending_tools: list[str] | None = None
    # Pydantic copies mutable model-field defaults per instance; these must not become ClassVars.
    tool_sets: list = []  # noqa: RUF012
    requests: list = []  # noqa: RUF012

    def bind_tools(self, tools, **kwargs):
        """Remember the bound tool names for the call that is about to be generated; return the model unchanged."""
        self.pending_tools = [tool_name(tool) for tool in tools]
        return self

    def _generate(self, messages, *args, **kwargs):
        """Record this request, then report the index of the scripted response about to be produced."""
        self.tool_sets.append(self.pending_tools)
        self.requests.append(list(messages))
        self.pending_tools = None
        if self.on_call is not None:
            self.on_call(self.i)
        return super()._generate(messages, *args, **kwargs)

    async def _agenerate(self, messages, *args, **kwargs):
        """Use the same deterministic response and request capture during async graph execution."""
        return self._generate(messages, *args, **kwargs)

    def calls_with_tools(self, expected):
        """Return requests with the expected capabilities (None means the tool-free summarizer)."""
        return [
            request
            for tools, request in zip(self.tool_sets, self.requests, strict=True)
            if (tools is None and expected is None)
            or (
                tools is not None
                and expected is not None
                and set(expected) <= set(tools)
                and ("task" in tools) == ("task" in expected)
            )
        ]

    def request_text(self):
        """Return all message text sent to the model across every call."""
        return "\n".join(message.text for request in self.requests for message in request)


def model_message(node, *deltas, usage=None):
    """Return a native-shaped v3 message projection with finalized model usage."""
    return SimpleNamespace(node=node, text=list(deltas), output=SimpleNamespace(usage_metadata=usage))


class _FakeGraphRunStream:
    """Scripted stream implementing the public projection and abort contract used by chat streaming."""

    def __init__(self, graph_iter):
        self._graph_iter = graph_iter
        self._exhausted = False

    def interleave(self, *_names):
        """Yield scripted items and mark exhaustion after natural completion."""
        # Do not forward GeneratorExit: abort owns iterator closure.
        for item in self._graph_iter:  # noqa: UP028  # pylint: disable=use-yield-from
            yield item
        self._exhausted = True

    def abort(self):
        """Close an unexhausted iterator once, suppressing ordinary close failures like the native stream."""
        if self._exhausted:
            return
        self._exhausted = True
        close = getattr(self._graph_iter, "close", None)
        if close is not None:
            with suppress(Exception):
                close()


class FakeAgent:
    """Small in-memory agent fake for view behavior tests."""

    def __init__(self, messages=None, parts=None, todos=None):
        self.messages = messages or []
        self.parts = parts if parts is not None else [("messages", model_message("model", "Hello"))]
        self.todos = todos
        self.configs = []
        # Compiled graphs expose channels for checkpoint pruning; fakes declare no delta channels.
        self.channels = {}

    def get_state(self, config):
        """Record the config and return the stored messages and, when set, todos."""
        self.configs.append(config)
        values = {"messages": self.messages}
        if self.todos is not None:
            values["todos"] = self.todos
        return SimpleNamespace(values=values)

    def stream_events(self, _input, config, **_kwargs):
        """Record the config and return a native-shaped run stream."""
        self.configs.append(config)
        return _FakeGraphRunStream(iter(self.parts))


class BlockingAgent(FakeAgent):
    """Hold a scripted graph at entry while the real worker monitor remains active."""

    def __init__(self, entered, release):
        """Bind test-owned events with a bounded release wait."""
        super().__init__()
        self.entered = entered
        self.release = release

    def stream_events(self, _input, config, **kwargs):
        """Expose graph entry and wait for the test's explicit release."""
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("blocked graph was not released")
        return super().stream_events(_input, config, **kwargs)


def page_json(response, script_id):
    """Return the parsed JSON embedded by a json_script element."""
    match = re.search(
        rb'<script id="' + script_id.encode() + rb'" type="application/json">(.*?)</script>',
        response.content,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{script_id} JSON script was not rendered")
    return json.loads(match.group(1))


def parse_run_events(response):
    """Return event names and payloads from the durable events JSON response."""
    return [(entry["event"], entry["data"]) for entry in response.json()["events"]]


def enable_agent_settings(  # pylint: disable=too-many-arguments
    test_case,
    credentials=None,
    *,
    mcp_credentials=None,
    mcp_environments=None,
    source_apps_by_group=None,
    scope_mode=_UNSET,
):
    """Override app deployment settings with isolated credentials and explicit source grants."""
    if scope_mode is _UNSET:
        scope_mode = getattr(test_case, "agent_scope_mode", None)
    agent_config = {
        "credentials": credentials or {},
        "mcp_credentials": mcp_credentials or {},
        "mcp_environments": mcp_environments or {},
        "source_apps_by_group": source_apps_by_group if source_apps_by_group is not None else {},
        "tracing": False,
    }
    if scope_mode is not None:
        agent_config["scope_mode"] = scope_mode
    test_case.enterContext(
        test_case.settings(
            PLUGINS_CONFIG={
                **settings.PLUGINS_CONFIG,
                "nautobot_agent": agent_config,
            }
        )
    )


@contextmanager
def override_agent_settings(**values):
    """Override selected agent settings on top of the deterministic test baseline."""
    agent_config = dict(settings.PLUGINS_CONFIG["nautobot_agent"])
    agent_config.update(values)
    with override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "nautobot_agent": agent_config}):
        yield


def runtime_graph(user, *, mcp_turn=None):
    """Build a graph, closing any discovery turn owned by this helper."""
    turn = mcp_turn if mcp_turn is not None else MCPTurn(host=Host())
    try:
        return current_runtime().graph(user, mcp_turn=turn).graph
    finally:
        if mcp_turn is None:
            turn.close()


@contextmanager
def install_runtime(runtime):
    """Replace the process runtime for a test block, restoring even an uninitialized runtime."""
    with patch.object(agent, "_RUNTIME", runtime):
        yield runtime


@contextmanager
def runtime_override(**adapters):
    """Temporarily install a fresh runtime, inheriting unspecified adapters."""
    current = current_runtime()
    inherited: dict[str, Any] = {
        "saver": current.saver,
        "model_factory": current.model_factory,
        "sources": current.sources,
    }
    with install_runtime(AgentRuntime(**{**inherited, **adapters})) as runtime:
        yield runtime


def use_runtime(test_case, **adapters):
    """Install a fresh runtime for the test's lifetime."""
    return test_case.enterContext(runtime_override(**adapters))


def create_configuration(name="Test default", model="openai:gpt-test", **fields):
    """Persist one LLM configuration row (the default one unless ``is_default=False`` is passed)."""
    fields.setdefault("is_default", True)
    return LLMConfiguration.objects.create(name=name, model=model, **fields)


def create_conversation(user, **metadata):
    """Persist a real conversation row, optionally supplying scenario-specific runtime metadata."""
    now = timezone.now()
    metadata.setdefault("created_at", now)
    metadata.setdefault("last_activity_at", now)
    return AgentConversation.objects.create(user=user, **metadata)


def create_reserved_run(conversation, *, prompt, client_request_id=None):
    """Create a queued run fixture without exercising admission policy."""
    policy = app_settings.chat_run_settings()
    now = timezone.now()
    using = router.db_for_write(AgentRun, instance=conversation)
    with transaction.atomic(using=using):
        run = AgentRun.objects.using(using).create(
            user_id=conversation.user_id,
            conversation=conversation,
            client_request_id=client_request_id or uuid.uuid4(),
            prompt=prompt,
            status=AgentRunStatus.QUEUED,
            submitted_at=now,
            queue_expires_at=now + timedelta(seconds=policy.queue_wait_timeout),
            soft_time_limit=policy.soft_time_limit,
            cleanup_timeout=policy.cleanup_timeout,
        )
    return run


def create_refused_run(conversation, prompt, anchor_id=""):
    """Persist a display-only refusal on its originating run."""
    run = create_reserved_run(conversation, prompt=prompt)
    run.status = AgentRunStatus.REFUSED
    run.refusal_anchor_id = anchor_id
    run.save(update_fields=("status", "refusal_anchor_id"))
    return run


def claimed_run(run_id) -> RunClaim:
    """Claim a fixture run, failing immediately if its queued reservation is unavailable."""
    claim = claim_run(run_id)
    assert claim is not None, f"Could not claim fixture run {run_id}"
    return claim


class TestRunWriteGuard(RunWriteGuard):  # pylint: disable=too-few-public-methods
    """Provision a real claimed run for guarded checkpoint tests."""

    def __init__(self, conversation, *, on_drain=None):
        run = create_reserved_run(conversation, prompt="checkpoint test")
        super().__init__(claimed_run(run.pk), on_drain=on_drain)


def guarded_test_config(config, conversation):
    """Attach a real claimed run guard to a durable saver config."""
    return attach_run_guard(config, TestRunWriteGuard(conversation))


def session_config(client) -> RunnableConfig:
    """Return the graph configuration of the conversation a client's session currently selects."""
    return {"configurable": {"thread_id": client.session[CONVERSATION_KEY]}}


def select_conversation(client, conversation):
    """Point a client's session at an owned conversation, as a prior admitted send would have."""
    session = client.session
    session[CONVERSATION_KEY] = str(conversation.pk)
    session.save()


def delete_run_events(run_id):
    """Delete a test stream using its production key and a test-owned connection."""
    publisher = RunEvents()
    try:
        key = publisher._key(run_id)  # pylint: disable=protected-access
        cache.client.get_client(write=True).delete(key)
    finally:
        publisher.close()


def post_run_message(client, text):
    """Submit through durable HTTP admission, execute the claimed run, then poll its event page."""
    payload = {
        "message": text,
        "conversation_id": client.session.get(CONVERSATION_KEY),
        "client_request_id": str(uuid.uuid4()),
    }
    with patch("nautobot_agent.views.publish_run"):
        accepted = client.post(
            reverse("plugins:nautobot_agent:agent_run_submit"),
            data=json.dumps(payload),
            content_type="application/json",
        )
    if accepted.status_code != 202:
        return accepted
    summary = accepted.json()
    try:
        execute_run(summary["run_id"])
        return client.get(summary["events_url"], {"after": "0-0"})
    finally:
        delete_run_events(summary["run_id"])


def open_new_conversation(client):
    """Open a fresh tab using the explicit selector without stopping existing runs."""
    return client.get(reverse("plugins:nautobot_agent:agent"), {"conversation": "new"})


@contextmanager
def run_dependencies(*, graph_source=None, scope_classifier=None):
    """Script graph and scope outcomes on the installed runtime, keeping its adapters and retained reuse."""
    runtime = current_runtime()

    def classify_scope(text, history=None, *, topic_switch_only=False, config=None):
        if scope_classifier is None:
            return ScopeDecision(allowed=True)
        return scope_classifier(text, history, topic_switch_only=topic_switch_only, config=config)

    with ExitStack() as stack:
        stack.enter_context(patch.object(runtime, "classify_scope", classify_scope))
        if graph_source is not None:

            def graph(user, **_kwargs):
                """Return the scripted graph source for the requesting user."""
                return graph_source(user)

            stack.enter_context(patch.object(runtime, "graph", graph))
        yield
