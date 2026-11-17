"""Scope selection, classification, and admission routing through the real classifier and production graph."""

import json
from typing import Any, cast
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import reverse
from langchain_core.exceptions import ModelAuthenticationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from nautobot.apps.testing import TestCase

from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.models import AgentCheckpoint, AgentConversation, LLMConfiguration
from nautobot_agent.prompting.definitions import CLASSIFIER_PROMPT, TOPIC_SWITCH_CLASSIFIER_PROMPT
from nautobot_agent.runtime.agent import AgentRuntime, current_runtime
from nautobot_agent.runtime.checkpointer import DjangoCheckpointSaver, thread_config
from nautobot_agent.runtime.errors import ModelFailure
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE, ScopeDecision, classify_request, project_scope_history
from nautobot_agent.runtime.tracing import ExecutionTrace
from nautobot_agent.runtime.turn import AgentTurn
from nautobot_agent.tests.support.chat import (
    NORMALIZED_MODEL_FAILURES,
    ScriptedToolModel,
    client_with_session,
    create_configuration,
    create_conversation,
    enable_agent_settings,
    install_runtime,
    parse_run_events,
    post_run_message,
    run_dependencies,
    runtime_override,
    session_config,
    stale_conversation_references,
    use_runtime,
)
from nautobot_agent.tests.support.graph import DurableSourceGraphTestCase, SourceGraphTestCase

SECRET = "CLASSIFIER-SECRET-SENTINEL-6b1d"


class ScopeHistoryTests(SimpleTestCase):
    """Classifier context includes only bounded retained user and assistant text."""

    def test_scope_projection_budgets_and_metadata_exclusion(self):
        messages = [
            SystemMessage("system secret"),
            ToolMessage("tool secret", tool_call_id="private", additional_kwargs={"metadata": "secret"}),
            HumanMessage(" \n\t "),
            HumanMessage("user text"),
            AIMessage(
                content=[
                    {"type": "text", "text": "assistant text"},
                    {"type": "reasoning", "reasoning": "private reasoning"},
                ],
                tool_calls=[{"name": "private_tool", "args": {"secret": "value"}, "id": "private"}],
                additional_kwargs={"reasoning": "secret"},
                response_metadata={"usage": "secret"},
            ),
        ]
        self.assertEqual(
            project_scope_history(messages),
            [
                {"role": "user", "content": "user text", "truncated": False},
                {"role": "assistant", "content": "assistant text", "truncated": False},
            ],
        )

        messages = [
            SystemMessage("system secret"),
            ToolMessage("tool secret", tool_call_id="private"),
            HumanMessage("excluded-oldest"),
            HumanMessage(("A" * 1_500) + ("a" * 1_500)),
            AIMessage("B" * 1_500),
            HumanMessage("C" * 1_500),
            AIMessage("D" * 2_500),
        ]
        self.assertEqual(
            project_scope_history(messages),
            [
                {"role": "user", "content": "a" * 1_000, "truncated": True},
                {"role": "assistant", "content": "B" * 1_500, "truncated": False},
                {"role": "user", "content": "C" * 1_500, "truncated": False},
                {"role": "assistant", "content": "D" * 2_000, "truncated": True},
            ],
        )
        self.assertEqual(
            project_scope_history([HumanMessage(str(number)) for number in range(5)]),
            [{"role": "user", "content": str(number), "truncated": False} for number in range(1, 5)],
        )

        current = "current-" + ("X" * 64)
        classifier = Mock()
        classifier.invoke.return_value = ScopeDecision(allowed=True)
        classify_request(classifier, current, [HumanMessage("U" * 2_500)])
        self.assertTrue(classifier.invoke.call_args.args[0][-1].text.endswith(current))
        for topic_switch_only, history, expected_prompt in (
            (False, [HumanMessage("List devices")], CLASSIFIER_PROMPT),
            (True, [], CLASSIFIER_PROMPT),
            (True, [ToolMessage("Tool output", tool_call_id="tool")], CLASSIFIER_PROMPT),
            (True, [HumanMessage("List devices")], TOPIC_SWITCH_CLASSIFIER_PROMPT),
        ):
            classifier.invoke.reset_mock()
            classify_request(classifier, "Only active ones", history, topic_switch_only=topic_switch_only)
            self.assertEqual(classifier.invoke.call_args.args[0][0].text, expected_prompt)

        summary = [
            HumanMessage("List devices"),
            AIMessage("Three devices"),
            HumanMessage("Generated summary", additional_kwargs={"lc_source": "summarization"}),
        ]
        self.assertEqual(
            project_scope_history(summary),
            [
                {"role": "user", "content": "List devices", "truncated": False},
                {"role": "assistant", "content": "Three devices", "truncated": False},
            ],
        )


def decision(allowed):
    """Return a scripted model reply carrying LangChain's structured-output tool call for the decision."""
    call = {"name": "ScopeDecision", "args": {"allowed": allowed}, "id": "decision"}
    return AIMessage(content="", tool_calls=[call])


def classifier_context(request):
    """Decode historical data from a captured classifier request, without pinning label wording."""
    return json.loads(request[1].text.split("\n", 1)[1]) if len(request) == 3 else []


def raise_secret(_index):
    """Fail the way a provider error carrying private details would."""
    raise RuntimeError(SECRET)


class ScopeScreenRuntimeOwnershipTests(SimpleTestCase):
    """Screening reads validated history and classifies through the turn's injected runtime."""

    def setUp(self):
        super().setUp()
        self.conversation = AgentConversation(pk=912345)
        self.events = []
        self.classifications = []
        self.injected = AgentRuntime(saver=InMemorySaver())
        self.installed = AgentRuntime(saver=InMemorySaver())
        self.injected.classify_scope = self.classifier("injected", False)
        self.installed.classify_scope = self.classifier("installed", True)
        self.turn = AgentTurn(
            runtime=self.injected,
            user=None,
            conversation=self.conversation,
            prompt="Is this still about Nautobot?",
            guard=self.guard(),
            mcp_turn=cast(Any, None),
            trace=ExecutionTrace(),
        )

    def classifier(self, name, allowed):
        """Record classifier inputs and ordering without constructing a provider."""

        def classify(prompt, messages=None, *, topic_switch_only=False, config=None):
            self.events.append(("classify", name))
            self.classifications.append((prompt, messages, topic_switch_only, config))
            return ScopeDecision(allowed=allowed)

        return classify

    def guard(self):
        """Record screening admission and reject unexpected durable writes."""
        events = self.events

        class RecordingGuard:
            """Supply the turn's admission and durable-write interface."""

            def require_new_work(self):
                """Record the authority check without external state."""
                events.append(("authority", None))

            def write(self, _conversation_id):
                """Screening must not mutate the conversation."""
                raise AssertionError("Unexpected durable write during scope screening")

        return RecordingGuard()

    def retain(self, runtime, messages):
        """Populate one runtime's real model-free checkpoint reader."""
        runtime.state_reader.update_state(thread_config(self.conversation.pk), {"messages": messages})

    def test_screen_uses_injected_reader_classifier_and_retained_tail_anchor(self):
        injected_history = [
            HumanMessage(content="Show the devices", id="injected-human"),
            AIMessage(content="There are three devices", id="injected-tail"),
        ]
        installed_history = [
            HumanMessage(content="Explain a recipe", id="installed-human"),
            AIMessage(content="Add flour", id="installed-tail"),
        ]
        self.retain(self.injected, injected_history)
        self.retain(self.installed, installed_history)

        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {"scope_mode": "conversation"}}),
            install_runtime(self.installed),
        ):
            result = self.turn.screen()

        self.assertEqual(result.allowed, False)
        self.assertEqual(result.anchor_id, "injected-tail")
        prompt, messages, topic_switch_only, _config = self.classifications[0]
        self.assertEqual(prompt, self.turn.prompt)
        self.assertEqual(messages, injected_history)
        self.assertIs(topic_switch_only, True)
        self.assertEqual(self.events, [("authority", None), ("classify", "injected"), ("authority", None)])
        self.assertNotIn(("classify", "installed"), self.events)


class BindingFailureModel(ScriptedToolModel):  # pylint: disable=abstract-method
    """Scripted model whose structured-output schema binding fails."""

    def bind_tools(self, *_args, **_kwargs):
        """Fail before any invocation."""
        raise RuntimeError(SECRET)


class ScopeClassificationTests(TestCase):
    """The real classifier turns structured model output into a decision or one safe failure."""

    @classmethod
    def setUpTestData(cls):
        create_configuration()

    def setUp(self):
        super().setUp()
        use_runtime(self)
        enable_agent_settings(self)

    @staticmethod
    def classify(model, text="Show my devices", history=()):
        """Classify with the real function over an isolated scripted model."""
        with runtime_override(model_factory=lambda _configuration: model):
            return current_runtime().classify_scope(text, history)

    def test_classifier_failures_are_safe(self):
        wrong_arguments = AIMessage(
            content="", tool_calls=[{"name": "ScopeDecision", "args": {"allowed": SECRET}, "id": "bad"}]
        )
        rows = {
            "binding": (BindingFailureModel(responses=[decision(True)]), ()),
            "invocation": (ScriptedToolModel(responses=[decision(True)], on_call=raise_secret), ()),
            "parsing": (ScriptedToolModel(responses=[wrong_arguments]), ()),
            "wrong output": (ScriptedToolModel(responses=[AIMessage(content=f"allowed {SECRET}")]), ()),
        }
        messages = set()
        for label, (model, retained) in rows.items():
            with self.subTest(label):
                with (
                    self.assertLogs("nautobot_agent.scope", level="ERROR") as logs,
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    self.classify(model, history=retained)
                messages.add(str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(len(messages), 1)  # one static failure regardless of cause

        build_error = ImproperlyConfigured("The configuration could not be built.")
        with (
            runtime_override(model_factory=lambda _configuration: (_ for _ in ()).throw(build_error)),
            self.assertRaises(ImproperlyConfigured) as caught,
        ):
            current_runtime().classify_scope("Show my devices")
        self.assertIs(caught.exception, build_error)

        for error_type, code in NORMALIZED_MODEL_FAILURES:
            with self.subTest(code=code):
                error = error_type(SECRET)
                model = ScriptedToolModel(
                    responses=[decision(True)],
                    on_call=lambda *_args, error=error, **_kwargs: (_ for _ in ()).throw(error),
                )
                with (
                    self.assertLogs("nautobot_agent.scope", level="ERROR") as logs,
                    self.assertRaises(ModelFailure) as caught,
                ):
                    self.classify(model)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIn(SECRET, "\n".join(logs.output))

        LLMConfiguration.objects.all().delete()
        with (
            runtime_override(model_factory=lambda _configuration: self.fail("model factory called")),
            self.assertRaises(ImproperlyConfigured) as caught,
        ):
            current_runtime().classify_scope("Show my devices")
        self.assertNotIn(str(caught.exception), messages)


class ScopeFirstDenialTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Outermost durable admission commits before first-denial persistence without an Agent configuration."""

    agent_scope_mode = None

    def test_first_denial_needs_no_default_and_persists_a_conversation(self):
        create_configuration(name="classifier", is_default=False, is_scope_classifier=True)
        classifier = ScriptedToolModel(responses=[decision(False)])

        with runtime_override(model_factory=lambda _configuration: classifier):
            response = post_run_message(self.client, "What is the capital of France?")

        events = parse_run_events(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([event for event, _ in events], ["message_start", "token", "done"])
        self.assertEqual(events[1][1], {"text": REFUSAL_MESSAGE})
        conversation = AgentConversation.objects.get(pk=response.json()["conversation_id"])
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))


class ScopeSessionTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Durable admission requires outermost commits before classification and failed-send session recovery."""

    agent_scope_mode = None

    def setUp(self):
        super().setUp()
        use_runtime(self, saver=DjangoCheckpointSaver())

    def test_stale_sends_preserve_context(self):
        create_configuration()
        references = stale_conversation_references(self.user)

        for label, reference in references.items():
            with self.subTest("stale", reference=label):
                classifier = ScriptedToolModel(responses=[decision(False)])
                client = client_with_session(self, self.user, **reference)
                client.get(reverse("plugins:nautobot_agent:agent"), {"conversation": "new"})

                with (
                    runtime_override(
                        model_factory=lambda _configuration, classifier=classifier: classifier,
                        saver=self.runtime.saver,
                        sources=self.runtime.sources,
                    ),
                ):
                    response = post_run_message(client, "Weather?")

                events = parse_run_events(response)
                self.assertEqual(events[1][1], {"text": REFUSAL_MESSAGE}, events)
                self.assertEqual(len(classifier.requests), 1)
                refusal_conversation = AgentConversation.objects.get(pk=response.json()["conversation_id"])
                self.assertEqual(refusal_conversation.user, self.user)
                self.assertEqual(client.session[CONVERSATION_KEY], str(refusal_conversation.pk))

    def test_invalid_and_unconfigured_sends_preserve_context(self):
        # Only a classifier row remains, so classification can admit a request that then lacks an Agent default.
        LLMConfiguration.objects.all().delete()
        create_configuration(name="classifier", is_default=False, is_scope_classifier=True)
        existing = create_conversation(self.user)

        for selected in (False, True):
            values = {CONVERSATION_KEY: str(existing.pk)} if selected else {}
            with self.subTest("invalid", selected=selected):
                client = client_with_session(self, self.user, **values)
                response = client.post(
                    reverse("plugins:nautobot_agent:agent_run_submit"), data=b"{", content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
                self.assertFalse(AgentCheckpoint.objects.exists())
                if selected:
                    self.assertEqual(client.session.get(CONVERSATION_KEY), str(existing.pk))
                else:
                    self.assertNotIn(CONVERSATION_KEY, client.session)

            with self.subTest("unconfigured", selected=selected):
                client = client_with_session(self, self.user, **values)
                with run_dependencies():
                    response = post_run_message(client, "List devices")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "failed")
                self.assertTrue(any(event == "error" for event, _ in parse_run_events(response)))
                self.assertFalse(AgentCheckpoint.objects.exists())
                expected = (
                    existing if selected else AgentConversation.objects.get(pk=response.json()["conversation_id"])
                )
                self.assertEqual(client.session.get(CONVERSATION_KEY), str(expected.pk))


class ScopeRoutingTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Outermost durable admission commits before classifier/graph execution across session transitions."""

    agent_scope_mode = None

    def setUp(self):
        super().setUp()
        self.classifier = ScriptedToolModel(responses=[decision(True)])

    def start_graph(self, script, **kwargs):
        graph = super().start_graph(script, **kwargs)
        classifier_configuration = create_configuration(
            name="dedicated classifier", is_default=False, is_scope_classifier=True
        )
        graph_model = self.model
        use_runtime(
            self,
            model_factory=lambda configuration: (
                self.classifier if configuration.pk == classifier_configuration.pk else graph_model
            ),
        )
        return graph

    def post(self, text):
        """Submit and execute one durable run through the real classifier."""
        return post_run_message(self.client, text)

    def tokens(self, response):
        """Return the streamed token texts of an admitted or refused send."""
        self.assertEqual(response.status_code, 200)
        return [data["text"] for event, data in parse_run_events(response) if event == "token"]

    def config(self) -> RunnableConfig:
        """Return the session's graph configuration."""
        return session_config(self.client)

    def history(self, graph):
        """Return the Agent's checkpointed conversation as (type, text) pairs."""
        return [(message.type, message.text) for message in graph.get_state(self.config()).values["messages"]]

    def test_retained_context_follows_owned_conversations_across_sessions(self):
        self.start_graph(
            [
                AIMessage(content="A started"),
                AIMessage(content="A continued"),
                AIMessage(content="B followed"),
                AIMessage(content="Legacy followed"),
            ]
        )
        self.classifier.responses = [decision(True)] * 4
        conversation_a = create_conversation(self.user)
        conversation_b = create_conversation(self.user)
        legacy = create_conversation(self.user)
        first_session = client_with_session(self, self.user, **{CONVERSATION_KEY: str(conversation_a.pk)})
        resumed_session = client_with_session(self, self.user, **{CONVERSATION_KEY: str(conversation_b.pk)})
        legacy_session = client_with_session(self, self.user, **{CONVERSATION_KEY: str(legacy.pk)})
        self.add_permissions("nautobot_agent.view_agentconversation")

        def submit(client, text, expected):
            response = post_run_message(client, text)
            self.assertEqual(self.tokens(response), [expected])

        submit(first_session, "Continue A", "A started")
        resume_a = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": conversation_a.pk})
        resume_b = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": conversation_b.pk})
        self.assertEqual(resumed_session.post(resume_a, follow=True).status_code, 200)
        submit(resumed_session, "Continue A again", "A continued")
        self.assertEqual(resumed_session.post(resume_b, follow=True).status_code, 200)
        submit(resumed_session, "Continue B", "B followed")
        submit(legacy_session, "Start legacy follow-up", "Legacy followed")

        contexts = [classifier_context(request) for request in self.classifier.requests]
        self.assertEqual(contexts[0], [])
        self.assertEqual(
            [(entry["role"], entry["content"]) for entry in contexts[1]],
            [("user", "Continue A"), ("assistant", "A started")],
        )
        self.assertEqual(contexts[2], [])
        self.assertEqual(contexts[3], [])

        # New chat starts a fresh conversation while the old checkpoint stays intact.
        old_config = session_config(legacy_session)
        old_checkpoint = current_runtime().saver.get_tuple(old_config)
        legacy_session.get(reverse("plugins:nautobot_agent:agent"), {"conversation": "new"})
        self.assertNotIn(CONVERSATION_KEY, legacy_session.session)
        self.assertEqual(current_runtime().saver.get_tuple(old_config), old_checkpoint)

    def test_precheckpoint_failure_leaves_followup_without_scope_context(self):
        for mode in ("strict", "conversation"):
            with self.subTest(mode=mode), self.settings(PLUGINS_CONFIG={"nautobot_agent": {"scope_mode": mode}}):
                self.start_graph([AIMessage(content="Recovered answer")])
                self.classifier = ScriptedToolModel(responses=[decision(True), decision(True)])
                self.client.get(reverse("plugins:nautobot_agent:agent"), {"conversation": "new"})

                with patch(
                    "nautobot_agent.chat.execution.RunWriteGuard.store_display",
                    side_effect=RuntimeError("failure before input checkpoint"),
                ):
                    response = self.post("List Nautobot devices")

                self.assertEqual(response.json()["status"], "failed")
                conversation = AgentConversation.objects.get(pk=response.json()["conversation_id"])
                self.assertEqual(current_runtime().read_conversation_values(conversation)["messages"], [])
                self.assertEqual(self.tokens(self.post("Continue")), ["Recovered answer"])
                self.assertEqual([classifier_context(request) for request in self.classifier.requests], [[], []])
                self.assertEqual(
                    [message.text for message in current_runtime().read_conversation_values(conversation)["messages"]],
                    ["Continue", "Recovered answer"],
                )

    def test_allowed_denied_allowed_followup_admits_only_original_text_and_keeps_context(self):
        graph = self.start_graph([AIMessage(content="First answer"), AIMessage(content="Third answer")])
        self.classifier.responses = [decision(True), decision(False), decision(True)]

        self.assertEqual(self.tokens(self.post("List devices")), ["First answer"])
        conversation = AgentConversation.objects.get(pk=self.client.session[CONVERSATION_KEY])
        self.assertEqual(self.history(graph), [("human", "List devices"), ("ai", "First answer")])
        accepted_metadata = (
            conversation.title,
            conversation.created_at,
            conversation.last_activity_at,
        )
        checkpoint = current_runtime().saver.get_tuple(self.config())
        agent_calls = len(self.scripted_model.requests)

        refusal = self.post("What is the capital of France?")
        self.assertEqual(refusal.json()["conversation_id"], str(conversation.pk))
        self.assertEqual(self.tokens(refusal), [REFUSAL_MESSAGE])
        conversation.refresh_from_db()
        self.assertEqual(
            (
                conversation.title,
                conversation.created_at,
                conversation.last_activity_at,
            ),
            accepted_metadata,
        )
        self.assertEqual(current_runtime().saver.get_tuple(self.config()), checkpoint)
        self.assertEqual(len(self.scripted_model.requests), agent_calls)

        self.assertEqual(self.tokens(self.post("Only active ones")), ["Third answer"])
        self.assertEqual(
            self.history(graph),
            [
                ("human", "List devices"),
                ("ai", "First answer"),
                ("human", "Only active ones"),
                ("ai", "Third answer"),
            ],
        )

        requests = self.classifier.requests
        self.assertEqual(len(requests), 3)
        contexts = [classifier_context(request) for request in requests]
        self.assertEqual(contexts[0], [])
        expected_context = [
            {"role": "user", "content": "List devices", "truncated": False},
            {"role": "assistant", "content": "First answer", "truncated": False},
        ]
        self.assertEqual(contexts[1], expected_context)
        self.assertEqual(contexts[2], expected_context)
        self.assertTrue(requests[0][-1].text.endswith("List devices"))
        self.assertTrue(requests[1][-1].text.endswith("What is the capital of France?"))
        self.assertTrue(requests[2][-1].text.endswith("Only active ones"))
        for request in requests:
            text = "\n".join(message.text for message in request)
            for value in (self.client.session[CONVERSATION_KEY], str(self.user.pk)):
                self.assertNotIn(value, text)

        agent_text = self.scripted_model.request_text()
        for value in (REFUSAL_MESSAGE, "capital of France", requests[0][0].text):
            self.assertNotIn(value, agent_text)

    def test_classifier_failure_neither_calls_the_agent_nor_replaces_accepted_context(self):
        graph = self.start_graph([AIMessage(content="First answer"), AIMessage(content="Unexpected")])
        self.classifier.on_call = raise_secret

        failed = self.post("List devices")
        self.assertEqual((failed.status_code, failed.json()["status"]), (200, "failed"))
        self.assertNotIn(SECRET, failed.content.decode())
        self.assertEqual(self.scripted_model.requests, [])

        self.classifier.on_call = None
        self.assertEqual(self.tokens(self.post("List devices")), ["First answer"])
        checkpoint = current_runtime().saver.get_tuple(self.config())
        conversation = AgentConversation.objects.get(pk=self.client.session[CONVERSATION_KEY])
        metadata = (
            conversation.title,
            conversation.created_at,
            conversation.last_activity_at,
        )
        agent_calls = len(self.scripted_model.requests)

        self.classifier.on_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(ModelAuthenticationError(SECRET))
        failed = self.post("Only active ones")
        self.assertEqual(failed.json()["status"], "failed")
        self.assertNotIn(SECRET, failed.content.decode())
        conversation.refresh_from_db()
        self.assertEqual(
            (
                conversation.title,
                conversation.created_at,
                conversation.last_activity_at,
            ),
            metadata,
        )
        self.assertEqual(current_runtime().saver.get_tuple(self.config()), checkpoint)
        self.assertEqual(len(self.scripted_model.requests), agent_calls)
        self.assertEqual(self.history(graph), [("human", "List devices"), ("ai", "First answer")])


class ScopeContinuationTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Classifier continuation context comes from the two most recent persisted exchanges."""

    agent_scope_mode = None

    def test_continuation_uses_recent_persisted_exchanges(self):
        answers = [f"Answer {index}" for index in range(3)]
        requests = [f"Request {index}" for index in range(3)]
        classifier = ScriptedToolModel(responses=[decision(True)] * len(requests))
        graph = self.start_graph([AIMessage(content=answer) for answer in answers])
        classifier_configuration = create_configuration(
            name="dedicated classifier", is_default=False, is_scope_classifier=True
        )
        graph_model = self.model
        use_runtime(
            self,
            model_factory=lambda configuration: (
                classifier if configuration.pk == classifier_configuration.pk else graph_model
            ),
        )

        for text, answer in zip(requests, answers, strict=True):
            response = post_run_message(self.client, text)
            tokens = [data["text"] for event, data in parse_run_events(response) if event == "token"]
            self.assertEqual((response.status_code, tokens), (200, [answer]))

        self.assertEqual(classifier_context(classifier.requests[0]), [])
        self.assertEqual(
            [(entry["role"], entry["content"]) for entry in classifier_context(classifier.requests[-1])],
            [
                (role, content)
                for index in range(2)
                for role, content in (("user", requests[index]), ("assistant", answers[index]))
            ],
        )
        messages = graph.get_state(session_config(self.client)).values["messages"]
        self.assertEqual([message.text for message in messages if message.type == "human"], requests)
