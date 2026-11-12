"""Production graph fixture hierarchy for tests."""

from typing import Any, ClassVar, cast

from django.test import Client, TestCase
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from nautobot.apps.testing import NautobotTestCaseMixin

from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.models import LLMConfiguration, Skill
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.checkpointer import thread_config
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import (
    FORBIDDEN_TOOLS,
    INTERNAL_SENTINEL,
    SOURCE_TOOLS,
    ScriptedToolModel,
    create_configuration,
    create_conversation,
    enable_agent_settings,
    parse_run_events,
    post_run_message,
    run_dependencies,
    runtime_graph,
    session_config,
    use_runtime,
)
from nautobot_agent.tests.support.db import CommittedTestCase
from nautobot_agent.tests.support.sources import (
    OUTSIDE_SENTINELS,
    create_source_fixture,
    fixture_catalog,
)

_UNSET = object()


class _GraphFixture:
    """Production-assembled graph fixture over a temporary source package (mixin; combine with a TestCase base).

    By default checkpoints use an isolated in-memory saver; ``durable_checkpoints`` retains the production
    Django saver for persistence-specific coverage. Nautobot's mixin supplies the principal, client,
    permissions and cache cleanup without populating unrelated statuses.
    """

    client: Any  # Django's installed Client.post stub incorrectly declares WSGIRequest.
    user: Any
    setUpNautobot: Any
    assertEqual: Any
    assertIn: Any
    assertNotIn: Any
    durable_checkpoints: ClassVar[bool] = False
    agent_scope_mode: ClassVar[str | None] = "off"

    def setUp(self):  # pylint: disable=invalid-name
        """Prepare the Nautobot-backed test fixture and isolated runtime."""
        cast(Any, super()).setUp()  # pylint: disable=no-member
        self.setUpNautobot()
        Skill.objects.all().delete()
        self.saver = InMemorySaver() if not self.durable_checkpoints else None
        self.runtime = use_runtime(self, saver=self.saver)
        enable_agent_settings(self)
        self.tree = create_source_fixture(self)
        self.location_token = self.tree.base.name
        self.model: ScriptedToolModel | None = None

    def start_graph(  # pylint: disable=too-many-arguments
        self,
        script,
        *,
        source_available=True,
        app_source_backends=None,
        mcp_turn=None,
    ):
        """Build the real graph over controlled core and optional explicitly granted App adapters."""
        self.model = ScriptedToolModel(responses=script)
        catalog = fixture_catalog(self, self.tree, source_available=source_available, app_sources=app_source_backends)
        self.runtime = use_runtime(
            self,
            model_factory=lambda _configuration: self.model,
            sources=catalog,
        )
        LLMConfiguration.objects.all().delete()
        create_configuration()
        return runtime_graph(self.user, mcp_turn=mcp_turn)

    def invoke(self, graph, text="Question", *, files=None, context=_UNSET):
        """Run one turn on a fresh owned conversation, with optional trusted run context; return state and config."""
        config = thread_config(create_conversation(self.user).pk)
        payload = {"messages": [{"role": "user", "content": text}]}
        if context is _UNSET:
            context = AgentContext(
                user_id=str(self.user.pk),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            )
        if files:
            payload["files"] = files
        return graph.invoke(payload, config, context=context, durability="sync"), config

    @staticmethod
    def tool_results(messages):
        """Index the tool results in a message list by tool call id."""
        return {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}

    @property
    def scripted_model(self) -> ScriptedToolModel:
        """Return the scripted model after graph initialization."""
        assert self.model is not None
        return self.model

    def assert_denied(self, result, *, expect_permission=True):
        """Assert a tool result is an error, and (by default) specifically a permission denial."""
        self.assertEqual(result.status, "error", result.text)
        if expect_permission:
            self.assertIn("permission denied", result.text)

    def assert_tool_surface(self):
        """Assert the filesystem subset is exactly four source tools, with no mutation or shell tools."""
        for tools in self.scripted_model.tool_sets:
            if tools is not None:
                self.assertEqual(set(tools) & set([*SOURCE_TOOLS, *FORBIDDEN_TOOLS]), set(SOURCE_TOOLS))

    def saved_messages(self, graph, client: Client | None = None) -> list[BaseMessage]:
        """Return a client's (default: this test's) checkpointed root messages."""
        return graph.get_state(session_config(client or self.client)).values["messages"]

    def assert_nothing_outside_reached_the_model(self, *, sentinels=OUTSIDE_SENTINELS, allow_location=False):
        """Assert no ``sentinels``, internal, or (by default) physical-location detail reached the model."""
        text = self.scripted_model.request_text()
        for sentinel in (*sentinels, INTERNAL_SENTINEL):
            self.assertNotIn(sentinel, text)
        if not allow_location:
            self.assertNotIn(self.location_token, text)


class GraphTestCase(_GraphFixture, NautobotTestCaseMixin, TestCase):
    """Fast transactional-rollback base for invoke-only graph tests (no durable HTTP admission)."""


class SourceGraphTestCase(_GraphFixture, NautobotTestCaseMixin, CommittedTestCase):
    """Committed base: durable admission owns its outermost transaction, so ``send_*`` run through it."""

    def send_response(self, text, client=None):
        """POST through durable admission, execute the run, and read its real event page."""
        client = client or self.client
        with run_dependencies():
            response = post_run_message(client, text)
        self.assertEqual(response.status_code, 200)
        self.assertIn(CONVERSATION_KEY, client.session)
        return response

    def send_message(self, text, client=None):
        """Return streamed message texts followed by the durable run's terminal event."""
        response = self.send_response(text, client)
        texts = []
        for event, data in parse_run_events(response):
            if event == "message_start":
                texts.append("")
            elif event == "token":
                texts[-1] += data["text"]
            elif event not in ("todos", "tool_call", "context"):
                texts.append(event)
        return texts


class DurableSourceGraphTestCase(SourceGraphTestCase):
    """Transactional base for end-to-end persistence regressions only; it keeps the production Django saver."""

    durable_checkpoints = True
