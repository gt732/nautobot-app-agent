"""Focused database-free validation of deployment settings."""

from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from nautobot.apps import NautobotAppConfig

from nautobot_agent import app_settings


class AppSettingsTests(SimpleTestCase):
    """Settings readers reject malformed deployment configuration safely."""

    def test_query_statement_timeout_requires_a_positive_non_boolean_integer(self):
        for value in (None, True, False, 0, -1, 1.5, "SECRET", 2_147_483_648):
            with (
                self.subTest(value=value),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"query_statement_timeout_ms": value}}),
                self.assertRaises(ImproperlyConfigured),
            ):
                app_settings.validate_all()
        for value in (250, 2_147_483_647):
            with (
                self.subTest(value=value),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"query_statement_timeout_ms": value}}),
            ):
                self.assertEqual(app_settings.query_statement_timeout_ms(), value)

    def test_chat_run_call_ceilings(self):
        for setting, reader, default in (
            ("chat_max_model_calls", app_settings.chat_max_model_calls, 40),
            ("chat_max_tool_calls", app_settings.chat_max_tool_calls, 100),
        ):
            with self.subTest(setting=setting):
                self.assertEqual(app_settings.DEFAULTS[setting], default)
                with self.settings(PLUGINS_CONFIG={}):
                    self.assertEqual(reader(), default)
                for value in (1, default):
                    with (
                        self.subTest(value=value),
                        self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting: value}}),
                    ):
                        self.assertEqual(reader(), value)
                for value in (True, False, 0, -1, 1.5, str(default), None, []):
                    with (
                        self.subTest(value=value),
                        self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting: value}}),
                        self.assertRaises(ImproperlyConfigured),
                    ):
                        reader()

        for setting in ("chat_max_model_calls", "chat_max_tool_calls"):
            with (
                self.subTest(setting=setting, validation="all"),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting: False}}),
                self.assertRaises(ImproperlyConfigured),
            ):
                app_settings.validate_all()

    def test_interpreter_limits(self):
        configured = {"max_duration_secs": 2.5, "max_host_calls": 7}
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"interpreter": configured}}):
            self.assertEqual(
                app_settings.interpreter_limits(),
                app_settings.InterpreterLimits(2.5, 64 * 1024 * 1024, 7, 16000),
            )

        malformed = (
            None,
            [],
            {"unknown": "INTERPRETER-SECRET"},
            {"max_duration_secs": True},
            {"max_duration_secs": 0},
            {"max_duration_secs": float("inf")},
            {"max_duration_secs": float("nan")},
            {"max_duration_secs": 10**1000},
            {"max_memory_bytes": 1.5},
            {"max_host_calls": True},
            {"max_host_calls": "INTERPRETER-SECRET"},
            {"max_output_chars": 999},
        )
        for value in malformed:
            with (
                self.subTest(value=repr(value)[:80]),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"interpreter": value}}),
                self.assertRaises(ImproperlyConfigured) as caught,
            ):
                app_settings.interpreter_limits()
            self.assertIn("interpreter", str(caught.exception))
            self.assertNotIn("INTERPRETER-SECRET", str(caught.exception))

    def test_profile_catalogs(self):
        readers = (
            (app_settings.credential_profiles, "credentials", {"alpha": {"key": object()}}),
            (app_settings.mcp_credential_profiles, "mcp_credentials", {"alpha": {"X-Token": None}}),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"alpha": {"TOKEN": None}}),
        )
        for reader, setting_name, catalog in readers:
            with self.subTest(reader=reader.__name__), self.settings(PLUGINS_CONFIG={"nautobot_agent": {}}):
                self.assertEqual(reader(), {})
            with (
                self.subTest(reader=reader.__name__, catalog=catalog),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting_name: catalog}}),
            ):
                first = reader()
                second = reader()
                self.assertEqual(first, catalog)
                self.assertIsNot(first, second)
                self.assertIsNot(first["alpha"], second["alpha"])
                first["alpha"]["added"] = "local"
                self.assertNotIn("added", reader()["alpha"])

        malformed = (
            (app_settings.credential_profiles, "credentials", None),
            (app_settings.credential_profiles, "credentials", []),
            (app_settings.credential_profiles, "credentials", {"PROFILE-SECRET": []}),
            (app_settings.credential_profiles, "credentials", {"": {"key": "value"}}),
            (app_settings.credential_profiles, "credentials", {"profile": {" ": "value"}}),
            (app_settings.mcp_credential_profiles, "mcp_credentials", None),
            (app_settings.mcp_credential_profiles, "mcp_credentials", {"profile": []}),
            (app_settings.mcp_credential_profiles, "mcp_credentials", "HEADER-SECRET"),
            (app_settings.mcp_credential_profiles, "mcp_credentials", {"profile": {"Bad Header": "value"}}),
            (app_settings.mcp_credential_profiles, "mcp_credentials", {"profile": {"X-Token": "a\nb"}}),
            (app_settings.mcp_environment_profiles, "mcp_environments", None),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"profile": []}),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"profile": {"BAD=KEY": "value"}}),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"profile": {"BAD\x00KEY": "value"}}),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"profile": {"TOKEN": "BAD\x00VALUE"}}),
            (app_settings.mcp_environment_profiles, "mcp_environments", {"profile": {"": "value"}}),
        )
        for reader, setting_name, catalog in malformed:
            with self.subTest(reader=reader.__name__, setting_name=setting_name, catalog=repr(catalog)[:80]):
                with (
                    self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting_name: catalog}}),
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    reader()
                self.assertIn(setting_name, str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertNotIn(repr(catalog), str(caught.exception))

    def test_scalar_settings(self):
        accepted = (
            *(
                (app_settings.scope_mode, {"nautobot_agent": {"scope_mode": value}}, expected)
                for value, expected in (
                    ("strict", app_settings.ScopeMode.STRICT),
                    ("conversation", app_settings.ScopeMode.CONVERSATION),
                    ("off", app_settings.ScopeMode.OFF),
                )
            ),
            (app_settings.tracing_enabled, {}, True),
            (app_settings.tracing_enabled, {"nautobot_agent": {}}, True),
            (app_settings.tracing_enabled, {"nautobot_agent": {"tracing": True}}, True),
            (app_settings.tracing_enabled, {"nautobot_agent": {"tracing": False}}, False),
            *(
                (app_settings.builtin_skills_enabled, {"nautobot_agent": {"builtin_skills": value}}, value)
                for value in (True, False)
            ),
        )
        for reader, plugins_config, expected in accepted:
            with (
                self.subTest(reader=reader.__name__, plugins_config=plugins_config),
                self.settings(PLUGINS_CONFIG=plugins_config),
            ):
                self.assertEqual(reader(), expected)

        malformed_parents = (
            None,
            [],
            "nautobot_agent",
            {"nautobot_agent": None},
            {"nautobot_agent": []},
            {"nautobot_agent": "scope_mode"},
        )
        rejected = (
            *((app_settings.scope_mode, parent) for parent in malformed_parents),
            *(
                (app_settings.scope_mode, {"nautobot_agent": {"scope_mode": value}})
                for value in (None, True, 1, {}, [], "", "STRICT", "conversation ", "unknown", "SCOPE-SECRET-SENTINEL")
            ),
            *((app_settings.tracing_enabled, parent) for parent in malformed_parents),
            *(
                (app_settings.tracing_enabled, {"nautobot_agent": {"tracing": value}})
                for value in (None, 0, 1, 1.5, "SECRET", [], {})
            ),
            *(
                (app_settings.builtin_skills_enabled, {"nautobot_agent": {"builtin_skills": value}})
                for value in (None, 0, 1, 1.5, "SECRET", [], {})
            ),
            (app_settings.chat_run_settings, {"nautobot_agent": {"chat_soft_time_limit": True}}),
        )
        for reader, plugins_config in rejected:
            with self.subTest(reader=reader.__name__, plugins_config=plugins_config):
                with self.settings(PLUGINS_CONFIG=plugins_config), self.assertRaises(ImproperlyConfigured) as caught:
                    reader()
                self.assertNotIn("SECRET", str(caught.exception))

    def test_chat_return_timeout_requires_a_positive_integer(self):
        self.assertEqual(app_settings.DEFAULTS["chat_return_timeout"], 600)
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {}}):
            self.assertEqual(app_settings.chat_return_timeout(), 600)
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"chat_return_timeout": 45}}):
            self.assertEqual(app_settings.chat_return_timeout(), 45)

        for value in (None, True, False, 0, -1, "600", 1.5):
            with (
                self.subTest(value=value),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"chat_return_timeout": value}}),
                self.assertRaises(ImproperlyConfigured) as caught,
            ):
                app_settings.chat_return_timeout()
            self.assertIn("chat_return_timeout", str(caught.exception))
            self.assertNotIn(repr(value), str(caught.exception))

        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {"chat_return_timeout": 0}}),
            self.assertRaises(ImproperlyConfigured),
        ):
            app_settings.validate_all()

    def test_chat_run_setting_boundaries(self):
        maximum = 2_147_483_647
        upper_bounds = {
            "chat_soft_time_limit": maximum - 12,
            "chat_cleanup_timeout": 1,
            "chat_hard_time_limit": maximum,
            "chat_queue_wait_timeout": maximum,
        }
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": upper_bounds}):
            policy = app_settings.chat_run_settings()
            self.assertEqual(policy.soft_time_limit, maximum - 12)
            self.assertEqual(policy.hard_time_limit, maximum)
            self.assertEqual(policy.queue_wait_timeout, maximum)
        for key in upper_bounds:
            with (
                self.subTest(setting=key, value=maximum + 1),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {**upper_bounds, key: maximum + 1}}),
                self.assertRaises(ImproperlyConfigured),
            ):
                app_settings.validate_all()

        settings_base = {
            "chat_soft_time_limit": 30,
            "chat_cleanup_timeout": 5,
        }
        for hard_limit in (45, 44):
            with (
                self.subTest(hard_limit=hard_limit),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {**settings_base, "chat_hard_time_limit": hard_limit}}),
            ):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    app_settings.chat_run_settings()
                self.assertNotIn("30", str(caught.exception))

        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {**settings_base, "chat_hard_time_limit": 45}}),
            self.assertRaises(ImproperlyConfigured),
        ):
            app_settings.validate_all()

        for queue, expected_queue in ((None, None), ("  worker-queue  ", "worker-queue")):
            with (
                self.subTest(queue=queue),
                self.settings(
                    PLUGINS_CONFIG={
                        "nautobot_agent": {
                            **settings_base,
                            "chat_hard_time_limit": 46,
                            "chat_celery_queue": queue,
                        }
                    }
                ),
            ):
                self.assertEqual(
                    app_settings.chat_run_settings(),
                    app_settings.ChatRunSettings(
                        celery_queue=expected_queue,
                        soft_time_limit=30,
                        hard_time_limit=46,
                        cleanup_timeout=5,
                        queue_wait_timeout=600,
                    ),
                )

        for queue in ("", " \t\n "):
            with (
                self.subTest(queue=queue),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"chat_celery_queue": queue}}),
                self.assertRaises(ImproperlyConfigured),
            ):
                app_settings.chat_run_settings()

    def test_source_mapping(self):
        for app_config, expected in (
            ({"source_apps_by_group": {}}, {}),
            ({"source_apps_by_group": {"Empty Group": []}}, {"Empty Group": ()}),
            (
                {"source_apps_by_group": {"Readers": ["enabled_app", "nested.package", "enabled_app"]}},
                {"Readers": ("enabled_app", "nested.package")},
            ),
        ):
            with self.subTest(app_config=app_config), self.settings(PLUGINS_CONFIG={"nautobot_agent": app_config}):
                self.assertEqual(app_settings.source_apps_by_group(), expected)

        malformed = (
            None,
            [],
            "Readers",
            {None: ["enabled_app"]},
            {1: ["enabled_app"]},
            {"": ["enabled_app"]},
            {"   ": ["enabled_app"]},
            {"Readers": None},
            {"Readers": "enabled_app"},
            {"Readers": [["enabled_app"]]},
            {"Readers": [None]},
            {"Readers": [""]},
            {"Readers": ["   "]},
            {"Readers": [".enabled_app"]},
            {"Readers": ["enabled_app."]},
            {"Readers": ["enabled..app"]},
            {"Readers": ["9enabled_app"]},
            {"Readers": ["enabled-app"]},
            {"Readers": ["enabled/app"]},
            {"Readers": ["/opt/enabled_app"]},
            {"Readers": ["enabled*"]},
            {"Readers": ["**"]},
            {"Readers": ["class"]},
            {"Readers": ["vendor.class"]},
            {"Readers": [" enabled_app"]},
            {"Readers": ["enabled_app "]},
        )
        for mapping in malformed:
            with self.subTest(mapping=mapping):
                with (
                    self.settings(PLUGINS_CONFIG={"nautobot_agent": {"source_apps_by_group": mapping}}),
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    app_settings.source_apps_by_group()
                self.assertIn("source_apps_by_group", str(caught.exception))

    def test_blocked_models_are_canonical_and_strict(self):
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {}}):
            self.assertEqual(app_settings.blocked_models(), ())
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": ["DCIM.location", "dcim.Location"]}}):
            self.assertEqual(app_settings.blocked_models(), ("dcim.Location",))
            app_settings.validate_all()

        for value in (None, (), "dcim.Location", [{}], ["dcim"], ["missing.Model"], ["SECRET.Model"]):
            with (
                self.subTest(value=repr(value)),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": value}}),
                self.assertRaises(ImproperlyConfigured) as caught,
            ):
                app_settings.validate_all()
            self.assertNotIn("SECRET", str(caught.exception))

        proxy = next(model for model in apps.get_models() if model._meta.proxy)
        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": [proxy._meta.label]}}),
            self.assertRaises(ImproperlyConfigured) as caught,
        ):
            app_settings.blocked_models()
        self.assertIn(proxy._meta.concrete_model._meta.label, str(caught.exception))

        swapped = next(model for model in apps.get_models(include_swapped=True) if model._meta.swapped)
        with (
            self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": [swapped._meta.label]}}),
            self.assertRaises(ImproperlyConfigured) as caught,
        ):
            app_settings.blocked_models()
        self.assertIn(swapped._meta.swapped, str(caught.exception))

    def test_query_limits_and_parent_containers(self):
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"query": {"max_page_bytes": 4096}}}):
            self.assertEqual(app_settings.query_limits(), 4096)

        malformed = (
            None,
            [],
            {"nautobot_agent": None},
            {"nautobot_agent": []},
            {"nautobot_agent": {"query": None}},
            {"nautobot_agent": {"query": []}},
            {"nautobot_agent": {"query": {"unknown": "SECRET-SENTINEL"}}},
            *(
                {"nautobot_agent": {"query": {"max_page_bytes": value}}}
                for value in (None, True, False, 1, 1023, 1.5, "SECRET-SENTINEL", [], {})
            ),
        )
        for plugins_config in malformed:
            with self.subTest(plugins_config=repr(plugins_config)[:80]):
                with (
                    self.settings(PLUGINS_CONFIG=plugins_config),
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    app_settings.query_limits()
                self.assertNotIn("SECRET-SENTINEL", str(caught.exception))
                self.assertNotIn(repr(plugins_config), str(caught.exception))

        readers = (
            (app_settings.interpreter_limits, "interpreter"),
            (app_settings.scope_mode, "scope_mode"),
            (app_settings.tracing_enabled, "tracing"),
            (app_settings.builtin_skills_enabled, "builtin_skills"),
            (app_settings.source_apps_by_group, "source_apps_by_group"),
            (app_settings.query_limits, "query"),
        )
        for reader, setting_name in readers:
            for plugins_config in (None, {"nautobot_agent": "SECRET-SENTINEL"}):
                with (
                    self.subTest(reader=reader.__name__, plugins_config=plugins_config),
                    self.settings(PLUGINS_CONFIG=plugins_config),
                    self.assertRaises(ImproperlyConfigured) as caught,
                ):
                    reader()
                self.assertIn(setting_name, str(caught.exception))
                self.assertNotIn("SECRET-SENTINEL", str(caught.exception))

    def test_ready_rejects_malformed_settings_without_exposing_values(self):
        config = apps.get_app_config("nautobot_agent")
        for setting_name, value in (
            ("chat_soft_timelimit", "SECRET"),
            ("mcp_environment", {"profile": {"TOKEN": "SECRET"}}),
            ("credentials", {"SECRET": []}),
            ("mcp_credentials", {"profile": {"Bad Header": "SECRET"}}),
            ("mcp_environments", {"profile": {"TOKEN": "SECRET\x00"}}),
            ("interpreter", {"max_host_calls": "SECRET"}),
            ("scope_mode", "SECRET"),
            ("tracing", "SECRET"),
            ("builtin_skills", "SECRET"),
            ("source_apps_by_group", {"Readers": ["SECRET/invalid"]}),
            ("query", {"max_page_bytes": "SECRET"}),
            ("chat_return_timeout", "SECRET"),
            ("chat_celery_queue", ["SECRET"]),
        ):
            with (
                self.subTest(setting_name=setting_name),
                patch.object(NautobotAppConfig, "ready"),
                patch("nautobot_agent.sources.source_access.initialize_agent_security"),
                self.settings(PLUGINS_CONFIG={"nautobot_agent": {setting_name: value}}),
                patch("nautobot_agent.runtime.llm.init_chat_model") as build,
                patch("nautobot_agent.runtime.agent.create_deep_agent") as factory,
                self.assertRaises(ImproperlyConfigured) as caught,
            ):
                config.ready()
            self.assertIn(setting_name, str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))
            build.assert_not_called()
            factory.assert_not_called()
