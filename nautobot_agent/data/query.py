"""Admit and execute validated live-data queries within the query domain."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.db.models import (
    Avg,
    BinaryField,
    Count,
    Max,
    Min,
    Model,
    Q,
    Sum,
)
from django.db.models import Field as ModelField
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import RawSQL
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.db.models.functions import Now, Random
from django.db.models.sql.query import Query
from nautobot.ipam.fields import VarbinaryIPField
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
)

from nautobot_agent.data.access import identity_briefs, reject_blocked_output, visible_or_null
from nautobot_agent.data.admission import (
    FIELD_FAMILIES,
    GROUPABLE_FIELDS,
    AdmittedRoot,
    RootQueryInput,
    admit_root,
    scalar_field,
    validate_comparison_types,
    validate_comparisons,
)
from nautobot_agent.data.conditions import QueryConditionInput, compile_condition, iter_condition_nodes
from nautobot_agent.data.paths import RELATION_PATH_MESSAGE, ResolvedPath, meta
from nautobot_agent.data.query_pagination import MAX_CURSOR_LENGTH, Page, QueryContinuation, page, query_fingerprint
from nautobot_agent.data.tool_io import MIN_LIMIT, orm_errors, tool_error

_AGGREGATE_ALIAS = re.compile(r"[A-Za-z](?:_?[A-Za-z0-9])*")
DEFAULT_LIMIT = 50


class _AggregateSpec(BaseModel):
    """Closed aggregate specification: one allowlisted function over one stored scalar path."""

    model_config = ConfigDict(extra="forbid")

    function: Literal["Count", "Sum", "Avg", "Min", "Max"]
    field: StrictStr
    distinct: StrictBool = False
    where: QueryConditionInput | None = None


class QueryInput(RootQueryInput):
    """Closed query inputs with a native injected runtime, omitted from the public tool schema."""

    mode: Literal["list", "count", "exists", "aggregate"] = "list"
    group_by: list[StrictStr] | None = None
    having: QueryConditionInput | None = None
    aggregates: dict[StrictStr, _AggregateSpec] | None = Field(default=None, validate_default=True)
    fields: list[StrictStr] | None = None
    order_by: list[StrictStr] | None = None
    distinct: StrictBool = False
    limit: Annotated[StrictInt, Field(ge=MIN_LIMIT)] = DEFAULT_LIMIT
    cursor: Annotated[StrictStr | None, Field(max_length=MAX_CURSOR_LENGTH)] = None

    @field_validator("having")
    @classmethod
    def _check_having(cls, value: QueryConditionInput | None, info: ValidationInfo) -> QueryConditionInput | None:
        if value is not None and "mode" in info.data:
            if info.data["mode"] != "aggregate":
                raise ValueError("having is only valid for grouped aggregate queries")
            if "group_by" in info.data and not info.data["group_by"]:
                raise ValueError("having is only valid for grouped aggregate queries")
        return value

    @field_validator("group_by")
    @classmethod
    def _check_group_by(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is None:
            return value
        if "mode" in info.data and info.data["mode"] != "aggregate":
            raise ValueError("group_by is only valid in aggregate mode")
        if len(set(value)) != len(value):
            raise ValueError("group_by keys must be unique")
        return value

    @field_validator("aggregates")
    @classmethod
    def _check_aggregates(
        cls, value: dict[str, _AggregateSpec] | None, info: ValidationInfo
    ) -> dict[str, _AggregateSpec] | None:
        if "mode" in info.data:
            if info.data["mode"] != "aggregate" and value is not None:
                raise ValueError("aggregates is only valid in aggregate mode")
            if info.data["mode"] == "aggregate" and not value:
                raise ValueError("aggregate mode requires at least one aggregate specification")
        if value is None:
            return value
        for alias in value:
            if _AGGREGATE_ALIAS.fullmatch(alias) is None:
                raise ValueError(
                    "aggregate aliases must start with an ASCII letter, contain ASCII letters and digits with "
                    "optional single internal underscores, and not end with an underscore"
                )
            if "group_by" in info.data and alias in (info.data["group_by"] or []):
                raise ValueError("aggregate aliases must not equal a group_by key")
        return value

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is not None and "mode" in info.data and info.data["mode"] == "aggregate":
            raise ValueError("fields is not allowed in aggregate mode")
        return value

    @field_validator("order_by")
    @classmethod
    def _check_order_by(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if (
            value
            and "mode" in info.data
            and info.data["mode"] == "aggregate"
            and "group_by" in info.data
            and not info.data["group_by"]
        ):
            raise ValueError("a whole-query aggregate summary cannot be ordered")
        return value

    @field_validator("distinct")
    @classmethod
    def _check_distinct(cls, value: bool, info: ValidationInfo) -> bool:
        if value and "mode" in info.data and info.data["mode"] == "aggregate":
            raise ValueError("distinct is not allowed in aggregate mode; set it inside an aggregate specification")
        return value

    @field_validator("cursor")
    @classmethod
    def _check_cursor(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or "mode" not in info.data:
            return value
        mode = info.data["mode"]
        if mode == "list":
            return value
        if mode == "aggregate" and "group_by" not in info.data:
            return value
        if mode == "aggregate" and info.data["group_by"]:
            return value
        raise ValueError("Cursors are supported only for list queries and grouped aggregates.")


_NUMERIC_FIELDS = FIELD_FAMILIES["number"]
_ORDERED_FIELDS = (
    *_NUMERIC_FIELDS,
    *FIELD_FAMILIES["text"],
    *FIELD_FAMILIES["date"],
    *FIELD_FAMILIES["time"],
)
_FUNCTION_FIELDS: dict[str, tuple[type[ModelField], ...]] = {
    "Count": GROUPABLE_FIELDS,
    "Sum": _NUMERIC_FIELDS,
    "Avg": _NUMERIC_FIELDS,
    "Min": _ORDERED_FIELDS,
    "Max": _ORDERED_FIELDS,
}
_DISTINCT_FUNCTIONS = frozenset({"Count", "Sum", "Avg"})
_LIST_RELATION_PATH_MESSAGE = (
    "The list path {path!r} traverses a reverse, many-valued, or generic relation; "
    "use trace_relationships to inspect related collections."
)
_AGGREGATE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "Count": Count,
    "Sum": Sum,
    "Avg": Avg,
    "Min": Min,
    "Max": Max,
}


def _having_output_name(key: str, output_fields: dict[str, ModelField]) -> tuple[str, str]:
    """Resolve the longest declared output name and one optional allowlisted lookup."""
    for name in sorted(output_fields, key=len, reverse=True):
        if key == name:
            return name, "exact"
        if key.startswith(name + LOOKUP_SEP):
            suffix = key[len(name) + len(LOOKUP_SEP) :]
            if suffix in {"exact", "in", "gt", "gte", "lt", "lte", "range", "isnull"}:
                return name, suffix
            break
    raise tool_error("ValidationError", f"The having key {key!r} must name a declared group or aggregate output.")


def _having_lookup_value(field: ModelField, lookup: str, value: Any, key: str) -> None:
    """Check literal shapes and let each output field prepare its own values."""
    if value is None and lookup in {"gt", "gte", "lt", "lte"}:
        raise tool_error("ValidationError", f"The having threshold for {key!r} cannot be null; use exact or isnull.")
    if lookup == "isnull":
        if not isinstance(value, bool):
            raise tool_error("ValidationError", f"The having value for {key!r} must be a strict boolean.")
        return
    if lookup == "in" and not isinstance(value, list):
        raise tool_error("ValidationError", f"The having value for {key!r} must be an array.")
    if lookup == "range" and not (isinstance(value, list) and len(value) == 2):
        raise tool_error("ValidationError", f"The having value for {key!r} must be a two-item array.")
    values = value if lookup in {"in", "range"} else (value,)
    try:
        for item in values:
            field.get_prep_value(item)
    except (DjangoValidationError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise tool_error("ValidationError", f"The having value for {key!r} is invalid for its output type.") from exc


def _compile_having_condition(
    condition: QueryConditionInput,
    output_fields: dict[str, ModelField],
    group_aliases: dict[str, str],
) -> Q:
    """Admit grouped output predicates and compile them against permission-aware aliases."""

    def rewrite(key: str) -> str:
        name, lookup = _having_output_name(key, output_fields)
        alias = group_aliases.get(name, name)
        return alias if lookup == "exact" else f"{alias}{LOOKUP_SEP}{lookup}"

    for node in iter_condition_nodes(condition):
        if node.lookups is not None:
            for key, value in node.lookups.items():
                name, lookup = _having_output_name(key, output_fields)
                _having_lookup_value(output_fields[name], lookup, value, key)
        if node.compare is None:
            continue
        comparison = node.compare
        left_field = output_fields.get(comparison.left)
        right_field = output_fields.get(comparison.right)
        if left_field is None or right_field is None:
            raise tool_error("ValidationError", "Having comparisons must use declared group or aggregate outputs.")
        validate_comparison_types(left_field, right_field, comparison.lookup)
    return compile_condition(condition, rewrite)[0]


def _reserved_names(model: type[Model]) -> set[str]:
    names = {"pk"}
    for field in meta(model).get_fields(include_hidden=True):
        names.add(field.name)
        names.add(getattr(field, "attname", field.name))
        if isinstance(field, ForeignObjectRel):
            names.add(cast(str, cast(Any, field).get_accessor_name() or field.name))
    return names


def _reject_relations(resolved_path: ResolvedPath, key: str, message: str = RELATION_PATH_MESSAGE) -> None:
    if resolved_path.crosses_unsupported_relation:
        raise tool_error("ValidationError", message.format(path=key))


@dataclass(frozen=True)
class _AdmittedAggregate:
    """Validated aggregate input and its isolated optional predicate."""

    alias: str
    function: str
    distinct: bool
    resolved_path: ResolvedPath
    condition: Q | None
    normalized_condition: dict[str, Any] | None


class _QueryProjection:
    """Allocate private ORM aliases and serialize selected projection values."""

    def __init__(self, user: Any, excluded_names: set[str]) -> None:
        """Bind the principal and reserve names before allocating private aliases."""
        self._user = user
        self._excluded_names = set(excluded_names)
        self._next_alias = 0
        self.annotations: dict[str, Any] = {}
        self.selected: list[str] = []
        self._public_aliases: dict[str, str] = {}
        self._brief_fields: dict[str, Any] = {}

    def _allocate_alias(self) -> str:
        while True:
            alias = f"_agent_projection_{self._next_alias}"
            self._next_alias += 1
            if alias not in self._excluded_names:
                self._excluded_names.add(alias)
                return alias

    def _select(self, public_name: str, expression: Any, brief_field: Any) -> str:
        if public_name in self._public_aliases:
            return self._public_aliases[public_name]
        alias = self._allocate_alias()
        self.annotations[alias] = expression
        self.selected.append(alias)
        self._public_aliases[public_name] = alias
        self._brief_fields[alias] = brief_field
        return alias

    def select(self, resolved_path: ResolvedPath) -> str:
        """Select a stored value under its public name, retaining terminal brief metadata."""
        return self._select(
            resolved_path.path,
            visible_or_null(resolved_path, resolved_path.stored_path, self._user),
            resolved_path.brief_field,
        )

    def ordering_term(self, resolved_path: ResolvedPath, prefix: str) -> str:
        """Return an ordering term, reusing an already selected value when available."""
        alias = self._public_aliases.get(resolved_path.path)
        if alias is None:
            alias = self._allocate_alias()
            self.annotations[alias] = visible_or_null(resolved_path, resolved_path.stored_path, self._user)
        return prefix + alias

    def aggregate_expression(
        self, resolved_path: ResolvedPath, function: str, distinct: bool, condition: Q | None = None
    ) -> Any:
        """Build an admitted aggregate over the visible-or-null stored input path."""
        expression = visible_or_null(resolved_path, resolved_path.stored_path, self._user)
        return _AGGREGATE_FUNCTIONS[function](expression, distinct=distinct, filter=condition)

    def alias(self, public_name: str) -> str:
        """Return the selected alias for a public name."""
        return self._public_aliases[public_name]

    def serialize_rows(self, results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rename selected aliases and batch-convert terminal FK briefs."""
        brief_maps = {
            alias: identity_briefs(field, [row[alias] for row in results])
            for alias, field in self._brief_fields.items()
            if field is not None
        }
        public_names = {alias: name for name, alias in self._public_aliases.items()}
        serialized_rows = []
        for row in results:
            serialized_row = {}
            for alias, value in row.items():
                public_name = public_names.get(alias, alias)
                resolved_value = brief_maps[alias][value] if alias in brief_maps else value
                serialized_row[public_name] = resolved_value
            serialized_rows.append(serialized_row)
        return serialized_rows


@dataclass(frozen=True)
class _AdmittedListQuery:
    """Internal admitted list projection and continuation state."""

    root: AdmittedRoot
    limit: int
    distinct: bool
    projection: _QueryProjection
    continuation: QueryContinuation
    keyset: bool
    order_terms: tuple[Any, ...]


@dataclass(frozen=True)
class _AdmittedAggregateQuery:
    """Internal admitted aggregate expressions and continuation state."""

    root: AdmittedRoot
    limit: int
    projection: _QueryProjection
    continuation: QueryContinuation
    aggregate_annotations: dict[str, Any]
    having: Q | None
    ordering: tuple[str, ...]


@dataclass(frozen=True)
class _AdmittedScalarQuery:
    """Internal admitted count or existence query."""

    root: AdmittedRoot
    mode: str
    distinct: bool


def _fingerprint(request: QueryInput, root: AdmittedRoot, strategy: str, **query_shape: Any) -> str:
    return query_fingerprint(
        {
            "model": root.label,
            "mode": request.mode,
            "filters": request.filters or {},
            "exclude": request.exclude or {},
            "where": request.where.model_dump(exclude_none=True) if request.where is not None else None,
            "distinct": request.distinct,
            "strategy": strategy,
            **query_shape,
        }
    )


def _native_ordering(root: AdmittedRoot) -> list[Any]:
    """Validate the model's native ordering for deterministic continuation, appending ``pk`` when it is absent."""
    metadata = meta(root.model)
    ordering = list(cast(Sequence[Any], metadata.ordering or ()))
    for term in ordering:
        if isinstance(term, str):
            if term == "?":
                raise tool_error("ValidationError", "Random native ordering cannot be continued.")
            _reject_relations(root.resolve(term.removeprefix("-")), term, _LIST_RELATION_PATH_MESSAGE)
        elif not hasattr(term, "flatten") or any(
            isinstance(expression, Random | Now | RawSQL) for expression in term.flatten()
        ):
            raise tool_error("ValidationError", "Native ordering cannot be deterministically continued.")
    has_pk = any(
        isinstance(term, str) and term.lstrip("-") in {"pk", metadata.pk.name, metadata.pk.attname} for term in ordering
    )
    if not has_pk:
        ordering.append("pk")
    return ordering


def _list_order_terms(
    request: QueryInput,
    root: AdmittedRoot,
    projection: _QueryProjection,
    public_fields: tuple[str, ...],
    public_orders: tuple[str, ...],
) -> tuple[list[Any], list[Any]]:
    """Choose the deterministic order terms and the matching fingerprint ordering.

    Distinct queries order by the public terms then remaining projected fields; non-distinct queries use the
    public terms plus ``pk``; with no public ordering the validated native ordering applies.
    """
    order_terms: list[Any] = [
        projection.ordering_term(root.resolve(term.removeprefix("-")), "-" if term.startswith("-") else "")
        for term in public_orders
    ]
    if request.distinct:
        ordered_names = {term.removeprefix("-") for term in public_orders}
        order_terms.extend(
            projection.ordering_term(root.resolve(field), "") for field in public_fields if field not in ordered_names
        )
        return order_terms, list(public_orders)
    if not public_orders:
        native = _native_ordering(root)
        return [*native], native
    if any(term.lstrip("-") == "pk" for term in public_orders):
        return order_terms, list(public_orders)
    order_terms.append(projection.ordering_term(root.resolve("pk"), ""))
    return order_terms, [*public_orders, "pk"]


def _admit_list_paths(request: QueryInput, root: AdmittedRoot, public_fields: tuple[str, ...]) -> list[ResolvedPath]:
    """Reject list fields and ordering terms the list mode cannot return or continue; return resolved fields."""
    public_orders = request.order_by or []
    for path in (*public_fields, *public_orders):
        normalized = path.removeprefix("-")
        _reject_relations(root.resolve(normalized), normalized, _LIST_RELATION_PATH_MESSAGE)
    resolved_fields = [root.resolve(path) for path in public_fields]
    for path in resolved_fields:
        reject_blocked_output(path)
    for path in resolved_fields:
        if any(
            isinstance(field, BinaryField) and not isinstance(field, VarbinaryIPField) for field in path.iter_fields()
        ):
            raise tool_error("ValidationError", f"The path {path.path!r} contains binary data and cannot be returned.")
    if "?" in public_orders:
        raise tool_error("ValidationError", "Random ordering cannot be continued.")
    unprojected = next((term for term in public_orders if term.removeprefix("-") not in public_fields), None)
    if request.distinct and unprojected is not None:
        raise tool_error(
            "ValidationError", f"The ordering term {unprojected!r} must name a projected field when distinct is true."
        )
    return resolved_fields


def _admit_list(request: QueryInput, root: AdmittedRoot) -> _AdmittedListQuery:
    public_fields = tuple(request.fields or ["pk"])
    public_orders = tuple(request.order_by or [])
    resolved_fields = _admit_list_paths(request, root, public_fields)
    keyset = request.distinct and list(public_orders) in (["pk"], ["-pk"]) and "pk" in public_fields
    projection = _QueryProjection(root.user, _reserved_names(root.model) | set(public_fields) | set(public_orders))
    for path in resolved_fields:
        projection.select(path)
    order_terms, fingerprint_ordering = _list_order_terms(request, root, projection, public_fields, public_orders)

    strategy = "pk" if keyset else "offset"
    shape = {
        "fields": list(public_fields),
        "ordering": fingerprint_ordering,
    }
    fingerprint = _fingerprint(request, root, strategy, **shape)
    continuation = QueryContinuation.bind(
        request.cursor,
        user_id=root.user.pk,
        fingerprint=fingerprint,
        strategy=strategy,
        descending=bool(public_orders and public_orders[0].startswith("-")),
    )
    return _AdmittedListQuery(
        root=root,
        limit=request.limit,
        distinct=request.distinct,
        projection=projection,
        continuation=continuation,
        keyset=keyset,
        order_terms=tuple(order_terms),
    )


def _admit_aggregate_specs(
    request: QueryInput,
    root: AdmittedRoot,
    reserved_names: set[str],
    compiled: dict[str, tuple[Q | None, Sequence[str]]],
) -> list[_AdmittedAggregate]:
    """Validate each aggregate alias, function, scalar path type and local condition."""
    aggregates = []
    for alias, spec in (request.aggregates or {}).items():
        if alias in reserved_names:
            raise tool_error("ValidationError", f"The aggregate alias {alias!r} collides with a model field or name.")
        if spec.distinct and spec.function not in _DISTINCT_FUNCTIONS:
            raise tool_error("ValidationError", f"distinct is not supported for the {spec.function} aggregate.")
        resolved_path = root.resolve(spec.field)
        reject_blocked_output(resolved_path)
        scalar = scalar_field(resolved_path)
        if not isinstance(scalar, _FUNCTION_FIELDS[spec.function]):
            raise tool_error(
                "ValidationError", f"The {spec.function} aggregate does not support the type of path {spec.field!r}."
            )
        condition, _ = compiled[alias]
        validate_comparisons(spec.where, root)
        if condition is not None:
            with orm_errors():
                Query(root.model).add_q(condition)
        normalized = spec.where.model_dump(exclude_none=True) if spec.where is not None else None
        aggregates.append(_AdmittedAggregate(alias, spec.function, spec.distinct, resolved_path, condition, normalized))
    return aggregates


def _compile_having(
    request: QueryInput,
    root: AdmittedRoot,
    projection: _QueryProjection,
    group_fields: dict[str, ModelField],
    aggregate_annotations: dict[str, Any],
) -> Q | None:
    """Compile HAVING against group fields and aggregate output fields, validating it on a metadata query."""
    if request.having is None:
        return None
    with orm_errors():
        metadata_query = Query(root.model)
        for alias, expression in {**projection.annotations, **aggregate_annotations}.items():
            metadata_query.add_annotation(expression, alias)
        aggregate_fields = {alias: metadata_query.annotations[alias].output_field for alias in aggregate_annotations}
        having = _compile_having_condition(
            request.having,
            {**group_fields, **aggregate_fields},
            {path: projection.alias(path) for path in group_fields},
        )
        metadata_query.add_q(having)
    return having


def _admit_aggregates(  # pylint: disable=too-many-locals
    request: QueryInput,
    root: AdmittedRoot,
    compiled: dict[str, tuple[Q | None, Sequence[str]]],
) -> _AdmittedAggregateQuery:
    reserved_names = _reserved_names(root.model)
    for key in root.predicate_paths:
        _reject_relations(root.resolve(key), key)
    groups = []
    group_fields = {}
    for path in request.group_by or []:
        resolved = root.resolve(path)
        reject_blocked_output(resolved)
        group_fields[path] = scalar_field(resolved)
        groups.append(resolved)

    aggregates = _admit_aggregate_specs(request, root, reserved_names, compiled)

    admitted_keys = {group.path for group in groups} | {item.alias for item in aggregates}
    ordering_specs = []
    for term in request.order_by or ():
        key = term.removeprefix("-")
        if key not in admitted_keys:
            raise tool_error(
                "ValidationError", f"The ordering term {term!r} must name a group_by key or aggregate alias."
            )
        ordering_specs.append(("-" if term.startswith("-") else "", key))

    projection = _QueryProjection(root.user, reserved_names | set(request.aggregates or {}))
    for group in groups:
        projection.select(group)
    aggregate_annotations = {
        item.alias: projection.aggregate_expression(item.resolved_path, item.function, item.distinct, item.condition)
        for item in aggregates
    }
    having = _compile_having(request, root, projection, group_fields, aggregate_annotations)
    ordering = [
        prefix + (key if key in aggregate_annotations else projection.alias(key)) for prefix, key in ordering_specs
    ]
    ordered_keys = {key for _, key in ordering_specs}
    ordering.extend(projection.alias(group.path) for group in groups if group.path not in ordered_keys)
    strategy = "offset"
    shape = {
        "group_by": [group.path for group in groups],
        "aggregates": [
            {
                "alias": item.alias,
                "function": item.function,
                "field": item.resolved_path.path,
                "distinct": item.distinct,
                **({"where": item.normalized_condition} if item.normalized_condition is not None else {}),
            }
            for item in sorted(aggregates, key=lambda item: item.alias)
        ],
        **({"having": request.having.model_dump(exclude_none=True)} if request.having is not None else {}),
        "ordering": [
            *ordering_specs,
            *(("", group.path) for group in groups if group.path not in ordered_keys),
        ],
    }
    fingerprint = _fingerprint(request, root, strategy, **shape)
    continuation = QueryContinuation.bind(
        request.cursor,
        user_id=root.user.pk,
        fingerprint=fingerprint,
        strategy=strategy,
    )
    return _AdmittedAggregateQuery(
        root=root,
        limit=request.limit,
        projection=projection,
        continuation=continuation,
        aggregate_annotations=aggregate_annotations,
        having=having,
        ordering=tuple(ordering),
    )


def _admit_query_plan(
    request: QueryInput,
) -> _AdmittedListQuery | _AdmittedAggregateQuery | _AdmittedScalarQuery:
    """Admit paths, shapes, and continuation binding before root restriction."""
    compiled = {}
    request_paths: list[str] = []
    predicate_paths: list[str] = []
    if request.mode == "list":
        request_paths = [*(request.fields or ["pk"]), *(term.removeprefix("-") for term in request.order_by or [])]
    elif request.mode == "aggregate":
        request_paths.extend(request.group_by or [])
        request_paths.extend(spec.field for spec in (request.aggregates or {}).values())
        for alias, spec in (request.aggregates or {}).items():
            condition, paths = compile_condition(spec.where)
            compiled[alias] = (condition, paths)
            predicate_paths.extend(paths)
    root = admit_root(request, request_paths, predicate_paths)
    if request.mode == "list":
        return _admit_list(request, root)
    if request.mode == "aggregate":
        return _admit_aggregates(request, root, compiled)
    return _AdmittedScalarQuery(root=root, mode=request.mode, distinct=request.distinct)


def _paged(
    plan: _AdmittedListQuery | _AdmittedAggregateQuery,
    queryset: Any,
    offset: int,
    position: Callable[[Page, int], str | int] | None = None,
) -> Page:
    """Build one cursor-bearing page and serialize its projection."""
    payload = page(queryset, plan.limit, offset=offset)
    next_token = None
    if payload["truncated"]:
        next_token = plan.continuation.next_cursor(
            position(payload, offset) if position is not None else offset + payload["count"]
        )
    payload["next_cursor"] = next_token
    payload["results"] = plan.projection.serialize_rows(payload["results"])
    return payload


def _list_step(plan: _AdmittedListQuery) -> Page:
    """Execute one restricted list page from its internally admitted plan."""
    root = plan.root
    projection = plan.projection
    queryset = root.restricted_queryset()
    with orm_errors():
        queryset = queryset.annotate(**projection.annotations)
        if plan.distinct:
            for alias in projection.selected:
                output_field = queryset.query.annotations[alias].output_field
                if output_field.db_type(connection) == "json":
                    raise tool_error("ValidationError", "The projected value type cannot preserve distinct ordering.")
        if plan.order_terms:
            queryset = queryset.order_by(*plan.order_terms)
        queryset, offset = plan.continuation.resume(queryset)
        queryset = queryset.values(*projection.selected)
        if plan.distinct:
            queryset = queryset.distinct()
        return _paged(
            plan,
            queryset,
            offset,
            (lambda payload, _offset: str(payload["results"][-1][projection.alias("pk")])) if plan.keyset else None,
        )


def _aggregate_step(plan: _AdmittedAggregateQuery) -> Page:
    """Execute the fully admitted aggregate query."""
    root = plan.root
    projection = plan.projection
    queryset = root.restricted_queryset()
    with orm_errors():
        queryset = queryset.order_by().annotate(**projection.annotations)
        if projection.selected:
            grouped_queryset = queryset.values(*projection.selected)
            grouped_queryset = grouped_queryset.annotate(**plan.aggregate_annotations)
            if plan.having is not None:
                grouped_queryset = grouped_queryset.filter(plan.having)
            grouped_queryset = grouped_queryset.order_by(*plan.ordering)
            grouped_queryset, offset = plan.continuation.resume(grouped_queryset)
            return _paged(plan, grouped_queryset, offset)
        aggregate_result = queryset.aggregate(**plan.aggregate_annotations)
        return {
            "count": 1,
            "truncated": False,
            "results": projection.serialize_rows([aggregate_result]),
        }


def execute_query(request: QueryInput) -> dict[str, Any]:
    """Admit and execute a query, returning its public result payload."""
    plan = _admit_query_plan(request)
    if isinstance(plan, _AdmittedListQuery):
        return {"model": plan.root.label, **_list_step(plan)}
    if isinstance(plan, _AdmittedAggregateQuery):
        return {"model": plan.root.label, **_aggregate_step(plan)}
    queryset = plan.root.restricted_queryset()
    with orm_errors():
        if plan.mode == "count":
            count = queryset.order_by().values("pk").distinct().count() if plan.distinct else queryset.count()
            return {"model": plan.root.label, "count": count}
        return {"model": plan.root.label, "exists": queryset.exists()}
