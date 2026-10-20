"""Thin queue adapter. CheckRun, not a Celery result backend, stores the result."""

from celery import shared_task

from config.celery import app

from .checks import execute_check


@shared_task(
    name="monitoring.execute_check",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=70,
    time_limit=80,
)
def execute_check_task(run_id):
    execute_check(run_id)


def publish_check(run_id):
    # A fresh connection avoids an unbounded wait for a pooled producer. Redis
    # connect/read timeouts and disabled publication retries bound the web wait.
    with app.connection_for_write(connect_timeout=1) as connection:
        execute_check_task.apply_async(
            args=[str(run_id)], task_id=str(run_id),
            connection=connection, retry=False,
        )


@shared_task(name="monitoring.dispatch_due_checks", ignore_result=True, soft_time_limit=50, time_limit=55)
def dispatch_scheduled_checks_task():
    from .scheduling import dispatch_due_checks

    dispatch_due_checks(publish=publish_check)
