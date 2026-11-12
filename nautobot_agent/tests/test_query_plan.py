"""Query execution preserves admission precedence before restricted ORM access."""

import json

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from langchain.tools import ToolException, ToolRuntime
from pydantic import ValidationError

from nautobot_agent.data.query import QueryInput, execute_query
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.data import build_fixtures, restrict_spy

User = get_user_model()


class QueryExecutionAdmissionTests(TestCase):
    """The public query interface rejects invalid requests before root restriction."""

    databases = ("default",)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        build_fixtures()
        cls.query_user = User.objects.create_superuser(username="query-plan")

    def request(self, **arguments):
        """Build a validated tool input carrying the test's trusted principal."""
        runtime = ToolRuntime(
            state={},
            context=AgentContext(
                user_id=str(self.query_user.pk),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
            config={},
            stream_writer=lambda _chunk: None,
            tool_call_id="query-plan",
            store=None,
        )
        return QueryInput.model_validate({"model": "dcim.Location", "runtime": runtime, **arguments})

    def test_admission_rejection_precedence_and_query_free_path(self):
        calls = restrict_spy(self)
        cases = (
            (
                {"filters": {"unknown_field": 1}, "order_by": ["?"], "limit": 1},
                "ValidationError",
                "Random ordering cannot be continued.",
            ),
            (
                {"fields": ["name"], "distinct": True, "order_by": ["pk"], "cursor": "bad"},
                "ValidationError",
                "must name a projected field when distinct is true.",
            ),
        )
        for arguments, category, message in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ToolException) as raised:
                execute_query(self.request(**arguments))
            error = json.loads(str(raised.exception))
            self.assertEqual(error["error"], category)
            self.assertIn(message, error["message"])
        self.assertEqual(calls, [])


class QueryCursorShapeTests(SimpleTestCase):
    """The owning request grammar rejects unsupported cursor shapes before admission."""

    @staticmethod
    def validate(**arguments):
        """Validate a domain request without querying the database."""
        runtime = ToolRuntime(
            state={},
            context=AgentContext(
                user_id="1", authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
            ),
            config={},
            stream_writer=lambda _chunk: None,
            tool_call_id="query-cursor-shape",
            store=None,
        )
        return QueryInput.model_validate({"model": "dcim.Location", "runtime": runtime, **arguments})

    def test_invalid_dependencies_do_not_add_cursor_errors(self):
        for arguments in (
            {"mode": "invalid", "cursor": "dummy"},
            {
                "mode": "aggregate",
                "group_by": ["name", "name"],
                "aggregates": {"total": {"function": "Count", "field": "pk"}},
                "cursor": "dummy",
            },
            {
                "mode": "aggregate",
                "group_by": [1],
                "aggregates": {"total": {"function": "Count", "field": "pk"}},
                "cursor": "dummy",
            },
            {
                "mode": "aggregate",
                "group_by": [],
                "aggregates": {"total": {"function": "Count", "field": "pk"}},
                "having": {"lookups": {"total__gte": 1}},
            },
            {"mode": "list", "group_by": ["name"], "cursor": "dummy"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError) as raised:
                self.validate(**arguments)
            if arguments.get("having") is not None:
                self.assertIn("having is only valid for grouped aggregate queries", str(raised.exception))
            else:
                self.assertNotIn(("cursor",), [error["loc"] for error in raised.exception.errors()])
