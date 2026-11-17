"""Conversation selection, model-free state restoration, and non-send orchestration."""

import logging
import time
import uuid
from collections.abc import Iterable, MutableMapping, Sequence
from typing import Any, NamedTuple, cast

from django.core.exceptions import SuspiciousOperation, ValidationError
from django.db import router, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404

from nautobot_agent import app_settings
from nautobot_agent.chat import history as chat_history
from nautobot_agent.chat import run_status, runs
from nautobot_agent.chat.errors import BUSY_MESSAGE, UNAVAILABLE_MESSAGE, ConversationBusy
from nautobot_agent.chat.history import EMPTY_CONTEXT_USAGE
from nautobot_agent.forms import CanonicalUUIDField
from nautobot_agent.models import (
    AgentCheckpoint,
    AgentCheckpointWrite,
    AgentConversation,
    AgentExecutionSpan,
    AgentRun,
)

logger = logging.getLogger(__name__)

CONVERSATION_KEY = "nautobot_agent_conversation_id"
DELETE_BATCH_SIZE = 100

DEPARTURE_COOKIE_NAME = "nautobot_agent_chat_departure"
_MAX_DEPARTURE_COOKIE_LENGTH = 128


def departure_expired(
    value: str | None,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    timeout_seconds: int,
) -> bool:
    """Return whether a bounded departure marker matches this user and selection and has expired."""
    if not isinstance(value, str) or len(value) > _MAX_DEPARTURE_COOKIE_LENGTH:
        return False
    fields = value.split(":")
    if len(fields) != 3:
        return False
    marker_user, marker_conversation, marker_timestamp = fields
    if marker_user != str(user_id) or marker_conversation not in ("new", str(conversation_id)):
        return False
    if not marker_timestamp.isascii() or not marker_timestamp.isdigit():
        return False
    departure_ms = int(marker_timestamp)
    if departure_ms <= 0:
        return False
    current_ms = time.time_ns() // 1_000_000
    return departure_ms <= current_ms and current_ms - departure_ms >= timeout_seconds * 1000


class Transcript(NamedTuple):
    """Read-only retained conversation transcript and availability status."""

    entries: list[dict[str, str]]
    available: bool


def transcript(target: AgentConversation) -> Transcript:
    """Read retained user/assistant text without constructing an agent."""
    try:
        return Transcript(chat_history.read_transcript(target), True)
    except Exception as exc:  # noqa: BLE001  # an unreadable transcript is shown as unavailable
        logger.error("Conversation transcript read failed (%s)", type(exc).__name__)
        return Transcript([], False)


def page_state(request) -> tuple[dict[str, Any], str | None]:
    """Resolve an explicit or convenient selection and restore it without model construction."""
    explicit = request.GET.getlist("conversation")
    if len(explicit) > 1:
        raise SuspiciousOperation("Only one conversation selector is allowed.")
    if not explicit:
        selected = resolve(request)
        marker = request.COOKIES.get(DEPARTURE_COOKIE_NAME)
        if selected is not None and departure_expired(
            marker,
            request.user.pk,
            cast(uuid.UUID, selected.pk),
            app_settings.chat_return_timeout(),
        ):
            _forget(request.session)
            selected = None
        return restore_state(selected)
    if explicit[0] == "new":
        _forget(request.session)
        return restore_state(None)
    try:
        payload, notice = conversation_state(request, explicit[0])
    except ValidationError as exc:
        raise SuspiciousOperation("Invalid conversation UUID.") from exc
    select(request.session, uuid.UUID(payload["conversation_id"]))
    return payload, notice


def conversation_state(request, selector: str) -> tuple[dict[str, Any], str | None]:
    """Resolve and restore an owned conversation without changing the session selection."""
    conversation_id = CanonicalUUIDField(required=True).clean(selector)
    selected = get_object_or_404(AgentConversation.objects.owner_history(request.user), pk=conversation_id)
    return restore_state(selected)


def select(session: MutableMapping[str, Any], conversation_id: uuid.UUID) -> None:
    """Record a conversation UUID already resolved through an owned path.

    Callers must establish ownership through conversation selection, run admission, or
    native owner-scoped object lookup; this trusted-input recorder is not authorization.
    """
    session[CONVERSATION_KEY] = str(conversation_id)


def restore_state(selected: AgentConversation | None) -> tuple[dict[str, Any], str | None]:
    """Restore rich state and display metadata, retaining a degraded history notice on failure."""
    history = []
    todos = []
    context_usage = EMPTY_CONTEXT_USAGE
    notice = None
    active_run = latest_run = None
    model_name = None
    if selected is not None:
        try:
            latest_run = run_status.reconcile_latest_run(selected)
            active_run = latest_run if latest_run is not None and not latest_run.is_terminal else None
            model_name, limit = _display_metadata(selected, active_run or latest_run)
            history, todos, context_usage = chat_history.read_chat_state(selected, limit=limit)
        except Exception as exc:  # noqa: BLE001  # the page still loads with a history-unavailable notice
            logger.error("Conversation state restore failed (%s)", type(exc).__name__)
            notice = "Conversation history is currently unavailable."
    return {
        "conversation_id": str(selected.pk) if selected is not None else "new",
        "history": history,
        "todos": todos,
        "context_usage": context_usage,
        "model_name": model_name,
        "active_run": run_status.run_summary(active_run) if active_run else None,
        "latest_run": run_status.run_summary(latest_run) if latest_run else None,
    }, notice


def _display_metadata(selected: AgentConversation, candidate: AgentRun | None):
    """Use the latest captured model metadata; optional display failure never loses retained state."""
    try:
        if candidate is None or not candidate.display_model:
            candidate = (
                AgentRun.objects.filter(conversation=selected)
                .exclude(display_model="")
                .order_by("-submitted_at")
                .first()
            )
        return (candidate.display_model, candidate.display_context_limit) if candidate else (None, None)
    except Exception as exc:  # noqa: BLE001  # optional display metadata never loses retained state
        logger.warning("Conversation display metadata unavailable (%s)", type(exc).__name__)
        return None, None


def _forget(session: MutableMapping[str, Any]) -> None:
    """Clear the selected conversation from the session."""
    session.pop(CONVERSATION_KEY, None)


def resolve(request) -> AgentConversation | None:
    """Return the convenient owned selection, forgetting malformed, stale, or foreign references."""
    reference = request.session.get(CONVERSATION_KEY)
    conversation_id = None
    if isinstance(reference, str):
        try:
            conversation_id = uuid.UUID(reference)
        except ValueError:
            conversation_id = None
    if conversation_id is not None:
        conversation = AgentConversation.objects.owner_history(request.user).filter(pk=conversation_id).first()
        if conversation is not None:
            return conversation
    _forget(request.session)
    return None


def _delete_storage_children(conversation_ids: Sequence[uuid.UUID], *, using: str) -> None:
    """Raw-delete children for locked eligible conversations, intentionally bypassing deletion signals."""
    # This guarded path deliberately uses Django's collector-free deletion API.
    # pylint: disable=protected-access
    AgentCheckpointWrite.objects.using(using).filter(
        checkpoint__conversation_id__in=conversation_ids,
    )._raw_delete(using)
    AgentCheckpoint.objects.using(using).filter(
        conversation_id__in=conversation_ids,
    )._raw_delete(using)
    AgentExecutionSpan.objects.using(using).filter(
        trace__conversation_id__in=conversation_ids,
    )._raw_delete(using)


def delete(request, conversation_id: uuid.UUID) -> None:
    """Delete one conversation through the shared guarded workflow."""
    deleted_count, skipped_messages = delete_many(request, (conversation_id,))
    if deleted_count == 1:
        return
    if BUSY_MESSAGE in skipped_messages:
        raise ConversationBusy(BUSY_MESSAGE)
    raise Http404


def delete_many(request, ids: Iterable[uuid.UUID]) -> tuple[int, list[str]]:  # pylint: disable=too-many-locals
    """Delete eligible conversations in atomic batches and return deduplicated skip reasons."""
    conversation_ids = list(dict.fromkeys(uuid.UUID(str(conversation_id)) for conversation_id in ids))
    if not conversation_ids:
        return 0, []

    using = router.db_for_write(AgentConversation)
    conversations = AgentConversation.objects.using(using).restrict(request.user, "delete")
    owner_by_id = dict(conversations.filter(pk__in=conversation_ids).values_list("pk", "user_id"))

    busy_ids: set[uuid.UUID] = set()
    unavailable_ids: set[uuid.UUID] = set()
    deleted_count = 0
    for offset in range(0, len(conversation_ids), DELETE_BATCH_SIZE):
        chunk = conversation_ids[offset : offset + DELETE_BATCH_SIZE]
        chunk_owners = {pk: owner_by_id[pk] for pk in chunk if pk in owner_by_id}
        with transaction.atomic(using=using):
            locked = runs.lock_conversation_reservations(chunk_owners, conversations, using=using)
            locked_ids = {conversation.pk for conversation, _ in locked}
            busy_ids.update(cast(uuid.UUID, conversation.pk) for conversation, reserved in locked if reserved)
            unavailable_ids.update(set(chunk).difference(locked_ids))
            eligible_ids = [cast(uuid.UUID, conversation.pk) for conversation, reserved in locked if not reserved]
            _delete_storage_children(eligible_ids, using=using)
            _, per_model_counts = conversations.filter(pk__in=eligible_ids).delete()
            deleted_count += per_model_counts.get(cast(Any, AgentConversation)._meta.label, 0)
        if request.session.get(CONVERSATION_KEY) in {str(pk) for pk in eligible_ids}:
            _forget(request.session)

    skipped_messages: list[str] = []
    for conversation_id in conversation_ids:
        if conversation_id in busy_ids:
            message = BUSY_MESSAGE
        elif conversation_id in unavailable_ids:
            message = UNAVAILABLE_MESSAGE
        else:
            continue
        if message not in skipped_messages:
            skipped_messages.append(message)
    return deleted_count, skipped_messages
