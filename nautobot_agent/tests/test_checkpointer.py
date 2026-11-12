"""Durable checkpoint saver: official LangGraph conformance plus storage behavior upstream does not assert."""

from collections.abc import AsyncGenerator
from typing import cast

from anthropic.types import Message as AnthropicMessage
from asgiref.sync import async_to_sync, sync_to_async
from deepagents.graph import DeepAgentState
from deepagents.middleware.filesystem import FilesystemState
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.conformance import validate
from langgraph.checkpoint.conformance.initializer import RegisteredCheckpointer
from langgraph.checkpoint.conformance.test_utils import generate_checkpoint, generate_config, generate_metadata
from langgraph.graph import END, START, StateGraph
from nautobot.apps.testing import NautobotTestCaseMixin, TestCase
from typing_extensions import TypedDict

from nautobot_agent.chat.history import project_history
from nautobot_agent.models import AgentCheckpoint, AgentConversation
from nautobot_agent.runtime.agent import current_runtime
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.checkpointer import (
    RUN_GUARD_CONFIG_KEY,
    DjangoCheckpointSaver,
    attach_run_guard,
    thread_config,
)
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import ConversationUnavailable
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import (
    TestRunWriteGuard,
    create_conversation,
    guarded_test_config,
    use_runtime,
)
from nautobot_agent.tests.support.db import CommittedTestCase
from nautobot_agent.tests.support.graph import DurableSourceGraphTestCase

User = get_user_model()


class _NestedState(TypedDict, total=False):
    request: str
    answer: str


class _FileState(FilesystemState, DeepAgentState):
    """Use production delta reducers without constructing a model."""


RESERVED_CHANNEL = "__error__"


# Optional upstream capabilities are deliberately inherited as unsupported, as in the production saver.
class _ProvisioningSaver(DjangoCheckpointSaver):  # pylint: disable=abstract-method
    """Test-only saver: the generated conformance UUIDs have no owner, so provision it before the real writes.

    Async adapters support the upstream harness only; production storage remains synchronous.
    Unsupported capabilities stay inherited so nonexistent-thread checks remain meaningful.
    """

    def __init__(self, user):
        super().__init__()
        self.user = user
        self.guards = {}
        self.provisioned: list[str] = []

    def _provision(self, config):
        thread_id = str(config["configurable"]["thread_id"])
        conversation, created = AgentConversation.objects.get_or_create(pk=thread_id, defaults={"user": self.user})
        if created:
            self.provisioned.append(thread_id)
        if thread_id not in self.guards:
            self.guards[thread_id] = TestRunWriteGuard(conversation)
        return attach_run_guard(config, self.guards[thread_id])

    def put(self, config, checkpoint, metadata, new_versions):
        return super().put(self._provision(config), checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        super().put_writes(self._provision(config), writes, task_id, task_path)

    async def aget_tuple(self, config):
        return await sync_to_async(self.get_tuple, thread_sensitive=True)(config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await sync_to_async(self.put, thread_sensitive=True)(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await sync_to_async(self.put_writes, thread_sensitive=True)(config, writes, task_id, task_path)


class CheckpointerConformanceTests(NautobotTestCaseMixin, CommittedTestCase):
    """Upstream async storage suites require committed saver rows; listing and thread deletion are unsupported."""

    def test_official_base_suites_pass(self):
        user = User.objects.create_user(username="checkpoint-conformance")

        async def factory() -> AsyncGenerator[BaseCheckpointSaver, None]:
            saver = _ProvisioningSaver(user)
            yield saver
            await sync_to_async(AgentConversation.objects.filter(pk__in=saver.provisioned).delete)()

        registered = RegisteredCheckpointer(name="DjangoCheckpointSaver", factory=factory)
        supported = {"put", "put_writes", "get_tuple"}
        report = async_to_sync(validate)(registered, capabilities=supported)
        for capability in supported:
            self.assertTrue(report.results[capability].passed, report.to_dict())


class CheckpointStorageTests(TestCase):
    """Row-level behavior the official suites do not assert; synchronous writes run in the per-test transaction."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user(username="checkpoint-owner")
        cls.conversation = create_conversation(cls.owner)

    def setUp(self):
        """Create an isolated conversation and saver for each storage test."""
        super().setUp()
        self.switch_conversation(self.conversation)
        self.saver = DjangoCheckpointSaver()

    def switch_conversation(self, conversation=None):
        """Switch the fixture to a fresh conversation and write guard."""
        self.conversation = conversation or create_conversation(self.owner)
        self.thread = str(self.conversation.pk)
        self.guard = TestRunWriteGuard(self.conversation)

    def guarded_config(self, **kwargs):
        """Build a saver configuration carrying this fixture's write guard."""
        config = generate_config(self.thread, **kwargs)
        return attach_run_guard(config, self.guard)

    def put(self, parent=None):
        """Persist a generated checkpoint with an optional parent."""
        config = self.guarded_config(checkpoint_id=parent)
        return self.saver.put(config, generate_checkpoint(), generate_metadata(), {})

    def fetch(self, checkpoint_id):
        """Load a checkpoint tuple and assert that it exists."""
        found = self.saver.get_tuple(generate_config(self.thread, checkpoint_id=checkpoint_id))
        self.assertIsNotNone(found)
        return cast(CheckpointTuple, found)

    def write(self, checkpoint_id, task_id, writes, task_path=""):
        """Persist pending writes under the selected checkpoint."""
        config = self.guarded_config(checkpoint_id=checkpoint_id)
        self.saver.put_writes(config, writes, task_id, task_path)

    def save_checkpoint(  # pylint: disable=too-many-arguments
        self, checkpoint_id, parent_id=None, values=None, checkpoint_ns="", channel_versions=None, metadata=None
    ):
        """Save a checkpoint with fixture-specified values and channel versions."""
        values = values or {}
        checkpoint = generate_checkpoint()
        checkpoint["id"] = checkpoint_id
        checkpoint["channel_values"] = values
        versions = {}
        if parent_id is not None:
            parent_config = generate_config(self.thread, checkpoint_id=parent_id)
            parent_config["configurable"]["checkpoint_ns"] = checkpoint_ns
            parent = self.saver.get_tuple(parent_config)
            if parent is not None:
                versions.update(parent.checkpoint["channel_versions"])
        for channel in values:
            versions[channel] = int(versions.get(channel, 0)) + 1
        versions.update(channel_versions or {})
        checkpoint["channel_versions"] = versions
        config = self.guarded_config(checkpoint_id=parent_id)
        config["configurable"]["checkpoint_ns"] = checkpoint_ns
        return self.saver.put(config, checkpoint, metadata if metadata is not None else generate_metadata(), {})

    def test_ownership_and_guard_fail_closed(self):
        config = generate_config(self.thread, checkpoint_id="unguarded")
        with self.assertRaises(ConversationUnavailable):
            self.saver.put(config, generate_checkpoint(), generate_metadata(), {})
        with self.assertRaises(ConversationUnavailable):
            self.saver.put_writes(config, [("channel", "value")], "task")
        with self.assertRaises(ConversationUnavailable):
            self.saver.prune_turn(config, channels={})
        self.assertFalse(AgentCheckpoint.objects.filter(conversation=self.conversation).exists())
        other = create_conversation(self.owner)
        wrong = TestRunWriteGuard(other)
        config = attach_run_guard(generate_config(self.thread, checkpoint_id="wrong-owner"), wrong)
        with self.assertRaises(ConversationUnavailable):
            self.saver.put(config, generate_checkpoint(), generate_metadata(), {})
        with self.assertRaises(ConversationUnavailable):
            self.saver.put_writes(config, [("channel", "value")], "task")
        with self.assertRaises(ConversationUnavailable):
            self.saver.prune_turn(config, channels={})
        self.assertFalse(AgentCheckpoint.objects.filter(conversation=self.conversation).exists())

    def test_checkpoint_ids_increase_beyond_parent(self):
        parent = self.save_checkpoint("0002")
        parent_id = parent["configurable"]["checkpoint_id"]
        with self.assertRaisesRegex(ValueError, "Checkpoint ID must increase"):
            self.save_checkpoint("0001", parent_id)

    def test_delta_history_replay(self):  # pylint: disable=too-many-locals,too-many-statements
        root = self.save_checkpoint("0001", values={"channel": "seed", "empty": None, "list": []})
        root_id = root["configurable"]["checkpoint_id"]
        child = self.save_checkpoint("0002", root_id, {"other": "other-seed"})
        child_id = child["configurable"]["checkpoint_id"]
        target = self.save_checkpoint("0003", child_id)
        target_id = target["configurable"]["checkpoint_id"]
        self.write(root_id, "z-task", [("channel", "root-z")])
        self.write(root_id, "a-task", [("channel", "root-a0"), ("channel", "root-a1")])
        self.write(child_id, "task", [("channel", "child"), ("other", "child-other")])
        self.write(target_id, "task", [("channel", "excluded-target")])

        actual = self.saver.get_delta_channel_history(
            config=generate_config(self.thread, checkpoint_id=target_id),
            channels=["channel", "other", "empty", "list", "missing", "channel"],
        )
        self.assertEqual(
            actual,
            {
                "channel": {
                    "seed": "seed",
                    "writes": [
                        ("a-task", "channel", "root-a0"),
                        ("a-task", "channel", "root-a1"),
                        ("z-task", "channel", "root-z"),
                        ("task", "channel", "child"),
                    ],
                },
                "other": {"seed": "other-seed", "writes": [("task", "other", "child-other")]},
                "empty": {"seed": None, "writes": []},
                "list": {"seed": [], "writes": []},
                "missing": {"writes": []},
            },
        )
        self.assertEqual(
            actual,
            BaseCheckpointSaver.get_delta_channel_history(
                self.saver,
                config=generate_config(self.thread, checkpoint_id=target_id),
                channels=["channel", "other", "empty", "list", "missing", "channel"],
            ),
        )

        self.switch_conversation()
        root = self.save_checkpoint("0001", values={"channel": "seed"})
        root_id = root["configurable"]["checkpoint_id"]
        child = self.save_checkpoint("0003", root_id)
        target = self.save_checkpoint("0005", child["configurable"]["checkpoint_id"])
        target_id = target["configurable"]["checkpoint_id"]
        fork = self.save_checkpoint("0002", root_id, {"channel": "off-path"})
        fork_id = fork["configurable"]["checkpoint_id"]
        self.write(fork_id, "task", [("channel", "off-path-write")])
        AgentCheckpoint.objects.filter(checkpoint_id=fork_id).update(checkpoint=b"bad", checkpoint_type="unknown")
        config = generate_config(self.thread, checkpoint_id=target_id)
        history = self.saver.get_delta_channel_history(config=config, channels=["channel"])
        expected = {"channel": {"seed": "seed", "writes": []}}
        self.assertEqual(history, expected)
        self.assertEqual(
            history,
            BaseCheckpointSaver.get_delta_channel_history(self.saver, config=config, channels=["channel"]),
        )

        self.switch_conversation()
        root = self.save_checkpoint("0001", values={"channel": "seed"})
        root_id = root["configurable"]["checkpoint_id"]
        exact = self.save_checkpoint("0003", root_id)
        exact_id = exact["configurable"]["checkpoint_id"]
        latest = self.save_checkpoint("0004", exact_id, {"channel": "latest-seed"})
        latest_id = latest["configurable"]["checkpoint_id"]
        self.write(exact_id, "task", [("channel", "exact-target")])
        self.write(latest_id, "task", [("channel", "latest-target")])

        self.assertEqual(
            self.saver.get_delta_channel_history(
                config=generate_config(self.thread, checkpoint_id=exact_id), channels=["channel"]
            ),
            {"channel": {"seed": "seed", "writes": []}},
        )
        self.assertEqual(
            self.saver.get_delta_channel_history(config=generate_config(self.thread), channels=["channel"]),
            {"channel": {"seed": "seed", "writes": [("task", "channel", "exact-target")]}},
        )

        self.switch_conversation()
        root = self.save_checkpoint("0001", values={"channel": "default-seed"})
        root_id = root["configurable"]["checkpoint_id"]
        incomplete_id = "0002"
        self.saver.put_writes(self.guarded_config(checkpoint_id=incomplete_id), [("channel", "incomplete")], "task")
        target = self.save_checkpoint("0003", incomplete_id, channel_versions={"channel": "1"})
        target_id = target["configurable"]["checkpoint_id"]
        scoped_root = self.save_checkpoint("0001", values={"channel": "scoped-seed"}, checkpoint_ns="ns")
        scoped_target = self.save_checkpoint("0003", scoped_root["configurable"]["checkpoint_id"], checkpoint_ns="ns")
        self.write(root_id, "task", [("channel", "default-write")])

        with self.assertRaisesRegex(RuntimeError, "Checkpoint ancestry is unavailable"):
            self.saver.get_delta_channel_history(
                config=generate_config(self.thread, checkpoint_id=target_id), channels=["channel"]
            )
        scoped_config = scoped_target
        self.assertEqual(
            self.saver.get_delta_channel_history(config=scoped_config, channels=["channel"]),
            {"channel": {"seed": "scoped-seed", "writes": []}},
        )
        self.assertEqual(
            self.saver.get_delta_channel_history(
                config=generate_config(str(create_conversation(self.owner).pk)), channels=["channel"]
            ),
            {"channel": {"writes": []}},
        )
        with self.assertRaisesRegex(RuntimeError, "Checkpoint history is unavailable"):
            self.saver.get_delta_channel_history(
                config=generate_config(self.thread, checkpoint_id=incomplete_id), channels=["channel"]
            )
        AgentCheckpoint.objects.filter(conversation=self.conversation, checkpoint_id=incomplete_id).delete()
        with self.assertRaisesRegex(RuntimeError, "Checkpoint ancestry is unavailable"):
            self.saver.get_delta_channel_history(config=target, channels=["channel"])

        self.switch_conversation()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.saver.get_delta_channel_history(config=generate_config(self.thread), channels=[]), {})
        self.assertEqual(len(queries), 0)
        self.assertEqual(
            self.saver.get_delta_channel_history(
                config=generate_config(str(create_conversation(self.owner).pk)), channels=["channel"]
            ),
            {"channel": {"writes": []}},
        )

    def test_delta_history_propagates_visited_checkpoint_decode_failure(self):
        root = self.save_checkpoint("0001", values={"channel": "seed"})
        root_id = root["configurable"]["checkpoint_id"]
        target = self.save_checkpoint("0002", root_id)
        AgentCheckpoint.objects.filter(checkpoint_id=root_id).update(checkpoint_type="unknown")
        with self.assertRaisesRegex(NotImplementedError, "Unknown serialization type"):
            self.saver.get_delta_channel_history(
                config=generate_config(self.thread, checkpoint_id=target["configurable"]["checkpoint_id"]),
                channels=["channel"],
            )
        AgentCheckpoint.objects.filter(checkpoint_id=root_id).update(checkpoint_type="msgpack")
        AgentCheckpoint.objects.filter(checkpoint_id=target["configurable"]["checkpoint_id"]).update(
            checkpoint_type="unknown"
        )
        with self.assertRaisesRegex(NotImplementedError, "Unknown serialization type"):
            self.saver.get_delta_channel_history(config=target, channels=["channel"])


class ProductionSnapshotReplayTests(DurableSourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Production graph executor connections require committed checkpoints for pending replay and saver replacement."""

    def setUp(self):
        super().setUp()
        use_runtime(self, saver=DjangoCheckpointSaver())

    def test_anthropic_signed_reasoning_survives_durable_tool_continuation(self):
        """Native Anthropic content blocks and tool results reload into a fresh graph runtime unchanged."""
        response = AnthropicMessage.model_validate(
            {
                "id": "msg_durable_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "private reasoning block",
                        "signature": "signed-thinking-sentinel",
                    },
                    {"type": "text", "text": "I will inspect the source."},
                    {
                        "type": "tool_use",
                        "id": "toolu_durable_test",
                        "name": "read_file",
                        "input": {"file_path": "/nautobot/__init__.py", "limit": 2},
                    },
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 8},
            }
        )
        native_model = ChatAnthropic(model="claude-test", api_key="credential-free-test")
        native_message = native_model._format_output(response).generations[0].message  # pylint: disable=protected-access
        self._assert_native_tool_continuation(native_message, ("private reasoning block", "signed-thinking-sentinel"))

    def _assert_native_tool_continuation(self, native_message, private_values):  # pylint: disable=too-many-locals
        assert isinstance(native_message, AIMessage)
        expected_content = native_message.content
        expected_extensions = native_message.additional_kwargs
        for value in private_values:
            self.assertIn(value, str(expected_content) + str(expected_extensions))

        graph = self.start_graph([native_message])
        conversation = create_conversation(self.user)
        config = guarded_test_config(thread_config(conversation.pk), conversation)
        context = AgentContext(
            user_id=str(self.user.pk), authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
        )
        graph.invoke(
            {"messages": [HumanMessage("Read the package entry point.")]},
            config,
            context=context,
            interrupt_after=["tools"],
        )
        persisted = current_runtime().read_conversation_values(conversation)["messages"]
        saved_assistant, saved_result = persisted[1], persisted[2]
        assert isinstance(saved_assistant, AIMessage)
        assert isinstance(saved_result, ToolMessage)
        self.assertEqual(saved_assistant.content, expected_content)
        self.assertEqual(saved_assistant.tool_calls, native_message.tool_calls)
        self.assertEqual(saved_assistant.additional_kwargs, expected_extensions)
        self.assertEqual(saved_result.tool_call_id, "toolu_durable_test")

        projection = project_history(persisted)
        for value in private_values:
            self.assertNotIn(value, str(projection))
        self.assertIn("I will inspect the source.", str(projection))

        use_runtime(self, saver=DjangoCheckpointSaver())
        continued = self.start_graph([AIMessage(content="The durable continuation completed.")])
        self.assertEqual(continued.get_state(config).values["messages"], persisted)
        continued.invoke(None, config, context=context, durability="sync")
        request = self.scripted_model.requests[0]
        resumed = next(message for message in request if isinstance(message, AIMessage))
        self.assertEqual(resumed.content, expected_content)
        self.assertEqual(resumed.tool_calls, native_message.tool_calls)
        self.assertEqual(resumed.additional_kwargs, expected_extensions)
        resumed_result = next(
            message
            for message in request
            if isinstance(message, ToolMessage) and message.tool_call_id == "toolu_durable_test"
        )
        self.assertEqual(resumed_result.content, saved_result.content)
        self.assertEqual(resumed_result.status, "success")


class CheckpointSubgraphTests(NautobotTestCaseMixin, CommittedTestCase):
    """Synchronous nested graph writes retain ownership without persisting the live guard."""

    def test_subgraph_keeps_live_guard_out_of_durable_state(self):
        user = User.objects.create_user(username="checkpoint-subgraph")
        conversation = create_conversation(user)
        config = guarded_test_config(thread_config(conversation.pk), conversation)
        # ty cannot match TypedDict class attributes to LangGraph's schema protocol.
        child = StateGraph(_NestedState)  # ty: ignore[invalid-argument-type]
        child.add_node("answer", lambda state: {"answer": "nested answer"})
        child.add_edge(START, "answer")
        child.add_edge("answer", END)
        parent = StateGraph(_NestedState)  # ty: ignore[invalid-argument-type]
        parent.add_node("child", child.compile(checkpointer=True))
        parent.add_edge(START, "child")
        parent.add_edge("child", END)
        saver = DjangoCheckpointSaver()
        graph = parent.compile(checkpointer=saver)
        result = graph.invoke({"request": "nested request"}, config, durability="sync")
        self.assertEqual(result, {"request": "nested request", "answer": "nested answer"})
        rows = AgentCheckpoint.objects.filter(conversation=conversation).exclude(checkpoint_ns="")
        self.assertTrue(rows.exists())
        for row in rows:
            self.assertNotIn(RUN_GUARD_CONFIG_KEY, saver.serde.loads_typed((row.metadata_type, bytes(row.metadata))))
