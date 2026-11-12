"""Real ORM and Nautobot permission coverage for the generic ``query_records`` tool."""
# pylint: disable=too-many-lines

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection, models
from django.db.models.expressions import RawSQL
from django.db.models.functions import Now, Random, Upper
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, register_lookup
from nautobot.apps.testing import TestCase
from nautobot.core.models import BaseModel
from nautobot.dcim.models import Device, Interface, Location, LocationType
from nautobot.extras.models import FileAttachment
from nautobot.tenancy.models import Tenant
from nautobot.users.models import ObjectPermission, Token
from netaddr import EUI, IPAddress, IPNetwork

from nautobot_agent.data.tool_io import dumps
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.data import (
    ALL_NAMES,
    INTERNAL_MODELS,
    REVERSE_PATH,
    TOKEN_KEY,
    build_assignment_fixtures,
    build_fixtures,
    build_twin_locations,
    call_query,
    call_trace,
    create_internal_rows,
    create_principals,
    failed,
    grant,
    list_names,
    query_byte_limit,
    restrict_spy,
    succeeded,
    temporary_model_tables,
)

User = get_user_model()


class QueryBooleanConditionTests(TestCase):
    """Boolean selection retains native Django semantics and bounded input behavior."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_assignment_fixtures(build_fixtures())
        cls.superuser = User.objects.create_superuser(username="boolean-query")

    def query(self, **args):
        """Run a query against the assignment fixture as a privileged caller."""
        return succeeded(self, call_query({"model": "dcim.Interface", **args}, self.superuser))

    def test_condition_validation_and_bounds(self):  # pylint: disable=too-many-locals
        """Malformed conditions fail safely with locations, and each cap accepts its bound and rejects one over."""
        with self.subTest("condition grammar and control like lookup"):
            malformed = (
                {},
                {"lookups": {}},
                {"lookups": None},
                {"and": []},
                {"or": []},
                {"not": []},
                {"and": None},
                {"or": None},
                {"not": None},
                {"and": [{"lookups": {"name": "x"}}, 3]},
                {"lookups": {"name": "x"}, "not": {"lookups": {"name": "y"}}},
                {"unknown": {"name": "x"}},
                {"lookups": {"name": "x"}, "extra": True},
                {"and_": [{"lookups": {"name": "x"}}]},
                {"or": {"lookups": {"name": "x"}}},
                {"not": "name == x"},
                {"lookups": {1: "x"}},
                "name == x",
            )
            for where in malformed:
                with self.subTest(where=where):
                    failed(
                        self, call_query({"model": "dcim.Interface", "where": where}, self.superuser), "ValidationError"
                    )
            for key in ("_connector", "_negated"):
                with self.subTest(key=key):
                    message = call_query(
                        {"model": "dcim.Interface", "where": {"lookups": {key: "AND"}}}, self.superuser
                    )
                    payload = failed(self, message, "FieldError")
                    self.assertNotIn("results", payload)
            deep = {"lookups": {"name": "assignment-neither"}}
            for _ in range(300):
                deep = {"not": deep}
            failed(
                self,
                call_query(
                    {
                        "model": "dcim.Interface",
                        "where": {"lookups": {"name": "x"}, "not": deep},
                    },
                    self.superuser,
                ),
                "ValidationError",
            )
        with self.subTest("validation diagnostics retain locations types and"):
            malformed = failed(
                self,
                call_query(
                    {"model": "dcim.Interface", "where": {"or": "private-sentinel-value"}},
                    self.superuser,
                ),
                "ValidationError",
            )
            detail = next(detail for detail in malformed["details"] if detail["loc"] == ["where", "or"])
            self.assertEqual(detail["type"], "list_type")
            self.assertNotIn("private-sentinel-value", detail["guidance"])
            self.assertNotIn("results", malformed)

            empty = failed(
                self,
                call_query({"model": "dcim.Interface", "where": {}}, self.superuser),
                "ValidationError",
            )
            self.assertEqual(empty["details"][0]["loc"], ["where"])
            self.assertNotIn("results", empty)

            misplaced = failed(
                self,
                call_query({"model": "dcim.Interface", "include": [{"relation": "devices"}]}, self.superuser),
                "ValidationError",
            )
            self.assertEqual(misplaced["details"][0]["loc"], ["include"])
            self.assertEqual(misplaced["details"][0]["type"], "extra_forbidden")
            self.assertNotIn("results", misplaced)

        with self.subTest("depth lookup entry and node caps"):
            lookup_leaf = {"lookups": {"name": "assignment-neither"}}
            depth_five = {
                "and": [
                    {
                        "or": [
                            {
                                "not": {
                                    "and": [
                                        {
                                            "or": [
                                                {"lookups": {"name": "not-an-interface"}},
                                            ]
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
            accepted = self.query(where=depth_five, filters={"name": "assignment-neither"})
            self.assertEqual(accepted["results"], [{"pk": str(self.data.neither.pk)}])
            depth_six = {"and": [depth_five]}
            over_limits = [depth_six]

            fifty_entries = {"and": [{"lookups": {"name": "assignment-neither"}} for _ in range(50)]}
            accepted = self.query(where=fifty_entries)
            self.assertEqual(accepted["results"], [{"pk": str(self.data.neither.pk)}])
            fifty_one_entries = {"and": [{"lookups": {"name": "assignment-neither"}} for _ in range(51)]}
            over_limits.append(fifty_one_entries)

            hundred_nodes = {
                "and": [
                    *({"not": {"lookups": {"name": "not-an-interface"}}} for _ in range(49)),
                    lookup_leaf,
                ]
            }
            accepted = self.query(where=hundred_nodes)
            self.assertEqual(accepted["results"], [{"pk": str(self.data.neither.pk)}])
            hundred_one_nodes = {"and": [*hundred_nodes["and"][:-1], {"and": [lookup_leaf]}]}
            over_limits.append(hundred_one_nodes)
            for condition in over_limits:
                with self.subTest(condition=condition):
                    payload = failed(
                        self,
                        call_query({"model": "dcim.Interface", "where": condition}, self.superuser),
                        "ValidationError",
                    )
                    self.assertEqual(payload["details"][0]["loc"], ["where"])
                    self.assertNotIn("results", payload)
                    self.assertNotIn("assignment-neither", dumps(payload))

    def test_field_comparison_admission_errors(self):
        """Comparison operands are strict paths, consume budget, and reject incompatible or multi-valued paths."""
        with self.subTest("comparison operands are strict paths and"):
            node = {"compare": {"left": "name", "lookup": "exact", "right": "name"}}
            self.assertEqual(
                self.query(mode="count", where={"and": [node] * 25})["count"],
                Interface.objects.count(),
            )
            invalid = [
                {"and": [node] * 26},
                {"compare": {"left": 1, "lookup": "exact", "right": "name"}},
                {"compare": {"left": "name", "lookup": "exact", "right": {"field": "name"}}},
                {"compare": {"left": "name", "lookup": "contains", "right": "name"}},
                {"compare": node["compare"], "lookups": {"name": "ignored"}},
            ]
            for where in invalid:
                with self.subTest(where=where):
                    failed(
                        self,
                        call_query({"model": "dcim.Interface", "where": where}, self.superuser),
                        "ValidationError",
                    )
        with self.subTest("comparison admission rejects incompatible and multivalued"):
            for comparison in (
                {"left": "name", "lookup": "exact", "right": "pk"},
                {"left": "name", "lookup": "exact", "right": "tagged_vlans__vid"},
                {"left": "pk", "lookup": "gt", "right": "device__pk"},
                {"left": "enabled", "lookup": "gt", "right": "enabled"},
                {"left": "name__lower", "lookup": "exact", "right": "name"},
                {"left": "_custom_field_data", "lookup": "exact", "right": "_custom_field_data"},
            ):
                failed(
                    self,
                    call_query(
                        {"model": "dcim.Interface", "where": {"compare": comparison}},
                        self.superuser,
                    ),
                    "ValidationError",
                )
            with patch.object(Interface, "sensitive_fields", ("name",), create=True):
                failed(
                    self,
                    call_query(
                        {
                            "model": "dcim.Interface",
                            "where": {"compare": {"left": "device__name", "lookup": "exact", "right": "name"}},
                        },
                        self.superuser,
                    ),
                    "PermissionDenied",
                )


class AgentQueryProbe(BaseModel):
    """Test-only App model on the global registry, with Nautobot's real restricted default manager.

    The test class owns this unmanaged table through the schema editor; Django's migration/serialization
    lifecycle must not create or read it before ``QueryAppModelTests`` starts.
    """

    name = models.CharField(max_length=64)

    class Meta(BaseModel.Meta):
        app_label = "nautobot_agent"
        abstract = False
        managed = False

    def __str__(self):
        return str(self.name)


class QueryToFieldParent(BaseModel):
    """Test-only parent with a unique non-primary-key relation target."""

    slug = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=64)

    class Meta(BaseModel.Meta):
        app_label = "nautobot_agent"
        abstract = False
        managed = False

    def __str__(self):
        return self.name


class QueryToFieldChild(BaseModel):
    """Test-only child whose parent FK stores the parent's slug."""

    name = models.CharField(max_length=64)
    parent = models.ForeignKey(QueryToFieldParent, to_field="slug", on_delete=models.CASCADE)

    class Meta(BaseModel.Meta):
        app_label = "nautobot_agent"
        abstract = False
        managed = False

    def __str__(self):
        return self.name


class QueryPermissionTests(TestCase):
    """Query behavior sharing the real permission graph and common root fixtures."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.visible_user, cls.hidden_user = create_principals(User.objects.create_user(username="visibleuser"))
        cls.superuser = User.objects.create_superuser(username="shared-query-root")

    def test_list_count_and_exists_expose_only_each_principals_rows(self):
        for user, expected in (
            (self.visible_user, ["Visible Alpha", "Visible Beta"]),
            (self.hidden_user, ["Hidden Gamma"]),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(list_names(self, user, {"order_by": ["name"]}), sorted(expected))
                count = succeeded(self, call_query({"model": "dcim.Location", "mode": "count"}, user))
                self.assertEqual(count, {"model": "dcim.Location", "count": len(expected)})
                exists = succeeded(self, call_query({"model": "dcim.Location", "mode": "exists"}, user))
                self.assertEqual(exists, {"model": "dcim.Location", "exists": True})
        nobody = User.objects.create_user(username="no-location-grant")
        for mode in ("list", "count", "exists"):
            with self.subTest(user="ungranted", mode=mode):
                failed(
                    self,
                    call_query({"model": "dcim.Location", "mode": mode}, nobody),
                    "PermissionDenied",
                )

    def test_each_continuation_rechecks_revoked_root_permission(self):
        user = User.objects.create_user(username="revoked-query-user")
        permission = grant(user, Location, constraints={"name__startswith": "Visible"})
        first = succeeded(
            self,
            call_query({"model": "dcim.Location", "fields": ["name"], "order_by": ["name"], "limit": 1}, user),
        )
        permission.delete()
        failed(
            self,
            call_query(
                {
                    "model": "dcim.Location",
                    "fields": ["name"],
                    "order_by": ["name"],
                    "limit": 1,
                    "cursor": first["next_cursor"],
                },
                user,
            ),
            "PermissionDenied",
        )

    def test_blocked_roots_deny_before_inventory_but_parent_identities_remain(self):
        grant(self.visible_user, Location)
        configured = {"nautobot_agent": {"blocked_models": ["dcim.Location"]}}
        for user in (self.visible_user, self.superuser):
            for mode in ("list", "count", "exists", "aggregate"):
                with (
                    self.subTest(user=user.username, mode=mode),
                    override_settings(PLUGINS_CONFIG=configured),
                    CaptureQueriesContext(connection) as captured,
                ):
                    arguments = {"model": "dcim.Location", "mode": mode}
                    if mode == "aggregate":
                        arguments["aggregates"] = {"total": {"function": "Count", "field": "pk"}}
                    failed(self, call_query(arguments, user), "PermissionDenied")
                self.assertFalse(any("dcim_location" in query["sql"].lower() for query in captured))

        identity_user = User.objects.create_user(username="blocked-location-identity")
        grant(identity_user, Device)
        with override_settings(PLUGINS_CONFIG=configured):
            payload = succeeded(
                self,
                call_query(
                    {"model": "dcim.Device", "fields": ["location", "location_id"], "order_by": ["pk"]},
                    identity_user,
                ),
            )
        self.assertEqual(
            payload["results"],
            [
                {
                    "location": {"id": str(self.data.device.location_id), "display": str(self.data.device.location)},
                    "location_id": str(self.data.device.location_id),
                }
            ],
        )

    def test_blocked_details_do_not_restrict_parent_selection_or_cursor_identity(self):
        grant(self.visible_user, Device, constraints={"location__name__startswith": "Visible"})
        grant(self.visible_user, Location)
        for name, location in (("edge-2", self.data.beta), ("hidden-edge", self.data.gamma)):
            Device.objects.create(
                name=name,
                location=location,
                device_type=self.data.device.device_type,
                role=self.data.device.role,
                status=self.data.active,
            )
        configured = {"nautobot_agent": {"blocked_models": ["dcim.Location", "tenancy.Tenant"]}}
        arguments = {
            "model": "dcim.Device",
            "fields": ["name", "location", "location_id", "tenant", "tenant_id"],
            "order_by": ["location__name"],
            "limit": 1,
        }
        first = succeeded(self, call_query(arguments, self.visible_user))
        continuations = [
            (request, succeeded(self, call_query(request, self.visible_user)))
            for request in (
                {**arguments, "fields": ["location__name"]},
                {"model": "dcim.Location", "fields": ["name"], "order_by": ["name"], "limit": 1},
            )
        ]
        with override_settings(PLUGINS_CONFIG=configured):
            for request, page in continuations:
                with self.subTest(continuation=request["model"]):
                    self.assertIsNotNone(page["next_cursor"])
                    failed(
                        self,
                        call_query({**request, "cursor": page["next_cursor"]}, self.visible_user),
                        "PermissionDenied",
                    )
            continued = succeeded(
                self,
                call_query({**arguments, "cursor": first["next_cursor"]}, self.visible_user),
            )
        self.assertEqual(
            continued["results"],
            [
                {
                    "name": "edge-2",
                    "location": {"id": str(self.data.beta.pk), "display": str(self.data.beta)},
                    "location_id": str(self.data.beta.pk),
                    "tenant": None,
                    "tenant_id": None,
                }
            ],
        )
        self.assertIsNone(continued["next_cursor"])
        for blocked in ([], configured["nautobot_agent"]["blocked_models"]):
            with self.settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": blocked}}):
                for selection in (
                    {},
                    {"filters": {"location__name__startswith": "Visible"}, "order_by": ["location"]},
                    {"exclude": {"location__name": "Hidden Gamma"}, "order_by": ["location__name"]},
                    {"where": {"compare": {"left": "location__name", "lookup": "exact", "right": "location__name"}}},
                ):
                    with self.subTest(blocked=blocked, selection=selection):
                        payload = succeeded(
                            self,
                            call_query({"model": "dcim.Device", "fields": ["name"], **selection}, self.visible_user),
                        )
                        self.assertEqual(sorted(row["name"] for row in payload["results"]), ["edge-1", "edge-2"])
                identities = succeeded(
                    self,
                    call_query(
                        {"model": "dcim.Device", "fields": ["location_id"], "distinct": True},
                        self.visible_user,
                    ),
                )
                self.assertEqual(
                    sorted(row["location_id"] for row in identities["results"]),
                    sorted([str(self.data.alpha.pk), str(self.data.beta.pk)]),
                )

    def test_blocked_manufacturer_does_not_change_native_device_type_identity(self):
        grant(self.visible_user, Device)
        configured = {"nautobot_agent": {"blocked_models": ["dcim.Manufacturer"]}}
        with override_settings(PLUGINS_CONFIG=configured):
            payload = succeeded(
                self,
                call_query(
                    {"model": "dcim.Device", "fields": ["device_type"], "order_by": ["pk"]},
                    self.visible_user,
                ),
            )
        self.assertEqual(
            payload["results"][0]["device_type"],
            {
                "id": str(self.data.device.device_type_id),
                "display": str(self.data.device.device_type.display),
            },
        )

    def test_related_projection_visibility(self):
        """Related projections are null without a target grant, briefs for terminal FKs, and never change root membership."""
        with self.subTest("related projection is null without a"):
            payload = succeeded(
                self,
                call_query(
                    {"model": "dcim.Location", "fields": ["name", "location_type__name"], "order_by": ["name"]},
                    self.visible_user,
                ),
            )
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                payload["results"],
                [
                    {"name": "Visible Alpha", "location_type__name": None},
                    {"name": "Visible Beta", "location_type__name": None},
                ],
            )
        with self.subTest("terminal foreign key projection returns brief"):
            grant(self.visible_user, Device)
            payload = succeeded(
                self,
                call_query(
                    {
                        "model": "dcim.Device",
                        "fields": ["device_type", "device_type_id", "location"],
                        "order_by": ["pk"],
                    },
                    self.visible_user,
                ),
            )
            self.assertEqual(
                payload["results"],
                [
                    {
                        "device_type": {
                            "id": str(self.data.device.device_type_id),
                            "display": str(self.data.device.device_type.display),
                        },
                        "device_type_id": str(self.data.device.device_type_id),
                        "location": {
                            "id": str(self.data.device.location_id),
                            "display": str(self.data.device.location.display),
                        },
                    }
                ],
            )
        with self.subTest("constrained two hop and transform gates"):
            self.enterContext(register_lookup(models.CharField, Upper))
            grant(self.visible_user, Device, constraints={"location__name__startswith": "Visible"})
            self.data.alpha.parent = self.data.beta
            self.data.alpha.save()

            visible = succeeded(
                self,
                call_query(
                    {
                        "model": "dcim.Device",
                        "fields": ["name", "location__name__upper", "location__parent__name"],
                    },
                    self.visible_user,
                ),
            )
            self.assertEqual(visible["count"], 1)
            self.assertEqual(
                visible["results"],
                [
                    {
                        "name": "edge-1",
                        "location__name__upper": "VISIBLE ALPHA",
                        "location__parent__name": "Visible Beta",
                    }
                ],
            )

            self.data.beta.name = "Hidden Beta"
            self.data.beta.save()
            hidden_hop = succeeded(
                self,
                call_query(
                    {
                        "model": "dcim.Device",
                        "fields": ["name", "location__parent__name"],
                    },
                    self.visible_user,
                ),
            )
            self.assertEqual(hidden_hop["count"], 1)
            self.assertEqual(hidden_hop["results"], [{"name": "edge-1", "location__parent__name": None}])
        with self.subTest("empty constraint on related model projects"):
            user = User.objects.create_user(username="empty-location-constraint")
            grant(user, Device)
            grant(user, Location, constraints={"name__in": []})
            payload = succeeded(
                self,
                call_query(
                    {"model": "dcim.Device", "fields": ["name", "location__name"]},
                    user,
                ),
            )
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["results"], [{"name": "edge-1", "location__name": None}])

    def test_predicates_require_view_permission_on_every_related_model(self):
        user = User.objects.create_user(username="predicate-device-viewer")
        grant(user, Device)
        cases = (
            ({"filters": {"location__name": self.data.alpha.name}}, "dcim.Location"),
            ({"exclude": {"location__name": self.data.alpha.name}}, "dcim.Location"),
            ({"where": {"not": {"lookups": {"location__name": self.data.alpha.name}}}}, "dcim.Location"),
            ({"filters": {"interfaces__name": self.data.interface.name}}, "dcim.Interface"),
        )
        for call in (call_query, call_trace):
            for args, target in cases:
                with self.subTest(tool=call.__name__, args=args):
                    error = failed(self, call({"model": "dcim.Device", **args}, user), "PermissionDenied")
                    self.assertIn(target, error["message"])
        grant(user, Location)
        for call in (call_query, call_trace):
            with self.subTest(tool=call.__name__, path="second hop"):
                error = failed(
                    self,
                    call(
                        {"model": "dcim.Device", "filters": {"location__location_type__name": self.data.campus.name}},
                        user,
                    ),
                    "PermissionDenied",
                )
                self.assertIn("dcim.LocationType", error["message"])
            with self.subTest(tool=call.__name__, path="permitted hop"):
                payload = succeeded(
                    self, call({"model": "dcim.Device", "filters": {"location__name": self.data.alpha.name}}, user)
                )
                self.assertEqual(payload["count"], 1)

    def test_row_limited_related_predicates_deny_fields_but_allow_identifiers(self):
        grant(self.visible_user, Device)
        grant(self.visible_user, Tenant, constraints={"name": "Public"})
        cases = (
            {"filters": {"tenant__description__icontains": "private"}},
            {"filters": {"location__description__icontains": "private"}},
            {"exclude": {"location__name": self.data.gamma.name}},
            {"where": {"lookups": {"location__name__startswith": "Hidden"}}},
            {"where": {"compare": {"left": "location__name", "lookup": "exact", "right": "name"}}},
        )
        for call in (call_query, call_trace):
            for args in cases:
                with self.subTest(tool=call.__name__, args=args):
                    failed(self, call({"model": "dcim.Device", **args}, self.visible_user), "PermissionDenied")
            with self.subTest(tool=call.__name__, path="self relation"):
                failed(
                    self,
                    call(
                        {"model": "dcim.Location", "filters": {"parent__description__icontains": "private"}},
                        self.visible_user,
                    ),
                    "PermissionDenied",
                )
            for path, value in (
                ("location", str(self.data.alpha.pk)),
                ("location_id", str(self.data.alpha.pk)),
                ("location__pk", str(self.data.alpha.pk)),
                ("location__pk__in", [str(self.data.alpha.pk)]),
                ("location__isnull", False),
            ):
                with self.subTest(tool=call.__name__, path=path):
                    payload = succeeded(
                        self, call({"model": "dcim.Device", "filters": {path: value}}, self.visible_user)
                    )
                    self.assertEqual(payload["count"], 1)
        failed(
            self,
            call_query(
                {
                    "model": "dcim.Device",
                    "mode": "aggregate",
                    "aggregates": {
                        "matching": {
                            "function": "Count",
                            "field": "pk",
                            "where": {"lookups": {"location__name": self.data.gamma.name}},
                        }
                    },
                },
                self.visible_user,
            ),
            "PermissionDenied",
        )

    def test_related_user_predicates_require_a_grant_not_a_restrict_manager(self):
        superuser = User.objects.create_superuser(username="rootuser")
        Token.objects.create(user=superuser, key=TOKEN_KEY)
        grant(self.visible_user, Token)
        for call in (call_query, call_trace):
            for username, expected in ((superuser.username, 1), ("missing-user", 0)):
                with self.subTest(tool=call.__name__, username=username):
                    payload = succeeded(
                        self,
                        call({"model": "users.Token", "filters": {"user__username": username}}, superuser),
                    )
                    self.assertEqual(payload["count"], expected)
            for path, value in (("user__username", superuser.username), ("user", str(superuser.pk))):
                with self.subTest(tool=call.__name__, denied_path=path):
                    payload = failed(
                        self,
                        call({"model": "users.Token", "filters": {path: value}}, self.visible_user),
                        "PermissionDenied",
                    )
                    self.assertIn("users.User", payload["message"])

    def test_unsupported_root_managers_fail_closed_even_for_a_superuser(self):
        superuser = User.objects.create_superuser(username="rootuser")
        for model in ("users.User", "contenttypes.ContentType"):
            for user in (superuser, self.visible_user):
                with self.subTest(model=model, user=user.username):
                    payload = failed(self, call_query({"model": model, "fields": ["pk"]}, user), "UnsupportedModel")
                    self.assertIn(model.split(".")[1], payload["message"])
                    self.assertNotIn("results", payload)

    def test_wildcard_exemption_does_not_authorize_sso_token_predicates(self):
        grant(self.visible_user, User)
        calls = restrict_spy(self, model=Token)
        with self.settings(EXEMPT_VIEW_PERMISSIONS=["*"]):
            for call in (call_query, call_trace):
                with self.subTest(tool=call.__name__):
                    payload = failed(
                        self,
                        call(
                            {
                                "model": "users.Token",
                                "filters": {"user__social_auth__extra_data__access_token__startswith": "secret"},
                            },
                            self.visible_user,
                        ),
                        "PermissionDenied",
                    )
                    self.assertIn("social_django.UserSocialAuth", payload["message"])
        self.assertEqual(calls, [])

    def test_untrusted_contexts_fail_closed_without_building_the_target_query(self):
        deleted = User.objects.create_user(username="deleted")
        deleted_pk = str(deleted.pk)
        deleted.delete()
        inactive = User.objects.create_user(username="inactive", is_active=False)
        grant(inactive, Location)
        contexts = {
            "missing": None,
            "wrong type": {"user_id": str(self.visible_user.pk)},
            "empty": AgentContext(
                user_id="", authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
            ),
            "malformed": AgentContext(
                user_id="not-a-uuid", authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
            ),
            # Intentionally invalid input: a non-string user_id must fail closed like every other malformed context.
            "non string": AgentContext(
                user_id=123,  # ty: ignore[invalid-argument-type]
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
            "unknown": AgentContext(
                user_id=str(uuid.uuid4()),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
            "deleted": AgentContext(
                user_id=deleted_pk, authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
            ),
            "inactive": AgentContext(
                user_id=str(inactive.pk),
                authority=AllowAllAuthority(),
                budget=RunBudget(tool_limit=100, model_limit=40),
            ),
        }
        calls = restrict_spy(self)

        for label, context in contexts.items():
            with self.subTest(case=label):
                message = call_query({"model": "dcim.Location", "fields": ["name"]}, context=context)
                payload = failed(self, message, "AuthenticationError")
                self.assertNotIn("Visible", message.content)
                self.assertNotIn("results", payload)

        self.assertEqual(calls, [])

    def query(self, **args):
        """Run a successful superuser Location query restricted to this test's fixture names."""
        filters = {"name__in": ALL_NAMES, **args.pop("filters", {})}
        return succeeded(self, call_query({"model": "dcim.Location", "filters": filters, **args}, self.superuser))

    def test_binary_projection_is_rejected_before_loading_attachments(self):
        calls = restrict_spy(self, model=FileAttachment)
        payload = failed(
            self,
            call_query({"model": "extras.FileAttachment", "fields": ["bytes"], "limit": 100}, self.superuser),
            "ValidationError",
        )
        self.assertIn("binary", payload["message"])
        self.assertEqual(calls, [])

    def test_truncation_uses_a_lookahead_row_at_below_exact_and_above_boundaries(self):
        cases = {1: (1, True), 2: (2, True), 3: (3, False), 4: (3, False), 50: (3, False), 100: (3, False)}
        for limit, (count, truncated) in cases.items():
            with self.subTest(limit=limit):
                payload = self.query(limit=limit, order_by=["name"], fields=["name"])
                self.assertEqual(
                    (payload["count"], payload["truncated"], len(payload["results"])), (count, truncated, count)
                )
        ordered = self.query(limit=2, order_by=["name"], fields=["name"])
        self.assertEqual([row["name"] for row in ordered["results"]], ["Hidden Gamma", "Visible Alpha"])

    def test_cursor_continuation_and_binding(self):
        """Cursors continue across page sizes and key orders and bind to the query and the principal."""
        with self.subTest("offset cursor continues with a different"):
            Location.objects.bulk_create(
                [
                    Location(name=f"Page {number:03d}", location_type=self.data.campus, status=self.data.active)
                    for number in range(7)
                ]
            )
            arguments = {"filters": {"name__startswith": "Page "}, "fields": ["name"], "order_by": ["name"]}
            first = succeeded(self, call_query({"model": "dcim.Location", "limit": 3, **arguments}, self.superuser))
            second = succeeded(
                self,
                call_query(
                    {"model": "dcim.Location", "limit": 2, "cursor": first["next_cursor"], **arguments}, self.superuser
                ),
            )
            third = succeeded(
                self,
                call_query(
                    {"model": "dcim.Location", "limit": 4, "cursor": second["next_cursor"], **arguments}, self.superuser
                ),
            )
            self.assertEqual(
                [row["name"] for row in first["results"] + second["results"] + third["results"]],
                [f"Page {number:03d}" for number in range(7)],
            )
            self.assertTrue(first["truncated"])
            self.assertTrue(second["truncated"])
            self.assertFalse(third["truncated"])
            self.assertIsNone(third["next_cursor"])
            wrong_query = call_query(
                {"model": "dcim.Location", "limit": 2, "cursor": first["next_cursor"], **arguments, "fields": ["pk"]},
                self.superuser,
            )
            failed(self, wrong_query, "InvalidCursor")
            token = first["next_cursor"]
            tampered = ("a" if token[0] != "a" else "b") + token[1:]
            failed(
                self,
                call_query({"model": "dcim.Location", "limit": 2, "cursor": tampered, **arguments}, self.superuser),
                "InvalidCursor",
            )
        with self.subTest("list cursor user binding and non"):
            first = self.query(limit=1, order_by=["name"], fields=["name"])
            other = User.objects.create_superuser(username="other-cursor-user")
            failed(
                self,
                call_query(
                    {
                        "model": "dcim.Location",
                        "filters": {"name__in": ALL_NAMES},
                        "limit": 1,
                        "order_by": ["name"],
                        "fields": ["name"],
                        "cursor": first["next_cursor"],
                    },
                    other,
                ),
                "InvalidCursor",
            )
        with self.subTest("distinct pk keysets continue in both"):
            for order, reverse in (("pk", False), ("-pk", True)):
                first = succeeded(
                    self,
                    call_query(
                        {
                            "model": "dcim.Location",
                            "filters": {"name__in": ALL_NAMES},
                            "fields": ["pk", "name"],
                            "distinct": True,
                            "order_by": [order],
                            "limit": 1,
                        },
                        self.superuser,
                    ),
                )
                failed(
                    self,
                    call_query(
                        {
                            "model": "dcim.Location",
                            "filters": {"name__in": ALL_NAMES},
                            "fields": ["pk", "name"],
                            "order_by": [order],
                            "distinct": False,
                            "limit": 1,
                            "cursor": first["next_cursor"],
                        },
                        self.superuser,
                    ),
                    "InvalidCursor",
                )
                second = succeeded(
                    self,
                    call_query(
                        {
                            "model": "dcim.Location",
                            "filters": {"name__in": ALL_NAMES},
                            "fields": ["pk", "name"],
                            "distinct": True,
                            "order_by": [order],
                            "limit": 2,
                            "cursor": first["next_cursor"],
                        },
                        self.superuser,
                    ),
                )
                rows = first["results"] + second["results"]
                expected = sorted(
                    (self.data.alpha, self.data.beta, self.data.gamma), key=lambda row: str(row.pk), reverse=reverse
                )
                self.assertEqual([row["pk"] for row in rows], [str(row.pk) for row in expected])
                self.assertEqual(len({row["pk"] for row in rows}), 3)
                self.assertFalse(second["truncated"])
                self.assertIsNone(second["next_cursor"])


class QueryEncoderTests(TestCase):
    """Scalar encoding and shared-root query boundaries."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.visible_user, cls.hidden_user = create_principals(User.objects.create_user(username="visibleuser"))
        cls.superuser = User.objects.create_superuser(username="rootuser")
        cls.holder = User.objects.create_user(username="token-holder")
        cls.token = Token.objects.create(user=cls.holder, key=TOKEN_KEY, description="holder")
        for model in (Token, ObjectPermission):
            grant(cls.holder, model)
        cls.conversation = create_internal_rows(cls.superuser)

    def assert_internal_denial(self, args, mode="list"):
        """Assert internal-storage denial without returning stored identifiers or data."""
        message = call_query({"mode": mode, **args}, self.superuser)
        payload = failed(self, message, "PermissionDenied")
        for leaked in ("results", "count", "exists"):
            self.assertNotIn(leaked, payload)
        self.assertNotIn(str(self.conversation.pk), message.content)

    def test_encoder_scalars_and_unsupported_types(self):
        """Scalars encode without enumerating networks and unsupported values report only their type."""
        with self.subTest("scalars encode without enumerating networks"):
            mac = EUI("00:1A:2B:3C:4D:5E")
            ident = uuid.uuid4()
            values = {
                "uuid": ident,
                "date": date(2026, 10, 5),
                "datetime": datetime(2026, 10, 5, 12, 30, tzinfo=UTC),
                "decimal": Decimal("1.50"),
                "zone": ZoneInfo("Europe/Paris"),
                "mac": mac,
                "address": IPAddress("2001:db8::1"),
                "v4_network": IPNetwork("10.0.0.0/8"),
                "v6_network": IPNetwork("2001:db8::/32"),
            }

            decoded = json.loads(dumps(values))

            self.assertEqual(
                decoded,
                {
                    "uuid": str(ident),
                    "date": "2026-10-05",
                    "datetime": "2026-10-05T12:30:00Z",
                    "decimal": 1.5,
                    "zone": "Europe/Paris",
                    "mac": str(mac),
                    "address": "2001:db8::1",
                    "v4_network": "10.0.0.0/8",
                    "v6_network": "2001:db8::/32",
                },
            )
        with self.subTest("unsupported values report only their type"):

            class Secret:  # pylint: disable=too-few-public-methods
                """Object whose representation must never be echoed."""

                def __repr__(self):
                    return "hunter2-secret"

            with self.assertRaises(TypeError) as raised:
                dumps({"value": Secret()})

            self.assertIn("Secret", str(raised.exception))
            self.assertNotIn("hunter2", str(raised.exception))

    def failure(self, args, error):
        """Run a failing superuser Location query and return its error payload."""
        return failed(self, call_query({"model": "dcim.Location", **args}, self.superuser), error)

    def test_invalid_cursor_shape_precedes_model_lookup_with_sanitized_guidance(self):
        observation = call_query(
            {"model": "dcim.MissingModel", "mode": "count", "cursor": "private-cursor-token"},
            self.superuser,
        )
        payload = failed(self, observation, "ValidationError")
        self.assertEqual(payload["message"], "Invalid tool arguments.")
        self.assertEqual([detail["loc"] for detail in payload["details"]], [["cursor"]])
        detail = payload["details"][0]
        self.assertEqual(detail["type"], "value_error")
        self.assertIn("guidance", detail)
        self.assertNotIn("private-cursor-token", observation.content)
        self.assertNotIn("MissingModel", observation.content)

    def assert_denied(self, model, args):
        """Assert a list query is denied locally and neither the payload nor the key leaks."""
        message = call_query({"model": model, **args}, self.holder)
        payload = failed(self, message, "PermissionDenied")
        self.assertNotIn(TOKEN_KEY, message.content)
        self.assertNotIn("results", payload)

    def test_sensitive_paths_denied(self):
        """Sensitive paths are denied directly, through relations, reverse joins, aliases, and declarations on other models."""
        self.enterContext(self.settings(STRICT_SENSITIVE_FIELDS=False))
        with self.subTest("direct related reverse and lookup paths"):
            self.assertEqual(list(Token.objects.filter(pk=self.token.pk).values_list("key", flat=True)), [TOKEN_KEY])
            cases = {
                ("users.Token", "field"): {"fields": ["pk", "key"]},
                ("users.Token", "order"): {"order_by": ["-key"]},
                ("users.Token", "filter lookup"): {"filters": {"key__startswith": TOKEN_KEY[:8]}},
                ("users.Token", "exact filter"): {"filters": {"key": TOKEN_KEY}},
                ("users.Token", "exclude"): {"exclude": {"key": TOKEN_KEY}},
                ("users.Token", "related field"): {"fields": ["user__password"]},
                ("users.Token", "related lookup"): {"filters": {"user__password__startswith": "!"}},
                ("users.Token", "related order"): {"order_by": ["-user__password"]},
                ("users.ObjectPermission", "forward many"): {"fields": ["users__password"]},
                ("users.ObjectPermission", "reverse chain"): {"fields": ["users__tokens__key"]},
                ("users.ObjectPermission", "reverse lookup"): {"filters": {"users__tokens__key__startswith": "01"}},
                ("users.ObjectPermission", "reverse order"): {"order_by": ["-users__tokens__key"]},
            }
            for (model, label), args in cases.items():
                with self.subTest(model=model, case=label):
                    self.assert_denied(model, args)
        with self.subTest("attname and pk aliases of declared"):
            with patch.object(Location, "sensitive_fields", ("location_type_id", "id"), create=True):
                cases = {
                    ("dcim.Location", "declared attname"): {"fields": ["location_type_id"]},
                    ("dcim.Location", "field name of a declared attname"): {"fields": ["location_type"]},
                    ("dcim.Location", "relation traversal through a declared attname"): {
                        "filters": {"location_type__name": "x"}
                    },
                    ("dcim.Location", "pk alias of a declared primary key"): {"fields": ["pk"]},
                    ("dcim.Location", "implicit pk"): {},
                    ("dcim.Location", "empty fields select pk"): {"fields": []},
                    ("dcim.Location", "pk lookup"): {"filters": {"pk__in": []}},
                    ("dcim.Location", "descending pk order"): {"order_by": ["-pk"]},
                    ("dcim.Location", "declared primary key name"): {"exclude": {"id": "x"}},
                    ("dcim.Device", "related pk alias"): {"fields": ["location__pk"]},
                    ("dcim.LocationType", "reverse attname"): {"fields": ["locations__location_type_id"]},
                }
                for (model, label), args in cases.items():
                    with self.subTest(model=model, case=label):
                        failed(self, call_query({"model": model, **args}, self.superuser), "PermissionDenied")
                allowed = succeeded(self, call_query({"model": "dcim.Location", "fields": ["name"]}, self.superuser))
                self.assertTrue(allowed["results"])
        with self.subTest("declarations added to other models are"):
            calls = restrict_spy(self)
            with patch.object(Location, "sensitive_fields", ("time_zone",), create=True):
                cases = {
                    ("dcim.Location", "field"): {"fields": ["time_zone"]},
                    ("dcim.Location", "lookup"): {"filters": {"time_zone__startswith": "America"}},
                    ("dcim.Location", "count lookup"): {
                        "mode": "count",
                        "filters": {"time_zone__startswith": "America"},
                    },
                    ("dcim.Location", "exists lookup"): {
                        "mode": "exists",
                        "filters": {"time_zone__startswith": "America"},
                    },
                    ("dcim.LocationType", "reverse"): {"fields": ["locations__time_zone"]},
                    ("dcim.LocationType", "reverse order"): {"order_by": ["-locations__time_zone"]},
                    ("dcim.Device", "forward then field"): {"fields": ["location__time_zone"]},
                    ("dcim.LocationType", "reverse then name lookup"): {"exclude": {"locations__time_zone__in": ["x"]}},
                }
                for (model, label), args in cases.items():
                    with self.subTest(model=model, case=label):
                        failed(self, call_query({"model": model, **args}, self.superuser), "PermissionDenied")
                self.assertEqual(calls, [])
                reverse = failed(
                    self,
                    call_query({"model": "dcim.LocationType", "fields": ["locations__name"]}, self.superuser),
                    "ValidationError",
                )
                self.assertIn("locations__name", reverse["message"])

    def test_internal_storage_denied_everywhere(self):
        """Internal models and reverse User paths are denied in filters, fields, ordering, and every mode."""
        with self.subTest("every direct internal model is denied"):
            for model in INTERNAL_MODELS:
                for mode in ("list", "count", "exists"):
                    with self.subTest(model=model, mode=mode):
                        self.assert_internal_denial({"model": model}, mode)
        with self.subTest("reverse user paths in filters and"):
            paths = (f"{REVERSE_PATH}__isnull", f"{REVERSE_PATH}__id", f"{REVERSE_PATH}__checkpoints__checkpoint_id")
            for path in paths:
                for clause in ("filters", "exclude"):
                    for mode in ("list", "count", "exists"):
                        with self.subTest(path=path, clause=clause, mode=mode):
                            self.assert_internal_denial({"model": "users.Token", clause: {path: None}}, mode)
        with self.subTest("reverse user paths in list fields"):
            for path in (REVERSE_PATH, f"{REVERSE_PATH}__id", f"{REVERSE_PATH}__isnull"):
                with self.subTest(path=path, clause="fields"):
                    self.assert_internal_denial({"model": "users.Token", "fields": ["pk", path]})
                for order in (path, f"-{path}"):
                    with self.subTest(path=path, clause="order_by", order=order):
                        self.assert_internal_denial({"model": "users.Token", "order_by": [order]})


class QueryAppModelTests(TestCase):
    """A generic App model resolves through the global registry and honors its real restricted default manager."""

    label = "nautobot_agent.AgentQueryProbe"

    @classmethod
    def setUpClass(cls):
        temporary_model_tables(cls, AgentQueryProbe)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        names = ("Probe Alpha", "Probe Beta", "Hidden Gamma")
        cls.rows = {name: AgentQueryProbe.objects.create(name=name) for name in names}
        cls.visible_user = User.objects.create_user(username="visibleprobe")
        grant(cls.visible_user, AgentQueryProbe, constraints={"name__startswith": "Probe"})
        cls.hidden_user = User.objects.create_user(username="hiddenprobe")
        grant(cls.hidden_user, AgentQueryProbe, constraints={"name__startswith": "Hidden"})
        cls.ungranted_user = User.objects.create_user(username="ungrantedprobe")

    def test_app_model_root_restriction(self):
        """App models expose only each principal's rows across list, count, and exists."""
        with self.subTest("list count and exists expose only"):
            cases = (
                (self.visible_user, ["Probe Alpha", "Probe Beta"]),
                (self.hidden_user, ["Hidden Gamma"]),
            )
            for user, names in cases:
                with self.subTest(user=user.username):
                    listed = succeeded(
                        self, call_query({"model": self.label, "fields": ["name"], "order_by": ["name"]}, user)
                    )
                    self.assertEqual(listed["model"], self.label)
                    self.assertEqual([row["name"] for row in listed["results"]], names)
                    count = succeeded(self, call_query({"model": self.label, "mode": "count"}, user))
                    self.assertEqual(count, {"model": self.label, "count": len(names)})
                    exists = succeeded(self, call_query({"model": self.label, "mode": "exists"}, user))
                    self.assertEqual(exists, {"model": self.label, "exists": bool(names)})
            for mode in ("list", "count", "exists"):
                with self.subTest(mode=mode):
                    denied = failed(
                        self,
                        call_query({"model": self.label, "mode": mode}, self.ungranted_user),
                        "PermissionDenied",
                    )
                    self.assertIn(self.label, denied["message"])
                    self.assertIn("nautobot_agent.view_agentqueryprobe", denied["message"])


class QueryToFieldProjectionTests(TestCase):
    """List projection gates and briefs use a to-field FK's stored value, not its target pk."""

    @classmethod
    def setUpClass(cls):
        temporary_model_tables(cls, QueryToFieldParent, QueryToFieldChild)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        visible = QueryToFieldParent.objects.create(slug="visible-parent", name="Visible Parent")
        hidden = QueryToFieldParent.objects.create(slug="hidden-parent", name="Hidden Parent")
        QueryToFieldChild.objects.create(name="child-visible", parent=visible)
        QueryToFieldChild.objects.create(name="child-hidden", parent=hidden)
        cls.viewer = User.objects.create_user(username="to-field-viewer")
        grant(cls.viewer, QueryToFieldChild)
        grant(cls.viewer, QueryToFieldParent, constraints={"slug": "visible-parent"})

    def test_non_pk_to_field_comparison_and_projection(self):
        """To-field FKs compare and project by their stored value with native visibility."""
        with self.subTest("comparison uses non pk fk storage"):
            for operand in ("parent", "parent_id"):
                with self.subTest(operand=operand):
                    payload = succeeded(
                        self,
                        call_query(
                            {
                                "model": "nautobot_agent.QueryToFieldChild",
                                "mode": "count",
                                "where": {"compare": {"left": operand, "lookup": "exact", "right": "parent__slug"}},
                            },
                            self.viewer,
                        ),
                    )
                    self.assertEqual(payload["count"], 2)
        with self.subTest("terminal fk brief and restricted two"):
            payload = succeeded(
                self,
                call_query(
                    {
                        "model": "nautobot_agent.QueryToFieldChild",
                        "fields": ["name", "parent", "parent__name"],
                        "order_by": ["name"],
                    },
                    self.viewer,
                ),
            )
            self.assertEqual(payload["count"], 2)
            self.assertEqual(
                payload["results"],
                [
                    {
                        "name": "child-hidden",
                        "parent": {
                            "id": str(QueryToFieldParent.objects.get(slug="hidden-parent").pk),
                            "display": "Hidden Parent",
                        },
                        "parent__name": None,
                    },
                    {
                        "name": "child-visible",
                        "parent": {
                            "id": str(QueryToFieldParent.objects.get(slug="visible-parent").pk),
                            "display": "Visible Parent",
                        },
                        "parent__name": "Visible Parent",
                    },
                ],
            )
            no_parent_grant = User.objects.create_user(username="query-to-field-no-parent-grant")
            grant(no_parent_grant, QueryToFieldChild)
            ungranted = succeeded(
                self,
                call_query(
                    {
                        "model": "nautobot_agent.QueryToFieldChild",
                        "fields": ["parent__name"],
                    },
                    no_parent_grant,
                ),
            )
            self.assertEqual(ungranted["count"], 2)
            self.assertEqual(ungranted["results"], [{"parent__name": None}, {"parent__name": None}])


class QueryDistinctAndContinuationTests(TestCase):
    """Distinct results, stable keysets, and response bounds share one joined fixture graph."""

    LOW = "00000000-0000-4000-8000-000000000001"
    HIGH = "ffffffff-ffff-4fff-8fff-ffffffffffff"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.superuser = User.objects.create_superuser(username="query-pagination-root")
        build_twin_locations(cls.data, cls.LOW, cls.HIGH)

    def run_query(self, model="dcim.Location", /, **args):
        """Invoke a flat query as the test's privileged principal."""
        return call_query({"model": model, **args}, self.superuser)

    def test_distinct_json_type_rejects_unsupported_backend_ordering(self):
        """Backends reporting JSON rather than JSONB cannot promise distinct continuation."""
        count = succeeded(
            self,
            self.run_query(mode="count", distinct=True, filters={"devices__name__startswith": "twin-"}),
        )
        self.assertEqual(count["count"], 2)
        with patch.object(models.JSONField, "db_type", return_value="json"):
            payload = failed(
                self,
                self.run_query(fields=["_custom_field_data"], distinct=True),
                "ValidationError",
            )
        self.assertEqual(payload["message"], "The projected value type cannot preserve distinct ordering.")

    def test_tied_ordering_continues_without_gaps(self):
        locations = [
            Location.objects.create(
                name=f"tie-{index}", facility=value, location_type=self.data.campus, status=self.data.active
            )
            for index, value in enumerate(("tie-a", "tie-a", "tie-b"))
        ]
        arguments = {
            "filters": {"pk__in": [str(location.pk) for location in locations]},
            "fields": ["pk", "name"],
            "order_by": ["-facility"],
            "limit": 1,
        }
        first = succeeded(self, self.run_query(**arguments))
        second = succeeded(self, self.run_query(**{**arguments, "cursor": first["next_cursor"], "limit": 2}))
        expected = [locations[2], *sorted(locations[:2], key=lambda row: str(row.pk))]
        self.assertEqual([row["pk"] for row in first["results"] + second["results"]], [str(row.pk) for row in expected])
        self.assertIsNone(second["next_cursor"])

    def test_hidden_related_values_are_distinct_null_tuples_and_rechecked_on_continuation(self):
        user = User.objects.create_user(username="page-hidden-values")
        grant(user, Location)
        permission = grant(user, LocationType, constraints={"name": self.data.campus.name})
        arguments = {
            "model": "dcim.Location",
            "filters": {"name__in": ALL_NAMES},
            "fields": ["location_type__name"],
            "distinct": True,
            "limit": 1,
        }
        first = succeeded(self, call_query(arguments, user))
        final = succeeded(self, call_query({**arguments, "cursor": first["next_cursor"]}, user))
        self.assertEqual(
            first["results"] + final["results"],
            [{"location_type__name": self.data.campus.name}, {"location_type__name": None}],
        )
        permission.delete()
        changed = succeeded(self, call_query({**arguments, "cursor": first["next_cursor"]}, user))
        self.assertEqual(changed["results"], [])
        self.assertIsNone(changed["next_cursor"])

    def test_volatile_and_raw_native_orderings_fail_instead_of_claiming_continuation(self):
        metadata = Location._meta  # ty: ignore[unresolved-attribute]  # Django adds model metadata.
        for ordering in (
            ("?",),
            (Random().asc(),),
            (Now().asc(),),
            (RawSQL("RANDOM()", []).asc(),),
            ("devices__name",),
        ):
            with (
                self.subTest(ordering=ordering),
                patch.object(metadata, "ordering", ordering),
            ):
                failed(
                    self, call_query({"model": "dcim.Location", "fields": ["name"]}, self.superuser), "ValidationError"
                )

    def call_with_limit(self, max_bytes, **arguments):
        """Invoke query_records with a deployment byte ceiling."""
        with query_byte_limit(max_bytes):
            return call_query({"model": "dcim.Location", **arguments}, self.superuser)

    def test_cursor_metadata_counts_toward_ceiling_and_narrowing_allows_retry(self):
        """Complete cursor metadata counts against the ceiling; smaller pages can be retried."""
        with self.subTest("cursor metadata is counted and oversize"):
            for location in (self.data.alpha, self.data.beta, self.data.gamma):
                Location.objects.filter(pk=location.pk).update(comments='é \\"' * 300)
            arguments = {
                "filters": {"name__in": ALL_NAMES},
                "fields": ["pk", "comments"],
                "order_by": ["pk"],
                "limit": 1,
            }
            first = succeeded(self, self.call_with_limit(1_000_000, **arguments))
            cursor = first["next_cursor"]
            self.assertTrue(cursor)
            first_size = len(dumps(first).encode("utf-8"))
            rejected_first = failed(self, self.call_with_limit(first_size - 1, **arguments), "PageSizeExceeded")
            self.assertEqual(rejected_first["actual_bytes"], first_size)
            self.assertNotIn("next_cursor", rejected_first)

            continuation = {**arguments, "limit": 2, "cursor": cursor}
            full_response = succeeded(self, self.call_with_limit(1_000_000, **continuation))
            smaller = {**continuation, "limit": 1}
            expected = succeeded(self, self.call_with_limit(1_000_000, **smaller))
            smaller_bytes = len(dumps(expected).encode("utf-8"))
            rejected = failed(self, self.call_with_limit(smaller_bytes, **continuation), "PageSizeExceeded")
            self.assertEqual(rejected["actual_bytes"], len(dumps(full_response).encode("utf-8")))
            self.assertNotIn("next_cursor", rejected)
            retried = succeeded(self, self.call_with_limit(smaller_bytes, **smaller))
            self.assertEqual(retried["results"], full_response["results"][:1])
            remaining = succeeded(
                self,
                self.call_with_limit(1_000_000, **{**smaller, "cursor": retried["next_cursor"]}),
            )
            self.assertEqual(remaining["results"], full_response["results"][1:])
            self.assertIsNone(remaining["next_cursor"])
