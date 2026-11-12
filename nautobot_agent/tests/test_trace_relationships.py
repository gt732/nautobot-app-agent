"""Focused real-ORM tests for explicitly selected relationship traces."""
# pylint: disable=too-many-lines

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from nautobot.apps.testing import TestCase
from nautobot.dcim.models import Device, Interface, Location, VirtualChassis
from nautobot.extras.models import ApprovalWorkflow
from nautobot.ipam.models import Namespace, Prefix
from nautobot.users.models import Token

from nautobot_agent.tests.support.data import (
    TOKEN_KEY,
    build_fixtures,
    call_trace,
    create_internal_rows,
    failed,
    grant,
    query_byte_limit,
    succeeded,
)

User = get_user_model()


class RelationshipTraceTests(TestCase):
    """Native permission restrictions apply at every explicit relationship hop."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.trace_user = User.objects.create_user(username="trace-user")
        for model in (Device, Location):
            grant(cls.trace_user, model)
        cls.superuser = User.objects.create_superuser(username="trace-root")
        cls.token = Token.objects.create(user=cls.superuser, key=TOKEN_KEY[:-1] + "8", description="holder")
        cls.conversation = create_internal_rows(cls.superuser)

    def create_device(self, name, location=None):
        """Create a Device shaped like the fixture device."""
        template = self.data.device
        return Device.objects.create(
            name=name,
            location=location or self.data.alpha,
            device_type=template.device_type,
            role=template.role,
            status=self.data.active,
        )

    def test_cold_hierarchy_labels_survive_projected_hidden_and_nested_identities(self):
        self.data.alpha.parent = self.data.beta
        self.data.alpha.save()
        user = User.objects.create_user(username="trace-cold-location")
        grant(user, Device)
        grant(user, Location, constraints={"name": "Visible Beta"})
        grant(user, Interface, constraints={"name": "cold-descendant"})

        device = self.data.device
        identity = {"id": str(self.data.alpha.pk), "display": "Visible Beta → Visible Alpha"}
        with (
            override_settings(LOCATION_NAME_AS_NATURAL_KEY=False),
            patch("nautobot.core.models.tree_queries.cache.get", return_value=""),
            patch("nautobot.core.models.tree_queries.cache.set"),
        ):
            request = {
                "model": "dcim.Device",
                "filters": {"pk": str(device.pk)},
                "fields": ["location"],
                "include": [{"relation": "location", "fields": ["name"]}],
            }
            payload = succeeded(self, call_trace(request, user))
            self.assertEqual(payload["results"][0]["fields"]["location"], identity)
            self.assertEqual(payload["results"][0]["relationships"]["location"], {**identity, "restricted": True})

            device.name = ""
            device.save(update_fields=["name"])
            Interface.objects.create(
                device=device,
                name="cold-descendant",
                type="1000base-t",
                status=self.data.active,
            )
            request = {
                "model": "dcim.Device",
                "filters": {"pk": str(device.pk)},
                "fields": ["pk", "name"],
                "include": [
                    {
                        "relation": "interfaces",
                        "fields": ["device"],
                        "include": [
                            {
                                "relation": "device",
                                "fields": ["location"],
                                "include": [{"relation": "location", "fields": ["name"]}],
                            }
                        ],
                    }
                ],
            }
            payload = succeeded(self, call_trace(request, user))

        row = payload["results"][0]
        self.assertEqual(
            row["relationships"]["interfaces"]["results"][0]["fields"]["device"],
            {
                "id": str(device.pk),
                "display": f"Test Acme Test Router ({device.pk})",
            },
        )
        nested_device = row["relationships"]["interfaces"]["results"][0]["relationships"]["device"]
        self.assertEqual(nested_device["fields"]["location"], identity)
        self.assertEqual(nested_device["relationships"]["location"], {**identity, "restricted": True})

    def test_missing_root_permission_and_local_projection_errors_are_public_observations(self):
        no_access = User.objects.create_user(username="trace-no-access")
        denied = failed(self, call_trace({"model": "dcim.Device"}, no_access), "PermissionDenied")
        self.assertIn("Device", denied["message"])
        failed(
            self,
            call_trace({"model": "dcim.Device", "fields": ["location__name"]}, self.trace_user),
            "ValidationError",
        )
        grant(self.trace_user, Prefix)
        self.data.active.content_types.add(ContentType.objects.get_for_model(Prefix))
        namespace = Namespace.objects.create(name="trace-properties")
        Prefix.objects.create(prefix="10.11.0.0/24", namespace=namespace, status=self.data.active)
        failed(self, call_trace({"model": "ipam.Prefix", "fields": ["prefix"]}, self.trace_user), "ValidationError")
        failed(self, call_trace({"model": "ipam.Prefix", "fields": ["locations"]}, self.trace_user), "ValidationError")
        for field in ("-name", "--name", "", "name__invalid", "not_a_field"):
            requests = (
                {"model": "dcim.Device", "fields": [field]},
                {"model": "dcim.Device", "include": [{"relation": "location", "fields": [field]}]},
            )
            for request in requests:
                with self.subTest(request=request):
                    failed(self, call_trace(request, self.trace_user), "ValidationError")

    def _assert_nested_m2m_occurrences(self, nested, prefixes, locations):
        """Check each nested attachment, including the location repeated under two roots."""
        by_prefix = {row["fields"]["pk"]: row for row in nested["results"]}
        first = by_prefix[str(prefixes[0].pk)]
        second = by_prefix[str(prefixes[1].pk)]
        shared_location_ids = [row["fields"]["pk"] for row in first["relationships"]["locations"]["results"]]
        self.assertEqual(shared_location_ids, sorted(str(location.pk) for location in locations[:3]))
        second_location_ids = [row["fields"]["pk"] for row in second["relationships"]["locations"]["results"]]
        self.assertEqual(second_location_ids, [str(locations[1].pk)])
        shared_prefixes = next(
            row["relationships"]["prefixes"]["results"]
            for row in first["relationships"]["locations"]["results"]
            if row["fields"]["pk"] == str(locations[1].pk)
        )
        repeated_prefixes = second["relationships"]["locations"]["results"][0]["relationships"]["prefixes"]["results"]
        expected_shared = sorted(str(prefixes[index].pk) for index in (0, 1, 3))
        self.assertEqual([row["fields"]["pk"] for row in shared_prefixes], expected_shared)
        self.assertEqual([row["fields"]["pk"] for row in repeated_prefixes], expected_shared)
        for row in first["relationships"]["locations"]["results"]:
            location_id = row["fields"]["pk"]
            expected = {
                str(locations[0].pk): [prefixes[0]],
                str(locations[1].pk): [prefixes[index] for index in (0, 1, 3)],
                str(locations[2].pk): [prefixes[index] for index in (0, 3)],
            }[location_id]
            self.assertEqual(
                [child["fields"]["pk"] for child in row["relationships"]["prefixes"]["results"]],
                sorted(str(prefix.pk) for prefix in expected),
            )

    def test_m2m_query_growth_and_visible_occurrence_pages(self):  # pylint: disable=too-many-locals
        user = User.objects.create_user(username="trace-m2m-growth")
        grant(user, Location, constraints={"name__startswith": "m2m-visible-"})
        self.data.active.content_types.add(ContentType.objects.get_for_model(Prefix))
        self.data.campus.content_types.add(ContentType.objects.get_for_model(Prefix))
        namespace = Namespace.objects.create(name="trace-m2m-growth")
        locations = [
            Location.objects.create(
                name=f"m2m-visible-{index}", location_type=self.data.campus, status=self.data.active
            )
            for index in range(4)
        ]
        hidden = Location.objects.create(name="m2m-hidden", location_type=self.data.campus, status=self.data.active)
        prefixes = [
            Prefix.objects.create(prefix=f"10.60.{index}.0/24", namespace=namespace, status=self.data.active)
            for index in range(4)
        ]
        prefixes[0].locations.add(locations[0], locations[1], hidden, locations[2])
        prefixes[1].locations.add(locations[1])
        prefixes[3].locations.add(locations[1], locations[2])
        hidden_prefix = Prefix.objects.create(prefix="10.61.0.0/24", namespace=namespace, status=self.data.active)
        hidden_prefix.locations.add(locations[1])
        grant(user, Prefix, constraints={"pk__in": [str(prefix.pk) for prefix in prefixes]})
        nested = succeeded(
            self,
            call_trace(
                {
                    "model": "ipam.Prefix",
                    "filters": {"pk__in": [str(prefixes[0].pk), str(prefixes[1].pk)]},
                    "fields": ["pk"],
                    "include": [
                        {
                            "relation": "locations",
                            "fields": ["pk"],
                            "include": [{"relation": "prefixes", "fields": ["pk"]}],
                        }
                    ],
                },
                user,
            ),
        )
        self._assert_nested_m2m_occurrences(nested, prefixes, locations)

        for root_model, target_model, relation, root_sets, targets_by_root in (
            (
                "ipam.Prefix",
                "dcim.Location",
                "locations",
                (prefixes[:1], prefixes, prefixes[2:3]),
                {
                    prefixes[0].pk: locations[:3],
                    prefixes[1].pk: [locations[1]],
                    prefixes[2].pk: [],
                    prefixes[3].pk: locations[1:3],
                },
            ),
            (
                "dcim.Location",
                "ipam.Prefix",
                "prefixes",
                (locations[:1], locations, locations[-1:]),
                {
                    locations[0].pk: [prefixes[0]],
                    locations[1].pk: [prefixes[0], prefixes[1], prefixes[3]],
                    locations[2].pk: [prefixes[0], prefixes[3]],
                    locations[3].pk: [],
                },
            ),
        ):
            with self.subTest(direction=relation):

                def query(selected, root_model=root_model, relation=relation):
                    return call_trace(
                        {
                            "model": root_model,
                            "filters": {"pk__in": [str(row.pk) for row in selected]},
                            "fields": ["pk"],
                            "include": [{"relation": relation, "fields": ["pk"], "limit": 2}],
                        },
                        user,
                    )

                succeeded(self, query(root_sets[0]))
                query_counts = []
                for roots in root_sets:
                    with CaptureQueriesContext(connection) as captured:
                        payload = succeeded(self, query(roots))
                    query_counts.append(len(captured))
                    pages = {row["fields"]["pk"]: row["relationships"][relation] for row in payload["results"]}
                    self.assertEqual(set(pages), {str(row.pk) for row in roots})
                    for root in roots:
                        expected_ids = sorted(str(row.pk) for row in targets_by_root[root.pk])
                        self.assertEqual(
                            pages[str(root.pk)],
                            {
                                "model": target_model,
                                "count": min(len(expected_ids), 2),
                                "truncated": len(expected_ids) > 2,
                                "results": [{"fields": {"pk": pk}, "relationships": {}} for pk in expected_ids[:2]],
                            },
                        )
                self.assertEqual(query_counts, [query_counts[0]] * 3)

    def test_include_visibility_follows_native_restriction(self):
        # Ungranted includes stay empty, even beneath empty roots.
        for include in (
            {"relation": "interfaces"},
            {"relation": "interfaces", "include": [{"relation": "ip_addresses"}]},
        ):
            with self.subTest(include=include):
                payload = succeeded(
                    self,
                    call_trace(
                        {"model": "dcim.Device", "filters": {"name": "no-match"}, "include": [include]},
                        self.trace_user,
                    ),
                )
                self.assertEqual(payload["results"], [])
        no_collection_grant = User.objects.create_user(username="trace-no-interface-grant")
        grant(no_collection_grant, Device)
        populated = succeeded(
            self, call_trace({"model": "dcim.Device", "include": [{"relation": "interfaces"}]}, no_collection_grant)
        )
        self.assertEqual(
            populated["results"][0]["relationships"]["interfaces"],
            {"model": "dcim.Interface", "count": 0, "truncated": False, "results": []},
        )

        # Constrained single grants yield a restricted brief; multiple collection grants union natively.
        user = User.objects.create_user(username="trace-constrained")
        grant(user, Device)
        grant(user, Location, constraints={"name": "Visible Beta"})
        grant(user, Interface, constraints={"name": "eth0"})
        grant(user, Interface)
        Interface.objects.create(
            device=self.data.device,
            name="eth1",
            type="1000base-t",
            status=self.data.active,
            mac_address="00:1a:2b:3c:4d:5f",
        )
        payload = succeeded(
            self,
            call_trace(
                {
                    "model": "dcim.Device",
                    "include": [
                        {"relation": "location", "fields": ["name"], "include": [{"relation": "devices"}]},
                        {"relation": "interfaces", "fields": ["name"]},
                    ],
                },
                user,
            ),
        )
        relationships = payload["results"][0]["relationships"]
        self.assertEqual(
            relationships["location"],
            {
                "id": str(self.data.device.location.pk),
                "display": str(self.data.device.location.display),
                "restricted": True,
            },
        )
        self.assertEqual(
            sorted(row["fields"]["name"] for row in relationships["interfaces"]["results"]), ["eth0", "eth1"]
        )

        # Group-granted and superuser principals go through the same restricted trace.
        group = Group.objects.create(name="trace-viewers")
        group_user = User.objects.create_user(username="trace-group-user")
        permission = grant(group_user, Device)
        permission.users.clear()
        permission.groups.add(group)
        group_user.groups.add(group)
        grouped = succeeded(self, call_trace({"model": "dcim.Device", "fields": ["name"]}, group_user))
        self.assertEqual([row["fields"]["name"] for row in grouped["results"]], ["edge-1"])
        superuser = User.objects.create_superuser(username="trace-superuser")
        privileged = succeeded(
            self,
            call_trace({"model": "dcim.Device", "include": [{"relation": "location", "fields": ["name"]}]}, superuser),
        )
        self.assertEqual(privileged["results"][0]["relationships"]["location"]["fields"]["name"], "Visible Alpha")

    def test_sensitive_fields_denied_in_roots_and_includes(self):
        Token.objects.create(user=self.trace_user, key=TOKEN_KEY, description="trace secret")
        grant(self.trace_user, Token)
        for filters in ({}, {"pk": "00000000-0000-0000-0000-000000000001"}):
            with self.subTest(filters=filters):
                message = call_trace({"model": "users.Token", "filters": filters, "fields": ["key"]}, self.trace_user)
                failed(self, message, "PermissionDenied")
                self.assertNotIn(TOKEN_KEY, message.content)

        superuser = User.objects.create_superuser(username="trace-include-admission")
        with patch.object(Token, "sensitive_fields", ("user",), create=True):
            failed(
                self,
                call_trace({"model": "users.Token", "include": [{"relation": "user"}]}, superuser),
                "PermissionDenied",
            )
        failed(
            self,
            call_trace({"model": "users.Token", "include": [{"relation": "user", "fields": ["password"]}]}, superuser),
            "UnsupportedModel",
        )

        # Empty include targets still reject declared sensitive fields.
        grant(self.trace_user, Interface)
        grant(self.trace_user, VirtualChassis)
        empty_device = self.create_device("trace-empty-device")
        empty_chassis = VirtualChassis.objects.create(name="trace-empty-chassis")
        empty_target_requests = (
            {
                "model": "dcim.Device",
                "filters": {"pk": str(empty_device.pk)},
                "include": [{"relation": "interfaces", "fields": ["name"]}],
            },
            {
                "model": "dcim.VirtualChassis",
                "filters": {"pk": str(empty_chassis.pk)},
                "include": [{"relation": "master", "fields": ["name"]}],
            },
        )
        with (
            patch.object(Interface, "sensitive_fields", ("name",), create=True),
            patch.object(Device, "sensitive_fields", ("name",), create=True),
        ):
            for request in empty_target_requests:
                with self.subTest(request=request):
                    failed(self, call_trace(request, self.trace_user), "PermissionDenied")

    def test_blocked_roots_and_explicit_includes_deny_before_target_inventory(self):
        user = User.objects.create_user(username="blocked-trace-targets")
        grant(user, Device)
        grant(user, Location)
        grant(user, Interface)
        superuser = self.superuser

        for principal in (user, superuser):
            for model, config in (
                ("dcim.Location", ["dcim.Location"]),
                ("dcim.Device", ["dcim.Device"]),
            ):
                with (
                    self.subTest(principal=principal.username, model=model),
                    override_settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": config}}),
                    CaptureQueriesContext(connection) as captured,
                ):
                    failed(self, call_trace({"model": model}, principal), "PermissionDenied")
                table = "dcim_location" if model == "dcim.Location" else "dcim_device"
                self.assertFalse(any(table in query["sql"].lower() for query in captured))

        includes = (
            # Single-valued include: default and explicit pk-only targets are both details.
            (
                {"model": "dcim.Device", "fields": ["pk"], "include": [{"relation": "location"}]},
                ["dcim.Location"],
                "dcim_location",
            ),
            (
                {"model": "dcim.Device", "fields": ["pk"], "include": [{"relation": "location", "fields": ["pk"]}]},
                ["dcim.Location"],
                "dcim_location",
            ),
            # Collection-valued includes follow the same target rule.
            (
                {"model": "dcim.Location", "fields": ["pk"], "include": [{"relation": "devices"}]},
                ["dcim.Device"],
                "dcim_device",
            ),
            (
                {
                    "model": "dcim.Location",
                    "fields": ["pk"],
                    "include": [{"relation": "devices", "fields": ["pk"]}],
                },
                ["dcim.Device"],
                "dcim_device",
            ),
        )
        for request, blocked, table in includes:
            with (
                self.subTest(request=request),
                override_settings(PLUGINS_CONFIG={"nautobot_agent": {"blocked_models": blocked}}),
                CaptureQueriesContext(connection) as captured,
            ):
                failed(self, call_trace(request, user), "PermissionDenied")
            self.assertFalse(any(table in query["sql"].lower() for query in captured))

    def test_trace_input_validation(self):
        grant(self.trace_user, ApprovalWorkflow)
        invalid_requests = (
            ({"model": "dcim.Device", "limit": 0}, "ValidationError"),
            ({"model": "dcim.Device", "limit": 6}, "ValidationError"),
            ({"model": "dcim.Device", "limit": True}, "ValidationError"),
            ({"model": "dcim.Device", "unexpected": "value"}, "ValidationError"),
            ({"model": "dcim.Device", "include": [{"relation": ""}]}, "UnsupportedRelationship"),
            ({"model": "dcim.Device", "include": [{"relation": "location__name"}]}, "UnsupportedRelationship"),
            (
                {"model": "dcim.Device", "include": [{"relation": "location"}, {"relation": "location"}]},
                "UnsupportedRelationship",
            ),
            ({"model": "dcim.Device", "include": [{"relation": "pk"}]}, "UnsupportedRelationship"),
            (
                {"model": "extras.ApprovalWorkflow", "include": [{"relation": "object_under_review"}]},
                "UnsupportedRelationship",
            ),
        )
        for request, error in invalid_requests:
            with self.subTest(request=request):
                failed(self, call_trace(request, self.trace_user), error)

        diagnostics = (
            ([{"relation": 7}], ["include", 0, "relation"], "string_type"),
            (
                [{"relation": "location", "where": {"lookups": {"name": "x"}}}],
                ["include", 0, "where"],
                "extra_forbidden",
            ),
        )
        for include, loc, error_type in diagnostics:
            with self.subTest(loc=loc):
                payload = failed(
                    self, call_trace({"model": "dcim.Device", "include": include}, self.trace_user), "ValidationError"
                )
                self.assertEqual((payload["details"][0]["loc"], payload["details"][0]["type"]), (loc, error_type))
                self.assertNotIn("results", payload)

    def test_depth_bound(self):
        grant(self.trace_user, Interface)
        five = {"relation": "devices", "fields": ["name"]}
        for relation in ("location", "device", "interfaces", "devices"):
            five = {"relation": relation, "fields": ["name"], "include": [five]}
        payload = succeeded(
            self,
            call_trace(
                {"model": "dcim.Location", "filters": {"pk": str(self.data.alpha.pk)}, "include": [five]},
                self.trace_user,
            ),
        )
        device = payload["results"][0]["relationships"]["devices"]["results"][0]
        returned_device = device["relationships"]["interfaces"]["results"][0]["relationships"]["device"]
        last_devices = returned_device["relationships"]["location"]["relationships"]["devices"]
        self.assertEqual(returned_device["fields"]["name"], "edge-1")
        self.assertEqual(last_devices["results"][0]["fields"]["name"], "edge-1")
        self.assertFalse(last_devices["truncated"])

        six = {"relation": "interfaces"}
        for relation in ("devices", "location", "device", "interfaces", "devices"):
            six = {"relation": relation, "include": [six]}
        failed(self, call_trace({"model": "dcim.Location", "include": [six]}, self.trace_user), "ValidationError")

    def test_child_bounds(self):
        exempt_user = User.objects.create_user(username="trace-exempt")
        with self.settings(EXEMPT_VIEW_PERMISSIONS=["dcim.device", "dcim.location", "dcim.devicetype"]):
            payload = succeeded(
                self,
                call_trace(
                    {
                        "model": "dcim.Device",
                        "include": [
                            {"relation": "location", "fields": ["name"]},
                            {"relation": "device_type", "fields": ["model"]},
                        ],
                    },
                    exempt_user,
                ),
            )
        relationships = payload["results"][0]["relationships"]
        self.assertEqual(relationships["location"]["fields"], {"name": "Visible Alpha"})
        self.assertEqual(relationships["device_type"]["fields"], {"model": "Test Router"})

        user = User.objects.create_user(username="trace-bounds")
        grant(user, Device)
        grant(user, Interface, constraints={"name": "not-visible"})
        grant(user, Interface, constraints={"name": "eth0"})
        for limit in (1, 20):
            with self.subTest(limit=limit):
                payload = succeeded(
                    self,
                    call_trace({"model": "dcim.Device", "include": [{"relation": "interfaces", "limit": limit}]}, user),
                )
                self.assertEqual(
                    payload["results"][0]["relationships"]["interfaces"],
                    {
                        "model": "dcim.Interface",
                        "count": 1,
                        "truncated": False,
                        "results": [{"fields": {"pk": str(self.data.interface.pk)}, "relationships": {}}],
                    },
                )
        for limit in (0, 21, True, 1.0, "1", None):
            with self.subTest(limit=limit):
                failed(
                    self,
                    call_trace({"model": "dcim.Device", "include": [{"relation": "interfaces", "limit": limit}]}, user),
                    "ValidationError",
                )

    def test_object_cap(self):
        """Shared singles and hidden markers count toward the 500-object cap; nulls and lookahead do not."""
        grant(self.trace_user, Interface)
        template = self.data.device
        request = {
            "model": "dcim.Location",
            "filters": {"name__in": ["Visible Alpha", "Visible Beta"]},
            "fields": ["name"],
            "include": [
                {
                    "relation": "devices",
                    "limit": 20,
                    "fields": ["name"],
                    "include": [
                        {"relation": "device_type", "fields": ["model"]},
                        {"relation": "location", "fields": ["name"]},
                        {"relation": "virtual_chassis", "fields": ["name"]},
                        {"relation": "interfaces", "fields": ["name"]},
                    ],
                }
            ],
        }
        for interface_count in (378, 379):
            with self.subTest(interface_count=interface_count):
                Device.objects.all().delete()
                # 21 alpha devices: the 21st is lookahead beyond limit=20 and is never traversed.
                alpha = [(number, self.data.alpha, "alpha") for number in range(1, 22)]
                beta = [(number, self.data.beta, "beta") for number in range(101, 121)]
                devices = Device.objects.bulk_create(
                    Device(
                        pk=f"00000000-0000-0000-0000-{number:012d}",
                        name=f"occurrence-{label}-{number:03d}",
                        location=location,
                        device_type=template.device_type,
                        role=template.role,
                        status=self.data.active,
                    )
                    for number, location, label in alpha + beta
                )
                returned, lookahead = devices[:20] + devices[21:], devices[20]
                quotas = [10] * (interface_count - 360) + [9] * (400 - interface_count)
                Interface.objects.bulk_create(
                    Interface(
                        device=device, name=f"{device.name}-eth{index}", type="1000base-t", status=self.data.active
                    )
                    for device, quota in [*zip(returned, quotas, strict=True), (lookahead, 20)]
                    for index in range(quota)
                )
                message = call_trace(request, self.trace_user)
                if interface_count == 379:
                    payload = failed(self, message, "TraceLimitExceeded")
                    self.assertNotIn("results", payload)
                    self.assertNotIn("Visible Alpha", message.content)
                    continue
                payload = succeeded(self, message)
                returned_rows = [
                    device for root in payload["results"] for device in root["relationships"]["devices"]["results"]
                ]
                interfaces = sum(len(row["relationships"]["interfaces"]["results"]) for row in returned_rows)
                # 2 roots + 40 devices x (device + hidden device_type + location) + 378 interfaces = 500.
                self.assertEqual(2 + len(returned_rows) * 3 + interfaces, 500)
                for row in returned_rows:
                    self.assertTrue(row["relationships"]["device_type"]["restricted"])
                    self.assertIsNone(row["relationships"]["virtual_chassis"])

    def test_oversized_nested_field_fails_but_narrowing_succeeds(self):
        ceiling = 1024
        self.data.device.comments = "related " + ("y" * 1400)
        self.data.device.save(update_fields=["comments"])
        nested_arguments = {
            "model": "dcim.Location",
            "filters": {"pk": str(self.data.alpha.pk)},
            "fields": ["name"],
            "include": [{"relation": "devices", "fields": ["comments"]}],
        }

        with query_byte_limit(ceiling):
            nested_error = failed(self, call_trace(nested_arguments, self.trace_user), "PageSizeExceeded")
            self.assertGreater(nested_error["actual_bytes"], ceiling)
            self.assertEqual(nested_error["max_bytes"], ceiling)
            self.assertNotIn("results", nested_error)

            narrowed = succeeded(
                self,
                call_trace(
                    {
                        **nested_arguments,
                        "include": [{"relation": "devices", "fields": ["name"]}],
                    },
                    self.trace_user,
                ),
            )
        self.assertEqual(
            narrowed,
            {
                "model": "dcim.Location",
                "count": 1,
                "truncated": False,
                "results": [
                    {
                        "fields": {"name": self.data.alpha.name},
                        "relationships": {
                            "devices": {
                                "model": "dcim.Device",
                                "count": 1,
                                "truncated": False,
                                "results": [{"fields": {"name": "edge-1"}, "relationships": {}}],
                            }
                        },
                    }
                ],
            },
        )

    def test_internal_storage_denied(self):
        users = User._default_manager  # pylint: disable=protected-access
        denied = [
            {"model": "users.User", "include": [{"relation": "nautobot_agent_conversations"}]},
            {
                "model": "users.Token",
                "include": [
                    {
                        "relation": "user",
                        "include": [
                            {"relation": "nautobot_agent_conversations", "include": [{"relation": "checkpoints"}]}
                        ],
                    }
                ],
            },
        ]
        # The installed User manager has no ``restrict``; give it an unrestricted queryset so the relation
        # validator, not an unsupported manager, is what stands between the include and the rows.
        with patch.object(users, "restrict", create=True, new=lambda _user, _action="view": users.all()):
            for args in denied:
                with self.subTest(args=args):
                    message = call_trace(args, self.superuser)
                    payload = failed(self, message, "PermissionDenied")
                    self.assertNotIn("results", payload)
                    self.assertNotIn(str(self.conversation.pk), message.content)

            ordinary = succeeded(
                self,
                call_trace(
                    {
                        "model": "users.Token",
                        "filters": {"user__username": "trace-root"},
                        "include": [{"relation": "user", "fields": ["username"]}],
                    },
                    self.superuser,
                ),
            )
        self.assertEqual(ordinary["results"][0]["fields"], {"pk": str(self.token.pk)})
        self.assertEqual(ordinary["results"][0]["relationships"]["user"]["fields"], {"username": "trace-root"})
