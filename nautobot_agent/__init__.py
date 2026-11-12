"""App declaration for nautobot_agent."""

# Metadata is inherited from Nautobot. If not including Nautobot in the environment, this should be added
from importlib import metadata

from nautobot.apps import NautobotAppConfig

from nautobot_agent import app_settings

__version__ = metadata.version(__name__)


class NautobotAgentConfig(NautobotAppConfig):
    """App configuration for the nautobot_agent app."""

    name = "nautobot_agent"
    verbose_name = "Nautobot Agent"
    version = __version__
    author = "Geury Torres"
    description = "AI agent integration for Nautobot."
    base_url = "nautobot-agent"

    def ready(self):
        """Register the app, validate deployment settings and dependency pins, and prime source discovery."""
        super().ready()
        # Imported lazily so that importing the app package does not load the agent stack.
        from nautobot_agent.sources.source_access import (  # pylint: disable=import-outside-toplevel
            initialize_agent_security,
        )

        initialize_agent_security()

        app_settings.validate_all()


config = NautobotAgentConfig  # pylint:disable=invalid-name
