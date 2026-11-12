"""Shared database connection release policy for live-data tools and the checkpoint saver."""

from django.db import connections


def close_connections() -> None:
    """Close this thread's idle connections; threads without an atomic block must not keep them open."""
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close()
