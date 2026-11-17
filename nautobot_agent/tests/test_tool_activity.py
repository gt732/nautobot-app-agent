"""Main-agent tool-call visibility through the production graph and durable events."""

import json
from unittest.mock import patch

from deepagents.backends.utils import create_file_data
from django.urls import reverse
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.tool import invalid_tool_call

from nautobot_agent.chat.conversation import CONVERSATION_KEY
from nautobot_agent.models import AgentConversation
from nautobot_agent.sources.backends import NautobotSourceBackend
from nautobot_agent.tests.support.chat import (
    INTERNAL_SENTINEL,
    display_id,
    open_new_conversation,
    page_json,
    post_run_message,
    recorded_run_events,
    run_dependencies,
    session_config,
    tool_call_message,
    tool_entry,
)
from nautobot_agent.tests.support.graph import SourceGraphTestCase
from nautobot_agent.tests.support.sources import MODELS_LINES

PAGE_URL = "plugins:nautobot_agent:agent"


def request_display_ids(messages):
    """Map each unique native call ID in this fixture to its expected message-scoped display ID."""
    return {
        call["id"]: display_id(message.id, "tool", call["id"])
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }


def call_statuses(events):
    """Return each streamed tool call ID with its statuses in arrival order."""
    statuses = {}
    for event, data in events:
        if event == "tool_call":
            statuses.setdefault(data["id"], []).append(data["status"])
    return statuses


class ToolActivityGraphTestCase(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Durable admission commits before graph executor threads stream and restore tool activity."""

    def send(self, text, client=None):
        """Submit one durable run and return its parsed JSON events."""
        client = client or self.client
        with run_dependencies(), recorded_run_events() as events:
            post_run_message(client, text)
            return events

    def history(self, client=None):
        """Return the restored page history for a client."""
        return page_json((client or self.client).get(reverse(PAGE_URL)), "agent-state")["history"]

    def test_live_and_restored_outcomes(self):  # pylint: disable=too-many-locals
        """Final outcomes restore correctly, and reused native IDs stay independent."""
        read_args = {"file_path": "/nautobot/models.py", "limit": 3}
        plan = [{"content": "Inspect", "status": "in_progress"}]
        task_args = {"description": "Inspect the Nautobot source.", "subagent_type": "general-purpose"}
        script = [
            AIMessage(
                content="Looking.",
                tool_calls=[
                    {"name": "read_file", "args": read_args, "id": "read"},
                    {"name": "ls", "args": {"path": "/"}, "id": "denied"},
                ],
            ),
            tool_call_message("write_todos", {"todos": plan}, "todos"),
            tool_call_message("task", task_args, "delegate"),
            tool_call_message("read_file", {"file_path": "/nautobot/models.py", "limit": 2}, "sub-read"),
            AIMessage(content="Delegate report."),
            AIMessage(content="Done."),
            AIMessage(content="Second answer."),
        ]
        graph = self.start_graph(script)
        events = self.send("Investigate.")
        saved_messages = self.saved_messages(graph)
        display_ids = request_display_ids(saved_messages)
        statuses = {call_id: values[-1] for call_id, values in call_statuses(events).items()}
        self.assertEqual(
            statuses,
            {
                display_ids["read"]: "completed",
                display_ids["denied"]: "failed",
                display_ids["todos"]: "completed",
                display_ids["delegate"]: "completed",
            },
        )
        # Prose precedes its calls, calls keep declaration order, and delegate internals never appear.
        names = [event for event, _ in events]
        self.assertLess(names.index("token"), names.index("tool_call"))
        self.assertEqual(list(statuses), [display_ids[key] for key in ("read", "denied", "todos", "delegate")])
        calls = [data for event, data in events if event == "tool_call"]
        self.assertTrue(all(set(data) == {"id", "name", "args", "status"} for data in calls))
        self.assertNotIn("sub-read", json.dumps(events))
        # Neither results, denial text, delegate reports, internal state, nor locations are disclosed.
        payload = json.dumps(calls)
        for hidden in (MODELS_LINES[0], "permission denied", "Delegate report", INTERNAL_SENTINEL, self.location_token):
            self.assertNotIn(hidden, payload)

        # Restored calls agree with the observed live outcomes.
        self.assert_denied(self.tool_results(saved_messages)["denied"])
        expected = [
            {"role": "user", "content": "Investigate."},
            {"role": "assistant", "content": "Looking."},
            tool_entry(display_ids["read"], "read_file", read_args, "completed"),
            tool_entry(display_ids["denied"], "ls", {"path": "/"}, "failed"),
            tool_entry(display_ids["todos"], "write_todos", {"todos": plan}, "completed"),
            tool_entry(display_ids["delegate"], "task", task_args, "completed"),
            {"role": "assistant", "content": "Done."},
        ]
        self.assertEqual(self.history(), expected)

        # A later send neither replays nor duplicates retained calls.
        second = self.send("Again.")
        self.assertEqual(call_statuses(second), {})
        self.assertEqual(
            self.history(),
            [*expected, {"role": "user", "content": "Again."}, {"role": "assistant", "content": "Second answer."}],
        )

        # Another session sees nothing and allocates nothing; a reset clears this session's pointer but retains state.
        other_client = self.client_class()
        other_client.force_login(self.user)
        self.assertEqual(self.history(other_client), [])
        self.assertNotIn(CONVERSATION_KEY, other_client.session)
        old_config = session_config(self.client)
        old_messages = graph.get_state(old_config).values["messages"]
        self.assertEqual(open_new_conversation(self.client).status_code, 200)
        self.assertEqual(self.history(), [])
        self.assertNotIn(CONVERSATION_KEY, self.client.session)
        self.assertEqual(graph.get_state(old_config).values["messages"], old_messages)
        self.assertEqual(AgentConversation.objects.count(), 1)
        self.assert_nothing_outside_reached_the_model()

        # Two posts reusing one native call id keep live and restored outcomes independent.
        first_args = {"file_path": "/nautobot/models.py"}
        second_args = {"file_path": "/internal/seeded.txt"}
        graph = self.start_graph(
            [
                tool_call_message("read_file", first_args, "call_0", "Reading source."),
                AIMessage(content="Read."),
                tool_call_message("read_file", second_args, "call_0", "Trying restricted source."),
                AIMessage(content="Denied."),
            ]
        )

        reuse_client = self.client_class()
        reuse_client.force_login(self.user)
        first_events = self.send("Read source.", reuse_client)
        graph.update_state(
            session_config(reuse_client),
            {"files": {second_args["file_path"]: create_file_data(INTERNAL_SENTINEL)}},
        )
        second_events = self.send("Read restricted source.", reuse_client)
        saved_messages = self.saved_messages(graph, reuse_client)
        requests = [message for message in saved_messages if isinstance(message, AIMessage) and message.tool_calls]
        self.assertEqual(len(requests), 2)
        expected_ids = [display_id(message.id, "tool", "call_0") for message in requests]
        first_calls = [data for event, data in first_events if event == "tool_call"]
        second_calls = [data for event, data in second_events if event == "tool_call"]
        final_first = {call["id"]: call for call in first_calls}
        final_second = {call["id"]: call for call in second_calls}
        self.assertEqual(final_first[expected_ids[0]]["status"], "completed")
        self.assertEqual(final_first[expected_ids[0]]["args"], first_args)
        self.assertEqual(final_second[expected_ids[1]]["status"], "failed")
        self.assertEqual(final_second[expected_ids[1]]["args"], second_args)
        self.assertNotEqual(expected_ids[0], expected_ids[1])

        results = [
            message
            for message in saved_messages
            if isinstance(message, ToolMessage) and message.tool_call_id == "call_0"
        ]
        self.assertEqual([result.status for result in results], ["success", "error"])
        restored_calls = [entry for entry in self.history(reuse_client) if entry.get("role") == "tool"]
        self.assertEqual(
            restored_calls,
            [
                tool_entry(expected_ids[0], "read_file", first_args, "completed"),
                tool_entry(expected_ids[1], "read_file", second_args, "failed"),
            ],
        )
        streamed_ids = set(final_first) | set(final_second)
        self.assertEqual(streamed_ids, set(expected_ids))

    def test_malformed_and_interrupted_calls(self):  # pylint: disable=too-many-locals
        """Malformed calls fail with raw arguments and interrupted calls stay unknown, both across SDK repair."""
        raw = '{"file_path": "</script><b>'
        script = [
            AIMessage(
                content="Trying.",
                invalid_tool_calls=[
                    invalid_tool_call(name="read_file", args=raw, id="bad-json", error="PARSER-DETAIL-1"),
                    invalid_tool_call(name=None, args=None, id=None, error="PARSER-DETAIL-2"),
                ],
            ),
            AIMessage(content="Second."),
        ]
        graph = self.start_graph(script)

        events = self.send("Break it.")

        message = next(m for m in self.saved_messages(graph) if isinstance(m, AIMessage) and m.invalid_tool_calls)
        invalid_ids = [display_id(message.id, "invalid", index) for index in range(2)]
        expected_calls = [
            {"id": invalid_ids[0], "name": "read_file", "args": raw, "status": "failed"},
            {"id": invalid_ids[1], "name": "unknown", "args": None, "status": "failed"},
        ]
        self.assertEqual([data for event, data in events if event == "tool_call"], expected_calls)
        self.assertNotIn("PARSER-DETAIL", json.dumps(events))
        restored = [
            {"role": "user", "content": "Break it."},
            {"role": "assistant", "content": "Trying."},
            tool_entry(invalid_ids[0], "read_file", raw, "failed"),
            tool_entry(invalid_ids[1], "unknown", None, "failed"),
        ]
        self.assertEqual(self.history(), restored)

        # The SDK answers the call that has an ID on the next send; both stay failed and are not replayed.
        self.assertEqual(call_statuses(self.send("Again.")), {})
        self.assertEqual(self.tool_results(self.saved_messages(graph))["bad-json"].status, "error")
        self.assertEqual(
            self.history(),
            [*restored, {"role": "user", "content": "Again."}, {"role": "assistant", "content": "Second."}],
        )

        # An interrupted call stays unknown after the SDK repairs it.
        args = {"file_path": "/nautobot/models.py"}
        script = [tool_call_message("read_file", args, "dangling", "Reading."), AIMessage(content="Recovered.")]
        graph = self.start_graph(script)
        interrupted_client = self.client_class()
        interrupted_client.force_login(self.user)

        with (
            patch.object(NautobotSourceBackend, "read", side_effect=RuntimeError("backend interrupted")),
            self.assertLogs("nautobot_agent.chat_stream", level="ERROR"),
        ):
            events = self.send("Question.", interrupted_client)

        request = next(
            message
            for message in self.saved_messages(graph, interrupted_client)
            if isinstance(message, AIMessage) and message.tool_calls
        )
        projected_id = display_id(request.id, "tool", "dangling")
        self.assertEqual(
            [data for event, data in events if event == "tool_call"],
            [{"id": projected_id, "name": "read_file", "args": args, "status": "running"}],
        )
        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("dangling", self.tool_results(self.saved_messages(graph, interrupted_client)))
        interrupted = [
            {"role": "user", "content": "Question."},
            {"role": "assistant", "content": "Reading."},
            tool_entry(projected_id, "read_file", args, "unknown"),
        ]
        self.assertEqual(self.history(interrupted_client), interrupted)

        events = self.send("Again.", interrupted_client)

        # The real SDK repaired the dangling call with an error result, which is not an execution outcome.
        repair = self.tool_results(self.saved_messages(graph, interrupted_client))["dangling"]
        self.assertEqual(repair.status, "error")
        self.assertIn("did not complete", repair.text)
        self.assertEqual(call_statuses(events), {})
        history = self.history(interrupted_client)
        self.assertEqual(
            history,
            [*interrupted, {"role": "user", "content": "Again."}, {"role": "assistant", "content": "Recovered."}],
        )
        self.assertNotIn("did not complete", json.dumps(history))
