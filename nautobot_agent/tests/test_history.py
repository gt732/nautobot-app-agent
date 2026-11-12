"""Tests for common retained-state validation and text-only history projections."""

from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from nautobot_agent.chat import history
from nautobot_agent.chat.history import transcript_entries
from nautobot_agent.runtime.agent import read_state
from nautobot_agent.runtime.scope import REFUSAL_MESSAGE
from nautobot_agent.tests.support.chat import create_conversation, create_refused_run, use_runtime


class RetainedStateProjectionTests(SimpleTestCase):
    """Pin retained-state validation and text-only transcript projection."""

    def test_checkpoint_validation_and_transcript_projection(self):
        for messages in ("not messages", [HumanMessage("valid"), object()]):
            reader = Mock()
            reader.get_state.return_value.values = {"messages": messages}
            with self.subTest(messages=messages), self.assertRaises(TypeError):
                read_state(reader, {})

        messages = [
            HumanMessage(""),
            HumanMessage("Generated summary", additional_kwargs={"lc_source": "summarization"}),
            AIMessage(" \n "),
            AIMessage(""),
            SystemMessage("system"),
            ToolMessage("tool", tool_call_id="call"),
        ]
        self.assertEqual(
            transcript_entries(messages),
            [{"role": "user", "content": ""}, {"role": "assistant", "content": " \n "}],
        )


class ConversationHistoryReadTests(TestCase):
    """Pin refusal-aware conversation reads while retaining checkpoint-only context usage."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="history-reader")
        self.runtime = use_runtime(self)

    def test_chat_state_and_transcript_merge_refusals_without_mutating_checkpoint(self):
        conversation = create_conversation(self.user)
        assistant = AIMessage(
            "checkpoint answer",
            id="answer",
            tool_calls=[{"name": "private_tool", "args": {}, "id": "call"}],
            usage_metadata={"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
        )
        messages = [
            HumanMessage("question", id="question"),
            ToolMessage("private result", tool_call_id="previous"),
            assistant,
            ToolMessage("secret tool output", tool_call_id="call"),
        ]
        original = deepcopy(messages)
        create_refused_run(conversation, "old", "summarized")
        create_refused_run(conversation, "refused", "answer")
        create_refused_run(conversation, "repeated", "answer")
        values = {"messages": messages, "todos": [{"content": "</script><b>x</b>", "status": "pending"}]}
        reader = Mock()
        reader.get_state.return_value.values = values
        with patch.object(self.runtime, "state_reader", reader):
            restored_history, todos, context_usage = history.read_chat_state(conversation, limit=100)
            transcript = history.read_transcript(conversation)
        expected = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": REFUSAL_MESSAGE},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "checkpoint answer"},
            {"role": "user", "content": "refused"},
            {"role": "assistant", "content": REFUSAL_MESSAGE},
            {"role": "user", "content": "repeated"},
            {"role": "assistant", "content": REFUSAL_MESSAGE},
        ]
        self.assertEqual(transcript, expected)
        self.assertEqual(
            restored_history,
            [
                *expected[:4],
                {
                    "role": "tool",
                    "id": '["answer","tool","call"]',
                    "name": "private_tool",
                    "args": {},
                    "status": "completed",
                },
                *expected[4:],
            ],
        )
        self.assertEqual(todos, values["todos"])
        self.assertEqual(context_usage, {"used": 18, "limit": 100, "percent": 18.0})
        self.assertEqual(messages, original)

        empty = create_conversation(self.user)
        reader = Mock()
        reader.get_state.return_value.values = {}
        with patch.object(self.runtime, "state_reader", reader):
            restored_history, _, _ = history.read_chat_state(empty)
            self.assertEqual(restored_history, [])
            self.assertEqual(history.read_transcript(empty), [])
            create_refused_run(empty, "old", "summarized")
            expected = [{"role": "user", "content": "old"}, {"role": "assistant", "content": REFUSAL_MESSAGE}]
            restored_history, _, context_usage = history.read_chat_state(empty)
            self.assertEqual(restored_history, expected)
            self.assertEqual(history.read_transcript(empty), expected)
            self.assertEqual(context_usage, {"used": None, "limit": None, "percent": None})
