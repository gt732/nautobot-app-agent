"""Live-data projection visibility; roots/includes separately require native restriction admission.

Related projection hops use the fallback constraint filter so models such as User can be masked
instead of rejecting an otherwise valid projection. Hidden single relations expose identity only.
"""

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, Exists, F, Model, OuterRef, Q, QuerySet, When
from django.db.models.expressions import Expression
from django.db.models.fields.related import ForeignKey
from langchain.tools import ToolRuntime
from nautobot.core.utils.permissions import get_permission_for_model, permission_is_exempt, qs_filter_from_constraints

from nautobot_agent.app_settings import blocked_models
from nautobot_agent.data.paths import ResolvedPath, meta
from nautobot_agent.data.tool_io import tool_error
from nautobot_agent.runtime.context import AgentContext

if TYPE_CHECKING:
    from nautobot.users.models import User

_INTERNAL_STORAGE_MODELS = frozenset(
    {
        "nautobot_agent.agentconversation",
        "nautobot_agent.agentcheckpoint",
        "nautobot_agent.agentcheckpointwrite",
        "nautobot_agent.agentexecutiontrace",
        "nautobot_agent.agentexecutionspan",
        "nautobot_agent.agentrun",
    }
)
_INTERNAL_STORAGE_MESSAGE = "Internal agent conversation and trace storage is not available to generic live-data tools."
BLOCKED_MODEL_MESSAGE = (
    "Access to this model or its details is restricted by administrator policy. "
    "Tell the user to contact their Nautobot administrator. "
    "Do not give advice on fixing this permission issue, suggest permission or configuration changes, "
    "retry the denied request, or offer workarounds."
)


def _is_internal_storage(model: type[Model]) -> bool:
    return meta(model).label_lower in _INTERNAL_STORAGE_MODELS


def path_denial(record: ResolvedPath) -> Literal["sensitive", "internal_storage"] | None:
    """Classify the first static path exclusion without checking permissions or querying the database."""
    for field in record.iter_fields():
        declarations = getattr(field.model, "sensitive_fields", ()) or ()
        if field.name in declarations or getattr(field, "attname", None) in declarations:
            return "sensitive"
        related_model = getattr(field, "related_model", None)
        if related_model is not None and _is_internal_storage(related_model):
            return "internal_storage"
    return None


def reject_sensitive(paths: Iterable[tuple[str, ResolvedPath]]) -> None:
    """Reject the first sensitive field or internal-storage hop in public path order."""
    for path, record in paths:
        denial = path_denial(record)
        if denial == "sensitive":
            raise tool_error("PermissionDenied", f"The path {path!r} refers to a sensitive field and cannot be used.")
        if denial == "internal_storage":
            raise tool_error("PermissionDenied", _INTERNAL_STORAGE_MESSAGE)


def reject_internal_target(model: type[Model]) -> None:
    """Reject generic data access to persisted agent conversations, runs, and traces."""
    if _is_internal_storage(model):
        raise tool_error("PermissionDenied", _INTERNAL_STORAGE_MESSAGE)


def _storage_owner_labels(model: type[Model]) -> set[str]:
    """Return concrete storage owners for a model, including multi-table parents."""
    concrete = meta(model).concrete_model
    return {meta(owner).label_lower for owner in (concrete, *meta(concrete).get_parent_list())}


def reject_blocked_target(model: type[Model]) -> None:
    """Reject explicit targets whose records are owned by a blocked concrete model."""
    configured = {label.lower() for label in blocked_models()}
    if configured.intersection(_storage_owner_labels(model)):
        raise tool_error("PermissionDenied", BLOCKED_MODEL_MESSAGE)


def reject_blocked_output(path: ResolvedPath) -> None:
    """Reject selected columns owned by blocked storage or traversal through blocked records."""
    configured = {label.lower() for label in blocked_models()}
    if not configured:
        return
    for field in path.iter_fields():
        if configured.intersection(_storage_owner_labels(field.model)):
            raise tool_error("PermissionDenied", BLOCKED_MODEL_MESSAGE)
        if path.local_field is not None:
            return
        related_model = getattr(field, "related_model", None)
        if related_model is not None and configured.intersection(_storage_owner_labels(related_model)):
            raise tool_error("PermissionDenied", BLOCKED_MODEL_MESSAGE)


def trusted_user(runtime: ToolRuntime[AgentContext]) -> "User":
    """Reload and validate the user identified by the trusted runtime context."""
    context = runtime.context
    if not isinstance(context, AgentContext) or not isinstance(context.user_id, str) or not context.user_id:
        raise tool_error("AuthenticationError", "No trusted user context is available for this query.")
    try:
        user = get_user_model()._default_manager.get(pk=context.user_id)  # pylint: disable=protected-access
    except (ObjectDoesNotExist, ValueError, TypeError, DjangoValidationError):
        raise tool_error("AuthenticationError", "The requesting user could not be authenticated.") from None
    if not user.is_authenticated or not user.is_active:
        raise tool_error("AuthenticationError", "The requesting user could not be authenticated.")
    return cast("User", user)


def manager_restrict(target: type[Model]) -> Callable[[Any, str], QuerySet]:
    """Require the model's native Nautobot view-restriction primitive."""
    restrict = getattr(target._default_manager, "restrict", None)  # pylint: disable=protected-access
    if not callable(restrict):
        raise tool_error(
            "UnsupportedModel",
            f"{meta(target).label} does not support Nautobot view-permission restriction and cannot be queried.",
        )
    return cast(Callable[[Any, str], QuerySet], restrict)


def require_view_permission(target: type[Model], user: "User") -> None:
    """Require the requesting user's model-level view permission."""
    permission = get_permission_for_model(target, "view")
    if not user.has_perm(permission):
        raise tool_error("PermissionDenied", f"Missing permission {permission!r} for model {meta(target).label}.")


def brief_identity(instance: Model | None) -> dict[str, Any] | None:
    """Serialize a related instance using Nautobot's brief ``id`` and ``display`` identity."""
    if instance is None:
        return None
    display = getattr(instance, "display", None)
    return {"id": instance.pk, "display": str(display if display is not None else instance)}


def _grant(model: type[Model], user: Any, action: str) -> bool | Q:
    """Return True for unrestricted access, False for denial, or the compiled row predicate."""
    if not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", False):
        return False
    permission = get_permission_for_model(model, action)
    if (
        (model is ContentType and action == "view")
        or getattr(user, "is_superuser", False)
        or (
            permission_is_exempt(permission) and callable(getattr(model._default_manager, "restrict", None))  # pylint: disable=protected-access
        )
    ):
        return True
    permissions = user.get_all_permissions()
    if (
        permission not in permissions
        or (constraints := getattr(user, "_object_perm_cache", {}).get(permission)) is None
    ):
        return False
    condition = qs_filter_from_constraints(constraints, {"$user": user})
    return condition or True


def _restrict_with_grant(queryset: QuerySet, grant: bool | Q) -> QuerySet:
    """Apply an already-resolved grant using Nautobot's constrained-PK algorithm."""
    if grant is False:
        return queryset.none()
    model = queryset.model
    manager = cast(Any, model)._default_manager.db_manager(queryset.db)  # pylint: disable=protected-access
    inner = manager.filter(grant)
    if hasattr(inner, "without_tree_fields"):
        inner = inner.without_tree_fields()
    return queryset.filter(pk__in=inner.order_by().values("pk"))


def visible_or_null(path: ResolvedPath, expression_path: str, user: Any) -> Expression | F:
    """Mask a projected value when any traversed related object is outside view restrictions."""
    conditions = []
    for hop in path.forward_hops:
        target = cast(type[Model], hop.field.related_model)
        grant = _grant(target, user, "view")
        if grant is True:
            continue
        restricted = _restrict_with_grant(
            cast(Any, target)._default_manager.all(),  # pylint: disable=protected-access
            grant,  # pylint: disable=protected-access
        )
        conditions.append(
            Exists(restricted.filter(**{hop.field.target_field.attname: OuterRef(hop.stored_path)}).order_by())
        )
    if not conditions:
        return F(expression_path)
    condition = conditions[0]
    for extra in conditions[1:]:
        condition &= extra
    return Case(When(condition=condition, then=F(expression_path)), default=None)


def _display_join_fields(model: type[Model]) -> list[str]:
    """Return generic eager FK joins, not a complete declaration of native display dependencies."""
    # An unrestricted select_related() also joins custom ForeignObject fields; those may
    # discard an existing identity when their composite target is absent.
    return [field.name for field in meta(model).concrete_fields if isinstance(field, ForeignKey) and not field.null]


def brief_join_paths(field: ForeignKey) -> list[str]:
    """Return paths covering the same joins identity_queryset uses, applied through a relation."""
    name = cast(str, field.name)
    return [name, *(f"{name}__{related}" for related in _display_join_fields(field.related_model))]


def identity_queryset(model: type[Model]) -> QuerySet:
    """Return the unrestricted source for a hidden relation's identity-only probe."""
    manager = cast(Any, model)._default_manager  # pylint: disable=protected-access
    related = _display_join_fields(model)
    return manager.select_related(*related) if related else manager.all()


def identity_briefs(field: ForeignKey, values: Sequence[Any]) -> dict[Any, dict[str, Any] | None]:
    """Batch-fetch identities by stored FK values, including ``to_field`` and missing targets."""
    stored_values = {value for value in values if value is not None}
    briefs: dict[Any, dict[str, Any] | None] = {}
    if stored_values:
        target_field = field.target_field
        for target in identity_queryset(field.related_model).filter(**{f"{target_field.attname}__in": stored_values}):
            briefs[getattr(target, target_field.attname)] = brief_identity(target)
    return {value: briefs.get(value) for value in values}
