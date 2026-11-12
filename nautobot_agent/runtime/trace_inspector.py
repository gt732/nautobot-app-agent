"""Database-backed loading and pure projection for the trace inspector."""

import json
import uuid
from typing import TypedDict, cast

from nautobot_agent.models import AgentExecutionSpan, ExecutionStatus
from nautobot_agent.runtime.trace_payloads import (
    SUMMARY_DETAIL_KEYS,
    json_encoder,
    summarize_spans,
)

TABS = (
    ("io", "Input / Output"),
    ("metadata", "Metadata"),
    ("timing", "Timing"),
    ("tokens", "Tokens"),
    ("logs", "Logs"),
)

STATUS_LABELS = dict(ExecutionStatus.choices)
SPAN_PAYLOAD_FIELDS = {
    "input": "input_data",
    "output": "output_data",
    "error": "error_data",
    "details": "details",
    "event_counts": "event_counts",
}
_PREVIEW_LIMIT = 1_024


class PayloadDescriptor(TypedDict):
    """Availability and bounded display JSON for one payload."""

    kind: str
    available: bool
    preview: str
    incomplete: bool


class SpanRow(TypedDict, total=False):
    """Summary row and derived tree/usage presentation values."""

    id: uuid.UUID
    run_id: uuid.UUID | None
    parent_run_id: uuid.UUID | None
    started_at: object
    ended_at: object
    duration_seconds: float | None
    kind: str
    name: str
    status: str
    status_label: str
    details: dict
    integrity_notice: str | None
    usage: dict | None
    event_count: int
    kind_icon: str
    selected: bool
    expanded: bool
    has_children: bool


class TreeToken(TypedDict, total=False):
    """One iterative tree entry or exit."""

    action: str
    run_id: uuid.UUID | None
    row: SpanRow
    has_children: bool


class SelectedSpan(SpanRow, total=False):
    """Selected summary enriched with bounded previews and repaired children."""

    input_payload: PayloadDescriptor
    output_payload: PayloadDescriptor
    error_payload: PayloadDescriptor
    children: list[SpanRow]
    metadata_preview: PayloadDescriptor
    events_preview: PayloadDescriptor
    event_counts_preview: PayloadDescriptor
    reported_costs_preview: PayloadDescriptor
    offset_seconds: float


class TraceOverview(TypedDict, total=False):
    """Execution-wide stored usage and diagnostic summary."""

    span_count: int
    root_count: int
    model_call_count: int
    usage_missing_calls: int
    usage_metrics: list[dict]
    reported_costs_preview: PayloadDescriptor
    has_integrity_anomalies: bool
    event_count: int


class InspectorContext(TypedDict):
    """Template context shared by overview and selected-span responses."""

    trace_overview: TraceOverview
    trace_rows: list[SpanRow]
    trace_tree_tokens: list[TreeToken]
    selected_span: SelectedSpan | None
    payloads: list[PayloadDescriptor]
    active_tab: str
    tabs: tuple[tuple[str, str], ...]


def _iter_bounded_json(value):
    """Yield compact JSON while encoding long strings and keys in bounded pieces."""
    encoder = json_encoder()

    def chunks(item):
        if isinstance(item, str):
            yield '"'
            for start in range(0, len(item), 256):
                yield json.encoder.encode_basestring(item[start : start + 256])[1:-1]
            yield '"'
        elif isinstance(item, dict):
            yield "{"
            for index, (key, nested) in enumerate(item.items()):
                if index:
                    yield ","
                yield from chunks(key)
                yield ":"
                yield from chunks(nested)
            yield "}"
        elif isinstance(item, list):
            yield "["
            for index, nested in enumerate(item):
                if index:
                    yield ","
                yield from chunks(nested)
            yield "]"
        else:
            yield from encoder.iterencode(item)

    yield from chunks(value)


def _preview(value):
    """Return a bounded compact JSON prefix and whether stored JSON continues."""
    remaining = _PREVIEW_LIMIT + 1
    parts = []
    for chunk in _iter_bounded_json(value):
        take = min(len(chunk), remaining)
        if take:
            parts.append(chunk[:take])
            remaining -= take
        if not remaining:
            break
    text = "".join(parts)
    incomplete = len(text) > _PREVIEW_LIMIT
    return text[:_PREVIEW_LIMIT], incomplete


def _payload(label, value) -> PayloadDescriptor:
    """Project one stored value to its bounded display prefix."""
    preview, incomplete = _preview(value)
    return {"kind": label, "available": value is not None, "preview": preview, "incomplete": incomplete}


def span_summaries(trace_id) -> list[dict]:
    """Return ordered lightweight span projections without reading payload columns."""
    fields = (
        "id",
        "run_id",
        "parent_run_id",
        "started_at",
        "ended_at",
        "kind",
        "name",
        "status",
        *(f"details__{key}" for key in SUMMARY_DETAIL_KEYS),
    )
    rows = list(AgentExecutionSpan.objects.filter(trace_id=trace_id).order_by("started_at", "id").values(*fields))
    for row in rows:
        row["details"] = {key: row.pop(f"details__{key}", None) for key in SUMMARY_DETAIL_KEYS}
        row["duration_seconds"] = (row["ended_at"] - row["started_at"]).total_seconds()
    return rows


def selected_span_data(trace_id, run_id) -> dict | None:
    """Read heavy stored fields for only the explicitly selected span."""
    return (
        AgentExecutionSpan.objects.filter(trace_id=trace_id, run_id=run_id)
        .values(*SPAN_PAYLOAD_FIELDS.values())
        .first()
    )


def inspect_trace(trace, span_id: uuid.UUID | None, *, include_tree=True) -> InspectorContext:
    """Load summaries and, for a selection, scoped heavy data and validate membership."""
    summaries = span_summaries(trace.pk)
    selected_data = selected_span_data(trace.pk, span_id) if span_id is not None else None
    if span_id is not None and selected_data is None:
        raise LookupError(span_id)
    return project(trace, summaries, span_id, selected_data, include_tree=include_tree)


def _repair_hierarchy(rows):
    """Index detached rows and repair missing/cyclic parent edges in recorded order."""
    by_run = {row["run_id"]: row for row in rows}
    parents = {}
    for row in rows:
        row["integrity_notice"] = None
        parent = row["parent_run_id"]
        parents[row["run_id"]] = parent if parent in by_run else None
        if parent is not None and parent not in by_run:
            row["integrity_notice"] = "Recorded parent span is missing."

    # Each parent chain is visited once; a gray node identifies a cycle in this walk.
    order = {row["run_id"]: index for index, row in enumerate(rows)}
    done = set()
    for start in by_run:
        path, positions, current = [], {}, start
        while current is not None and current not in done and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        if current in positions:
            cut = min(path[positions[current] :], key=order.__getitem__)
            parents[cut] = None
            by_run[cut]["integrity_notice"] = "Parent cycle detected; display edge was severed."
        done.update(path)

    children = {run_id: [] for run_id in by_run}
    roots = []
    for row in rows:
        run_id = row["run_id"]
        parent = parents[run_id]
        row["parent_run_id"] = parent
        if parent is None:
            roots.append(run_id)
        else:
            children[parent].append(run_id)

    return by_run, children, roots


def _tree_tokens(by_run, children, roots, span_id) -> list[TreeToken]:
    """Iteratively render the repaired forest, expanding roots and selected ancestry."""
    ancestors = set()
    current = span_id
    while current is not None and current in by_run and current not in ancestors:
        ancestors.add(current)
        current = by_run[current]["parent_run_id"]

    root_ids = set(roots)
    tokens: list[TreeToken] = []
    stack = [(run_id, False) for run_id in reversed(roots)]
    while stack:
        run_id, leaving = stack.pop()
        if leaving:
            tokens.append({"action": "leave", "run_id": run_id, "has_children": bool(children[run_id])})
            continue
        row = by_run[run_id]
        row["selected"] = run_id == span_id
        row["expanded"] = run_id in ancestors or run_id in root_ids
        row["has_children"] = bool(children[run_id])
        tokens.append({"action": "enter", "row": row, "run_id": run_id})
        stack.append((run_id, True))
        stack.extend((child, False) for child in reversed(children[run_id]))
    return tokens


def _kind_icon(row):
    """Map interpreted span facts to existing display icon classes."""
    if row["kind"] == "chat_model":
        return "mdi-brain"
    if row["kind"] == "tool":
        return "mdi-wrench"
    return "mdi-cog-outline"


def project(trace, summaries, span_id, selected_data, *, include_tree=True) -> InspectorContext:  # pylint: disable=too-many-locals
    """Build a non-mutating inspector context from detached trace/span projections."""
    rows = [cast(SpanRow, dict(row)) for row in summaries]
    by_run, children, roots = _repair_hierarchy(rows)
    if span_id is not None and span_id not in by_run:
        raise LookupError(span_id)
    tree_tokens: list[TreeToken] = _tree_tokens(by_run, children, roots, span_id) if include_tree else []
    usage_stats = summarize_spans(rows)

    for row in rows:
        row["kind_icon"] = _kind_icon(row)
        row["status_label"] = STATUS_LABELS.get(row["status"], row["status"].title())
        row.pop("details", None)

    detail: SelectedSpan | None = None
    request_payload = _payload("request", trace.request) if span_id is None else None
    if span_id is not None:
        detail = cast(SelectedSpan, dict(by_run[span_id]))
        detail.update(
            input_payload=_payload("input", selected_data["input_data"]),
            output_payload=_payload("output", selected_data["output_data"]),
            error_payload=_payload("error", selected_data["error_data"]),
        )
        # Stored details combine metadata with the callback event list under "events".
        details = selected_data["details"]
        metadata = {key: value for key, value in details.items() if key != "events"}
        detail["metadata_preview"] = _payload("metadata", metadata)
        detail["events_preview"] = _payload("events", details["events"])
        detail["event_counts_preview"] = _payload("event_counts", selected_data["event_counts"])
        detail["reported_costs_preview"] = _payload("reported_costs", metadata.get("reported_costs", []))
        detail["offset_seconds"] = (by_run[span_id]["started_at"] - trace.started_at).total_seconds()
        detail["children"] = [by_run[child] for child in children[span_id]]

    overview_row: SpanRow = {
        "run_id": None,
        "name": "Execution overview",
        "kind": "overview",
        "status": trace.status,
        "status_label": trace.execution_label,
        "selected": span_id is None,
        "expanded": True,
        "has_children": bool(rows),
        "kind_icon": "mdi-graph-outline",
        "duration_seconds": trace.duration_seconds,
    }
    tree_tokens = (
        [
            {"action": "enter", "row": overview_row, "run_id": None},
            *tree_tokens,
            {"action": "leave", "run_id": None, "has_children": bool(rows)},
        ]
        if include_tree
        else []
    )
    payloads = (
        [detail["input_payload"], detail["output_payload"], detail["error_payload"]] if detail else [request_payload]
    )
    payloads = [
        payload for payload in payloads if payload is not None and (payload["kind"] != "error" or payload["available"])
    ]
    reported_costs = usage_stats.pop("reported_costs")
    return {
        "trace_overview": {
            "span_count": len(rows),
            "root_count": len(roots),
            **usage_stats,
            "reported_costs_preview": _payload("reported_costs", reported_costs),
            "has_integrity_anomalies": any(row.get("integrity_notice") for row in rows),
        },
        "trace_rows": rows,
        "trace_tree_tokens": tree_tokens,
        "selected_span": detail,
        "payloads": payloads,
        "active_tab": "metadata" if detail and not any(payload["available"] for payload in payloads) else "io",
        "tabs": TABS,
    }
