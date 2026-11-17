"""Trace payload capture and interpretation contracts.

Functions here operate on detached callback envelopes or projected stored rows; they do not
load ORM objects. This module owns capture metadata/event shape and summary interpretation.
The trace inspector owns selected-span display projection.
"""

import json
import math
import traceback
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from nautobot_agent.runtime.checkpointer import durable_items
from nautobot_agent.runtime.context import AgentContext

METADATA_KEYS = ("ls_provider", "ls_model_name", "langgraph_node", "ls_agent_type")
SUMMARY_DETAIL_KEYS = ("usage_summary", "reported_costs", "event_log_count")


def json_encoder():
    """Create the shared compact UTF-8 JSON encoder used for retrieval."""
    return json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def safe_string(value: str):
    """Preserve full text while encoding PostgreSQL-unsafe Unicode and NUL values."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return {"type": "str_with_surrogates", "utf8_hex": value.encode("utf-8", "surrogatepass").hex()}
    return {"type": "str_with_nul", "parts": value.split("\x00")} if "\x00" in value else value


def snapshot(value):
    """Detach a complete JSON-safe snapshot without imposing storage previews."""
    return _snapshot(value, None)


def _snapshot(value, active):  # pylint: disable=too-many-return-statements
    """Encode leaf values directly and composites under circular-reference tracking."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return safe_string(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "float", "value": str(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        return f"<circular reference: {type(value).__qualname__} id={identity}>"
    active.add(identity)
    try:
        return _snapshot_composite(value, active)
    finally:
        active.remove(identity)


def _snapshot_composite(value, active):  # pylint: disable=too-many-return-statements
    """Encode a reference-tracked value by its type, recursing through ``active``."""
    qualified_type = safe_string(f"{type(value).__module__}.{type(value).__qualname__}")
    if isinstance(value, BaseException):
        return {
            "type": qualified_type,
            "message": safe_string(str(value)),
            "args": _snapshot(value.args, active),
            "traceback": safe_string("".join(traceback.format_exception(value))),
            "attributes": _snapshot(vars(value), active),
            "cause": _snapshot(value.__cause__, active),
            "context": _snapshot(value.__context__, active),
        }
    if isinstance(value, BaseModel):
        return {"type": qualified_type, "fields": _snapshot(dict(value), active)}
    if isinstance(value, AgentContext):
        return {"type": qualified_type, "fields": {"user_id": _snapshot(value.user_id, active)}}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": qualified_type,
            "fields": {item.name: _snapshot(getattr(value, item.name), active) for item in fields(value)},
        }
    if isinstance(value, Mapping):
        durable = list(durable_items(value))
        if all(isinstance(key, str) and isinstance(safe_string(key), str) for key, _ in durable):
            return {key: _snapshot(item, active) for key, item in durable}
        return {
            "type": safe_string(type(value).__name__),
            "items": [[_snapshot(key, active), _snapshot(item, active)] for key, item in durable],
        }
    if isinstance(value, list | tuple):
        return [_snapshot(item, active) for item in value]
    if isinstance(value, set | frozenset):
        return {"type": safe_string(type(value).__name__), "items": [_snapshot(item, active) for item in value]}
    return {"type": qualified_type, "repr": safe_string(repr(value))}


def callback_envelope(run_id, arguments, kwargs):
    """Capture the callback dictionary after assembling its existing fields."""
    return snapshot({"run_id": run_id, "arguments": arguments, "kwargs": kwargs})


def event_log_details(entries):
    """Build the persisted event list and its stored count."""
    return {"events": entries, "event_log_count": len(entries)}


def span_metadata(kind, kwargs):
    """Build the stored metadata fields for a callback span."""
    metadata = kwargs.get("metadata")
    details = {}
    if isinstance(metadata, Mapping):
        details.update({key: snapshot(metadata[key]) for key in METADATA_KEYS if key in metadata})
    if kind == "chain":
        details["callback_context"] = snapshot(kwargs)
    return details


def event_entry(sequence, timestamp, kind, payload, *, include_payload=True):
    """Build the stored callback event entry."""
    entry = {"sequence": sequence, "timestamp": timestamp.isoformat(), "kind": kind}
    if include_payload:
        entry["payload"] = payload
    return entry


def _snapshot_mapping(value):
    """Read a mapping, unwrapping typed snapshots used by usage and cost extraction."""
    if isinstance(value, dict) and isinstance(value.get("type"), str) and isinstance(value.get("fields"), dict):
        return value["fields"]
    return value if isinstance(value, dict) else {}


def _normalize_usage(value):
    """Normalize reported counters; unknown is distinct from zero."""
    data = _snapshot_mapping(value)

    def number(item):
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            return item if item >= 0 else None
        if isinstance(item, float) and math.isfinite(item) and item >= 0 and item.is_integer():
            return int(item)
        return None

    return {
        "input_tokens": number(data.get("input_tokens", data.get("prompt_tokens"))),
        "output_tokens": number(data.get("output_tokens", data.get("completion_tokens"))),
        "total_tokens": number(data.get("total_tokens")),
    }


def _known_usage(value):
    usage = _normalize_usage(value)
    return usage if any(item is not None for item in usage.values()) else None


def _response_snapshot(output):
    data = _snapshot_mapping(output)
    return _snapshot_mapping(_snapshot_mapping(data.get("arguments")).get("response"))


def _call_usage_summary(output):
    """Extract explicit aggregate usage or first-candidate-per-batch usage."""
    response = _response_snapshot(output)
    aggregate = _known_usage(_snapshot_mapping(response.get("llm_output")).get("token_usage"))
    if aggregate is not None:
        return {**aggregate, "source": "llm_output.token_usage"}
    batches = response.get("generations")
    if not isinstance(batches, list) or not batches:
        return {}
    records = []
    for batch in batches:
        if not isinstance(batch, list) or not batch:
            return {}
        message = _snapshot_mapping(_snapshot_mapping(batch[0]).get("message"))
        usage = _known_usage(message.get("usage_metadata"))
        if usage is None:
            metadata = _snapshot_mapping(message.get("response_metadata"))
            usage = _known_usage(metadata.get("token_usage")) or _known_usage(metadata.get("usage"))
        if usage is None:
            return {}
        records.append(usage)
    return {
        **{
            key: sum(item[key] for item in records) if all(item[key] is not None for item in records) else None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        },
        "source": "first candidate per input batch",
    }


def _valid_cost(value):
    """Accept finite, nonnegative numeric provider cost reports."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return value >= 0 and (isinstance(value, int) or math.isfinite(value))


def _costs_at(mapping, path, currency_fallback=None):
    currency = mapping.get("currency")
    if not isinstance(currency, str) or not currency:
        currency = currency_fallback
    currency = currency if isinstance(currency, str) and currency else None
    return [
        {"amount": mapping[key], "currency": currency, "source": f"{path}.{key}"}
        for key in ("cost", "total_cost")
        if key in mapping and _valid_cost(mapping[key])
    ]


def _extract_reported_costs(output):
    """Extract explicit provider cost reports, preferring aggregate reports over generations."""
    response = _response_snapshot(output)
    llm_output = _snapshot_mapping(response.get("llm_output"))
    result = _costs_at(
        _snapshot_mapping(llm_output.get("token_usage")), "llm_output.token_usage", llm_output.get("currency")
    )
    if result:
        return result
    if not isinstance(response.get("generations"), list):
        return result
    for batch_index, batch in enumerate(response["generations"]):
        if not isinstance(batch, list):
            continue
        for candidate_index, generation in enumerate(batch):
            message = _snapshot_mapping(_snapshot_mapping(generation).get("message"))
            metadata = _snapshot_mapping(message.get("response_metadata"))
            path = f"generations[{batch_index}][{candidate_index}].message.response_metadata"
            nested = []
            for key in ("token_usage", "usage"):
                nested.extend(
                    _costs_at(_snapshot_mapping(metadata.get(key)), f"{path}.{key}", metadata.get("currency"))
                )
            result.extend(nested or _costs_at(metadata, path))
    return result


def model_call_summaries(output):
    """Return the optional stored usage-summary and reported-cost dictionary fields."""
    summary = _call_usage_summary(output)
    costs = _extract_reported_costs(output)
    return {**({"usage_summary": summary} if summary else {}), **({"reported_costs": costs} if costs else {})}


def summarize_spans(rows):
    """Interpret usage and event summaries once on inspector-owned row copies."""
    usages = []
    reported_costs = []
    event_count = 0

    for row in rows:
        count = row["details"].get("event_log_count") or 0
        row["event_count"] = count
        event_count += count
        if row["kind"] != "chat_model":
            row["usage"] = None
            continue
        row["usage"] = row["details"].get("usage_summary")
        usages.append(row["usage"])
        reported_costs.extend(row["details"].get("reported_costs") or [])

    model_call_count = len(usages)
    reported_usages = [usage for usage in usages if usage is not None]
    known_totals = {
        key: [usage[key] for usage in reported_usages if usage[key] is not None]
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    return {
        "model_call_count": model_call_count,
        "usage_missing_calls": model_call_count - len(reported_usages),
        "usage_metrics": [
            {
                "label": key.replace("_", " ").title(),
                "value": sum(values) if values else None,
                "missing": model_call_count - len(values),
            }
            for key, values in known_totals.items()
        ],
        "reported_costs": reported_costs,
        "event_count": event_count,
    }
