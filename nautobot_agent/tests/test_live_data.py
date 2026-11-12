"""Owning-layer tests for live-data path policy and related identity visibility."""

import json
import uuid
from typing import Any, cast
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext, isolate_apps
from langchain.tools import ToolException
from nautobot.apps.testing import TestCase
from nautobot.dcim.models import Device, DeviceType, Location

from nautobot_agent.data.access import (
    identity_briefs,
    path_denial,
    reject_blocked_output,
    reject_blocked_target,
    reject_sensitive,
    visible_or_null,
)
from nautobot_agent.data.admission import admit_related
from nautobot_agent.data.paths import StandardRelation, relation_by_accessor
from nautobot_agent.data.paths import resolve_path as _resolve_path
from nautobot_agent.models import AgentConversation
from nautobot_agent.tests.support.chat import create_conversation
from nautobot_agent.tests.support.data import build_fixtures, grant

User = get_user_model()
DEVICE_META = cast(Any, Device)._meta
LOCATION_META = cast(Any, Location)._meta


class ResolvedPathPolicyTests(SimpleTestCase):
    """Path policy distinguishes tolerant admission metadata from strict stored scalar paths."""

    def test_path_resolution_classification(self):
        relation = _resolve_path(Device, "location")
        self.assertIs(relation.brief_field, DEVICE_META.get_field("location"))
        self.assertEqual(relation.forward_hops, ())
        self.assertIs(relation.local_field, DEVICE_META.get_field("location"))

        nested = _resolve_path(Device, "location__name")
        self.assertEqual(len(nested.forward_hops), 1)
        self.assertEqual(nested.forward_hops[0].stored_path, "location_id")
        self.assertEqual(nested.forward_hops[0].field, DEVICE_META.get_field("location"))
        self.assertEqual(nested.brief_field, None)
        self.assertEqual(nested.local_field, None)
        self.assertIsNone(_resolve_path(Device, "location_id").brief_field)
        terminal_relation = _resolve_path(Device, "location__parent")
        self.assertIs(terminal_relation.brief_field, LOCATION_META.get_field("parent"))
        self.assertEqual(relation.stored_scalar(), DEVICE_META.get_field("location").target_field)
        self.assertEqual(nested.stored_scalar().name, "name")

        self.assertEqual(_resolve_path(Device, "pk").local_field, DEVICE_META.pk)
        self.assertIsNone(_resolve_path(Device, "name__lower").local_field)
        # Lookup-shaped and unknown keys retain metadata for admission to pass to the ORM.
        lookup = _resolve_path(Device, "name__lower")
        self.assertFalse(lookup.crosses_unsupported_relation)
        unknown = _resolve_path(Device, "not_a_field")
        self.assertFalse(unknown.crosses_unsupported_relation)

        for path in ("name__lower", "location_id__name", "pk__x"):
            with self.subTest(path=path), self.assertRaises(ToolException) as caught:
                _resolve_path(Device, path).stored_scalar()
            self.assertEqual(json.loads(str(caught.exception))["error"], "ValidationError")

        with self.assertRaises(ToolException) as unresolved:
            _resolve_path(Device, "not_a_field").stored_scalar()
        self.assertIn("does not resolve to a stored field", json.loads(str(unresolved.exception))["message"])

        reverse = _resolve_path(Device, "interfaces__name")
        self.assertTrue(reverse.crosses_unsupported_relation)
        for path in ("interfaces", "interfaces__unknown", "tags", "tags__unknown", "location_id__name", "pk__x"):
            with self.subTest(path=path), self.assertRaises(ToolException) as caught:
                _resolve_path(Device, path).stored_scalar()
            self.assertEqual(json.loads(str(caught.exception))["error"], "ValidationError")
        for path in ("unknown", "location__unknown", "location__"):
            with self.subTest(path=path), self.assertRaises(ToolException) as caught:
                _resolve_path(Device, path).stored_scalar()
            self.assertIn("does not resolve to a stored field", json.loads(str(caught.exception))["message"])
        self.assertIs(
            _resolve_path(Location, "_custom_field_data").local_field, LOCATION_META.get_field("_custom_field_data")
        )
        for path in ("-name", "parent__", "name__lower"):
            with self.subTest(path=path):
                self.assertIsNone(_resolve_path(Location, path).local_field)
        model = apps.get_model("dcim.VirtualChassis")
        relation = _resolve_path(model, "master")
        self.assertIs(relation.brief_field, model._meta.get_field("master"))
        self.assertIs(relation.local_field, relation.brief_field)
        self.assertFalse(relation.crosses_unsupported_relation)
        self.assertIs(relation.stored_scalar(), DEVICE_META.pk)
        nested = _resolve_path(model, "master__name")
        self.assertEqual(nested.forward_hops[0].stored_path, "master_id")
        self.assertEqual(nested.stored_scalar().name, "name")

    def test_field_iteration_and_static_path_denial_precedence(self):
        location = DEVICE_META.get_field("location")
        resolved = _resolve_path(Device, "location__missing")
        self.assertEqual(list(resolved.iter_fields()), [location])
        self.assertIsNone(path_denial(_resolve_path(Device, "location__missing")))

        with patch.object(Location, "sensitive_fields", ("location_type_id",), create=True):
            self.assertEqual(path_denial(_resolve_path(Location, "location_type")), "sensitive")
            self.assertEqual(path_denial(_resolve_path(Location, "location_type_id")), "sensitive")
            self.assertEqual(path_denial(_resolve_path(Location, "location_type__missing")), "sensitive")

            # Sensitive declarations on the relation take precedence over its internal target.
            with patch.object(User, "sensitive_fields", ("nautobot_agent_conversations",), create=True):
                internal_relation = _resolve_path(User, "nautobot_agent_conversations")
                self.assertEqual(path_denial(internal_relation), "sensitive")

            with self.assertRaises(ToolException) as caught:
                reject_sensitive(
                    [
                        ("first_public_alias", _resolve_path(Location, "location_type_id")),
                        ("nautobot_agent_conversations", internal_relation),
                    ]
                )
            error = json.loads(str(caught.exception))
            self.assertEqual(error["error"], "PermissionDenied")
            self.assertIn("'first_public_alias'", error["message"])

        self.assertEqual(path_denial(_resolve_path(User, "nautobot_agent_conversations")), "internal_storage")
        with self.assertRaises(ToolException) as caught:
            reject_sensitive([("nautobot_agent_conversations", _resolve_path(User, "nautobot_agent_conversations"))])
        self.assertEqual(json.loads(str(caught.exception))["error"], "PermissionDenied")

    def test_related_admission_checks_relation_and_target_paths_in_order(self):
        relation = cast(StandardRelation, relation_by_accessor(Device, "location"))
        with patch.object(Device, "sensitive_fields", ("location",), create=True):
            with self.assertRaises(ToolException) as caught:
                admit_related(Device, relation, [])
            self.assertEqual(json.loads(str(caught.exception))["error"], "PermissionDenied")

        with patch.object(Location, "sensitive_fields", ("location_type_id",), create=True):
            for requested_path in ("location_type", "location_type_id", "location_type__missing"):
                with self.subTest(requested_path=requested_path):
                    with self.assertRaises(ToolException) as caught:
                        admit_related(Device, relation, [requested_path])
                    error = json.loads(str(caught.exception))
                    self.assertEqual(error["error"], "PermissionDenied")

    @isolate_apps()
    def test_blocked_target_uses_concrete_storage_owner(self):
        class PolicyParent(models.Model):
            """Registry-only concrete storage parent."""

            class Meta:
                app_label = "nautobot_agent"

        class PolicyChild(PolicyParent):
            """Registry-only multi-table child."""

            class Meta:
                app_label = "nautobot_agent"

        with patch("nautobot_agent.data.access.blocked_models", return_value=("dcim.Location",)):
            with self.assertRaises(ToolException) as caught:
                reject_blocked_target(Location)
            self.assertEqual(json.loads(str(caught.exception))["error"], "PermissionDenied")
            reject_blocked_target(Device)

            proxy = next(model for model in apps.get_models() if model._meta.proxy)
            with patch(
                "nautobot_agent.data.access.blocked_models", return_value=(proxy._meta.concrete_model._meta.label,)
            ):
                with self.assertRaises(ToolException):
                    reject_blocked_target(proxy)
        with patch("nautobot_agent.data.access.blocked_models", return_value=("nautobot_agent.PolicyParent",)):
            with self.assertRaises(ToolException):
                reject_blocked_target(PolicyChild)
            with self.assertRaises(ToolException):
                reject_blocked_output(_resolve_path(PolicyChild, "id"))
        with patch("nautobot_agent.data.access.blocked_models", return_value=("nautobot_agent.PolicyChild",)):
            reject_blocked_target(PolicyParent)

    def test_blocked_output_distinguishes_identity_from_detail_paths(self):
        with patch("nautobot_agent.data.access.blocked_models", return_value=("dcim.location",)):
            for path in ("location", "location_id"):
                with self.subTest(path=path):
                    reject_blocked_output(_resolve_path(Device, path))
            for path in ("location__name", "location__parent__name", "location__location_type__name"):
                with self.subTest(path=path), self.assertRaises(ToolException) as caught:
                    reject_blocked_output(_resolve_path(Device, path))
                self.assertEqual(json.loads(str(caught.exception))["error"], "PermissionDenied")
        with patch("nautobot_agent.data.access.blocked_models", return_value=("dcim.devicetype",)):
            with self.assertRaises(ToolException):
                reject_blocked_output(_resolve_path(Device, "device_type__model"))


class LiveDataVisibilityTests(TestCase):
    """Projection visibility uses grants and hidden identity batches at the data layer."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.viewer = User.objects.create_user(username="live-data-viewer")

    def test_masking_distinguishes_absent_empty_and_constrained_grants(self):
        path = _resolve_path(Device, "device_type__model")
        for name, constraints, expected in (
            ("absent", "absent", None),
            ("unrestricted", None, "Test Router"),
            ("empty-list", [], "Test Router"),
            ("matching", {"model": "Test Router"}, "Test Router"),
            ("nonmatching", {"model": "Other Router"}, None),
        ):
            with self.subTest(grant=name):
                user = User.objects.create_user(username=f"live-data-{name}")
                if name != "absent":
                    grant(user, DeviceType, constraints=constraints)
                self.assertEqual(
                    Device.objects.filter(pk=self.data.device.pk)
                    .annotate(_visible=visible_or_null(path, "device_type__model", user))
                    .values_list("_visible", flat=True)
                    .get(),
                    expected,
                )

        # User has no native restrict(); the same grant constraints mask its related values.
        owner = User.objects.create_user(username="live-data-fallback")
        grant(owner, User, constraints={"username": owner.username})
        conversation = create_conversation(owner)
        path = _resolve_path(AgentConversation, "user__username")
        self.assertEqual(
            list(
                AgentConversation.objects.filter(pk=conversation.pk)
                .annotate(_visible=visible_or_null(path, "user__username", owner))
                .values_list("_visible", flat=True)
            ),
            [owner.username],
        )

    def test_identity_briefs_batch_once_and_skip_empty_or_null_only_batches(self):
        field = self.data.device._meta.get_field("device_type")
        types = [
            DeviceType.objects.create(
                manufacturer=self.data.device.device_type.manufacturer, model=f"Live Data Batch {index:03}"
            )
            for index in range(20)
        ]
        expected = {
            item.pk: {"id": item.pk, "display": f"{self.data.device.device_type.manufacturer.name} {item.model}"}
            for item in types
        }
        values = [item.pk for item in types] + [types[0].pk, None, uuid.uuid4()]
        with CaptureQueriesContext(connection) as queries:
            identities = identity_briefs(field, values)
        self.assertEqual(len(queries), 1)
        self.assertEqual(identities, {value: expected.get(value) for value in values})

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(identity_briefs(field, [None, None]), {None: None})
        self.assertEqual(len(queries), 0)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(identity_briefs(field, []), {})
        self.assertEqual(len(queries), 0)
