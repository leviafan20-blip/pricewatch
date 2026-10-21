"""Database-driven intervals and bounded, restart-safe scheduled publication."""

from datetime import UTC, timedelta
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .checks import (
    ACTIVE_STATUSES, DELIVERY_RETRY_SECONDS, EXECUTION_LIMIT_ERROR,
    MAX_DELIVERY_ATTEMPTS, MAX_EXECUTION_ATTEMPTS, CheckQueueError,
    _get_or_create_run_locked, _monitor_execution_lock, _publish_run,
    _reserve_delivery_locked,
)
from .models import CheckRun, Monitor

logger = logging.getLogger(__name__)
DISPATCH_BATCH_SIZE = 20


def _utc_now(now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise ValueError("Scheduler timestamps must be timezone-aware")
    return now.astimezone(UTC)


def configure_schedule(monitor_id, *, user, enabled, interval_hours, now=None):
    now = _utc_now(now)
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor_id, owner=user, archived_at__isnull=True)
        if not isinstance(enabled, bool):
            raise ValidationError("Некорректное состояние расписания.")
        if isinstance(interval_hours, bool) or not isinstance(interval_hours, int) or interval_hours not in Monitor.Interval.values:
            raise ValidationError("Выберите интервал: 1, 3, 6, 12 или 24 часа.")
        was_enabled = monitor.auto_enabled
        interval_changed = monitor.check_interval_hours != interval_hours
        monitor.auto_enabled = enabled
        monitor.check_interval_hours = interval_hours
        if not enabled:
            monitor.next_check_at = None
            # A paused schedule must not start already queued automatic work.
            monitor.check_runs.filter(status=CheckRun.Status.QUEUED, scheduled_for__isnull=False).update(
                status=CheckRun.Status.CANCELLED, finished_at=now, next_delivery_at=None,
            )
        elif not was_enabled:
            monitor.next_check_at = now
        elif interval_changed:
            monitor.next_check_at = now + timedelta(hours=interval_hours)
        monitor.save(update_fields=["auto_enabled", "check_interval_hours", "next_check_at", "updated_at"])
    return monitor


def dispatch_due_checks(*, publish, now=None, batch_size=DISPATCH_BATCH_SIZE):
    """Claim at most one current attempt per monitor, then publish outside locks.

    Missed intervals are coalesced: the next time is based on NOW, never a loop
    over the outage. A bounded delivery reservation repairs a publish crash gap.
    """
    now = _utc_now(now)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= DISPATCH_BATCH_SIZE:
        raise ValueError("Invalid scheduler batch size")
    stats = {"created": 0, "published": 0, "recovered": 0, "coalesced": 0, "failed": 0, "busy": 0}
    reservations = []
    delivery_due = CheckRun.objects.filter(
        monitor_id=OuterRef("pk"),
        status__in=ACTIVE_STATUSES, next_delivery_at__lte=now,
    )
    with transaction.atomic(durable=True):
        monitors = Monitor.objects.select_related("owner").annotate(delivery_due=Exists(delivery_due)).filter(
            Q(auto_enabled=True, archived_at__isnull=True, owner__is_active=True, next_check_at__lte=now)
            | Q(delivery_due=True)
        ).order_by(
            "-delivery_due", "next_check_at", "pk",
        ).select_for_update(of=("self",), skip_locked=True)[:batch_size]
        for monitor in monitors:
            if (monitor.auto_enabled and monitor.archived_at is None and monitor.owner.is_active
                    and monitor.next_check_at is not None and monitor.next_check_at <= now):
                due = monitor.next_check_at
                run, created = _get_or_create_run_locked(monitor, scheduled_for=due, now=now)
                monitor.next_check_at = now + timedelta(hours=monitor.check_interval_hours)
                if created:
                    monitor.last_scheduled_at = now
                    stats["created"] += 1
                else:
                    stats["coalesced"] += 1
                monitor.save(update_fields=["next_check_at", "last_scheduled_at", "updated_at"])
            else:
                run = monitor.check_runs.filter(status__in=ACTIVE_STATUSES).first()
            if run is None or run.status not in ACTIVE_STATUSES:
                continue
            if run.next_delivery_at is not None and run.next_delivery_at > now:
                continue
            # A live worker's session lock never expires merely because it is slow.
            with _monitor_execution_lock(monitor.pk) as execution_free:
                if not execution_free:
                    run.next_delivery_at = now + timedelta(seconds=DELIVERY_RETRY_SECONDS)
                    run.save(update_fields=["next_delivery_at"])
                    stats["busy"] += 1
                    continue
                if monitor.archived_at is not None or not monitor.owner.is_active or (
                    run.scheduled_for is not None and not monitor.auto_enabled
                ):
                    run.status = CheckRun.Status.CANCELLED
                    run.finished_at = now
                    run.claim_token = None
                    run.next_delivery_at = None
                    run.save(update_fields=["status", "finished_at", "claim_token", "next_delivery_at"])
                    continue
                if (run.delivery_attempts >= MAX_DELIVERY_ATTEMPTS
                        or run.execution_attempts >= MAX_EXECUTION_ATTEMPTS):
                    run.status = CheckRun.Status.FAILED
                    run.finished_at = now
                    run.claim_token = None
                    run.next_delivery_at = None
                    run.error_code = "queue_delivery_timeout"
                    run.error_message = "Не удалось подтвердить выполнение задания после повторных отправок."
                    if run.execution_attempts >= MAX_EXECUTION_ATTEMPTS:
                        run.error_code, run.error_message = EXECUTION_LIMIT_ERROR
                    run.save(update_fields=["status", "finished_at", "claim_token", "next_delivery_at", "error_code", "error_message"])
                    stats["failed"] += 1
                    continue
                recovering = run.delivery_attempts > 0
                if _reserve_delivery_locked(run, now):
                    reservations.append(run)
                    if recovering:
                        stats["recovered"] += 1
    for run in reservations:
        try:
            _publish_run(run, publish)
        except CheckQueueError:
            stats["failed"] += 1
        else:
            stats["published"] += 1
    if any(stats.values()):
        logger.info("Schedule dispatch: %s", stats)
    return stats
