"""Owns the bounded boolean grammar and model-facing repair guidance for live-data query and trace tools."""

from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal, cast, overload

from django.db.models import F, Q
from django.db.models.lookups import Exact, GreaterThan, GreaterThanOrEqual, LessThan, LessThanOrEqual
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, StrictStr, model_validator

MAX_BOOLEAN_DEPTH = 5
MAX_LOOKUP_ENTRIES = 50
MAX_CONDITION_NODES = 100
CONDITION_DEPTH_LIMIT_MESSAGE = f"A where condition cannot exceed boolean depth {MAX_BOOLEAN_DEPTH}."
CONDITION_LOOKUP_ENTRIES_LIMIT_MESSAGE = (
    f"A where condition cannot contain more than {MAX_LOOKUP_ENTRIES} lookup entries."
)
CONDITION_NODES_LIMIT_MESSAGE = f"A where condition cannot contain more than {MAX_CONDITION_NODES} nodes."
QUERY_CONDITION_GRAMMAR_GUIDANCE = (
    "Each condition object must contain exactly one non-null key: a nonempty `lookups` dictionary whose "
    "entries are ANDed, a nonempty `and` or `or` list, one `not` condition, or `compare` with strict-string "
    "`left` and `right` paths and `lookup` set to exact, gt, gte, lt, or lte. Conditions retain the shared bounds "
    f"of depth {MAX_BOOLEAN_DEPTH}, {MAX_LOOKUP_ENTRIES} lookup entries (comparison uses two), and "
    f"{MAX_CONDITION_NODES} nodes."
)
CONDITION_LIMIT_GUIDANCE = {
    CONDITION_DEPTH_LIMIT_MESSAGE: (
        "Flatten an `and` directly inside an `and` (or an `or` directly inside an `or`) into the parent's list, "
        "or replace a single-child `and`/`or` with that child; both preserve meaning and remove one nesting level. "
        "If the request cannot be expressed within the bounds, report that to the user instead of removing conditions."
    ),
    CONDITION_NODES_LIMIT_MESSAGE: (
        "AND-ed sibling `lookups` objects may be merged into one `lookups` dictionary only when all their keys "
        "are distinct; never merge objects with a shared key, because that would drop a predicate. "
        "If the request cannot be expressed within the bounds, report that to the user instead of removing conditions."
    ),
    CONDITION_LOOKUP_ENTRIES_LIMIT_MESSAGE: (
        "Reduce lookup entries only when every predicate remains; comparison nodes consume two entries. "
        "Entries of top-level AND-ed `lookups` objects may move into `filters` only when each key is absent "
        "from `filters` and every other moved entry; never move `or`/`not` conjuncts or a duplicate key."
    ),
}
ROOT_CONDITION_LOCATION_GUIDANCE = {"where": QUERY_CONDITION_GRAMMAR_GUIDANCE}
ROOT_CONDITION_VALUE_ERROR_GUIDANCE: dict[tuple[str | int | None, ...], dict[str, str]] = {
    ("where",): CONDITION_LIMIT_GUIDANCE
}


def _precheck_condition_tree(value: Any) -> Any:  # pylint: disable=too-cognitively-complex  # one bounded walk enforcing three limits in document order
    """Bound raw condition structure before recursive model validation."""
    # Children are pushed reversed so nodes are visited in document order (left-to-right preorder).
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = entries = 0
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        nodes += 1
        if nodes > MAX_CONDITION_NODES:
            raise ValueError(CONDITION_NODES_LIMIT_MESSAGE)
        if isinstance(node.get("lookups"), dict):
            entries += len(node["lookups"])
        if isinstance(node.get("compare"), dict):
            entries += 2
        if entries > MAX_LOOKUP_ENTRIES:
            raise ValueError(CONDITION_LOOKUP_ENTRIES_LIMIT_MESSAGE)
        for operator in ("and", "or", "not"):
            if operator not in node:
                continue
            if depth + 1 > MAX_BOOLEAN_DEPTH:
                raise ValueError(CONDITION_DEPTH_LIMIT_MESSAGE)
            children = node[operator]
            if operator == "not":
                children = [children]
            elif not isinstance(children, list):
                continue
            stack.extend((child, depth + 1) for child in reversed(children))
    return value


QUERY_CONDITION_LIMIT_GUIDANCE = {
    **CONDITION_LIMIT_GUIDANCE,
    CONDITION_LOOKUP_ENTRIES_LIMIT_MESSAGE: (
        "Reduce lookup entries only when every predicate remains; comparison nodes consume two entries. Do not move "
        "aggregate-local or grouped conditions into root filters."
    ),
}


class FieldComparison(BaseModel):
    """Explicit stored-field comparison."""

    model_config = ConfigDict(extra="forbid")
    left: StrictStr
    lookup: Literal["exact", "gt", "gte", "lt", "lte"]
    right: StrictStr


class _QueryCondition(BaseModel):
    """Shared recursive boolean grammar including field comparisons."""

    model_config = ConfigDict(extra="forbid")
    lookups: dict[StrictStr, JsonValue] | None = None
    and_: list["_QueryCondition"] | None = Field(default=None, alias="and", min_length=1)
    or_: list["_QueryCondition"] | None = Field(default=None, alias="or", min_length=1)
    not_: "_QueryCondition | None" = Field(default=None, alias="not")
    compare: FieldComparison | None = None

    @model_validator(mode="after")
    def _check_operator(self) -> "_QueryCondition":
        supplied = self.model_fields_set
        if len(supplied) != 1 or getattr(self, next(iter(supplied))) is None:
            raise ValueError("A condition must contain exactly one non-null operator: lookups, and, or, not, compare.")
        if self.lookups is not None and not self.lookups:
            raise ValueError("lookups must contain at least one entry.")
        return self


QueryConditionInput = Annotated[_QueryCondition, BeforeValidator(_precheck_condition_tree)]


def _compile_condition_node(node: _QueryCondition, paths: list[str], rewrite: Callable[[str], str]) -> Q:
    if node.lookups is not None:
        paths.extend(node.lookups)
        return Q(*((rewrite(key), value) for key, value in node.lookups.items()))
    if node.compare is not None:
        paths.extend((node.compare.left, node.compare.right))
        lookup = {
            "exact": Exact,
            "gt": GreaterThan,
            "gte": GreaterThanOrEqual,
            "lt": LessThan,
            "lte": LessThanOrEqual,
        }[node.compare.lookup]
        return Q(lookup(F(rewrite(node.compare.left)), F(rewrite(node.compare.right))))
    children_nodes = node.and_ if node.and_ is not None else node.or_
    if children_nodes is not None:
        connector = Q.AND if node.and_ is not None else Q.OR
        children = [_compile_condition_node(child, paths, rewrite) for child in children_nodes]
        return Q(*children, _connector=connector)
    return ~_compile_condition_node(cast(_QueryCondition, node.not_), paths, rewrite)


@overload
def compile_condition(condition: _QueryCondition, rewrite: Callable[[str], str] = ...) -> tuple[Q, list[str]]: ...


@overload
def compile_condition(condition: None, rewrite: Callable[[str], str] = ...) -> tuple[None, list[str]]: ...


def compile_condition(
    condition: _QueryCondition | None, rewrite: Callable[[str], str] = lambda path: path
) -> tuple[Q | None, list[str]]:
    """Compile a condition and return every model/operand path in traversal order."""
    if condition is None:
        return None, []
    paths: list[str] = []
    return _compile_condition_node(condition, paths, rewrite), paths


def iter_condition_nodes(condition: _QueryCondition | None) -> Iterator[_QueryCondition]:
    """Iterate query nodes in boolean traversal order without changing the tree."""
    if condition is None:
        return
    stack = [condition]
    while stack:
        node = stack.pop()
        yield node
        if node.not_ is not None:
            stack.append(node.not_)
        children = node.and_ if node.and_ is not None else node.or_
        if children is not None:
            stack.extend(reversed(children))
