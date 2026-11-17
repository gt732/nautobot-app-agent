"""Tests for the Agent chat views."""
# pylint: disable=too-many-lines

import re
import uuid
from typing import Any
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import resolve_url
from django.urls import reverse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from nautobot.apps.testing import NautobotTestCaseMixin, TestCase

from nautobot_agent.chat.conversation import CONVERSATION_KEY, DEPARTURE_COOKIE_NAME
from nautobot_agent.models import AgentCheckpoint, AgentConversation, LLMConfiguration
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.checkpointer import thread_config
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE
from nautobot_agent.tests.support.chat import (
    ScriptedToolModel,
    client_with_session,
    create_configuration,
    create_conversation,
    create_refused_run,
    enable_agent_settings,
    guarded_test_config,
    page_json,
    parse_run_events,
    post_run_message,
    run_dependencies,
    select_conversation,
    stale_conversation_references,
    use_runtime,
)
from nautobot_agent.tests.support.db import CommittedTestCase

User = get_user_model()


class AgentViewTestCase(TestCase):
    """Test model-free page reads and owned-history restoration under rollback isolation."""

    def setUp(self):
        super().setUp()
        use_runtime(self)
        enable_agent_settings(self)
        create_configuration()

    def test_page_requires_login(self):
        """An authenticated page is model-free, uncached and linked; anonymous users go to login."""
        url = reverse("plugins:nautobot_agent:agent")
        with run_dependencies(graph_source=lambda _user: (_ for _ in ()).throw(AssertionError("provider invoked"))):
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(CONVERSATION_KEY, self.client.session)
        self.assertFalse(AgentConversation.objects.exists())
        self.assertTemplateUsed(response, "nautobot_agent/agent.html")
        self.assertEqual(page_json(response, "agent-state")["conversation_id"], "new")
        self.assertContains(response, 'id="agent-input"')
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(page_json(response, "agent-state")["history"], [])
        self.assertEqual(page_json(response, "agent-state")["todos"], [])
        self.assertRegex(response.content.decode(), rf'href="{re.escape(url)}">\s*Agent\s*</a>')

        self.client.logout()
        response = self.client.get(url)
        self.assertRedirects(response, f"{resolve_url(settings.LOGIN_URL)}?next={url}", fetch_redirect_response=False)

    def test_page_uses_explicit_conversation_identity_and_rejects_invalid_targets(self):
        url = reverse("plugins:nautobot_agent:agent")
        conversation = create_conversation(self.user)
        explicit = self.client.get(url, {"conversation": str(conversation.pk).upper()})
        self.assertEqual(explicit.status_code, 200)
        self.assertEqual(page_json(explicit, "agent-state")["conversation_id"], str(conversation.pk))
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

        fresh = self.client.get(url, {"conversation": "new"})
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(page_json(fresh, "agent-state")["conversation_id"], "new")
        self.assertNotIn(CONVERSATION_KEY, self.client.session)

        select_conversation(self.client, conversation)
        omitted = self.client.get(url)
        self.assertEqual(omitted.status_code, 200)
        self.assertEqual(page_json(omitted, "agent-state")["conversation_id"], str(conversation.pk))
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

        foreign = create_conversation(get_user_model().objects.create_user(username=f"foreign-{uuid.uuid4()}"))
        for selector in (
            "malformed",
            conversation.pk.hex,
            f"{{{conversation.pk}}}",
            f"urn:uuid:{conversation.pk}",
            "",
        ):
            with self.subTest(selector=selector):
                invalid = self.client.get(url, {"conversation": selector})
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

        duplicate = self.client.get(f"{url}?conversation={conversation.pk}&conversation={conversation.pk}")
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))
        for selector in (str(foreign.pk), str(uuid.uuid4())):
            with self.subTest(missing_or_foreign=selector):
                missing_or_foreign = self.client.get(url, {"conversation": selector})
                self.assertEqual(missing_or_foreign.status_code, 404)
                self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

    def test_departure_cookie_expires_only_matching_plain_selection_at_boundary(
        self,
    ):  # pylint: disable=too-many-locals
        page_url = reverse("plugins:nautobot_agent:agent")
        conversation = create_conversation(self.user)
        other = create_conversation(self.user)
        now_ms = 2_000_000_000_000

        def marker(selector, timestamp, *, user_id=None):
            return f"{user_id or self.user.pk}:{selector}:{timestamp}"

        def get_page(cookie, *, selector=None):
            client = client_with_session(self, self.user, **{CONVERSATION_KEY: str(conversation.pk)})
            if cookie is not None:
                client.cookies[DEPARTURE_COOKIE_NAME] = cookie
            query = {} if selector is None else {"conversation": selector}
            return client, client.get(page_url, query)

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": [HumanMessage("retained question", id="question")]}
        checkpoint["channel_versions"] = {"messages": 1}
        current_runtime().saver.put(
            guarded_test_config(thread_config(conversation.pk), conversation),
            checkpoint,
            {"source": "input", "step": 0},
            {"messages": 1},
        )
        self.assertTrue(AgentCheckpoint.objects.filter(conversation=conversation).exists())

        with (
            patch("nautobot_agent.chat.conversation.time.time_ns", return_value=now_ms * 1_000_000),
            patch("nautobot_agent.chat.conversation.app_settings.chat_return_timeout", return_value=10),
        ):
            client, just_before = get_page(marker(str(conversation.pk), now_ms - 9_999))
            self.assertEqual(page_json(just_before, "agent-state")["conversation_id"], str(conversation.pk))
            self.assertEqual(client.session[CONVERSATION_KEY], str(conversation.pk))

            for age_ms in (10_000, 10_001):
                with self.subTest(age_ms=age_ms):
                    client, expired = get_page(marker(str(conversation.pk), now_ms - age_ms))
                    self.assertEqual(page_json(expired, "agent-state")["conversation_id"], "new")
                    self.assertNotIn(CONVERSATION_KEY, client.session)
                    self.assertTrue(AgentCheckpoint.objects.filter(conversation=conversation).exists())
                    resumed = client.get(page_url, {"conversation": str(conversation.pk)})
                    self.assertEqual(page_json(resumed, "agent-state")["conversation_id"], str(conversation.pk))
                    self.assertEqual(
                        [item["content"] for item in page_json(resumed, "agent-state")["history"]],
                        ["retained question"],
                    )

            unusable = (
                None,
                "malformed",
                marker(str(conversation.pk), 0),
                marker(str(conversation.pk), -1),
                marker(str(conversation.pk), now_ms + 1),
                marker(str(other.pk), now_ms - 20_000),
                marker(str(conversation.pk), now_ms - 20_000, user_id=uuid.uuid4()),
                "x" * 129,
                f"{str(self.user.pk).upper()}:{conversation.pk}:{now_ms - 20_000}",
                marker("not-a-uuid", now_ms - 20_000),
            )
            for cookie in unusable:
                with self.subTest(cookie=cookie):
                    client, response = get_page(cookie)
                    self.assertEqual(page_json(response, "agent-state")["conversation_id"], str(conversation.pk))
                    self.assertEqual(client.session[CONVERSATION_KEY], str(conversation.pk))

            recent_new = get_page(marker("new", now_ms - 9_999))[1]
            self.assertEqual(page_json(recent_new, "agent-state")["conversation_id"], str(conversation.pk))
            expired_new_client, expired_new = get_page(marker("new", now_ms - 10_000))
            self.assertEqual(page_json(expired_new, "agent-state")["conversation_id"], "new")
            self.assertNotIn(CONVERSATION_KEY, expired_new_client.session)

            for selector in (str(conversation.pk), "new"):
                with self.subTest(explicit_selector=selector):
                    client, explicit = get_page(marker(str(conversation.pk), now_ms - 20_000), selector=selector)
                    self.assertEqual(page_json(explicit, "agent-state")["conversation_id"], selector)
                    if selector == "new":
                        self.assertNotIn(CONVERSATION_KEY, client.session)
                    else:
                        self.assertEqual(client.session[CONVERSATION_KEY], str(conversation.pk))

    def test_expired_marker_does_not_bypass_session_ownership(self):
        page_url = reverse("plugins:nautobot_agent:agent")
        foreign = create_conversation(User.objects.create_user(username=f"departure-owner-{uuid.uuid4()}"))
        client = client_with_session(self, self.user, **{CONVERSATION_KEY: str(foreign.pk)})
        now_ms = 2_000_000_000_000
        client.cookies[DEPARTURE_COOKIE_NAME] = f"{self.user.pk}:new:{now_ms - 600_000}"
        with (
            patch("nautobot_agent.chat.conversation.time.time_ns", return_value=now_ms * 1_000_000),
            patch("nautobot_agent.chat.conversation.app_settings.chat_return_timeout", return_value=600),
        ):
            response = client.get(page_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(page_json(response, "agent-state")["conversation_id"], "new")
        self.assertNotIn(CONVERSATION_KEY, client.session)
        self.assertEqual(list(AgentConversation.objects.all()), [foreign])

    def test_page_without_default_or_available_credentials(self):
        """Missing defaults block restoration; configured pages restore without constructing a model."""
        page_url = reverse("plugins:nautobot_agent:agent")
        LLMConfiguration.objects.all().delete()
        for label in ("no default", "removed profile"):
            with self.subTest(label=label):
                if label == "removed profile":
                    create_configuration(credential_profile="removed-profile")

                response = self.client.get(page_url, {"conversation": "new"})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="agent-input"')
                self.assertNotContains(response, 'role="alert"')
                self.assertEqual(page_json(response, "agent-state")["history"], [])
                self.assertEqual(page_json(response, "agent-state")["todos"], [])

                conversation = create_conversation(self.user)
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {
                    "messages": [
                        HumanMessage("saved question", id="question"),
                        AIMessage(
                            "saved answer",
                            id="answer",
                            tool_calls=[{"name": "private_tool", "args": {"secret": "private"}, "id": "call"}],
                        ),
                        ToolMessage("private result", tool_call_id="call"),
                    ]
                }
                checkpoint["channel_versions"] = {"messages": 1}
                current_runtime().saver.put(
                    guarded_test_config(thread_config(conversation.pk), conversation),
                    checkpoint,
                    {"source": "input", "step": 0},
                    {"messages": 1},
                )
                create_refused_run(conversation, "refused request", "question")
                select_conversation(self.client, conversation)

                with (
                    patch("nautobot_agent.runtime.llm.init_chat_model", side_effect=AssertionError("provider invoked")),
                    patch(
                        "nautobot_agent.mcp.tools.MCPToolCache.get",
                        side_effect=AssertionError("MCP catalog requested"),
                    ),
                ):
                    response = self.client.get(page_url)

                self.assertEqual(response.status_code, 200)
                if label == "no default":
                    for selector in (None, str(conversation.pk)):
                        with self.subTest(selector=selector):
                            select_conversation(self.client, conversation)
                            query = {} if selector is None else {"conversation": selector}
                            with patch(
                                "nautobot_agent.chat.conversation.page_state",
                                side_effect=AssertionError("unconfigured page restored a conversation"),
                            ):
                                blocked = self.client.get(page_url, query)
                            state = page_json(blocked, "agent-state")
                            self.assertEqual(state["conversation_id"], "new")
                            self.assertEqual(state["history"], [])
                            self.assertEqual(state["todos"], [])
                            self.assertIsNone(state["model_name"])
                            self.assertIsNone(state["active_run"])
                            self.assertIsNone(state["latest_run"])
                            self.assertEqual(state["context_usage"], {"used": None, "limit": None, "percent": None})
                            self.assertNotIn(CONVERSATION_KEY, self.client.session)
                            self.assertContains(blocked, "No language model configured")
                            self.assertNotContains(blocked, "saved question")
                            self.assertTrue(AgentConversation.objects.filter(pk=conversation.pk).exists())
                    continue
                self.assertEqual(
                    page_json(response, "agent-state")["history"],
                    [
                        {"role": "user", "content": "saved question"},
                        {"role": "user", "content": "refused request"},
                        {"role": "assistant", "content": REFUSAL_MESSAGE},
                        {"role": "assistant", "content": "saved answer"},
                        {
                            "role": "tool",
                            "id": '["answer","tool","call"]',
                            "name": "private_tool",
                            "args": {"secret": "private"},
                            "status": "completed",
                        },
                    ],
                )
                self.assertEqual(page_json(response, "agent-state")["todos"], [])
                self.assertIsNone(page_json(response, "agent-state")["model_name"])
                self.assertEqual(
                    page_json(response, "agent-state")["context_usage"], {"used": None, "limit": None, "percent": None}
                )
                self.assertEqual(self.client.session[CONVERSATION_KEY], str(conversation.pk))

    def test_page_with_empty_or_unreadable_history(self):
        """Readable empty history stays empty; failed reads add only a sanitized notice."""
        page_url = reverse("plugins:nautobot_agent:agent")
        LLMConfiguration.objects.filter(is_default=True).update(credential_profile="removed-profile")
        empty = create_conversation(self.user)
        select_conversation(self.client, empty)
        response = self.client.get(page_url)
        self.assertNotContains(response, "Conversation history is currently unavailable.")
        self.assertEqual(page_json(response, "agent-state")["history"], [])
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(empty.pk))
        self.assertIsNone(response.context["notice"])
        with patch.object(current_runtime().saver, "get_tuple", side_effect=RuntimeError("private storage detail")):
            failed = self.client.get(page_url)
        self.assertEqual(page_json(failed, "agent-state")["history"], [])
        self.assertContains(failed, "Conversation history is currently unavailable.")
        self.assertNotContains(failed, "private storage detail")
        self.assertEqual(self.client.session[CONVERSATION_KEY], str(empty.pk))


class ConversationOwnershipTests(TestCase):
    """Session-selected conversations are owner-checked and repaired before any graph or checkpoint access."""

    PAGE = "plugins:nautobot_agent:agent"

    def setUp(self):
        super().setUp()
        use_runtime(self)
        enable_agent_settings(self)

    def test_foreign_and_stale_references_are_forgotten(self):
        foreign = create_conversation(User.objects.create_user(username="conversation-owner"))
        superuser = User.objects.create_superuser(username="conversation-root")
        selected = {CONVERSATION_KEY: str(foreign.pk)}

        for user in (self.user, superuser):
            with self.subTest(user=user.username):
                client = client_with_session(self, user, **selected)
                response = client.get(reverse(self.PAGE))

                self.assertEqual(response.status_code, 200)
                self.assertNotIn(CONVERSATION_KEY, client.session)
                self.assertEqual(
                    (page_json(response, "agent-state")["history"], page_json(response, "agent-state")["todos"]),
                    ([], []),
                )
                self.assertEqual(list(AgentConversation.objects.all()), [foreign])

        AgentConversation.objects.all().delete()
        for label, reference in stale_conversation_references(self.user).items():
            with self.subTest(label):
                client = client_with_session(self, self.user, **reference)
                response = client.get(reverse(self.PAGE))

                self.assertEqual(response.status_code, 200)
                self.assertNotIn(CONVERSATION_KEY, client.session)
                self.assertFalse(AgentConversation.objects.exists())
                self.assertEqual(
                    (page_json(response, "agent-state")["history"], page_json(response, "agent-state")["todos"]),
                    ([], []),
                )


class TodoMiddlewareGraphTests(NautobotTestCaseMixin, CommittedTestCase):
    """Real durable admission requires outermost commits before the todo graph executes."""

    client: Any  # Django's installed Client.post stub incorrectly declares WSGIRequest.

    def setUp(self):
        super().setUp()
        self.setUpNautobot()
        use_runtime(self, saver=InMemorySaver())
        enable_agent_settings(self)

    def test_real_todo_middleware_plans_and_clears_restored_view(self):
        """Scripted write_todos calls reach durable events, the checkpoint, and restored page."""

        plan = [{"content": "Inspect", "status": "in_progress"}, {"content": "Report", "status": "pending"}]
        script = [
            ("Planning.", plan),
            ("Planned.", None),
            ("Clearing.", []),
            ("Cleared.", None),
        ]
        model = ScriptedToolModel(
            responses=[
                AIMessage(
                    content=text,
                    tool_calls=[{"name": "write_todos", "args": {"todos": todos}, "id": f"call-todos-{index}"}]
                    if todos is not None
                    else [],
                )
                for index, (text, todos) in enumerate(script)
            ]
        )
        create_configuration()
        use_runtime(
            self,
            model_factory=lambda _configuration: model,
        )
        page_url = reverse("plugins:nautobot_agent:agent")

        def send(client, message):
            with run_dependencies():
                response = post_run_message(client, message)
            events = parse_run_events(response)
            for event, data in events:
                if event == "todos":
                    self.assertEqual(set(data), {"todos"})
            return events, [data["todos"] for event, data in events if event == "todos"]

        events, snapshots = send(self.client, "Plan.")
        thread_id = self.client.session[CONVERSATION_KEY]
        self.assertEqual(AgentConversation.objects.get(pk=thread_id).user, self.user)
        self.assertEqual(snapshots, [[], plan])
        self.assertEqual([data["text"] for event, data in events if event == "token"], ["Planning.", "Planned."])
        self.assertEqual((events[0][0], events[-1][0]), ("todos", "done"))
        self.assertEqual(page_json(self.client.get(page_url), "agent-state")["todos"], plan)

        self.assertEqual(send(self.client, "Clear.")[1], [plan, []])
        self.assertEqual(page_json(self.client.get(page_url), "agent-state")["todos"], [])
