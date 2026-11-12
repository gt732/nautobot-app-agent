"""Protocol-level tests for chat streaming and retained-state projection."""

import copy
import json
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.exceptions import ModelNotFoundError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import invalid_tool_call
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.stream import GraphRunStream

from nautobot_agent.chat import chat_stream, history
from nautobot_agent.runtime.agent import read_state
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import RunDraining
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import FakeAgent, display_id, model_message, tool_entry
from nautobot_agent.tools.render_chart import build_chart_config, render_chart

CONFIG: RunnableConfig = {"configurable": {"thread_id": "test"}}
CONTEXT = AgentContext(user_id="1", authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40))
CONTEXT_LIMIT = 100000


def stream(fake_agent):
    """Consume one direct graph protocol turn, snapshotting each projected event."""
    return [
        (event, copy.deepcopy(data))
        for event, data in chat_stream.stream_chat_events(
            fake_agent,
            "hello",
            CONFIG,
            CONTEXT,
            CONTEXT_LIMIT,
            control=SimpleNamespace(drain_requested=False),
            on_cleanup=lambda: None,
        )
    ]


class _NativeState(TypedDict, total=False):
    """Minimal conversation state for a real compiled LangGraph run."""

    messages: Annotated[list, add_messages]
    skills_metadata: Any
    todos: list[dict[str, str]]


class ChatStreamNativeCleanupTests(SimpleTestCase):
    """Exercise cleanup through public GraphRunStream instances from a real compiled graph."""

    def setUp(self):
        self.order = []
        # ty cannot match TypedDict class attributes to LangGraph's schema protocol.
        builder = StateGraph(_NativeState)  # ty: ignore[invalid-argument-type]
        builder.add_node("first", lambda state: {"todos": [{"content": "first", "status": "completed"}]})
        builder.add_node("second", self.second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        original_abort = GraphRunStream.abort

        def observed_abort(native_stream):
            self.order.append("abort")
            original_abort(native_stream)

        abort = patch.object(GraphRunStream, "abort", autospec=True, side_effect=observed_abort)
        abort.start()
        self.addCleanup(abort.stop)

    def second(self, state: _NativeState):
        """Record that the graph advanced past the first step."""
        self.assertEqual(state["todos"][0]["content"], "first")
        self.order.append("second")
        return {"todos": [{"content": "second", "status": "completed"}]}

    def events(self, control):
        """Return the chat event generator over the real graph."""
        return chat_stream.stream_chat_events(
            self.graph,
            "hello",
            CONFIG,
            CONTEXT,
            CONTEXT_LIMIT,
            control=control,
            on_cleanup=lambda: self.order.append("cleanup"),
        )

    @staticmethod
    def todo_contents(events):
        """Return projected todo contents in event order."""
        return [[todo["content"] for todo in data["todos"]] for event, data in events if event == chat_stream.TODOS]

    def test_cancellation_drains_without_done_or_later_graph_steps(self):
        control = SimpleNamespace(drain_requested=False)
        seen = []
        with self.assertRaises(RunDraining):
            for event, data in self.events(control):
                seen.append(event)
                if event == chat_stream.TODOS and data["todos"]:
                    control.drain_requested = True

        self.assertNotIn(chat_stream.DONE, seen)
        self.assertEqual(self.order, ["cleanup", "abort"])
        self.assertEqual(self.graph.get_state(CONFIG).values["todos"][0]["content"], "first")


class ChatStreamTestCase(SimpleTestCase):
    """Pin chat stream events and retained-state projections without HTTP transport."""

    def test_send_streams_tokens_then_done(self):
        """Only non-empty main-model deltas emit tokens, after one message-start per response."""
        fake = FakeAgent(
            parts=[
                ("messages", model_message("model", "Hi", "", "!")),
                ("messages", model_message("model")),
                ("messages", model_message("tools", "tool text")),
                ("messages", model_message("model", " there")),
            ]
        )

        events = stream(fake)

        visible = [(event, data) for event, data in events if event != chat_stream.CONTEXT]
        self.assertEqual(
            visible,
            [
                (chat_stream.MESSAGE_START, {}),
                (chat_stream.TOKEN, {"text": "Hi"}),
                (chat_stream.TOKEN, {"text": "!"}),
                (chat_stream.MESSAGE_START, {}),
                (chat_stream.TOKEN, {"text": " there"}),
                (chat_stream.DONE, {}),
            ],
        )
        self.assertEqual(sum(event == chat_stream.CONTEXT for event, _ in events), 3)
        self.assertNotIn("tool text", json.dumps(events))

    def test_errors_terminate_stream_without_leaks(self):
        """Agent, state, and malformed-history failures retain their type without leaking details."""

        def failing_parts():
            yield "messages", model_message("model", "partial")
            raise RuntimeError("PROVIDER-SECRET-SENTINEL")

        def normalized_parts():
            yield "messages", model_message("model", "partial")
            raise ModelNotFoundError("PROVIDER-SECRET-SENTINEL")

        class UnavailableStateAgent(FakeAgent):
            """Fake whose checkpoint cannot be read."""

            def get_state(self, config):
                raise RuntimeError("PROVIDER-SECRET-SENTINEL")

        malformed = FakeAgent(messages=[HumanMessage("valid"), object()])
        with self.assertRaises(TypeError):
            read_state(malformed, CONFIG)

        rows = (
            (
                "after content",
                FakeAgent(parts=failing_parts()),
                [
                    (chat_stream.MESSAGE_START, {}),
                    (chat_stream.TOKEN, {"text": "partial"}),
                    (chat_stream.CONTEXT, {"used": None, "limit": CONTEXT_LIMIT, "percent": None}),
                ],
                "RuntimeError",
            ),
            (
                "normalized model error",
                FakeAgent(parts=normalized_parts()),
                [
                    (chat_stream.MESSAGE_START, {}),
                    (chat_stream.TOKEN, {"text": "partial"}),
                    (chat_stream.CONTEXT, {"used": None, "limit": CONTEXT_LIMIT, "percent": None}),
                ],
                "model_not_found",
            ),
            ("state unavailable", UnavailableStateAgent(), [], "RuntimeError"),
            ("malformed history", malformed, [], "TypeError"),
        )
        for label, agent, prefix, code in rows:
            with self.subTest(label):
                with self.assertLogs("nautobot_agent.chat_stream", level="ERROR") as logs:
                    events = stream(agent)
                self.assertEqual(events[:-1], prefix)
                self.assertEqual(events[-1], (chat_stream.ERROR, {"error_code": code}))
                self.assertNotIn("PROVIDER-SECRET-SENTINEL", json.dumps(events) + "\n".join(logs.output))
                if label == "after content":
                    diagnostics = "\n".join(logs.output)
                    self.assertIn("RuntimeError", diagnostics)
                    self.assertIn("failing_parts", diagnostics)
                    self.assertNotIn(__file__, diagnostics)

    def test_tool_outcome_restoration(self):  # pylint: disable=too-many-locals
        """Restored outcomes disclose only call inputs; reused native ids pair in order with unambiguous display ids."""
        html_args = {"file_path": "</script><b>x</b>"}
        script_raw = "<img src=x onerror=alert(1)>"

        def repair(name, call_id):
            return (
                f"Tool call {name} with id {call_id} did not complete - no result was recorded. "
                "It may have been cancelled or interrupted."
            )

        calls = [
            {"name": "read_file", "args": html_args, "id": "ok"},
            {"name": "ls", "args": {"path": "/"}, "id": "denied"},
            {"name": "grep", "args": {"pattern": "x", "path": "/nautobot"}, "id": "pending"},
            {"name": "glob", "args": {"pattern": "*.py", "path": "/nautobot"}, "id": "interrupted"},
            {"name": "ls", "args": {"path": "/nautobot"}, "id": "lookalike"},
        ]
        messages = [
            HumanMessage(content="Question", id="human-1"),
            AIMessage(
                content="",
                id="ai-outcomes",
                tool_calls=calls,
                invalid_tool_calls=[invalid_tool_call(name=None, args=script_raw, id=None, error="PARSER-SECRET")],
            ),
            ToolMessage(content="RESULT-SECRET", artifact="ARTIFACT-SECRET", tool_call_id="ok", name="read_file"),
            ToolMessage(
                content="Error: permission denied for ls on /\nERROR-SECRET",
                tool_call_id="denied",
                name="ls",
                status="error",
            ),
            ToolMessage(content=repair("glob", "interrupted"), tool_call_id="interrupted", name="glob", status="error"),
            ToolMessage(content=repair("ls", "other"), tool_call_id="lookalike", name="ls", status="error"),
            ToolMessage(content="ORPHAN-SECRET", tool_call_id="orphan", name="ls"),
            AIMessage(content="Answer", id="ai-2"),
        ]

        projected = history.project_history(messages)

        self.assertEqual(
            projected,
            [
                {"role": "user", "content": "Question"},
                tool_entry(display_id("ai-outcomes", "tool", "ok"), "read_file", html_args, "completed"),
                tool_entry(display_id("ai-outcomes", "tool", "denied"), "ls", {"path": "/"}, "failed"),
                tool_entry(
                    display_id("ai-outcomes", "tool", "pending"),
                    "grep",
                    {"pattern": "x", "path": "/nautobot"},
                    "unknown",
                ),
                tool_entry(
                    display_id("ai-outcomes", "tool", "interrupted"),
                    "glob",
                    {"pattern": "*.py", "path": "/nautobot"},
                    "unknown",
                ),
                tool_entry(display_id("ai-outcomes", "tool", "lookalike"), "ls", {"path": "/nautobot"}, "failed"),
                tool_entry(display_id("ai-outcomes", "invalid", 0), "unknown", script_raw, "failed"),
                {"role": "assistant", "content": "Answer"},
            ],
        )
        payload = json.dumps(projected)
        for hidden in (
            "RESULT-SECRET",
            "ARTIFACT-SECRET",
            "ERROR-SECRET",
            "PARSER-SECRET",
            "ORPHAN-SECRET",
            "did not",
        ):
            self.assertNotIn(hidden, payload)

        first = AIMessage(content="", id="message|tool|first", tool_calls=[{"name": "ls", "args": {}, "id": "call_0"}])
        second = AIMessage(content="", id="message", tool_calls=[{"name": "read_file", "args": {}, "id": "call_0"}])
        entries = history.project_history([first, second])
        self.assertEqual(
            [entry["id"] for entry in entries],
            [display_id("message|tool|first", "tool", "call_0"), display_id("message", "tool", "call_0")],
        )

    def test_chart_projection_live_and_restored(self):  # pylint: disable=too-many-locals
        """Charts come from captured live arguments once, restore identically, and only for valid successes."""
        arguments = {
            "chart_type": "bar",
            "title": "Inventory",
            "orientation": "horizontal",
            "stacked": True,
            "x_label": "Count",
            "y_label": "Device",
            "data": {"Current": {"Routers": 3, "Switches": -2}, "Planned": {"Routers": -1, "Other": 4}},
        }
        retained = [
            AIMessage(
                content="",
                id="ai-retained-chart",
                tool_calls=[{"name": "render_chart", "args": arguments, "id": "call_0"}],
            ),
            render_chart.invoke({"name": "render_chart", "args": arguments, "id": "call_0", "type": "tool_call"}),
        ]
        call = {"name": "render_chart", "args": arguments.copy(), "id": "call_0"}
        request = AIMessage(content="", id="ai-live-chart", tool_calls=[call])
        result = render_chart.invoke({**call, "type": "tool_call"})

        def parts():
            yield "values", {"messages": [*retained, request]}
            call["args"] = {"chart_type": "invalid"}
            yield "values", {"messages": [*retained, request, result]}
            yield "values", {"messages": [*retained, request, result]}

        events = stream(FakeAgent(messages=retained, parts=parts()))

        calls = [data for event, data in events if event == chat_stream.TOOL_CALL]
        expected = build_chart_config(arguments)
        self.assertEqual(
            [call["id"] for call in calls],
            [display_id("ai-live-chart", "tool", "call_0")] * 2,
        )
        self.assertEqual([call["status"] for call in calls], ["running", "completed"])
        self.assertNotIn("chart", calls[0])
        self.assertEqual(calls[1]["args"], arguments)
        self.assertEqual(calls[1]["chart"], expected)
        live_config = calls[1]["chart"]
        self.assertEqual(live_config["xAxis"]["type"], "value")
        self.assertEqual(live_config["yAxis"]["data"], ["Routers", "Switches", "Other"])
        self.assertEqual([item["data"] for item in live_config["series"]], [[3, -2, None], [-1, None, 4]])
        self.assertTrue(all(item["stackStrategy"] == "samesign" for item in live_config["series"]))

        saved_arguments = {
            "chart_type": "bar",
            "title": "Inventory",
            "orientation": "horizontal",
            "stacked": True,
            "data": {"Devices": {"May": 2, "April": -1}},
        }
        second_arguments = {
            "chart_type": "bar",
            "title": "Latest inventory",
            "orientation": "horizontal",
            "stacked": True,
            "data": {"Devices": {"May": 4, "April": -2}},
        }
        saved = [
            HumanMessage(content="Chart this", id="human-chart"),
            AIMessage(
                content="",
                id="ai-saved-chart",
                tool_calls=[{"name": "render_chart", "args": saved_arguments, "id": "call_0"}],
            ),
            ToolMessage(content="done", tool_call_id="call_0", name="render_chart"),
            AIMessage(
                content="",
                id="ai-saved-chart-again",
                tool_calls=[{"name": "render_chart", "args": second_arguments, "id": "call_0"}],
            ),
            ToolMessage(content="done", tool_call_id="call_0", name="render_chart"),
        ]
        restored = history.project_history(saved)

        self.assertEqual(len(restored), 3)
        self.assertEqual([entry["status"] for entry in restored[1:]], ["completed", "completed"])
        self.assertEqual(
            [entry["id"] for entry in restored[1:]],
            [
                display_id("ai-saved-chart", "tool", "call_0"),
                display_id("ai-saved-chart-again", "tool", "call_0"),
            ],
        )
        self.assertEqual(
            [entry["chart"] for entry in restored[1:]],
            [build_chart_config(saved_arguments), build_chart_config(second_arguments)],
        )
        restored_config = restored[1]["chart"]
        self.assertEqual(restored_config["series"][0]["data"], [2, -1])
        self.assertTrue(all(item["stack"] == "raw" for item in restored_config["series"]))

        valid = {"chart_type": "pie", "title": "Inventory", "data": {"Devices": {"Routers": 1}}}
        unsuccessful_calls = [
            {"name": "render_chart", "args": valid, "id": "failed"},
            {"name": "render_chart", "args": valid, "id": "running"},
            {"name": "render_chart", "args": {"chart_type": "pie"}, "id": "invalid-input"},
        ]
        unsuccessful = [
            HumanMessage(content="Chart this", id="human-failed-chart"),
            AIMessage(
                content="",
                id="ai-failed-chart",
                tool_calls=unsuccessful_calls,
                invalid_tool_calls=[invalid_tool_call(name="render_chart", args='{"chart_type":"pie"}', id="invalid")],
            ),
            ToolMessage(content="failure", tool_call_id="failed", name="render_chart", status="error"),
            render_chart.invoke({**unsuccessful_calls[2], "type": "tool_call"}),
        ]

        restored = history.project_history(unsuccessful)

        unsuccessful_entries = restored[1:]
        self.assertEqual([entry["status"] for entry in unsuccessful_entries], ["failed", "unknown", "failed", "failed"])
        self.assertTrue(all("chart" not in entry for entry in unsuccessful_entries))

        invalid_call = {"name": "render_chart", "args": {"chart_type": "pie"}, "id": "invalid-live"}
        request = AIMessage(content="", id="ai-invalid-live-chart", tool_calls=[invalid_call])
        result = render_chart.invoke({**invalid_call, "type": "tool_call"})
        invalid_parts = [
            ("values", {"messages": [request, result]}),
            ("values", {"messages": [request, result]}),
        ]

        events = stream(FakeAgent(parts=invalid_parts))

        failed = [data for event, data in events if event == chat_stream.TOOL_CALL]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["status"], "failed")
        self.assertNotIn("chart", failed[0])
