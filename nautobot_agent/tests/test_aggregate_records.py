"""Aggregate mode through the native query tool: Count, Sum, Avg, Min, Max, restriction, ordering, and validation."""
# pylint: disable=too-many-lines

from typing import ClassVar
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from nautobot.apps.testing import TestCase
from nautobot.dcim.models import Device, DeviceType, Location, LocationType, Manufacturer
from nautobot.extras.models import Status
from nautobot.users.models import Token

from nautobot_agent.tests.support.data import (
    REVERSE_PATH,
    TOKEN_KEY,
    build_assignment_fixtures,
    build_fixtures,
    build_twin_locations,
    call_query,
    create_principals,
    failed,
    grant,
    restrict_spy,
    succeeded,
)

User = get_user_model()

TOTAL = {"total": {"function": "Count", "field": "pk"}}
FUNCTIONS = ("Count", "Sum", "Avg", "Min", "Max")
ACTIVE_AGG_FILTERS = {"status__name": "Active", "location_type__name__startswith": "Agg "}
LOW_ID = "00000000-0000-4000-8000-000000000001"
HIGH_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
EMPTY_GROUP_PAGE = {"model": "dcim.Location", "count": 0, "truncated": False, "results": [], "next_cursor": None}


def aggregate(case, user, **args):
    """Run a successful Location aggregate query and return its parsed envelope."""
    return succeeded(case, call_query({"model": "dcim.Location", "mode": "aggregate", **args}, user))


def aggregate_model(case, model, user, **args):
    """Run a successful aggregate query over any model and return its parsed envelope."""
    return succeeded(case, call_query({"model": model, "mode": "aggregate", **args}, user))


def ranked(payload):
    """Return ``(category, count)`` pairs in result order for the ranking query shape."""
    return [(row["location_type__name"], row["total"]) for row in payload["results"]]


def device_types(case, user, **args):
    """Run a successful DeviceType aggregate query and return its parsed envelope."""
    return aggregate_model(case, "dcim.DeviceType", user, **args)


AGG_TYPES = {"manufacturer__name__startswith": "Agg "}


# Nautobot's public TestCase correctly inherits Django's transactional base.
class AggregateCoreTests(TestCase):  # pylint: disable=nb-incorrect-base-class
    """Exercise aggregate, paging, validation, and security rules on one shared fixture set."""

    @classmethod
    def build_ranking(cls):
        """Create 205 category roots: 155 Active, of which 105 are Visible, 50 Hidden, plus 50 Planned."""
        planned, _ = Status.objects.get_or_create(name="Planned", defaults={"color": "0000ff"})
        planned.content_types.add(ContentType.objects.get_for_model(Location))
        types = {name: LocationType.objects.create(name=f"Agg {name}") for name in ("Alpha", "Beta", "Gamma", "Delta")}
        population = (
            ("Alpha", "Visible", cls.data.active, 40),
            ("Alpha", "Hidden", cls.data.active, 20),
            ("Beta", "Visible", cls.data.active, 30),
            ("Beta", "Hidden", cls.data.active, 15),
            ("Gamma", "Visible", cls.data.active, 30),
            ("Gamma", "Hidden", cls.data.active, 15),
            ("Delta", "Visible", cls.data.active, 5),
            ("Delta", "Visible", planned, 50),
        )
        Location.objects.bulk_create(
            [
                Location(
                    name=f"{prefix} Agg {category} {status.name} {n:03d}",
                    location_type=types[category],
                    status=status,
                )
                for category, prefix, status, count in population
                for n in range(count)
            ]
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_fixtures()
        cls.superuser = User.objects.create_superuser(username="rootuser")
        cls.build_ranking()
        cls.visible_user, cls.hidden_user = create_principals(User.objects.create_user(username="visibleuser"))
        cls.ranking_visible_user = User.objects.create_user(username="ranking-visible")
        cls.ranking_hidden_user = User.objects.create_user(username="ranking-hidden")
        grant(cls.ranking_visible_user, Location, constraints={"name__startswith": "Visible"})
        grant(cls.ranking_hidden_user, Location, constraints={"name__startswith": "Hidden"})
        for user in (cls.ranking_visible_user, cls.ranking_hidden_user):
            grant(user, LocationType)
            grant(user, Status)
        for number in range(105):
            Location.objects.create(
                name=f"Page Group {number:03}",
                location_type=cls.data.campus,
                status=cls.data.active,
            )
        build_twin_locations(cls.data, LOW_ID, HIGH_ID)
        heights = {
            "Agg Acme": {"a-one": 1, "a-two": 1, "a-three": 1, "a-big": 5},
            "Agg Bolt": {"b-two": 2, "b-four": 4, "b-six-1": 6, "b-six-2": 6},
        }
        for name, models in heights.items():
            manufacturer = Manufacturer.objects.create(name=name)
            for model, u_height in models.items():
                DeviceType.objects.create(manufacturer=manufacturer, model=model, u_height=u_height)
        Token.objects.create(user=cls.superuser, key=TOKEN_KEY, description="holder")

    def test_count_semantics_after_filters(self):
        payload = aggregate(
            self,
            self.ranking_visible_user,
            filters={"name__startswith": "Visible Agg"},
            group_by=["status__name"],
            aggregates={
                "roots": {"function": "Count", "field": "pk"},
                "types": {"function": "Count", "field": "location_type", "distinct": True},
                "typed": {"function": "Count", "field": "location_type_id"},
            },
        )
        self.assertEqual(
            payload["results"],
            [
                {"status__name": "Active", "roots": 105, "types": 4, "typed": 105},
                {"status__name": "Planned", "roots": 50, "types": 1, "typed": 50},
            ],
        )
        filtered = aggregate(
            self,
            self.ranking_visible_user,
            filters={"location_type__name__startswith": "Agg ", "parent__isnull": True, "status__name": "Active"},
            exclude={"location_type__name": "Agg Alpha"},
            group_by=["location_type__name"],
            aggregates=TOTAL,
            order_by=["-total"],
        )
        self.assertEqual(ranked(filtered), [("Agg Beta", 30), ("Agg Gamma", 30), ("Agg Delta", 5)])

    def query(self, **args):
        """Query only the isolated page-group population with optional shape overrides."""
        query = {"filters": {"name__startswith": "Page Group "}, "group_by": ["name"], "aggregates": TOTAL, **args}
        return aggregate(self, self.superuser, **query)

    def test_cursor_binding(self):
        local = {"function": "Count", "field": "pk", "where": {"lookups": {"name__endswith": "0"}}}
        for label, original, changes in (
            ("filters", {}, {"filters": {"name__startswith": "Page Group 1"}}),
            ("aggregates", {}, {"aggregates": {"other": TOTAL["total"]}}),
            ("ordering", {}, {"order_by": ["-total"]}),
            (
                "local condition",
                {"aggregates": {"total": local}},
                {"aggregates": {"total": {**local, "where": {"lookups": {"name__endswith": "1"}}}}},
            ),
        ):
            with self.subTest(changed=label):
                token = self.query(limit=2, **original)["next_cursor"]
                self.assertIsNotNone(token)
                query = {"filters": {"name__startswith": "Page Group "}, "group_by": ["name"], "aggregates": TOTAL}
                request = {"model": "dcim.Location", "mode": "aggregate", **query, **changes, "cursor": token}
                failed(self, call_query(request, self.superuser), "InvalidCursor")

    def test_terminal_fk_briefs_and_intermediate_numeric_gates_for_related_access(self):
        Location.objects.filter(pk=LOW_ID).update(latitude=1)
        Location.objects.filter(pk=HIGH_ID).update(latitude=3)
        no_targets = User.objects.create_user(username="aggregate-no-targets")
        empty_targets = User.objects.create_user(username="aggregate-empty-targets")
        visible_targets = User.objects.create_user(username="aggregate-visible-targets")
        for user in (no_targets, empty_targets, visible_targets):
            grant(user, Device)
        grant(empty_targets, Location, constraints={"name__in": []})
        grant(visible_targets, Location, constraints={"name": "Twin"})
        query = {
            "filters": {"name__startswith": "twin-"},
            "group_by": ["location__name"],
            "aggregates": {
                "roots": {"function": "Count", "field": "pk"},
                "stored_fk": {"function": "Count", "field": "location"},
                "visible_numeric": {"function": "Count", "field": "location__latitude"},
                "total_latitude": {"function": "Sum", "field": "location__latitude"},
                "average_latitude": {"function": "Avg", "field": "location__latitude"},
                "minimum_latitude": {"function": "Min", "field": "location__latitude"},
                "maximum_latitude": {"function": "Max", "field": "location__latitude"},
            },
        }
        masked = {
            "location__name": None,
            "roots": 4,
            "stored_fk": 4,
            "visible_numeric": 0,
            "total_latitude": None,
            "average_latitude": None,
            "minimum_latitude": None,
            "maximum_latitude": None,
        }
        visible = {
            "location__name": "Twin",
            "roots": 4,
            "stored_fk": 4,
            "visible_numeric": 4,
            "total_latitude": 8,
            "average_latitude": 2,
            "minimum_latitude": 1,
            "maximum_latitude": 3,
        }
        for user, expected in ((no_targets, masked), (empty_targets, masked), (visible_targets, visible)):
            with self.subTest(user=user.username):
                self.assertEqual(aggregate_model(self, "dcim.Device", user, **query)["results"], [expected])
        terminal_fk = aggregate_model(
            self,
            "dcim.Device",
            no_targets,
            filters={"name__startswith": "twin-"},
            group_by=["location"],
            aggregates=TOTAL,
        )
        self.assertEqual([row["location"]["id"] for row in terminal_fk["results"]], [LOW_ID, HIGH_ID])
        self.assertEqual(terminal_fk["results"][0]["location"]["display"], Location.objects.get(pk=LOW_ID).display)

    def test_blocked_detail_outputs_deny_but_parent_fk_summaries_remain(self):
        configured = {"nautobot_agent": {"blocked_models": ["dcim.Location"]}}
        with override_settings(PLUGINS_CONFIG=configured):
            for output in (
                {"group_by": ["location__name"], "aggregates": TOTAL},
                {"aggregates": {"names": {"function": "Count", "field": "location__name"}}},
            ):
                with self.subTest(output=output):
                    failed(
                        self,
                        call_query(
                            {"model": "dcim.Device", "mode": "aggregate", **output},
                            self.superuser,
                        ),
                        "PermissionDenied",
                    )
            parent_summary = aggregate_model(
                self,
                "dcim.Device",
                self.superuser,
                filters={"name__startswith": "twin-"},
                group_by=["location"],
                order_by=["location"],
                aggregates={
                    "total": {
                        "function": "Count",
                        "field": "pk",
                        "where": {"lookups": {"location__name": "Twin"}},
                    },
                    "stored": {"function": "Count", "field": "location_id"},
                },
            )
        self.assertEqual(
            [(row["location"]["id"], row["total"], row["stored"]) for row in parent_summary["results"]],
            [(LOW_ID, 2, 2), (HIGH_ID, 2, 2)],
        )

    def setUp(self):
        super().setUp()
        self.calls = restrict_spy(self)

    def rejected(self, args, error="ValidationError"):
        """Return the failed payload for one Location request."""
        return failed(self, call_query({"model": "dcim.Location", **args}, self.superuser), error)

    def schema_locations(self, args):
        """Return public validation locations for a malformed request."""
        return sorted(map(str, (detail["loc"] for detail in self.rejected(args)["details"])))

    def test_request_combinations_are_rejected_by_the_input_schema(self):
        agg = {"mode": "aggregate"}
        cases = {
            "aggregate mode requires aggregates": (agg, [["aggregates"]]),
            "empty aggregate specs": ({**agg, "aggregates": {}}, [["aggregates"]]),
            "aggregate-only options rejected by list mode": ({"aggregates": TOTAL}, [["aggregates"]]),
            "list-only options rejected by aggregate mode": (
                {**agg, "aggregates": TOTAL, "fields": ["name"], "distinct": True},
                [["fields"], ["distinct"]],
            ),
            "summary ordering": ({**agg, "aggregates": TOTAL, "order_by": ["total"]}, [["order_by"]]),
            "duplicate groups": ({**agg, "aggregates": TOTAL, "group_by": ["name", "name"]}, [["group_by"]]),
            "invalid group type": ({**agg, "aggregates": TOTAL, "group_by": [5]}, [["group_by", 0]]),
            "unknown mode": ({"mode": "aggregates"}, [["mode"]]),
            "group alias collision": (
                {**agg, "aggregates": {"name": TOTAL["total"]}, "group_by": ["name"]},
                [["aggregates"]],
            ),
            "unknown spec key": (
                {**agg, "aggregates": {"total": {**TOTAL["total"], "extra": 1}}},
                [["aggregates", "total", "extra"]],
            ),
            "invalid function": (
                {**agg, "aggregates": {"total": {"function": "count", "field": "pk"}}},
                [["aggregates", "total", "function"]],
            ),
            "missing field": (
                {**agg, "aggregates": {"total": {"function": "Count"}}},
                [["aggregates", "total", "field"]],
            ),
            "non-boolean distinct": (
                {**agg, "aggregates": {"total": {"function": "Count", "field": "pk", "distinct": 1}}},
                [["aggregates", "total", "distinct"]],
            ),
        }
        having = {"lookups": {"total__gte": 1}}
        cases.update(
            {
                label: (args, expected)
                for label, args, expected in (
                    (
                        "empty grouping rejects having",
                        {"mode": "aggregate", "group_by": [], "aggregates": TOTAL, "having": having},
                        [["having"]],
                    ),
                    ("list mode rejects having", {"mode": "list", "having": {"lookups": {"name": "x"}}}, [["having"]]),
                )
            }
        )
        cases["trailing underscore alias in having"] = (
            {
                "mode": "aggregate",
                "group_by": ["location_type__name"],
                "aggregates": {"total_": TOTAL["total"]},
                "having": {"lookups": {"total___gte": 1}},
            },
            [["aggregates"]],
        )
        for label, (args, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.schema_locations(args), sorted(map(str, expected)))
        self.assertEqual(self.calls, [])

    def test_alias_path_and_ordering_errors(self):
        aliases = ("", "1total", "_total", "to-tal", "a__b", "pk", "location_type_id")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.rejected({"mode": "aggregate", "aggregates": {alias: TOTAL["total"]}})
        self.rejected({"mode": "aggregate", "aggregates": {"first": TOTAL["total"], "pk": TOTAL["total"]}})

        secret = call_query(
            {
                "model": "dcim.Location",
                "mode": "aggregate",
                "aggregates": {"total": {"function": "Count", "field": "pk", "distinct": "hunter2-secret"}},
            },
            self.superuser,
        )
        failed(self, secret, "ValidationError")
        self.assertNotIn("hunter2-secret", secret.content)

        bad_paths = ("devices", "parent__devices", "name__lower", "no_such_field", "")
        for path in bad_paths:
            specs = (
                {"group_by": [path], "aggregates": TOTAL},
                {"aggregates": {"total": {"function": "Count", "field": path}}},
            )
            for args in specs:
                with self.subTest(path=path, args=args):
                    self.rejected({"mode": "aggregate", **args})

        for term in ("name", "?", "--total", "-", "total__x", "status__name", ""):
            with self.subTest(term=term):
                self.rejected(
                    {"mode": "aggregate", "group_by": ["location_type__name"], "aggregates": TOTAL, "order_by": [term]}
                )
        self.assertEqual(self.calls, [])

    def test_filter_errors_keep_categories(self):
        for clause in ("filters", "exclude"):
            for key in ("devices__name", "children__isnull", "status__content_types__app_label"):
                with self.subTest(clause=clause, key=key):
                    self.rejected({"mode": "aggregate", "aggregates": TOTAL, clause: {key: None}})
        self.assertEqual(self.calls, [])

        allowed = {
            "location_type__name__startswith": "Test",
            "name__in": ["Visible Alpha", "Visible Beta", "Hidden Gamma"],
            "status__name__in": ["Active"],
            "parent__isnull": True,
            "name__regex": ".*",
            "pk__isnull": False,
        }
        self.assertEqual(aggregate(self, self.superuser, filters=allowed, aggregates=TOTAL)["results"], [{"total": 3}])
        for filters, error in (
            ({"no_such_field": 1}, "FieldError"),
            ({"name__nonsense": 1}, "FieldError"),
            ({"pk": "not-a-uuid"}, "ValidationError"),
        ):
            with self.subTest(filters=filters):
                self.rejected({"mode": "aggregate", "aggregates": TOTAL, "filters": filters}, error)

    def test_operation_type_validation(self):
        calls = restrict_spy(self, DeviceType)
        numeric = ("model", "created", "pk", "manufacturer", "manufacturer_id", "is_full_depth", "manufacturer__name")
        ordered = ("pk", "id", "manufacturer", "manufacturer_id", "is_full_depth", "manufacturer__id")
        rejected = [
            *({"function": function, "field": field} for function in ("Sum", "Avg") for field in numeric),
            *({"function": function, "field": field} for function in ("Min", "Max") for field in ordered),
            {"function": "Min", "field": "u_height", "distinct": True},
            {"function": "Max", "field": "u_height", "distinct": True},
        ]
        for spec in rejected:
            with self.subTest(spec=spec):
                failed(
                    self,
                    call_query(
                        {
                            "model": "dcim.DeviceType",
                            "mode": "aggregate",
                            "aggregates": {"total": TOTAL["total"], "value": spec},
                        },
                        self.superuser,
                    ),
                    "ValidationError",
                )
        self.assertEqual(calls, [])
        for function, expected in (("Min", 1), ("Max", 6)):
            explicit = {"value": {"function": function, "field": "u_height", "distinct": False}}
            payload = device_types(self, self.superuser, filters=AGG_TYPES, aggregates=explicit)
            self.assertEqual(payload["results"], [{"value": expected}])

    OPERATIONS: ClassVar[dict] = {
        "total": {"function": "Count", "field": "latitude"},
        "total_distinct": {"function": "Count", "field": "latitude", "distinct": True},
        "sum_lat": {"function": "Sum", "field": "latitude"},
        "sum_lat_distinct": {"function": "Sum", "field": "latitude", "distinct": True},
        "avg_lat": {"function": "Avg", "field": "latitude"},
        "avg_lat_distinct": {"function": "Avg", "field": "latitude", "distinct": True},
        "min_lat": {"function": "Min", "field": "latitude"},
        "max_lat": {"function": "Max", "field": "latitude"},
    }
    NULL_ROW: ClassVar[dict] = {**dict.fromkeys(OPERATIONS), "total": 0, "total_distinct": 0}
    TEST_TYPES: ClassVar[dict] = {
        "location_type__name__startswith": "Test",
        "name__in": ["Visible Alpha", "Visible Beta", "Hidden Gamma"],
    }

    def test_operations_cover_only_objects_visible_to_the_principal(self):
        args = {
            "filters": {"name__in": ["Visible Alpha", "Hidden Gamma"]},
            "aggregates": {
                "total": self.OPERATIONS["total"],
                "sum_lat": self.OPERATIONS["sum_lat"],
                "first_name": {"function": "Min", "field": "name"},
                "last_name": {"function": "Max", "field": "name"},
            },
        }
        cases = {
            "superuser": (self.superuser, 1, 41.8781, "Hidden Gamma", "Visible Alpha"),
            "visible": (self.visible_user, 1, 41.8781, "Visible Alpha", "Visible Alpha"),
            "hidden": (self.hidden_user, 0, None, "Hidden Gamma", "Hidden Gamma"),
        }
        for label, (user, total, latitude_sum, first, last) in cases.items():
            with self.subTest(principal=label):
                payload = aggregate(self, user, **args)
                self.assertEqual(
                    payload["results"],
                    [{"total": total, "sum_lat": latitude_sum, "first_name": first, "last_name": last}],
                )

        for user, total, first_name in ((self.visible_user, 2, "Visible Alpha"), (self.hidden_user, 1, "Hidden Gamma")):
            with self.subTest(suppressed_category_principal=user.username):
                grouped = aggregate(
                    self,
                    user,
                    filters={"name__in": ["Visible Alpha", "Visible Beta", "Hidden Gamma"]},
                    group_by=["location_type__name"],
                    aggregates={"total": TOTAL["total"], "first_name": {"function": "Min", "field": "name"}},
                )
                self.assertEqual(
                    grouped["results"], [{"location_type__name": None, "total": total, "first_name": first_name}]
                )

    def test_user_groups_and_aggregates_are_masked_without_user_grant(self):
        owner = User.objects.create_user(username="aggregate-owner")
        hidden_owner = User.objects.create_user(username="aggregate-hidden-owner")
        Token.objects.create(user=owner, key="aggregate-owner-token", description="owner")
        Token.objects.create(user=hidden_owner, key="aggregate-hidden-token", description="hidden")
        viewer = User.objects.create_user(username="aggregate-viewer")
        grant(viewer, Token)
        arguments = {
            "group_by": ["user__username"],
            "aggregates": {
                "rows": {"function": "Count", "field": "pk"},
                "named": {"function": "Count", "field": "user__username"},
                "first_username": {"function": "Min", "field": "user__username"},
            },
        }

        token_filters = {"description__in": ["owner", "hidden"]}
        masked = aggregate_model(self, "users.Token", viewer, filters=token_filters, **arguments)
        self.assertEqual(masked["results"], [{"user__username": None, "rows": 2, "named": 0, "first_username": None}])

        grant(viewer, User, constraints={"username": owner.username})
        admitted = aggregate_model(self, "users.Token", viewer, filters=token_filters, **arguments)
        self.assertEqual(
            {row["user__username"]: row for row in admitted["results"]},
            {
                None: {"user__username": None, "rows": 1, "named": 0, "first_username": None},
                owner.username: {
                    "user__username": owner.username,
                    "rows": 1,
                    "named": 1,
                    "first_username": owner.username,
                },
            },
        )

    def test_sensitive_and_internal_paths_denied_in_every_position(self):
        self.enterContext(self.settings(STRICT_SENSITIVE_FIELDS=False))

        def positions(path, value=None):
            return {
                "group key": {"group_by": [path], "aggregates": TOTAL},
                "aggregate input": {"aggregates": {"total": {"function": "Count", "field": path}}},
                "aggregate-local predicate": {
                    "aggregates": {
                        "total": {"function": "Count", "field": "description", "where": {"lookups": {path: value}}}
                    }
                },
            }

        token_paths = (
            "key",
            "user__password",
            REVERSE_PATH,
            f"{REVERSE_PATH}__checkpoints__checkpoint_id",
        )
        for path in token_paths:
            for label, args in positions(path, TOKEN_KEY).items():
                with self.subTest(path=path, case=label):
                    message = call_query({"model": "users.Token", "mode": "aggregate", **args}, self.superuser)
                    payload = failed(self, message, "PermissionDenied")
                    self.assertNotIn(TOKEN_KEY, message.content)
                    self.assertNotIn("results", payload)
        allowed = aggregate_model(self, "users.Token", self.superuser, group_by=["description"], aggregates=TOTAL)
        self.assertEqual(allowed["results"], [{"description": "holder", "total": 1}])

        count_name = {"total": {"function": "Count", "field": "name"}}
        with patch.object(Location, "sensitive_fields", ("location_type_id", "id"), create=True):
            for label, args in {
                "declared attname group": {"group_by": ["location_type_id"], "aggregates": count_name},
                "field name of a declared attname": {"group_by": ["location_type"], "aggregates": count_name},
                "pk alias as input": {"aggregates": TOTAL},
                "related pk alias": {"group_by": ["parent__pk"], "aggregates": count_name},
            }.items():
                with self.subTest(sensitive=label):
                    message = call_query({"model": "dcim.Location", "mode": "aggregate", **args}, self.superuser)
                    failed(self, message, "PermissionDenied")
            allowed = aggregate(
                self, self.superuser, filters={"name__in": ["Visible Alpha", "Visible Beta"]}, aggregates=count_name
            )
            self.assertEqual(allowed["results"], [{"total": 2}])


class AggregateBooleanConditionTests(TestCase):
    """Aggregate predicates preserve complete-population and relation safety rules."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_assignment_fixtures(build_fixtures())
        cls.superuser = User.objects.create_superuser(username="boolean-aggregate")

    def interfaces(self, aggregates=None, **args):
        """Aggregate selected assignment interfaces."""
        return call_query(
            {
                "model": "dcim.Interface",
                "mode": "aggregate",
                "aggregates": aggregates or {"assigned_vids": {"function": "Count", "field": "untagged_vlan__vid"}},
                **args,
            },
            self.superuser,
        )

    def test_boolean_conditions_exact_values_and_denied_paths(self):
        vid = {
            "and": [
                {"lookups": {"status__name": "Active"}},
                {"or": [{"lookups": {"untagged_vlan__vid": 3101}}, {"lookups": {"untagged_vlan__vid": 3104}}]},
            ]
        }
        compared = {
            "assigned_vids": {
                "function": "Count",
                "field": "untagged_vlan__vid",
                "where": {
                    "compare": {"left": "untagged_vlan__vid", "lookup": "lt", "right": "device__device_type__u_height"}
                },
            }
        }
        for label, args, expected in (
            ("scalar and forward path", {"where": vid}, [{"assigned_vids": 3}]),
            ("single lookup", {"where": {"lookups": {"untagged_vlan__vid": 3101}}}, [{"assigned_vids": 2}]),
            ("local comparison", {"aggregates": compared}, [{"assigned_vids": 0}]),
        ):
            with self.subTest(admitted=label):
                payload = succeeded(self, self.interfaces(**args))
                self.assertEqual((payload["results"], payload["count"]), (expected, 1))

        def local(where):
            return {"assigned_vids": {"function": "Count", "field": "untagged_vlan__vid", "where": where}}

        for where in (
            {"and": [{"lookups": {"tagged_vlans__vid": 3102}}]},
            {"or": [{"lookups": {"device__interfaces__name": "assignment-both"}}]},
            {"not": {"lookups": {"tagged_vlans__vid": 3102}}},
        ):
            with self.subTest(where=where):
                failed(self, self.interfaces(where=where), "ValidationError")
        for where in (
            {"compare": {"left": "untagged_vlan__vid", "lookup": "exact", "right": "name"}},
            {"or": [{"lookups": {"untagged_vlan__vid": 3101}}, {"lookups": {"tagged_vlans__vid": 3102}}]},
        ):
            with self.subTest(local=where):
                failed(self, self.interfaces(aggregates=local(where)), "ValidationError")


class AggregateHavingTests(TestCase):
    """Grouped conditions use declared, visibility-aware outputs before continuation."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.data = build_assignment_fixtures(build_fixtures())
        cls.superuser = User.objects.create_superuser(username="having-aggregate")

    def query(self, *, error=None, **arguments):
        """Run a grouped scenario using the shared fixture and optional expected error."""
        message = call_query(
            {
                "model": "dcim.Interface",
                "mode": "aggregate",
                "group_by": ["untagged_vlan__vid"],
                "where": {"compare": {"left": "status__name", "lookup": "exact", "right": "status__name"}},
                "aggregates": {
                    "all_rows": {"function": "Count", "field": "pk"},
                    "assigned_vids": {
                        "function": "Count",
                        "field": "untagged_vlan__vid",
                        "where": {"lookups": {"status__name": "Active"}},
                    },
                },
                **arguments,
            },
            self.superuser,
        )
        if error is not None:
            return failed(self, message, error)
        return succeeded(self, message)

    def test_having_filters_before_pages_and_binds_continuation(self):
        first = self.query(order_by=["untagged_vlan__vid"], limit=1)
        self.assertTrue(first["truncated"])
        rest = self.query(order_by=["untagged_vlan__vid"], limit=10, cursor=first["next_cursor"])
        self.assertEqual(
            first["results"] + rest["results"],
            [
                {"untagged_vlan__vid": 3101, "all_rows": 2, "assigned_vids": 2},
                {"untagged_vlan__vid": 3104, "all_rows": 1, "assigned_vids": 1},
                {"untagged_vlan__vid": None, "all_rows": 4, "assigned_vids": 0},
            ],
        )
        selected = self.query(having={"lookups": {"all_rows__gte": 2}}, order_by=["untagged_vlan__vid"], limit=1)
        self.assertEqual(selected["results"], [{"untagged_vlan__vid": 3101, "all_rows": 2, "assigned_vids": 2}])
        self.assertTrue(selected["truncated"])
        self.query(
            error="InvalidCursor",
            order_by=["untagged_vlan__vid"],
            limit=1,
            having={"lookups": {"all_rows__gte": 1}},
            cursor=first["next_cursor"],
        )

    def test_having_validation_and_modes(self):
        compared = self.query(
            having={
                "or": [
                    {"compare": {"left": "all_rows", "lookup": "gt", "right": "assigned_vids"}},
                    {"lookups": {"untagged_vlan__vid": 3104}},
                ]
            }
        )
        self.assertEqual(
            compared["results"],
            [
                {"untagged_vlan__vid": 3104, "all_rows": 1, "assigned_vids": 1},
                {"untagged_vlan__vid": None, "all_rows": 4, "assigned_vids": 0},
            ],
        )
        empty = self.query(having={"lookups": {"all_rows__gt": 1000}})
        self.assertEqual(empty, {**EMPTY_GROUP_PAGE, "model": "dcim.Interface"})

        malformed = {"compare": {"left": "name", "lookup": "private-sentinel-value", "right": "name"}}
        for arguments, location in (
            ({"having": {"and": [{"lookups": {"all_rows": 1}}] * 101}}, ["having"]),
            (
                {"aggregates": {"assigned_vids": {"function": "Count", "field": "pk", "where": malformed}}},
                ["aggregates", "assigned_vids", "where", "compare", "lookup"],
            ),
        ):
            with self.subTest(location=location):
                payload = self.query(error="ValidationError", **arguments)
                self.assertEqual(payload["details"][0]["loc"], location)
                self.assertNotIn("private-sentinel-value", str(payload))
        for having in (
            {"lookups": {"untagged_vlan__vid__name": "not allowed"}},
            {"lookups": {"_agent_projection_0": 1}},
            {"lookups": {"status__name": "Active"}},
            {"lookups": {"all_rows__isnull": "yes"}},
            {"lookups": {"all_rows__in": 1}},
            {"lookups": {"all_rows__range": [1]}},
            {"lookups": {"all_rows__gt": None}},
            {"compare": {"left": "status__name", "lookup": "exact", "right": "all_rows"}},
        ):
            with self.subTest(having=having):
                self.query(error="ValidationError", having=having)

    def test_having_sees_masked_group_keys_and_preserves_annotation_nulls(self):
        visible_user, _ = create_principals(User.objects.create_user(username="having-visible"))
        args = {
            "model": "dcim.Location",
            "mode": "aggregate",
            "filters": {"name__in": ["Visible Alpha", "Visible Beta", "Hidden Gamma"]},
            "group_by": ["location_type__name"],
            "aggregates": {
                "nonnull_latitudes": {"function": "Count", "field": "latitude"},
                "sum_latitude": {"function": "Sum", "field": "latitude", "where": {"lookups": {"latitude__gt": 50}}},
            },
        }
        null_row = [{"location_type__name": None, "nonnull_latitudes": 1, "sum_latitude": None}]
        for having, expected in (
            ({"lookups": {"location_type__name__isnull": True}}, null_row),
            ({"not": {"lookups": {"location_type__name": "Test Campus"}}}, []),
            ({"not": {"lookups": {"sum_latitude__gt": 0}}}, []),
            ({"lookups": {"sum_latitude__isnull": True}}, null_row),
        ):
            with self.subTest(having=having):
                payload = succeeded(self, call_query({**args, "having": having}, visible_user))
                self.assertEqual(payload["results"], expected)
