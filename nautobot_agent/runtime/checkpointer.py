"""Django ORM LangGraph checkpoint saver."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast

from django.db import transaction
from django.db.models import Prefetch
from langchain_core.runnables import RunnableConfig
from langgraph._internal._typing import MISSING
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from nautobot_agent.db_connections import close_connections
from nautobot_agent.models import AgentCheckpoint, AgentCheckpointWrite
from nautobot_agent.runtime.control import ConversationUnavailable

RUN_GUARD_CONFIG_KEY = "__nautobot_run_guard"


def thread_config(conversation_id) -> RunnableConfig:
    """Return the canonical thread-only graph configuration."""
    return {"configurable": {"thread_id": str(conversation_id)}}


class ConversationWriteGuard(Protocol):  # pylint: disable=too-few-public-methods
    """Minimal live invocation guard required for durable conversation writes."""

    def write(self, conversation_id: str) -> AbstractContextManager[object]:
        """Guard a complete durable mutation for the matching conversation."""
        ...


def attach_run_guard(config: Mapping[str, Any], guard: ConversationWriteGuard) -> RunnableConfig:
    """Copy a run config's outer/configurable mappings and attach its live owner."""
    attached = dict(config)
    attached["configurable"] = dict(config.get("configurable", {}))
    attached["configurable"][RUN_GUARD_CONFIG_KEY] = guard
    return cast(RunnableConfig, attached)


def require_run_guard(config: Mapping[str, Any]) -> ConversationWriteGuard:
    """Require a callable owner guard; the owner validates identity on writes."""
    configurable = config.get("configurable", {})
    guard = configurable.get(RUN_GUARD_CONFIG_KEY) if isinstance(configurable, Mapping) else None
    if guard is None or not callable(getattr(guard, "write", None)):
        raise ConversationUnavailable("Conversation ownership is unavailable.")
    return guard


def durable_items(mapping: Mapping[Any, Any]) -> Iterator[tuple[Any, Any]]:
    """Iterate mapping entries except the reserved live guard."""
    return ((key, value) for key, value in mapping.items() if key != RUN_GUARD_CONFIG_KEY)


def _config(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id}}


# Upstream listing/thread/copy/prune/run-deletion methods are intentionally unsupported.
class DjangoCheckpointSaver(BaseCheckpointSaver[int]):  # pylint: disable=abstract-method
    """Store LangGraph checkpoints and pending writes through the synchronous Django ORM API."""

    _HISTORY_WINDOW_SIZE = 64

    @staticmethod
    def _complete_checkpoints():
        """Return a lazy queryset containing only complete checkpoints."""
        return AgentCheckpoint.objects.filter(
            checkpoint__isnull=False,
            checkpoint_type__isnull=False,
            metadata__isnull=False,
            metadata_type__isnull=False,
        )

    def _checkpoint_tuple(self, row: AgentCheckpoint, thread_id: str) -> CheckpointTuple:
        """Build a tuple from a complete checkpoint row."""
        metadata = self.serde.loads_typed((cast(str, row.metadata_type), bytes(cast(bytes, row.metadata))))
        prefetched_writes = getattr(row, "_prefetched_objects_cache", {}).get("writes")
        writes = (
            prefetched_writes
            if prefetched_writes is not None
            else AgentCheckpointWrite.objects.filter(checkpoint=row).order_by("task_id", "idx")
        )
        return CheckpointTuple(
            config=_config(thread_id, cast(str, row.checkpoint_ns), cast(str, row.checkpoint_id)),
            checkpoint=self.serde.loads_typed((cast(str, row.checkpoint_type), bytes(cast(bytes, row.checkpoint)))),
            metadata=metadata,
            parent_config=(
                _config(thread_id, cast(str, row.checkpoint_ns), cast(str, row.parent_checkpoint_id))
                if row.parent_checkpoint_id
                else None
            ),
            pending_writes=[
                (write.task_id, write.channel, self.serde.loads_typed((write.value_type, bytes(write.value))))
                for write in writes
            ],
        )

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """Return delta writes and nearest stored seeds by traversing bounded ORM windows."""
        if not channels:
            return {}

        configurable = cast(dict, config)["configurable"]
        target = self.get_tuple(config)
        if target is None:
            if configurable.get("checkpoint_id"):
                raise RuntimeError("Checkpoint history is unavailable; reload the conversation.")
            return {channel: {"writes": []} for channel in channels}
        result: dict[str, DeltaChannelHistory] = {channel: {"writes": []} for channel in channels}
        # LangGraph version keys persist after the first update, unlike periodically stored delta values.
        remaining = set(channels).intersection(target.checkpoint["channel_versions"])
        if target.parent_config is None:
            # A reader may hold the pre-prune payload of a row now rewritten as a root snapshot.
            values = target.checkpoint["channel_values"]
            result.update(
                {
                    channel: {"seed": values[channel], "writes": []}
                    for channel in remaining
                    if isinstance(values.get(channel), _DeltaSnapshot)
                }
            )
        elif remaining:
            cursor_id = cast(dict, target.parent_config)["configurable"]["checkpoint_id"]
            self._collect_ancestor_history(configurable, cursor_id, channels, remaining, result)
        return result

    def _collect_ancestor_history(self, configurable, cursor_id, channels, remaining, result) -> None:
        """Walk parents newest-first, collecting writes until every remaining channel reaches a stored seed."""
        thread_id = str(configurable["thread_id"])
        rows = self._complete_checkpoints().filter(
            conversation_id=thread_id,
            checkpoint_ns=configurable.get("checkpoint_ns", ""),
        )
        rows = rows.prefetch_related(
            Prefetch(
                "writes",
                queryset=AgentCheckpointWrite.objects.filter(channel__in=channels).order_by("task_id", "idx"),
            )
        )
        for row in self._root_chain(rows, cursor_id):
            checkpoint = self._checkpoint_tuple(row, thread_id)
            for write in reversed(checkpoint.pending_writes or ()):
                if write[1] in remaining:
                    result[write[1]]["writes"].append(write)
            values = checkpoint.checkpoint["channel_values"]
            for channel in remaining.intersection(values):
                result[channel]["seed"] = values[channel]
            remaining.difference_update(values)
            if not remaining:
                break
        for history in result.values():
            history["writes"].reverse()

    def _root_chain(self, rows, start_id) -> Iterator[AgentCheckpoint]:
        """Follow actual parents using bounded windows, with caller-owned loading policy."""
        cursor_id = start_id
        window: dict[str, AgentCheckpoint] = {}
        while cursor_id:
            if cursor_id not in window:
                window = {
                    row.checkpoint_id: row
                    for row in rows.filter(checkpoint_id__lte=cursor_id).order_by("-checkpoint_id")[
                        : self._HISTORY_WINDOW_SIZE
                    ]
                }
            row = window.get(cursor_id)
            if row is None:
                raise RuntimeError("Checkpoint ancestry is unavailable; reload the conversation.")
            yield row
            cursor_id = row.parent_checkpoint_id

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Return the exact or latest complete checkpoint for the thread and namespace."""
        configurable = cast(dict, config)["configurable"]
        thread_id = str(configurable["thread_id"])
        rows = self._complete_checkpoints().filter(
            conversation_id=thread_id,
            checkpoint_ns=configurable.get("checkpoint_ns", ""),
        )
        if checkpoint_id := configurable.get("checkpoint_id"):
            row = rows.filter(checkpoint_id=checkpoint_id).first()
        else:
            row = rows.order_by("-checkpoint_id").first()
        if row is None:
            return None
        return self._checkpoint_tuple(row, thread_id)

    def _root_retention_boundary(self, thread_id: str) -> str | None:
        """Find the oldest ancestor needed by the latest root's delta channels."""
        rows = self._complete_checkpoints().filter(conversation_id=thread_id, checkpoint_ns="")
        latest = rows.order_by("-checkpoint_id").first()
        if latest is None:
            return None
        checkpoint = self.serde.loads_typed((cast(str, latest.checkpoint_type), bytes(cast(bytes, latest.checkpoint))))
        remaining = {"messages", "files"}.intersection(checkpoint["channel_versions"])
        boundary = cast(str, latest.checkpoint_id)
        # History reconstruction starts at the parent, even when the latest row has a snapshot.
        for row in self._root_chain(rows, latest.parent_checkpoint_id if remaining else None):
            boundary = min(boundary, cast(str, row.checkpoint_id))
            checkpoint = self.serde.loads_typed((cast(str, row.checkpoint_type), bytes(cast(bytes, row.checkpoint))))
            remaining.difference_update(checkpoint["channel_values"])
            if not remaining:
                break
        # Without a seed, reconstruction needs the complete write history back to the root.
        return None if remaining else boundary

    def _snapshot_root(self, thread_id: str, channels: Mapping[str, Any]) -> str | None:
        """Materialize delta state before severing history; pending latest writes stay pending."""
        config = thread_config(thread_id)
        latest = self.get_tuple(config)
        if latest is None:
            return None
        checkpoint = latest.checkpoint
        missing = [
            name
            for name, channel in channels.items()
            if isinstance(channel, DeltaChannel)
            and name in checkpoint["channel_versions"]
            and name not in checkpoint["channel_values"]
        ]
        histories = self.get_delta_channel_history(config=latest.config, channels=missing)
        for name in missing:
            history = histories[name]
            channel = channels[name].from_checkpoint(history.get("seed", MISSING))
            channel.replay_writes(history["writes"])
            checkpoint["channel_values"][name] = _DeltaSnapshot(channel.get())
        # Never discard an active delta channel whose reducer is unavailable.
        if not {"messages", "files"}.intersection(checkpoint["channel_versions"]).issubset(
            checkpoint["channel_values"]
        ):
            return None
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        AgentCheckpoint.objects.filter(
            conversation_id=thread_id, checkpoint_ns="", checkpoint_id=checkpoint["id"]
        ).update(checkpoint_type=checkpoint_type, checkpoint=checkpoint_blob, parent_checkpoint_id=None)
        return checkpoint["id"]

    def prune_turn(self, config: RunnableConfig, *, channels: Mapping[str, Any]) -> None:
        """Discard finished subagents and root history preceding all required delta seeds."""
        thread_id = str(cast(dict, config)["configurable"]["thread_id"])
        try:
            with require_run_guard(config).write(thread_id), cast(AbstractContextManager, transaction.atomic()):
                rows = AgentCheckpoint.objects.filter(conversation_id=thread_id)
                boundary = self._root_retention_boundary(thread_id)
                if boundary is None:
                    boundary = self._snapshot_root(thread_id, channels)
                if boundary is not None:
                    rows.filter(checkpoint_ns="", checkpoint_id__lt=boundary).only("pk").delete()
                    rows.filter(checkpoint_ns="", checkpoint_id=boundary).update(parent_checkpoint_id=None)
                rows.exclude(checkpoint_ns="").only("pk").delete()
        finally:
            close_connections()

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store one complete checkpoint, preserving writes already linked to it."""
        configurable = cast(dict, config)["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        parent_id = configurable.get("checkpoint_id")
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        guard = require_run_guard(config)
        metadata_type, metadata_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        try:
            with guard.write(thread_id):
                if parent_id is not None and checkpoint["id"] <= parent_id:
                    raise ValueError(
                        "Checkpoint ID must increase beyond its parent; check worker clock synchronization."
                    )
                with cast(AbstractContextManager, transaction.atomic()):
                    AgentCheckpoint.objects.update_or_create(
                        conversation_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint["id"],
                        defaults={
                            "parent_checkpoint_id": configurable.get("checkpoint_id"),
                            "checkpoint_type": checkpoint_type,
                            "metadata_type": metadata_type,
                            "checkpoint": checkpoint_blob,
                            "metadata": metadata_blob,
                        },
                    )
        finally:
            close_connections()
        return attach_run_guard(_config(thread_id, checkpoint_ns, checkpoint["id"]), guard)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes; ordinary slots keep their first value, reserved slots are replaced."""
        configurable = cast(dict, config)["configurable"]
        thread_id = str(configurable["thread_id"])
        encoded = [
            (WRITES_IDX_MAP.get(channel, index), channel, *self.serde.dumps_typed(value))
            for index, (channel, value) in enumerate(writes)
        ]
        try:
            with require_run_guard(config).write(thread_id), cast(AbstractContextManager, transaction.atomic()):
                checkpoint, _ = AgentCheckpoint.objects.get_or_create(
                    conversation_id=thread_id,
                    checkpoint_ns=configurable.get("checkpoint_ns", ""),
                    checkpoint_id=configurable["checkpoint_id"],
                )
                for idx, channel, value_type, value_blob in encoded:
                    defaults = {
                        "channel": channel,
                        "value_type": value_type,
                        "value": value_blob,
                    }
                    if idx >= 0:
                        AgentCheckpointWrite.objects.get_or_create(
                            checkpoint=checkpoint, task_id=task_id, idx=idx, defaults=defaults
                        )
                    else:
                        AgentCheckpointWrite.objects.update_or_create(
                            checkpoint=checkpoint, task_id=task_id, idx=idx, defaults=defaults
                        )
        finally:
            close_connections()
