"""Shared admission, metadata discovery, and execution lifecycle for live-data tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, cast

from django.db.models import (
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    FloatField,
    ForeignKey,
    IntegerField,
    Model,
    Q,
    QuerySet,
    TextField,
    TimeField,
    UUIDField,
)
from django.db.models import Field as ModelField
from langchain.tools import ToolRuntime
from nautobot.apps.utils import get_model_from_name
from pydantic import BaseModel, ConfigDict, JsonValue, StrictStr

from nautobot_agent.data.access import (
    _grant,
    manager_restrict,
    path_denial,
    reject_blocked_target,
    reject_internal_target,
    reject_sensitive,
    require_view_permission,
    trusted_user,
)
from nautobot_agent.data.conditions import QueryConditionInput, compile_condition, iter_condition_nodes
from nautobot_agent.data.paths import ResolvedPath, StandardRelation, meta, resolve_path, standard_relation
from nautobot_agent.data.tool_io import orm_errors, tool_error
from nautobot_agent.runtime.context import AgentContext

FIELD_FAMILIES: dict[str, tuple[type[ModelField], ...]] = {
    "number": (IntegerField, FloatField, DecimalField),
    "text": (CharField, TextField),
    "datetime": (DateTimeField,),
    "date": (DateField,),
    "time": (TimeField,),
    "boolean": (BooleanField,),
    "uuid": (UUIDField,),
}
GROUPABLE_FIELDS = tuple(
    field for family in ("number", "text", "date", "time", "boolean", "uuid") for field in FIELD_FAMILIES[family]
)


class RootQueryInput(BaseModel):
    """Closed root selection with a trusted runtime omitted from public tool schemas."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    model: StrictStr
    runtime: ToolRuntime[AgentContext]
    filters: dict[StrictStr, JsonValue] | None = None
    exclude: dict[StrictStr, JsonValue] | None = None
    where: QueryConditionInput | None = None


def _comparison_family(field: ModelField) -> str | None:
    # DateTimeField inherits DateField, so resolve the more specific family first.
    for family, field_types in FIELD_FAMILIES.items():
        if isinstance(field, field_types):
            return family
    return None


def validate_comparison_types(left: ModelField, right: ModelField, lookup: str) -> None:
    """Require compatible stored scalar families without casts or null normalization."""
    left_type, right_type = _comparison_family(left), _comparison_family(right)
    if left_type is None or left_type != right_type:
        raise tool_error("ValidationError", "Comparison operands must have compatible stored scalar types.")
    if left_type in {"boolean", "uuid"} and lookup != "exact":
        raise tool_error("ValidationError", "Boolean and UUID comparisons support only the exact lookup.")


def scalar_field(resolved_path: ResolvedPath) -> ModelField:
    """Resolve an admitted path to a supported stored scalar."""
    scalar = resolved_path.stored_scalar()
    if not isinstance(scalar, GROUPABLE_FIELDS):
        raise tool_error("ValidationError", f"The path {resolved_path.path!r} is not a supported stored scalar type.")
    return scalar


def model_from_name(name: str) -> type[Model]:
    """Look up a registered model with the shared public tool error mapping."""
    try:
        return get_model_from_name(name)
    except TypeError as exc:
        raise tool_error("ModelLookupError", str(exc)) from None


def describe(model: type[Model]) -> dict[str, Any]:
    """Project public model metadata while omitting statically forbidden paths."""
    reject_internal_target(model)
    reject_blocked_target(model)
    opts = meta(model)
    fields: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for field in opts.get_fields():
        if field.concrete and not field.many_to_many:
            if path_denial(resolve_path(model, field.name)) is None:
                record = {
                    "name": field.name,
                    "type": type(field).__name__,
                    "null": bool(field.null),
                }
                if field.attname != field.name:
                    record["attname"] = field.attname
                target = getattr(field, "related_model", None)
                if target is not None and isinstance(target, type) and issubclass(target, Model):
                    record["related_model"] = cast(Any, target)._meta.label
                fields.append(record)

        resolved_relation = standard_relation(field)
        if resolved_relation is not None and path_denial(resolve_path(model, resolved_relation.query_name)) is None:
            relationships.append(
                {
                    "accessor": resolved_relation.accessor,
                    "query_name": resolved_relation.query_name,
                    "related_model": cast(Any, resolved_relation.target)._meta.label,
                    "kind": resolved_relation.kind,
                    "many": resolved_relation.many,
                }
            )

    fields.sort(key=lambda item: item["name"])
    relationships.sort(key=lambda item: (item["accessor"], item["query_name"], item["related_model"], item["kind"]))
    return {
        "model": opts.label,
        "primary_key": opts.pk.name,
        "fields": fields,
        "relationships": relationships,
    }


def _admit_paths(model: type[Model], requested_paths: Sequence[str]) -> dict[str, ResolvedPath]:
    """Resolve distinct path names once, then check sensitivity in the public request order."""
    paths = {path: resolve_path(model, path) for path in dict.fromkeys(requested_paths)}
    reject_sensitive((path, paths[path]) for path in requested_paths)
    return paths


@dataclass(frozen=True)
class AdmittedRelated:
    """A related target with its native restriction and admitted field paths."""

    restrict_target: Callable[[Any, str], QuerySet]
    _paths: dict[str, ResolvedPath]

    def resolve(self, path: str) -> ResolvedPath:
        """Look up previously admitted metadata; raise KeyError when the path was not admitted."""
        return self._paths[path]


def admit_related(model: type[Model], relation: StandardRelation, requested_paths: Sequence[str]) -> AdmittedRelated:
    """Admit a standard relation target without constructing its restricted queryset."""
    relation_path = relation.query_name
    reject_sensitive([(relation_path, resolve_path(model, relation_path))])
    reject_internal_target(relation.target)
    reject_blocked_target(relation.target)
    restrict = manager_restrict(relation.target)
    paths = _admit_paths(relation.target, requested_paths)
    return AdmittedRelated(restrict, paths)


@dataclass(frozen=True)
class AdmittedRoot:  # pylint: disable=too-many-instance-attributes
    """A root carrying the request's resolved paths, with restriction deferred until execution."""

    user: Any
    model: type[Model]
    _restrict: Callable[[Any, str], QuerySet]
    _filters: Mapping[str, Any]
    _exclude: Mapping[str, Any]
    _where: Q | None
    predicate_paths: list[str]
    _paths: dict[str, ResolvedPath] = dataclass_field(default_factory=dict, compare=False, repr=False)

    def resolve(self, path: str) -> ResolvedPath:
        """Return a memoized record that has passed sensitive-field and internal-storage admission."""
        try:
            return self._paths[path]
        except KeyError:
            resolved = resolve_path(self.model, path)
            reject_sensitive([(path, resolved)])
            self._paths[path] = resolved
            return resolved

    @property
    def label(self) -> str:
        """Return the model's ``app_label.ModelName`` label."""
        return meta(self.model).label

    def restricted_queryset(self) -> QuerySet:
        """Construct and predicate-filter the root only after request admission succeeds."""
        queryset = self._restrict(self.user, "view")
        with orm_errors():
            if self._where is not None:
                queryset = queryset.filter(self._where, Q(*self._filters.items()))
            elif self._filters:
                queryset = queryset.filter(Q(*self._filters.items()))
            if self._exclude:
                queryset = queryset.exclude(Q(*self._exclude.items()))
        return queryset


def validate_comparisons(condition: QueryConditionInput | None, root: AdmittedRoot) -> None:
    """Admit all comparison operands before constructing a restricted queryset."""
    for node in iter_condition_nodes(condition):
        if node.compare is None:
            continue
        fields = []
        for path in (node.compare.left, node.compare.right):
            resolved = root.resolve(path)
            fields.append(scalar_field(resolved))
        validate_comparison_types(fields[0], fields[1], node.compare.lookup)


def _require_predicate_permissions(user: Any, paths: Sequence[ResolvedPath]) -> None:
    """Authorize predicate hops independently of permission-masked projection paths."""
    grants = {}
    for path in paths:
        for index, field in enumerate(path.iter_fields()):
            related = getattr(field, "related_model", None)
            if related is None:
                continue
            if related not in grants:
                grants[related] = _grant(related, user, "view")
            if grants[related] is False:
                raise tool_error(
                    "PermissionDenied",
                    f"Missing view permission for related model {meta(related).label}.",
                )
            if grants[related] is True:
                continue
            remaining = path.parts[index + 1 :]
            if isinstance(field, ForeignKey) and field.concrete:
                if remaining and remaining[0] in ("pk", meta(related).pk.name, field.target_field.name):
                    remaining = remaining[1:]
                if remaining in ((), ("exact",), ("in",), ("isnull",)):
                    continue
            raise tool_error(
                "PermissionDenied",
                f"Filtering related fields of {meta(related).label} requires unrestricted view permission.",
            )


def admit_root(
    request: RootQueryInput, request_paths: Sequence[str], additional_predicate_paths: Sequence[str] = ()
) -> AdmittedRoot:
    """Run shared root authorization, predicate compilation, resolution, and sensitivity checks."""
    user = trusted_user(request.runtime)
    target = model_from_name(request.model)
    reject_internal_target(target)
    reject_blocked_target(target)
    restrict = manager_restrict(target)
    require_view_permission(target, user)
    predicate, where_paths = compile_condition(request.where)
    filters = request.filters or {}
    exclude = request.exclude or {}
    predicate_paths = [*filters, *exclude, *where_paths, *additional_predicate_paths]
    all_requested_paths = [*predicate_paths, *request_paths]
    paths = _admit_paths(target, all_requested_paths)
    _require_predicate_permissions(user, [paths[path] for path in dict.fromkeys(predicate_paths)])
    root = AdmittedRoot(user, target, restrict, filters, exclude, predicate, predicate_paths, paths)
    validate_comparisons(request.where, root)
    return root
