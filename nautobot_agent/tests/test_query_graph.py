"""Transactional production-graph coverage for trusted query and trace context propagation.

These cases need committed rows visible to executor threads; graph checkpoints use the in-memory saver. The
transaction-wrapped tool behavior cases live in ``test_query_records`` and ``test_trace_relationships``.
"""

from deepagents.backends.utils import create_file_data
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from langchain_core.messages import AIMessage, ToolMessage
from nautobot.dcim.models import Device
from nautobot.users.models import ObjectPermission

from nautobot_agent.data.access import BLOCKED_MODEL_MESSAGE
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import (
    INTERNAL_SENTINEL,
    parse_run_events,
    post_run_message,
    run_dependencies,
    tool_call_message,
)
from nautobot_agent.tests.support.data import (
    build_fixtures,
    call_query,
    call_trace,
    create_principals,
    failed,
    grant,
    list_names,
    succeeded,
)
from nautobot_agent.tests.support.db import CommittedTestCase
from nautobot_agent.tests.support.graph import SourceGraphTestCase


class QueryConnectionLifecycleTests(CommittedTestCase):
    """Committed fixtures, real database failures, and tool-thread connection release outside any atomic block."""

    def setUp(self):
        ContentType.objects.clear_cache()
        super().setUp()
        self.user = get_user_model().objects.create_user(username="query-connection")
        self.data = build_fixtures()
        self.visible_user, _ = create_principals(self.user)

    def test_invalid_lookup_values_are_sanitized_and_tool_stays_usable(self):
        for tool, call in (("query", call_query), ("trace", call_trace)):
            with self.subTest(tool=tool):
                private_pattern = "[REGEX-SECRET-SENTINEL"
                message = call(
                    {"model": "dcim.Location", "filters": {"name__regex": private_pattern}}, self.visible_user
                )

                self.assertEqual(failed(self, message)["error"], "InvalidLookupValue")
                self.assertNotIn(private_pattern, message.content)
                self.assertIsNone(connection.connection)
                self.assertEqual(
                    list_names(self, self.visible_user, {"order_by": ["name"]}), ["Visible Alpha", "Visible Beta"]
                )


class QueryGraphTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """The production graph and real view hand each request's principal to the tool, main and delegate.

    Transactional because the query tools read committed rows from executor threads; checkpoints use the base class's
    in-memory saver.
    """

    def setUp(self):
        ContentType.objects.clear_cache()
        super().setUp()
        build_fixtures()
        self.visible_user, self.hidden_user = create_principals(self.user)
        grant(self.visible_user, Device, constraints={"location__name__startswith": "Visible"})
        grant(self.hidden_user, Device, constraints={"location__name__startswith": "Hidden"})
        self.hidden_client = self.client_class()
        self.hidden_client.force_login(self.hidden_user)

    def run_context(self, user):
        """Return the trusted run context for a user."""
        return AgentContext(
            user_id=str(user.pk), authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
        )

    def tool_result(self, call_id):
        """Return the most recent model-visible tool result for a call id."""
        for request in reversed(self.scripted_model.requests):
            for message in request:
                if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
                    return message
        raise AssertionError(f"No model request carried a result for {call_id}")

    def stream(self, text, client):
        """Submit one durable run and return its parsed JSON events."""
        with run_dependencies():
            response = post_run_message(client, text)
        self.assertEqual(response.status_code, 200, response.content)
        return parse_run_events(response)

    def test_main_graph_keeps_each_users_results_isolated_and_refreshes_permissions(self):
        script = [
            tool_call_message("query_records", {"model": "dcim.Location", "fields": ["name"]}, "first-query"),
            AIMessage(content="First."),
            tool_call_message("query_records", {"model": "dcim.Location", "fields": ["name"]}, "second-query"),
            AIMessage(content="Second."),
            tool_call_message("query_records", {"model": "dcim.Location", "fields": ["name"]}, "revoked-query"),
            AIMessage(content="Revoked."),
        ]
        self.start_graph(script)

        self.stream("List locations.", self.client)
        visible_requests = self.scripted_model.request_text()
        hidden_start = len(self.scripted_model.requests)
        self.stream("List locations.", self.hidden_client)
        hidden_requests = "\n".join(
            message.text for request in self.scripted_model.requests[hidden_start:] for message in request
        )
        ObjectPermission.objects.filter(users=self.visible_user, object_types__model="location").delete()
        self.stream("List locations again.", self.client)

        self.assertEqual(
            [row["name"] for row in succeeded(self, self.tool_result("first-query"))["results"]],
            ["Visible Alpha", "Visible Beta"],
        )
        self.assertEqual(
            [row["name"] for row in succeeded(self, self.tool_result("second-query"))["results"]],
            ["Hidden Gamma"],
        )
        denied = failed(self, self.tool_result("revoked-query"), "PermissionDenied")
        self.assertIn("dcim.view_location", denied["message"])
        self.assertNotIn("Hidden Gamma", visible_requests)
        for name in ("Visible Alpha", "Visible Beta"):
            self.assertNotIn(name, hidden_requests)
        self.assert_nothing_outside_reached_the_model()

    def test_delegate_receives_policy_denial_then_can_query_permitted_parent(self):
        script = [
            tool_call_message(
                "task",
                {"description": "Inspect inventory and summarize it.", "subagent_type": "general-purpose"},
                "task",
            ),
            tool_call_message(
                "query_records", {"model": "dcim.Device", "fields": ["location__name"]}, "blocked-detail"
            ),
            tool_call_message(
                "query_records", {"model": "dcim.Device", "fields": ["name", "location"]}, "permitted-device"
            ),
            AIMessage(content="The permitted parent query completed."),
            AIMessage(content="Done."),
        ]
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": ["dcim.Location"]}}):
            self.start_graph(script)
            self.stream("Inspect inventory.", self.client)

        denial = failed(self, self.tool_result("blocked-detail"), "PermissionDenied")
        self.assertEqual(denial["message"], BLOCKED_MODEL_MESSAGE)
        rows = succeeded(self, self.tool_result("permitted-device"))["results"]
        self.assertEqual(
            [(row["name"], row["location"]["display"]) for row in rows],
            [("edge-1", "Visible Alpha")],
        )
        self.assert_nothing_outside_reached_the_model()

    def test_runtime_identity_cannot_be_forged_or_missing(self):  # pylint: disable=too-many-locals
        hidden_id = str(self.hidden_user.pk)
        forged = {"model": "dcim.Location", "fields": ["name"], "runtime": {"context": {"user_id": hidden_id}}}
        script = [
            tool_call_message("query_records", {"model": "dcim.Location", "user_id": hidden_id}, "extra-id"),
            tool_call_message("query_records", forged, "forged"),
            tool_call_message("query_records", {"user_id": hidden_id}, "invalid"),
            AIMessage(content="Done."),
        ]
        graph = self.start_graph(script)
        state_sentinel = {"/large_tool_results/seeded.txt": create_file_data(INTERNAL_SENTINEL)}

        state, _ = self.invoke(graph, context=self.run_context(self.visible_user), files=state_sentinel)

        results = self.tool_results(state["messages"])
        trusted_id = str(self.visible_user.pk)
        extra = failed(self, results["extra-id"], "ValidationError")
        self.assertEqual([(d["loc"], d["type"]) for d in extra["details"]], [(["user_id"], "extra_forbidden")])
        overwritten = succeeded(self, results["forged"])
        self.assertEqual(sorted(row["name"] for row in overwritten["results"]), ["Visible Alpha", "Visible Beta"])
        invalid = failed(self, results["invalid"], "ValidationError")
        self.assertEqual(
            sorted((tuple(d["loc"]), d["type"]) for d in invalid["details"]),
            [(("model",), "missing"), (("user_id",), "extra_forbidden")],
        )
        for call_id in ("extra-id", "forged", "invalid"):
            text = results[call_id].text
            for leaked in (trusted_id, hidden_id, INTERNAL_SENTINEL, "Hidden Gamma"):
                self.assertNotIn(leaked, text)
        self.assertNotIn(trusted_id, self.scripted_model.request_text())
        self.assert_nothing_outside_reached_the_model()

        # Missing authority stops the graph before even the model can request a query.
        requests_before = len(self.scripted_model.requests)
        with self.assertRaises(AttributeError):
            self.invoke(graph, context=None)
        self.assertEqual(len(self.scripted_model.requests), requests_before)
