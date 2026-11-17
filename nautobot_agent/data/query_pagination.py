"""Bounded, signed continuation metadata and shared page contract for live-data queries."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict, cast

from django.core import signing
from django.db.models import QuerySet

from nautobot_agent.data.tool_io import tool_error

CURSOR_LIFETIME_SECONDS = 3600
MAX_CURSOR_LENGTH = 4096
_CURSOR_SALT = "nautobot_agent.query_records.cursor.v1"


class Page(TypedDict):
    """One materialized result page; ``next_cursor`` is added once the page is signed."""

    count: int
    truncated: bool
    results: list[Any]
    next_cursor: NotRequired[str | None]


@dataclass(frozen=True)
class QueryContinuation:
    """Immutable query binding and optional validated page position; it grants no data access."""

    _user_id: str
    _fingerprint: str
    _strategy: str
    _descending: bool
    _position: int | str | None
    _expires_at: int | None

    @classmethod
    def bind(
        cls,
        token: str | None,
        *,
        user_id: Any,
        fingerprint: str,
        strategy: str,
        descending: bool = False,
    ) -> QueryContinuation:
        """Bind trusted query metadata and validate an optional incoming continuation token."""
        normalized_user_id = str(user_id)
        if token is None:
            return cls(normalized_user_id, fingerprint, strategy, descending, None, None)
        position, expires_at = _decode_cursor(
            token,
            user_id=normalized_user_id,
            fingerprint=fingerprint,
        )
        return cls(normalized_user_id, fingerprint, strategy, descending, position, expires_at)

    def resume(self, queryset: QuerySet) -> tuple[QuerySet, int]:
        """Apply the bound keyset position or return the offset for page slicing."""
        if self._strategy == "pk" and self._position is not None:
            lookup = "pk__lt" if self._descending else "pk__gt"
            return queryset.filter(**{lookup: self._position}), 0
        if self._strategy == "offset" and self._position is not None:
            return queryset, cast(int, self._position)
        return queryset, 0

    def next_cursor(self, position: int | str) -> str:
        """Sign the next position, preserving a resumed absolute expiry."""
        expires_at = self._expires_at
        if expires_at is None:
            expires_at = int(time.time()) + CURSOR_LIFETIME_SECONDS
        payload = {
            "q": self._fingerprint,
            "u": self._user_id,
            "p": position,
            "e": expires_at,
        }
        return signing.dumps(payload, salt=_CURSOR_SALT, compress=True)


def _canonical(value: Any) -> Any:
    """Normalize JSON-like query values without collapsing scalar types or list order."""
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "deconstruct"):
        return {"expression": _canonical(value.deconstruct())}
    raise tool_error("ValidationError", "The effective query ordering cannot be canonically continued.")


def query_fingerprint(query: Any) -> str:
    """Return the SHA-256 fingerprint for canonical result-defining query metadata."""
    encoded = json.dumps(_canonical(query), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invalid_cursor() -> Exception:
    return tool_error("InvalidCursor", "The continuation cursor is invalid for this query.")


def _decode_cursor(
    token: str,
    *,
    user_id: Any,
    fingerprint: str,
) -> tuple[int | str, int]:
    """Verify a bounded signed token and return its position and absolute expiry."""
    try:
        payload = signing.loads(token, salt=_CURSOR_SALT)
    except (signing.BadSignature, TypeError, ValueError, UnicodeError):
        raise _invalid_cursor() from None
    query, owner, position, expires = (payload[key] for key in ("q", "u", "p", "e"))
    if query != fingerprint or owner != str(user_id):
        raise _invalid_cursor()
    if expires <= int(time.time()):
        raise tool_error("CursorExpired", "The continuation cursor has expired; start a new query.")
    return position, expires


def page(queryset: QuerySet, limit: int, *, offset: int = 0) -> Page:
    """Materialize a raw page and one lookahead row."""
    fetched = list(queryset[offset : offset + limit + 1])
    has_more = len(fetched) > limit
    results = fetched[:limit]
    return {"count": len(results), "truncated": has_more, "results": results}
