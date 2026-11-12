"""Real Redis coverage and bounds for disposable run-event streams."""
# pylint: disable=protected-access

import json
import threading
import uuid
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.sentinel import Sentinel, SentinelConnectionPool

from nautobot_agent.chat import events
from nautobot_agent.chat.events import EventReadUnavailable, EventStreamError, InvalidCursor, RunEvents, read_run_events


class SentinelEventClientTests(SimpleTestCase):
    """Keep private stream connections independent during Sentinel failover."""

    def test_private_pool_discovers_failover_without_disconnecting_shared_pool(self):
        manager = Sentinel([("localhost", 26379)])
        shared_pool = SentinelConnectionPool("cache", manager, check_connection=True)
        self.addCleanup(shared_pool.disconnect)
        cache = Mock()
        cache.client.get_client.return_value = Redis(connection_pool=shared_pool)
        with patch.object(events, "caches", {"default": cache}):
            _, pool, client = RunEvents._build_client()
        self.addCleanup(pool.disconnect)
        self.assertIs(client.connection_pool, pool)
        connection = pool.make_connection()
        self.assertIs(connection.connection_pool, pool.proxy)
        self.assertIsNot(pool.proxy, shared_pool.proxy)
        self.assertTrue(pool.check_connection)
        self.assertEqual((connection.socket_timeout, connection.socket_connect_timeout), (1, 1))
        with (
            patch.object(manager, "discover_master", side_effect=[("first", 6379), ("second", 6379)]),
            patch.object(shared_pool, "disconnect") as shared_disconnect,
        ):
            self.assertEqual(pool.get_master_address(), ("first", 6379))
            self.assertEqual(pool.get_master_address(), ("second", 6379))
            shared_disconnect.assert_not_called()


class RunEventStreamTests(SimpleTestCase):
    """Use isolated random stream keys against the configured default cache."""

    def setUp(self):
        self.publisher = RunEvents()
        self.addCleanup(self.publisher.close)
        self.run_id = uuid.uuid4()
        self.key = self.publisher._key(self.run_id)
        self.redis_client = self.publisher._get_client()[1]
        self.addCleanup(self.redis_client.delete, self.key)

    def use_reader(self):
        """Substitute a test-owned reader and restore the module reference before closing it."""
        previous = events._reader
        reader = RunEvents()
        events._reader = reader

        def restore_and_close():
            events._reader = previous
            reader.close()

        self.addCleanup(restore_and_close)
        return reader

    def test_concurrent_polling_delivers_each_run_page(self):
        self.use_reader()
        other = uuid.uuid4()
        self.addCleanup(self.redis_client.delete, self.publisher._key(other))
        self.publisher.start(self.run_id)
        self.publisher.start(other)
        expected = {
            self.run_id: {"run": "first"},
            other: {"run": "second"},
        }
        for run_id, payload in expected.items():
            self.publisher.publish(run_id, "done", payload)

        barrier = threading.Barrier(3)
        pages = {}
        failures = []

        def read(run_id):
            try:
                barrier.wait(timeout=5)
                pages[run_id] = read_run_events(run_id)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                failures.append(exc)

        workers = [threading.Thread(target=read, args=(run_id,)) for run_id in expected]

        def join_workers():
            for worker in workers:
                worker.join(timeout=5)

        self.addCleanup(join_workers)
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        join_workers()

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(
            {run_id: page.events[0]["data"] for run_id, page in pages.items()},
            expected,
        )

    def test_transient_read_failures_allow_retry_from_same_cursor(self):
        reader = self.use_reader()
        self.publisher.start(self.run_id)
        cursor = self.publisher.publish(self.run_id, "token", {"text": "before"})
        last = self.publisher.publish(self.run_id, "token", {"text": "after"})
        client = reader._get_client()[1]
        for error in (RedisTimeoutError("private address"), RedisConnectionError("private address")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(client, "eval", side_effect=error),
                self.assertRaises(EventReadUnavailable),
            ):
                reader.read(self.run_id, cursor)
            recovered = reader.read(self.run_id, cursor)
            self.assertEqual(recovered.events, [{"event": "token", "data": {"text": "after"}}])
            self.assertEqual(recovered.next_cursor, last)
            self.assertFalse(recovered.resync_required)

    def test_byte_limited_pages_resume_without_skipping_or_repeating_events(self):
        self.publisher.start(self.run_id)
        texts = ("first", "second", "third")
        expected = [{"event": "token", "data": {"text": text}} for text in texts]
        cursors = [self.publisher.publish(self.run_id, "token", {"text": text}) for text in texts]
        limit = 2 + max(len(json.dumps(item, separators=(",", ":")).encode()) for item in expected)
        after = "0-0"
        with patch.object(events, "_MAX_PAGE_BYTES", limit):
            for index, item in enumerate(expected):
                page = self.publisher.read(self.run_id, after)
                self.assertEqual(page.events, [item])
                self.assertEqual(page.next_cursor, cursors[index])
                self.assertEqual(page.has_more, index < len(expected) - 1)
                self.assertFalse(page.resync_required)
                after = page.next_cursor

    def test_resync_and_missing_stream_rules(self):
        reader = self.use_reader()
        self.assertTrue(self.publisher.read(self.run_id, "0-0").queued)
        self.assertTrue(self.publisher.read(self.run_id, "1-0").resync_required)

        marker = self.publisher.start(self.run_id)
        with self.assertRaises(EventStreamError):
            self.publisher.start(self.run_id)
        done = self.publisher.publish(self.run_id, "done", {"recovered": True})
        self.assertTrue(self.publisher.read(self.run_id, "18446744073709551615-0").resync_required)
        build_client = reader._build_client
        attempts = iter((RuntimeError("temporary construction failure"), None))

        def fail_once():
            failure = next(attempts)
            if failure is not None:
                raise failure
            return build_client()

        with patch.object(reader, "_build_client", side_effect=fail_once):
            self.assertTrue(read_run_events(self.run_id).resync_required)
            recovered = read_run_events(self.run_id)
        self.assertEqual(recovered.events[0]["data"], {"recovered": True})

        first = self.publisher.publish(self.run_id, "token", {"index": 0})
        for index in range(102):
            self.publisher.publish(self.run_id, "token", {"index": index + 1})
        self.redis_client.xdel(self.key, marker, done, first)
        self.assertEqual(self.redis_client.xlen(self.key), 102)
        self.assertTrue(self.publisher.read(self.run_id).resync_required)
        self.assertTrue(self.publisher.read(self.run_id, first).resync_required)
        self.assertTrue(self.publisher.read(self.run_id, marker).resync_required)
        retained_id = self.redis_client.xrange(self.key, count=1)[0][0].decode()
        resumed = self.publisher.read(self.run_id, retained_id)
        self.assertFalse(resumed.resync_required)
        self.assertTrue(resumed.has_more)

        self.redis_client.delete(self.key)
        with self.assertRaises(EventStreamError):
            self.publisher.publish(self.run_id, "token", {"text": "late"})
        self.assertFalse(self.redis_client.exists(self.key))

        for value in ("latest", "abc", "-1-0", "01-0", "18446744073709551616-0"):
            with self.subTest(value=value), self.assertRaises(InvalidCursor):
                self.publisher.read(self.run_id, value)
        self.assertTrue(self.publisher.read(self.run_id, "18446744073709551615-0").resync_required)

    def test_oversized_events_preserve_later_progress_and_require_history_rebuild(self):
        self.publisher.start(self.run_id)
        payloads = (
            ("tool_call", {"id": "tool", "name": "eval_python", "args": {"code": "x" * 70_000}}),
            ("todos", {"todos": [{"content": "x" * 70_000, "status": "pending"}]}),
            ("token", {"text": "é" * 35_000}),
        )
        for name, payload in payloads:
            self.publisher.publish(self.run_id, name, payload)
        self.publisher.publish(self.run_id, "token", {"text": "later progress"})
        self.publisher.publish(self.run_id, "done", {})
        page = self.publisher.read(self.run_id)
        self.assertFalse(page.resync_required)
        self.assertEqual(
            page.events,
            [{"event": "display_omitted", "data": {}}] * 3
            + [{"event": "token", "data": {"text": "later progress"}}, {"event": "done", "data": {}}],
        )
