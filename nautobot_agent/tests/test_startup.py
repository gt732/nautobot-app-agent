"""Database-free source initialization regressions."""

from unittest.mock import patch

from nautobot_agent.sources import backends
from nautobot_agent.sources.backends import SourceCatalog
from nautobot_agent.sources.source_access import initialize_agent_security
from nautobot_agent.tests.support.sources import (
    TemporarySourceTestCase,
    loaded_package_module,
)


class StartupSourceTests(TemporarySourceTestCase):
    """Startup primes source discovery without database, model-provider, or graph construction."""

    def test_startup_is_model_and_database_free(self):
        with (
            patch.object(backends, "nautobot", loaded_package_module("nautobot", self.tree.package)),
            patch.object(backends, "enabled_app_configs", return_value=[]),
            patch.object(backends, "source_catalog", SourceCatalog()),
            patch("nautobot_agent.runtime.llm.init_chat_model") as build,
        ):
            initialize_agent_security()
            backend = backends.source_catalog.core

        build.assert_not_called()
        self.assertIsInstance(backend, backends.NautobotSourceBackend)
        result = backend.read("/__init__.py")
        assert result.file_data is not None
        self.assertIn("9.9.9-test", result.file_data["content"])

    def test_unavailable_source_at_startup_is_logged_without_a_path_and_is_not_fatal(self):
        with (
            patch.object(backends, "nautobot", loaded_package_module("nautobot", self.tree.package, initializer=False)),
            patch.object(backends, "enabled_app_configs", return_value=[]),
            patch.object(backends, "source_catalog", SourceCatalog()),
            self.assertLogs("nautobot_agent", level="WARNING") as logs,
        ):
            initialize_agent_security()
            backend = backends.source_catalog.core

        self.assertIsInstance(backend, backends.UnavailableSourceBackend)
        output = "\n".join(logs.output)
        self.assertNotIn(self.location_token, output)
