"""Create the initial Agent schema."""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import nautobot_agent.models


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar[list] = [
        ("contenttypes", "0002_remove_content_type_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list] = [
        migrations.CreateModel(
            name="MCPServer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True
                    ),
                ),
                ("last_updated", models.DateTimeField(auto_now=True)),
                (
                    "name",
                    models.CharField(
                        help_text=(
                            "Non-sensitive display label shown to chat users and sent to the configured model provider; "
                            "it does not affect tool names."
                        ),
                        max_length=255,
                    ),
                ),
                (
                    "url",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="HTTP(S) Streamable HTTP endpoint. Do not include credentials or secrets in the URL or query string.",
                        max_length=2048,
                    ),
                ),
                (
                    "credential_profile",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="For HTTP, an optional deployment-defined header profile; for stdio, an optional deployment-defined environment profile. Secret values are never stored or displayed here.",
                        max_length=255,
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Advertise this trusted server's tools to chat users; remote tools may access or change external systems.",
                    ),
                ),
                (
                    "transport",
                    models.CharField(
                        choices=[("http", "HTTP"), ("stdio", "Standard input/output")], default="http", max_length=8
                    ),
                ),
                ("launch", models.JSONField(blank=True, default=dict)),
            ],
            options={
                "abstract": False,
                "verbose_name": "MCP server",
                "verbose_name_plural": "MCP servers",
                "ordering": ("name",),
                "constraints": [
                    models.UniqueConstraint(fields=("name",), name="nbagent_mcp_name_unique"),
                ],
            },
        ),
        migrations.CreateModel(
            name="AgentConversation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True
                    ),
                ),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("last_activity_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nautobot_agent_conversations",
                        db_index=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
                "ordering": ("-last_activity_at", "id"),
                "verbose_name": "conversation",
                "verbose_name_plural": "conversations",
                "permissions": (("viewall_agentconversation", "View all conversations"),),
            },
        ),
        migrations.CreateModel(
            name="AgentCheckpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("checkpoint_ns", models.CharField(blank=True, default="", max_length=512)),
                ("checkpoint_id", models.CharField(max_length=128)),
                ("parent_checkpoint_id", models.CharField(blank=True, max_length=128, null=True)),
                ("checkpoint_type", models.CharField(blank=True, max_length=32, null=True)),
                ("metadata_type", models.CharField(blank=True, max_length=32, null=True)),
                ("checkpoint", models.BinaryField(blank=True, null=True)),
                ("metadata", models.BinaryField(blank=True, null=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=nautobot_agent.models.cascade_without_payload,
                        related_name="checkpoints",
                        db_index=False,
                        to="nautobot_agent.agentconversation",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AgentExecutionTrace",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True
                    ),
                ),
                ("request", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("ok", "OK"),
                            ("error", "Error"),
                            ("cancelled", "Cancelled"),
                            ("incomplete", "Incomplete"),
                            ("refused", "Refused"),
                        ],
                        default="running",
                        max_length=16,
                    ),
                ),
                (
                    "capture_state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("complete", "Complete"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="execution_traces",
                        db_index=False,
                        to="nautobot_agent.agentconversation",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "verbose_name": "execution trace",
                "verbose_name_plural": "execution traces",
                "ordering": ("-started_at", "id"),
            },
        ),
        migrations.CreateModel(
            name="AgentExecutionSpan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("run_id", models.UUIDField()),
                ("parent_run_id", models.UUIDField(blank=True, null=True)),
                ("kind", models.CharField(max_length=32)),
                ("name", models.CharField(max_length=512)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("ok", "OK"),
                            ("error", "Error"),
                            ("cancelled", "Cancelled"),
                            ("incomplete", "Incomplete"),
                            ("refused", "Refused"),
                        ],
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField()),
                ("input_data", models.JSONField(blank=True, null=True)),
                ("output_data", models.JSONField(blank=True, null=True)),
                ("error_data", models.JSONField(blank=True, null=True)),
                ("details", models.JSONField(default=dict)),
                ("event_counts", models.JSONField(default=dict)),
                (
                    "trace",
                    models.ForeignKey(
                        on_delete=nautobot_agent.models.cascade_without_payload,
                        related_name="spans",
                        db_index=False,
                        to="nautobot_agent.agentexecutiontrace",
                    ),
                ),
            ],
            options={
                "ordering": ("started_at", "id"),
            },
        ),
        migrations.CreateModel(
            name="AgentRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_request_id", models.UUIDField()),
                ("prompt", models.TextField()),
                ("refusal_anchor_id", models.TextField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("refused", "Refused"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                            ("dispatch_failed", "Dispatch failed"),
                            ("interrupted", "Interrupted"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("submitted_at", models.DateTimeField()),
                ("queue_expires_at", models.DateTimeField()),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_requested", models.BooleanField(default=False)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("live_events_incomplete", models.BooleanField(default=False)),
                ("external_outcome_unknown", models.BooleanField(default=False)),
                ("cleanup_incomplete", models.BooleanField(default=False)),
                ("soft_time_limit", models.PositiveIntegerField()),
                ("cleanup_timeout", models.PositiveIntegerField()),
                ("display_model", models.CharField(blank=True, default="", max_length=512)),
                ("display_context_limit", models.PositiveIntegerField(blank=True, null=True)),
                ("mcp_unavailable_servers", models.JSONField(default=list)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        db_index=False,
                        to="nautobot_agent.agentconversation",
                    ),
                ),
                (
                    "trace",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="runs",
                        to="nautobot_agent.agentexecutiontrace",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="nautobot_agent_runs",
                        db_index=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="LLMConfiguration",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True
                    ),
                ),
                ("last_updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                (
                    "model",
                    models.CharField(help_text="Chat model as provider:model, e.g. openai:gpt-5.4.", max_length=512),
                ),
                (
                    "credential_profile",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Name of a credential profile defined in the deployment's PLUGINS_CONFIG; blank for none.",
                        max_length=255,
                    ),
                ),
                (
                    "parameters",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Optional constructor keyword arguments as a JSON object. Do not store credentials here.",
                    ),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        blank=True,
                        default=None,
                        help_text="Use this configuration as the chat model for the Nautobot Agent.",
                        null=True,
                        verbose_name="Default Agent model",
                    ),
                ),
                (
                    "is_scope_classifier",
                    models.BooleanField(
                        blank=True,
                        default=None,
                        help_text="Use this configuration for the Nautobot scope classifier. If none is selected, the default Agent configuration is used.",
                        null=True,
                        verbose_name="Scope classifier",
                    ),
                ),
            ],
            options={
                "verbose_name": "LLM configuration",
                "verbose_name_plural": "LLM configurations",
                "ordering": ("name",),
                "constraints": [
                    models.UniqueConstraint(fields=("name",), name="nbagent_llm_name_unique"),
                    models.UniqueConstraint(fields=("is_default",), name="nbagent_llm_default_unique"),
                    models.UniqueConstraint(fields=("is_scope_classifier",), name="nbagent_llm_scope_unique"),
                    models.CheckConstraint(
                        condition=models.Q(("is_default", True), ("is_default__isnull", True), _connector="OR"),
                        name="nbagent_llm_default_true",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("is_scope_classifier", True), ("is_scope_classifier__isnull", True), _connector="OR"
                        ),
                        name="nbagent_llm_scope_true",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Skill",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False, unique=True
                    ),
                ),
                ("last_updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("identifier", models.SlugField(blank=True, default="", editable=False, max_length=64, db_index=False)),
                ("description", models.CharField(max_length=1024)),
                ("instructions", models.TextField()),
                ("enabled", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ("name", "pk"),
                "verbose_name": "Skill",
                "verbose_name_plural": "Skills",
                "constraints": [
                    models.UniqueConstraint(fields=("identifier",), name="nbagent_skill_identifier_unique")
                ],
            },
        ),
        migrations.CreateModel(
            name="AgentCheckpointWrite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("task_id", models.CharField(max_length=128)),
                ("idx", models.IntegerField()),
                ("channel", models.CharField(max_length=255)),
                ("value_type", models.CharField(max_length=32)),
                ("value", models.BinaryField()),
                (
                    "checkpoint",
                    models.ForeignKey(
                        on_delete=nautobot_agent.models.cascade_without_payload,
                        related_name="writes",
                        db_index=False,
                        to="nautobot_agent.agentcheckpoint",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("checkpoint", "task_id", "idx"), name="nbagent_checkpoint_write_unique"
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="agentcheckpoint",
            constraint=models.UniqueConstraint(
                fields=("conversation", "checkpoint_ns", "checkpoint_id"), name="nbagent_checkpoint_unique"
            ),
        ),
        migrations.AddIndex(
            model_name="agentexecutiontrace",
            index=models.Index(fields=["conversation", "-started_at"], name="nbagent_trace_conv_start"),
        ),
        migrations.AddIndex(
            model_name="agentexecutionspan",
            index=models.Index(fields=["trace", "started_at", "id"], name="nbagent_span_trace_start"),
        ),
        migrations.AddConstraint(
            model_name="agentexecutionspan",
            constraint=models.UniqueConstraint(fields=("trace", "run_id"), name="nbagent_span_trace_run_uniq"),
        ),
        migrations.AddIndex(
            model_name="agentrun",
            index=models.Index(fields=["conversation", "submitted_at"], name="nbagent_run_conv_submit"),
        ),
        migrations.AddConstraint(
            model_name="agentrun",
            constraint=models.UniqueConstraint(fields=("user", "client_request_id"), name="nbagent_run_request_unique"),
        ),
        migrations.AddIndex(
            model_name="agentconversation",
            index=models.Index(fields=["user", "-last_activity_at", "id"], name="nbagent_conv_owner_activity"),
        ),
        migrations.AddIndex(
            model_name="agentconversation",
            index=models.Index(fields=("-last_activity_at", "id"), name="nbagent_conv_activity"),
        ),
        migrations.AddIndex(
            model_name="agentexecutiontrace",
            index=models.Index(fields=("-started_at", "id"), name="nbagent_trace_start"),
        ),
    ]
