"""PostgreSQL publication reservations and execution budgets after process loss."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from dataclasses import replace
import threading
from unittest.mock import Mock

from django.db import connection
from django.utils import timezone
import pytest

from apps.monitoring import checks
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.scheduling import dispatch_due_checks
from apps.notifications.models import Notification
from tests.monitoring.test_checks import in_thread, queued

pytestmark = pytest.mark.django_db(transaction=True)


class ProcessLost(BaseException):
    """Bypass normal exception handling, like an abruptly lost process."""


def test_manual_commit_gap_is_recovered_without_post_or_schedule(monitor, user):
    def lost(_):
        assert not connection.in_atomic_block
        raise ProcessLost

    with pytest.raises(ProcessLost):
        checks.request_check(monitor.pk, user=user, publish=lost)
    run = CheckRun.objects.get()
    assert run.status == "queued" and run.delivery_attempts == 1
    assert not monitor.auto_enabled and monitor.next_check_at is None

    def publish(run_id):
        assert not connection.in_atomic_block
        assert run_id == str(run.pk)
        assert CheckRun.objects.get(pk=run_id).delivery_attempts == 2

    now = run.next_delivery_at
    assert not any(dispatch_due_checks(publish=Mock(side_effect=AssertionError("early")), now=now - timedelta(microseconds=1)).values())
    assert dispatch_due_checks(publish=publish, now=now)["recovered"] == 1
    assert not any(dispatch_due_checks(publish=Mock(side_effect=AssertionError("duplicate")), now=now).values())
    assert CheckRun.objects.count() == 1
    monitor.refresh_from_db()
    assert monitor.next_check_at is None and monitor.last_scheduled_at is None


@pytest.mark.parametrize("manual_post", [False, True])
def test_manual_delivery_budget_is_shared_by_post_and_dispatcher(monitor, user, monkeypatch, manual_post):
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    publish = Mock()
    run = checks.request_check(monitor.pk, user=user, publish=publish).run
    for _ in range(2):
        clock[0] += timedelta(seconds=checks.DELIVERY_RETRY_SECONDS)
        if manual_post:
            assert checks.request_check(monitor.pk, user=user, publish=publish).published
        else:
            assert dispatch_due_checks(publish=publish)["recovered"] == 1
    clock[0] += timedelta(seconds=checks.DELIVERY_RETRY_SECONDS)
    assert not checks.request_check(monitor.pk, user=user, publish=publish).published
    assert dispatch_due_checks(publish=publish)["failed"] == 1
    run.refresh_from_db()
    assert run.status == "failed" and run.error_code == "queue_delivery_timeout"
    assert run.delivery_attempts == publish.call_count == 3 and run.execution_attempts == 0
    assert run.next_delivery_at is None and not Observation.objects.exists()
    inspect = Mock(side_effect=AssertionError("terminal delivery"))
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    checks.execute_check(run.pk)
    assert inspect.call_count == 0 and CheckRun.objects.count() == 1


def test_manual_post_and_dispatcher_compete_for_one_reservation(monitor, user):
    run = queued(monitor, user)
    CheckRun.objects.filter(pk=run.pk).update(next_delivery_at=timezone.now() - timedelta(seconds=1))
    calls = []
    barrier = threading.Barrier(2)

    def post():
        barrier.wait(timeout=10)
        return checks.request_check(monitor.pk, user=user, publish=calls.append)

    def dispatch():
        barrier.wait(timeout=10)
        return dispatch_due_checks(publish=calls.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(in_thread, post), pool.submit(in_thread, dispatch)]
        for future in futures:
            future.result(timeout=10)
    assert calls == [str(run.pk)] and CheckRun.objects.count() == 1
    run.refresh_from_db()
    assert run.delivery_attempts == 2 and run.execution_attempts == 0


def test_live_manual_worker_is_not_republished_or_charged(monitor, user, product, monkeypatch):
    run = queued(monitor, user)
    started, release = threading.Event(), threading.Event()

    def inspect(*_):
        assert not connection.in_atomic_block
        started.set()
        assert release.wait(10)
        return product

    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(in_thread, lambda: checks.execute_check(run.pk))
        try:
            assert started.wait(10)
            CheckRun.objects.filter(pk=run.pk).update(next_delivery_at=timezone.now() - timedelta(seconds=1))
            assert checks.execute_check(run.pk) is None
            assert dispatch_due_checks(publish=Mock(side_effect=AssertionError("live")))["busy"] == 1
            run.refresh_from_db()
            assert run.delivery_attempts == run.execution_attempts == 1
        finally:
            release.set()
        assert future.result(timeout=10).status == "succeeded"
    assert Observation.objects.count() == 1


@pytest.mark.parametrize("via_dispatcher", [False, True])
def test_execution_limit_preserves_history_notifications_and_fences_old_claim(monitor, user, product, monkeypatch, via_dispatcher):
    Monitor.objects.filter(pk=monitor.pk).update(notification_mode="any")
    monkeypatch.setattr(checks.services, "inspect_url", Mock(return_value=product))
    checks.execute_check(queued(monitor, user).pk)
    monkeypatch.setattr(checks.services, "inspect_url", Mock(return_value=replace(product, price=Decimal("100"))))
    checks.execute_check(queued(monitor, user).pk)
    observations = list(Observation.objects.values_list("pk", flat=True))
    notices = list(Notification.objects.values_list("pk", flat=True))
    assert len(observations) == 2 and len(notices) == 1
    run = queued(monitor, user)
    inspect = Mock(side_effect=ProcessLost)
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    for count in range(1, 4):
        with pytest.raises(ProcessLost):
            checks.execute_check(run.pk)
        run.refresh_from_db()
        assert run.status == "running" and run.execution_attempts == count
        assert run.delivery_attempts == 1
    old_token = run.claim_token
    if via_dispatcher:
        CheckRun.objects.filter(pk=run.pk).update(next_delivery_at=timezone.now() - timedelta(seconds=1))
        assert dispatch_due_checks(publish=Mock(side_effect=AssertionError("exhausted")))["failed"] == 1
    else:
        checks.execute_check(run.pk)
    checks.execute_check(run.pk)  # Further late deliveries are terminal no-ops.
    checks._finish_run(run.pk, monitor.pk, old_token, product=product)
    run.refresh_from_db()
    assert inspect.call_count == run.execution_attempts == 3
    assert run.status == "failed" and run.error_code == "execution_attempts_exhausted"
    assert run.claim_token is None and run.next_delivery_at is None and run.finished_at is not None
    assert list(Observation.objects.values_list("pk", flat=True)) == observations
    assert list(Notification.objects.values_list("pk", flat=True)) == notices


def test_third_execution_can_succeed_and_late_delivery_does_not_spend_budget(monitor, user, product, monkeypatch):
    run = queued(monitor, user)
    inspect = Mock(side_effect=[ProcessLost, ProcessLost, product])
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    for _ in range(2):
        with pytest.raises(ProcessLost):
            checks.execute_check(run.pk)
    assert checks.execute_check(run.pk).status == "succeeded"
    assert checks.execute_check(run.pk).status == "succeeded"
    run.refresh_from_db()
    assert run.execution_attempts == inspect.call_count == 3
    assert Observation.objects.filter(check_run=run).count() == 1


def test_cancelled_delivery_never_spends_execution_budget(monitor, user, monkeypatch):
    run = queued(monitor, user)
    checks.archive_monitor(monitor.pk, user=user)
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=AssertionError("cancelled")))
    assert checks.execute_check(run.pk).status == "cancelled"
    run.refresh_from_db()
    assert run.execution_attempts == 0


@pytest.mark.parametrize("disabled", ["owner", "archive", "schedule"])
def test_orphan_recovery_cancels_ineligible_work(monitor, user, disabled):
    if disabled == "schedule":
        from apps.monitoring.scheduling import configure_schedule
        configure_schedule(monitor.pk, user=user, enabled=True, interval_hours=1)
        dispatch_due_checks(publish=Mock())
        run = CheckRun.objects.get()
    else:
        run = queued(monitor, user)
    checks._claim_run(run.pk, monitor.pk)
    if disabled == "owner":
        user.is_active = False
        user.save(update_fields=["is_active"])
    elif disabled == "archive":
        checks.archive_monitor(monitor.pk, user=user)
    else:
        configure_schedule(monitor.pk, user=user, enabled=False, interval_hours=1)
    CheckRun.objects.filter(pk=run.pk).update(next_delivery_at=timezone.now() - timedelta(seconds=1))
    dispatch_due_checks(publish=Mock(side_effect=AssertionError("ineligible")))
    run.refresh_from_db()
    assert run.status == "cancelled" and run.execution_attempts == 1
    assert run.claim_token is None and run.next_delivery_at is None
