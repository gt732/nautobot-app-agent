"""Live-data fixtures and native tool-call assertions."""

import json
import uuid
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from langchain.tools import ToolRuntime
from nautobot.dcim.models import Device, DeviceType, Interface, Location, LocationType, Manufacturer
from nautobot.extras.models import Role, Status
from nautobot.ipam.models import VLAN
from nautobot.users.models import ObjectPermission

from nautobot_agent.models import AgentCheckpoint, AgentCheckpointWrite
from nautobot_agent.runtime.budgets import RunBudget
from nautobot_agent.runtime.context import AgentContext
from nautobot_agent.tests.support import AllowAllAuthority
from nautobot_agent.tests.support.chat import create_conversation
from nautobot_agent.tools.query_records import query_records
from nautobot_agent.tools.trace_relationships import trace_relationships

## Shared live-query, trace, and history fixtures
User = get_user_model()

ALL_NAMES = ["Visible Alpha", "Visible Beta", "Hidden Gamma"]
TOKEN_KEY = "0123456789abcdef0123456789abcdef01234567"
INTERNAL_MODELS = (
    "nautobot_agent.AgentConversation",
    "nautobot_agent.AgentCheckpoint",
    "nautobot_agent.AgentCheckpointWrite",
    "nautobot_agent.AgentExecutionTrace",
    "nautobot_agent.AgentExecutionSpan",
)
REVERSE_PATH = "user__nautobot_agent_conversations"
_UNSET = object()


def create_internal_rows(user):
    """Persist one real owned conversation with a checkpoint and a pending write, as the saver would."""
    conversation = create_conversation(user)
    checkpoint = AgentCheckpoint.objects.create(conversation=conversation, checkpoint_id="internal-checkpoint")
    AgentCheckpointWrite.objects.create(
        checkpoint=checkpoint, task_id="task", idx=0, channel="channel", value_type="json", value=b"INTERNAL-VALUE"
    )
    return conversation


def build_fixtures():
    """Create a Location graph with a Status content type, two location types, and one Device with an Interface."""
    active, _ = Status.objects.get_or_create(name="Active", defaults={"color": "00ff00"})
    for model in (Location, Device, Interface):
        active.content_types.add(ContentType.objects.get_for_model(model))
    campus = LocationType.objects.create(name="Test Campus")
    campus.content_types.add(ContentType.objects.get_for_model(Device))
    branch = LocationType.objects.create(name="Test Branch")
    alpha = Location.objects.create(
        name="Visible Alpha",
        location_type=campus,
        status=active,
        time_zone="America/Chicago",
        latitude=Decimal("41.878100"),
    )
    beta = Location.objects.create(name="Visible Beta", location_type=branch, status=active)
    gamma = Location.objects.create(name="Hidden Gamma", location_type=campus, status=active)
    role = Role.objects.create(name="Test Edge")
    role.content_types.add(ContentType.objects.get_for_model(Device))
    manufacturer = Manufacturer.objects.create(name="Test Acme")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model="Test Router")
    device = Device.objects.create(name="edge-1", location=alpha, device_type=device_type, role=role, status=active)
    interface = Interface.objects.create(
        device=device, name="eth0", type="1000base-t", status=active, mac_address="00:1a:2b:3c:4d:5e"
    )
    return SimpleNamespace(
        active=active,
        campus=campus,
        branch=branch,
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        device=device,
        interface=interface,
    )


def build_assignment_fixtures(data):
    """Extend a caller's base fixture graph with named, deterministic VLAN assignments."""
    for model in (Interface, VLAN):
        data.active.content_types.add(ContentType.objects.get_for_model(model))

    vlan_specs = (
        ("assignment-vlan-untagged", 3101),
        ("assignment-vlan-tagged-a", 3102),
        ("assignment-vlan-tagged-b", 3103),
        ("assignment-vlan-both", 3104),
    )
    vlans = {name: VLAN.objects.create(vid=vid, name=name, status=data.active) for name, vid in vlan_specs}
    template = data.device
    assignments = {}
    for suffix, untagged, tagged in (
        ("untagged-only", "assignment-vlan-untagged", ()),
        ("tagged-only", None, ("assignment-vlan-tagged-a", "assignment-vlan-tagged-b")),
        ("both", "assignment-vlan-both", ("assignment-vlan-tagged-a", "assignment-vlan-tagged-b")),
        ("neither", None, ()),
    ):
        device = Device.objects.create(
            name=f"assignment-{suffix}",
            location=data.alpha,
            device_type=template.device_type,
            role=template.role,
            status=data.active,
        )
        interface = Interface.objects.create(
            device=device,
            name=f"assignment-{suffix}",
            type="1000base-t",
            status=data.active,
            mode="tagged" if tagged else "access",
            untagged_vlan=vlans[untagged] if untagged else None,
        )
        if tagged:
            interface.tagged_vlans.set([vlans[name] for name in tagged])
        assignments[suffix.replace("-", "_")] = interface

    # These distinct child interfaces satisfy separate leaves on one device, but never together.
    split_device = Device.objects.create(
        name="assignment-split-child",
        location=data.alpha,
        device_type=template.device_type,
        role=template.role,
        status=data.active,
    )
    split_untagged = Interface.objects.create(
        device=split_device,
        name="assignment-split-untagged",
        type="1000base-t",
        status=data.active,
        untagged_vlan=vlans["assignment-vlan-untagged"],
    )
    split_tagged = Interface.objects.create(
        device=split_device,
        name="assignment-split-tagged",
        type="1000base-t",
        status=data.active,
        mode="tagged",
    )
    split_tagged.tagged_vlans.add(vlans["assignment-vlan-tagged-a"])
    return SimpleNamespace(
        **vars(data),
        vlans=vlans,
        **assignments,
        split_device=split_device,
        split_untagged=split_untagged,
        split_tagged=split_tagged,
    )


def build_twin_locations(data, low_id, high_id):
    """Create two same-named children under different parents, with two devices each and fixed identifiers."""
    child = LocationType.objects.create(name="Twin Child", parent=data.campus)
    child.content_types.add(ContentType.objects.get_for_model(Device))
    template = data.device
    # Create the higher identifier first, so insertion order cannot explain stored-key ordering.
    for location_id, parent, prefix in ((high_id, data.alpha, "high"), (low_id, data.gamma, "low")):
        twin = Location.objects.create(
            id=location_id, name="Twin", parent=parent, location_type=child, status=data.active
        )
        for n in range(2):
            Device.objects.create(
                name=f"twin-{prefix}-{n}",
                location=twin,
                device_type=template.device_type,
                role=template.role,
                status=data.active,
            )


def grant(user, model, *, actions=("view",), constraints=None):
    """Give a user an ObjectPermission over a model, following Nautobot's own permission conventions."""
    permission = ObjectPermission.objects.create(
        name=f"agent-test {user.username} {'+'.join(actions)} {model._meta.label} {uuid.uuid4().hex}",
        actions=list(actions),
        constraints=constraints,
    )
    permission.users.add(user)
    permission.object_types.add(ContentType.objects.get_for_model(model))
    return permission


def create_principals(visible_user):
    """Constrain the given user to Visible rows and return it with a second ordinary user limited to Hidden rows."""
    grant(visible_user, Location, constraints={"name__startswith": "Visible"})
    hidden_user = User.objects.create_user(username="hiddenuser")
    grant(hidden_user, Location, constraints={"name__startswith": "Hidden"})
    return visible_user, hidden_user


@contextmanager
def query_byte_limit(max_bytes):
    """Temporarily set the deployment ceiling for complete encoded data-tool responses."""
    with patch("nautobot_agent.data.tool_io.query_limits", return_value=max_bytes):
        yield


def temporary_model_tables(cls, *models):
    """Create unmanaged tables before TestCase setup and drop them in reverse order after teardown."""

    def drop_table(model):
        with connection.schema_editor() as editor:
            editor.delete_model(model)

    ContentType.objects.clear_cache()
    cls.addClassCleanup(ContentType.objects.clear_cache)
    for model in models:
        with connection.schema_editor() as editor:
            editor.create_model(model)
        cls.addClassCleanup(drop_table, model)


def _invoke_tool(tool, arguments, context, call_id):
    """Invoke a real tool as the native ToolNode does: a tool call carrying an injected ToolRuntime."""
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _chunk: None, tool_call_id=call_id, store=None
    )
    return tool.invoke(
        {"type": "tool_call", "name": tool.name, "id": call_id, "args": {**arguments, "runtime": runtime}}
    )


def call_query(args, user=None, *, context=_UNSET, call_id="direct"):
    """Invoke the real query tool; an explicit context (even None) is passed through, else inferred from user."""
    if context is _UNSET:
        assert user is not None
        context = AgentContext(
            user_id=str(user.pk), authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
        )
    return _invoke_tool(query_records, args, context, call_id)


def call_trace(args, user, call_id="trace"):
    """Invoke the real trace tool through its native injected-runtime interface."""
    return _invoke_tool(
        trace_relationships,
        args,
        AgentContext(
            user_id=str(user.pk), authority=AllowAllAuthority(), budget=RunBudget(tool_limit=100, model_limit=40)
        ),
        call_id,
    )


def succeeded(case, message):
    """Assert a tool message is a success and return its parsed JSON payload."""
    case.assertEqual(message.status, "success", message.content)
    return json.loads(message.content)


def failed(case, message, error=None):
    """Assert a tool message is an error observation and return its parsed JSON payload."""
    case.assertEqual(message.status, "error", message.content)
    payload = json.loads(message.content)
    if error is not None:
        case.assertEqual(payload["error"], error, message.content)
    return payload


def list_names(case, user, args=None):
    """Return the ``name`` column of a list query in result order."""
    payload = succeeded(case, call_query({"model": "dcim.Location", "fields": ["name"], **(args or {})}, user))
    return [row["name"] for row in payload["results"]]


def restrict_spy(case, model=Location):
    """Record native restriction calls and whether their starting QuerySets are already filtered."""
    queryset_class = type(model.objects.get_queryset())
    original = queryset_class.restrict
    calls = []

    def spy(queryset, user, action="view"):
        calls.append((user.pk, action, bool(queryset.query.where)))
        return original(queryset, user, action)

    case.enterContext(patch.object(queryset_class, "restrict", spy))
    return calls
