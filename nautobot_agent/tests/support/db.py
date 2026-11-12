"""Committed-data test base with table-targeted cleanup."""

from django.core.management import call_command
from django.core.management.color import no_style
from django.db import connections, transaction
from django.test import TransactionTestCase

_BASELINE: dict[str, dict[str, int]] = {}


def _row_counts(connection) -> dict[str, int]:
    """Return every Django table's row count in one round trip."""
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            " UNION ALL ".join(f"SELECT {index}, COUNT(*) FROM {quote(table)}" for index, table in enumerate(tables))
        )
        return {tables[index]: count for index, count in cursor.fetchall()}


def _baseline(alias: str) -> dict[str, int]:
    """Return the migrated row counts, after one full flush heals leftovers from an aborted earlier run."""
    if alias not in _BASELINE:
        call_command("flush", verbosity=0, interactive=False, database=alias, reset_sequences=False)
        _BASELINE[alias] = _row_counts(connections[alias])
    return _BASELINE[alias]


class CommittedTestCase(TransactionTestCase):
    """Base for tests that need committed rows: durable admission, worker threads, or a second connection.

    Django's teardown truncates every table and replays Nautobot's ``post_migrate`` handlers after each test.
    This base empties only the tables whose row count changed, and falls back to that full flush when a test
    changed a table that Nautobot's migrations populate. In-place edits of migrated rows are not detected; a
    test making them must restore them itself.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for alias in cls._databases_names(include_mirrors=False):
            _baseline(alias)

    def _fixture_teardown(self):
        for alias in self._databases_names(include_mirrors=False):
            connection = connections[alias]
            baseline = _baseline(alias)
            changed = [table for table, count in _row_counts(connection).items() if count != baseline.get(table)]
            if any(baseline.get(table) for table in changed):
                call_command("flush", verbosity=0, interactive=False, database=alias, reset_sequences=False)
            elif changed and connection.features.can_defer_constraint_checks:
                # Deferred foreign keys let one transaction delete the few rows in any order; far cheaper than TRUNCATE.
                quote = connection.ops.quote_name
                with transaction.atomic(using=alias), connection.cursor() as cursor:
                    for table in changed:
                        cursor.execute(f"DELETE FROM {quote(table)}")
            elif changed:
                connection.ops.execute_sql_flush(connection.ops.sql_flush(no_style(), changed, allow_cascade=True))
