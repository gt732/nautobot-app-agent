"""Allowlisted runtime Nautobot and enabled-App metadata."""

import nautobot
from langchain_core.tools import StructuredTool

from nautobot_agent.data.tool_io import configure_tool_errors, dumps
from nautobot_agent.sources.backends import enabled_app_configs


def _get_nautobot_environment() -> str:
    """Report this worker's Nautobot version and enabled Nautobot Apps with declared runtime versions.

    This allowlisted inventory excludes sensitive settings and does not establish consistency
    across workers or synchronization with checkout metadata.
    """
    configs = sorted(enabled_app_configs(), key=lambda config: config.name)
    enabled_apps = []
    for config in configs:
        declared_version = getattr(config, "version", None)
        version = str(declared_version) if declared_version is not None else ""
        enabled_apps.append(
            {
                "name": config.name,
                "display_name": str(config.verbose_name),
                "version": version if version.strip() else None,
            }
        )
    return dumps({"nautobot_version": nautobot.__version__, "enabled_apps": enabled_apps})


get_nautobot_environment = StructuredTool.from_function(_get_nautobot_environment, name="get_nautobot_environment")
configure_tool_errors(get_nautobot_environment)
