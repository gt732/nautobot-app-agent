"""Metadata-only tests for registered Nautobot model descriptions."""

import json
from unittest.mock import patch

from django.db import models
from django.test import SimpleTestCase, override_settings
from nautobot.apps.utils import get_model_from_name

from nautobot_agent.tests.support.data import INTERNAL_MODELS
from nautobot_agent.tools.describe_model import describe_model


class DescribeModelFixtureParent(models.Model):
    """Minimal registered model for sensitive primary-key metadata."""

    slug = models.CharField(max_length=64, primary_key=True)

    class Meta:
        app_label = "nautobot_agent"
        abstract = False
        managed = False


def call_describe(args, call_id="describe"):
    """Invoke the tool through LangChain's native tool-call validation path."""
    return describe_model.invoke({"type": "tool_call", "name": "describe_model", "id": call_id, "args": args})


class DescribeModelTests(SimpleTestCase):
    """Describe selected public Django metadata without touching model data."""

    def describe(self, label):
        """Return parsed metadata and its serialization; SimpleTestCase forbids database access."""
        message = call_describe({"model": label})
        self.assertEqual(message.status, "success", message.content)
        return json.loads(message.content), message.content

    def test_token_key_is_not_discoverable(self):
        description, _ = self.describe("users.Token")
        self.assertNotIn("key", {field["name"] for field in description["fields"]})

    def test_internal_models_and_user_relations_are_excluded(self):
        for label in (*INTERNAL_MODELS, "nautobot_agent.AgentRun"):
            with self.subTest(model=label):
                message = call_describe({"model": label})
                self.assertEqual(message.status, "error")
                self.assertEqual(json.loads(message.content)["error"], "PermissionDenied")

        description, _ = self.describe("users.User")
        accessors = {relation["accessor"] for relation in description["relationships"]}
        self.assertNotIn("nautobot_agent_conversations", accessors)
        self.assertNotIn("nautobot_agent_runs", accessors)

    def test_sensitive_fields_are_omitted_by_name_and_attname(self):
        device = get_model_from_name("dcim.Device")
        for declaration in ("location", "location_id"):
            with (
                self.subTest(declaration=declaration),
                patch.object(device, "sensitive_fields", (declaration,), create=True),
            ):
                description, _ = self.describe("dcim.Device")
                self.assertNotIn("location", {field["name"] for field in description["fields"]})
                self.assertNotIn("location", {item["accessor"] for item in description["relationships"]})

    def test_sensitive_primary_key_keeps_structural_label(self):
        with patch.object(DescribeModelFixtureParent, "sensitive_fields", ("slug",), create=True):
            description, _ = self.describe("nautobot_agent.DescribeModelFixtureParent")
        self.assertEqual(description["primary_key"], "slug")
        self.assertNotIn("slug", {field["name"] for field in description["fields"]})

    def test_blocked_model_description_is_denied(self):
        with override_settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": ["dcim.Location"]}}):
            message = call_describe({"model": "dcim.Location"})
        self.assertEqual(message.status, "error")
        self.assertEqual(json.loads(message.content)["error"], "PermissionDenied")

    def test_unsupported_models_and_bad_labels(self):
        for label in ("missing.Model", "dcim.NoSuchModel", "not-a-model-label", ""):
            with self.subTest(label=label):
                message = call_describe({"model": label})
                self.assertEqual(message.status, "error")
                self.assertEqual(json.loads(message.content)["error"], "ModelLookupError")
        for args in ({}, {"model": "dcim.Device", "extra": 1}, {"model": 1}, {"model": None}):
            with self.subTest(args=args):
                message = call_describe(args)
                self.assertEqual(message.status, "error")
                self.assertEqual(json.loads(message.content)["error"], "ValidationError")
        missing = json.loads(call_describe({}).content)
        detail = missing["details"][0]
        self.assertEqual(detail["loc"], ["model"])
        self.assertEqual(detail["type"], "missing")
