"""Forms for the Nautobot Agent app."""

import uuid
from typing import ClassVar

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from nautobot.apps.forms import BootstrapMixin, ConfirmationForm

from nautobot_agent import app_settings
from nautobot_agent.models import AgentConversation, LLMConfiguration, MCPServer, MCPTransport, Skill


def _profile_choices(field, choices, saved, known):
    """Offer current profiles while displaying, but rejecting, a saved stale profile."""
    field.choices = choices
    if saved and saved not in known:
        field.widget.choices = [*choices, (saved, f"{saved} (unavailable)")]


class CanonicalUUIDField(forms.Field):
    """Validate the run JSON protocol's canonical string UUIDs without scalar coercion."""

    # Django defines this overridable field configuration as an instance-typed attribute.
    default_error_messages: dict[str, str] = {"invalid": "Enter a canonical UUID string."}  # noqa: RUF012

    def to_python(self, value):
        """Parse once and keep nullable input distinct from an empty or noncanonical string."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValidationError(self.error_messages["invalid"], code="invalid") from exc
        if str(parsed) != value.lower():
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return parsed


class AgentRunSubmissionForm(forms.Form):
    """Normalize the JSON payload used to submit one durable run."""

    message = forms.CharField(strip=True, max_length=32_000)
    conversation_id = CanonicalUUIDField(required=False)
    client_request_id = CanonicalUUIDField(required=True)

    def clean(self):
        """Validate the JSON shape without Django's permissive scalar coercions."""
        cleaned = super().clean()
        if set(self.data) != set(self.fields):
            raise ValidationError("Invalid request body.")
        if not isinstance(self.data.get("message"), str):
            self.add_error("message", "Enter a string.")
        return cleaned


class AgentConversationForm(BootstrapMixin, forms.ModelForm):
    """Rename an existing conversation without writing runtime-owned metadata."""

    title = forms.CharField(max_length=255, error_messages={"required": "Enter a title."})

    class Meta:
        """Form metadata."""

        model = AgentConversation
        fields = ("title",)

    def save(self, commit=True):
        """Persist only the title for an existing conversation."""
        instance = super().save(commit=False)
        if commit:
            instance.save(update_fields=("title",))
        return instance


class AgentConversationBulkDeleteForm(ConfirmationForm):
    """Block the generic deletion Job, which bypasses conversation reservation locks."""

    def __init__(self, model, *args, delete_all=False, **kwargs):
        """Accept Nautobot's bulk-delete Job arguments without exposing a deletion form."""
        del model, delete_all
        super().__init__(*args, **kwargs)

    def perform_pre_delete(self, queryset):
        """Require the guarded conversation deletion views for every selection."""
        raise PermissionDenied("Delete conversations through Conversation History, not the generic bulk-delete Job.")


class LLMConfigurationForm(BootstrapMixin, forms.ModelForm):
    """Create or edit one LLM configuration; credential profiles are chosen by name only."""

    credential_profile = forms.ChoiceField(required=False, label="Credential Profile")

    class Meta:
        """Form metadata."""

        model = LLMConfiguration
        fields = ("name", "model", "credential_profile", "parameters", "is_default", "is_scope_classifier")
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {
            "is_default": forms.BooleanField,
            "is_scope_classifier": forms.BooleanField,
        }

    def __init__(self, *args, **kwargs):
        """Load current profile names without exposing deployment credential values."""
        super().__init__(*args, **kwargs)
        profiles = app_settings.credential_profiles()
        field = self.fields["credential_profile"]
        choices = [("", "---------"), *((name, name) for name in profiles)]
        saved = self.instance.credential_profile
        # Widget-only stale choices remain visible but fail validation when submitted unchanged.
        _profile_choices(field, choices, saved, profiles)

    def clean_parameters(self):
        """Treat an empty Parameters textarea as an empty object."""
        parameters = self.cleaned_data["parameters"]
        return {} if parameters is None else parameters


class SkillForm(BootstrapMixin, forms.ModelForm):
    """Guide administrators through authoring a global instruction skill."""

    name = forms.CharField(
        max_length=255,
        label="Name",
        help_text=(
            "Choose a friendly name, for example Device Audit. The application generates and keeps its identifier "
            "for you."
        ),
        error_messages={"required": "Enter a name."},
    )
    description = forms.CharField(
        max_length=1024,
        label="Description",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "Explain what this skill does and when the agent should use it; the agent uses this to choose relevant "
            "skills. Charting and Mermaid diagrams are built in; these records add your own custom instructions."
        ),
        error_messages={"required": "Enter a description."},
    )
    instructions = forms.CharField(
        label="Instructions",
        strip=False,
        widget=forms.Textarea(attrs={"rows": 16}),
        help_text=(
            "Write Markdown instructions that guide the agent through steps, examples of inputs and outputs, "
            "and common edge cases. Headings are optional. Keep instructions within 65,536 UTF-8 bytes; "
            "fewer than 500 lines is recommended, not required. Preserve formatting as written."
        ),
        error_messages={"required": "Enter instructions."},
    )
    enabled = forms.BooleanField(
        required=False,
        label="Enabled",
        help_text=(
            "When enabled, this skill is available globally starting with the next send. Active turns and "
            "retained history are not changed. Instructions guide the agent but do not add tools or grant "
            "permissions. Instructions may be sent to the configured model provider; do not include secrets."
        ),
    )

    class Meta:
        """Expose only administrator-authored skill fields."""

        model = Skill
        fields = ("name", "description", "instructions", "enabled")


class MCPServerForm(BootstrapMixin, forms.ModelForm):
    """Edit an MCP endpoint or one normalized stdio definition using deployment profile names only."""

    launch = forms.JSONField(
        required=False,
        label="MCP server JSON",
        widget=forms.Textarea(),
        help_text=(
            "For stdio only, enter a bare object with command and optional args, cwd, and env. "
            "Values are stored literally; never include secrets; use a deployment environment profile."
        ),
    )
    credential_profile = forms.ChoiceField(
        required=False,
        label="Credential Profile",
        help_text=(
            "Optional HTTP header or stdio environment profile, selected by transport. "
            "Values are deployment-only and never stored or displayed here."
        ),
    )

    class Meta:
        """Form metadata."""

        model = MCPServer
        fields = ("name", "transport", "url", "launch", "credential_profile", "enabled")
        field_classes: ClassVar[dict[str, type[forms.Field]]] = {"url": forms.CharField}
        labels: ClassVar[dict[str, str]] = {"url": "URL"}
        help_texts: ClassVar[dict[str, str]] = {
            "url": (
                "HTTP-only endpoint; do not include secrets, including in the query string. "
                "Tools run with this server's external access and may perform writes."
            ),
            "enabled": (
                "Enabling shares this server's tools with chat users. Configure only fully trusted servers; "
                "their tools may read or change external systems."
            ),
        }
        # URL validation is server-owned so an unused HTTP input cannot block a stdio submission.
        widgets: ClassVar[dict[str, forms.Widget]] = {"url": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        """Populate safe profile choices for the selected deployment profiles."""
        super().__init__(*args, **kwargs)
        http_profiles = app_settings.mcp_credential_profiles()
        stdio_profiles = app_settings.mcp_environment_profiles()
        choices = [
            ("", "---------"),
            ("HTTP header profiles", [(name, name) for name in http_profiles]),
            ("Stdio environment profiles", [(name, name) for name in stdio_profiles]),
        ]
        _profile_choices(
            self.fields["credential_profile"],
            choices,
            self.instance.credential_profile,
            {*http_profiles, *stdio_profiles},
        )
        if self.is_bound and self.data.get("transport") in MCPTransport.values:
            # Ignore the inactive input before its field validators run.
            data = self.data.copy()
            if data["transport"] == MCPTransport.STDIO:
                data["url"] = ""
            else:
                data["launch"] = "{}"
            self.data = data
