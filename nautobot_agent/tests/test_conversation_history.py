"""Rendered native list, management, and resume behavior for conversation history."""
# pylint: disable=too-many-lines

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import SUCCESS, get_messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from nautobot.apps.testing import TestCase
from nautobot.core.jobs.bulk_actions import BulkDeleteObjects
from nautobot.extras.models import JobResult, SavedView

from nautobot_agent.chat.conversation import CONVERSATION_KEY, DEPARTURE_COOKIE_NAME
from nautobot_agent.chat.conversation import delete as delete_conversation
from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
)
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE
from nautobot_agent.tables import AgentConversationTable
from nautobot_agent.tests.support.chat import (
    client_with_session,
    create_configuration,
    create_conversation,
    create_refused_run,
    create_reserved_run,
    create_storage_artifacts,
    page_json,
    run_dependencies,
    use_runtime,
)
from nautobot_agent.tests.support.data import grant

User = get_user_model()


# Native TestCase provides rollback isolation; the checker incorrectly requires its transactional sibling.
class _ConversationHistoryCase(TestCase):  # pylint: disable=too-many-instance-attributes,nb-incorrect-base-class
    """History uses native permissions and owner-scoped resume."""

    @classmethod
    def setUpTestData(cls):
        cls.other = User.objects.create_user(username="history-other")
        cls.foreign_row = create_conversation(cls.other, title="Foreign result")
        cls.created_at = timezone.now()
        cls.activity_at = cls.created_at + timedelta(seconds=1)
        cls.list_url = reverse("plugins:nautobot_agent:agentconversation_list")

    def setUp(self):
        super().setUp()
        self.runtime = use_runtime(self)
        # Nautobot creates ``self.user`` per test, so owner-bound rows and grants stay here.
        self.owner = self.user
        grant(self.owner, AgentConversation, actions=("view",), constraints={"user": "$user"})
        self.own_row = create_conversation(
            self.owner,
            title='<b>quoted "history"</b>',
            created_at=self.created_at,
            last_activity_at=self.activity_at,
        )


class ConversationHistoryViewTests(_ConversationHistoryCase):
    """Native history visibility and coordinated resume behavior."""

    def test_trace_counts_require_grant_and_link_only_nonzero_restricted_counts(self):
        self.owner.set_config("tables.AgentConversationTable.columns", ["title", "traces", "resume"], commit=True)
        client = client_with_session(self, self.owner)
        denied = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertEqual(denied.status_code, 200)
        self.assertNotContains(denied, "Traces")
        configured = AgentConversationTable([self.own_row], user=self.owner, exclude=())
        self.assertNotIn("traces", configured.columns)
        anonymous_table = AgentConversationTable([self.own_row], user=None, exclude=())
        self.assertNotIn("traces", anonymous_table.columns)

        AgentExecutionTrace.objects.create(
            conversation=self.own_row, request="one", status="ok", started_at=timezone.now()
        )
        AgentExecutionTrace.objects.create(
            conversation=self.own_row,
            request="excluded",
            status="refused",
            started_at=timezone.now(),
        )
        AgentExecutionTrace.objects.create(
            conversation=self.foreign_row, request="foreign", status="ok", started_at=timezone.now()
        )
        grant(self.owner, AgentExecutionTrace, constraints={"status": "ok"})
        zero_row = create_conversation(self.owner, title="No traces")
        plural_row = create_conversation(self.owner, title="Plural trace count")
        for index in range(2):
            AgentExecutionTrace.objects.create(
                conversation=plural_row,
                request=f"plural {index}",
                status="ok",
                started_at=timezone.now(),
            )

        listed = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertEqual(listed.status_code, 200)
        self.assertContains(
            listed,
            f'href="{reverse("plugins:nautobot_agent:agentexecutiontrace_list")}'
            f'?conversation={self.own_row.pk}">1 trace</a>',
        )
        self.assertContains(
            listed,
            f'href="{reverse("plugins:nautobot_agent:agentexecutiontrace_list")}'
            f'?conversation={plural_row.pk}">2 traces</a>',
        )
        self.assertContains(listed, "<td>—</td>", html=True)
        self.assertNotContains(listed, f"?conversation={zero_row.pk}")
        self.assertNotContains(listed, str(self.foreign_row.pk))

    def test_foreign_history_requires_explicit_permission(self):
        reviewer = User.objects.create_user(username="history-reviewer")
        grant(reviewer, AgentConversation, actions=("view",))
        create_conversation(reviewer, title="Reviewer owned")
        inactive = User.objects.create_user(username="inactive-history-reader", is_active=False)
        grant(inactive, AgentConversation, actions=("viewall",))
        all_url = reverse("plugins:nautobot_agent:agentconversation_all")
        foreign_detail = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.foreign_row.pk})

        # An unconstrained native view grant still lists and reads only owned history.
        response = client_with_session(self, reviewer).get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertNotContains(response, "Foreign result")
        self.assertContains(response, "Reviewer owned")
        self.assertNotContains(response, "quoted")
        personal = client_with_session(self, reviewer).get(self.list_url)
        self.assertContains(personal, f'href="{self.list_url}"')
        self.assertNotContains(personal, f'href="{all_url}"')
        self.assertEqual(client_with_session(self, reviewer).get(all_url).status_code, 403)
        self.assertEqual(client_with_session(self, reviewer).get(foreign_detail).status_code, 404)
        request = client_with_session(self, reviewer).get(self.list_url).wsgi_request
        self.assertRaises(Http404, delete_conversation, request, self.foreign_row.pk)
        self.assertFalse(AgentConversation.objects.readable_by(inactive).exists())

        superuser = User.objects.create_superuser(username="history-list-root")
        root_client = client_with_session(self, superuser)
        create_conversation(superuser, title="Root owned")
        personal = root_client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(personal, "Root owned")
        self.assertNotContains(personal, "Foreign result")
        all_rows = root_client.get(all_url, HTTP_HX_REQUEST="true")
        self.assertEqual(all_rows.status_code, 200)
        self.assertContains(all_rows, "Foreign result")
        self.assertContains(all_rows, "&lt;b&gt;quoted &quot;history&quot;&lt;/b&gt;", html=False)

        grant(reviewer, AgentConversation, actions=("viewall",))
        all_response = client_with_session(self, reviewer).get(all_url)
        self.assertEqual(all_response.status_code, 200)
        self.assertContains(all_response, f'href="{all_url}"')
        response = client_with_session(self, reviewer).get(all_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Foreign result")
        self.assertContains(response, "quoted")
        personal = client_with_session(self, reviewer).get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(personal, "Reviewer owned")
        self.assertNotContains(personal, "Foreign result")
        filtered = client_with_session(self, reviewer).get(
            self.list_url, {"user": self.other.username}, HTTP_HX_REQUEST="true"
        )
        self.assertNotContains(filtered, "Foreign result")
        self.assertEqual(client_with_session(self, reviewer).get(foreign_detail).status_code, 200)
        foreign_edit = reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.foreign_row.pk})
        foreign_delete = reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.foreign_row.pk})
        self.assertEqual(client_with_session(self, reviewer).get(foreign_edit).status_code, 403)
        self.assertEqual(client_with_session(self, reviewer).get(foreign_delete).status_code, 403)

    def test_detail_is_sanitized_model_free_and_fails_unavailable(self):
        detail_url = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.own_row.pk})
        foreign_url = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.foreign_row.pk})
        no_graph = {"graph_source": lambda _user: (_ for _ in ()).throw(AssertionError("graph initialized"))}
        client = client_with_session(self, self.owner)
        retained = [
            HumanMessage(content="<script>alert('user')</script>\nline"),
            AIMessage(content="**safe** <script>alert('assistant')</script>"),
            AIMessage(
                content="",
                tool_calls=[{"name": "private_tool", "args": {"secret": "private"}, "id": "call"}],
            ),
            ToolMessage(content="private tool output", tool_call_id="call", name="private_tool"),
        ]
        for label, messages in (("sanitized", retained), ("malformed", [object()])):
            with self.subTest(label):
                reader = Mock()
                reader.get_state.return_value = SimpleNamespace(values={"messages": messages})
                with patch.object(self.runtime, "state_reader", reader), run_dependencies(**no_graph):
                    response = client.get(detail_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Cache-Control"], "private, no-store")
                if label == "malformed":
                    self.assertContains(response, "Conversation history is currently unavailable.")
                    continue
                self.assertContains(response, "&lt;script&gt;alert(&#x27;user&#x27;)&lt;/script&gt;", html=False)
                self.assertContains(response, "<strong>safe</strong>", html=True)
                self.assertNotContains(response, "<script>alert('assistant')</script>", html=False)
                self.assertNotContains(response, "private_tool")
                self.assertNotContains(response, "private tool output")

        with self.subTest("read failure"):
            with patch.object(current_runtime().saver, "get_tuple", side_effect=OSError("secret")):
                response = client.get(detail_url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Conversation history is currently unavailable.")
            self.assertNotContains(response, "secret")
            self.assertEqual(client.get(foreign_url).status_code, 404)

        with self.subTest("management pages"):
            grant(self.owner, AgentConversation, actions=("change", "delete"), constraints={"user": "$user"})
            edit_url = reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.own_row.pk})
            delete_url = reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.own_row.pk})
            with run_dependencies(**no_graph):
                self.assertEqual(client.get(self.list_url).status_code, 200)
                self.assertEqual(client.get(edit_url).status_code, 200)
                confirmation = client.get(delete_url)
                self.assertEqual(confirmation.status_code, 200)
                self.assertContains(confirmation, "&lt;b&gt;quoted &quot;history&quot;&lt;/b&gt;", html=False)

    def test_detail_assistant_markdown_blocks_resource_loads_and_keeps_links(self):
        source = """
**safe** [details](https://outside.invalid/details)

![image](https://outside.invalid/markdown)

<picture><source srcset="https://outside.invalid/source"><img src="https://outside.invalid/image"></picture>
<video poster="https://outside.invalid/poster" src="https://outside.invalid/video"></video>
<audio src="https://outside.invalid/audio"></audio>
<style>@import "https://outside.invalid/css";</style>
<span style="background:url(https://outside.invalid/background)">visible</span>
<svg><image href="https://outside.invalid/svg"></image></svg>
<iframe src="https://outside.invalid/frame"></iframe>
"""
        reader = Mock()
        reader.get_state.return_value = SimpleNamespace(values={"messages": [AIMessage(content=source)]})
        detail_url = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.own_row.pk})
        with patch.object(self.runtime, "state_reader", reader):
            response = client_with_session(self, self.owner).get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>safe</strong>", html=True)
        self.assertContains(response, 'href="https://outside.invalid/details"')
        self.assertContains(response, "visible")
        for path in ("markdown", "source", "image", "poster", "video", "audio", "css", "background", "svg", "frame"):
            with self.subTest(path=path):
                self.assertNotContains(response, f"https://outside.invalid/{path}")

    def test_rich_projection_failure_leaves_detail_transcript_available(self):
        create_configuration()
        client = client_with_session(self, self.owner)
        reader = Mock()
        reader.get_state.return_value.values = {
            "messages": [
                HumanMessage("Chart the inventory", id="question"),
                AIMessage(
                    "Inventory chart",
                    id="answer",
                    tool_calls=[{"name": "render_chart", "args": {}, "id": "chart"}],
                ),
                ToolMessage("secret result", tool_call_id="chart"),
            ],
        }
        create_refused_run(self.own_row, "refused question", "answer")
        detail_url = reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.own_row.pk})
        with (
            patch.object(self.runtime, "state_reader", reader),
            patch("nautobot_agent.chat.history.build_chart_config", side_effect=RuntimeError("private chart failure")),
        ):
            page = client.get(reverse("plugins:nautobot_agent:agent"), {"conversation": str(self.own_row.pk)})
            state = client.get(reverse("plugins:nautobot_agent:agent_state"), {"conversation": str(self.own_row.pk)})
            detail = client.get(detail_url)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["notice"])
        self.assertEqual(page_json(page, "agent-state")["history"], [])
        self.assertEqual(state.status_code, 503)
        self.assertContains(detail, "Inventory chart")
        self.assertContains(detail, REFUSAL_MESSAGE)
        self.assertNotContains(detail, "Conversation history is currently unavailable.")
        for response in (page, detail):
            self.assertNotContains(response, "secret result")
            self.assertNotContains(response, "private chart failure")

    def test_resume_is_owner_only_and_permission_gated(self):  # pylint: disable=too-many-locals
        row_url = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": self.own_row.pk})
        foreign_resume = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": self.foreign_row.pk})

        unprivileged = client_with_session(self, User.objects.create_user(username="history-unprivileged"))
        self.assertEqual(unprivileged.get(self.list_url).status_code, 403)
        self.assertEqual(unprivileged.post(row_url).status_code, 403)

        client = client_with_session(self, self.owner)
        now_ms = 2_000_000_000_000
        client.cookies[DEPARTURE_COOKIE_NAME] = f"{self.owner.pk}:{self.own_row.pk}:{now_ms - 600_000}"
        self.assertEqual(client.get(row_url).status_code, 405)
        missing = reverse(
            "plugins:nautobot_agent:agentconversation_resume",
            kwargs={"pk": "00000000-0000-0000-0000-000000000001"},
        )
        self.assertEqual(client.post(missing).status_code, 404)
        self.assertNotIn(CONVERSATION_KEY, client.session)

        for htmx in (False, True):
            with self.subTest(no_default=True, htmx=htmx):
                blocked = client.post(row_url, HTTP_HX_REQUEST="true" if htmx else "false")
                if htmx:
                    self.assertEqual(blocked.status_code, 200)
                    self.assertEqual(blocked["HX-Redirect"], self.list_url)
                else:
                    self.assertRedirects(blocked, self.list_url, fetch_redirect_response=False)
                self.assertNotIn(CONVERSATION_KEY, client.session)
                self.assertIn(
                    "Configure a default language model before resuming a conversation.",
                    [str(message) for message in get_messages(blocked.wsgi_request)],
                )

        create_configuration()
        with run_dependencies(
            graph_source=lambda _user: (_ for _ in ()).throw(AssertionError("resume built an agent"))
        ):
            response = client.post(row_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["HX-Redirect"],
            f"{reverse('plugins:nautobot_agent:agent')}?conversation={self.own_row.pk}",
        )
        with (
            run_dependencies(graph_source=lambda _user: (_ for _ in ()).throw(AssertionError("resume built an agent"))),
            patch("nautobot_agent.chat.conversation.time.time_ns", return_value=now_ms * 1_000_000),
            patch("nautobot_agent.chat.conversation.app_settings.chat_return_timeout", return_value=600),
        ):
            resumed = client.get(response["HX-Redirect"])
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(page_json(resumed, "agent-state")["conversation_id"], str(self.own_row.pk))
        self.assertEqual(client.session[CONVERSATION_KEY], str(self.own_row.pk))
        activity = AgentConversation.objects.get(pk=self.own_row.pk).last_activity_at
        repeated = client.post(row_url, follow=True)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(AgentConversation.objects.get(pk=self.own_row.pk).last_activity_at, activity)

        for user in (self.owner, User.objects.create_superuser(username="history-root")):
            foreign_client = client_with_session(self, user)
            self.assertEqual(foreign_client.post(foreign_resume).status_code, 404)
            self.assertNotIn(CONVERSATION_KEY, foreign_client.session)

    def test_detail_actions_follow_object_permissions_without_foreign_resume(self):
        create_configuration()
        client = client_with_session(self, self.owner)
        foreign_resume = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": self.foreign_row.pk})
        # Detail actions are per object: granted foreign change/delete, but never foreign resume.
        change_url = reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.foreign_row.pk})
        delete_url = reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.foreign_row.pk})
        reviewer = User.objects.create_user(username="history-actions")
        grant(reviewer, AgentConversation, actions=("view", "change", "delete"))
        grant(reviewer, AgentConversation, actions=("viewall",))
        reviewer_client = client_with_session(self, reviewer)
        detail = reviewer_client.get(
            reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.foreign_row.pk})
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, change_url)
        self.assertContains(detail, delete_url)
        self.assertNotContains(detail, foreign_resume)
        owner_detail = client.get(reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.own_row.pk}))
        self.assertContains(owner_detail, "Resume")
        self.assertNotContains(
            owner_detail,
            reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.own_row.pk}),
        )
        self.assertNotContains(
            owner_detail,
            reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.own_row.pk}),
        )
        self.assertEqual(reviewer_client.post(foreign_resume).status_code, 404)
        changed = reviewer_client.post(change_url, {"title": "Changed"})
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(
            changed["Location"],
            reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.foreign_row.pk}),
        )
        self.foreign_row.refresh_from_db()
        self.assertEqual(self.foreign_row.title, "Changed")


class ConversationHistoryManagementTests(_ConversationHistoryCase):
    """Title-only native editing and reservation-aware confirmed deletion."""

    def test_generic_bulk_delete_job_rejects_conversations(self):
        self._grant_all_delete()
        reserved = create_reserved_run(self.own_row, prompt="active")
        job = BulkDeleteObjects()
        job.job_result = JobResult(user=self.owner)
        for delete_all in (False, True):
            with self.subTest(delete_all=delete_all):
                with self.assertRaises(PermissionDenied):
                    job.run(
                        content_type=ContentType.objects.get_for_model(AgentConversation),
                        pk_list=[str(self.own_row.pk)],
                        delete_all=delete_all,
                    )
                self.assertTrue(AgentConversation.objects.filter(pk=self.own_row.pk).exists())
                self.assertTrue(AgentRun.objects.filter(pk=reserved.pk).exists())
        response = client_with_session(self, self.owner).post(
            reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.own_row.pk}),
            {"confirm": "true"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AgentConversation.objects.filter(pk=self.own_row.pk).exists())
        self.assertTrue(AgentRun.objects.filter(pk=reserved.pk).exists())

    def test_single_delete_logs_identifier_without_prompt_text(self):
        grant(self.owner, AgentConversation, actions=("delete",), constraints={"user": "$user"})
        target = create_conversation(self.owner, title="Sensitive first prompt")
        client = client_with_session(self, self.owner)
        url = reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": target.pk})
        with self.assertLogs("nautobot.core.views.mixins", level="INFO") as logs:
            response = client.post(url, {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgentConversation.objects.filter(pk=target.pk).exists())
        self.assertIn(str(target.pk), " ".join(logs.output))
        self.assertNotIn(target.title, " ".join(logs.output))
        htmx_target = create_conversation(self.owner, title="HTMX delete target")
        response = client.post(
            reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": htmx_target.pk}),
            {"confirm": "true"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], reverse("plugins:nautobot_agent:agentconversation_list"))
        self.assertFalse(AgentConversation.objects.filter(pk=htmx_target.pk).exists())

    def _bulk_url(self):
        return reverse("plugins:nautobot_agent:agentconversation_bulk_delete")

    def _grant_all_delete(self, user=None):
        grant(user or self.owner, AgentConversation, actions=("view", "delete"))
        grant(user or self.owner, AgentConversation, actions=("viewall",))

    def test_native_bulk_confirmation_permissions_and_personal_controls(self):  # pylint: disable=too-many-locals
        reviewer = User.objects.create_user(username="bulk-history-reviewer")
        self._grant_all_delete(reviewer)
        target = create_conversation(self.other, title="Bulk confirmation target")
        client = client_with_session(self, reviewer, **{CONVERSATION_KEY: str(self.own_row.pk)})
        all_url = self._bulk_url()

        no_delete = User.objects.create_user(username="bulk-no-delete")
        grant(no_delete, AgentConversation, actions=("viewall",))
        self.assertEqual(client_with_session(self, no_delete).post(all_url, {"pk": str(target.pk)}).status_code, 403)
        no_viewall = User.objects.create_user(username="bulk-no-viewall")
        grant(no_viewall, AgentConversation, actions=("view", "delete"))
        self.assertEqual(client_with_session(self, no_viewall).post(all_url, {"pk": str(target.pk)}).status_code, 403)

        confirmation = client.post(all_url, {"pk": str(target.pk)})
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "Bulk confirmation target")
        self.assertContains(confirmation, "Confirm Bulk Deletion")
        self.assertTrue(AgentConversation.objects.filter(pk=target.pk).exists())

        unconfirmed = client.post(all_url, {"pk": str(target.pk), "confirm": "true"})
        self.assertEqual(unconfirmed.status_code, 200)
        self.assertTrue(AgentConversation.objects.filter(pk=target.pk).exists())
        invalid = client.post(all_url, {"pk": str(target.pk), "_confirm": "yes"})
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("confirm", invalid.data["form"].errors)
        self.assertTrue(AgentConversation.objects.filter(pk=target.pk).exists())
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(reviewer)
        self.assertEqual(
            csrf_client.post(all_url, {"pk": str(target.pk), "_confirm": "yes", "confirm": "true"}).status_code, 403
        )

        deleted = client.post(
            all_url,
            {"pk": str(target.pk), "_confirm": "yes", "confirm": "true", "return_url": self.list_url},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(deleted["Location"], reverse("plugins:nautobot_agent:agentconversation_all"))
        self.assertFalse(AgentConversation.objects.filter(pk=target.pk).exists())
        self.assertEqual(client.session[CONVERSATION_KEY], str(self.own_row.pk))

        personal_client = client_with_session(self, self.owner)
        grant(self.owner, AgentConversation, actions=("delete",), constraints={"user": "$user"})
        personal = personal_client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertEqual(personal.status_code, 200)
        self.assertNotContains(personal, self._bulk_url())
        self.assertContains(
            personal,
            reverse("plugins:nautobot_agent:agentconversation_delete", kwargs={"pk": self.own_row.pk}),
        )
        self.assertNotContains(personal, 'name="pk"')
        self.assertEqual(personal_client.post(all_url, {"pk": str(self.own_row.pk)}).status_code, 403)
        empty = client.post(all_url, {})
        self.assertEqual(empty.status_code, 302)
        self.assertTrue(AgentConversation.objects.filter(pk=self.own_row.pk).exists())

        change_only = User.objects.create_user(username="bulk-history-change-only")
        grant(change_only, AgentConversation, actions=("view", "change", "viewall"), constraints={"user": "$user"})
        create_conversation(change_only, title="Change only")
        change_list = client_with_session(self, change_only).get(
            reverse("plugins:nautobot_agent:agentconversation_all"), HTTP_HX_REQUEST="true"
        )
        self.assertContains(change_list, "Change only")
        self.assertNotContains(change_list, 'name="pk"')
        self.assertNotContains(change_list, self._bulk_url())

    def test_bulk_cascades_all_artifacts_and_preserves_safe_return_and_native_surfaces(self):
        self._grant_all_delete()
        first = create_conversation(self.other, title="Cascade first")
        second = create_conversation(self.other, title="Cascade second")
        artifacts = create_storage_artifacts(first)
        create_refused_run(first, "refused request")
        terminal_run = create_reserved_run(first, prompt="terminal")
        AgentRun.objects.filter(pk=terminal_run.pk).update(status=AgentRunStatus.COMPLETED)
        survivors = create_storage_artifacts(create_conversation(self.other, title="Unrelated same owner"))
        url = self._bulk_url()
        client = client_with_session(self, self.owner, **{CONVERSATION_KEY: str(first.pk)})
        initial = client.post(f"{url}?q=Cascade", {"pk": [str(first.pk), str(second.pk)]})
        self.assertEqual(initial.status_code, 200)
        self.assertContains(initial, "Cascade first")
        self.assertContains(initial, "Cascade second")
        self.assertContains(initial, "Cancel")
        all_url = reverse("plugins:nautobot_agent:agentconversation_all")
        self.assertContains(initial, f'href="{all_url}?q=Cascade"')
        self.assertContains(client.get(f"{all_url}?q=Cascade", HTTP_HX_REQUEST="true"), "Cascade first")
        htmx_confirmation = client.post(
            f"{url}?q=Cascade",
            {"pk": [str(first.pk), str(second.pk)]},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(htmx_confirmation.status_code, 200)
        self.assertContains(htmx_confirmation, "Confirm Bulk Deletion")

        response = client.post(
            f"{url}?q=Cascade",
            {"pk": [str(first.pk), str(second.pk)], "_confirm": "yes", "confirm": "true"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], f"{all_url}?q=Cascade")
        self.assertFalse(AgentConversation.objects.filter(pk__in=[first.pk, second.pk]).exists())
        for row in (*artifacts, terminal_run):
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        self.assertFalse(AgentRun.objects.filter(conversation_id=first.pk).exists())
        for row in survivors:
            self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
        self.assertNotIn(CONVERSATION_KEY, client.session)
        self.assertEqual([message.level for message in get_messages(response.wsgi_request)], [SUCCESS])

        # An untrusted external return target falls back to the local All Conversations list.
        third = create_conversation(self.other, title="Safe return")
        safe = client.post(
            f"{url}?return_url=https://outside.example/",
            {"pk": str(third.pk), "_confirm": "yes", "confirm": "true"},
        )
        self.assertEqual(safe.status_code, 302)
        self.assertEqual(safe["Location"], all_url)

    def test_bulk_select_all_and_constraints_are_scoped(self):  # pylint: disable=too-many-locals
        reviewer = User.objects.create_user(username="bulk-resolver-reviewer")
        grant(reviewer, AgentConversation, actions=("view",), constraints={"user": "$user"})
        grant(reviewer, AgentConversation, actions=("viewall",))
        grant(reviewer, AgentConversation, actions=("delete",))
        allowed = [
            create_conversation(
                reviewer,
                title=f"Saved target {index}",
                last_activity_at=self.activity_at - timedelta(seconds=index),
            )
            for index in range(3)
        ]
        forbidden = create_conversation(self.other, title="Saved target forbidden")
        outside_filter = create_conversation(reviewer, title="Outside saved view")
        saved = SavedView.objects.create(
            owner=reviewer,
            name="Conversation targets",
            view="plugins:nautobot_agent:agentconversation_all",
            config={"filter_params": {"q": "Saved target"}},
        )
        client = client_with_session(self, reviewer)
        url = self._bulk_url()
        params = f"?saved_view={saved.pk}"
        visible_page = client.get(
            reverse("plugins:nautobot_agent:agentconversation_all"),
            {"saved_view": str(saved.pk), "per_page": "1"},
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(visible_page, "Saved target 0")
        self.assertNotContains(visible_page, "Saved target 2")
        initial = client.post(f"{url}{params}", {"_all": "1"})
        self.assertEqual(initial.status_code, 200)

        # Nautobot 3.1's resolver sees only the static model queryset; assert actual candidate
        # content and survivors, not that the helper was called with particular arguments.
        with patch(
            "nautobot.core.views.utils.get_view_for_model",
            return_value=SimpleNamespace(queryset=AgentConversation.objects.all()),
        ):
            selected = client.post(
                url,
                {"pk": [str(row.pk) for row in allowed] + [str(forbidden.pk)]},
            )
            self.assertEqual(selected.status_code, 200)
            self.assertContains(selected, "Delete these 3")
            for row in allowed:
                self.assertContains(selected, row.title)
            self.assertNotContains(selected, forbidden.title)
            mixed = client.post(
                url,
                {
                    "pk": [str(allowed[0].pk), str(forbidden.pk)],
                    "_confirm": "yes",
                    "confirm": "true",
                },
            )
            self.assertEqual(mixed.status_code, 200)
            self.assertIn("pk", mixed.data["form"].errors)
        self.assertEqual(AgentConversation.objects.filter(pk__in=[row.pk for row in allowed]).count(), 3)
        self.assertTrue(AgentConversation.objects.filter(pk=forbidden.pk).exists())

        with patch(
            "nautobot.core.views.utils.get_view_for_model",
            return_value=SimpleNamespace(queryset=AgentConversation.objects.all()),
        ):
            response = client.post(
                f"{url}{params}",
                {"_all": "1", "_confirm": "yes", "confirm": "true"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgentConversation.objects.filter(pk__in=[row.pk for row in allowed]).exists())
        self.assertTrue(AgentConversation.objects.filter(pk=forbidden.pk).exists())
        self.assertTrue(AgentConversation.objects.filter(pk=outside_filter.pk).exists())

        filtered = create_conversation(reviewer, title="Filtered selection target")
        filter_miss = create_conversation(reviewer, title="Filtered selection miss")
        filtered_response = client.post(
            f"{url}?q=Filtered%20selection%20target",
            {"_all": "1", "_confirm": "yes", "confirm": "true"},
        )
        self.assertEqual(filtered_response.status_code, 302)
        self.assertFalse(AgentConversation.objects.filter(pk=filtered.pk).exists())
        self.assertTrue(AgentConversation.objects.filter(pk=filter_miss.pk).exists())

        constrained = User.objects.create_user(username="bulk-delete-constrained")
        grant(constrained, AgentConversation, actions=("view", "viewall"))
        grant(constrained, AgentConversation, actions=("delete",), constraints={"user": "$user"})
        permitted = create_conversation(constrained, title="Delete permitted")
        constrained_client = client_with_session(self, constrained)
        mixed = {"pk": [str(permitted.pk), str(self.foreign_row.pk)], "_confirm": "yes", "confirm": "true"}
        rejected = constrained_client.post(self._bulk_url(), mixed)
        self.assertEqual(rejected.status_code, 200)
        self.assertIn("pk", rejected.data["form"].errors)
        self.assertTrue(AgentConversation.objects.filter(pk=permitted.pk).exists())
        self.assertTrue(AgentConversation.objects.filter(pk=self.foreign_row.pk).exists())
        select_all = constrained_client.post(self._bulk_url(), {"_all": "true", "_confirm": "yes", "confirm": "true"})
        self.assertEqual(select_all.status_code, 302)
        self.assertFalse(AgentConversation.objects.filter(pk=permitted.pk).exists())
        self.assertTrue(AgentConversation.objects.filter(pk=self.foreign_row.pk).exists())

    def test_rename(self):
        edit_url = reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.own_row.pk})
        client = client_with_session(self, self.owner)
        self.assertEqual(client.get(edit_url).status_code, 403)
        grant(self.owner, AgentConversation, actions=("change",), constraints={"user": "$user"})
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(edit_url, {"title": "Forged rename"}).status_code, 403)
        foreign_url = reverse("plugins:nautobot_agent:agentconversation_edit", kwargs={"pk": self.foreign_row.pk})
        self.assertEqual(client.get(foreign_url).status_code, 404)
        root = User.objects.create_superuser(username="history-edit-root")
        self.assertEqual(client_with_session(self, root).get(foreign_url).status_code, 200)

        response = client.get(edit_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="&lt;b&gt;quoted &quot;history&quot;&lt;/b&gt;"', html=False)

        invalid = client.post(edit_url, {"title": "   ", "user": self.other.pk})
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "Enter a title.")
        self.own_row.refresh_from_db()
        self.assertEqual(self.own_row.title, '<b>quoted "history"</b>')
        self.assertEqual(self.own_row.user, self.owner)
        too_long = client.post(edit_url, {"title": "x" * 256})
        self.assertEqual(too_long.status_code, 200)
        self.assertContains(too_long, "at most 255")

        escaped_title = "<img src=x onerror='alert(1)'>"
        duplicate = create_conversation(self.owner, title=escaped_title)
        timestamp = self.own_row.created_at
        response = client.post(
            f"{edit_url}?return_url=https://outside.example/",
            {
                "title": f"  {escaped_title}  ",
                "user": self.other.pk,
                "created_at": "2001-01-01",
                "last_activity_at": "2001-01-01",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("plugins:nautobot_agent:agentconversation", kwargs={"pk": self.own_row.pk}),
        )
        self.own_row.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertEqual(self.own_row.title, escaped_title)
        self.assertEqual(duplicate.title, escaped_title)
        self.assertEqual(self.own_row.created_at, timestamp)
        self.assertEqual(self.own_row.last_activity_at, self.activity_at)
        self.assertEqual(self.own_row.user, self.owner)
        listed = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(listed, "&lt;img src=x onerror=&#x27;alert(1)&#x27;&gt;", html=False)
