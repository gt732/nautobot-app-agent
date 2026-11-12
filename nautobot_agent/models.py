"""Models for the Nautobot Agent app."""
# pylint: disable=too-many-lines

# Keep persistent app entities together in Nautobot's conventional models module.

import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, ClassVar, cast
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, router, transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DurationField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Greatest, Now, Substr
from django.utils import timezone
from django.utils.text import slugify
from nautobot.apps.models import BaseManager, BaseModel, RestrictedQuerySet

from nautobot_agent import app_settings
from nautobot_agent.skills import BUILTIN_SKILL_IDENTIFIERS, SkillDefinition, builtin_skill_definitions

# Chat-model initializer controls owned by the app; never accepted as user-supplied constructor kwargs.
RESERVED_PARAMETER_KEYS = ("model", "model_provider", "configurable_fields", "config_prefix")


def parse_model_spec(value) -> tuple[str, str] | None:
    """Split ``provider:model`` at the first colon and normalize both components."""
    provider, separator, model_name = str(value or "").partition(":")
    provider = provider.strip()
    model_name = model_name.strip()
    return (provider, model_name) if separator and provider and model_name else None


def cascade_without_payload(collector, field, sub_objs, using):
    """Collect cascading children using only identity and their parent foreign key."""
    return models.CASCADE(collector, field, sub_objs.only("pk", field.attname), using)


cascade_without_payload.lazy_sub_objs = True  # ty: ignore[unresolved-attribute]


_ROLE_CONSTRAINT_FIELDS = {
    "nbagent_llm_default_unique": "is_default",
    "nbagent_llm_scope_unique": "is_scope_classifier",
}


def unique_constraint_name(error, vendor) -> str | None:
    """Extract named uniqueness diagnostics from supported database errors."""
    cause = error.__cause__
    if vendor == "postgresql":
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
        return constraint if isinstance(constraint, str) else None
    if vendor == "mysql" and getattr(cause, "args", ()) and cause.args[0] == 1062:
        message = str(cause.args[1]) if len(cause.args) > 1 else ""
        match = re.search(r" for key ['`](?:[^'`]+\.)?([^'`.]+)['`]$", message)
        if match:
            return match[1]
    return None


class NamedConstraintConflict(ValidationError):
    """A known named database constraint was violated during a save."""

    def __init__(self, field, message):
        """Expose a translated database conflict as a field validation error."""
        super().__init__({field: message})


class ConflictTranslatingSaveMixin:  # pylint: disable=too-few-public-methods
    """Translate only explicitly recognized named constraints raised during save."""

    def _save_translating(self, conflict_for, **kwargs):
        """Write on one resolved database alias and translate only recognized named conflicts."""
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        kwargs["using"] = using
        try:
            with transaction.atomic(using=using):
                return super().save(**kwargs)  # ty: ignore[unresolved-attribute] Concrete models supply save.
        except IntegrityError as error:
            conflict = conflict_for(unique_constraint_name(error, transaction.get_connection(using).vendor))
            if conflict is None:
                raise
            raise conflict from error


class LLMConfiguration(ConflictTranslatingSaveMixin, BaseModel):
    """One chat-model configuration with independent Agent and scope-classifier assignments."""

    name = models.CharField(max_length=255)
    model = models.CharField(max_length=512, help_text="Chat model as provider:model, e.g. openai:gpt-5.4.")
    credential_profile = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Name of a credential profile defined in the deployment's PLUGINS_CONFIG; blank for none.",
    )
    parameters = models.JSONField(
        default=dict,
        blank=True,
        help_text="Optional constructor keyword arguments as a JSON object. Do not store credentials here.",
    )
    is_default = models.BooleanField(
        default=None,
        blank=True,
        null=True,
        verbose_name="Default Agent model",
        help_text="Use this configuration as the chat model for the Nautobot Agent.",
    )
    is_scope_classifier = models.BooleanField(
        default=None,
        blank=True,
        null=True,
        verbose_name="Scope classifier",
        help_text=(
            "Use this configuration for the Nautobot scope classifier. "
            "If none is selected, the default Agent configuration is used."
        ),
    )
    # Model and classifier configuration identities use this timestamp for retained reuse.
    last_updated = models.DateTimeField(auto_now=True)

    # BaseModel defaults metadata association to True; disable it to drop the unused metadata UI.
    is_metadata_associable_model = False

    class Meta:
        """Model metadata."""

        ordering = ("name",)
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=("name",), name="nbagent_llm_name_unique"),
            models.UniqueConstraint(fields=("is_default",), name="nbagent_llm_default_unique"),
            models.UniqueConstraint(fields=("is_scope_classifier",), name="nbagent_llm_scope_unique"),
            models.CheckConstraint(
                condition=Q(is_default=True) | Q(is_default__isnull=True),
                name="nbagent_llm_default_true",
            ),
            models.CheckConstraint(
                condition=Q(is_scope_classifier=True) | Q(is_scope_classifier__isnull=True),
                name="nbagent_llm_scope_true",
            ),
        ]
        verbose_name = "LLM configuration"
        verbose_name_plural = "LLM configurations"

    def __str__(self) -> str:
        """Return the configuration name."""
        return str(self.name)

    def _normalize_roles(self):
        for field in _ROLE_CONSTRAINT_FIELDS.values():
            value = getattr(self, field)
            if value is False:
                setattr(self, field, None)
            elif value is not None and value is not True:
                raise ValidationError({field: "Role assignments must be True or None."})

    # pylint: disable-next=arguments-differ  # Positional save options are intentionally unsupported.
    def save(self, **kwargs):  # ty: ignore[invalid-method-override] Local callers use keyword-only save options.
        """Transfer selected roles atomically without broadening partial saves."""
        self._normalize_roles()
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        kwargs["using"] = using
        update_fields = kwargs.get("update_fields")
        with transaction.atomic(using=using):
            for field in _ROLE_CONSTRAINT_FIELDS.values():
                if getattr(self, field) is True and (update_fields is None or field in update_fields):
                    type(self).objects.using(using).filter(**{field: True}).exclude(pk=self.pk).update(
                        **{field: None, "last_updated": timezone.now()}
                    )
            return self._save_translating(self._constraint_conflict, **kwargs)

    def validate_constraints(self, exclude=None):
        """Allow role reassignment; normalization and database constraints protect role values."""
        super().validate_constraints(exclude=set(exclude or ()) | set(_ROLE_CONSTRAINT_FIELDS.values()))

    @staticmethod
    def _constraint_conflict(constraint):
        if constraint == "nbagent_llm_name_unique":
            return NamedConstraintConflict("name", "An LLM configuration with this name already exists.")
        field = _ROLE_CONSTRAINT_FIELDS.get(constraint)
        return (
            NamedConstraintConflict(field, "Another configuration was assigned this role while you were saving.")
            if field is not None
            else None
        )

    def resolve_model_arguments(self) -> tuple[str, str, dict[str, Any]]:
        """Validate and resolve the provider constructor arguments without mutating configuration."""
        errors = {}

        parsed = parse_model_spec(self.model)
        if parsed is None:
            errors["model"] = "Enter the model as provider:model."

        if not isinstance(self.parameters, dict):
            errors["parameters"] = "Parameters must be a JSON object."
            parameters = None
        else:
            parameters = self.parameters
            reserved_parameters = [key for key in RESERVED_PARAMETER_KEYS if key in parameters]
            if reserved_parameters:
                errors["parameters"] = f"Parameters must not set: {', '.join(reserved_parameters)}."

        profile_name = self.credential_profile
        profile = None
        if profile_name != "":
            profiles = app_settings.credential_profiles()
            if not isinstance(profile_name, str) or profile_name not in profiles:
                errors["credential_profile"] = "Select a credential profile defined in the deployment."
            else:
                profile = profiles[profile_name]
                reserved_profile = [key for key in RESERVED_PARAMETER_KEYS if key in profile]
                if reserved_profile:
                    errors["credential_profile"] = f"Credential profile must not set: {', '.join(reserved_profile)}."

        if errors or parsed is None or parameters is None:
            raise ValidationError(errors)

        provider, model_name = parsed
        kwargs = dict(parameters)
        if profile is not None:
            kwargs.update(profile)
        return provider, model_name, kwargs

    def clean(self):
        """Validate model arguments and normalize role assignments before constraint validation."""
        self._normalize_roles()
        super().clean()
        self.resolve_model_arguments()


class MCPTransport(models.TextChoices):  # pylint: disable=too-many-ancestors
    """Supported MCP launch transports."""

    HTTP = "http", "HTTP"
    STDIO = "stdio", "Standard input/output"


class MCPServer(ConflictTranslatingSaveMixin, BaseModel):
    """One trusted remote MCP server configuration."""

    name = models.CharField(
        max_length=255,
        help_text="Non-sensitive display label shown to chat users and sent to the configured model provider; it does not affect tool names.",
    )
    transport = models.CharField(
        max_length=8,
        choices=MCPTransport.choices,
        default=MCPTransport.HTTP,
    )
    url = models.CharField(
        max_length=2048,
        blank=True,
        default="",
        help_text="HTTP(S) Streamable HTTP endpoint. Do not include credentials or secrets in the URL or query string.",
    )
    launch = models.JSONField(blank=True, default=dict)
    credential_profile = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "For HTTP, an optional deployment-defined header profile; for stdio, an optional deployment-defined "
            "environment profile. Secret values are never stored or displayed here."
        ),
    )
    enabled = models.BooleanField(
        default=False,
        help_text="Advertise this trusted server's tools to chat users; remote tools may access or change external systems.",
    )
    last_updated = models.DateTimeField(auto_now=True)

    is_metadata_associable_model = False

    class Meta:
        """Model metadata."""

        ordering = ("name",)
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=("name",), name="nbagent_mcp_name_unique"),
        ]
        verbose_name = "MCP server"
        verbose_name_plural = "MCP servers"

    def __str__(self) -> str:
        """Return the administrator-supplied display name."""
        return str(self.name)

    # pylint: disable-next=arguments-differ  # Positional save options are intentionally unsupported.
    def save(self, **kwargs):  # ty: ignore[invalid-method-override] Local callers use keyword-only save options.
        """Translate identified duplicate-name races into field validation errors."""
        return self._save_translating(self._constraint_conflict, **kwargs)

    @staticmethod
    def _constraint_conflict(constraint):
        if constraint == "nbagent_mcp_name_unique":
            return NamedConstraintConflict("name", "An MCP server with this name already exists.")
        return None

    def resolve_credential_profile(self) -> dict[str, str]:
        """Resolve the selected deployment profile for this server's transport."""
        if self.transport == MCPTransport.HTTP:
            profiles = app_settings.mcp_credential_profiles()
        elif self.transport == MCPTransport.STDIO:
            profiles = app_settings.mcp_environment_profiles()
        else:
            raise ValidationError({"transport": "Select a valid transport."})

        name = self.credential_profile
        if not name:
            return {}
        if not isinstance(name, str):
            raise ValidationError({"credential_profile": "The selected profile is unavailable for this transport."})
        profile = profiles.get(name)
        if profile is None:
            raise ValidationError({"credential_profile": "The selected profile is unavailable for this transport."})
        if any(value is None for value in profile.values()):
            raise ValidationError({"credential_profile": "The selected profile contains unset deployment values."})
        return cast(dict[str, str], dict(profile))

    def clean(self):
        """Validate transport-specific MCP server configuration and its selected profile."""
        self.validate_configuration()

    def validate_configuration(self) -> dict[str, str]:
        """Validate transport settings and return the resolved deployment profile."""
        super().clean()
        errors = {}
        profile = {}
        if not isinstance(self.name, str) or not self.name.strip():
            errors["name"] = "Enter a nonblank display name."
        if self.transport == MCPTransport.HTTP:
            errors.update(self._http_errors())
        elif self.transport == MCPTransport.STDIO:
            errors.update(self._stdio_errors())
        if self.transport in MCPTransport.values:
            try:
                profile = self.resolve_credential_profile()
            except ValidationError as error:
                errors.update(error.message_dict)
        if errors:
            raise ValidationError(errors)
        return profile

    def _http_errors(self):
        """Validate the endpoint and reject stdio launch fields; both messages may apply."""
        errors = {}
        if self.launch != {}:
            errors["launch"] = "HTTP servers cannot include stdio launch fields."
        url = self.url
        if not isinstance(url, str) or any(character.isspace() for character in url):
            errors["url"] = "Enter an HTTP(S) URL."
            return errors
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            # Accessing the port rejects nonnumeric and out-of-range values.
            _ = parsed.port
        except (TypeError, ValueError):
            parsed = hostname = None
        if (
            parsed is None
            or parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or "#" in url
        ):
            errors["url"] = (
                "Enter an HTTP(S) URL with a hostname and without user information or a fragment. "
                "Do not include secrets in the URL or query string."
            )
        return errors

    def _stdio_errors(self):
        """Validate executable inputs without filesystem pre-checks or expansion."""
        errors = {}
        if not isinstance(self.url, str) or self.url != "":
            errors["url"] = "Stdio servers cannot include an HTTP URL."
        definition = self.launch
        if not isinstance(definition, dict) or set(definition) - {"command", "args", "cwd", "env"}:
            errors["launch"] = "Enter a bare launch object containing only command, args, cwd, and env."
            return errors
        command = definition.get("command")
        args = definition.get("args", [])
        cwd = definition.get("cwd", "")
        env = definition.get("env", {})
        messages = []
        if not isinstance(command, str) or not command.strip():
            messages.append("Enter a nonblank executable command.")
        elif "\0" in command or len(command) > 2048:
            messages.append("The command must be at most 2,048 characters and cannot contain NUL.")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            messages.append("Enter a list of string arguments.")
        elif any("\0" in arg for arg in args):
            messages.append("Arguments cannot contain NUL.")
        if not isinstance(cwd, str):
            messages.append("Enter a working directory path.")
        elif "\0" in cwd or len(cwd) > 2048:
            messages.append("The working directory must be at most 2,048 characters and cannot contain NUL.")
        elif cwd and not os.path.isabs(cwd):
            messages.append("Enter an absolute working directory path or leave it blank.")
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
        ):
            messages.append("Enter an object containing string environment names and values.")
        elif any(not app_settings.valid_mcp_environment_entry(key, value) for key, value in env.items()):
            messages.append("Environment names cannot be empty or contain NUL or '='; values cannot contain NUL.")
        if messages:
            errors["launch"] = messages
        return errors


_SKILL_IDENTIFIER_CONSTRAINT = "nbagent_skill_identifier_unique"


class SkillQuerySet(RestrictedQuerySet):
    """Skill queries shared by administration and unrestricted runtime capture."""

    def runtime_snapshot(self) -> tuple[SkillDefinition, ...]:
        """Combine configured built-ins with validated, enabled custom definitions."""
        rows = self.filter(enabled=True).order_by().only("name", "identifier", "description", "instructions")
        definitions = list(builtin_skill_definitions()) if app_settings.builtin_skills_enabled() else []
        for skill in rows:
            skill.validate_content()
            definitions.append(
                SkillDefinition(
                    identifier=str(skill.identifier),
                    description=str(skill.description),
                    instructions=str(skill.instructions),
                )
            )
        return tuple(sorted(definitions, key=lambda definition: definition.identifier))


class Skill(ConflictTranslatingSaveMixin, BaseModel):
    """Administrator-authored instruction content exposed as a generated skill document."""

    name = models.CharField(max_length=255)
    identifier = models.SlugField(max_length=64, editable=False, blank=True, default="", db_index=False)
    description = models.CharField(max_length=1024)
    instructions = models.TextField()
    enabled = models.BooleanField(default=False)
    last_updated = models.DateTimeField(auto_now=True)

    objects = BaseManager.from_queryset(SkillQuerySet)()
    is_metadata_associable_model = False

    class Meta:
        """Model metadata."""

        ordering = ("name", "pk")
        constraints: ClassVar[list] = [
            models.UniqueConstraint(fields=("identifier",), name=_SKILL_IDENTIFIER_CONSTRAINT),
        ]
        verbose_name = "Skill"
        verbose_name_plural = "Skills"

    def __str__(self) -> str:
        """Return the friendly administrator-facing name."""
        return str(self.name)

    def _identifier_conflict(self):
        return NamedConstraintConflict("name", f"The generated identifier '{self.identifier}' is already in use.")

    def _initial_identifier(self) -> str:
        """Generate the stable ASCII identifier from the initial friendly name."""
        value = slugify(self.name, allow_unicode=False).replace("_", "-")
        value = re.sub(r"[^a-z0-9-]", "-", value.lower())
        value = re.sub(r"-+", "-", value).strip("-")[:64].rstrip("-")
        return value or cast(UUID, self.pk).hex

    def validate_content(self) -> None:  # pylint: disable=too-many-branches
        """Validate normalized author content and the generated identifier contract."""
        errors = {}
        if not isinstance(self.name, str) or not self.name.strip():
            errors["name"] = "Enter a nonblank name."
        else:
            self.name = self.name.strip()  # ty: ignore[invalid-assignment] Django fields expose strings on instances.
            try:
                self.name.encode("utf-8")
            except UnicodeEncodeError:
                errors["name"] = "Name must contain valid UTF-8 text."
        if not isinstance(self.description, str) or not self.description.strip():
            errors["description"] = "Enter a nonblank description."
        else:
            self.description = self.description.strip()  # ty: ignore[invalid-assignment] Django instance string.
            try:
                self.description.encode("utf-8")
            except UnicodeEncodeError:
                errors["description"] = "Description must contain valid UTF-8 text."
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            errors["instructions"] = "Enter nonblank instructions."
        else:
            try:
                size = len(self.instructions.encode("utf-8"))
            except UnicodeEncodeError:
                errors["instructions"] = "Instructions must contain valid UTF-8 text."
            else:
                if size > 65536:
                    errors["instructions"] = "Instructions must be at most 65,536 UTF-8 bytes."
        if (
            not isinstance(self.identifier, str)
            or not 1 <= len(self.identifier) <= 64
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.identifier) is None
        ):
            errors["identifier"] = "Identifier must use lowercase letters, numbers, and single interior hyphens."
        if self.identifier in BUILTIN_SKILL_IDENTIFIERS:
            errors["name"] = f"The identifier '{self.identifier}' is reserved for a built-in skill."
        if errors:
            raise ValidationError(errors)

    def clean(self):
        """Normalize friendly fields and reject invalid or duplicate identities."""
        super().clean()
        if self._state.adding:
            self.identifier = self._initial_identifier()  # ty: ignore[invalid-assignment] Django instance string.
        self.validate_content()
        if Skill.objects.filter(identifier=self.identifier).exclude(pk=self.pk).exists():
            raise self._identifier_conflict()

    # pylint: disable-next=arguments-differ  # Positional save options are intentionally unsupported.
    def save(self, **kwargs):  # ty: ignore[invalid-method-override] Local callers use keyword-only save options.
        """Generate identity on insertion and translate named uniqueness races."""
        self.full_clean()
        return self._save_translating(
            lambda constraint: self._identifier_conflict() if constraint == _SKILL_IDENTIFIER_CONSTRAINT else None,
            **kwargs,
        )


class AgentConversationQuerySet(RestrictedQuerySet):
    """Reusable conversation visibility projections for UI and administration."""

    def owner_history(self, user):
        """Return only the user's personal history, including for superusers."""
        return self.filter(user_id=user.pk)

    def readable_by(self, user):
        """Return conversations in the user's ownership scope and native View restrictions."""
        if AgentConversation.view_all_allowed(user):
            queryset = self
        else:
            queryset = self.owner_history(user)
        return queryset.restrict(user, "view")

    def with_trace_counts(self, user):
        """Annotate counts only when the user may view trace summaries."""
        if not AgentExecutionTrace.summary_view_allowed(user):
            return self
        restricted_traces = AgentExecutionTrace.objects.restrict(user, "view")
        return self.annotate(trace_count=restricted_traces.count_per_conversation())


class AgentConversation(BaseModel):
    """One user-owned chat conversation; its primary key is the LangGraph thread ID."""

    runs: models.Manager

    objects = BaseManager.from_queryset(AgentConversationQuerySet)()

    title = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_activity_at = models.DateTimeField(default=timezone.now, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="nautobot_agent_conversations",
        db_index=False,
    )

    # BaseModel defaults metadata association to True; conversations are not metadata targets.
    is_metadata_associable_model = False

    class Meta:
        """Model metadata."""

        ordering = ("-last_activity_at", "id")
        indexes = (
            models.Index(fields=("user", "-last_activity_at", "id"), name="nbagent_conv_owner_activity"),
            models.Index(fields=("-last_activity_at", "id"), name="nbagent_conv_activity"),
        )
        verbose_name = "conversation"
        verbose_name_plural = "conversations"
        permissions = (("viewall_agentconversation", "View all conversations"),)

    @staticmethod
    def view_all_allowed(user) -> bool:
        """Return whether an active user may expand reads beyond personal history."""
        return bool(user and user.is_active and user.has_perm("nautobot_agent.viewall_agentconversation"))

    def __str__(self) -> str:
        """Return the conversation title or a UUID-based fallback."""
        return str(self.title) if self.title else f"Conversation {self.pk}"

    def record_acceptance(self) -> None:
        """Persist activity when a request is accepted."""
        self.last_activity_at = timezone.now()
        self.save(update_fields=("last_activity_at",))


class AgentRunStatus(models.TextChoices):  # pylint: disable=too-many-ancestors
    """Durable chat run lifecycle, distinct from trace and span outcomes."""

    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    REFUSED = "refused", "Refused"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    DISPATCH_FAILED = "dispatch_failed", "Dispatch failed"
    INTERRUPTED = "interrupted", "Interrupted"


class HeartbeatNow(Now):  # pylint: disable=abstract-method
    """Database statement time rendered in UTC on MySQL."""

    def as_mysql(self, compiler, connection, **extra_context):
        """Use a timezone-independent UTC timestamp on MySQL."""
        return "UTC_TIMESTAMP(6)", []


class AgentRunQuerySet(models.QuerySet):
    """Reusable durable-run state projections."""

    ACTIVE = (AgentRunStatus.QUEUED, AgentRunStatus.RUNNING)

    def active(self):
        """Return queued or running runs, the only ones that reserve their conversation."""
        return self.filter(status__in=self.ACTIVE)

    def with_heartbeat_stale(self):
        """Annotate whether the committed heartbeat is at or beyond the stale threshold."""
        return self.annotate(
            heartbeat_is_stale=Case(
                When(
                    heartbeat_at__isnull=False,
                    heartbeat_at__lte=HeartbeatNow() - Value(AgentRun.STALE_HEARTBEAT_AFTER),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )

    def with_queue_expired(self):
        """Annotate whether a queued deadline is at or before database statement time."""
        return self.annotate(
            queue_is_expired=Case(
                When(
                    status=AgentRunStatus.QUEUED,
                    queue_expires_at__lte=HeartbeatNow(),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )

    def with_queue_warning(self):
        """Project the queue warning threshold using database statement time."""
        return self.annotate(
            queue_warning_is_due=Case(
                When(
                    submitted_at__lte=HeartbeatNow() - Value(AgentRun.QUEUED_WARNING_AFTER),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )


class AgentRun(models.Model):  # pylint: disable=too-many-instance-attributes
    """Internal durable chat execution record; never a native CRUD/live-data resource."""

    heartbeat_is_stale: bool
    queue_is_expired: bool
    queue_warning_is_due: bool

    objects = models.Manager.from_queryset(AgentRunQuerySet)()
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="nautobot_agent_runs", db_index=False
    )
    conversation = models.ForeignKey(AgentConversation, on_delete=models.CASCADE, related_name="runs", db_index=False)
    conversation_id: UUID
    client_request_id = models.UUIDField()
    prompt = models.TextField()
    # NULL means no refusal; "" anchors a recorded refusal before retained graph messages.
    refusal_anchor_id = models.TextField(null=True, blank=True)  # pylint: disable=nb-string-field-blank-null
    status = models.CharField(max_length=20, choices=AgentRunStatus.choices, default=AgentRunStatus.QUEUED)
    submitted_at = models.DateTimeField()
    # Database-clock deadline captured at admission; queued work cannot start at or after it.
    queue_expires_at = models.DateTimeField()
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, blank=True, default="")
    live_events_incomplete = models.BooleanField(default=False)
    # Monotonic diagnostic facts; absence never proves that no external effect happened.
    external_outcome_unknown = models.BooleanField(default=False)
    cleanup_incomplete = models.BooleanField(default=False)
    soft_time_limit = models.PositiveIntegerField()
    cleanup_timeout = models.PositiveIntegerField()
    display_model = models.CharField(max_length=512, blank=True, default="")
    display_context_limit = models.PositiveIntegerField(null=True, blank=True)
    mcp_unavailable_servers = models.JSONField(default=list)
    trace = models.ForeignKey(
        "AgentExecutionTrace", on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    trace_id: UUID | None

    class Meta:
        """Internal records inherit ownership and deletion from their conversation."""

        default_permissions = ()
        constraints = (
            models.UniqueConstraint(fields=("user", "client_request_id"), name="nbagent_run_request_unique"),
        )
        indexes = (models.Index(fields=("conversation", "submitted_at"), name="nbagent_run_conv_submit"),)

    STALE_HEARTBEAT_AFTER = timedelta(seconds=app_settings.CHAT_RUN_STALE_HEARTBEAT_AFTER)
    QUEUED_WARNING_AFTER = timedelta(seconds=30)

    @property
    def queued_warning_due(self) -> bool:
        """Whether this queued run has reached the client wait-warning threshold."""
        return self.status == AgentRunStatus.QUEUED and self.queue_warning_is_due

    @property
    def is_terminal(self) -> bool:
        """Whether execution has reached a safely terminal state."""
        return self.status not in AgentRunQuerySet.ACTIVE

    @property
    def cancellable(self) -> bool:
        """Whether the client may offer a cancellation request for this run."""
        return not self.is_terminal and not self.cancel_requested


class ExecutionStatus(models.TextChoices):  # pylint: disable=too-many-ancestors
    """Lifecycle statuses shared by execution traces and spans."""

    RUNNING = "running", "Running"
    OK = "ok", "OK"
    ERROR = "error", "Error"
    CANCELLED = "cancelled", "Cancelled"
    INCOMPLETE = "incomplete", "Incomplete"
    REFUSED = "refused", "Refused"


TRACE_EXECUTION_LABELS = {
    status: "Unconfirmed" if status == ExecutionStatus.INCOMPLETE else label
    for status, label in ExecutionStatus.choices
}


class TraceCaptureState(models.TextChoices):  # pylint: disable=too-many-ancestors
    """Diagnostic completeness state, independent of execution outcome."""

    PENDING = "pending", "Pending"
    COMPLETE = "complete", "Complete"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class AgentExecutionTraceQuerySet(RestrictedQuerySet):
    """Trace-list projections shared by the trace and conversation views."""

    def with_turn_number(self):
        """Add the global per-conversation turn number, independent of visibility."""
        return self.annotate(turn_number=_trace_turn_number())

    def with_summary_projections(self, user):
        """Add lightweight trace-list projections for ranking, fixed previews, and visible conversations."""
        queryset = self.with_turn_number().annotate(
            duration_ms=Case(
                When(ended_at__isnull=True, then=Value(None, output_field=DurationField())),
                default=Greatest(F("ended_at") - F("started_at"), Value(timedelta(0))),
                output_field=DurationField(),
            ),
        )
        if AgentExecutionTrace.full_inspection_allowed(user):
            queryset = queryset.annotate(request_preview=Substr("request", 1, 101))
        return queryset.annotate(
            conversation_visible=Exists(
                AgentConversation.objects.readable_by(user).filter(pk=OuterRef("conversation_id"))
            )
        )

    def count_per_conversation(self):
        """Count these traces for an outer conversation row, defaulting to zero."""
        counts = (
            self.filter(conversation_id=OuterRef("pk"))
            .order_by()
            .values("conversation_id")
            .annotate(total=Count("pk"))
            .values("total")
        )
        return Coalesce(Subquery(counts, output_field=IntegerField()), Value(0), output_field=IntegerField())


def _trace_through(started_at, pk):
    """Select traces through one inclusive, stable (start time, primary key) position."""
    return Q(started_at__lt=started_at) | Q(started_at=started_at, pk__lte=pk)


def _trace_turn_number():
    """Build a stable per-conversation rank across all persisted traces."""
    earlier = (
        AgentExecutionTrace.objects.filter(
            conversation_id=OuterRef("conversation_id"),
        )
        .filter(_trace_through(OuterRef("started_at"), OuterRef("pk")))
        .order_by()
        .values("conversation_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return Subquery(earlier, output_field=IntegerField())


class AgentExecutionTrace(BaseModel):
    """One user's attempted chat send and its persisted execution spans."""

    objects = BaseManager.from_queryset(AgentExecutionTraceQuerySet)()

    conversation = models.ForeignKey(
        AgentConversation,
        on_delete=models.CASCADE,
        related_name="execution_traces",
        db_index=False,
    )
    conversation_id: UUID
    turn_number: int
    request = models.TextField()
    status = models.CharField(max_length=16, choices=ExecutionStatus.choices, default=ExecutionStatus.RUNNING)
    capture_state = models.CharField(
        max_length=16,
        choices=TraceCaptureState.choices,
        default=TraceCaptureState.PENDING,
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    # BaseModel defaults metadata association to True; traces are not metadata targets.
    is_metadata_associable_model = False

    class Meta:
        """Model metadata."""

        ordering = ("-started_at", "id")
        indexes = (
            models.Index(fields=("conversation", "-started_at"), name="nbagent_trace_conv_start"),
            models.Index(fields=("-started_at", "id"), name="nbagent_trace_start"),
        )
        verbose_name = "execution trace"
        verbose_name_plural = "execution traces"

    @staticmethod
    def summary_view_allowed(user) -> bool:
        """Return whether a user may view trace summaries, preserving native exempt-view grants."""
        return bool(user and user.has_perm("nautobot_agent.view_agentexecutiontrace"))

    @staticmethod
    def full_inspection_allowed(user) -> bool:
        """Return whether an active user with both trace grants may inspect trace payloads."""
        return bool(
            user
            and user.is_active
            and AgentExecutionTrace.summary_view_allowed(user)
            and user.has_perm("nautobot_agent.view_agentexecutionspan")
        )

    def conversation_position(self, visible_queryset):
        """Return global turn position and permission-restricted adjacent traces."""
        traces = AgentExecutionTrace.objects.filter(conversation_id=self.conversation_id)
        total = traces.count()
        visible = visible_queryset.filter(conversation_id=self.conversation_id)
        previous_key = Q(started_at__lt=self.started_at) | Q(started_at=self.started_at, pk__lt=self.pk)
        next_key = Q(started_at__gt=self.started_at) | Q(started_at=self.started_at, pk__gt=self.pk)
        previous = (
            visible.filter(previous_key)
            .only("pk", "started_at", "conversation_id")
            .order_by("-started_at", "-pk")
            .first()
        )
        next_trace = (
            visible.filter(next_key).only("pk", "started_at", "conversation_id").order_by("started_at", "pk").first()
        )
        return {
            "number": self.turn_number,
            "total": total,
            "previous": previous,
            "next": next_trace,
        }

    def __str__(self) -> str:
        """Return the trace identifier."""
        return f"Trace {self.pk}"

    @property
    def execution_label(self):
        """Distinguish an unconfirmed trace outcome from an unfinished recorded span."""
        return TRACE_EXECUTION_LABELS.get(self.status, self.status)

    @property
    def duration_seconds(self):
        """Derive elapsed seconds, preserving unknown ends and clamping clock skew."""
        if self.ended_at is None:
            return None
        return max(0.0, (cast(datetime, self.ended_at) - cast(datetime, self.started_at)).total_seconds())


class AgentExecutionSpan(models.Model):
    """One callback run captured during an execution trace."""

    objects = models.Manager()

    trace = models.ForeignKey(
        AgentExecutionTrace, on_delete=cascade_without_payload, related_name="spans", db_index=False
    )
    run_id = models.UUIDField()
    parent_run_id = models.UUIDField(null=True, blank=True)
    kind = models.CharField(max_length=32)
    name = models.CharField(max_length=512)
    status = models.CharField(max_length=16, choices=ExecutionStatus.choices)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    input_data = models.JSONField(null=True, blank=True)
    output_data = models.JSONField(null=True, blank=True)
    error_data = models.JSONField(null=True, blank=True)
    details = models.JSONField(default=dict)
    event_counts = models.JSONField(default=dict)

    class Meta:
        """Model metadata."""

        ordering = ("started_at", "id")
        constraints = (models.UniqueConstraint(fields=("trace", "run_id"), name="nbagent_span_trace_run_uniq"),)
        indexes = (models.Index(fields=("trace", "started_at", "id"), name="nbagent_span_trace_start"),)


class AgentCheckpoint(models.Model):
    """Opaque LangGraph checkpoint row; null payload fields denote a write-first placeholder."""

    objects = models.Manager()

    conversation = models.ForeignKey(
        AgentConversation, on_delete=cascade_without_payload, related_name="checkpoints", db_index=False
    )
    checkpoint_ns = models.CharField(max_length=512, blank=True, default="")
    checkpoint_id = models.CharField(max_length=128)
    # NULL distinguishes write-first placeholders; these opaque tags are not optional user-facing strings.
    # pylint: disable=nb-string-field-blank-null
    parent_checkpoint_id = models.CharField(max_length=128, null=True, blank=True)
    checkpoint_type = models.CharField(max_length=32, null=True, blank=True)
    metadata_type = models.CharField(max_length=32, null=True, blank=True)
    # pylint: enable=nb-string-field-blank-null
    checkpoint = models.BinaryField(null=True, blank=True)
    metadata = models.BinaryField(null=True, blank=True)

    class Meta:
        """Model metadata."""

        constraints = (
            models.UniqueConstraint(
                fields=("conversation", "checkpoint_ns", "checkpoint_id"),
                name="nbagent_checkpoint_unique",
            ),
        )


class AgentCheckpointWrite(models.Model):
    """Opaque pending write, unique per checkpoint, task, and effective index."""

    objects = models.Manager()

    checkpoint = models.ForeignKey(
        AgentCheckpoint, on_delete=cascade_without_payload, related_name="writes", db_index=False
    )
    task_id = models.CharField(max_length=128)
    idx = models.IntegerField()
    channel = models.CharField(max_length=255)
    value_type = models.CharField(max_length=32)
    value = models.BinaryField()

    class Meta:
        """Model metadata."""

        constraints = (
            models.UniqueConstraint(
                fields=("checkpoint", "task_id", "idx"),
                name="nbagent_checkpoint_write_unique",
            ),
        )
