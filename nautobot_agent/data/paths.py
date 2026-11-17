"""Django model-metadata helpers shared by trace, query planning, and path policy."""

from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal, cast

from django.core.exceptions import FieldDoesNotExist
from django.db.models import Field, Model
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.related import ForeignKey, ManyToManyField, OneToOneField
from django.db.models.fields.reverse_related import ManyToManyRel, ManyToOneRel, OneToOneRel

from nautobot_agent.data.tool_io import tool_error

RELATION_PATH_MESSAGE = (
    "The path {path!r} must use stored fields and forward ForeignKey or OneToOne relations only; "
    "reverse, many-valued, generic, and other relations are not allowed in aggregate paths or comparison operands. "
    "For parent counts, group the child query by its stored forward parent ID, then join those counts "
    "to paginated parent rows in Python."
)

RelationKind = Literal[
    "one_to_one",
    "foreign_key",
    "many_to_many",
    "reverse_one_to_one",
    "reverse_foreign_key",
    "reverse_many_to_many",
]


@dataclass(frozen=True)
class StandardRelation:
    """One standard Django relation (or its reverse) with a model target."""

    kind: RelationKind
    accessor: str
    query_name: str
    many: bool
    target: type[Model]


@dataclass(frozen=True)
class ForwardHop:
    """A stored forward FK traversed before the terminal path field."""

    stored_path: str
    field: ForeignKey


@dataclass(frozen=True)
class ResolvedPath:  # pylint: disable=too-many-instance-attributes
    """Resolved path facts kept together to avoid repeating Django metadata traversal."""

    path: str
    _steps: tuple[tuple[int, Any, Any], ...] = dataclass_field(repr=False, compare=False)
    parts: tuple[str, ...]
    crosses_unsupported_relation: bool
    brief_field: Any | None
    stored_path: str
    forward_hops: tuple[ForwardHop, ...]
    local_field: Any | None

    def iter_fields(self) -> Iterator[Any]:
        """Yield resolved Django fields in traversal order, stopping at the first unresolved step."""
        for _, _, field in self._steps:
            if field is None:
                break
            yield field

    def stored_scalar(self) -> Field:
        """Resolve a supported stored scalar, preserving first-failure path diagnostics."""
        for index, opts, field in self._steps:
            part = self.parts[index]
            if field is None:
                raise tool_error(
                    "ValidationError", f"The path {self.path!r} does not resolve to a stored field of {opts.label}."
                )
            if not isinstance(field, Field) or not field.concrete or (field.is_relation and not _is_forward_fk(field)):
                raise tool_error("ValidationError", RELATION_PATH_MESSAGE.format(path=self.path))
            if index < len(self.parts) - 1:
                if not _is_forward_fk(field) or part not in (field.name, "pk"):
                    raise tool_error("ValidationError", RELATION_PATH_MESSAGE.format(path=self.path))
                continue
            scalar = field
            while scalar.is_relation:
                scalar = scalar.target_field
            return scalar
        raise tool_error("ValidationError", f"The path {self.path!r} does not resolve to a stored field.")


def _is_forward_fk(field: Any) -> bool:
    return isinstance(field, ForeignKey) and field.concrete


def resolve_path(model: type[Model], path: str) -> ResolvedPath:
    """Walk a path once and retain metadata facts without raising for unresolved parts."""
    parts = tuple(path.split(LOOKUP_SEP))
    steps = tuple(_walk_path(model, path))
    crosses_unsupported_relation = any(
        field is not None and field.is_relation and not _is_forward_fk(field) for _, _, field in steps
    )
    terminal_index, _, terminal_field = next(
        (step for step in reversed(steps) if step[2] is not None), (-1, None, None)
    )
    brief_field = (
        terminal_field if isinstance(terminal_field, ForeignKey) and parts[-1] == terminal_field.name else None
    )
    forward_hops = tuple(
        ForwardHop(LOOKUP_SEP.join([*parts[:index], field.attname]), field)
        for index, _, field in steps
        if index < terminal_index and _is_forward_fk(field)
    )
    local_field = None
    # Reverse relations have no ``attname``; the concrete-field check must come first.
    if len(parts) == 1 and terminal_field is not None and terminal_field in meta(model).concrete_fields:
        names_field = parts[0] in (terminal_field.name, terminal_field.attname) or (
            parts[0] == "pk" and terminal_field.primary_key
        )
        if names_field:
            local_field = terminal_field
    # The walk stops at the first unresolved part, so a resolved final step means the whole path resolved.
    stored_parts = list(parts)
    last_index, _, last_field = steps[-1]
    if last_index == len(parts) - 1 and _is_forward_fk(last_field):
        stored_parts[-1] = cast(str, last_field.attname)
    return ResolvedPath(
        path,
        steps,
        parts,
        crosses_unsupported_relation,
        brief_field,
        LOOKUP_SEP.join(stored_parts),
        forward_hops,
        local_field,
    )


def _walk_path(model: type[Model], path: str) -> Iterator[tuple[int, Any, Any]]:
    """Yield ``(index, opts, field)`` for each ``LOOKUP_SEP`` part of ``path`` exactly as given.

    ``opts`` is the ``_meta`` of the model the part is looked up on and ``pk`` maps to its primary key. A part
    that does not resolve yields ``field=None`` and ends the walk; after a field without ``related_model`` the
    next part yields ``opts=None, field=None`` and ends. Traversal follows ``field.related_model._meta``.
    """
    opts = meta(model)
    for index, part in enumerate(path.split(LOOKUP_SEP)):
        if opts is None:
            yield index, None, None
            return
        try:
            field = opts.get_field(opts.pk.name if part == "pk" else part)
        except FieldDoesNotExist:
            yield index, opts, None
            return
        yield index, opts, field
        related_model = getattr(field, "related_model", None)
        opts = related_model._meta if related_model is not None else None


# First match wins: OneToOneField subclasses ForeignKey and OneToOneRel subclasses ManyToOneRel. The second
# element, when set, is the forward field class the reverse relation's ``field`` must be.
_RELATION_KINDS: tuple[tuple[type, type | None, RelationKind, bool], ...] = (
    (OneToOneField, None, "one_to_one", False),
    (ForeignKey, None, "foreign_key", False),
    (ManyToManyField, None, "many_to_many", True),
    (OneToOneRel, OneToOneField, "reverse_one_to_one", False),
    (ManyToOneRel, ForeignKey, "reverse_foreign_key", True),
    (ManyToManyRel, ManyToManyField, "reverse_many_to_many", True),
)


def standard_relation(field: Any) -> StandardRelation | None:
    """Classify ``field`` as a standard relation, or ``None`` for nonstandard relations and non-model targets."""
    classified = next(
        (
            (kind, many)
            for field_class, forward_class, kind, many in _RELATION_KINDS
            if isinstance(field, field_class) and (forward_class is None or isinstance(field.field, forward_class))
        ),
        None,
    )
    if classified is None:
        return None
    kind, many = classified
    target = getattr(field, "related_model", None)
    if not (isinstance(target, type) and issubclass(target, Model)):
        return None
    if isinstance(field, OneToOneRel | ManyToOneRel | ManyToManyRel):
        accessor = field.get_accessor_name()
        if not isinstance(accessor, str) or not accessor:
            return None
    else:
        accessor = field.name
    return StandardRelation(kind=kind, accessor=accessor, query_name=field.name, many=many, target=target)


def meta(model: type[Model]) -> Any:
    """Return Django's dynamically-created metadata for a model class."""
    return cast(Any, model)._meta


def relation_by_accessor(model: type[Model], accessor: str) -> StandardRelation | None:
    """Return the first supported relation with this accessor in Django metadata order."""
    for field in meta(model).get_fields():
        relation = standard_relation(field)
        if relation is not None and relation.accessor == accessor:
            return relation
    return None
