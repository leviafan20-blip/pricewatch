"""Real PostgreSQL transactions and locks; no live broker or product website."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
import pytest

from apps.monitoring import checks
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.scheduling import configure_schedule, dispatch_due_checks
from tests.monitoring.test_checks import in_thread

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def clock(monkeypatch):
    value = timezone.now().replace(microsecond=0)
    monkeypatch.setattr(timezone, "now", lambda: value)
    return value


def enable(monitor, user, clock, hours=6):
    return configure_schedule(monitor.pk, user=user, enabled=True, interval_hours=hours, now=clock)


@pytest.mark.parametrize("hours", [1, 3, 6, 12, 24])
def test_supported_intervals_and_first_due_now(monitor, user, clock, hours):
    monitor = enable(monitor, user, clock, hours)
    assert monitor.auto_enabled and monitor.check_interval_hours == hours
    assert monitor.next_check_at == clock and monitor.last_scheduled_at is None
    assert CheckRun.objects.count() == 0


@pytest.mark.parametrize("hours", [0, 2, 5, 25, -1, True, "3", 1.0, None])
def test_invalid_interval_does_not_modify_schedule(monitor, user, clock, hours):
    with pytest.raises(ValidationError):
        enable(monitor, user, clock, hours)
    monitor.refresh_from_db()
    assert not monitor.auto_enabled and monitor.next_check_at is None


def test_dispatch_commit_precedes_publication_and_repeated_tick_is_noop(monitor, user, clock):
    enable(monitor, user, clock, 3)
    def publish(run_id):
        assert not connection.in_atomic_block
        run = CheckRun.objects.get(pk=run_id)
        assert run.scheduled_for == clock and run.delivery_attempts == 1
        assert run.next_delivery_at == clock + timedelta(minutes=5)
    first = dispatch_due_checks(publish=publish, now=clock)
    second = dispatch_due_checks(publish=Mock(side_effect=AssertionError("duplicate")), now=clock)
    monitor.refresh_from_db()
    assert first["created"] == first["published"] == 1
    assert not any(second.values())
    assert CheckRun.objects.count() == 1
    assert monitor.last_scheduled_at == clock
    assert monitor.next_check_at == clock + timedelta(hours=3)


def test_pause_resume_cancels_only_scheduled_queue_and_preserves_last_time(monitor, user, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    first = CheckRun.objects.get()
    paused = configure_schedule(monitor.pk, user=user, enabled=False, interval_hours=6, now=clock)
    first.refresh_from_db()
    assert first.status == "cancelled" and first.next_delivery_at is None
    assert not paused.auto_enabled and paused.next_check_at is None
    assert paused.last_scheduled_at == clock
    assert not any(dispatch_due_checks(publish=Mock(), now=clock).values())
    resumed_at = clock + timedelta(minutes=1)
    monkeypatch.setattr(timezone, "now", lambda: resumed_at)
    resumed = enable(monitor, user, resumed_at)
    assert resumed.next_check_at == resumed_at
    assert dispatch_due_checks(publish=Mock(), now=resumed_at)["created"] == 1
    assert CheckRun.objects.count() == 2
    checks.archive_monitor(monitor.pk, user=user)
    monitor.refresh_from_db()
    assert monitor.archived_at and not monitor.auto_enabled and monitor.next_check_at is None


def test_unchanged_schedule_keeps_due_time_and_interval_change_resets_it(monitor, user, clock):
    enable(monitor, user, clock, 6)
    dispatch_due_checks(publish=Mock(), now=clock)
    later = clock + timedelta(minutes=10)
    unchanged = enable(monitor, user, later, 6)
    assert unchanged.next_check_at == clock + timedelta(hours=6)
    changed = enable(monitor, user, later, 12)
    assert changed.next_check_at == later + timedelta(hours=12)
    assert changed.last_scheduled_at == clock and CheckRun.objects.count() == 1


def test_long_outage_coalesces_to_one_check(monitor, user, clock):
    enable(monitor, user, clock, 1)
    overdue = clock - timedelta(days=30)
    Monitor.objects.filter(pk=monitor.pk).update(next_check_at=overdue)
    publish = Mock()
    stats = dispatch_due_checks(publish=publish, now=clock)
    run = CheckRun.objects.get()
    monitor.refresh_from_db()
    assert stats["created"] == 1 and publish.call_count == 1
    assert run.scheduled_for == overdue and run.queued_at == clock
    assert monitor.next_check_at == clock + timedelta(hours=1)


def test_manual_active_check_coalesces_cycle_without_duplicate(monitor, user, clock):
    manual = checks.request_check(monitor.pk, user=user, publish=Mock()).run
    enable(monitor, user, clock)
    stats = dispatch_due_checks(publish=Mock(side_effect=AssertionError("duplicate")), now=clock)
    monitor.refresh_from_db()
    assert stats["coalesced"] == 1 and stats["created"] == 0
    assert CheckRun.objects.get().pk == manual.pk
    assert monitor.last_scheduled_at is None
    configure_schedule(monitor.pk, user=user, enabled=False, interval_hours=6)
    manual.refresh_from_db()
    assert manual.status == "queued"


def test_disabled_archived_inactive_and_future_monitors_are_skipped(user, clock):
    def make(name):
        return checks.create_monitor(user=user, name=name, url="https://shop.example/item")
    disabled, archived, inactive, future = [make(name) for name in ["off", "archive", "inactive", "future"]]
    enable(archived, user, clock)
    checks.archive_monitor(archived.pk, user=user)
    enable(future, user, clock)
    Monitor.objects.filter(pk=future.pk).update(next_check_at=clock + timedelta(days=1))
    enable(inactive, user, clock)
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert not any(dispatch_due_checks(publish=Mock(), now=clock).values())
    assert not CheckRun.objects.exists()


def test_foreign_or_archived_schedule_cannot_be_changed(monitor, user, django_user_model, clock):
    other = django_user_model.objects.create_user(username="other")
    with pytest.raises(Monitor.DoesNotExist):
        enable(monitor, other, clock)
    checks.archive_monitor(monitor.pk, user=user)
    with pytest.raises(Monitor.DoesNotExist):
        enable(monitor, user, clock)


def test_broker_error_does_not_damage_previous_observation(monitor, user, product, clock, monkeypatch):
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    prior = checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk).observation
    enable(monitor, user, clock)
    stats = dispatch_due_checks(publish=Mock(side_effect=OSError("secret")), now=clock)
    failed = CheckRun.objects.get(scheduled_for__isnull=False)
    assert stats["failed"] == 1 and failed.error_code == "queue_unavailable"
    assert failed.status == "failed" and failed.next_delivery_at is None
    assert "secret" not in failed.error_message
    assert Observation.objects.get().pk == prior.pk


def test_publish_crash_gap_recovers_same_id_and_has_bounded_attempts(monitor, user, clock):
    enable(monitor, user, clock)
    publish = Mock()  # Accepted reservation, but simulate no message reaching worker.
    dispatch_due_checks(publish=publish, now=clock)
    run = CheckRun.objects.get()
    for minute in [5, 10]:
        stats = dispatch_due_checks(publish=publish, now=clock + timedelta(minutes=minute))
        assert stats["created"] == 0 and stats["recovered"] == 1
        assert publish.call_args.args == (str(run.pk),)
    stats = dispatch_due_checks(publish=publish, now=clock + timedelta(minutes=15))
    run.refresh_from_db()
    assert stats["failed"] == 1 and run.error_code == "queue_delivery_timeout"
    assert run.status == "failed" and run.next_delivery_at is None
    assert publish.call_count == 3 and run.delivery_attempts == 3
    assert CheckRun.objects.count() == 1 and Observation.objects.count() == 0


def test_late_delivery_and_completed_slot_are_idempotent(monitor, user, product, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    inspect = Mock(return_value=product)
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    assert checks.execute_check(run.pk).status == "succeeded"
    assert checks.execute_check(run.pk).status == "succeeded"
    Monitor.objects.filter(pk=monitor.pk).update(next_check_at=clock)
    stats = dispatch_due_checks(publish=Mock(), now=clock)
    assert not stats["created"] and not stats["published"]
    assert CheckRun.objects.count() == Observation.objects.count() == inspect.call_count == 1


def test_manual_post_does_not_republish_recent_scheduled_attempt(monitor, user, clock):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    publish = Mock()
    result = checks.request_check(monitor.pk, user=user, publish=publish)
    assert not result.created and not result.published and not publish.called


def test_live_session_lock_prevents_recovery_publish(monitor, user, product, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    started, release = threading.Event(), threading.Event()
    def inspect(*_):
        started.set()
        assert release.wait(10)
        return product
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(in_thread, lambda: checks.execute_check(run.pk))
        try:
            assert started.wait(10)
            stats = dispatch_due_checks(publish=Mock(side_effect=AssertionError("live")), now=clock + timedelta(minutes=5))
            assert stats["busy"] == 1 and not stats["published"]
        finally:
            release.set()
        assert future.result(timeout=10).status == "succeeded"
    assert Observation.objects.count() == 1


def test_orphaned_running_delivery_is_recovered(monitor, user, product, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    checks._claim_run(run.pk, monitor.pk)
    stats = dispatch_due_checks(publish=Mock(), now=clock + timedelta(minutes=5))
    assert stats["recovered"] == 1
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    assert checks.execute_check(run.pk).status == "succeeded"
    assert Observation.objects.count() == 1


def test_pause_guards_worker_even_if_cancel_update_was_bypassed(monitor, user, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    Monitor.objects.filter(pk=monitor.pk).update(auto_enabled=False, next_check_at=None)
    inspect = Mock(side_effect=AssertionError("paused"))
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    assert checks.execute_check(run.pk).status == "cancelled" and not inspect.called


def test_already_running_check_can_finish_after_pause(monitor, user, product, clock, monkeypatch):
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    def inspect(*_):
        configure_schedule(monitor.pk, user=user, enabled=False, interval_hours=6)
        return product
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    assert checks.execute_check(run.pk).status == "succeeded"


def test_concurrent_dispatchers_create_and_publish_once(monitor, user, clock):
    enable(monitor, user, clock)
    barrier = threading.Barrier(2)
    calls = []
    def dispatch():
        barrier.wait(timeout=10)
        return dispatch_due_checks(publish=calls.append, now=clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(in_thread, dispatch) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(item["created"] for item in results) == 1
    assert sum(item["published"] for item in results) == 1
    assert len(calls) == CheckRun.objects.count() == 1


def test_locked_monitor_is_skipped_without_waiting(monitor, user, clock):
    enable(monitor, user, clock)
    locked, release = threading.Event(), threading.Event()
    def hold():
        with transaction.atomic():
            Monitor.objects.select_for_update().get(pk=monitor.pk)
            locked.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(in_thread, hold)
        try:
            assert locked.wait(10)
            assert not any(dispatch_due_checks(publish=Mock(), now=clock).values())
        finally:
            release.set()
        future.result(timeout=10)


def test_dispatch_batch_is_bounded_and_does_not_starve_remaining(user, clock):
    for i in range(25):
        monitor = checks.create_monitor(user=user, name=str(i), url="https://shop.example/item")
        enable(monitor, user, clock)
    publish = Mock()
    assert dispatch_due_checks(publish=publish, now=clock)["created"] == 20
    assert dispatch_due_checks(publish=publish, now=clock)["created"] == 5
    assert CheckRun.objects.count() == 25 and publish.call_count == 25


def test_interval_uses_utc_across_dst_and_database_session_is_utc(monitor, user):
    local = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    enable(monitor, user, local, 3)
    dispatch_due_checks(publish=Mock(), now=local)
    monitor.refresh_from_db()
    assert monitor.next_check_at == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert monitor.next_check_at - local.astimezone(UTC) == timedelta(hours=3)
    with connection.cursor() as cursor:
        cursor.execute("SHOW TIME ZONE")
        assert cursor.fetchone()[0] == "UTC"


def test_naive_time_is_rejected(monitor, user):
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        enable(monitor, user, naive)
    with pytest.raises(ValueError):
        dispatch_due_checks(publish=Mock(), now=naive)


def test_database_constraints_for_schedule_and_slot(monitor, user, clock, product, monkeypatch):
    with pytest.raises(IntegrityError), transaction.atomic():
        Monitor.objects.filter(pk=monitor.pk).update(auto_enabled=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        Monitor.objects.filter(pk=monitor.pk).update(check_interval_hours=2)
    enable(monitor, user, clock)
    dispatch_due_checks(publish=Mock(), now=clock)
    run = CheckRun.objects.get()
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    checks.execute_check(run.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url, scheduled_for=clock)
    run.refresh_from_db()
    run.scheduled_for += timedelta(seconds=1)
    with pytest.raises(ValidationError):
        run.save()
