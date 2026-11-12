"""Runtime-owned graph construction, adapters, and retained model/classifier/state-reader caches."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, TypedDict, cast

from deepagents import create_deep_agent
from deepagents.graph import DeepAgentState
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT, SubAgent
from deepagents.middleware.summarization import compute_summarization_defaults
from django.core.exceptions import ImproperlyConfigured, ValidationError
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware, TodoListMiddleware
from langchain.agents.middleware.todo import Todo
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from nautobot_agent.mcp.tools import MCPCatalogSnapshot, MCPToolCache, UnavailableMCPServer
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import AgentConversation, LLMConfiguration, MCPServer, Skill
from nautobot_agent.prompting.definitions import BASE_PROMPT, SKILLS_PROMPT, TOOL_SAFETY_PROMPT, mcp_availability_prompt
from nautobot_agent.runtime import scope
from nautobot_agent.runtime.checkpointer import DjangoCheckpointSaver, thread_config
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.runtime.control import RunControlMiddleware
from nautobot_agent.runtime.errors import ModelFailure
from nautobot_agent.runtime.llm import build_chat_model, get_default_configuration, get_scope_classifier_configuration
from nautobot_agent.runtime.scope import ScopeDecision
from nautobot_agent.sources import backends, source_access
from nautobot_agent.sources.source_access import SourceAccessMiddleware, SourceBackend
from nautobot_agent.tools.describe_model import describe_model
from nautobot_agent.tools.get_nautobot_environment import get_nautobot_environment
from nautobot_agent.tools.monty.middleware import InterpreterMiddleware
from nautobot_agent.tools.query_records import query_records
from nautobot_agent.tools.render_chart import render_chart
from nautobot_agent.tools.trace_relationships import trace_relationships


@dataclass(frozen=True)
class AgentGraph:
    """Compiled graph and immutable display facts from one configuration snapshot."""

    graph: Any
    model_spec: str
    context_limit: int
    mcp_unavailable_servers: tuple[UnavailableMCPServer, ...] = ()


@dataclass(frozen=True)
class _PreparedModel:
    """Successful model construction and metadata validation for one LLM identity."""

    key: tuple[Any, Any]
    model: BaseChatModel
    context_limit: int
    summarization_defaults: Mapping[str, Any]


ModelFactory = Callable[[LLMConfiguration], BaseChatModel]


def _snapshot_identity(row) -> tuple[Any, Any]:
    """Return the version identity used for mutable database-backed configuration."""
    return row.pk, row.last_updated


class _ConversationState(DeepAgentState, total=False):
    """Retained display channels, including the planning middleware's additional state."""

    todos: list[Todo]


class StateReader(Protocol):  # pylint: disable=too-few-public-methods
    """Graph-like object exposing a point-in-time state snapshot."""

    def get_state(self, config: RunnableConfig) -> Any:
        """Return a snapshot whose ``values`` mapping holds the graph channels."""


class ConversationValues(TypedDict):
    """Graph values with a validated message channel; other retained channels pass through unvalidated."""

    messages: Sequence[BaseMessage]


def read_state(reader: StateReader, config: RunnableConfig) -> ConversationValues:
    """Read graph state once and validate its retained message channel."""
    values = reader.get_state(config).values
    messages = values.get("messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes | bytearray):
        raise TypeError("Invalid reconstructed message sequence")
    if any(not isinstance(message, BaseMessage) for message in messages):
        raise TypeError("Invalid reconstructed message")
    return {**values, "messages": messages}


class AgentRuntime:  # pylint: disable=too-many-instance-attributes
    """Own runtime adapters, prepared model reuse, and the shared MCP catalog cache."""

    def __init__(
        self,
        *,
        saver=None,
        model_factory: ModelFactory = build_chat_model,
        sources=None,
    ):
        """Bind adapters for this runtime's lifetime and start retained reuse empty."""
        self.saver = saver if saver is not None else DjangoCheckpointSaver()
        self.model_factory = model_factory
        self.sources = sources if sources is not None else backends.source_catalog
        self.mcp_tool_cache = MCPToolCache()
        self._classifier: tuple[tuple[Any, Any], Any] | None = None
        self._prepared_model: _PreparedModel | None = None

    @cached_property
    def state_reader(self):
        """Compile this runtime's model-free checkpoint reader once."""
        # ty cannot match TypedDict class attributes to LangGraph's schema protocol.
        builder = StateGraph(_ConversationState)  # ty: ignore[invalid-argument-type]
        return builder.add_edge(START, END).compile(checkpointer=self.saver)

    def read_conversation_values(self, conversation: AgentConversation) -> ConversationValues:
        """Read validated latest canonical state, without checking conversation permissions.

        Arbitrary checkpoints and namespaces use ``read_state()`` instead.
        """
        return read_state(self.state_reader, thread_config(conversation.pk))

    def graph(self, user, *, mcp_turn: MCPTurn) -> AgentGraph:
        """Build a graph from this turn's captured configuration and authorization snapshot.

        ``mcp_turn`` supplies run-owned discovery if this call leads the runtime's shared catalog
        cache miss. Tool dispatch resolves the live turn from ``AgentContext.mcp_turn`` at call time.
        """
        source_apps = source_access.resolve_source_apps(user, self.sources)
        try:
            definitions = Skill.objects.runtime_snapshot()
        except ValidationError as error:
            raise ImproperlyConfigured("Skill configuration is invalid.") from error
        configuration = get_default_configuration()
        servers = list(MCPServer.objects.filter(enabled=True).order_by("pk"))
        prepared = self._prepare_model(configuration)
        server_snapshot = tuple(_snapshot_identity(server) for server in servers)
        catalog = self.mcp_tool_cache.get(server_snapshot, servers, mcp_turn)
        return self._build_graph(configuration, source_apps, definitions, catalog, prepared)

    def classify_scope(self, text, history=None, *, topic_switch_only: bool = False, config=None) -> ScopeDecision:
        """Classify a request through a classifier cached per configuration snapshot."""
        configuration = get_scope_classifier_configuration()
        key = _snapshot_identity(configuration)
        entry = self._classifier
        if entry is None or entry[0] != key:
            entry = (key, scope.bind_scope_decision(self.model_factory(configuration)))
            self._classifier = entry
        return scope.classify_request(entry[1], text, history, topic_switch_only=topic_switch_only, config=config)

    def _prepare_model(self, configuration) -> _PreparedModel:
        """Construct and validate model metadata before any MCP or source assembly."""
        key = _snapshot_identity(configuration)
        prepared = self._prepared_model
        if prepared is None or prepared.key != key:
            model = self.model_factory(configuration)
            limit = _profile_context_limit(model)
            if limit is None:
                raise ModelFailure("model_context_limit_missing")
            prepared = _PreparedModel(key, model, limit, compute_summarization_defaults(model))
            self._prepared_model = prepared
        return prepared

    def _build_graph(  # pylint: disable=too-many-locals
        self, configuration, source_apps, definitions, catalog: MCPCatalogSnapshot, prepared: _PreparedModel
    ):
        """Build and configure a graph for one captured LLM, MCP, source, and skill snapshot."""
        model = prepared.model
        summarization_defaults = prepared.summarization_defaults
        tools = [
            describe_model,
            get_nautobot_environment,
            query_records,
            trace_relationships,
            *catalog.tools,
        ]

        backend = SourceBackend(self.sources, source_apps, definitions)
        system_prompt = BASE_PROMPT
        subagent_prompt = f"{GENERAL_PURPOSE_SUBAGENT['system_prompt']}\n\n{TOOL_SAFETY_PROMPT}"
        if catalog.unavailable:
            availability = mcp_availability_prompt(tuple(server.as_dict() for server in catalog.unavailable))
            system_prompt = f"{system_prompt}\n{availability}"
            subagent_prompt = f"{subagent_prompt}\n\n{availability}"
        graph = create_deep_agent(
            model=model,
            system_prompt=system_prompt,
            backend=backend,
            tools=[*tools, render_chart],
            subagents=[
                cast(
                    SubAgent,
                    {
                        **GENERAL_PURPOSE_SUBAGENT,
                        "description": (
                            "Research helper with Nautobot query, metadata, source, and available MCP tools; "
                            "no eval_python, render_chart, or write_todos. "
                            "Keep calculations, charts, and task tracking in the main agent."
                        ),
                        "system_prompt": subagent_prompt,
                        "tools": tools,
                        "middleware": [
                            RunControlMiddleware(),
                            SourceAccessMiddleware(backend=backend),
                            SummarizationMiddleware(
                                model=model,
                                trigger=summarization_defaults["trigger"],
                                keep=summarization_defaults["keep"],
                            ),
                        ],
                    },
                ),
            ],
            state_schema=_ConversationState,
            context_schema=AgentContext,
            # The factory merges heterogeneous middleware states; its invariant generic cannot express that union.
            middleware=cast(
                list[AgentMiddleware[Any, Any, Any]],
                [
                    TodoListMiddleware(),
                    RunControlMiddleware(),
                    InterpreterMiddleware(),
                    SourceAccessMiddleware(backend=backend),
                    SummarizationMiddleware(
                        model=model,
                        trigger=summarization_defaults["trigger"],
                        keep=summarization_defaults["keep"],
                    ),
                    SkillsMiddleware(
                        backend=backend,
                        sources=[source_access.SKILLS_SOURCE],
                        system_prompt=SKILLS_PROMPT,
                    ),
                ],
            ),
            checkpointer=self.saver,
        )
        return AgentGraph(graph, configuration.model, prepared.context_limit, catalog.unavailable)


def _profile_context_limit(model) -> int | None:
    """Return a positive integer input limit from a model profile, excluding booleans."""
    profile = getattr(model, "profile", None)
    if isinstance(profile, Mapping):
        candidate = profile.get("max_input_tokens")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    return None


_RUNTIME: AgentRuntime | None = None


def current_runtime() -> AgentRuntime:
    """Return the active runtime, creating the default one on first use."""
    global _RUNTIME  # pylint: disable=global-statement
    if _RUNTIME is None:
        _RUNTIME = AgentRuntime()
    return _RUNTIME
