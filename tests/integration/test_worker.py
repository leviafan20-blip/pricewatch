"""Real broker + prefork worker + PostgreSQL + controlled HTTP, never eager."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import threading
import time
import uuid

from django.conf import settings
from django.db import close_old_connections, connection
from django.urls import reverse
import pytest
import redis

from apps.monitoring.checks import CheckQueueError, _claim_run, archive_monitor, create_monitor, request_check, update_monitor
from apps.monitoring.models import CheckRun, Observation
from apps.monitoring.tasks import execute_check_task, publish_check
from config.celery import app
from tests.integration.http_server import counter_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(os.environ.get("PRICEWATCH_RUN_INTEGRATION") != "1", reason="requires the Docker integration profile"),
]


def wait_for(predicate, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Integration condition timed out")


@pytest.fixture(autouse=True)
def actual_services():
    assert settings.SETTINGS_MODULE == "config.settings.integration"
    assert connection.settings_dict["NAME"] == "test_pricewatch"
    assert app.conf.task_always_eager is False
    assert app.conf.task_default_queue == "pricewatch-integration"
    replies = app.control.inspect(timeout=3).ping()
    assert replies and any(key.startswith("integration@") for key in replies)


@pytest.fixture
def broker():
    client = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
    assert client.ping()
    yield client
    client.close()


@pytest.fixture
def token():
    return uuid.uuid4().hex


def fixture_url(token, outcome="success"):
    return f"http://shop.pricewatch.example/products/{token}/{outcome}"


def monitor_for(user, token, outcome="success"):
    return create_monitor(user=user, name="Интеграционная лампа", url=fixture_url(token, outcome))


def completed(run):
    def terminal():
        run.refresh_from_db()
        return run if run.status not in {"queued", "running"} else None
    return wait_for(terminal)


def test_real_worker_saves_extracted_observation(user, token, broker):
    monitor = monitor_for(user, token)
    run = completed(request_check(monitor.pk, user=user, publish=publish_check).run)
    assert run.status == "succeeded", (run.error_code, run.error_message)
    assert run.observation.price == Decimal("1299.95")
    assert run.observation.title == "PriceWatch Desk Lamp"
    assert run.observation.currency == "USD" and run.observation.availability == "in_stock"
    assert int(broker.get(counter_key(token, "requests"))) == 1


@pytest.mark.parametrize("outcome,code", [("missing", "missing_price"), ("error", "http_error")])
def test_real_failures_preserve_previous_data(user, token, broker, outcome, code):
    monitor = monitor_for(user, token)
    previous = completed(request_check(monitor.pk, user=user, publish=publish_check).run).observation
    update_monitor(monitor.pk, user=user, name=monitor.name, url=fixture_url(token, outcome))
    failed = completed(request_check(monitor.pk, user=user, publish=publish_check).run)
    assert failed.status == "failed" and failed.error_code == code
    assert Observation.objects.filter(check_run__monitor=monitor).count() == 1
    previous.refresh_from_db()
    assert previous.price == Decimal("1299.95")
    assert int(broker.get(counter_key(token, "requests"))) == 2


def test_concurrent_submissions_and_real_deliveries_inspect_once(user, token, broker):
    monitor = monitor_for(user, token, "slow")
    barrier = threading.Barrier(2)
    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return request_check(monitor.pk, user=user, publish=publish_check).run.pk
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        ids = [future.result(timeout=15) for future in futures]
    assert ids[0] == ids[1]
    run = CheckRun.objects.get(pk=ids[0])
    wait_for(lambda: broker.get(counter_key(token, "active")) == "1")
    for _ in range(3):
        publish_check(str(run.pk))
    assert completed(run).status == "succeeded"
    # Give duplicate broker deliveries a full opportunity to reach both children.
    time.sleep(1)
    assert CheckRun.objects.filter(monitor=monitor).count() == 1
    assert Observation.objects.filter(check_run=run).count() == 1
    assert int(broker.get(counter_key(token, "requests"))) == 1
    assert broker.get(counter_key(token, "overlap")) is None


def test_terminal_redelivery_and_lost_publish_recovery(user, token, broker):
    monitor = monitor_for(user, token)
    # Simulate web death between committing PostgreSQL and publishing to Redis.
    first = request_check(monitor.pk, user=user, publish=lambda _: None).run
    assert first.status == "queued"
    repaired = request_check(monitor.pk, user=user, publish=publish_check)
    assert not repaired.created and repaired.run.pk == first.pk
    assert completed(first).status == "succeeded"
    observation_id = first.observation.pk
    publish_check(str(first.pk))
    publish_check(str(first.pk))
    time.sleep(1)
    assert Observation.objects.get(check_run=first).pk == observation_id
    assert int(broker.get(counter_key(token, "requests"))) == 1


def test_archived_queued_delivery_does_not_fetch(user, token, broker):
    monitor = monitor_for(user, token)
    run = request_check(monitor.pk, user=user, publish=lambda _: None).run
    archive_monitor(monitor.pk, user=user)
    publish_check(str(run.pk))
    time.sleep(0.7)
    run.refresh_from_db()
    assert run.status == "cancelled"
    assert broker.get(counter_key(token, "requests")) is None
    assert not Observation.objects.filter(check_run=run).exists()


def test_html_post_does_not_wait_for_network(client, user, token, broker):
    client.force_login(user)
    monitor = monitor_for(user, token, "slow")
    started = time.monotonic()
    response = client.post(reverse("monitoring:check", args=[monitor.pk]))
    elapsed = time.monotonic() - started
    assert response.status_code == 302 and elapsed < 0.9
    run = CheckRun.objects.get(monitor=monitor)
    assert completed(run).status == "succeeded"


def test_worker_revalidates_url_instead_of_trusting_database(user, token):
    monitor = monitor_for(user, token)
    # Corrupted/bypassed form input still cannot reach a private endpoint.
    run = CheckRun.objects.create(monitor=monitor, url_snapshot="http://127.0.0.1/secret")
    publish_check(str(run.pk))
    run = completed(run)
    assert run.status == "failed" and run.error_code == "unsafe_address"
    assert not Observation.objects.filter(check_run=run).exists()


def test_running_attempt_without_worker_lock_can_be_recovered(user, token, broker):
    monitor = monitor_for(user, token)
    run = request_check(monitor.pk, user=user, publish=lambda _: None).run
    _claim_run(run.pk, monitor.pk)
    repaired = request_check(monitor.pk, user=user, publish=publish_check)
    assert not repaired.created and repaired.run.pk == run.pk
    assert completed(run).status == "succeeded"
    assert Observation.objects.filter(check_run=run).count() == 1
    assert int(broker.get(counter_key(token, "requests"))) == 1


def test_real_unavailable_broker_fails_quickly_without_touching_existing_data(user, token, monkeypatch):
    from kombu import Connection
    from apps.monitoring import tasks
    monitor = monitor_for(user, token)
    # Deliberately closed port inside this container; the real Redis stays running.
    monkeypatch.setattr(tasks.app, "connection_for_write", lambda **_: Connection(
        "redis://127.0.0.1:1/0", connect_timeout=0.2,
        transport_options={"socket_connect_timeout": 0.2, "socket_timeout": 0.2, "max_retries": 0, "retry_on_timeout": False},
    ))
    started = time.monotonic()
    with pytest.raises(CheckQueueError) as error:
        request_check(monitor.pk, user=user, publish=publish_check)
    assert time.monotonic() - started < 2
    assert error.value.run.status == "failed" and error.value.run.error_code == "queue_unavailable"
    assert not Observation.objects.filter(check_run=error.value.run).exists()
