"""Coordinated, read-only source policy for the Nautobot Agent graph."""

import logging
from collections.abc import Sequence

from deepagents.backends import BackendProtocol, CompositeBackend, StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission, FsToolName
from django.core.exceptions import ImproperlyConfigured

from nautobot_agent import app_settings
from nautobot_agent.prompting.definitions import filesystem_tool_descriptions, source_prompt
from nautobot_agent.skills import SkillDefinition
from nautobot_agent.sources import backends
from nautobot_agent.sources.backends import APPS_MOUNT, SOURCE_MOUNT, AppSource, SkillsBackend, SourceCatalog

logger = logging.getLogger("nautobot_agent.source_access")

FILESYSTEM_TOOLS: list[FsToolName] = ["ls", "read_file", "glob", "grep"]

SKILLS_SOURCE = "/skills/"
TOOL_RESULTS_SOURCE = "/large_tool_results/"


def resolve_source_apps(user, catalog: SourceCatalog) -> tuple[str, ...]:
    """Resolve the current principal's sorted, eligible App package selection."""
    mapping = app_settings.source_apps_by_group()
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "pk", None) is None
        or getattr(getattr(user, "_state", None), "adding", True)
    ):
        raise ImproperlyConfigured("An authenticated, active, persisted user is required for source access.")
    eligible = catalog.apps
    if user.is_superuser:
        return tuple(sorted(eligible))
    # Filtering creates a fresh related queryset instead of reusing prefetched membership.
    names = user.groups.filter().values_list("name", flat=True)
    granted = {package for name in names for package in mapping.get(name, ())}
    return tuple(sorted(granted.intersection(eligible)))


def _warn_unknown_packages(mapping: dict[str, tuple[str, ...]], eligible: dict[str, AppSource]) -> None:
    unknown = sorted({name for packages in mapping.values() for name in packages}.difference(eligible))
    for name in unknown:
        logger.warning("Configured source package %s is not enabled and registered; skipping it.", name)


def _build_source_permissions(roots: Sequence[str]) -> list[FilesystemPermission]:
    """Return ordered filesystem rules: approved reads first, then a mandatory catch-all denial.

    Unmatched paths are permitted by the SDK, so the final catch-all deny must stay last. Only each exact
    approved root and its descendants are readable; no parent such as ``/apps`` is allowed.
    """
    allowed = [path for root in roots for path in (root.rstrip("/"), root + "**")]
    return [
        FilesystemPermission(operations=["read"], paths=allowed, mode="allow"),
        FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny"),
    ]


def initialize_agent_security() -> None:
    """Validate source configuration and prime source discovery without database or provider work."""
    mapping = app_settings.source_apps_by_group()
    apps = backends.source_catalog.apps
    _warn_unknown_packages(mapping, apps)
    _ = backends.source_catalog.core


class SourceBackend(CompositeBackend):
    """Mount the exact captured source and skill snapshots for one graph."""

    def __init__(self, catalog: SourceCatalog, source_apps: tuple[str, ...], definitions: tuple[SkillDefinition, ...]):
        """Configure core, permitted App, and skill mounts over a state backend."""
        mounts: dict[str, BackendProtocol] = {
            SOURCE_MOUNT: catalog.core,
            SKILLS_SOURCE: SkillsBackend(definitions),
            **catalog.app_mounts(source_apps),
        }
        super().__init__(default=StateBackend(), routes=mounts)


class SourceAccessMiddleware(FilesystemMiddleware):
    """Apply the source backend's read-only tool and prompt policy."""

    @property
    def name(self) -> str:
        """Replace Deep Agents' built-in filesystem middleware rather than append another."""
        return "FilesystemMiddleware"

    def __init__(self, *, backend: SourceBackend):
        """Derive source prompts and permissions from the backend's actual mounts."""
        roots = list(backend.routes)
        readable_roots = [*roots, TOOL_RESULTS_SOURCE]
        super().__init__(
            backend=backend,
            system_prompt=source_prompt(
                roots, SOURCE_MOUNT, SKILLS_SOURCE, apps_mount=APPS_MOUNT, tool_results_root=TOOL_RESULTS_SOURCE
            ),
            custom_tool_descriptions=filesystem_tool_descriptions(readable_roots),
            tools=FILESYSTEM_TOOLS,
            human_message_token_limit_before_evict=None,
            permissions=_build_source_permissions(readable_roots),
        )
