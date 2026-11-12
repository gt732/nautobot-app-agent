"""Consumer-visible tests for worker-local Nautobot environment metadata."""

import json
from types import ModuleType
from unittest.mock import PropertyMock, patch

from django.apps import AppConfig
from django.test import SimpleTestCase
from nautobot.apps import NautobotAppConfig

from nautobot_agent.sources.backends import SourceCatalog
from nautobot_agent.tools.get_nautobot_environment import get_nautobot_environment


def _config(config_class, name, *, display_name=None, version=None):
    module = ModuleType(name)
    module.__file__ = f"/unavailable/{name}/__init__.py"
    config = config_class(name, module)
    if display_name is not None:
        config.verbose_name = display_name
    if version is not None:
        config.version = version
    return config


def _call_environment(arguments=None):
    return get_nautobot_environment.invoke(
        {
            "type": "tool_call",
            "name": "get_nautobot_environment",
            "id": "environment",
            "args": arguments or {},
        }
    )


class EnvironmentToolTests(SimpleTestCase):
    """Expose only selected runtime metadata, without database or source access."""

    @patch("nautobot_agent.tools.get_nautobot_environment.nautobot.__version__", "3.2.runtime")
    @patch("nautobot_agent.sources.backends.apps.get_app_configs")
    def test_environment_lists_enabled_apps(self, get_configs):
        blank = _config(NautobotAppConfig, "z_package", display_name="Zed", version=" \t\n")
        versioned = _config(NautobotAppConfig, "a_package", display_name="Alpha", version="1.2.3")
        empty = _config(NautobotAppConfig, "e_package", display_name="Empty", version="")
        # This selected registered App has no usable source-module metadata; inventory does not depend on it.
        absent = _config(NautobotAppConfig, "m_package", display_name="Middle")
        absent.version = None
        disabled = _config(NautobotAppConfig, "disabled", display_name="Disabled", version="9")
        non_nautobot = _config(AppConfig, "ordinary", display_name="Ordinary", version="8")
        get_configs.return_value = [blank, versioned, empty, absent, disabled, non_nautobot]
        plugins = ["z_package", "a_package", "e_package", "m_package", "ordinary", "unregistered"]

        cases = (
            (
                plugins,
                [
                    {"name": "a_package", "display_name": "Alpha", "version": "1.2.3"},
                    {"name": "e_package", "display_name": "Empty", "version": None},
                    {"name": "m_package", "display_name": "Middle", "version": None},
                    {"name": "z_package", "display_name": "Zed", "version": None},
                ],
            ),
            (["configured_but_unregistered"], []),
        )
        for selected, expected in cases:
            with self.subTest(plugins=selected), self.settings(PLUGINS=selected):
                with patch.object(SourceCatalog, "apps", new_callable=PropertyMock, side_effect=AssertionError):
                    message = _call_environment()
                self.assertEqual(message.status, "success", message.content)
                self.assertEqual(
                    json.loads(message.content), {"nautobot_version": "3.2.runtime", "enabled_apps": expected}
                )
