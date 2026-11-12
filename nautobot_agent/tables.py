"""Tables for the Nautobot Agent app."""

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from nautobot.apps.tables import BaseTable, BooleanColumn, ButtonsColumn, ToggleColumn

from nautobot_agent.chat import run_status
from nautobot_agent.models import AgentConversation, AgentExecutionTrace, LLMConfiguration, MCPServer, Skill


class AgentExecutionTraceTable(BaseTable):
    """Render the permission-aware summary of execution traces."""

    id = tables.Column(verbose_name="Trace", linkify=True)
    status = tables.Column(verbose_name="Execution")
    diagnostics = tables.Column(accessor="capture_state", verbose_name="Diagnostics")
    turn = tables.Column(accessor="turn_number", verbose_name="Turn #", empty_values=(), orderable=False)
    user = tables.Column(accessor="conversation__user__username", verbose_name="User", orderable=False)
    conversation = tables.Column(empty_values=(), orderable=False)
    request = tables.Column(accessor="request_preview", verbose_name="Request", empty_values=(), orderable=False)
    duration_ms = tables.Column(
        accessor="duration_seconds",
        verbose_name="Duration (s)",
        empty_values=(),
        orderable=True,
        order_by=("duration_ms",),
    )

    def __init__(self, *args, user=None, **kwargs):
        """Exclude request previews for users without full-inspection access."""
        super().__init__(*args, user=user, **kwargs)
        if not AgentExecutionTrace.full_inspection_allowed(user):
            self.exclude = (*self.exclude, "request")

    def before_render(self, request):
        """Reconcile only the authorized traces on the displayed page."""
        run_status.reconcile_trace_runs([row.record for row in self.paginated_rows])

    def render_status(self, record):
        """Keep unconfirmed execution distinct from capture diagnostics."""
        return record.execution_label

    def render_diagnostics(self, record):
        """Render the independent persisted capture state."""
        return record.get_capture_state_display()

    def render_turn(self, record):
        """Link the model-projected turn number to its trace."""
        return format_html('<a href="{}">{}</a>', record.get_absolute_url(), record.turn_number)

    def render_conversation(self, record):
        """Show conversation titles only when the conversation is readable."""
        if not record.conversation_visible:
            return "Conversation"
        title = record.conversation.title or "Untitled conversation"
        return format_html('<a href="{}">{}</a>', record.conversation.get_absolute_url(), title)

    def render_request(self, record):
        """Render at most 100 characters with an explicit continuation marker."""
        preview = record.request_preview or ""
        return f"{preview[:100]}…" if len(preview) > 100 else preview

    def render_duration_ms(self, value):
        """Format the model-projected seconds without treating zero as missing."""
        return f"{value:.2f}" if value is not None else "—"

    class Meta(BaseTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Configure approved trace summary columns."""

        model = AgentExecutionTrace
        fields = (
            "id",
            "turn",
            "user",
            "conversation",
            "request",
            "status",
            "diagnostics",
            "started_at",
            "duration_ms",
        )


class AgentConversationTable(BaseTable):
    """Render permission-restricted conversation history with owner-only resume."""

    title = tables.Column(
        empty_values=(),
        orderable=False,
        attrs={
            "th": {"style": "width: 28rem"},
            "td": {"style": "width: 28rem; max-width: 28rem; white-space: normal; overflow-wrap: anywhere"},
        },
    )
    user = tables.Column(accessor="user__username", verbose_name="User", orderable=False)
    created_at = tables.Column(verbose_name="Created", orderable=False)
    last_activity_at = tables.Column(verbose_name="Last activity", orderable=False)
    resume = tables.Column(empty_values=(), orderable=False)
    actions = ButtonsColumn(AgentConversation, buttons=("edit", "delete"))
    traces = tables.Column(accessor="trace_count", verbose_name="Traces", empty_values=(), orderable=False)

    def __init__(self, *args, user=None, **kwargs):
        """Hide trace counts unless the user may view trace summaries."""
        super().__init__(*args, user=user, **kwargs)
        self.request_user = user
        if not AgentExecutionTrace.summary_view_allowed(user):
            self.exclude = (*self.exclude, "traces")

    def render_title(self, record):
        """Keep long titles bounded, linked, and expandable without JavaScript."""
        label = record.title or str(record)
        link = format_html(
            '<a href="{}">{}</a>', record.get_absolute_url(), label[:59] + "…" if len(label) > 60 else label
        )
        if len(label) <= 60:
            return link
        return format_html(
            '<div class="text-break" style="max-width: 28rem; white-space: normal; overflow-wrap: anywhere">'
            '{}<details><summary class="small text-muted">Show full title</summary>{}</details></div>',
            link,
            label,
        )

    def render_traces(self, record):
        """Link a nonzero trace count to its conversation-filtered trace list."""
        count = record.trace_count
        if not count:
            return "—"
        url = f"{reverse('plugins:nautobot_agent:agentexecutiontrace_list')}?conversation={record.pk}"
        label = f"{count} trace" if count == 1 else f"{count} traces"
        return format_html('<a href="{}">{}</a>', url, label)

    def render_resume(self, record):
        """Render a native HTMX POST button only for the row's owner."""
        if not self.request_user or record.user_id != self.request_user.pk:
            return "—"
        url = reverse("plugins:nautobot_agent:agentconversation_resume", kwargs={"pk": record.pk})
        return format_html(
            '<button type="button" hx-post="{}" hx-swap="none" hx-params="none">Resume</button>',
            url,
        )

    class Meta(BaseTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Configure the conversation summary columns."""

        model = AgentConversation
        fields = ("title", "user", "created_at", "last_activity_at", "traces", "resume", "actions")


class AllConversationsTable(AgentConversationTable):  # pylint: disable=nb-no-model-found
    """Add native bulk selection only to the cross-user conversation list."""

    pk = ToggleColumn()

    def __init__(self, *args, user=None, **kwargs):
        """Hide bulk selection when the user cannot perform native deletes."""
        super().__init__(*args, user=user, **kwargs)
        if not user or not user.has_perm("nautobot_agent.delete_agentconversation"):
            self.exclude = (*self.exclude, "pk")

    class Meta(AgentConversationTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Keep the checkbox first and retain every existing history column."""

        default_columns = ("pk", *AgentConversationTable.Meta.fields)


class LLMConfigurationTable(BaseTable):
    """List LLM configurations with links to their native detail pages."""

    name = tables.Column(linkify=True)
    is_default = BooleanColumn()
    is_scope_classifier = BooleanColumn()
    actions = ButtonsColumn(LLMConfiguration, buttons=("detail", "edit", "delete"))

    class Meta(BaseTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Configure the configuration metadata columns."""

        model = LLMConfiguration
        fields = ("name", "model", "credential_profile", "is_default", "is_scope_classifier", "actions")


class MCPServerTable(BaseTable):
    """List MCP servers with native detail and mutation actions."""

    name = tables.Column(linkify=True)
    enabled = BooleanColumn()
    actions = ButtonsColumn(MCPServer, buttons=("detail", "edit", "delete"))

    class Meta(BaseTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Configure MCP server columns."""

        model = MCPServer
        fields = ("name", "transport", "credential_profile", "enabled", "actions")


class SkillTable(BaseTable):
    """List skills without loading their potentially large instruction bodies."""

    name = tables.Column(linkify=True)
    enabled = BooleanColumn()
    actions = ButtonsColumn(Skill, buttons=("detail", "edit", "delete"))

    class Meta(BaseTable.Meta):  # pylint: disable=too-few-public-methods  # ty: ignore[invalid-attribute-override] django-tables2 inner Meta configuration.
        """Configure the skill columns."""

        model = Skill
        fields = ("name", "identifier", "description", "enabled", "actions")
