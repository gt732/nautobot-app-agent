"""Database-side execution limits and tool-thread connection ownership."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock, call, patch

from django.db import connection, transaction
from django.test import SimpleTestCase
from langchain_core.tools import ToolException

from nautobot_agent.data.tool_io import _statement_timeout, execute_data_tool
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.control import RunControlMiddleware
from nautobot_agent.tests.support.db import CommittedTestCase


class MySQLStatementTimeoutTests(SimpleTestCase):
    """Session timeout values and restoration are independent of the configured backend."""

    def test_session_timeout_units_and_restoration_on_success_and_failure(self):
        for mariadb, variable, timeout, previous in (
            (False, "max_execution_time", 2500, 725),
            (True, "max_statement_time", 2.5, 9.5),
        ):
            for failure in (False, True):
                with self.subTest(mariadb=mariadb, failure=failure):
                    cursor = MagicMock()
                    cursor.__enter__.return_value = cursor
                    cursor.fetchone.return_value = (previous,)
                    database = SimpleNamespace(
                        vendor="mysql", mysql_is_mariadb=mariadb, cursor=MagicMock(return_value=cursor)
                    )
                    with (
                        self.settings(PLUGINS_CONFIG={"nautobot_agent": {"query_statement_timeout_ms": 2500}}),
                        patch("nautobot_agent.data.tool_io.connection", database),
                    ):
                        expectation = self.assertRaisesMessage(ValueError, "query failed") if failure else nullcontext()
                        with expectation, _statement_timeout():
                            self.assertEqual(cursor.execute.call_args, call(f"SET SESSION {variable} = %s", [timeout]))
                            if failure:
                                raise ValueError("query failed")
                    self.assertEqual(
                        cursor.execute.call_args_list,
                        [
                            call(f"SELECT @@SESSION.{variable}"),
                            call(f"SET SESSION {variable} = %s", [timeout]),
                            call(f"SET SESSION {variable} = %s", [previous]),
                        ],
                    )


class ToolDatabaseBoundaryTests(CommittedTestCase):
    """Exercise real thread-local connections and server-side cancellation."""

    def test_model_and_tool_calls_release_connections_on_success_failure_and_refusal(self):
        def invoke(call_type, outcome):
            handler_calls = []

            def admit():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                if outcome == "refusal":
                    raise ValueError("refused")

            def handler(_request):
                handler_calls.append("called")
                if outcome == "failure":
                    raise ValueError("failed")
                return "source result"

            request = SimpleNamespace(
                runtime=SimpleNamespace(
                    context=SimpleNamespace(
                        authority=SimpleNamespace(require_new_work=admit),
                        budget=RunBudget(tool_limit=1, model_limit=1),
                    )
                )
            )
            try:
                middleware = RunControlMiddleware()
                call_method = middleware.wrap_model_call if call_type == "model" else middleware.wrap_tool_call
                result = call_method(request, handler)
            except ValueError as exc:
                result = str(exc)
            return result, handler_calls, connection.connection is None

        with ThreadPoolExecutor(max_workers=1) as executor:
            results = list(
                executor.map(
                    invoke,
                    ("model", "model", "model", "tool", "tool", "tool"),
                    ("success", "failure", "refusal", "success", "failure", "refusal"),
                )
            )
        self.assertEqual(
            results,
            [
                ("source result", ["called"], True),
                ("failed", ["called"], True),
                ("refused", [], True),
                ("source result", ["called"], True),
                ("failed", ["called"], True),
                ("refused", [], True),
            ],
        )

    def test_model_and_tool_calls_preserve_caller_atomic_connection(self):
        request = SimpleNamespace(
            runtime=SimpleNamespace(
                context=SimpleNamespace(
                    authority=SimpleNamespace(require_new_work=lambda: None),
                    budget=RunBudget(tool_limit=1, model_limit=1),
                )
            )
        )
        middleware = RunControlMiddleware()

        with transaction.atomic():
            for call_method in (middleware.wrap_model_call, middleware.wrap_tool_call):
                result = call_method(request, lambda _request: "source result")
                self.assertEqual(result, "source result")
                self.assertIsNotNone(connection.connection)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    self.assertEqual(cursor.fetchone(), (1,))
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('statement_timeout', '5s', true)")
                execute_data_tool(
                    lambda: {"value": 1},
                    logger=logging.getLogger(__name__),
                    failure_message="Query against %r failed",
                    model="test",
                )
                with connection.cursor() as cursor:
                    cursor.execute("SHOW statement_timeout")
                    self.assertEqual(cursor.fetchone(), ("5s",))

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL statement timeout regression")
    def test_query_timeout_is_sanitized_and_connection_recovers(self):
        def slow_payload():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(0.05)")
            return {"unexpected": "completed"}

        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {"query_statement_timeout_ms": 5}}),
            self.assertLogs("nautobot_agent.tests.timeout", level="ERROR"),
            self.assertRaises(ToolException) as caught,
        ):
            execute_data_tool(
                slow_payload,
                logger=logging.getLogger("nautobot_agent.tests.timeout"),
                failure_message="Query against %r failed",
                model="test",
            )
        self.assertEqual(
            json.loads(str(caught.exception)),
            {"error": "DatabaseError", "message": "The live-data query could not be completed."},
        )
        self.assertIsNone(connection.connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL invalid regular expression regression")
    def test_invalid_regex_is_repairable_without_database_details_or_error_traceback(self):
        def invalid_payload():
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s ~ %s", ["STORED-VALUE-SENTINEL", "[REGEX-SECRET-SENTINEL"])
                return {"match": cursor.fetchone()[0]}

        with (
            self.assertLogs("nautobot_agent.tests.lookup", level="INFO") as logs,
            self.assertRaises(ToolException) as caught,
        ):
            execute_data_tool(
                invalid_payload,
                logger=logging.getLogger("nautobot_agent.tests.lookup"),
                failure_message="Query against %r failed",
                model="test",
            )
        self.assertEqual(json.loads(str(caught.exception))["error"], "InvalidLookupValue")
        for record in logs.records:
            self.assertEqual(record.levelno, logging.INFO)
            self.assertIsNone(record.exc_info)
        for secret in ("STORED-VALUE-SENTINEL", "REGEX-SECRET-SENTINEL"):
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, "\n".join(logs.output))
        self.assertIsNone(connection.connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))
