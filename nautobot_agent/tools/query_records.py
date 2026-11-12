"""Validated query schemas and transport adapters for live-data results."""

from __future__ import annotations

import logging
from inspect import cleandoc
from string import Template
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import Field, StrictInt

from nautobot_agent.data.conditions import (
    MAX_BOOLEAN_DEPTH,
    MAX_CONDITION_NODES,
    MAX_LOOKUP_ENTRIES,
    QUERY_CONDITION_GRAMMAR_GUIDANCE,
    QUERY_CONDITION_LIMIT_GUIDANCE,
    ROOT_CONDITION_LOCATION_GUIDANCE,
    ROOT_CONDITION_VALUE_ERROR_GUIDANCE,
)
from nautobot_agent.data.query import DEFAULT_LIMIT, execute_query
from nautobot_agent.data.query import QueryInput as _QueryInput
from nautobot_agent.data.query_pagination import CURSOR_LIFETIME_SECONDS
from nautobot_agent.data.tool_io import (
    MIN_LIMIT,
    VALIDATION_GUIDANCE_LIMIT,
    configure_tool_errors,
    execute_data_tool,
)
from nautobot_agent.runtime.context import AgentContext

MAX_LIMIT = 100
INTERPRETER_MAX_LIMIT = 1000


class DirectQueryInput(_QueryInput):
    """Shared query grammar with the direct-tool row cap."""

    limit: Annotated[StrictInt, Field(ge=MIN_LIMIT, le=MAX_LIMIT)] = DEFAULT_LIMIT


class InterpreterQueryInput(_QueryInput):
    """Shared query grammar with the larger Monty-only row cap."""

    limit: Annotated[StrictInt, Field(ge=MIN_LIMIT, le=INTERPRETER_MAX_LIMIT)] = DEFAULT_LIMIT


_AGGREGATE_GUIDANCE = (
    'Use a map from alias to a `{"function","field","distinct","where"}` object; function is `Count`, '
    "`Sum`, `Avg`, `Min`, or `Max`, field is a stored scalar path, distinct is boolean (supported only "
    "for `Count`, `Sum`, and `Avg`), and optional where is a query condition local to that aggregate. Aliases "
    "start with an ASCII letter and contain ASCII letters and digits with optional single internal underscores; "
    "they cannot end in an underscore or be model fields, relations, `pk`, or group keys. For parent counts, "
    "aggregate children grouped by their stored forward parent ID, then join those counts to paginated parents "
    "in Python. Aggregate predicates, group keys, and input paths must not traverse reverse, many-valued, or "
    "generic relations. "
    'Illustrative fragment: `{"ExampleAlias": {"function": "Count", "field": "<stored_path>", "distinct": false}}`.'
)


logger = logging.getLogger("nautobot_agent.tools")


def _query_records(model: str, runtime: ToolRuntime[AgentContext], **arguments: Any) -> str:  # noqa: D417
    """Query live Nautobot data, restricted to objects the requesting user may view.

    Read-only. Resolve the model by `app_label.ModelName` (for example `dcim.Device`). Use `describe_model` for
    stored fields and standard relationships; use relationship `query_name` for Django ORM lookup paths.
    Errors are returned as JSON with the Django message; apply validation repair guidance directly. Inspect
    installed source for implementation semantics or omitted extensions, or when implementation evidence is
    needed to explain an error.
    Root view permission is required; native permission denial names the model and permission. Related filters, excludes, and `where`
    leaves require unrestricted related-model view permission beyond foreign-key identifiers and null checks.
    Administrator policy may deny selected output details and aggregate inputs; denied values are not silently
    masked. This does not prohibit native relationship filters, ordering, or parent summaries by stored local
    foreign-key identity. Projected, ordered, grouped, or aggregated scalar values behind a related hop are
    visible-or-null per hop; null can mean unset or not visible.
    A terminal foreign-key name returns a brief identity regardless of target permission, while its attname returns
    the stored identifier. Use trace_relationships when per-model restriction markers must distinguish hidden
    related objects from unset relations. Describe an empty permitted result as "No matching records were found
    within your access", not global absence or a diagnosis of constraints. Report truncation.
    List and grouped-aggregate queries use the adapter-specific limit range and default stated above. Successful
    pages include `next_cursor`; reuse the same query arguments with the previous cursor until it is null,
    optionally changing the limit. Cursors expire ${cursor_lifetime} seconds after the first page. Each adapter
    enforces its own schema maximum.
    Optional `where` is a root predicate in every mode; each node is exactly one non-null `lookups`, `and`, `or`,
    `not`, or `compare`. Comparison operands are stored scalar paths using exact/gt/gte/lt/lte and
    SQL three-valued null semantics; root/local predicates retain native related-filter behavior. Boolean groups
    nest to depth ${boolean_depth}; each tree allows at most ${lookup_entries} lookup/path entries and
    ${condition_nodes} nodes. `where` and `filters` are combined in one native filter; `exclude` follows.
    Bounds constrain expression size, not query cost or runtime.
    Invalid structures and bounds are handled validation errors. Validation details include fixed repair `guidance`
    on the first ${guidance_limit} errors; a top-level note marks the cutoff. Aggregate-local conditions affect
    only their own input expression. Grouped `having` accepts only declared group paths and aggregate aliases
    and sees projected visible-or-null values; it is applied before ordering and pagination. Negated non-null
    annotation comparisons exclude null/masked values, while explicit `isnull` can select them.

    For any whole-population summary, ranking, or top-N question use `aggregate` mode: the database computes
    every group over all matching visible objects, so never rank or total from a limited `list`. Aggregate
    values cover the complete groups returned; groups beyond `limit` are omitted (`truncated`), which does not
    make returned values partial. Decimal results use JSON-number encoding and can lose precision.
    For child counts by parent, aggregate the child root grouped by its stored forward parent ID, then join those
    counts to paginated parents in code. In aggregate mode, root `filters`, `exclude`, and `where` leaves,
    aggregate-local `where`, group keys, and inputs allow stored fields and forward ForeignKey/OneToOne hops only,
    never reverse, many-valued, or generic traversal.

    Args:
        cursor: Optional signed continuation token for list queries and grouped aggregate queries only.
        model: Model name as `app_label.ModelName`.
        runtime: Injected trusted run context; not a public argument.
        filters: Django filter keyword lookups (for example `{"name__icontains": "edge"}`), or null.
        exclude: Django exclude keyword lookups, or null.
        where: Optional bounded root condition composed with filters before exclude; null omits it. Each node is
            exactly one of lookups, and, or, not, or compare with strict stored-path operands.
            Maximum depth ${boolean_depth}, ${lookup_entries} lookup/path entries, and ${condition_nodes} nodes total.
        having: Grouped aggregate mode only. A condition on declared group_by names and aggregate aliases; literal
            lookups allow one exact/in/gt/gte/lt/lte/range/isnull suffix. Comparisons use output aliases. Runs
            before ordering/pagination and accepts no raw model path.
        fields: List mode only. Stored scalar paths to return; empty or null returns only `pk`. Raw binary
            contents are rejected; Nautobot IP address fields remain supported. A terminal forward
            foreign-key name path (including traversals ending in a foreign-key name) returns a brief identity
            `{"id": "<target pk>", "display": "<label>"}`; its attname or `pk` returns the stored identifier.
            Administrator policy preserves bare local foreign-key names/attnames; paths that retrieve or traverse
            blocked record details may be denied rather than masked. Otherwise related hops before the terminal
            field are visible-or-null under related-object access. Reverse and many-valued paths are rejected;
            use `trace_relationships` for related collections. Null is required in aggregate mode.
        order_by: Django ordering paths; prefix with `-` for descending. Used by list mode and, naming only group
            keys or aggregate aliases, by grouped aggregate mode; ignored by count/exists; rejected for a
            whole-query aggregate summary. Grouped rows use group keys ascending as a final deterministic tie-break.
            With list `distinct`, every term must be exactly a projected path. List ordering rejects reverse/many-
            valued paths; use `trace_relationships` for related collections.
        distinct: Strict boolean, default false; true is rejected in aggregate mode (use per-aggregate `distinct`).
            In list mode returns unique tuples of the projected fields (default `pk`) and clears inherited ordering;
            `order_by` may only name projected paths. A foreign-key name orders by its stored identifier, both with
            `distinct` and in ordinary list queries. Count mode counts distinct matching root primary keys, ignoring
            `fields` and `order_by`; exists ignores it. Without distinct, joins that repeat a root repeat its
            rows/count.
        limit: Strict integer within the adapter-specific inclusive range stated above; list rows or aggregate
            groups returned at most (ignored by count/exists).
        mode: `list` returns rows (count is rows returned, `truncated` shows more exist), `count` returns the
            number of matching objects, `exists` returns whether any exist, and `aggregate` returns grouped (or one
            whole-query) summaries computed by the database over every matching visible root.
        group_by: Aggregate mode only. Unique stored scalar paths (`pk`, fields, forward foreign-key paths); null or
            empty gives one whole-query summary row. Null group values form their own group. Related hops before a
            grouped scalar are visible-or-null, so inaccessible targets collapse into a null group. A terminal
            foreign-key name returns a brief identity `{"id": "<target pk>", "display": "<label>"}` regardless
            of target permission; a foreign-key attname or `pk` groups by the stored identifier. Administrator
            policy may deny returned related details, but does not prohibit local identity grouping or native
            relationship filtering and ordering.
        aggregates: Aggregate mode only; required. Maps an alias (starting with an ASCII letter and containing ASCII
            letters and digits with optional single internal underscores, but no trailing underscore; not a model
            field, relation, `pk`, or group key) to `{"function": F, "field": path, "distinct": bool,
            "where": condition}`, where F is exactly `Count`, `Sum`, `Avg`, `Min`, or `Max`, and optional `where`
            affects only that aggregate's inputs. Administrator policy may deny detail-valued aggregate inputs;
            terminal local foreign-key identities remain usable. Grouping and `Count` accept stored numeric, boolean, text,
            date/datetime/time, and UUID values, including foreign-key stored values. JSON, binary, duration, and
            other scalar families are excluded. `Count` counts non-null inputs; `pk` counts every root. `Sum`/`Avg`
            need integer, float, or Decimal paths; `Min`/`Max` those plus text, date/datetime/time. Per-aggregate
            `distinct` (Count/Sum/Avg only) collapses equal input values, even from different objects; it is not a
            sum over distinct objects. An empty summary gives `Count` 0 and null for the others. Related hops
            before the terminal input field are visible-or-null; hidden values contribute null, so `Count` excludes
            them and other functions ignore them. A terminal foreign-key input uses its stored identifier without
            gating the terminal relation. Aggregate mode rejects non-null `fields` and top-level `distinct=True`.
    """
    # Each trusted tool schema has already validated its own cap and the shared grammar.
    # Construct the domain request without revalidating admitted arguments.
    return execute_data_tool(
        lambda: execute_query(_QueryInput.model_construct(model=model, runtime=runtime, **arguments)),
        logger=logger,
        failure_message="Live-data query against %r failed with a database error",
        model=model,
    )


_QUERY_RECORDS_SHARED_DESCRIPTION = Template(cleandoc(_query_records.__doc__ or "")).substitute(
    cursor_lifetime=CURSOR_LIFETIME_SECONDS,
    boolean_depth=MAX_BOOLEAN_DEPTH,
    lookup_entries=MAX_LOOKUP_ENTRIES,
    condition_nodes=MAX_CONDITION_NODES,
    guidance_limit=VALIDATION_GUIDANCE_LIMIT,
)
query_records, interpreter_query_records = (
    StructuredTool.from_function(
        _query_records,
        name="query_records",
        args_schema=schema,
        description=(
            f"{adapter}: inclusive strict-integer limit range {MIN_LIMIT:,}-{maximum:,} "
            f"(default {DEFAULT_LIMIT:,}) rows/groups per page. {_QUERY_RECORDS_SHARED_DESCRIPTION}"
        ),
    )
    for schema, adapter, maximum in (
        (DirectQueryInput, "Direct adapter", MAX_LIMIT),
        (InterpreterQueryInput, "Monty adapter", INTERPRETER_MAX_LIMIT),
    )
)


_LOCAL_CONDITION_GUIDANCE = (
    "This is a condition local to the named aggregate: use the query condition grammar and keep it attached to "
    "that aggregate; it does not filter the root population."
)
_HAVING_GUIDANCE = (
    "This is a grouped-result condition: use declared group_by paths and aggregate aliases only, and keep it "
    "attached to having; it filters groups after aggregation, not root rows."
)


_QUERY_VALIDATION_ERROR_GUIDANCE = {
    "location_guidance": {"aggregates": _AGGREGATE_GUIDANCE, **ROOT_CONDITION_LOCATION_GUIDANCE},
    "value_error_guidance": dict[tuple[str | int | None, ...], dict[str, str]](
        {
            **ROOT_CONDITION_VALUE_ERROR_GUIDANCE,
            ("having",): QUERY_CONDITION_LIMIT_GUIDANCE,
            ("aggregates", None, "where"): QUERY_CONDITION_LIMIT_GUIDANCE,
        }
    ),
    "scoped_guidance": dict[tuple[str | int | None, ...], str](
        {
            ("having",): _HAVING_GUIDANCE + " " + QUERY_CONDITION_GRAMMAR_GUIDANCE,
            ("aggregates", None, "where"): _LOCAL_CONDITION_GUIDANCE + " " + QUERY_CONDITION_GRAMMAR_GUIDANCE,
        }
    ),
}
for _query_tool in (query_records, interpreter_query_records):
    configure_tool_errors(_query_tool, **_QUERY_VALIDATION_ERROR_GUIDANCE)
