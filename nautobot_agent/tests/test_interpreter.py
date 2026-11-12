"""Interpreter and production-graph coverage for Python execution and output bounds."""

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.urls import reverse
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from nautobot.dcim.models import Location
from pydantic_monty import NOT_HANDLED

from nautobot_agent.app_settings import InterpreterLimits
from nautobot_agent.runtime.control import RunDraining
from nautobot_agent.tests.support.chat import (
    override_agent_settings,
    page_json,
    parse_run_events,
    post_run_message,
    run_dependencies,
    session_config,
    tool_call_message,
)
from nautobot_agent.tests.support.data import build_fixtures, grant
from nautobot_agent.tests.support.graph import SourceGraphTestCase
from nautobot_agent.tools.monty.bridge import invoke_query_tool
from nautobot_agent.tools.monty.driver import _os_payload, run_eval_python
from nautobot_agent.tools.monty.middleware import InterpreterMiddleware
from nautobot_agent.tools.monty.results import _tagged, jsonable_output


class InterpreterLiveDataTests(SourceGraphTestCase):  # pylint: disable=nb-incorrect-base-class,too-many-ancestors
    """Interpreter and graph executor connections read committed users, grants, and query fixtures."""

    def setUp(self):
        super().setUp()
        self.data = build_fixtures()
        self.restricted_user = get_user_model().objects.create_user(username="interpreter-restricted")
        grant(self.restricted_user, Location, constraints={"name__startswith": "Visible"})
        self.restricted_client = self.client_class()
        self.restricted_client.force_login(self.restricted_user)

    def test_eval_python_query_uses_restricted_user_and_streams_only_outer_activity(self):
        code = 'query_records(model="dcim.Location", mode="count")\nquery_records(model="dcim.Location", mode="count")'
        Location.objects.bulk_create(
            [
                Location(name=f"Visible page {index}", location_type=self.data.campus, status=self.data.active)
                for index in range(15)
            ]
        )
        paging_code = (
            "cursor = None\ncount = 0\npages = 0\n"
            "while True:\n"
            '    page = query_records(model="dcim.Location", fields=["pk"], order_by=["pk"], limit=5, cursor=cursor)\n'
            '    count += page["count"]\n'
            "    pages += 1\n"
            '    cursor = page["next_cursor"]\n'
            "    if cursor is None:\n"
            "        break\n"
            '{"count": count, "pages": pages}\n'
        )
        graph = self.start_graph(
            [
                tool_call_message("eval_python", {"code": code}, "restricted-count"),
                AIMessage(content="Counted."),
                tool_call_message("eval_python", {"code": paging_code}, "paged-count"),
                AIMessage(content="Paged visible records."),
            ]
        )

        with run_dependencies(), override_agent_settings(chat_max_tool_calls=1):
            response = post_run_message(self.restricted_client, "Count visible locations.")
        events = parse_run_events(response)
        results = [
            message
            for request in self.scripted_model.requests
            for message in request
            if isinstance(message, ToolMessage) and message.tool_call_id == "restricted-count"
        ]

        self.assertEqual(len(results), 1)
        self.assertEqual(json.loads(results[0].content)["result"]["count"], 17)
        activity = [data for event, data in events if event == "tool_call"]
        self.assertEqual([entry["name"] for entry in activity], ["eval_python", "eval_python"])
        self.assertTrue(all(entry["args"] == {"code": code} for entry in activity))
        messages = graph.get_state(session_config(self.restricted_client)).values["messages"]
        request = next(message for message in messages if isinstance(message, AIMessage) and message.tool_calls)
        projected_id = json.dumps([request.id, "tool", "restricted-count"], separators=(",", ":"))
        transcript = page_json(self.restricted_client.get(reverse("plugins:nautobot_agent:agent")), "agent-state")[
            "history"
        ]
        self.assertIn(
            {
                "role": "tool",
                "id": projected_id,
                "name": "eval_python",
                "args": {"code": code},
                "status": "completed",
            },
            transcript,
        )
        self.send_message("Summarize visible records.", self.restricted_client)
        result = self.tool_results(self.saved_messages(graph, self.restricted_client))["paged-count"]
        self.assertEqual(json.loads(result.content)["result"], {"count": 17, "pages": 4})

    def test_direct_and_interpreter_adapters_enforce_their_own_caps(self):
        code = 'query_records(model="dcim.Location", limit=1001)'
        graph = self.start_graph(
            [
                tool_call_message("eval_python", {"code": code}, "interpreter-over-limit"),
                tool_call_message("query_records", {"model": "dcim.Location", "limit": 101}, "direct-over-limit"),
                AIMessage(content="Both invalid page requests were rejected."),
            ]
        )

        self.send_message("Check the page limits.", self.restricted_client)
        results = self.tool_results(self.saved_messages(graph, self.restricted_client))
        self.assertIn("error", json.loads(results["interpreter-over-limit"].content))
        self.assertEqual(results["direct-over-limit"].status, "error")
        self.assertNotIn("results", json.loads(results["direct-over-limit"].content))

    def test_failures_propagate_without_partial_summary(self):
        Location.objects.bulk_create(
            [
                Location(
                    name=f"Visible large {index:03d} " + ("x" * 60),
                    location_type=self.data.campus,
                    status=self.data.active,
                )
                for index in range(15)
            ]
        )
        count = 'query_records(model="dcim.Location", mode="count")\n'
        scripts = {
            "denied": 'query_records(model="dcim.Device", mode="count")\n{"complete": True}\n',
            "oversized-page": (
                'query_records(model="dcim.Location", filters={"name__startswith": "Visible large"}, '
                'fields=["name"], limit=40)\n{"complete": True}\n'
            ),
            "over-budget": count + count + '{"complete": True}\n',
            "invalid-shape": (
                'query_records(model="dcim.MissingModel", mode="count", cursor="private-cursor-token")\n'
            ),
        }
        config = {"query": {"max_page_bytes": 1024}, "interpreter": {"max_host_calls": 1}}
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": config}):
            graph = self.start_graph(
                [tool_call_message("eval_python", {"code": code}, call_id) for call_id, code in scripts.items()]
                + [AIMessage(content="None of the analyses completed.")]
            )
            self.send_message("Run the failing analyses.", self.restricted_client)

        results = self.tool_results(self.saved_messages(graph, self.restricted_client))
        for call_id in scripts:
            with self.subTest(call=call_id):
                payload = json.loads(results[call_id].content)
                self.assertIn("error", payload)
                self.assertIsNone(payload["result"])
                if call_id == "invalid-shape":
                    observation = json.loads(payload["error"]["message"])
                    self.assertEqual(observation["error"], "ValidationError")
                    self.assertEqual([detail["loc"] for detail in observation["details"]], [["cursor"]])
                    self.assertIn("guidance", observation["details"][0])
                    self.assertNotIn("private-cursor-token", payload["error"]["message"])
                    self.assertNotIn("MissingModel", payload["error"]["message"])


class InterpreterOutputLimitGraphTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Retain representative full-graph coverage for successful result bounding."""

    def test_oversized_eval_python_result_is_a_valid_bounded_envelope(self):
        limit = 1000
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"interpreter": {"max_output_chars": limit}}}):
            graph = self.start_graph(
                [
                    tool_call_message("eval_python", {"code": '"x" * 2000'}, "long-result"),
                    AIMessage(content="Summarized."),
                ]
            )
            state, _ = self.invoke(graph, "Summarize a computed result.")

        results = [message for message in state["messages"] if isinstance(message, ToolMessage)]
        self.assertEqual(len(results), 1)
        content = results[0].content
        payload = json.loads(content)
        self.assertEqual(payload["error"]["type"], "OutputLimitExceeded")
        self.assertLessEqual(len(content), limit)
        cases = {
            "TypeCheckError": 'result = 1 + "wrong"\n' + ("# extended source\n" * 500),
            "ValueError": 'raise ValueError("diagnostic " * 1000)',
        }
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"interpreter": {"max_output_chars": limit}}}):
            for error_type, code in cases.items():
                with self.subTest(error=error_type):
                    result = self._invoke_interpreter(code, error_type)
                    payload = json.loads(result.content)
                    self.assertLessEqual(len(result.content), limit)
                    self.assertEqual(payload["error"]["type"], error_type)
                    self.assertTrue(payload["error"]["message"])
                    self.assertTrue(payload["error"]["traceback"])
                    self.assertIsNone(payload["attempted_code"])
                    self.assertEqual(payload["stdout"], "")

    def _invoke_interpreter(self, code, call_id):
        middleware = InterpreterMiddleware()
        tool_call = {
            "name": "eval_python",
            "args": {"code": code},
            "id": call_id,
            "type": "tool_call",
        }
        message = AIMessage(content="", tool_calls=[tool_call])
        # ty cannot match TypedDict class attributes to LangGraph's schema protocol.
        graph = StateGraph(MessagesState)  # ty: ignore[invalid-argument-type]
        graph.add_node(
            "tools",
            ToolNode(list(middleware.tools), wrap_tool_call=middleware.wrap_tool_call),
        )
        graph.add_edge(START, "tools")
        state = graph.compile().invoke({"messages": [message]})
        return state["messages"][-1]


def _runtime():
    return ToolRuntime(
        state={},
        context="trusted-context",
        config={"configurable": {"marker": "trusted-config"}},
        stream_writer=lambda _chunk: None,
        tool_call_id="outer",
        store=None,
    )


def _query_tool(behavior=None):
    seen = []

    def query(model: str, runtime: ToolRuntime, config: RunnableConfig, limit: int = 10, mode: str = "list"):
        """Query records."""
        seen.append((model, limit, mode, runtime.context, config["configurable"]["marker"]))
        if behavior is not None:
            return behavior()
        return json.dumps({"results": [model, limit, mode]})

    return StructuredTool.from_function(query, name="query"), seen


_MONTY_LIMITS = InterpreterLimits(
    max_duration_secs=30,
    max_memory_bytes=256 * 1024 * 1024,
    max_host_calls=3,
    max_output_chars=10000,
)


def _run_monty(code, tool, limits=_MONTY_LIMITS):
    return run_eval_python(code, limits, _runtime(), tool)


def _assert_monty_recovers(test, tool):
    outcome = _run_monty("sum(i * i for i in range(10))", tool)
    test.assertIsNone(outcome["error"])
    test.assertEqual(outcome["result"], 285)


class MontyExecutionTests(SimpleTestCase):
    """Preserve sandbox execution, trusted query access, and isolation behavior."""

    def test_compute_stdout_query_and_trusted_runtime(self):
        tool, seen = _query_tool()
        outcome = _run_monty(
            'print("hello")\nrows = query("dcim.Device", 7, mode="count")\nrows["results"][1] * 2', tool
        )

        self.assertEqual(outcome["result"], 14)
        self.assertEqual(outcome["stdout"], "hello\n")
        self.assertIsNone(outcome["error"])
        self.assertEqual(seen, [("dcim.Device", 7, "count", "trusted-context", "trusted-config")])
        result = invoke_query_tool(
            tool,
            args=("dcim.Device", 9),
            kwargs={
                "mode": "list",
                "runtime": "forged-runtime",
                "config": {"configurable": {"marker": "forged-config"}},
                "tool_call_id": "forged-call",
                "context": "forged-context",
                "state": {"user_id": "attacker"},
                "store": "forged-store",
                "stream_writer": "forged-writer",
            },
            runtime=_runtime(),
        )
        self.assertEqual(cast(Any, result)["return_value"], {"results": ["dcim.Device", 9, "list"]})
        self.assertEqual(seen[-1], ("dcim.Device", 9, "list", "trusted-context", "trusted-config"))

    def test_errors_control_exceptions_and_isolated_calls(self):
        tool, _ = _query_tool()
        for code, error_type in (("1 / 0", "ZeroDivisionError"), ("query(1)", "TypeCheckError")):
            with self.subTest(code=code):
                outcome = _run_monty(code, tool)
                self.assertIsNone(outcome["result"])
                self.assertEqual(outcome["error"]["type"], error_type)
                self.assertEqual(outcome["attempted_code"], code)

        self.assertEqual(_run_monty("leaked = 41\nleaked", tool)["result"], 41)
        self.assertEqual(_run_monty("eval('leaked')", tool)["error"]["type"], "NameError")
        for exception in (GraphInterrupt(), RunDraining("draining"), SoftTimeLimitExceeded()):
            with self.subTest(exception=type(exception).__name__):

                def fail(exception=exception):
                    raise exception

                tool, _ = _query_tool(fail)
                with self.assertRaises(type(exception)):
                    _run_monty("query('dcim.Device')", tool)

    def test_resource_exhaustion_returns_errors_and_worker_recovers(self):
        tool, seen = _query_tool()
        cases = (
            ("while True:\n    pass", replace(_MONTY_LIMITS, max_duration_secs=1), "TimeoutError"),
            (
                "chunks = []\nwhile True:\n    chunks.append('x' * 100000)",
                replace(_MONTY_LIMITS, max_memory_bytes=8 * 1024 * 1024),
                "MemoryError",
            ),
        )
        for code, limits, error_type in cases:
            with self.subTest(error=error_type):
                outcome = _run_monty(code, tool, limits)
                self.assertIsNone(outcome["result"])
                self.assertEqual(outcome["error"]["type"], error_type)
                self.assertEqual(outcome["attempted_code"], code)
                self.assertEqual(seen, [])
        _assert_monty_recovers(self, tool)
        seen.clear()
        code = "[fn('dcim.Device') for fn in [query] * 5]"
        outcome = _run_monty(code, tool)
        self.assertEqual(len(seen), _MONTY_LIMITS.max_host_calls)
        self.assertEqual(outcome["error"]["type"], "IterationBudgetExceeded")
        self.assertEqual(outcome["attempted_code"], code)
        self.assertIsNone(outcome["result"])

    def test_host_file_and_environment_are_inaccessible(self):
        tool, seen = _query_tool()
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "host-sentinel.txt"
            sentinel.write_text("host-file-sentinel", encoding="utf-8")
            outcome = _run_monty(
                f"from pathlib import Path\nPath({str(sentinel)!r}).exists()",
                tool,
            )
        self.assertIsNone(outcome["error"])
        self.assertFalse(outcome["result"])

        with mock.patch.dict(os.environ, {"NAUTOBOT_AGENT_TEST_SENTINEL": "host-env-sentinel"}):
            outcome = _run_monty(
                "import os\n[os.getenv('NAUTOBOT_AGENT_TEST_SENTINEL'), len(os.environ)]",
                tool,
            )
        self.assertIsNone(outcome["error"])
        self.assertEqual(outcome["result"], [None, 0])
        self.assertEqual(seen, [])

    def test_rich_values_are_jsonable(self):
        class Unknown:  # pylint: disable=too-few-public-methods
            """Value without a JSON encoding."""

            def __repr__(self):
                return "unknown-value"

        cases = (
            ((1, "two"), {"$tuple": [1, "two"]}),
            (frozenset({"a"}), {"$frozenset": ["a"]}),
            ({"a"}, {"$set": ["a"]}),
            (b"\x00\xff", {"$bytes": "AP8="}),
            ({1: "one"}, {"$dict": [[1, "one"]]}),
            (
                SimpleNamespace(name="record", attributes={"items": (1, 2)}),
                {"$object": {"name": "record", "attributes": {"items": {"$tuple": [1, 2]}}}},
            ),
            (Unknown(), "unknown-value"),
        )
        for value, expected in cases:
            with self.subTest(value=type(value).__name__):
                self.assertEqual(_tagged(value), expected)
        self.assertEqual(
            _tagged({"values": [(1, 2)], "plain": [True]}),
            {"values": [{"$tuple": [1, 2]}], "plain": [True]},
        )
        mixed = {"values": [(1, 2)], "plain": [True], "unknown": Unknown()}
        self.assertEqual(
            jsonable_output(mixed),
            {
                "values": [{"$tuple": [1, 2]}],
                "plain": [True],
                "unknown": "unknown-value",
            },
        )

    def test_bridge_rejects_invalid_arguments_and_tool_errors(self):
        tool, seen = _query_tool()
        for args, kwargs in ((("a", 1, "list", "extra"), {}), (("a",), {"model": "b"})):
            with self.subTest(args=args, kwargs=kwargs):
                payload = cast(Any, invoke_query_tool(tool, args, kwargs, _runtime()))
                self.assertIsInstance(payload["exception"], TypeError)
        error_tool, _ = _query_tool(lambda: ToolMessage(content="query failed", tool_call_id="inner", status="error"))
        payload = cast(Any, invoke_query_tool(error_tool, ("dcim.Device",), {}, _runtime()))
        self.assertIsInstance(payload["exception"], RuntimeError)
        self.assertEqual(str(payload["exception"]), "query failed")
        command_tool, _ = _query_tool(lambda: Command(update={"messages": []}))
        payload = cast(Any, invoke_query_tool(command_tool, ("dcim.Device",), {}, _runtime()))
        self.assertIn("Graph-state updates", str(payload["exception"]))
        self.assertEqual(seen, [])

    def test_os_handler_exception_and_not_handled(self):
        progress = cast(Any, SimpleNamespace(function_name="getenv", args=("SECRET",), kwargs={}))

        def fail(**_kwargs) -> Any:
            raise PermissionError("host access denied")

        exception_payload = cast(Any, _os_payload(progress, cast(Any, fail)))
        self.assertIsInstance(exception_payload["exception"], PermissionError)
        self.assertIsNone(_os_payload(progress, cast(Any, lambda **_kwargs: NOT_HANDLED)))
