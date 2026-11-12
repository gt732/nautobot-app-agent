"""Focused MCP server validation and native configuration behavior."""

import asyncio
import json
import os
from typing import cast
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import Client, SimpleTestCase
from django.urls import reverse
from fastmcp import Client as MCPClient
from fastmcp import Context, FastMCP
from nautobot.apps.testing import TestCase

from nautobot_agent import app_settings
from nautobot_agent.forms import MCPServerForm
from nautobot_agent.mcp import tools as mcp_tools
from nautobot_agent.mcp.launch import Launch
from nautobot_agent.mcp.turn import MCPTurn
from nautobot_agent.models import MCPServer
from nautobot_agent.tests.support.chat import enable_agent_settings
from nautobot_agent.tests.support.html import hidden_values
from nautobot_agent.tests.support.mcp import Host

ACTIONS = ("view", "add", "change", "delete")


class MCPServerConfigurationTests(TestCase):
    """Model validation, deployment profile choices, and native CRUD permissions."""

    def setUp(self):
        super().setUp()
        self._server_count = 0
        enable_agent_settings(
            self,
            mcp_credentials={"headers-prod": {"Authorization": "Bearer do-not-render"}},
            mcp_environments={"stdio-prod": {"TOKEN": "env-never-render"}},
        )
        self.list_url = reverse("plugins:nautobot_agent:mcpserver_list")
        self.add_url = reverse("plugins:nautobot_agent:mcpserver_add")

    def grant(self, *actions, constraints=None):
        """Replace MCP object grants with the requested actions and optional row constraint."""
        self.add_permissions(
            *(f"nautobot_agent.{action}_mcpserver" for action in actions),
            constraints=constraints or {},
        )

    def server(self, **overrides):
        """Return a valid unsaved server row with a distinct display name."""
        self._server_count += 1
        return MCPServer(
            name=overrides.pop("name", f"Configuration fixture {self._server_count}"),
            url=overrides.pop("url", "http://127.0.0.1:8000/mcp"),
            **overrides,
        )

    def test_duplicate_name_race_returns_bound_form_error(self):
        self.grant(*ACTIONS)
        self.client.force_login(self.user)
        original_save = MCPServer.save
        inserted = False

        def save_with_competitor(instance, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                original_save(MCPServer(name=instance.name, url="https://winner.example/mcp"))
            return original_save(instance, **kwargs)

        with patch.object(MCPServer, "save", autospec=True, side_effect=save_with_competitor):
            response = self.client.post(
                self.add_url, {"name": "racing", "transport": "http", "url": "https://loser.example/mcp"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(response.context["form"]["url"].value(), "https://loser.example/mcp")
        self.assertFalse(MCPServer.objects.exists())

    def test_model_and_form_validation(self):  # pylint: disable=too-many-locals
        for url in (
            "ftp://example.test/mcp",
            "http://user:password@example.test/mcp",
            "https://example.test/mcp#fragment",
            "http://example.test:invalid/mcp",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.server(url=url).full_clean()
        form = MCPServerForm(
            {"name": "Private endpoint", "transport": "http", "url": "https://private.example/mcp?mode=tools"}
        )
        self.assertTrue(form.is_valid(), form.errors)

        MCPServer(name="Valid stdio", transport="stdio", launch={"command": "runner"}).full_clean()
        invalid_fields = (
            {"command": ""},
            {"args": "not-a-list"},
            {"env": ["not", "an", "object"]},
            {"cwd": "relative/path"},
            {"command": "run\0ner"},
            {"args": ["bad\0argument"]},
            {"cwd": "bad\0directory"},
            {"cwd": None},
            {"env": {"BAD=KEY": "value"}},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                MCPServer(name="Invalid stdio", transport="stdio", launch={"command": "runner", **fields}).full_clean()

        missing_stdio_profile = MCPServerForm(
            {
                "name": "Missing profile",
                "transport": "stdio",
                "launch": '{"command":"runner"}',
                "credential_profile": "missing-environment",
            }
        )
        self.assertFalse(missing_stdio_profile.is_valid())
        self.assertIn("credential_profile", missing_stdio_profile.errors)

        existing = self.server(name="Existing name", url="https://existing.example")
        existing.save()
        self.assertFalse(MCPServerForm({"transport": "http", "url": "https://example.test"}).is_valid())
        missing_transport = MCPServerForm({"name": "Missing transport", "url": "https://example.test"})
        self.assertFalse(missing_transport.is_valid())
        self.assertIn("transport", missing_transport.errors)
        missing_url = MCPServerForm({"name": "Missing URL", "transport": "http"})
        self.assertFalse(missing_url.is_valid())
        self.assertIn("url", missing_url.errors)
        for name in ("", "   "):
            form = MCPServerForm({"name": name, "transport": "http", "url": "https://example.test"})
            self.assertFalse(form.is_valid())
            self.assertIn("name", form.errors)
        duplicate = MCPServerForm(
            {
                "name": existing.name,
                "transport": "http",
                "url": "https://different.example",
                "credential_profile": "headers-prod",
            },
            instance=self.server(url="https://different.example"),
        )
        self.assertFalse(duplicate.is_valid())
        self.assertIn("name", duplicate.errors)
        self.assertFalse(MCPServerForm({"name": "Blank JSON", "transport": "stdio", "launch": " "}).is_valid())

        invalid = (
            "{invalid",
            "[]",
            '{"command":"server","unknown":true}',
            '{"command":4}',
            '{"command":"server","args":{}}',
        )
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                form = MCPServerForm({"name": f"Invalid {len(raw)}", "transport": "stdio", "launch": raw})
                self.assertFalse(form.is_valid())
                self.assertIn("launch", form.errors)

        cwd_too_long = os.path.abspath("x" * 2049)
        launch_failures = (
            {"command": "x" * 2049},
            {"cwd": cwd_too_long},
        )
        for launch in launch_failures:
            raw = json.dumps({"command": "runner", **launch})
            form = MCPServerForm({"name": f"Constraint {len(raw)}", "transport": "stdio", "launch": raw})
            self.assertFalse(form.is_valid())
            self.assertIn("launch", form.errors)

    def test_launch_spec_is_validated_and_trusted(self):
        row = MCPServer(
            name="Factory order",
            url="https://mcp.example.test",
            enabled=True,
            credential_profile="headers-prod",
        )
        with patch.object(
            app_settings, "mcp_credential_profiles", return_value={"headers-prod": {"Authorization": "secret-value"}}
        ):
            launch = Launch.from_server(row)
        self.assertEqual(launch.url, "https://mcp.example.test")
        self.assertEqual(launch.alias, row.pk.hex[:12])  # ty: ignore[unresolved-attribute] Nautobot UUIDField descriptor is typed as the field.
        self.assertNotIn("secret-value", repr(launch))
        with self.assertRaises(TypeError):
            cast(dict, launch.headers)["Authorization"] = "mutated"

        with self.assertRaises(ImproperlyConfigured):
            Launch.from_server(MCPServer(name="Invalid factory order", url="not-a-url", enabled=True))

        stdio = MCPServer(
            name="Detached environment",
            transport="stdio",
            launch={"command": "runner", "args": ["--serve"], "env": {"PLAIN": "value"}},
            enabled=True,
        )
        specification = Launch.from_server(stdio)
        self.assertEqual(specification.args, ("--serve",))
        self.assertNotIn("value", repr(specification))

        client = launch.client(None)
        self.assertEqual(client.transport.url, "https://mcp.example.test")
        self.assertEqual(client.transport.headers, {"Authorization": "secret-value"})
        stdio_client = specification.client(None)
        self.assertFalse(stdio_client.transport.keep_alive)

        remote = FastMCP("discard-server-logs")
        secret = "server-log-secret-must-not-leak"

        @remote.tool
        async def log_secret(ctx: Context) -> str:
            await ctx.info(secret)
            return "logged"

        async def exercise(logging_client):
            async with logging_client:
                await logging_client.call_tool("log_secret", {})

        with self.assertLogs("fastmcp.client.from_server", level="INFO") as baseline:
            asyncio.run(exercise(MCPClient(remote)))
        self.assertIn(secret, "\n".join(baseline.output))
        client.transport = MCPClient(remote).transport
        with self.assertNoLogs("fastmcp.client.from_server", level="INFO"):
            asyncio.run(exercise(client))

    def test_crud_and_profile_values_not_rendered(self):  # pylint: disable=too-many-locals
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.grant(*ACTIONS)
        self.client.force_login(self.user)
        payload = {
            "name": "HTTP inventory",
            "transport": "http",
            "url": "https://mcp.example/tools",
            "credential_profile": "headers-prod",
            "enabled": "on",
        }
        created = self.client.post(self.add_url, payload)
        row = MCPServer.objects.get(url="https://mcp.example/tools")
        detail_url = reverse("plugins:nautobot_agent:mcpserver", kwargs={"pk": row.pk})
        edit_url = reverse("plugins:nautobot_agent:mcpserver_edit", kwargs={"pk": row.pk})
        delete_url = reverse("plugins:nautobot_agent:mcpserver_delete", kwargs={"pk": row.pk})
        self.assertRedirects(created, detail_url, fetch_redirect_response=False)
        self.assertContains(self.client.get(detail_url), "HTTP inventory")
        self.assertNotContains(self.client.get(detail_url), "do-not-render")

        pasted = self.client.post(
            self.add_url,
            {
                "name": "Python inventory",
                "transport": "stdio",
                "launch": '{"command":"python","args":["-m","srv"]}',
                "credential_profile": "stdio-prod",
            },
        )
        stdio = MCPServer.objects.get(name="Python inventory")
        stdio_detail = reverse("plugins:nautobot_agent:mcpserver", kwargs={"pk": stdio.pk})
        self.assertRedirects(pasted, stdio_detail, fetch_redirect_response=False)
        self.assertEqual(
            (stdio.launch["args"], stdio.credential_profile, stdio.enabled), (["-m", "srv"], "stdio-prod", False)
        )
        self.assertContains(self.client.get(stdio_detail), "Python inventory")
        self.assertContains(self.client.get(self.list_url, HTTP_HX_REQUEST="true"), "Python inventory")
        self.assertNotContains(self.client.get(stdio_detail), "env-never-render")
        rejected_json = '{"command":"not-saved","cwd":"relative"}'
        rejected = self.client.post(
            self.add_url,
            {"name": "Bad JSON", "transport": "stdio", "launch": rejected_json},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(
            json.loads(rejected.context["form"]["launch"].value()),
            {"command": "not-saved", "cwd": "relative"},
        )
        self.assertFalse(MCPServer.objects.filter(launch__command="not-saved").exists())

        edited = self.client.post(
            edit_url,
            {**payload, "name": "HTTP inventory renamed", "url": "https://mcp.example/updated", "enabled": ""},
        )
        self.assertRedirects(edited, detail_url, fetch_redirect_response=False)
        row.refresh_from_db()
        self.assertEqual(
            (row.name, row.url, row.enabled), ("HTTP inventory renamed", "https://mcp.example/updated", False)
        )

        self.assertEqual(self.client.post(delete_url, {}).status_code, 200)
        self.assertTrue(MCPServer.objects.filter(pk=row.pk).exists())
        confirmation = self.client.get(delete_url)
        removed = self.client.post(delete_url, hidden_values(confirmation))
        self.assertRedirects(removed, self.list_url, fetch_redirect_response=False)
        self.assertFalse(MCPServer.objects.filter(pk=row.pk).exists())

        form = MCPServerForm()
        self.assertNotIn("do-not-render", str(form))
        self.assertNotIn("env-never-render", str(form))

        stale = self.server(credential_profile="removed-profile")
        stale.save()
        edit = MCPServerForm(instance=stale)
        self.assertIn("removed-profile", str(edit))
        payload = {"name": stale.name, "transport": "http", "url": stale.url, "credential_profile": "removed-profile"}
        self.assertFalse(MCPServerForm(payload, instance=stale).is_valid())
        payload["credential_profile"] = "forged-profile"
        self.assertFalse(MCPServerForm(payload, instance=stale).is_valid())
        payload["credential_profile"] = "headers-prod"
        self.assertTrue(MCPServerForm(payload, instance=stale).is_valid())

    def test_editor_cannot_save_stdio_or_credential_bearing_servers(self):
        self.grant(*ACTIONS)
        self.client.force_login(self.user)
        plain = {"transport": "http", "url": "https://untrusted.example/mcp", "credential_profile": ""}
        protected = (
            {"transport": "stdio", "launch": '{"command":"/bin/sh","args":["-c","true"]}'},
            {**plain, "credential_profile": "headers-prod"},
        )
        for index, fields in enumerate(protected):
            with self.subTest(fields=fields):
                payload = {"name": f"Protected {index}", **fields, "enabled": "on"}
                before = list(MCPServer.objects.values())
                self.assertEqual(self.client.post(self.add_url, payload).status_code, 403)
                self.assertEqual(list(MCPServer.objects.values()), before)

                row = self.server()
                row.save()
                edit_url = reverse("plugins:nautobot_agent:mcpserver_edit", kwargs={"pk": row.pk})
                before = list(MCPServer.objects.values())
                self.assertEqual(self.client.post(edit_url, payload).status_code, 403)
                self.assertEqual(list(MCPServer.objects.values()), before)

                row.transport = fields["transport"]
                row.url = fields.get("url", "")
                row.launch = json.loads(fields.get("launch", "{}"))
                row.credential_profile = fields.get("credential_profile", "")
                row.save()
                before = list(MCPServer.objects.values())
                for replacement in (fields, plain):
                    with self.subTest(replacement=replacement):
                        self.assertEqual(self.client.post(edit_url, {"name": row.name, **replacement}).status_code, 403)
                        self.assertEqual(list(MCPServer.objects.values()), before)
                forged_initial = edit_url + "?transport=http&credential_profile="
                self.assertEqual(self.client.post(forged_initial, {"name": row.name, **plain}).status_code, 403)
                self.assertEqual(list(MCPServer.objects.values()), before)

    def test_action_denial_and_constraints_roll_back_mutations(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        csrf_response = csrf_client.post(
            self.add_url, {"name": "CSRF blocked", "transport": "http", "url": "https://mcp.example"}
        )
        self.assertEqual(csrf_response.status_code, 403)
        self.assertFalse(MCPServer.objects.exists())

        self.client.force_login(self.user)
        denied = self.client.post(self.add_url, {"name": "Denied", "transport": "http", "url": "https://mcp.example"})
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(MCPServer.objects.exists())

        visible = self.server(url="https://allowed.example/mcp")
        visible.save()
        hidden = self.server(url="https://outside.example/mcp")
        hidden.save()
        self.grant(*ACTIONS, constraints={"url": "https://allowed.example/mcp"})
        response = self.client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(response, visible.name)
        self.assertNotContains(response, hidden.name)
        self.assertEqual(
            self.client.post(
                reverse("plugins:nautobot_agent:mcpserver_edit", kwargs={"pk": visible.pk}),
                {
                    "name": visible.name,
                    "transport": "http",
                    "url": "https://changed.example/mcp",
                    "credential_profile": "",
                    "enabled": "on",
                },
            ).status_code,
            200,
        )
        visible.refresh_from_db()
        self.assertEqual(visible.url, "https://allowed.example/mcp")
        self.assertEqual(
            self.client.get(reverse("plugins:nautobot_agent:mcpserver", kwargs={"pk": hidden.pk})).status_code, 404
        )


class MCPServerProfileResolutionTests(SimpleTestCase):
    """Exercise transport-specific profile resolution without database access."""

    def catalogs(self):
        """Return transport catalogs with valid, empty, and unset profile values."""
        return {
            "mcp_credentials": {
                "http-only": {"Authorization": "http-secret"},
                "unset": {"Authorization": "unset-http-secret", "X-Unset": None},
                "empty": {},
                "empty-string": {"X-Value": ""},
                "shared": {"X-Transport": "http-secret"},
            },
            "mcp_environments": {
                "stdio-only": {"TOKEN": "stdio-secret"},
                "unset": {"TOKEN": "unset-stdio-secret", "UNSET": None},
                "empty": {},
                "empty-string": {"VALUE": ""},
                "shared": {"X-Transport": "stdio-secret"},
            },
        }

    def server(self, transport, credential_profile="", **values):
        """Return a valid unsaved MCP server for the requested transport."""
        launch = (
            {"url": "https://mcp.example.test", **values}
            if transport == "http"
            else {"launch": {"command": "runner", **values}}
        )
        return MCPServer(
            name="Resolver fixture",
            transport=transport,
            credential_profile=credential_profile,
            **launch,
        )

    def configure(self, credentials=None, environments=None):
        """Return temporary real application settings for the profile readers."""
        catalogs = self.catalogs()
        config = {
            "mcp_credentials": catalogs["mcp_credentials"] if credentials is None else credentials,
            "mcp_environments": catalogs["mcp_environments"] if environments is None else environments,
        }
        return self.settings(PLUGINS_CONFIG={"nautobot_agent": config})

    def test_resolver_errors_are_field_attributed_and_secret_free(self):
        with self.configure():
            for transport, wrong_transport_profile, other_secret, unset_secret in (
                ("http", "stdio-only", "stdio-secret", "unset-http-secret"),
                ("stdio", "http-only", "http-secret", "unset-stdio-secret"),
            ):
                for profile in ("missing", wrong_transport_profile, "unset"):
                    with self.subTest(transport=transport, profile=profile):
                        with self.assertRaises(ValidationError) as caught:
                            self.server(transport, profile).clean()
                        self.assertEqual(set(caught.exception.message_dict), {"credential_profile"})
                        self.assertNotIn(other_secret, repr(caught.exception))
                        self.assertNotIn(unset_secret, repr(caught.exception))
                        self.assertNotIn("Authorization", repr(caught.exception))
                        self.assertNotIn("TOKEN", repr(caught.exception))

            for profile in (["invalid"], {"invalid": "profile"}):
                with self.subTest(profile=profile), self.assertRaises(ValidationError) as caught:
                    self.server("http", profile).clean()
                self.assertEqual(set(caught.exception.message_dict), {"credential_profile"})

            invalid_transport = self.server("invalid", "")
            with self.assertRaises(ValidationError) as caught:
                invalid_transport.resolve_credential_profile()
            self.assertEqual(set(caught.exception.message_dict), {"transport"})
            with self.assertRaises(ValidationError) as caught:
                invalid_transport.full_clean(validate_unique=False, validate_constraints=False)
            self.assertEqual(len(caught.exception.message_dict["transport"]), 1)

    def test_runtime_revalidates_current_settings_before_discovery(self):
        with self.configure(credentials={"runtime": {"Authorization": "runtime-sentinel"}}):
            validated = self.server("http", "runtime", enabled=True)
            validated.clean()
            with self.configure(credentials={}):
                turn = MCPTurn(host=Host())
                self.addCleanup(turn.close)
                discover = Mock()
                turn.discover = discover
                for row in (validated, self.server("http", "runtime", enabled=True)):
                    with self.subTest(row=row):
                        snapshot = mcp_tools.MCPToolCache().get(("runtime-revalidation",), [row], turn)
                        self.assertEqual(snapshot.tools, ())
                        self.assertEqual(snapshot.unavailable, (mcp_tools.UnavailableMCPServer(str(row.pk), row.name),))
                        self.assertNotIn("runtime-sentinel", repr(snapshot))
                discover.assert_not_called()

        for profile in ("missing", "unset"):
            with self.subTest(transport="stdio", profile=profile), self.configure():
                turn = MCPTurn(host=Host())
                self.addCleanup(turn.close)
                row = self.server("stdio", profile, enabled=True)
                snapshot = mcp_tools.MCPToolCache().get(("stdio-revalidation", profile), [row], turn)
                self.assertEqual(snapshot.tools, ())
                self.assertEqual(snapshot.unavailable, (mcp_tools.UnavailableMCPServer(str(row.pk), row.name),))

    def test_stdio_launch_profile_precedence_and_detachment(self):
        with self.configure(environments={"runtime": {"TOKEN": "profile-value"}}):
            server = self.server("stdio", "runtime", env={"TOKEN": "inline-value", "OTHER": "retained"})
            launch = Launch.from_server(server)
            self.assertEqual(dict(launch.env), {"TOKEN": "profile-value", "OTHER": "retained"})
            server.launch["env"]["OTHER"] = "changed"
            self.assertEqual(dict(launch.env), {"TOKEN": "profile-value", "OTHER": "retained"})
            with self.assertRaises(TypeError):
                cast(dict, launch.env)["OTHER"] = "mutated"


class MCPServerProfileFormTests(TestCase):
    """Exercise strict profile form validation and disabled-record recovery."""

    def test_invalid_persisted_profile_can_be_cleared_while_disabling(self):
        catalogs = {
            "mcp_credentials": {"unset": {"Authorization": None}},
            "mcp_environments": {"unset": {"TOKEN": None}},
        }
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": catalogs}):
            for transport in ("http", "stdio"):
                with self.subTest(transport=transport):
                    launch = (
                        {"url": "https://mcp.example.test"}
                        if transport == "http"
                        else {"launch": {"command": "runner"}}
                    )
                    row = MCPServer(
                        name=f"Recover {transport} row",
                        transport=transport,
                        credential_profile="unset",
                        enabled=True,
                        **launch,
                    )
                    row.save()
                    payload = {
                        "name": row.name,
                        "transport": transport,
                        "url": row.url,
                        "launch": '{"command":"runner"}',
                        "credential_profile": "unset",
                    }
                    rejected = MCPServerForm(payload, instance=row)
                    self.assertFalse(rejected.is_valid())
                    self.assertIn("credential_profile", rejected.errors)
                    row.refresh_from_db()
                    self.assertTrue(row.enabled)
                    payload["credential_profile"] = ""
                    form = MCPServerForm(payload, instance=row)
                    self.assertTrue(form.is_valid(), form.errors)
                    recovered = form.save()
                    recovered.refresh_from_db()
                    self.assertEqual((recovered.credential_profile, recovered.enabled), ("", False))
