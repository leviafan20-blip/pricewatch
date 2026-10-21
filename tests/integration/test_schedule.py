"""Actual Beat -> Redis -> prefork worker -> PostgreSQL + controlled HTTP."""

from datetime import timedelta
import os
import time
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
import pytest
import redis

from apps.monitoring import checks
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.scheduling import configure_schedule
from apps.monitoring.tasks import dispatch_scheduled_checks_task, publish_check
from config.celery import app
from tests.integration.http_server import counter_key
from tests.integration.test_worker import fixture_url, wait_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(os.environ.get("PRICEWATCH_RUN_INTEGRATION") != "1", reason="requires Docker integration worker and Beat"),
]


@pytest.fixture(autouse=True)
def schedule_environment():
    assert settings.SETTINGS_MODULE == "config.settings.integration"
    assert connection.settings_dict["NAME"] == "test_pricewatch"
    assert not app.conf.task_always_eager and app.conf.timezone == "UTC"
    yield
    # This named DB belongs only to the integration suite. Pause before Django's
    # transactional-test teardown, so Beat cannot race the subsequent SQL flush.
    Monitor.objects.filter(auto_enabled=True).update(auto_enabled=False, next_check_at=None)
    CheckRun.objects.filter(status="queued", scheduled_for__isnull=False).update(
        status="cancelled", finished_at=timezone.now(), next_delivery_at=None,
    )
    wait_for(lambda: not CheckRun.objects.filter(status="running", scheduled_for__isnull=False).exists())


@pytest.fixture
def broker():
    client = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    assert client.ping()
    yield client
    client.close()


@pytest.fixture
def token():
    return uuid.uuid4().hex


def make_monitor(user, token, outcome="success"):
    return checks.create_monitor(user=user, name="Автоматическая лампа", url=fixture_url(token, outcome))


def schedule(monitor, user, *, enabled=True, hours=1):
    return configure_schedule(monitor.pk, user=user, enabled=enabled, interval_hours=hours)


def wait_run(monitor, status="succeeded", count=1):
    return wait_for(lambda: monitor.check_runs.filter(status=status).order_by("-queued_at").first()
                    if monitor.check_runs.filter(status=status).count() >= count else None)


def test_beat_automatically_creates_one_observation(user, token, broker):
    monitor = make_monitor(user, token)
    enabled_at = timezone.now()
    schedule(monitor, user)
    run = wait_run(monitor)
    monitor.refresh_from_db()
    assert run.scheduled_for >= enabled_at and run.delivery_attempts == 1
    assert run.next_delivery_at is None
    assert monitor.next_check_at - monitor.last_scheduled_at == timedelta(hours=1)
    assert monitor.last_scheduled_at.tzinfo is not None
    assert Observation.objects.filter(check_run__monitor=monitor).count() == 1
    time.sleep(3)
    assert monitor.check_runs.count() == 1
    assert broker.get(counter_key(token, "requests")) == "1"


def test_pause_resume_and_archive_with_live_beat(user, token, broker):
    monitor = make_monitor(user, token)
    schedule(monitor, user)
    first = wait_run(monitor)
    schedule(monitor, user, enabled=False)
    time.sleep(3)
    assert monitor.check_runs.count() == 1
    monitor.refresh_from_db()
    assert monitor.next_check_at is None
    schedule(monitor, user)
    second = wait_run(monitor, count=2)
    assert first.pk != second.pk
    assert Observation.objects.filter(check_run__monitor=monitor).count() == 2
    checks.archive_monitor(monitor.pk, user=user)
    time.sleep(3)
    assert monitor.check_runs.count() == 2
    assert broker.get(counter_key(token, "requests")) == "2"


def test_overdue_intervals_are_coalesced_after_outage(user, token, broker):
    monitor = make_monitor(user, token)
    overdue = timezone.now() - timedelta(days=14)
    with transaction.atomic():
        schedule(monitor, user)
        Monitor.objects.filter(pk=monitor.pk).update(next_check_at=overdue)
    run = wait_run(monitor)
    monitor.refresh_from_db()
    assert run.scheduled_for == overdue
    assert monitor.next_check_at > timezone.now() + timedelta(minutes=59)
    time.sleep(3)
    assert monitor.check_runs.count() == 1 and broker.get(counter_key(token, "requests")) == "1"


def test_multiple_users_have_independent_scheduled_history(user, token, broker, django_user_model):
    other = django_user_model.objects.create_user(username="scheduled-other")
    second_token = uuid.uuid4().hex
    first, second = make_monitor(user, token), make_monitor(other, second_token)
    schedule(first, user, hours=3)
    # This case checks owner isolation, not same-host concurrency. The shared
    # site limiter may correctly reject simultaneous requests to the fixture.
    first_run = wait_run(first)
    schedule(second, other, hours=12)
    second_run = wait_run(second)
    assert first_run.observation.check_run.monitor.owner_id == user.pk
    assert second_run.observation.check_run.monitor.owner_id == other.pk
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.next_check_at - first.last_scheduled_at == timedelta(hours=3)
    assert second.next_check_at - second.last_scheduled_at == timedelta(hours=12)
    assert broker.get(counter_key(token, "requests")) == broker.get(counter_key(second_token, "requests")) == "1"


def test_live_dispatch_deliveries_and_beat_do_not_duplicate_checks(user, token, broker):
    monitor = make_monitor(user, token, "slow")
    schedule(monitor, user)
    for _ in range(3):
        dispatch_scheduled_checks_task.apply_async(expires=5)
    run = wait_run(monitor)
    time.sleep(3)
    assert monitor.check_runs.count() == 1
    assert Observation.objects.filter(check_run=run).count() == 1
    assert broker.get(counter_key(token, "requests")) == "1"
    assert broker.get(counter_key(token, "overlap")) is None


def test_beat_recovers_persisted_unpublished_reservation(user, token, broker):
    monitor = make_monitor(user, token)
    abandoned = timezone.now() - timedelta(minutes=10)
    with transaction.atomic():
        schedule(monitor, user)
        monitor = Monitor.objects.select_for_update().get(pk=monitor.pk)
        run, created = checks._get_or_create_run_locked(monitor, scheduled_for=abandoned, now=abandoned)
        assert created and checks._reserve_delivery_locked(run, abandoned)
        monitor.last_scheduled_at = abandoned
        monitor.next_check_at = timezone.now() + timedelta(hours=1)
        monitor.save(update_fields=["last_scheduled_at", "next_check_at"])
    completed = wait_run(monitor)
    assert completed.pk == run.pk and completed.delivery_attempts == 2
    assert monitor.check_runs.count() == Observation.objects.filter(check_run=run).count() == 1
    assert broker.get(counter_key(token, "requests")) == "1"


def test_beat_recovers_manual_commit_gap_with_schedule_disabled(user, token, broker):
    monitor = make_monitor(user, token)
    # No message reaches Redis. Only Beat can repair this; there is no repeat POST.
    run = checks.request_check(monitor.pk, user=user, publish=lambda _: None).run
    CheckRun.objects.filter(pk=run.pk).update(next_delivery_at=timezone.now() - timedelta(seconds=1))
    completed = wait_run(monitor)
    assert completed.pk == run.pk and completed.scheduled_for is None
    assert completed.delivery_attempts == 2 and completed.execution_attempts == 1
    assert not monitor.auto_enabled and monitor.next_check_at is None
    assert monitor.check_runs.count() == Observation.objects.filter(check_run=run).count() == 1
    assert broker.get(counter_key(token, "requests")) == "1"
    publish_check(str(run.pk))
    time.sleep(1)
    completed.refresh_from_db()
    assert completed.execution_attempts == 1
    assert Observation.objects.filter(check_run=run).count() == 1


def test_automatic_parser_error_preserves_previous_price(user, token, broker):
    monitor = make_monitor(user, token)
    schedule(monitor, user)
    prior = wait_run(monitor).observation
    with transaction.atomic():
        checks.update_monitor(monitor.pk, user=user, name=monitor.name, url=fixture_url(token, "missing"))
        Monitor.objects.filter(pk=monitor.pk).update(next_check_at=timezone.now())
    failed = wait_run(monitor, status="failed")
    assert failed.error_code == "missing_price"
    assert Observation.objects.filter(check_run__monitor=monitor).get().pk == prior.pk
    monitor.refresh_from_db()
    assert monitor.auto_enabled and monitor.next_check_at > timezone.now()
    time.sleep(3)
    assert monitor.check_runs.count() == 2 and broker.get(counter_key(token, "requests")) == "2"


def test_late_queued_automatic_message_is_cancelled_after_pause(user, token, broker):
    monitor = make_monitor(user, token)
    with transaction.atomic():
        schedule(monitor, user)
        monitor = Monitor.objects.select_for_update().get(pk=monitor.pk)
        run, _ = checks._get_or_create_run_locked(monitor, scheduled_for=timezone.now())
        schedule(monitor, user, enabled=False)
    publish_check(str(run.pk))
    time.sleep(3)
    run.refresh_from_db()
    assert run.status == "cancelled"
    assert not Observation.objects.filter(check_run=run).exists()
    assert broker.get(counter_key(token, "requests")) is None
