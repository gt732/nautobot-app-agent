"""Bounded, disposable Redis Streams delivery for durable chat runs."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.cache import caches
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry
from redis.sentinel import SentinelConnectionPool

logger = logging.getLogger(__name__)

_MAX_LENGTH = 10_000
_TTL = 3_600
_MAX_EVENT_BYTES = 64 * 1024
_MAX_PAGE_BYTES = 256 * 1024
_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)\Z")
_START = "start"
_START_SCRIPT = f"""
if redis.call('EXISTS', KEYS[1]) ~= 0 then return false end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '=', {_MAX_LENGTH}, '*', 'event', 'start', 'data', '{{}}')
redis.call('EXPIRE', KEYS[1], {_TTL})
return id
"""
_READ_SCRIPT = """
local kind = redis.call('TYPE', KEYS[1]).ok
if kind ~= 'stream' then return {kind} end
return {
    kind,
    redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 1),
    redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1),
    redis.call('XRANGE', KEYS[1], '(' .. ARGV[1], '+', 'COUNT', 101)
}
"""
_APPEND_SCRIPT = f"""
if redis.call('TYPE', KEYS[1]).ok ~= 'stream' then return false end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '=', {_MAX_LENGTH}, '*', 'event', ARGV[1], 'data', ARGV[2])
redis.call('EXPIRE', KEYS[1], {_TTL})
return id
"""


class EventStreamError(Exception):
    """A publication/reader operation could not establish stream correctness."""


class EventReadUnavailable(Exception):
    """A transient Redis failure prevented reading without invalidating coverage."""


class InvalidCursor(ValueError):
    """A cursor is not a valid Redis stream ID."""


@dataclass(frozen=True)
class EventPage:
    """One bounded event page and its coverage result.

    ``queued`` is true only when the request cursor was ``"0-0"`` and the stream
    does not exist.
    """

    events: list[dict]
    next_cursor: str
    has_more: bool
    resync_required: bool
    queued: bool = False


class RunEvents:
    """Lazily own an isolated bounded cache Redis client.

    Each instance owns its private pool; execution publishers remain independent
    from the process-retained polling reader.
    Writes raise ``EventStreamError`` when publication cannot be confirmed.
    Reads raise ``EventReadUnavailable`` for transient connection failures and
    return ``EventPage.resync_required`` for uncertain coverage or invalid storage.
    Invalid cursors raise ``InvalidCursor``.
    """

    def __init__(self):
        """Allocate bookkeeping without connecting or sharing a prefork client."""
        self._client = None
        self._pool = None
        self._cache = None
        self._init_lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    cache, pool, client = self._build_client()
                    self._cache = cache
                    self._pool = pool
                    self._client = client
        return self._cache, self._client

    @staticmethod
    def _build_client():
        """Construct a private cache client without publishing partial state."""
        cache = caches["default"]
        shared = cache.client.get_client(write=True)
        options = dict(shared.connection_pool.connection_kwargs)
        options.update(
            connection_class=shared.connection_pool.connection_class,
            decode_responses=False,
            socket_timeout=1,
            socket_connect_timeout=1,
            retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False,
            retry_on_error=[],
        )
        shared_pool = shared.connection_pool
        if isinstance(shared_pool, SentinelConnectionPool):
            # The copied connection kwargs contain the shared pool's failover proxy.
            options.pop("connection_pool", None)
            pool = type(shared_pool)(
                shared_pool.service_name,
                shared_pool.sentinel_manager,
                is_master=shared_pool.is_master,
                check_connection=shared_pool.check_connection,
                **options,
            )
        else:
            pool = type(shared_pool)(**options)
        client = type(shared)(connection_pool=pool)
        return cache, pool, client

    def _key(self, run_id: UUID | str) -> str:
        cache, _ = self._get_client()
        return cache.make_key(f"nautobot_agent:run:{run_id}:events")

    def start(self, run_id: UUID | str) -> str:
        """Atomically create the coverage marker and apply retention."""
        try:
            _, client = self._get_client()
            result = client.eval(_START_SCRIPT, 1, self._key(run_id))
            if not result:
                raise EventStreamError("stream already exists")
            return result.decode()
        except EventStreamError:
            raise
        except Exception as exc:  # noqa: BLE001  # any client failure is reported as an unavailable event stream
            raise EventStreamError(type(exc).__name__) from None

    def publish(self, run_id: UUID | str, event: str, payload: dict) -> str:
        """Append a bounded event, substituting a display-omission marker when too large."""
        try:
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            event_size = len(
                json.dumps({"event": event, "data": payload}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )
            if event_size > _MAX_EVENT_BYTES:
                # Keep later progress flowing; the browser must rebuild durable history.
                event, encoded = "display_omitted", "{}"
            _, client = self._get_client()
            result = client.eval(_APPEND_SCRIPT, 1, self._key(run_id), event, encoded)
            if not result:
                raise EventStreamError("stream no longer exists")
            return result.decode()
        except EventStreamError:
            raise
        except Exception as exc:  # noqa: BLE001  # any client failure is reported as an unavailable event stream
            raise EventStreamError(type(exc).__name__) from None

    def read(self, run_id: UUID | str, after: str = "0-0") -> EventPage:
        """Read a nonblocking, bounded page, reporting any uncertain coverage."""
        after_key = _id_key(after)
        resync = EventPage([], after, False, True)
        try:
            _, client = self._get_client()
            page = client.eval(_READ_SCRIPT, 1, self._key(run_id), after)
            kind = page[0]
            if kind == b"none":
                return EventPage([], after, False, after != "0-0", queued=after == "0-0")
            if kind != b"stream":
                return resync
            first, tail, following = page[1:]
            if not _covers(first, tail, after, after_key):
                return resync
            events = _event_page(following, after)
            return resync if events is None else events
        except (RedisConnectionError, RedisTimeoutError) as exc:
            raise EventReadUnavailable(type(exc).__name__) from None
        except Exception as exc:  # noqa: BLE001  # malformed or failed reads make the browser resync from durable state
            logger.warning("Run event read failed (%s)", type(exc).__name__)
            return resync

    def close(self):
        """Disconnect only this instance's private pool."""
        if self._pool is not None:
            self._pool.disconnect()
        self._client = None
        self._pool = None
        self._cache = None


def _reset_reader_lock_after_fork():
    """Replace the retained reader lock in a child without acquiring its inherited state."""
    _reader._init_lock = threading.Lock()  # pylint: disable=protected-access


_reader = RunEvents()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_reader_lock_after_fork)


def _id_key(value: str) -> tuple[int, int]:
    """Return numeric ordering for a validated Redis stream ID."""
    if not isinstance(value, str) or _CURSOR.fullmatch(value) is None:
        raise InvalidCursor("invalid stream cursor")
    milliseconds, sequence = value.split("-", 1)
    if any(len(part) > 20 or int(part) > 2**64 - 1 for part in (milliseconds, sequence)):
        raise InvalidCursor("invalid stream cursor")
    return int(milliseconds), int(sequence)


def _fields(flat: list[bytes]) -> dict[bytes, bytes]:
    """Pair a Redis stream entry's flat field/value list."""
    return dict(zip(flat[::2], flat[1::2], strict=True))


def _covers(first: list, tail: list, after: str, after_key: tuple[int, int]) -> bool:
    """Whether the retained stream still holds every event after the cursor."""
    if not first or not tail:
        return False
    first_id, marker_fields = first[0]
    marker = _fields(marker_fields)
    last_id = tail[0][0].decode()
    if after == "0-0":
        return marker.get(b"event") == b"start" and marker.get(b"data") == b"{}"
    return _id_key(first_id.decode()) <= after_key <= _id_key(last_id)


def _decode_entry(fields: list[bytes]) -> dict[str, Any] | None:
    """Return one stored entry as a page item, or None when it is malformed."""
    entry = _fields(fields)
    event = entry.get(b"event")
    data = entry.get(b"data")
    if not event or data is None:
        return None
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {"event": event.decode(), "data": payload}


def _event_page(following: list, after: str) -> EventPage | None:
    """Build a byte-bounded page from entries after the cursor, or None when any entry is malformed."""
    result = []
    total = 2  # JSON array brackets; include separators in the response-byte bound.
    cursor = after
    for entry_id, fields in following[:100]:
        item = _decode_entry(fields)
        if item is None:
            return None
        if item["event"] != _START:
            size = len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            size += bool(result)
            if total + size > _MAX_PAGE_BYTES:
                return EventPage(result, cursor, True, False)
            result.append(item)
            total += size
        cursor = entry_id.decode()
    return EventPage(result, cursor, len(following) > 100, False)


def read_run_events(run_id: UUID | str, after: str = "0-0") -> EventPage:
    """Read an event page using the process-retained Redis reader."""
    return _reader.read(run_id, after)
