"""LLM configuration model, form, native HTML page, and Agent runtime behavior."""

import json
import uuid
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.urls import reverse
from langchain_core.messages import AIMessage
from nautobot.apps.testing import TestCase
from nautobot.users.models import ObjectPermission

from nautobot_agent.models import LLMConfiguration
from nautobot_agent.tests.support.chat import (
    ScriptedToolModel,
    create_configuration,
    enable_agent_settings,
    page_json,
    post_run_message,
    run_dependencies,
    runtime_graph,
    use_runtime,
)
from nautobot_agent.tests.support.graph import SourceGraphTestCase
from nautobot_agent.tests.support.html import anchors, controls, hidden_values
from nautobot_agent.tests.support.sources import fixture_catalog

RESERVED_KEYS = ("model", "model_provider", "configurable_fields", "config_prefix")
ACTIONS = ("view", "add", "change", "delete")


class LLMConfigurationModelTests(TestCase):
    """Model validation and resolution own constructor arguments and role uniqueness."""

    def setUp(self):
        super().setUp()
        enable_agent_settings(
            self,
            {"corp": {"api_key": "unused"}, "empty": {}, "reserved": {"model": "not-used"}},
        )

    def validation_errors(self, **fields):
        """Return the field errors raised by full validation of a row, or an empty dict."""
        row = LLMConfiguration(name=fields.pop("name", f"row-{uuid.uuid4().hex}"), **fields)
        try:
            row.full_clean()
        except ValidationError as error:
            return error.message_dict
        return {}

    def test_field_validation_and_independent_errors(self):
        for identifier in ("", "gpt-test", ":gpt-test", "openai:", " :x", "openai: "):
            with self.subTest(identifier=identifier):
                self.assertIn("model", self.validation_errors(model=identifier))
        self.assertEqual(self.validation_errors(model="openai:ft:gpt-test:org:id"), {})

        self.assertIn("parameters", self.validation_errors(model="openai:x", parameters=["not", "an", "object"]))
        for key in RESERVED_KEYS:
            with self.subTest(key=key):
                self.assertIn("parameters", self.validation_errors(model="openai:x", parameters={key: "x"}))
                enable_agent_settings(self, {"empty": {}, "reserved": {key: "synthetic"}})
                self.assertEqual(
                    set(
                        self.validation_errors(
                            model="openai:x", credential_profile="reserved", parameters={key: "synthetic"}
                        )
                    ),
                    {"parameters", "credential_profile"},
                )
        nested = {"extra_body": {"model": "nested-keys-are-provider-options"}, "temperature": 0.2}
        self.assertEqual(self.validation_errors(model="openai:x", parameters=nested), {})

        self.assertEqual(
            set(self.validation_errors(model="bad", parameters={"model": "unsafe"})),
            {"model", "parameters"},
        )
        self.assertEqual(
            set(
                self.validation_errors(
                    model="openai:x", credential_profile="gone", parameters={"model_provider": "unsafe"}
                )
            ),
            {"credential_profile", "parameters"},
        )
        self.assertEqual(
            set(self.validation_errors(model="openai:x", credential_profile="reserved", parameters=["invalid"])),
            {"credential_profile", "parameters"},
        )
        self.assertIn("credential_profile", self.validation_errors(model="openai:x", credential_profile="gone"))
        self.assertIn("credential_profile", self.validation_errors(model="openai:x", credential_profile="reserved"))
        self.assertEqual(self.validation_errors(model="openai:x", credential_profile="empty"), {})
        self.assertEqual(self.validation_errors(model="openai:x", credential_profile=""), {})

        for profile_name in (123, [], {}):
            with self.subTest(profile_type=type(profile_name).__name__):
                non_string_profile = LLMConfiguration(
                    name="non-string-profile", model="openai:x", credential_profile=profile_name, parameters={}
                )
                with self.assertRaises(ValidationError) as caught:
                    non_string_profile.resolve_model_arguments()
                self.assertIn("credential_profile", caught.exception.message_dict)

    def test_role_errors_aggregate_with_resolution_errors(self):
        create_configuration(name="default-holder", model="openai:one", is_default=True)
        errors = self.validation_errors(model="bad", parameters={"model": "unsafe"}, is_default=True)
        self.assertEqual(set(errors), {"model", "parameters"})

    def test_role_transfer_and_failed_save_rollback(self):
        first = LLMConfiguration(name="first", model="openai:first", is_default=True, is_scope_classifier=True)
        second = LLMConfiguration(name="second", model="openai:second", is_default=True, is_scope_classifier=True)
        first.full_clean()
        second.full_clean()
        first.save()
        second.save()
        first.refresh_from_db()
        self.assertEqual((first.is_default, first.is_scope_classifier), (None, None))
        self.assertEqual(LLMConfiguration.objects.get(is_default=True), second)
        self.assertEqual(LLMConfiguration.objects.get(is_scope_classifier=True), second)

        unset = LLMConfiguration.objects.get(pk=second.pk)
        unset.is_default = False
        unset.is_scope_classifier = False
        unset.save(update_fields=("is_default", "is_scope_classifier"))
        unset.refresh_from_db()
        self.assertIsNone(unset.is_default)

        first = LLMConfiguration(name="classifier-one", model="openai:one", is_scope_classifier=True)
        second = LLMConfiguration(name="classifier-two", model="openai:two", is_scope_classifier=True)
        first.full_clean()
        second.full_clean()
        first.save()
        second.save()
        first.refresh_from_db()
        self.assertIsNone(first.is_scope_classifier)
        self.assertEqual(LLMConfiguration.objects.get(is_scope_classifier=True), second)

        replacement = LLMConfiguration(name=second.name, model="openai:replacement", is_scope_classifier=True)
        with self.assertRaises(ValidationError):
            replacement.save()
        self.assertEqual(LLMConfiguration.objects.get(is_scope_classifier=True), second)

        first.is_scope_classifier = True  # ty: ignore[invalid-assignment] Django instance boolean.
        first.save(update_fields=("name",))
        self.assertEqual(LLMConfiguration.objects.get(is_scope_classifier=True), second)
        first.save(update_fields=())
        self.assertEqual(LLMConfiguration.objects.get(is_scope_classifier=True), second)

        LLMConfiguration.objects.all().delete()
        LLMConfiguration.objects.create(name="same", model="openai:first")
        with self.assertRaises(ValidationError) as duplicate:
            LLMConfiguration.objects.create(name="same", model="openai:second")
        self.assertIn("name", duplicate.exception.message_dict)


class _ConfigurationPageCase(TestCase):
    """Shared real-client helpers for the native configuration pages."""

    def setUp(self):
        super().setUp()
        enable_agent_settings(self, {"corp": {"api_key": "unused"}})
        self.list_url = reverse("plugins:nautobot_agent:llmconfiguration_list")
        self.add_url = reverse("plugins:nautobot_agent:llmconfiguration_add")

    @staticmethod
    def detail_url(pk):
        """Return a configuration's detail page URL."""
        return reverse("plugins:nautobot_agent:llmconfiguration", kwargs={"pk": pk})

    @staticmethod
    def edit_url(pk):
        """Return a configuration's edit page URL."""
        return reverse("plugins:nautobot_agent:llmconfiguration_edit", kwargs={"pk": pk})

    @staticmethod
    def delete_url(pk):
        """Return a configuration's delete confirmation page URL."""
        return reverse("plugins:nautobot_agent:llmconfiguration_delete", kwargs={"pk": pk})

    def grant(self, *actions, **kwargs):
        """Replace the test user's app grants with these actions and constraints."""
        ObjectPermission.objects.filter(name__startswith="nautobot_agent.").delete()
        self.add_permissions(*(f"nautobot_agent.{action}_llmconfiguration" for action in actions), **kwargs)

    @staticmethod
    def hrefs_by_id(response):
        """Map rendered link ids to their destinations."""
        return {anchor["id"]: anchor["href"] for anchor in anchors(response) if anchor["id"]}

    @staticmethod
    def href_with_text(response, text):
        """Return the destination of the first rendered link whose text contains ``text``."""
        return next(anchor["href"] for anchor in anchors(response) if text in anchor["text"])

    def hx_list(self):
        """Request the list table the way the browser's HTMX call does."""
        return self.client.get(self.list_url, HTTP_HX_REQUEST="true")

    def save(self, data, pk=None, query="", client=None):
        """POST a standard form submission to the add page (no pk) or a configuration's edit page."""
        payload = {"name": "page-row", "model": "openai:gpt-test", "parameters": "", **data}
        return (client or self.client).post((self.edit_url(pk) if pk else self.add_url) + query, payload)

    def confirm_delete(self, pk, query=""):
        """GET the rendered confirmation and submit exactly the hidden values it rendered."""
        url = self.delete_url(pk) + query
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        return self.client.post(url, hidden_values(page))

    def assert_success(self, response):
        """Assert exactly one native success message was queued."""
        sent = list(get_messages(response.wsgi_request))
        self.assertEqual([message.level for message in sent], [message_levels.SUCCESS])

    @staticmethod
    def snapshot():
        """Return every persisted row, for exact before/after comparison."""
        return list(LLMConfiguration.objects.values())


class ConfigurationReadTests(_ConfigurationPageCase):
    """Routing, list and detail pages."""

    def test_read_views_follow_grants(self):
        self.grant(*ACTIONS)

        unknown = uuid.uuid4()
        for url in (self.detail_url(unknown), self.edit_url(unknown), self.delete_url(unknown)):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(f"{self.list_url}not-a-uuid/").status_code, 404)

        row = create_configuration(name="zz-listed", model="openai:<b>x</b>")
        self.grant("view")

        shell = self.client.get(self.list_url)
        table = self.hx_list()

        self.assertNotContains(shell, "zz-listed")
        self.assertContains(table, "zz-listed")
        self.assertContains(table, "openai:&lt;b&gt;x&lt;/b&gt;")
        self.assertNotContains(table, "<b>x</b>")
        links = {anchor["href"].split("?")[0] for anchor in anchors(table)}
        self.assertIn(self.detail_url(row.pk), links)
        self.assertNotIn(self.edit_url(row.pk), links)
        self.assertNotIn(self.delete_url(row.pk), links)
        self.assertNotIn("add-button", {anchor["id"] for anchor in anchors(shell)})

        self.grant("view", "add", "change", "delete")
        self.assertEqual(self.hrefs_by_id(self.client.get(self.list_url))["add-button"], self.add_url)
        for label, url in (("Edit", self.edit_url(row.pk)), ("Delete", self.delete_url(row.pk))):
            href = self.href_with_text(self.hx_list(), label)
            self.assertEqual(href.split("?")[0], url)
            self.assertEqual(self.client.get(href).status_code, 200)

        create_configuration(name="zz-grouped", is_default=False)
        self.grant("view")
        self.add_permissions("extras.add_staticgroupassociation")
        self.assertTrue(self.user.has_perm("extras.add_staticgroupassociation"))

        shell, table = self.client.get(self.list_url), self.hx_list()
        self.assertContains(table, "zz-grouped")
        for page in (shell, table):
            self.assertNotContains(page, "update_dynamic_groups_for_selected")
            self.assertNotContains(page, "update_dynamic_groups_for_all")

    def test_detail_renders_parameters_and_escapes_metadata(self):
        row = create_configuration(
            name="<i>name</i>",
            credential_profile="corp",
            parameters={"temperature": 0.2, "extra_body": {"top_k": 5}, "note": "<script>alert(2)</script>"},
        )
        self.grant("view")

        page = self.client.get(self.detail_url(row.pk))

        for value in ("corp", "temperature", "0.2", "extra_body", "top_k"):
            self.assertContains(page, value)
        self.assertNotContains(page, "<i>name</i>")
        self.assertNotContains(page, "<script>alert(2)</script>")
        self.assertContains(page, "&lt;i&gt;name&lt;/i&gt;")
        self.assertContains(page, "&lt;script&gt;alert(2)&lt;/script&gt;")
        self.assertFalse({"edit-button", "delete-button"} & set(self.hrefs_by_id(page)))

        self.grant("view", "change", "delete")
        page = self.client.get(self.detail_url(row.pk))
        for element_id, url in (("edit-button", self.edit_url(row.pk)), ("delete-button", self.delete_url(row.pk))):
            href = self.hrefs_by_id(page)[element_id]
            self.assertEqual(href.split("?")[0], url)


class ConfigurationAccessTests(_ConfigurationPageCase):
    """Permissions, constraints, anonymous users, CSRF and unsupported requests."""

    def test_mutation_permissions(self):
        row = create_configuration(name="shown")
        self.grant("view")
        before = self.snapshot()

        for url in (self.add_url, self.edit_url(row.pk), self.delete_url(row.pk)):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.save({"name": "created"}).status_code, 403)
        self.assertEqual(self.save({"model": "openai:changed"}, pk=row.pk).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url(row.pk), {"confirm": "True"}).status_code, 403)

        self.client.logout()
        for url in (self.list_url, self.detail_url(row.pk), self.edit_url(row.pk), self.delete_url(row.pk)):
            self.assertIn("login", self.client.get(url)["Location"])
        self.assertIn("login", self.client.post(self.delete_url(row.pk), {"confirm": "True"})["Location"])
        self.assertEqual(self.snapshot(), before)

        self.client.force_login(self.user)
        row = create_configuration(name="doomed", is_default=False)

        self.grant("add")
        created = self.save({"name": "added"})
        self.assertEqual(created.status_code, 403)
        self.assertFalse(LLMConfiguration.objects.filter(name="added").exists())

        self.grant("change")
        edited = self.save({"name": "doomed", "model": "openai:edited"}, pk=row.pk)
        self.assertEqual(edited.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.model, "openai:gpt-test")

        self.grant("delete")
        self.assertRedirects(self.confirm_delete(row.pk), self.list_url, fetch_redirect_response=False)
        self.assertFalse(LLMConfiguration.objects.filter(pk=row.pk).exists())

    def test_constraints_limit_visible_rows_and_roll_back_violating_saves(self):
        inside = create_configuration(name="alpha-inside", is_default=False)
        outside = create_configuration(name="omega-outside", is_default=False)
        self.grant(*ACTIONS, constraints={"name": "alpha-inside"})
        before = self.snapshot()

        table = self.hx_list()
        self.assertContains(table, "alpha-inside")
        self.assertNotContains(table, "omega-outside")
        self.assertEqual(self.client.get(self.detail_url(inside.pk)).status_code, 200)
        for url in (self.detail_url(outside.pk), self.edit_url(outside.pk), self.delete_url(outside.pk)):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(self.delete_url(outside.pk), {"confirm": "True"}).status_code, 404)

        self.assertEqual(self.save({"name": "forbidden"}).status_code, 403)
        self.assertEqual(self.save({"name": "renamed"}, pk=inside.pk).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_editor_cannot_redirect_profile_or_ambient_credentials(self):
        self.grant(*ACTIONS)
        for profile in ("corp", ""):
            with self.subTest(profile=profile):
                row = create_configuration(name=f"protected-{profile}", credential_profile=profile, is_default=False)
                before = self.snapshot()
                payload = {
                    "name": row.name,
                    "credential_profile": profile,
                    "parameters": json.dumps({"base_url": "https://untrusted.example/v1"}),
                }
                self.assertEqual(self.save({**payload, "name": f"new-{profile}"}).status_code, 403)
                self.assertEqual(self.save(payload, pk=row.pk).status_code, 403)
                self.assertEqual(self.snapshot(), before)

    def test_posts_without_a_csrf_token_are_rejected(self):
        row = create_configuration()
        self.grant(*ACTIONS)
        before = self.snapshot()
        strict = self.client_class(enforce_csrf_checks=True)
        strict.force_login(self.user)

        created = self.save({}, client=strict)
        edited = self.save({"model": "openai:changed"}, pk=row.pk, client=strict)
        deleted = strict.post(self.delete_url(row.pk), {"confirm": "True"})

        for response in (created, edited, deleted):
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(), before)


class ConfigurationMutationTests(_ConfigurationPageCase):
    """Native create, update and delete through the rendered forms."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def test_duplicate_name_race_returns_bound_form_error(self):
        self.grant(*ACTIONS)
        original_save = LLMConfiguration.save
        inserted = False

        def save_with_competitor(instance, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                original_save(LLMConfiguration(name=instance.name, model="openai:winner"))
            return original_save(instance, **kwargs)

        with patch.object(LLMConfiguration, "save", autospec=True, side_effect=save_with_competitor):
            response = self.save({"name": "racing", "model": "openai:loser"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)
        self.assertEqual(response.context["form"]["model"].value(), "openai:loser")
        self.assertFalse(LLMConfiguration.objects.exists())

    def test_crud_persists_and_return_links_are_safe(self):
        self.grant(*ACTIONS)
        parameters = {"temperature": 0.1, "extra_body": {"model": "nested-is-provider-data"}}

        created = self.save(
            {
                "name": "audited",
                "model": "openai:ft:gpt:org:id",
                "credential_profile": "corp",
                "parameters": json.dumps(parameters),
                "is_default": "on",
            }
        )
        row = LLMConfiguration.objects.get()
        self.assertRedirects(created, self.detail_url(row.pk))
        self.assert_success(created)
        self.assertEqual(
            (row.model, row.credential_profile, row.parameters, row.is_default),
            ("openai:ft:gpt:org:id", "corp", parameters, True),
        )

        edited = self.save({"name": "audited", "model": "openai:second", "credential_profile": ""}, pk=row.pk)
        self.assertRedirects(edited, self.detail_url(row.pk))
        self.assert_success(edited)
        row.refresh_from_db()
        self.assertEqual((row.model, row.credential_profile, row.parameters), ("openai:second", "", {}))

        deleted = self.confirm_delete(row.pk)
        self.assertRedirects(deleted, self.list_url, fetch_redirect_response=False)
        self.assert_success(deleted)
        self.assertFalse(LLMConfiguration.objects.filter(pk=row.pk).exists())

        row = create_configuration(name="doomed", is_default=False)

        self.assertEqual(self.href_with_text(self.client.get(self.edit_url(row.pk)), "Cancel"), self.detail_url(row.pk))
        from_list = self.href_with_text(self.hx_list(), "Delete")
        cancel = self.href_with_text(self.client.get(from_list), "Cancel")
        self.assertEqual(cancel, self.list_url)
        self.assertEqual(self.client.get(cancel).status_code, 200)
        self.assertTrue(LLMConfiguration.objects.filter(pk=row.pk).exists())

        unsafe = "?" + urlencode({"return_url": "https://evil.example/landing/"})
        unsafe_page = self.client.get(self.delete_url(row.pk) + unsafe)
        self.assertEqual(self.href_with_text(unsafe_page, "Cancel"), self.detail_url(row.pk))
        saved = self.save({"name": "page-row"}, pk=row.pk, query=unsafe)
        self.assertRedirects(saved, self.detail_url(row.pk), fetch_redirect_response=False)
        query = from_list.removeprefix(self.delete_url(row.pk))
        self.assertRedirects(self.confirm_delete(row.pk, query), self.list_url, fetch_redirect_response=False)

    def test_invalid_submissions_do_not_save(self):  # pylint: disable=too-many-locals
        self.grant(*ACTIONS)
        page = self.client.get(self.add_url)
        rendered_controls = controls(page).controls
        for field in ("is_default", "is_scope_classifier"):
            self.assertEqual(rendered_controls[field]["type"], "checkbox")

        created = self.save({"name": "classifier", "is_scope_classifier": "on"})
        row = LLMConfiguration.objects.get()
        self.assertRedirects(created, self.detail_url(row.pk), fetch_redirect_response=False)
        self.assertEqual((row.is_default, row.is_scope_classifier), (None, True))

        # Exercise role acquisition, coexistence, independent release, and final release.
        for roles, expected in (
            ({"is_default": "on", "is_scope_classifier": "on"}, (True, True)),
            ({"is_scope_classifier": "on"}, (None, True)),
            ({"is_default": "on"}, (True, None)),
            ({}, (None, None)),
        ):
            with self.subTest(roles=roles, expected=expected):
                response = self.save({"name": "classifier", **roles}, pk=row.pk)
                self.assertRedirects(response, self.detail_url(row.pk), fetch_redirect_response=False)
                row.refresh_from_db()
                self.assertEqual((row.is_default, row.is_scope_classifier), expected)

        LLMConfiguration.objects.all().delete()
        original_save = LLMConfiguration.save
        inserted = False

        def save_with_competitor(instance, *args, **kwargs):
            nonlocal inserted
            if instance.name == "racing" and not inserted:
                inserted = True
                original_save(LLMConfiguration(name="winner", model="openai:winner", is_default=True))
            return original_save(instance, *args, **kwargs)

        with patch.object(LLMConfiguration, "save", autospec=True, side_effect=save_with_competitor):
            response = self.save({"name": "racing", "model": "openai:racing", "is_default": "on"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(LLMConfiguration.objects.get(is_default=True).name, "racing")
        self.assertIsNone(LLMConfiguration.objects.get(name="winner").is_default)

        LLMConfiguration.objects.all().delete()
        first = create_configuration(name="first", model="openai:one", parameters={"temperature": 0.1})
        create_configuration(name="second", model="openai:two", is_default=False, is_scope_classifier=True)
        self.grant(*ACTIONS)
        before = self.snapshot()

        for data, pk in (
            ({"name": "new", "model": "gpt-test"}, None),
            ({"name": "first", "model": ":gpt-test"}, first.pk),
            ({"name": "new", "parameters": json.dumps({"model": "x"})}, None),
            ({"name": "first", "parameters": "{bad json"}, first.pk),
        ):
            with self.subTest(data=data):
                self.assertEqual(self.save(data, pk=pk).status_code, 200)
                self.assertEqual(self.snapshot(), before)

        keep = {"name": "first", "model": "openai:one"}
        self.assertEqual(self.save({**keep, "is_default": "on"}, pk=first.pk).status_code, 302)
        self.assertEqual(self.save({**keep, "parameters": ""}, pk=first.pk).status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.parameters, {})

        LLMConfiguration.objects.all().delete()
        row = create_configuration(name="legacy", model="openai:old", credential_profile="removed", is_default=False)
        self.grant(*ACTIONS)

        rendered = controls(self.client.get(self.edit_url(row.pk))).options["credential_profile"]
        self.assertEqual([value for value, selected in rendered if selected], ["removed"])

        rejected = self.save({"name": "legacy", "model": "openai:new", "credential_profile": "removed"}, pk=row.pk)
        self.assertEqual(rejected.status_code, 200)
        row.refresh_from_db()
        self.assertEqual((row.model, row.credential_profile), ("openai:old", "removed"))

        accepted = self.save({"name": "legacy", "model": "openai:new", "credential_profile": "corp"}, pk=row.pk)
        self.assertRedirects(accepted, self.detail_url(row.pk), fetch_redirect_response=False)
        row.refresh_from_db()
        self.assertEqual((row.model, row.credential_profile), ("openai:new", "corp"))


class AgentConfigurationGraphTests(SourceGraphTestCase):  # pylint: disable=too-many-ancestors
    """Real durable admission requires outermost commits to test graph/configuration cutover."""

    def setUp(self):
        super().setUp()
        self.models = {
            name: ScriptedToolModel(
                responses=[AIMessage(content=f"{name.split(':')[1]} answer")], profile={"max_input_tokens": limit}
            )
            for name, limit in {
                "openai:alpha": 100_000,
                "openai:beta": 200_000,
                "openai:gamma": 300_000,
            }.items()
        }
        use_runtime(
            self,
            saver=self.runtime.saver,
            sources=fixture_catalog(self, self.tree),
        )
        self.page_url = reverse("plugins:nautobot_agent:agent")

    def use_scripted_models(self):
        """Replace only the model factory: unknown models fail the way an unusable configuration does."""

        def build(configuration):
            if configuration.model not in self.models:
                raise ImproperlyConfigured("model could not be constructed")
            return self.models[configuration.model]

        use_runtime(self, model_factory=build)

    def assert_unconfigured(self):
        """Assert an unusable model blocks execution but not model-free page rendering."""
        with self.assertRaises(ImproperlyConfigured):
            runtime_graph(self.user)
        page = self.client.get(self.page_url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'role="alert"')
        with run_dependencies():
            send = post_run_message(self.client, "hello")
        self.assertEqual(send.status_code, 200)
        self.assertEqual(send.json()["status"], "failed")

    def test_missing_or_failing_default_returns_safe_unavailable_configuration(self):
        self.use_scripted_models()
        row = create_configuration(name="alpha", model="openai:alpha")
        runtime_graph(self.user)

        LLMConfiguration.objects.filter(pk=row.pk).update(is_default=None)
        self.assert_unconfigured()
        LLMConfiguration.objects.create(name="beta", model="openai:beta")
        self.assert_unconfigured()

        row.is_default = True
        row.model = "openai:unusable"
        row.save()
        self.assert_unconfigured()

        row.model = "openai:alpha"
        row.save()
        runtime_graph(self.user)
        self.assertEqual(self.send_message("again"), ["alpha answer", "done"])

    def test_configuration_change_build_failure_recovers_with_history(self):
        self.use_scripted_models()
        row = create_configuration(name="alpha", model="openai:alpha")
        runtime_graph(self.user)
        self.assertEqual(self.send_message("before failure"), ["alpha answer", "done"])

        row.model = "openai:beta"
        row.save()
        with (
            patch(
                "nautobot_agent.runtime.agent.create_deep_agent",
                side_effect=RuntimeError("graph construction failed"),
            ),
            self.assertRaises(RuntimeError),
        ):
            runtime_graph(self.user)

        runtime_graph(self.user)
        self.assertEqual(self.send_message("after retry"), ["beta answer", "done"])
        history = page_json(self.client.get(self.page_url), "agent-state")["history"]
        self.assertEqual(
            [(entry["role"], entry["content"]) for entry in history if entry["role"] != "tool"],
            [
                ("user", "before failure"),
                ("assistant", "alpha answer"),
                ("user", "after retry"),
                ("assistant", "beta answer"),
            ],
        )

    def test_real_factory_supplies_credentials_and_context_and_a_removed_profile_blocks_a_restarted_worker(self):
        credentials = {"local": {"api_key": "placeholder", "base_url": "http://localhost:11434/v1"}}
        enable_agent_settings(self, credentials)
        create_configuration(
            name="local",
            model="openai:local-model",
            credential_profile="local",
            parameters={"profile": {"max_input_tokens": 8192}},
        )

        runtime_graph(self.user)
        page = self.client.get(self.page_url)
        self.assertIsNone(page_json(page, "agent-state")["context_usage"]["limit"])

        enable_agent_settings(self, {})
        use_runtime(self)
        self.assert_unconfigured()
