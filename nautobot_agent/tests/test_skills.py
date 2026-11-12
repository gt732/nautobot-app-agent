"""Focused custom Skill domain and native administration tests."""

from types import SimpleNamespace
from unittest.mock import patch

from deepagents.middleware.skills import _parse_skill_metadata
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, SimpleTestCase
from django.urls import reverse
from django.utils.html import escape
from nautobot.apps.models import BaseModel
from nautobot.apps.testing import TestCase

from nautobot_agent.forms import SkillForm
from nautobot_agent.models import NamedConstraintConflict, Skill, unique_constraint_name
from nautobot_agent.skills import SkillDefinition
from nautobot_agent.sources.backends import SkillsBackend


class SkillDomainTests(TestCase):
    """Exercise stable identity, validation, immutable capture and parser compatibility."""

    def skill(self, **overrides):
        """Return a valid unsaved skill value."""
        fields = {"name": "Device Audit", "description": "Audit configured devices", "instructions": "Check state."}
        fields.update(overrides)
        return Skill(**fields)

    def test_identifier_generation_and_stable_rename(self):
        skill = self.skill(name="Model__Inventory!!")
        skill.full_clean()
        self.assertEqual(skill.identifier, "model-inventory")
        skill.save()
        skill.name = "Renamed for administrators"
        skill.full_clean()
        skill.save()
        self.assertEqual(skill.identifier, "model-inventory")
        self.assertEqual(skill.name, "Renamed for administrators")

        non_latin = self.skill(name="日本語")
        non_latin.full_clean()
        self.assertEqual(non_latin.identifier, non_latin.pk.hex)
        long_name = self.skill(name=f"{'a' * 63}---b")
        long_name.full_clean()
        self.assertEqual(long_name.identifier, "a" * 63)
        exact_length = self.skill(name="a" * 64)
        exact_length.full_clean()
        self.assertEqual(exact_length.identifier, "a" * 64)
        self.assertLessEqual(len(long_name.identifier), 64)

        existing = self.skill()
        existing.save()
        existing.name = "Renamed display label"
        existing.save()
        with self.assertRaises(ValidationError) as duplicate_error:
            self.skill().full_clean()
        self.assertIn("name", duplicate_error.exception.message_dict)
        self.assertIn("device-audit", duplicate_error.exception.message_dict["name"][0])
        self.assertNotIn("Renamed display label", str(duplicate_error.exception))

        existing.identifier = ""
        with self.assertRaises(ValidationError) as blank_error:
            existing.save()
        self.assertIn("identifier", blank_error.exception.message_dict)

    def test_content_validation(self):
        self.skill(description="d" * 1024).full_clean()
        with self.assertRaises(ValidationError) as error:
            self.skill(description="d" * 1025).full_clean()
        self.assertIn("description", error.exception.message_dict)

        self.skill(instructions="é" * 32768).full_clean()
        with self.assertRaises(ValidationError) as error:
            self.skill(instructions="é" * 32769).full_clean()
        self.assertIn("instructions", error.exception.message_dict)
        long_body = "\n".join(f"line {line}" for line in range(501))
        self.skill(instructions=long_body).full_clean()

        for field in ("name", "description", "instructions"):
            for value in (" \n ", "invalid \ud800"):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ValidationError) as error:
                        self.skill(**{field: value}).full_clean()
                    self.assertIn(field, error.exception.message_dict)
        body = "  indented\n\ntrailing  \n"
        skill = self.skill(instructions=body)
        skill.save()
        skill.refresh_from_db()
        self.assertEqual(skill.instructions, body)

    def test_builtin_identifiers_cannot_be_shadowed_by_custom_skills(self):
        for name in ("Nautobot Charts", "Nautobot Mermaid"):
            with self.subTest(name=name):
                form = SkillForm(
                    {"name": name, "description": "Override", "instructions": "Custom body.", "enabled": True}
                )
                self.assertFalse(form.is_valid())
                self.assertIn("name", form.errors)
        skill = self.skill(enabled=True)
        skill.save()
        Skill.objects.filter(pk=skill.pk).update(identifier="nautobot-charts")
        with self.assertRaises(ValidationError):
            Skill.objects.runtime_snapshot()

    def test_builtins_remain_available_without_database_skills(self):
        custom = self.skill(enabled=True)
        custom.save()
        disabled = self.skill(name="Disabled skill", enabled=False)
        disabled.save()
        snapshot = Skill.objects.runtime_snapshot()
        self.assertEqual(
            tuple(item.identifier for item in snapshot),
            ("device-audit", "nautobot-charts", "nautobot-mermaid"),
        )
        Skill.objects.all().delete()
        backend = SkillsBackend(Skill.objects.runtime_snapshot())
        for identifier in ("nautobot-charts", "nautobot-mermaid"):
            path = f"/{identifier}/SKILL.md"
            result = backend.read(path)
            assert result.file_data is not None
            parsed = _parse_skill_metadata(result.file_data["content"], path, identifier)
            assert parsed is not None
            self.assertEqual(parsed["name"], identifier)
            self.assertTrue(backend.write(path, "override").error)

    def test_runtime_snapshot_rejects_queryset_updated_invalid_identifier(self):
        skill = self.skill(enabled=True)
        skill.save()
        Skill.objects.filter(pk=skill.pk).update(identifier="bad--id")

        with self.assertRaises(ValidationError) as error:
            Skill.objects.runtime_snapshot()
        self.assertIn("identifier", error.exception.message_dict)

    def test_disabling_builtins_preserves_only_enabled_custom_skills(self):
        custom = self.skill(enabled=True)
        custom.save()
        self.skill(name="Disabled skill", enabled=False).save()
        with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"builtin_skills": False}}):
            snapshot = Skill.objects.runtime_snapshot()
            self.assertEqual(tuple(item.identifier for item in snapshot), ("device-audit",))
            backend = SkillsBackend(snapshot)
            result = backend.read("/device-audit/SKILL.md")
            assert result.file_data is not None
            self.assertIn(custom.instructions, result.file_data["content"])
            for identifier in ("nautobot-charts", "nautobot-mermaid"):
                self.assertIsNotNone(backend.read(f"/{identifier}/SKILL.md").error)
            Skill.objects.filter(pk=custom.pk).delete()
            self.assertEqual(Skill.objects.runtime_snapshot(), ())

            reserved = self.skill(name="Nautobot Charts")
            with self.assertRaises(ValidationError):
                reserved.full_clean()

    def test_unique_races_translate_without_retry(self):  # pylint: disable=too-many-locals
        loser = self.skill()
        winner = self.skill(description="Competing row")
        original_clean = Skill.full_clean
        original_save = BaseModel.save
        insert_count = 0

        def clean_with_competitor(instance, *args, **kwargs):
            result = original_clean(instance, *args, **kwargs)
            if instance is loser:
                winner.save()
            return result

        def counted_save(instance, *args, **kwargs):
            nonlocal insert_count
            if instance is loser:
                insert_count += 1
            return original_save(instance, *args, **kwargs)

        with (
            patch.object(Skill, "full_clean", clean_with_competitor),
            patch.object(BaseModel, "save", autospec=True, side_effect=counted_save),
        ):
            with self.assertRaises(NamedConstraintConflict) as conflict:
                loser.save()

        self.assertEqual(insert_count, 1)
        self.assertIn("device-audit", conflict.exception.message_dict["name"][0])
        self.assertFalse(Skill.objects.filter(pk=loser.pk).exists())
        self.assertEqual(Skill.objects.get(pk=winner.pk).description, "Competing row")
        for vendor, cause, expected in (
            ("postgresql", SimpleNamespace(diag=SimpleNamespace(constraint_name="another_unique")), "another_unique"),
            (
                "mysql",
                SimpleNamespace(args=(1062, "Duplicate entry 'x' for key 'skills.other_unique'")),
                "other_unique",
            ),
            ("mysql", SimpleNamespace(args=(1452, "foreign key failure")), None),
        ):
            with self.subTest(vendor=vendor):
                translated = IntegrityError("database write failed")
                translated.__cause__ = Exception(*getattr(cause, "args", ()))
                translated.__cause__.__dict__.update(vars(cause))
                self.assertEqual(unique_constraint_name(translated, vendor), expected)

    def test_generated_document_round_trips_yaml_sensitive_metadata_and_body(self):
        description = (
            'YAML: "quoted" # literal 😀\n'
            + "".join(chr(codepoint) for codepoint in range(1, 32))
            + "\u007f\u0085\u009f\u2028\u2029\ufeff\ufffe\uffff"
        )
        body = "# Instruction\n\nKeep this body unchanged.  \n"
        definition = SkillDefinition("device-audit", description, body)
        parsed = _parse_skill_metadata(definition.document, "/skills/device-audit/SKILL.md", "device-audit")
        if parsed is None:
            self.fail("Generated skill metadata must parse.")
        self.assertEqual(parsed["name"], "device-audit")
        self.assertEqual(parsed["description"], description)
        self.assertTrue(definition.document.endswith(body))

        second = self.skill(name="Zebra", description="second", instructions="Second.")
        first = self.skill(name="Alpha", description="first", instructions="First.")
        second.enabled = first.enabled = True
        second.save()
        first.save()
        snapshot = Skill.objects.runtime_snapshot()
        self.assertEqual(
            tuple(item.identifier for item in snapshot),
            ("alpha", "nautobot-charts", "nautobot-mermaid", "zebra"),
        )
        with self.assertRaises(AttributeError):
            snapshot[0].instructions = "changed"


class SkillsSnapshotBackendTests(SimpleTestCase):
    """Exercise SDK in-memory operations over the captured database projection."""

    def test_read_search_download_and_mutation_policy(self):
        definition = SkillDefinition("device-audit", "Audit devices", "Check state.\nReturn findings.")
        backend = SkillsBackend((definition,))
        path = "/device-audit/SKILL.md"
        read = backend.read(path)
        assert read.file_data is not None
        self.assertIn("Check state.", read.file_data["content"])
        self.assertEqual([match["path"] for match in (backend.glob("*/SKILL.md").matches or [])], [path])
        self.assertEqual([match["path"] for match in (backend.grep("Check state.").matches or [])], [path])
        self.assertEqual(backend.download_files([path])[0].content, read.file_data["content"].encode())
        self.assertTrue(backend.write(path, "changed").error)
        self.assertTrue(backend.edit(path, "Check", "Changed").error)
        self.assertTrue(backend.delete(path).error)
        self.assertEqual(backend.upload_files([(path, b"changed")])[0].error, "permission_denied")
        self.assertEqual(backend.read(path).file_data, read.file_data)


class SkillNativeAdministrationTests(TestCase):
    """Exercise permission-aware native skill CRUD, CSRF, help, and escaped details."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.list_url = reverse("plugins:nautobot_agent:skill_list")
        self.add_url = reverse("plugins:nautobot_agent:skill_add")

    def grant(self, *actions, constraints=None):
        """Grant native skill actions, optionally constrained to matching rows."""
        self.add_permissions(
            *(f"nautobot_agent.{action}_skill" for action in actions),
            constraints=constraints or {},
        )

    def payload(self, **overrides):
        """Return valid authoring fields without an enabled-by-default assumption."""
        fields = {
            "name": "Device Audit",
            "description": "Audit configured devices when asked.",
            "instructions": "  Check the device.\n\nReturn findings.  \n",
        }
        fields.update(overrides)
        return fields

    def test_crud_and_escaped_detail(self):
        form = SkillForm(self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.fields), {"name", "description", "instructions", "enabled"})
        self.assertEqual(form.cleaned_data["instructions"], self.payload()["instructions"])
        self.assertFalse(form.cleaned_data["enabled"])

        denied = self.client.post(self.add_url, self.payload())
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Skill.objects.exists())

        self.grant("view", "add", "change", "delete")
        body = "<script>alert('not executable')</script>\n  keep spacing  \n"
        created = self.client.post(self.add_url, self.payload(instructions=body, enabled=""))
        skill = Skill.objects.get()
        detail_url = reverse("plugins:nautobot_agent:skill", kwargs={"pk": skill.pk})
        edit_url = reverse("plugins:nautobot_agent:skill_edit", kwargs={"pk": skill.pk})
        delete_url = reverse("plugins:nautobot_agent:skill_delete", kwargs={"pk": skill.pk})
        self.assertRedirects(created, detail_url, fetch_redirect_response=False)
        self.assertEqual((skill.identifier, skill.enabled, skill.instructions), ("device-audit", False, body))
        detail = self.client.get(detail_url)
        self.assertContains(detail, escape(body))
        self.assertNotContains(detail, body)

        listing = self.client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(listing, "device-audit")
        self.assertNotContains(listing, "not executable")

        edited = self.client.post(
            edit_url,
            self.payload(name="New Friendly Label", instructions="Updated\n  instructions", identifier="forged"),
        )
        self.assertRedirects(edited, detail_url, fetch_redirect_response=False)
        skill.refresh_from_db()
        self.assertEqual(skill.name, "New Friendly Label")
        self.assertEqual(skill.identifier, "device-audit")
        self.assertEqual(skill.instructions, "Updated\n  instructions")

        confirmation = self.client.get(delete_url)
        self.assertContains(confirmation, skill.name)
        deleted = self.client.post(delete_url, {"confirm": "true"})
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(Skill.objects.filter(pk=skill.pk).exists())

    def test_object_constraints_hide_and_roll_back_skill_mutations(self):
        allowed = Skill.objects.create(name="Allowed skill", description="Safe description", instructions="Keep me.")
        hidden = Skill.objects.create(name="Hidden skill", description="Hidden", instructions="Hidden body.")
        self.grant("view", "add", "change", constraints={"name__startswith": "Allowed"})
        listing = self.client.get(self.list_url, HTTP_HX_REQUEST="true")
        self.assertContains(listing, allowed.name)
        self.assertNotContains(listing, hidden.name)
        self.assertEqual(
            self.client.get(reverse("plugins:nautobot_agent:skill", kwargs={"pk": hidden.pk})).status_code,
            404,
        )
        denied_create = self.client.post(self.add_url, self.payload(name="Outside scope"))
        self.assertEqual(denied_create.status_code, 200)
        self.assertTrue(denied_create.context["form"].non_field_errors())
        self.assertFalse(Skill.objects.filter(name="Outside scope").exists())
        update = self.client.post(
            reverse("plugins:nautobot_agent:skill_edit", kwargs={"pk": allowed.pk}),
            self.payload(name="Outside scope", instructions="Changed."),
        )
        self.assertEqual(update.status_code, 200)
        self.assertTrue(update.context["form"].non_field_errors())
        allowed.refresh_from_db()
        self.assertEqual((allowed.name, allowed.instructions), ("Allowed skill", "Keep me."))

    def test_native_skill_create_edit_and_delete_posts_require_csrf(self):
        self.grant("view", "add", "change", "delete")
        skill = Skill.objects.create(name="CSRF target", description="Description", instructions="Body.")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        routes = (
            (self.add_url, self.payload()),
            (
                reverse("plugins:nautobot_agent:skill_edit", kwargs={"pk": skill.pk}),
                self.payload(name="CSRF target", instructions="Changed."),
            ),
            (reverse("plugins:nautobot_agent:skill_delete", kwargs={"pk": skill.pk}), {"confirm": "true"}),
        )
        for url, payload in routes:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)
                self.assertEqual(client.post(url, payload).status_code, 403)
        skill.refresh_from_db()
        self.assertEqual(skill.instructions, "Body.")
        self.assertEqual(Skill.objects.count(), 1)
