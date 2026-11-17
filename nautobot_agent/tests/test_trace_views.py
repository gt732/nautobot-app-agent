"""Behavioral coverage for the read-only native trace viewer."""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.db.models import Value
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from nautobot.apps.testing import TestCase

from nautobot_agent.models import (
    AgentConversation,
    AgentExecutionSpan,
    AgentExecutionTrace,
    AgentRun,
    AgentRunStatus,
    ExecutionStatus,
    HeartbeatNow,
    TraceCaptureState,
)
from nautobot_agent.tables import AgentExecutionTraceTable
from nautobot_agent.tests.support.chat import claimed_run, client_with_session, create_reserved_run
from nautobot_agent.tests.support.data import grant

User = get_user_model()


# Native TestCase provides rollback isolation; the checker incorrectly requires its transactional sibling.
class TraceViewTests(TestCase):  # pylint: disable=too-many-public-methods,nb-incorrect-base-class
    """Test trace viewer access, filtering, and safe presentation."""

    @override_settings(
        EXEMPT_VIEW_PERMISSIONS=[
            "nautobot_agent.agentexecutiontrace",
            "nautobot_agent.agentexecutionspan",
        ]
    )
    def test_exempt_trace_summary_preserves_active_user_requirement_for_full_inspection(self):
        active = User.objects.create_user(username="trace-summary-exempt")
        inactive = User.objects.create_user(username="trace-summary-inactive", is_active=False)
        for user in (active, inactive):
            self.assertTrue(AgentExecutionTrace.summary_view_allowed(user))
            self.assertEqual(AgentExecutionTrace.full_inspection_allowed(user), user.is_active)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewer = User.objects.create_user(username="trace-viewer")
        cls.other = User.objects.create_user(username="other<&")
        cls.list_url = reverse("plugins:nautobot_agent:agentexecutiontrace_list")
        grant(cls.viewer, AgentExecutionTrace)

    def trace(self, user, *, request="REQUEST-PAYLOAD-SENTINEL", **values):
        """Create one attempted-send record without running provider code."""
        if "conversation" not in values:
            values["conversation"] = AgentConversation.objects.create(user=user, title="Trace conversation")
        started_at = values.pop("started_at", None) or timezone.now()
        duration = values.pop("duration", 12.345)
        values.setdefault(
            "ended_at",
            None if duration is None else started_at + timedelta(milliseconds=duration),
        )
        return AgentExecutionTrace.objects.create(
            request=request,
            status=values.pop("status", "ok"),
            started_at=started_at,
            **values,
        )

    def test_stale_trace_repaired_on_list_and_detail_without_owner_returning(self):
        client = client_with_session(self, self.viewer)
        for surface in ("list", "detail"):
            with self.subTest(surface=surface):
                trace = self.trace(self.other, status=ExecutionStatus.RUNNING, duration=None)
                run = create_reserved_run(trace.conversation, prompt="Abandoned request")
                claimed_run(run.pk)
                AgentRun.objects.filter(pk=run.pk).update(
                    trace=trace,
                    heartbeat_at=HeartbeatNow() - Value(AgentRun.STALE_HEARTBEAT_AFTER + timedelta(seconds=1)),
                )
                url = self.list_url if surface == "list" else trace.get_absolute_url()
                response = client.get(url, HTTP_HX_REQUEST="true")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Unconfirmed")
                trace.refresh_from_db()
                run.refresh_from_db()
                self.assertEqual(run.status, AgentRunStatus.INTERRUPTED)
                self.assertEqual(trace.status, ExecutionStatus.INCOMPLETE)
                self.assertEqual(trace.capture_state, TraceCaptureState.FAILED)
                self.assertIsNotNone(trace.ended_at)

    def test_trace_list_reconciles_only_visible_page_and_preserves_live_runs(self):
        traces = [self.trace(self.other, status=ExecutionStatus.RUNNING, duration=None) for _ in range(3)]
        rows = []
        for trace in traces:
            run = create_reserved_run(trace.conversation, prompt="Request")
            claimed_run(run.pk)
            AgentRun.objects.filter(pk=run.pk).update(trace=trace)
            rows.append(run)
        for run in rows[:2]:
            AgentRun.objects.filter(pk=run.pk).update(
                heartbeat_at=HeartbeatNow() - Value(AgentRun.STALE_HEARTBEAT_AFTER + timedelta(seconds=1))
            )
        client = client_with_session(self, self.viewer)
        response = client.get(self.list_url, {"per_page": 1, "sort": "started_at"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unconfirmed")
        for run, expected in zip(
            rows,
            (AgentRunStatus.INTERRUPTED, AgentRunStatus.RUNNING, AgentRunStatus.RUNNING),
            strict=True,
        ):
            run.refresh_from_db()
            self.assertEqual(run.status, expected)
        response = client.get(traces[-1].get_absolute_url())
        self.assertEqual(response.status_code, 200)
        rows[-1].refresh_from_db()
        self.assertEqual(rows[-1].status, AgentRunStatus.RUNNING)

    def span(self, trace, *, parent=None, kind="chain", name="span", **values):
        """Persist a representative callback snapshot without invoking a provider."""
        started = values.pop("started_at", trace.started_at)
        defaults = {
            "status": "ok",
            "started_at": started,
            "ended_at": started + timedelta(milliseconds=1),
            "details": {"events": []},
        }
        defaults.update(values)
        return AgentExecutionSpan.objects.create(
            trace=trace,
            run_id=uuid.uuid4(),
            parent_run_id=parent.run_id if parent else None,
            kind=kind,
            name=name,
            **defaults,
        )

    def full_access_user(self, username):
        """Create staff with both inspection grants."""
        user = User.objects.create_user(username=username, is_staff=True)
        grant(user, AgentExecutionTrace)
        grant(user, AgentExecutionSpan)
        return user

    def test_execution_and_diagnostics_are_separate_in_summary_and_list(self):
        trace = self.trace(
            self.other,
            status="incomplete",
            capture_state="partial",
        )
        client = client_with_session(self, self.viewer)
        detail = client.get(trace.get_absolute_url())
        self.assertContains(detail, "Execution:")
        self.assertContains(detail, "Unconfirmed")
        self.assertContains(detail, "Diagnostics:")
        self.assertContains(detail, "Partial")
        self.assertNotContains(detail, "private.input")
        listing = client.get(self.list_url, {"capture_state": "partial"}, HTTP_HX_REQUEST="true")
        self.assertEqual(list(listing.context["table"].data), [trace])
        self.assertEqual(listing.context["table"].rows[0].get_cell("status"), "Unconfirmed")
        self.assertEqual(listing.context["table"].rows[0].get_cell("diagnostics"), "Partial")
        filtered = client.get(self.list_url, {"status": "incomplete"}, HTTP_HX_REQUEST="true")
        self.assertEqual(list(filtered.context["table"].data), [trace])
        choices = dict(filtered.renderer_context["view"].filterset.form.fields["status"].choices)
        self.assertEqual(choices["incomplete"], "Unconfirmed")

    def test_capture_warning_is_overview_only_for_partial_and_failed(self):
        client = client_with_session(self, self.full_access_user("trace-warning-inspector"))
        for state, warning in (
            ("partial", "Some trace details are unavailable."),
            ("failed", "Trace details could not be saved."),
            ("pending", None),
            ("complete", None),
        ):
            trace = self.trace(self.other, capture_state=state)
            url = trace.get_absolute_url()
            overview = client.get(url)
            if warning:
                self.assertContains(overview, warning)
                self.assertContains(overview, "Check server logs for details.")
            else:
                self.assertNotContains(overview, "Some trace details are unavailable.")
                self.assertNotContains(overview, "Trace details could not be saved.")
            span = self.span(trace, name=f"{state} span")
            selected = client.get(url, {"span": str(span.run_id)})
            self.assertNotContains(selected, "Some trace details are unavailable.")
            self.assertNotContains(selected, "Trace details could not be saved.")

    def test_ended_pending_capture_is_unconfirmed_not_running_in_summary_and_full_views(self):
        notice = "The execution has ended; saving its trace details has not been confirmed."
        ended = self.trace(self.other, status="ok", capture_state="pending")
        running = self.trace(self.other, status="running", duration=None, capture_state="pending")
        for client in (
            client_with_session(self, self.viewer),
            client_with_session(self, self.full_access_user("trace-pending-inspector")),
        ):
            response = client.get(ended.get_absolute_url())
            self.assertContains(response, notice)
            self.assertNotContains(response, "This execution is still running.")
            self.assertNotContains(client.get(running.get_absolute_url()), notice)

    def test_list_and_summary_rendering(self):  # pylint: disable=too-many-locals
        prompt_title = "PRIVATE-FIRST-PROMPT: explain the hidden inventory"
        convo = AgentConversation.objects.create(user=self.other, title=prompt_title)
        trace = self.trace(self.other, conversation=convo)
        self.span(
            trace,
            kind="tool",
            name="private span",
            input_data={"sentinel": "SPAN-INPUT-SENTINEL"},
            output_data={"sentinel": "SPAN-OUTPUT-SENTINEL"},
        )
        self.viewer.set_config(
            "tables.AgentExecutionTraceTable.columns", ["id", "user", "conversation", "request"], commit=True
        )
        summary_client = client_with_session(self, self.viewer)
        with CaptureQueriesContext(connection) as list_queries:
            rows = summary_client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(
            rows,
            reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk}),
        )
        self.assertContains(rows, "other&lt;&amp;", html=False)
        self.assertNotContains(rows, prompt_title)
        self.assertNotContains(rows, f"?conversation={convo.pk}")
        self.assertContains(rows, reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk}))
        self.assertNotContains(rows, "REQUEST-PAYLOAD-SENTINEL")
        self.assertNotContains(rows, "SPAN-INPUT-SENTINEL")
        self.assertNotContains(rows, "SPAN-OUTPUT-SENTINEL")
        configured_table = AgentExecutionTraceTable([trace], user=self.viewer, exclude=())
        self.assertNotIn("request", configured_table.columns)
        self.assertNotIn("request", AgentExecutionTraceTable([trace], user=None).columns)

        trace_list_sql = [
            query["sql"].lower() for query in list_queries if "agentexecutiontrace" in query["sql"].lower()
        ]
        self.assertTrue(trace_list_sql)
        self.assertTrue(all("request" not in query for query in trace_list_sql))
        hidden_search = summary_client.get(self.list_url, {"q": "PRIVATE-FIRST-PROMPT"}, HTTP_HX_REQUEST="true")
        self.assertEqual(list(hidden_search.context["table"].data), [])

        detail_url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk})
        with CaptureQueriesContext(connection) as queries:
            summary = summary_client.get(detail_url)
        self.assertEqual(summary.status_code, 200)
        sql = " ".join(query["sql"].lower() for query in queries)
        self.assertNotIn("agentexecutionspan", sql)
        trace_reads = [query["sql"].lower() for query in queries if "agentexecutiontrace" in query["sql"]]
        self.assertTrue(trace_reads)
        self.assertTrue(all("request" not in query for query in trace_reads))
        self.assertNotContains(summary, "REQUEST-PAYLOAD-SENTINEL")
        self.assertNotContains(summary, "SPAN-INPUT-SENTINEL")
        self.assertNotContains(summary, "SPAN-OUTPUT-SENTINEL")
        self.assertNotContains(summary, prompt_title)
        self.assertEqual(summary["Cache-Control"], "private, no-store")

        self.viewer.clear_config("tables.AgentExecutionTraceTable.columns", commit=True)
        staff = self.full_access_user("trace-staff")
        full = client_with_session(self, staff).get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(full, "REQUEST-PAYLOAD-SENTINEL")
        detail_responses = (
            client_with_session(self, staff).get(detail_url),
            client_with_session(self, staff).get(detail_url, HTTP_HX_REQUEST="true"),
        )
        for response in detail_responses:
            self.assertNotContains(response, "SPAN-INPUT-SENTINEL")
            self.assertNotContains(response, "SPAN-OUTPUT-SENTINEL")
            self.assertContains(response, "REQUEST-PAYLOAD-SENTINEL")
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(response.context["trace_overview"]["span_count"], 1)
            self.assertIsNone(response.context["selected_span"])

        span = AgentExecutionSpan.objects.get(trace=trace)
        for response in (
            client_with_session(self, staff).get(detail_url, {"span": str(span.run_id)}),
            client_with_session(self, staff).get(detail_url, {"span": str(span.run_id)}, HTTP_HX_REQUEST="true"),
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["selected_span"]["run_id"], span.run_id)
            self.assertContains(response, "SPAN-INPUT-SENTINEL")
            self.assertContains(response, "SPAN-OUTPUT-SENTINEL")
            self.assertContains(response, "private span")

        AgentExecutionTrace.objects.all().delete()
        convo = AgentConversation.objects.create(user=self.other, title="A & <conversation>")
        started = timezone.now().replace(microsecond=0)
        first = self.trace(
            self.other,
            pk=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            conversation=convo,
            started_at=started - timedelta(seconds=1),
            duration=30,
            status="refused",
            request="<script>" + "x" * 105,
        )
        second = self.trace(
            self.other,
            pk=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            conversation=convo,
            started_at=started,
            status="error",
            duration=10,
            request="private-request-marker",
        )
        third = self.trace(
            self.other,
            pk=uuid.UUID("00000000-0000-0000-0000-000000000003"),
            conversation=convo,
            started_at=started + timedelta(seconds=1),
            duration=20,
            request="private-request-marker",
        )
        client = client_with_session(self, self.viewer)
        base = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertNotContains(base, f"?conversation={convo.pk}")
        self.assertNotContains(base, "A &amp; &lt;conversation&gt;", html=False)
        self.assertContains(base, reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": first.pk}))
        self.assertContains(
            base,
            f'<a href="{reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": first.pk})}">1</a>',
            html=True,
        )
        self.assertNotContains(base, "private-request-marker")
        self.assertEqual(
            {row.pk: row.turn_number for row in base.context["table"].data},
            {first.pk: 1, second.pk: 2, third.pk: 3},
        )
        self.assertEqual(
            [
                row.pk
                for row in client.get(self.list_url, {"sort": "started_at"}, HTTP_HX_REQUEST="true")
                .context["table"]
                .data
            ],
            [first.pk, second.pk, third.pk],
        )
        grant(self.viewer, AgentConversation)
        ordinary_rows = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertNotContains(ordinary_rows, convo.get_absolute_url())
        grant(self.viewer, AgentConversation, actions=("viewall",))
        visible_rows = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(visible_rows, convo.get_absolute_url())
        self.assertContains(visible_rows, "A &amp; &lt;conversation&gt;", html=False)
        self.assertContains(client.get(first.get_absolute_url()), "A &amp; &lt;conversation&gt;", html=False)
        filtered = client.get(self.list_url, {"conversation": str(convo.pk), "status": "error"}, HTTP_HX_REQUEST="true")
        self.assertEqual(list(filtered.context["table"].data), [second])
        self.assertEqual(filtered.context["table"].data[0].turn_number, 2)
        for params, expected in (
            ({"user": self.other.username}, {first.pk, second.pk, third.pk}),
            ({"status": "refused"}, {first.pk}),
            ({"started_after": (started + timedelta(milliseconds=500)).isoformat()}, {third.pk}),
            ({"started_before": started.isoformat()}, {first.pk, second.pk}),
            ({"q": "A &"}, set()),
            ({"q": self.other.username}, {first.pk, second.pk, third.pk}),
            ({"q": str(first.pk)}, {first.pk}),
            ({"q": str(first.pk)[-12:]}, {first.pk}),
            ({"q": "  A &  "}, set()),
            ({"q": "   "}, {first.pk, second.pk, third.pk}),
            ({"q": "private-request-marker"}, set()),
        ):
            response = client.get(self.list_url, params, HTTP_HX_REQUEST="true")
            self.assertEqual({row.pk for row in response.context["table"].data}, expected, params)
        inspector = self.full_access_user("trace-list-inspector")
        preview = client_with_session(self, inspector).get(
            self.list_url, {"conversation": str(convo.pk)}, HTTP_HX_REQUEST="true"
        )
        self.assertContains(preview, "&lt;script&gt;" + "x" * 92 + "…", html=False)
        self.assertNotContains(preview, "x" * 93)

        malformed = client.get(self.list_url, {"conversation": "not-a-uuid"}, HTTP_HX_REQUEST="true")
        self.assertEqual(malformed.status_code, 200)

        AgentExecutionTrace.objects.all().delete()
        untitled = AgentConversation.objects.create(user=self.other, title="")
        running = self.trace(self.other, conversation=untitled, status="running", duration=None)
        zero = self.trace(self.other, duration=0)
        elapsed = self.trace(self.other, duration=8344.25)
        rows = client_with_session(self, self.viewer).get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(rows, "Untitled conversation")
        self.assertContains(rows, "Running")
        self.assertContains(rows, "0.00")
        self.assertContains(rows, "Duration (s)")
        self.assertContains(rows, "<td>8.34</td>", html=True)
        self.assertNotContains(rows, "8344.25")
        self.assertIn(str(elapsed.pk), rows.content.decode())
        self.assertContains(rows, "—")
        self.assertIn(str(running.pk), rows.content.decode())
        self.assertIn(str(zero.pk), rows.content.decode())

        AgentExecutionTrace.objects.all().delete()
        fixed = timezone.now() - timedelta(days=1)
        lower = self.trace(self.other, started_at=fixed)
        higher = self.trace(self.other, started_at=fixed)
        latest = self.trace(self.other, started_at=fixed + timedelta(seconds=1))
        client = client_with_session(self, self.viewer)
        response = client.get(self.list_url, {"per_page": 2}, HTTP_HX_REQUEST="true")
        body = response.content.decode()
        first, second = sorted((lower.pk, higher.pk))
        self.assertLess(body.index(str(latest.pk)), body.index(str(first)))
        self.assertNotIn(str(second), body)
        next_page = client.get(self.list_url, {"per_page": 2, "page": 2}, HTTP_HX_REQUEST="true")
        self.assertContains(next_page, str(second))
        self.assertNotContains(next_page, str(first))
        self.assertEqual(client.get(self.list_url, {"export": "csv"}).status_code, 404)

        AgentExecutionTrace.objects.all().delete()
        empty = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(list(empty.context["table"].data), [])

    def test_full_inspection_requires_both_grants_and_explicit_controls_are_forbidden(self):
        trace = self.trace(self.other)
        span = self.span(trace, input_data={"private": True})
        nonstaff_both = User.objects.create_user(username="trace-nonstaff-both")
        grant(nonstaff_both, AgentExecutionTrace)
        grant(nonstaff_both, AgentExecutionSpan)
        staff_without_span = User.objects.create_user(username="trace-staff-no-span", is_staff=True)
        grant(staff_without_span, AgentExecutionTrace)
        url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk})

        for user in (self.viewer, staff_without_span):
            client = client_with_session(self, user)
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["full_access"])
            self.assertNotContains(response, "private")
            with self.subTest(user=user.username):
                query = {"span": str(span.run_id)}
                self.assertEqual(client.get(url, query).status_code, 403)
                self.assertEqual(client.get(url, query, HTTP_HX_REQUEST="true").status_code, 403)

        nonstaff_client = client_with_session(self, nonstaff_both)
        permitted = nonstaff_client.get(url, {"span": str(span.run_id)})
        self.assertEqual(permitted.status_code, 200)
        self.assertTrue(permitted.context["full_access"])
        self.assertEqual(permitted.context["selected_span"]["run_id"], span.run_id)
        self.assertContains(nonstaff_client.get(self.list_url, HTTP_HX_REQUEST="true"), "REQUEST-PAYLOAD-SENTINEL")

    def test_owner_constraints_follow_conversation_for_list_detail_and_counts(self):
        owner = User.objects.create_user(username="trace-owner")
        grant(owner, AgentExecutionTrace, constraints={"conversation__user": "$user"})
        own = self.trace(owner)
        foreign = self.trace(self.other)
        client = client_with_session(self, owner)
        rows = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(rows, str(own.pk))
        self.assertNotContains(rows, str(foreign.pk))
        detail = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": own.pk})
        hidden = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": foreign.pk})
        self.assertEqual(client.get(detail).status_code, 200)
        self.assertEqual(client.get(hidden).status_code, 404)
        counts = AgentConversation.objects.filter(pk__in=(own.conversation_id, foreign.conversation_id))
        self.assertEqual(
            dict(counts.with_trace_counts(owner).values_list("pk", "trace_count")),
            {own.conversation_id: 1, foreign.conversation_id: 0},
        )

    @override_settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": False}})
    def test_trace_permissions_and_object_restrictions(self):  # pylint: disable=too-many-locals
        visible = self.trace(self.other)
        hidden = self.trace(self.other)
        span = self.span(visible, input_data={"visible": True})
        user = User.objects.create_user(username="restricted-trace-staff", is_staff=True)
        grant(user, AgentExecutionTrace, constraints={"id": str(visible.pk)})
        grant(user, AgentExecutionSpan)
        client = client_with_session(self, user)
        url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": visible.pk})
        hidden_url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": hidden.pk})
        self.assertEqual(client.get(url, {"span": str(span.run_id)}).status_code, 200)
        missing_url = reverse(
            "plugins:nautobot_agent:agentexecutiontrace",
            kwargs={"pk": "00000000-0000-0000-0000-000000000001"},
        )
        self.assertEqual(client.get(hidden_url).status_code, 404)
        self.assertEqual(client.get(hidden_url, {"span": str(span.run_id)}).status_code, 404)
        self.assertEqual(client.get(missing_url).status_code, 404)
        self.assertEqual(client.get(missing_url, {"span": str(span.run_id)}).status_code, 404)

        AgentExecutionTrace.objects.all().delete()
        trace = self.trace(self.other)
        denied = User.objects.create_user(username="denied")
        client = client_with_session(self, denied)
        detail_url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk})
        self.assertEqual(client.get(self.list_url, HTTP_HX_REQUEST="true").status_code, 403)
        self.assertEqual(client.get(detail_url).status_code, 403)

        AgentExecutionTrace.objects.all().delete()
        existing = self.trace(self.other)
        grant(self.viewer, AgentExecutionTrace, actions=("view", "add", "change", "delete"))
        client = client_with_session(self, self.viewer)
        response = client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(response, str(existing.pk))
        self.assertNotContains(response, "Add")
        self.assertNotContains(response, "Edit")
        self.assertNotContains(response, "Delete")
        detail_url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": existing.pk})
        for path in (self.list_url, detail_url):
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(path=path, method=method):
                    # Native permission checks may reject an unmapped action before DRF returns 405.
                    self.assertIn(getattr(client, method)(path).status_code, (403, 405))
        for suffix in (
            "add/",
            "import/",
            "bulk-edit/",
            "bulk-delete/",
            f"{existing.pk}/edit/",
            f"{existing.pk}/delete/",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(client.get(f"{self.list_url}{suffix}").status_code, 404)
        self.assertEqual(list(AgentExecutionTrace.objects.values_list("pk", "status")), [(existing.pk, "ok")])

    @override_settings(PLUGINS_CONFIG={"nautobot_agent": {"tracing": False}})
    def test_span_selection_rejects_invalid_or_foreign_values(self):
        trace = self.trace(self.other)
        foreign = self.trace(self.other)
        span = self.span(trace)
        foreign_span = self.span(foreign)
        client = client_with_session(self, self.full_access_user("trace-validation-staff"))
        url = reverse("plugins:nautobot_agent:agentexecutiontrace", kwargs={"pk": trace.pk})

        invalid_queries = (
            {"span": "not-a-uuid"},
            {"span": str(uuid.uuid4())},
            {"span": str(foreign_span.run_id)},
            {"span": [str(span.run_id), str(foreign_span.run_id)]},
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                self.assertEqual(client.get(url, query).status_code, 404)

    def payload_url(self, trace):
        """Return the explicit protected full-content action."""
        return reverse("plugins:nautobot_agent:agentexecutiontrace_payload", kwargs={"pk": trace.pk})

    def read_stream(self, response):
        """Consume the streaming JSON body as the HTTP client would."""
        self.assertTrue(response.streaming)
        return b"".join(response.streaming_content)

    def test_full_json_payload_inline_and_download_share_exact_unicode_encoding(self):
        trace = self.trace(self.other, request='prompt "雪"')
        raw_binary_snapshot = {"type": "bytes", "hex": "0001ff"}
        span = self.span(
            trace,
            input_data={"unicode": "雪 ☃", "binary": raw_binary_snapshot, "records": [{"value": "雪" * 20}] * 3_000},
            output_data={"result": "complete" + "☃" * 70_000},
            error_data={"message": "captured"},
            details={},
            event_counts=[],
        )
        span.refresh_from_db()
        self.assertEqual(span.input_data["binary"], raw_binary_snapshot)
        client = client_with_session(self, self.full_access_user("trace-payload-inspector"))
        endpoint = self.payload_url(trace)
        cases = (
            ({"field": "request"}, 'prompt "雪"'),
            (
                {"field": "input", "span": str(span.run_id)},
                span.input_data,
            ),
            ({"field": "output", "span": str(span.run_id)}, {"result": "complete" + "☃" * 70_000}),
            ({"field": "error", "span": str(span.run_id)}, {"message": "captured"}),
            ({"field": "details", "span": str(span.run_id)}, {}),
            ({"field": "event_counts", "span": str(span.run_id)}, []),
        )
        for params, expected in cases:
            for download in (False, True):
                query = {**params, **({"download": "1"} if download else {})}
                with self.subTest(params=query):
                    response = client.get(endpoint, query)
                    body = self.read_stream(response)
                    self.assertEqual(body, json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode())
                    self.assertEqual(json.loads(body), expected)
                    if params["field"] in ("request", "input"):
                        self.assertIn("雪".encode(), body)
                    self.assertEqual(response["Content-Type"], "application/json")
                    self.assertEqual(response["Cache-Control"], "private, no-store")
                    self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                    disposition, filename = response["Content-Disposition"].split("; ")
                    self.assertEqual(disposition, "attachment" if download else "inline")
                    self.assertTrue(filename.startswith(f'filename="trace-{trace.pk}-'))
                    self.assertTrue(filename.endswith('.json"'))

    def test_payload_contract_rejects_invalid_parameters_membership_and_missing_values_privately(self):  # pylint: disable=too-many-locals
        trace = self.trace(self.other)
        foreign = self.trace(self.other)
        span = self.span(trace, input_data=None)
        foreign_span = self.span(foreign, input_data={"foreign": True})
        client = client_with_session(self, self.full_access_user("trace-payload-contract"))
        endpoint = self.payload_url(trace)
        invalid_urls = (
            f"{endpoint}?field=request&field=input",
            f"{endpoint}?field=request&span={span.run_id}",
            f"{endpoint}?field=capture_report",
            f"{endpoint}?field=capture_report&span={span.run_id}",
            f"{endpoint}?field=input",
            f"{endpoint}?field=input&span=bad",
            f"{endpoint}?field=input&span={foreign_span.run_id}",
            f"{endpoint}?field=input&span={span.run_id}",
            f"{endpoint}?field=input&span={span.run_id}&download=0",
            f"{endpoint}?field=input&span={span.run_id}&download=1&download=1",
            f"{endpoint}?field=input&span={span.run_id}&extra=1",
            f"{endpoint}?field=__dict__&span={span.run_id}",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response["Cache-Control"], "private, no-store")
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        empty_span = self.span(trace, input_data="")
        empty_response = client.get(
            endpoint,
            {"field": "input", "span": str(empty_span.run_id)},
        )
        self.assertEqual(self.read_stream(empty_response), b'""')
        empty_request_trace = self.trace(self.other, request="")
        empty_request = client.get(self.payload_url(empty_request_trace), {"field": "request"})
        self.assertEqual(self.read_stream(empty_request), b'""')
        missing_trace_url = reverse(
            "plugins:nautobot_agent:agentexecutiontrace_payload",
            kwargs={"pk": uuid.uuid4()},
        )
        missing = client.get(missing_trace_url, {"field": "request"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Cache-Control"], "private, no-store")
        self.assertEqual(missing["X-Content-Type-Options"], "nosniff")

    def test_payload_requires_full_grants_and_obeys_trace_object_restrictions(self):
        visible = self.trace(self.other)
        hidden = self.trace(self.other)
        span = self.span(visible, output_data={"visible": True})
        summary_only = client_with_session(self, self.viewer)
        url = self.payload_url(visible)
        denied = summary_only.get(url, {"field": "output", "span": str(span.run_id)})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied["Cache-Control"], "private, no-store")
        self.assertEqual(denied["X-Content-Type-Options"], "nosniff")

        restricted = User.objects.create_user(username="payload-restricted-staff", is_staff=True)
        grant(restricted, AgentExecutionTrace, constraints={"id": str(visible.pk)})
        grant(restricted, AgentExecutionSpan)
        restricted_client = client_with_session(self, restricted)
        allowed = restricted_client.get(url, {"field": "output", "span": str(span.run_id)})
        self.assertEqual(json.loads(self.read_stream(allowed)), {"visible": True})
        hidden_url = self.payload_url(hidden)
        hidden_response = restricted_client.get(hidden_url, {"field": "request"})
        self.assertEqual(hidden_response.status_code, 404)
        self.assertEqual(hidden_response["Cache-Control"], "private, no-store")
        self.assertEqual(hidden_response["X-Content-Type-Options"], "nosniff")
        hidden_report = restricted_client.get(hidden_url, {"field": "capture_report"})
        self.assertEqual(hidden_report.status_code, 404)
        self.assertEqual(hidden_report["Cache-Control"], "private, no-store")
        for method in (restricted_client.post, restricted_client.options):
            unsupported = method(url)
            self.assertEqual(unsupported.status_code, 405)
            self.assertEqual(unsupported["Cache-Control"], "private, no-store")
            self.assertEqual(unsupported["X-Content-Type-Options"], "nosniff")

    def test_payload_server_error_is_private_and_does_not_expose_exception_text(self):
        trace = self.trace(self.other)
        client = client_with_session(self, self.full_access_user("payload-server-error"))
        with (
            patch(
                "nautobot_agent.views.AgentExecutionTraceUIViewSet.get_object",
                side_effect=DatabaseError("private payload"),
            ),
            self.assertLogs("nautobot_agent.views", level="ERROR") as logs,
        ):
            response = client.get(self.payload_url(trace), {"field": "request"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertNotIn(b"private payload", response.content)
        self.assertIn("DatabaseError", "".join(logs.output))
        self.assertNotIn("private payload", "".join(logs.output))
