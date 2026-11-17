"""Deployment validation, including profile catalogs whose values are read at use time and never logged."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from keyword import iskeyword
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULTS: dict[str, Any] = {
    "blocked_models": [],
    "builtin_skills": True,
    "credentials": {},
    "mcp_environments": {},
    "mcp_credentials": {},
    "source_apps_by_group": {},
    "scope_mode": "conversation",
    "tracing": True,
    "interpreter": {
        "max_duration_secs": 5.0,
        "max_memory_bytes": 64 * 1024 * 1024,
        "max_host_calls": 20,
        "max_output_chars": 16000,
    },
    "query": {"max_page_bytes": 1_048_576},
    "query_statement_timeout_ms": 30_000,
    "chat_return_timeout": 600,
    "chat_max_model_calls": 40,
    "chat_max_tool_calls": 100,
    "chat_celery_queue": None,
    "chat_soft_time_limit": 300,
    "chat_hard_time_limit": 600,
    "chat_cleanup_timeout": 20,
    "chat_queue_wait_timeout": 600,
}

_BLOCKED_MODELS_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['blocked_models'] must be a list of installed concrete model labels."
)
CHAT_RUN_HEARTBEAT_INTERVAL = 5
CHAT_RUN_HEARTBEAT_FAILURE_AFTER = 4 * CHAT_RUN_HEARTBEAT_INTERVAL
CHAT_RUN_SHUTDOWN_MARGIN = 2 * CHAT_RUN_HEARTBEAT_INTERVAL
# Leave one shutdown margin between worker self-fencing and observer interruption.
CHAT_RUN_STALE_HEARTBEAT_AFTER = CHAT_RUN_HEARTBEAT_FAILURE_AFTER + CHAT_RUN_SHUTDOWN_MARGIN
_CHAT_RUN_SETTING_KEYS = (
    "chat_celery_queue",
    "chat_soft_time_limit",
    "chat_hard_time_limit",
    "chat_cleanup_timeout",
    "chat_queue_wait_timeout",
)


def _positive_setting(key: str) -> int:
    """Read a positive integer setting with its value-free diagnostic."""
    diagnostic = f"PLUGINS_CONFIG['nautobot_agent']['{key}'] must be a positive integer."
    return _positive_int(_app_config(diagnostic).get(key, DEFAULTS[key]), diagnostic)


def chat_max_model_calls() -> int:
    """Return the configured per-run logical model-call ceiling."""
    return _positive_setting("chat_max_model_calls")


def query_statement_timeout_ms() -> int:
    """Return the database-side time limit for each live-data SQL statement."""
    timeout = _positive_setting("query_statement_timeout_ms")
    if timeout > 2_147_483_647:
        raise ImproperlyConfigured(
            "PLUGINS_CONFIG['nautobot_agent']['query_statement_timeout_ms'] must not exceed 2147483647."
        )
    return timeout


def chat_max_tool_calls() -> int:
    """Return the configured per-run graph tool-call ceiling."""
    return _positive_setting("chat_max_tool_calls")


def chat_return_timeout() -> int:
    """Return the configured delay before an ordinary departure expires selection."""
    return _positive_setting("chat_return_timeout")


@dataclass(frozen=True)
class InterpreterLimits:
    """Validated limits for one stateless Monty execution.

    ``max_duration_secs`` bounds interpreter execution across the submitted feed,
    including its resumptions. Time spent waiting on host calls is excluded, so it is
    neither a total wall-time deadline nor a CPU-clock limit; SQL statement timeouts and
    run limits still apply. ``max_memory_bytes`` bounds interpreter memory,
    ``max_host_calls`` the host-tool calls per execution, and ``max_output_chars`` the
    returned result size.
    """

    max_duration_secs: float
    max_memory_bytes: int
    max_host_calls: int
    max_output_chars: int


_INTERPRETER_DEFAULTS = DEFAULTS["interpreter"]
_INTERPRETER_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['interpreter'] must be a dict containing only "
    "positive max_duration_secs, max_memory_bytes, max_host_calls, and max_output_chars "
    "(max_output_chars must be at least 1000)."
)


def _app_config(diagnostic: str) -> dict:
    """Return the deployment app settings, raising only a safe reader diagnostic."""
    plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    if not isinstance(plugins_config, dict):
        raise ImproperlyConfigured(diagnostic)
    app_config = plugins_config.get("nautobot_agent", {})
    if not isinstance(app_config, dict):
        raise ImproperlyConfigured(diagnostic)
    return app_config


def _positive_int(value: Any, diagnostic: str, minimum: int = 1) -> int:
    """Validate a non-boolean positive integer under the caller's static diagnostic."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ImproperlyConfigured(diagnostic)
    return value


def interpreter_limits() -> InterpreterLimits:
    """Return merged, validated interpreter limits without exposing configured values."""
    app_config = _app_config(_INTERPRETER_DIAGNOSTIC)
    configured = app_config.get("interpreter", {})
    if not isinstance(configured, dict) or set(configured).difference(_INTERPRETER_DEFAULTS):
        raise ImproperlyConfigured(_INTERPRETER_DIAGNOSTIC)
    values = _INTERPRETER_DEFAULTS | configured
    duration = values["max_duration_secs"]
    try:
        valid_duration = (
            not isinstance(duration, bool)
            and isinstance(duration, int | float)
            and duration > 0
            and math.isfinite(duration)
        )
    except (OverflowError, TypeError, ValueError):
        valid_duration = False
    if not valid_duration:
        raise ImproperlyConfigured(_INTERPRETER_DIAGNOSTIC)
    integer_limits: dict[str, int] = {}
    for key in ("max_memory_bytes", "max_host_calls", "max_output_chars"):
        integer_limits[key] = _positive_int(values[key], _INTERPRETER_DIAGNOSTIC)
    if integer_limits["max_output_chars"] < 1000:
        raise ImproperlyConfigured(_INTERPRETER_DIAGNOSTIC)
    return InterpreterLimits(
        max_duration_secs=float(duration),
        max_memory_bytes=integer_limits["max_memory_bytes"],
        max_host_calls=integer_limits["max_host_calls"],
        max_output_chars=integer_limits["max_output_chars"],
    )


class ScopeMode(StrEnum):
    """Deployment-owned policy for the pre-send scope gate."""

    STRICT = "strict"
    CONVERSATION = "conversation"
    OFF = "off"


_SCOPE_MODE_DIAGNOSTIC = "PLUGINS_CONFIG['nautobot_agent']['scope_mode'] must be one of: strict, conversation, off."


def scope_mode() -> ScopeMode:
    """Return the validated, uncached deployment scope policy."""
    app_config = _app_config(_SCOPE_MODE_DIAGNOSTIC)
    value = app_config.get("scope_mode", DEFAULTS["scope_mode"])
    if not isinstance(value, str):
        raise ImproperlyConfigured(_SCOPE_MODE_DIAGNOSTIC)
    try:
        return ScopeMode(value)
    except ValueError:
        raise ImproperlyConfigured(_SCOPE_MODE_DIAGNOSTIC) from None


_BUILTIN_SKILLS_DIAGNOSTIC = "PLUGINS_CONFIG['nautobot_agent']['builtin_skills'] must be true or false."


def builtin_skills_enabled() -> bool:
    """Return whether packaged skills are included in runtime snapshots."""
    app_config = _app_config(_BUILTIN_SKILLS_DIAGNOSTIC)
    value = app_config.get("builtin_skills", DEFAULTS["builtin_skills"])
    if not isinstance(value, bool):
        raise ImproperlyConfigured(_BUILTIN_SKILLS_DIAGNOSTIC)
    return value


_TRACING_DIAGNOSTIC = "PLUGINS_CONFIG['nautobot_agent']['tracing'] must be true or false."


def tracing_enabled() -> bool:
    """Return whether execution tracing is enabled, requiring an explicit boolean value."""
    app_config = _app_config(_TRACING_DIAGNOSTIC)
    value = app_config.get("tracing", DEFAULTS["tracing"])
    if not isinstance(value, bool):
        raise ImproperlyConfigured(_TRACING_DIAGNOSTIC)
    return value


_CREDENTIAL_PROFILES_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['credentials'] must map profile names to dicts of "
    "chat-model keyword arguments with nonblank string keys."
)
_MCP_CREDENTIAL_PROFILES_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['mcp_credentials'] must map profile names to dicts of "
    "valid HTTP header names and single-line string values."
)
_MCP_ENVIRONMENT_PROFILES_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['mcp_environments'] must map profile names to dicts of "
    "environment names without '=' or NUL and values without NUL."
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _profile_catalog(
    setting_name: str,
    diagnostic: str,
    valid_entry: Callable[[str, Any], bool],
) -> dict[str, dict[str, Any]]:
    """Validate one profile catalog and return fresh dictionaries without exposing configured values."""
    configured = _app_config(diagnostic).get(setting_name, DEFAULTS[setting_name])
    if not isinstance(configured, dict):
        raise ImproperlyConfigured(diagnostic)
    profiles = {}
    for name, profile in configured.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(profile, dict):
            raise ImproperlyConfigured(diagnostic)
        copied = {}
        for key, value in profile.items():
            if not isinstance(key, str) or not valid_entry(key, value):
                raise ImproperlyConfigured(diagnostic)
            copied[key] = value
        profiles[name] = copied
    return profiles


def credential_profiles() -> dict[str, dict[str, Any]]:
    """Return fresh validated LLM credential profiles; provider values are intentionally unconstrained."""
    return _profile_catalog("credentials", _CREDENTIAL_PROFILES_DIAGNOSTIC, lambda key, _value: bool(key.strip()))


def mcp_credential_profiles() -> dict[str, dict[str, str | None]]:
    """Return fresh validated HTTP-header profiles without exposing their values to forms."""

    def valid_header(key, value):
        return _HEADER_NAME.fullmatch(key) is not None and (
            value is None or (isinstance(value, str) and "\r" not in value and "\n" not in value)
        )

    return _profile_catalog("mcp_credentials", _MCP_CREDENTIAL_PROFILES_DIAGNOSTIC, valid_header)


def valid_mcp_environment_entry(key, value) -> bool:
    """Validate an environment entry; catalogs allow null placeholders, but selected profiles must be fully set."""
    return (
        isinstance(key, str)
        and bool(key)
        and "=" not in key
        and "\x00" not in key
        and (value is None or (isinstance(value, str) and "\x00" not in value))
    )


def mcp_environment_profiles() -> dict[str, dict[str, str | None]]:
    """Return fresh validated stdio environment profiles without exposing their values to forms."""
    return _profile_catalog("mcp_environments", _MCP_ENVIRONMENT_PROFILES_DIAGNOSTIC, valid_mcp_environment_entry)


_SOURCE_APPS_BY_GROUP_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['source_apps_by_group'] must map nonblank group names to package-name lists."
)


def source_apps_by_group() -> dict[str, tuple[str, ...]]:
    """Validate and normalize the deployment-owned group-to-package mapping."""
    app_config = _app_config(_SOURCE_APPS_BY_GROUP_DIAGNOSTIC)
    mapping = app_config.get("source_apps_by_group", DEFAULTS["source_apps_by_group"])
    if not isinstance(mapping, dict):
        raise ImproperlyConfigured(_SOURCE_APPS_BY_GROUP_DIAGNOSTIC)
    normalized = {}
    for group_name, packages in mapping.items():
        if not isinstance(group_name, str) or not group_name.strip() or not isinstance(packages, list):
            raise ImproperlyConfigured(_SOURCE_APPS_BY_GROUP_DIAGNOSTIC)
        if any(
            not isinstance(package, str)
            or not package.strip()
            or any(not part.isidentifier() or iskeyword(part) for part in package.split("."))
            for package in packages
        ):
            raise ImproperlyConfigured(_SOURCE_APPS_BY_GROUP_DIAGNOSTIC)
        normalized[group_name] = tuple(dict.fromkeys(packages))
    return normalized


def blocked_models() -> tuple[str, ...]:
    """Return canonical, deduplicated blocked model labels without querying the database."""
    app_config = _app_config(_BLOCKED_MODELS_DIAGNOSTIC)
    configured = app_config.get("blocked_models", DEFAULTS["blocked_models"])
    if not isinstance(configured, list):
        raise ImproperlyConfigured(_BLOCKED_MODELS_DIAGNOSTIC)

    if not configured:
        return ()
    registry = {config.label.lower(): config for config in apps.get_app_configs()}
    return tuple(dict.fromkeys(_blocked_model_label(label, registry) for label in configured))


def _blocked_model_label(label: Any, registry: dict[str, Any]) -> str:
    """Resolve one configured label to its installed, concrete, non-swapped model label."""
    if not isinstance(label, str) or label.count(".") != 1:
        raise ImproperlyConfigured(_BLOCKED_MODELS_DIAGNOSTIC)
    app_label, model_name = label.lower().split(".")
    config = registry.get(app_label)
    try:
        model = config.get_model(model_name) if config is not None else None
    except LookupError:
        model = None
    if model is None:
        raise ImproperlyConfigured(_BLOCKED_MODELS_DIAGNOSTIC)
    if model._meta.swapped:
        raise ImproperlyConfigured(
            f"PLUGINS_CONFIG['nautobot_agent']['blocked_models'] names a swapped model; configure its replacement "
            f"'{model._meta.swapped}' instead."
        )
    if model._meta.proxy:
        raise ImproperlyConfigured(
            "PLUGINS_CONFIG['nautobot_agent']['blocked_models'] cannot name a proxy model; configure its concrete "
            f"model '{model._meta.concrete_model._meta.label}' instead."
        )
    return model._meta.label


@dataclass(frozen=True)
class ChatRunSettings:
    """Validated deployment policy and task budgets for durable chat runs."""

    celery_queue: str | None
    soft_time_limit: int
    hard_time_limit: int
    cleanup_timeout: int
    queue_wait_timeout: int


_CHAT_RUN_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent'] chat run settings require a null or nonblank "
    "chat_celery_queue, integer time budgets and queue wait timeout between 1 and 2147483647, and soft plus "
    f"cleanup plus the fixed {CHAT_RUN_SHUTDOWN_MARGIN}-second shutdown margin less than hard."
)


def chat_run_settings() -> ChatRunSettings:
    """Validate chat run queue and task budgets without database or worker access."""
    app_config = _app_config(_CHAT_RUN_DIAGNOSTIC)
    values = {key: app_config.get(key, DEFAULTS[key]) for key in _CHAT_RUN_SETTING_KEYS}
    queue = values["chat_celery_queue"]
    if queue is not None and (not isinstance(queue, str) or not queue.strip()):
        raise ImproperlyConfigured(_CHAT_RUN_DIAGNOSTIC)
    integer_values: dict[str, int] = {}
    for key in _CHAT_RUN_SETTING_KEYS:
        if key == "chat_celery_queue":
            continue
        integer_values[key] = _positive_int(values[key], _CHAT_RUN_DIAGNOSTIC)
        if integer_values[key] > 2_147_483_647:
            raise ImproperlyConfigured(_CHAT_RUN_DIAGNOSTIC)
    if (
        integer_values["chat_soft_time_limit"] + integer_values["chat_cleanup_timeout"] + CHAT_RUN_SHUTDOWN_MARGIN
        >= integer_values["chat_hard_time_limit"]
    ):
        raise ImproperlyConfigured(_CHAT_RUN_DIAGNOSTIC)
    return ChatRunSettings(
        celery_queue=queue.strip() if queue is not None else None,
        soft_time_limit=integer_values["chat_soft_time_limit"],
        hard_time_limit=integer_values["chat_hard_time_limit"],
        cleanup_timeout=integer_values["chat_cleanup_timeout"],
        queue_wait_timeout=integer_values["chat_queue_wait_timeout"],
    )


_QUERY_LIMIT_DIAGNOSTIC = (
    "PLUGINS_CONFIG['nautobot_agent']['query'] must be a dict containing only "
    "max_page_bytes as an integer of at least 1024."
)


def query_limits() -> int:
    """Return the validated query-response byte ceiling without exposing configured values."""
    app_config = _app_config(_QUERY_LIMIT_DIAGNOSTIC)
    configured = app_config.get("query", {})
    if not isinstance(configured, dict) or set(configured).difference({"max_page_bytes"}):
        raise ImproperlyConfigured(_QUERY_LIMIT_DIAGNOSTIC)
    return _positive_int(
        configured.get("max_page_bytes", DEFAULTS["query"]["max_page_bytes"]), _QUERY_LIMIT_DIAGNOSTIC, 1024
    )


def validate_all() -> None:
    """Run every deployment setting reader; keep each reader's diagnostics explicit."""
    configured = _app_config("PLUGINS_CONFIG['nautobot_agent'] must be a dict.")
    unknown = set(configured).difference(DEFAULTS)
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ImproperlyConfigured(f"PLUGINS_CONFIG['nautobot_agent'] contains unknown settings: {names}.")
    interpreter_limits()
    scope_mode()
    builtin_skills_enabled()
    tracing_enabled()
    credential_profiles()
    mcp_credential_profiles()
    mcp_environment_profiles()
    blocked_models()
    source_apps_by_group()
    chat_run_settings()
    query_limits()
    query_statement_timeout_ms()
    chat_return_timeout()
    chat_max_model_calls()
    chat_max_tool_calls()
