"""Handled tool I/O, repair rendering, and JSON encoding."""

import contextlib
import json
import logging
from collections.abc import Callable, Generator, Mapping
from functools import partial
from typing import Any, cast

from django.core.exceptions import FieldError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, DataError, connection, connections, transaction
from langchain.tools import ToolException
from nautobot.core.celery.encoders import NautobotKombuJSONEncoder
from netaddr import EUI, IPAddress, IPNetwork
from pydantic import ValidationError

from nautobot_agent.app_settings import query_limits, query_statement_timeout_ms
from nautobot_agent.db_connections import close_connections

MIN_LIMIT = 1
_DATABASE_ERROR_MESSAGE = "The live-data query could not be completed."

_VALUE_ERROR_PREFIX = "Value error, "  # Pydantic v2 renders ValueError messages with this prefix.
VALIDATION_GUIDANCE_LIMIT = 20
_VALIDATION_GUIDANCE_NOTE = (
    f"Only the first {VALIDATION_GUIDANCE_LIMIT} validation errors carry guidance; fix those first."
)
_RETRY_SAFETY_GUIDANCE = (
    "Change only what the reported error requires; keep the user's intent and all requested conditions, "
    "and do not broaden filters or remove conditions to obtain a result."
)
_VALIDATION_TYPE_GUIDANCE = {
    "missing": "Provide the required argument in the shape described by the tool.",
    "extra_forbidden": "Remove the unsupported argument and use only the arguments in the tool description.",
    "string_type": "Provide a string.",
    "int_type": "Provide an integer.",
    "float_type": "Provide a number.",
    "bool_type": "Provide a boolean.",
    "list_type": "Provide a JSON array.",
    "dict_type": "Provide a JSON object.",
    "literal_error": "Choose one of the allowed literal values.",
    "greater_than_equal": "Use a value at or above the stated minimum.",
    "less_than_equal": "Use a value at or below the stated maximum.",
    "string_too_long": "Provide text with no more than the allowed number of characters.",
    "too_short": "Provide a value with at least the required number of items or characters.",
    "too_long": "Provide a value with no more than the allowed number of items or characters.",
    "string_pattern_mismatch": "Use text matching the required format.",
    "value_error": (
        "Apply the rule stated in the message; it may require changing a related argument the message names "
        "(for example `mode`, `group_by`, `chart_type`) while keeping the requested content."
    ),
}
_GENERIC_VALIDATION_GUIDANCE = (
    "Correct the argument to match the required shape and constraints in the tool description."
)


class UnserializableValueError(TypeError):
    """Raised by the encoder for a value with no JSON representation; message is type-only."""


class _QueryJSONEncoder(NautobotKombuJSONEncoder):
    """Nautobot's encoder plus netaddr scalars (MAC addresses, IP addresses, networks)."""

    def default(self, obj):
        if isinstance(obj, EUI | IPAddress | IPNetwork):
            return str(obj)
        try:
            return super().default(obj)
        except TypeError:
            raise UnserializableValueError(f"Object of type {type(obj).__name__} is not JSON serializable") from None


def dumps(payload: Mapping[str, Any]) -> str:
    """Serialize ``payload`` as compact JSON with the extended encoder."""
    return json.dumps(payload, cls=_QueryJSONEncoder, separators=(",", ":"))


def tool_error(error: str, message: str, **extra: Any) -> ToolException:
    """Build a handled tool exception whose text is the compact JSON error observation."""
    return ToolException(dumps({"error": error, "message": message, **extra}))


def _most_specific[T](
    location: tuple[str | int, ...], scoped: Mapping[tuple[str | int | None, ...], T] | None
) -> T | None:
    """Return the value of the most specific configured scope; None in a scope matches one location component."""
    guidance = scoped or {}
    for prefix in sorted(guidance, key=len, reverse=True):
        if len(location) >= len(prefix) and all(
            expected is None or expected == actual for expected, actual in zip(prefix, location, strict=False)
        ):
            return guidance[prefix]
    return None


def _validation_guidance(
    error: Mapping[str, Any],
    shown_locations: set[str],
    location_guidance: Mapping[str, str] | None,
    value_error_guidance: Mapping[tuple[str | int | None, ...], Mapping[str, str]] | None,
    scoped_guidance: Mapping[tuple[str | int | None, ...], str] | None,
) -> str:
    """Resolve fixed repair guidance from sanitized diagnostic metadata only."""
    loc = error["loc"]
    error_type = error["type"]
    stripped = error["msg"].removeprefix(_VALUE_ERROR_PREFIX)
    locations = location_guidance or {}
    overrides = _most_specific(loc, value_error_guidance) or {}
    scope_hint = _most_specific(loc, scoped_guidance)
    if error_type == "extra_forbidden" and len(loc) == 1:
        repair = _VALIDATION_TYPE_GUIDANCE["extra_forbidden"]
    elif error_type == "value_error" and stripped in overrides:
        repair = overrides[stripped]
    else:
        repair = _VALIDATION_TYPE_GUIDANCE.get(error_type, _GENERIC_VALIDATION_GUIDANCE)
        location = loc[0] if loc else None
        if (
            isinstance(location, str)
            and scope_hint is None
            and location in locations
            and location not in shown_locations
        ):
            repair += " " + locations[location]
            shown_locations.add(location)
    if scope_hint is not None:
        repair += " " + scope_hint
    return f"{repair} {_RETRY_SAFETY_GUIDANCE}"


def validation_error_content(
    exc: Any,
    *,
    location_guidance: Mapping[str, str] | None = None,
    value_error_guidance: Mapping[tuple[str | int | None, ...], Mapping[str, str]] | None = None,
    scoped_guidance: Mapping[tuple[str | int | None, ...], str] | None = None,
) -> str:
    """Render public errors from the native Pydantic v2 input schema, never runtime or raw input."""
    errors = (
        error
        for error in cast(ValidationError, exc).errors(include_input=False, include_context=False, include_url=False)
        if error["loc"][:1] != ("runtime",)
    )
    details = []
    shown_locations: set[str] = set()
    for index, error in enumerate(errors):
        detail = {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        if index < VALIDATION_GUIDANCE_LIMIT:
            detail["guidance"] = _validation_guidance(
                error,
                shown_locations,
                location_guidance,
                value_error_guidance,
                scoped_guidance,
            )
        details.append(detail)
    content = {"error": "ValidationError", "message": "Invalid tool arguments.", "details": details}
    if len(details) > VALIDATION_GUIDANCE_LIMIT:
        content["guidance_note"] = _VALIDATION_GUIDANCE_NOTE
    return dumps(content)


def configure_tool_errors(tool: Any, **guidance: Any) -> None:
    """Install the standard handled-error behavior on a LangChain tool."""
    tool.handle_tool_error = True
    tool.handle_validation_error = (
        partial(validation_error_content, **guidance) if guidance else validation_error_content
    )


def _orm_error(exc: Exception) -> ToolException:
    message = " ".join(cast(list[str], exc.messages)) if isinstance(exc, DjangoValidationError) else str(exc)
    return tool_error(type(exc).__name__, message)


@contextlib.contextmanager
def orm_errors() -> Generator[None, None, None]:
    """Turn native ORM argument errors into handled tool errors."""
    try:
        yield
    except (FieldError, DjangoValidationError, ValueError, TypeError) as exc:
        raise _orm_error(exc) from None


def _close_stale_connections() -> None:
    for db_connection in connections.all(initialized_only=True):
        if not db_connection.in_atomic_block:
            db_connection.close_if_unusable_or_obsolete()


@contextlib.contextmanager
def _statement_timeout() -> Generator[None, None, None]:
    """Apply a database-side statement limit without changing subsequent callers' policy."""
    timeout_ms = query_statement_timeout_ms()
    if connection.vendor == "postgresql":
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                previous = cursor.fetchone()[0]
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f"{timeout_ms}ms"])
            yield
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
    elif connection.vendor == "mysql":
        # MariaDB measures seconds; MySQL measures milliseconds and limits SELECT statements.
        variable = "max_statement_time" if connection.mysql_is_mariadb else "max_execution_time"
        timeout = timeout_ms / 1000 if connection.mysql_is_mariadb else timeout_ms
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT @@SESSION.{variable}")
            previous = cursor.fetchone()[0]
            cursor.execute(f"SET SESSION {variable} = %s", [timeout])
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"SET SESSION {variable} = %s", [previous])
    else:
        yield


def execute_data_tool(
    build_payload: Callable[[], Mapping[str, Any]],
    *,
    logger: logging.Logger,
    failure_message: str,
    model: str,
) -> str:
    """Run one data tool: manage connections, serialize the payload, and sanitize database errors."""
    try:
        _close_stale_connections()
        with _statement_timeout():
            payload = build_payload()
            try:
                encoded = dumps(payload)
            except UnserializableValueError as exc:
                raise tool_error("SerializationError", f"A queried value could not be serialized: {exc}") from None
        max_response_bytes = query_limits()
        actual_bytes = len(encoded.encode("utf-8"))
        if actual_bytes > max_response_bytes:
            raise tool_error(
                "PageSizeExceeded",
                "The complete query or trace response exceeds the configured byte limit. For a query using a "
                "cursor, retry with that original cursor and a smaller limit. If a query row or group cannot fit, "
                "restart with a narrower projection or aggregate selection. Changing either requires restarting "
                "the query. For traces, retry with smaller root/include limits, narrower fields, or fewer "
                "relationship includes; the whole call must fit.",
                actual_bytes=actual_bytes,
                max_bytes=max_response_bytes,
            )
        return encoded
    except DataError:
        logger.info("Database rejected a query value for %r", model)
        raise tool_error(
            "InvalidLookupValue",
            "The database rejected a lookup value (for example an invalid regular expression). "
            "Correct the query values before retrying.",
        ) from None
    except DatabaseError:
        logger.exception(failure_message, model)
        raise tool_error("DatabaseError", _DATABASE_ERROR_MESSAGE) from None
    finally:
        close_connections()
