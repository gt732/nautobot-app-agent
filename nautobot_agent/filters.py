"""Native filters for Agent conversations and execution traces."""

import django_filters
from nautobot.apps.filters import BaseFilterSet, SearchFilter

from nautobot_agent.models import (
    TRACE_EXECUTION_LABELS,
    AgentConversation,
    AgentExecutionTrace,
    TraceCaptureState,
)


class AgentConversationFilterSet(BaseFilterSet):
    """Search conversations by title, UUID, or username."""

    user = django_filters.CharFilter(field_name="user__username", lookup_expr="exact")
    q = SearchFilter(
        filter_predicates={"id": "icontains", "title": "icontains", "user__username": "icontains"},
        strip=True,
    )

    class Meta:
        """Filter metadata."""

        model = AgentConversation
        fields = ()


class AgentExecutionTraceFilterSet(BaseFilterSet):
    """Filter trace summaries without exposing or searching request payloads."""

    conversation = django_filters.UUIDFilter(field_name="conversation_id")
    user = django_filters.CharFilter(field_name="conversation__user__username", lookup_expr="exact")
    status = django_filters.ChoiceFilter(
        field_name="status", choices=tuple(TRACE_EXECUTION_LABELS.items()), label="Execution"
    )
    capture_state = django_filters.ChoiceFilter(choices=TraceCaptureState.choices, label="Diagnostics")
    started_after = django_filters.DateTimeFilter(field_name="started_at", lookup_expr="gte")
    started_before = django_filters.DateTimeFilter(field_name="started_at", lookup_expr="lte")
    q = SearchFilter(
        filter_predicates={
            "id": "icontains",
            "conversation__user__username": "icontains",
        },
        strip=True,
    )

    class Meta:
        """Filter metadata."""

        model = AgentExecutionTrace
        fields = ()
