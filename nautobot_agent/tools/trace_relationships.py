"""The trace_relationships tool and its admission/execution engine."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from string import Template
from typing import Annotated, Any

from django.db.models import Model, Prefetch, QuerySet, prefetch_related_objects
from django.db.models.fields.related import ForeignKey
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from nautobot_agent.data.access import brief_identity, brief_join_paths, identity_queryset
from nautobot_agent.data.admission import RootQueryInput, admit_related, admit_root
from nautobot_agent.data.conditions import (
    MAX_BOOLEAN_DEPTH,
    MAX_CONDITION_NODES,
    MAX_LOOKUP_ENTRIES,
    ROOT_CONDITION_LOCATION_GUIDANCE,
    ROOT_CONDITION_VALUE_ERROR_GUIDANCE,
)
from nautobot_agent.data.paths import ResolvedPath, meta, relation_by_accessor
from nautobot_agent.data.query_pagination import Page, page
from nautobot_agent.data.tool_io import (
    MIN_LIMIT,
    VALIDATION_GUIDANCE_LIMIT,
    configure_tool_errors,
    execute_data_tool,
    orm_errors,
    tool_error,
)
from nautobot_agent.runtime.context import AgentContext

TRACE_MAX_LIMIT = 5
TRACE_INCLUDE_MAX_LIMIT = 20
TRACE_MAX_DEPTH = 5
TRACE_MAX_OBJECTS = 500


class _TraceInclude(BaseModel):
    """Closed recursive relationship selection."""

    model_config = ConfigDict(extra="forbid")

    relation: StrictStr
    fields: list[StrictStr] | None = None
    limit: Annotated[StrictInt, Field(ge=MIN_LIMIT, le=TRACE_INCLUDE_MAX_LIMIT)] = TRACE_INCLUDE_MAX_LIMIT
    include: list[_TraceInclude] | None = None


class TraceInput(RootQueryInput):
    """Closed trace request; runtime is injected and omitted from the public tool schema."""

    fields: list[StrictStr] | None = None
    limit: Annotated[StrictInt, Field(ge=MIN_LIMIT, le=TRACE_MAX_LIMIT)] = TRACE_MAX_LIMIT
    include: list[_TraceInclude] | None = None


_INCLUDE_GUIDANCE = (
    "Each include is an object with a single `relation` accessor (no `__` path) and optional `fields`, `limit`, "
    "and recursive `include` list; includes do not accept `where`. "
    'Illustrative fragment: `[{"relation": "<accessor>", "fields": ["<local_field>"], "limit": 1, "include": []}]`.'
)


logger = logging.getLogger("nautobot_agent.tools")


@dataclass(frozen=True)
class TraceField:
    """A requested local field and its FK when serialized as a brief identity."""

    name: str
    attname: str
    brief_field: ForeignKey | None


@dataclass(frozen=True)
class TraceIncludePlan:
    """Validated relationship metadata and permission primitive for one requested branch."""

    accessor: str
    target_model: type[Model]
    is_collection: bool
    restrict_target: Callable[[Any, str], QuerySet]
    fields: tuple[TraceField, ...]
    limit: int
    children: tuple[TraceIncludePlan, ...]


def _resolve_trace_fields(
    model: type[Model],
    fields: Sequence[str],
    resolver: Callable[[str], ResolvedPath],
) -> tuple[TraceField, ...]:
    """Resolve local concrete fields with the caller's exact-string resolver."""
    opts = meta(model)
    projections = []
    for name in fields:
        record = resolver(name)
        concrete = record.local_field
        if concrete is None:
            raise tool_error("ValidationError", f"Projection {name!r} is not a local concrete field on {opts.label}.")
        projections.append(
            TraceField(
                name=name,
                attname=concrete.attname,
                brief_field=record.brief_field if name == concrete.name else None,
            )
        )
    return tuple(projections)


def _build_trace_plan(  # pylint: disable=too-many-locals
    model: type[Model],
    includes: Sequence[_TraceInclude],
    depth: int = 0,
) -> tuple[TraceIncludePlan, ...]:
    """Admit branches in left-to-right depth-first order; recursion is bounded to five edges."""
    plans = []
    siblings = set()
    for selection in includes:
        relation_name = selection.relation
        opts = meta(model)
        if relation_name in siblings:
            raise tool_error("UnsupportedRelationship", f"Duplicate relationship {relation_name!r}.")
        siblings.add(relation_name)
        relation = relation_by_accessor(model, relation_name)
        if relation is None:
            raise tool_error(
                "UnsupportedRelationship",
                f"{opts.label} has no supported relationship {relation_name!r}.",
            )

        names = selection.fields or ["pk"]
        admitted = admit_related(model, relation, names)
        fields = _resolve_trace_fields(
            relation.target,
            names,
            admitted.resolve,
        )
        restrict = admitted.restrict_target
        child_selections = selection.include or []
        child_depth = depth + 1
        if child_selections and child_depth >= TRACE_MAX_DEPTH:
            raise tool_error("ValidationError", f"Relationship includes cannot exceed {TRACE_MAX_DEPTH} edges.")
        children = _build_trace_plan(relation.target, child_selections, child_depth) if child_selections else ()
        plans.append(
            TraceIncludePlan(
                relation.accessor,
                relation.target,
                relation.many,
                restrict,
                fields,
                selection.limit,
                children,
            )
        )
    return tuple(plans)


_TRACE_PREFETCH_ATTRIBUTE = "_nautobot_agent_trace_related"


def _join_brief_targets(queryset: QuerySet, fields: Sequence[TraceField]) -> QuerySet:
    """Join brief targets and display relations; empty ``select_related()`` would join all non-null FKs."""
    paths = [path for field in fields if field.brief_field is not None for path in brief_join_paths(field.brief_field)]
    return queryset.select_related(*paths) if paths else queryset


def _fetch_relation_batch(instances: Sequence[Model], accessor: str, queryset: QuerySet) -> list[Any]:
    """Read ordered single targets or collection lists without retaining temporary prefetch state."""
    if not instances:
        return []
    distinct_instances = list({id(instance): instance for instance in instances}.values())
    destination = _TRACE_PREFETCH_ATTRIBUTE
    try:
        prefetch_related_objects(distinct_instances, Prefetch(accessor, queryset=queryset, to_attr=destination))
        return [getattr(instance, destination) for instance in instances]
    finally:
        for instance in distinct_instances:
            if hasattr(instance, destination):
                delattr(instance, destination)


def _fetch_collection_pages(parents: Sequence[Model], plan: TraceIncludePlan, queryset: QuerySet) -> list[Page]:
    """Fetch bounded pages for standard collection kinds using Django's partitioned sliced prefetch.

    Reverse foreign keys yield per-parent child rows. Forward and reverse many-to-many relations use Django
    related-manager membership semantics, where duplicate through rows remain repeated child occurrences.
    """
    batches = _fetch_relation_batch(parents, plan.accessor, queryset[: plan.limit + 1])
    return [
        {"count": min(len(rows), plan.limit), "truncated": len(rows) > plan.limit, "results": rows[: plan.limit]}
        for rows in batches
    ]


def _serialize_trace_fields(instance: Model, fields: Sequence[TraceField]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for field in fields:
        if field.brief_field is not None:
            serialized[field.name] = brief_identity(getattr(instance, field.name))
        else:
            serialized[field.name] = getattr(instance, field.attname)
    return serialized


def run(request: TraceInput) -> dict[str, Any]:
    """Execute one bounded trace from a validated request with schema defaults filled in."""
    names = request.fields or ["pk"]
    root = admit_root(request, names)
    fields = _resolve_trace_fields(root.model, names, root.resolve)
    includes = _build_trace_plan(root.model, request.include or [])
    queryset = _join_brief_targets(root.restricted_queryset(), fields)
    with orm_errors():
        batch = page(queryset.order_by("pk").distinct(), request.limit)
    execution = _TraceExecution(root.user)
    execution.charge(batch["count"])
    results = execution.prepare(batch["results"], fields, includes)
    return {"model": root.label, "count": batch["count"], "truncated": batch["truncated"], "results": results}


class _TraceExecution:
    """Request-local emitted-occurrence budget and batched relationship traversal."""

    def __init__(self, user: Any):
        self.user = user
        self.returned_count = 0

    def charge(self, count: int) -> None:
        """Accept emitted occurrences before preparing their descendants."""
        self.returned_count += count
        if self.returned_count > TRACE_MAX_OBJECTS:
            raise tool_error(
                "TraceLimitExceeded",
                f"Trace would return more than the {TRACE_MAX_OBJECTS}-object cap. "
                "Narrow the filters, lower the root or include limits, or request fewer includes.",
            )

    def prepare(
        self, instances: Sequence[Model], fields: Sequence[TraceField], includes: Sequence[TraceIncludePlan]
    ) -> list[dict[str, Any]]:
        """Build output dictionaries from complete sibling batches, preserving distinct occurrences."""
        if not instances:
            return []
        results = [{"fields": _serialize_trace_fields(instance, fields), "relationships": {}} for instance in instances]
        for include in includes:
            restricted = include.restrict_target(self.user, "view").order_by("pk")
            queryset = _join_brief_targets(restricted, include.fields)
            if include.is_collection:
                self.prepare_collection(instances, results, include, queryset)
            else:
                self.prepare_single(instances, results, include, queryset)
        return results

    def prepare_single(
        self,
        instances: Sequence[Model],
        results: Sequence[dict[str, Any]],
        include: TraceIncludePlan,
        queryset: QuerySet,
    ) -> None:
        """Batch visible singles and probe only unresolved identity markers."""
        visible = _fetch_relation_batch(instances, include.accessor, queryset)
        unresolved = [index for index, related in enumerate(visible) if related is None]
        if unresolved:
            probes = _fetch_relation_batch(
                [instances[index] for index in unresolved], include.accessor, identity_queryset(include.target_model)
            )
            for index, probe in zip(unresolved, probes, strict=True):
                identity = brief_identity(probe)
                results[index]["relationships"][include.accessor] = (
                    {**identity, "restricted": True} if identity is not None else None
                )
            self.charge(sum(probe is not None for probe in probes))
        children = [related for related in visible if related is not None]
        self.charge(len(children))
        prepared = iter(self.prepare(children, include.fields, include.children))
        for result, related in zip(results, visible, strict=True):
            if related is not None:
                result["relationships"][include.accessor] = next(prepared)

    def prepare_collection(
        self,
        instances: Sequence[Model],
        results: Sequence[dict[str, Any]],
        include: TraceIncludePlan,
        queryset: QuerySet,
    ) -> None:
        """Combine accepted pages before descending into any child's includes."""
        pages = _fetch_collection_pages(instances, include, queryset)
        self.charge(sum(collection["count"] for collection in pages))
        children = [child for collection in pages for child in collection["results"]]
        prepared = self.prepare(children, include.fields, include.children)
        offset = 0
        for result, collection in zip(results, pages, strict=True):
            count = collection["count"]
            result["relationships"][include.accessor] = {
                "model": meta(include.target_model).label,
                "count": count,
                "truncated": collection["truncated"],
                "results": prepared[offset : offset + count],
            }
            offset += count


def trace_relationships(model: str, runtime: ToolRuntime[AgentContext], **arguments: Any) -> str:  # noqa: D417
    """Trace explicit nested relationships in live Nautobot data, restricting related rows.

    Use `describe_model` to discover stored fields and standard relationships. Use each relation's `accessor` for
    `include` entries; use installed source only for implementation semantics or custom/omitted extensions not
    captured by metadata. Includes support standard forward/reverse FK, OneToOne, and M2M only, at most ${trace_depth}
    relationship edges.
    Joined projections, arbitrary properties, generic/custom relations, and duplicate sibling includes are
    rejected. FK names in `fields` return brief identities (`id`, `display`) regardless of related permission;
    FK attnames return stored identifiers. These local identities remain available under administrator policy;
    explicit includes targeting a blocked model are denied, including default or primary-key-only selections.

    Roots require model view permission and return model/count/truncated/results; each object has fields/relationships
    maps. Collections return the same metadata.
    Singles return an object envelope or null. A hidden single returns its brief identity with
    `restricted: true` and no nested details; an unset single is null. Collections omit hidden rows.
    Collections use one untraversed lookahead row for truncation; results are ordered by pk and roots deduplicated.
    A call may return at most ${trace_objects} objects in total (roots, collection rows, and non-null singles);
    exceeding that fails the whole call, so use small explicit limits. A permitted empty result means "No matching records were
    found within your access"; null/empty relationships mean no visible match, not proof of absence or a diagnosis
    of constraints.
    Errors fail the whole call; never claim partial branches succeeded. The configured `query.max_page_bytes` ceiling
    applies to the complete serialized UTF-8 response, including escaped output. An oversized trace returns no
    results; retry with narrower root/include limits or fewer selected fields, not a cursor. Inspect installed source
    only when implementation evidence is needed to explain the cause. Validation details include fixed repair guidance
    on the first ${guidance_limit} errors; apply it directly without a compulsory source read.
    A top-level note marks the cutoff.
    Optional root `where` uses the shared bounded `lookups`/`and`/`or`/`not`/`compare` grammar.
    Comparisons use compatible stored scalar fields and forward FK/OneToOne paths only; Boolean/UUID
    comparisons support exact only. It is combined with `filters` in one native filter, then `exclude` applies.
    Conditions are not available on include nodes.
    Django Q is not a correlated related-row quantifier. Positive conjunctions through a shared multi-valued
    path in one filter use native join semantics; negation/exclusion is not a custom same-child NOT EXISTS.
    Use child roots for combined child properties. Bounds are depth ${boolean_depth}, ${lookup_entries} lookup
    entries, and ${condition_nodes} condition nodes; invalid shapes/bounds are handled validation errors,
    not SQL cost or runtime guarantees.

    Args:
        model: Root model name as app_label.ModelName.
        runtime: Injected trusted run context; not a public argument.
        filters: Optional root Django lookup dictionary; filter-only joins need no separate model grants.
        exclude: Optional root Django exclusion dictionary.
        fields: Local concrete field names or FK attnames; empty/null defaults to pk.
        where: Optional bounded root condition using lookups/and/or/not/compare. Comparison lookups are
            exact/gt/gte/lt/lte, with two stored scalar paths. It cannot be used in include nodes.
        limit: Strict integer ${minimum} to ${trace_limit}, default ${trace_limit}; root collection bound.
        include: Recursive list of objects with relation (single accessor, no __ path), optional local fields,
            limit (strict integer ${minimum} to ${include_limit}, default ${include_limit}; collection effect only),
            and optional nested include.
            Empty/null means no traversal. A single-valued relation does not use its limit.
    """
    return execute_data_tool(
        lambda: run(TraceInput.model_construct(model=model, runtime=runtime, **arguments)),
        logger=logger,
        failure_message="Relationship trace of %r failed with a database error",
        model=model,
    )


trace_relationships: BaseTool = tool(
    args_schema=TraceInput,
    description=Template(trace_relationships.__doc__ or "").substitute(
        trace_depth=TRACE_MAX_DEPTH,
        trace_objects=TRACE_MAX_OBJECTS,
        guidance_limit=VALIDATION_GUIDANCE_LIMIT,
        boolean_depth=MAX_BOOLEAN_DEPTH,
        lookup_entries=MAX_LOOKUP_ENTRIES,
        condition_nodes=MAX_CONDITION_NODES,
        minimum=MIN_LIMIT,
        trace_limit=TRACE_MAX_LIMIT,
        include_limit=TRACE_INCLUDE_MAX_LIMIT,
    ),
)(trace_relationships)


configure_tool_errors(
    trace_relationships,
    location_guidance={"include": _INCLUDE_GUIDANCE, **ROOT_CONDITION_LOCATION_GUIDANCE},
    value_error_guidance=ROOT_CONDITION_VALUE_ERROR_GUIDANCE,
)
