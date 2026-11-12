"""Behavioral regression tests for persisted, display-only scope refusals."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.urls import reverse
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage, ToolMessage

from nautobot_agent.chat import conversation
from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.chat.runs import finish_run
from nautobot_agent.models import (
    AgentCheckpoint,
    AgentConversation,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
)
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.checkpointer import DjangoCheckpointSaver, attach_run_guard
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE, ScopeDecision
from nautobot_agent.tests.support.chat import (
    TestRunWriteGuard,
    client_with_session,
    create_configuration,
    page_json,
    parse_run_events,
    post_run_message,
    run_dependencies,
    session_config,
    use_runtime,
)
from nautobot_agent.tests.support.data import grant
from nautobot_agent.tests.support.graph import DurableSourceGraphTestCase

User = get_user_model()


def _unexpected_graph(_user):
    raise AssertionError("Main agent invoked")


class RefusalHistoryTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Real admission commits before refusal execution, persistence, and owner-checked restoration."""

    agent_scope_mode = None

    def setUp(self):
        super().setUp()
        use_runtime(self, saver=DjangoCheckpointSaver())
        self.enterContext(self.settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": False}}))
        grant(self.user, AgentConversation, actions=("view",), constraints={"user": "$user"})

    def refuse(self, client, message):
        """Submit a durable refusal without allowing main-agent construction."""
        with run_dependencies(
            graph_source=_unexpected_graph,
            scope_classifier=lambda *_args, **_kwargs: ScopeDecision(allowed=False),
        ):
            return post_run_message(client, message)

    def test_repeated_refusals_persist_without_tracing_or_accepted_context(self):
        first = self.refuse(self.client, "unrelated first request")
        selected = AgentConversation.objects.get(pk=first.json()["conversation_id"])
        metadata = (selected.title, selected.created_at, selected.last_activity_at)
        observed = []

        def classify(_text, classifier_history, **_kwargs):
            observed.extend(classifier_history)
            return ScopeDecision(allowed=False)

        with run_dependencies(graph_source=_unexpected_graph, scope_classifier=classify):
            second = post_run_message(self.client, "unrelated second request")
        selected.refresh_from_db()
        self.assertEqual(
            (first.json()["conversation_id"], second.json()["conversation_id"]), (str(selected.pk), str(selected.pk))
        )
        self.assertEqual((selected.title, selected.created_at, selected.last_activity_at), metadata)
        self.assertEqual(
            list(
                selected.runs.filter(refusal_anchor_id__isnull=False)
                .order_by("submitted_at")
                .values_list("prompt", flat=True)
            ),
            ["unrelated first request", "unrelated second request"],
        )
        self.assertEqual(observed, [])
        self.assertFalse(AgentCheckpoint.objects.exists())
        self.assertFalse(AgentExecutionTrace.objects.exists())
        selected.delete()
        self.assertFalse(AgentRun.objects.exists())

    def test_failed_refusal_write_leaves_no_display_refusal(self):
        queryset_class = type(AgentRun.objects.all())
        original_update = queryset_class.update

        def fail_refusal_write(queryset, **values):
            if "refusal_anchor_id" in values:
                raise DatabaseError("write failed")
            return original_update(queryset, **values)

        with patch.object(queryset_class, "update", new=fail_refusal_write):
            response = self.refuse(self.client, "first refusal fails to store")
        self.assertEqual(response.status_code, 200)
        events = parse_run_events(response)
        self.assertIn("error", [event for event, _data in events])
        self.assertNotIn("done", [event for event, _data in events])
        self.assertNotIn(REFUSAL_MESSAGE, str(events))
        run = AgentRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(run.status, AgentRunStatus.FAILED)
        self.assertIsNone(run.refusal_anchor_id)

    def test_existing_permissions_protect_saved_refusals_and_page_restore(self):
        create_configuration()
        response = self.refuse(self.client, "persisted refusal request")
        selected = AgentConversation.objects.get(pk=response.json()["conversation_id"])
        detail_url = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": selected.pk})
        owner = client_with_session(self, self.user, **{CONVERSATION_KEY: str(selected.pk)})
        listing = owner.get(reverse("plugins:nautobot_agent:agentconversation_list"), HTTP_HX_REQUEST="true")
        self.assertContains(listing, selected.title)
        detail = owner.get(detail_url)
        self.assertContains(detail, "persisted refusal request")
        self.assertContains(detail, REFUSAL_MESSAGE)
        with run_dependencies(graph_source=_unexpected_graph):
            restored = owner.get(reverse("plugins:nautobot_agent:agent"))
        self.assertEqual(
            page_json(restored, "agent-state")["history"],
            [
                {"role": "user", "content": "persisted refusal request"},
                {"role": "assistant", "content": REFUSAL_MESSAGE},
            ],
        )


class RefusalDurableHistoryTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Durable admission and graph executor connections require committed refusal/checkpoint state."""

    agent_scope_mode = None

    def test_refusals_never_enter_model_context(self):
        use_runtime(self, saver=DjangoCheckpointSaver())
        graph = self.start_graph([AIMessage("first answer"), AIMessage("accepted answer")])
        inputs = []

        def classify(_text, classifier_history, **_kwargs):
            inputs.append([message.text for message in classifier_history])
            return ScopeDecision(allowed=_text not in {"interstitial refusal", "refused follow-up"})

        with run_dependencies(scope_classifier=classify):
            first = post_run_message(self.client, "accepted question")
            refusal = post_run_message(self.client, "interstitial refusal")
            followup = post_run_message(self.client, "accepted followup")
        self.assertEqual([response.status_code for response in (first, refusal, followup)], [200, 200, 200])
        selected = AgentConversation.objects.get(pk=self.client.session[CONVERSATION_KEY])
        self.assertEqual(inputs, [[], ["accepted question", "first answer"], ["accepted question", "first answer"]])
        self.assertEqual(
            [message.text for message in graph.get_state(session_config(self.client)).values["messages"]],
            ["accepted question", "first answer", "accepted followup", "accepted answer"],
        )
        self.assertEqual(
            [entry["content"] for entry in conversation.transcript(selected).entries],
            [
                "accepted question",
                "first answer",
                "interstitial refusal",
                REFUSAL_MESSAGE,
                "accepted followup",
                "accepted answer",
            ],
        )
        restored = self.client.get(reverse("plugins:nautobot_agent:agent"))
        self.assertEqual(
            [entry["content"] for entry in page_json(restored, "agent-state")["history"]],
            [entry["content"] for entry in conversation.transcript(selected).entries],
        )

        config = session_config(self.client)
        messages = graph.get_state(config).values["messages"]
        guard = TestRunWriteGuard(selected)
        graph.update_state(
            attach_run_guard(config, guard),
            {
                "messages": [RemoveMessage(id=message.id) for message in messages]
                + [
                    SystemMessage("internal retained state", id="retained-system"),
                    ToolMessage("private retained result", tool_call_id="retained-call", id="actual-tail"),
                ]
            },
        )
        finish_run(guard.claim.run_id, AgentRunStatus.COMPLETED)
        checkpoint = current_runtime().saver.get_tuple(config)
        with run_dependencies(scope_classifier=classify):
            response = post_run_message(self.client, "refused follow-up")
        self.assertEqual(response.status_code, 200)
        refusal = AgentRun.objects.get(conversation=selected, prompt="refused follow-up")
        self.assertEqual(refusal.refusal_anchor_id, "actual-tail")
        self.assertEqual(inputs[-1], ["internal retained state", "private retained result"])
        self.assertEqual(current_runtime().saver.get_tuple(config), checkpoint)
