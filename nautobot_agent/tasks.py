"""Celery entry points for durable Nautobot Agent runs."""

from __future__ import annotations

from uuid import UUID

from nautobot.core.celery import app, nautobot_task

from nautobot_agent.app_settings import CHAT_RUN_SHUTDOWN_MARGIN
from nautobot_agent.chat.execution import execute_run


def publish_run(  # noqa: PLR0913  # implements the RunPublisher protocol signature
    run_id: UUID,
    user_id: UUID,
    queue: str | None,
    *,
    soft: int,
    hard: int,
    cleanup: int,
) -> None:
    """Publish one run UUID with bounded, scoped broker resources and no retries."""
    connection = app.connection_for_write(
        connect_timeout=1,
        transport_options={
            **app.conf.broker_transport_options,
            "socket_timeout": 1,
            "socket_connect_timeout": 1,
            "retry_on_timeout": False,
            "max_retries": 0,
        },
    )
    with connection, connection.Producer(serializer=app.conf.task_serializer) as producer:
        execute_chat_run.apply_async(
            args=(str(run_id),),
            producer=producer,
            queue=queue,
            soft_time_limit=soft + cleanup + CHAT_RUN_SHUTDOWN_MARGIN,
            time_limit=hard,
            serializer=app.conf.task_serializer,
            retry=False,
            nautobot_job_user_id=str(user_id),
        )


@nautobot_task(
    name="nautobot_agent.execute_chat_run",
    ignore_result=True,
    store_errors_even_if_ignored=False,
    acks_late=False,
    max_retries=0,
)
def execute_chat_run(run_id: str) -> None:
    """Execute one queued run using its database-fenced authority."""
    execute_run(run_id)
