"""Lifecycle checks use real PostgreSQL locks and controlled inspection results."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading
import uuid
from unittest.mock import Mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
import pytest

from apps.monitoring import checks
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.types import ProductData

pytestmark = pytest.mark.django_db(transaction=True)


def queued(monitor, user):
    return checks.request_check(monitor.pk, user=user, publish=Mock()).run


def in_thread(function):
    close_old_connections()
    try:
        return function()
    finally:
        connection.close()


def test_create_normalizes_without_network(user, monkeypatch):
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=AssertionError("No HTTP in web")))
    monitor = checks.create_monitor(user=user, name="  Лампа  ", url="https://SHOP.example/item", extraction_config={"price_selector": ".price", "currency_hint": "rub"})
    assert monitor.name == "Лампа"
    assert monitor.url == "https://shop.example/item"
    assert monitor.extraction_config == {"price_selector": ".price", "currency_hint": "RUB"}
    assert monitor.owner == user
    assert CheckRun.objects.count() == 0


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/", "https://other.example/", "file:///etc/passwd", "https://u:p@shop.example/"])
def test_create_rejects_unsafe_or_unapproved_url(user, url):
    with pytest.raises(ValidationError):
        checks.create_monitor(user=user, name="Товар", url=url)
    assert not Monitor.objects.exists()


@pytest.mark.parametrize("config", [{"unknown": "value"}, {"price_selector": "["}, {"price_selector": 3}, {"currency_hint": "ZZZ"}, {"price_attribute": "data price"}, {"price_attribute": "data-price"}, {"in_stock_values": [""]}])
def test_create_rejects_invalid_config(user, config):
    with pytest.raises(ValidationError):
        checks.create_monitor(user=user, name="Товар", url="https://shop.example/", extraction_config=config)


def test_publish_happens_after_commit_and_repeated_post_reuses_run(monitor, user):
    def publish(run_id):
        assert not connection.in_atomic_block
        assert CheckRun.objects.get(pk=run_id).status == "queued"
    first = checks.request_check(monitor.pk, user=user, publish=publish)
    second = checks.request_check(monitor.pk, user=user, publish=publish)
    assert first.created and not second.created
    assert first.published and not second.published
    assert first.run.delivery_attempts == 1 and first.run.execution_attempts == 0
    assert first.run.pk == second.run.pk
    assert CheckRun.objects.count() == 1


def test_broker_failure_is_durable_and_retry_creates_new_attempt(monitor, user):
    with pytest.raises(checks.CheckQueueError) as error:
        checks.request_check(monitor.pk, user=user, publish=Mock(side_effect=OSError("redis://secret")))
    run = error.value.run
    assert run.status == "failed" and run.error_code == "queue_unavailable"
    assert "secret" not in run.error_message
    second = queued(monitor, user)
    assert second.pk != run.pk
    assert Observation.objects.count() == 0


def test_publish_exception_after_worker_success_does_not_overwrite_result(monitor, user, product, monkeypatch):
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    def publish(run_id):
        checks.execute_check(run_id)
        raise OSError("lost broker acknowledgement")
    with pytest.raises(checks.CheckQueueError) as error:
        checks.request_check(monitor.pk, user=user, publish=publish)
    assert error.value.run.status == "succeeded"
    assert Observation.objects.count() == 1


def test_success_is_atomic_and_duplicate_delivery_has_no_effect(monitor, user, product, monkeypatch):
    def inspect(url, config):
        assert not connection.in_atomic_block
        assert url == monitor.url
        assert CheckRun.objects.get().status == "running"
        return product
    inspect = Mock(side_effect=inspect)
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    run = queued(monitor, user)
    assert checks.execute_check(str(run.pk)).status == "succeeded"
    assert checks.execute_check(str(run.pk)).status == "succeeded"
    assert inspect.call_count == 1
    run.refresh_from_db()
    assert run.execution_attempts == 1
    observation = Observation.objects.get()
    assert observation.price == Decimal("1299.95")
    assert observation.check_run_id == run.pk


@pytest.mark.parametrize("error,code", [(ScrapingError("missing_price", "Цена отсутствует."), "missing_price"), (ScrapingError("timeout", "Истекло время."), "timeout"), (ValueError("private query=secret"), "internal_error"), (SoftTimeLimitExceeded(), "worker_timeout")])
def test_failure_preserves_previous_observation(monitor, user, product, monkeypatch, error, code):
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    previous = checks.execute_check(queued(monitor, user).pk).observation
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=error))
    run = checks.execute_check(queued(monitor, user).pk)
    assert run.status == "failed" and run.error_code == code
    assert run.finished_at is not None and run.claim_token is None
    assert Observation.objects.count() == 1
    assert Observation.objects.get().pk == previous.pk
    assert "secret" not in run.error_message


def test_invalid_observation_rolls_back_then_records_error(monitor, user, monkeypatch):
    invalid = ProductData("Лампа", Decimal("-1"), "USD", "in_stock", "json_ld")
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: invalid)
    run = checks.execute_check(queued(monitor, user).pk)
    assert run.status == "failed" and run.error_code == "internal_error"
    assert not Observation.objects.exists()


def test_out_of_stock_without_price_is_success(monitor, user, monkeypatch):
    product = ProductData("Лампа", None, None, "out_of_stock", "json_ld", ("Нет цены",))
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    run = checks.execute_check(queued(monitor, user).pk)
    assert run.status == "succeeded"
    assert run.observation.price is None and run.observation.currency == ""


def test_long_valid_product_title_is_preserved(monitor, user, monkeypatch):
    product = ProductData("Л" * 1000, Decimal("10"), "RUB", "in_stock", "json_ld")
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    run = checks.execute_check(queued(monitor, user).pk)
    assert run.status == "succeeded" and run.observation.title == product.title


def test_edit_does_not_mutate_snapshot(monitor, user, product, monkeypatch):
    run = queued(monitor, user)
    checks.update_monitor(monitor.pk, user=user, name="Новое имя", url="https://shop.example/changed", extraction_config={"price_selector": ".new"})
    inspect = Mock(return_value=product)
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    checks.execute_check(run.pk)
    assert inspect.call_args.args[0] == "https://shop.example/item"
    assert inspect.call_args.args[1].price_selector is None
    run.refresh_from_db()
    assert run.extraction_config_snapshot == {}
    run.url_snapshot = "https://shop.example/tampered"
    with pytest.raises(ValidationError):
        run.save()


def test_archive_cancels_queued_and_prevents_new_checks(monitor, user, monkeypatch):
    run = queued(monitor, user)
    inspect = Mock(side_effect=AssertionError("Archived"))
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    checks.archive_monitor(monitor.pk, user=user)
    assert checks.execute_check(run.pk).status == "cancelled"
    with pytest.raises(Monitor.DoesNotExist):
        queued(monitor, user)
    assert inspect.call_count == 0


def test_running_check_can_finish_after_archive(monitor, user, product, monkeypatch):
    def inspect(*_):
        checks.archive_monitor(monitor.pk, user=user)
        return product
    monkeypatch.setattr(checks.services, "inspect_url", inspect)
    assert checks.execute_check(queued(monitor, user).pk).status == "succeeded"
    assert Observation.objects.count() == 1


@pytest.mark.parametrize("operation", ["update", "archive", "check"])
def test_domain_checks_owner(monitor, django_user_model, operation):
    other = django_user_model.objects.create_user(username="other")
    with pytest.raises(Monitor.DoesNotExist):
        if operation == "update":
            checks.update_monitor(monitor.pk, user=other, name="Stolen", url=monitor.url)
        elif operation == "archive":
            checks.archive_monitor(monitor.pk, user=other)
        else:
            checks.request_check(monitor.pk, user=other, publish=Mock(side_effect=AssertionError("No publish")))


def test_orphaned_running_attempt_is_reclaimed(monitor, user, product, monkeypatch):
    run = queued(monitor, user)
    checks._claim_run(run.pk, monitor.pk)
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    assert checks.execute_check(run.pk).status == "succeeded"
    assert Observation.objects.count() == 1


def test_concurrent_deliveries_inspect_once(monitor, user, product, monkeypatch):
    run = queued(monitor, user)
    started, release = threading.Event(), threading.Event()
    inspect = Mock()
    def slow_inspect(*_):
        inspect()
        started.set()
        assert release.wait(10)
        return product
    monkeypatch.setattr(checks.services, "inspect_url", slow_inspect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_thread, lambda: checks.execute_check(run.pk))
        try:
            assert started.wait(10)
            duplicate = pool.submit(in_thread, lambda: checks.execute_check(run.pk))
            assert duplicate.result(timeout=10) is None
        finally:
            release.set()
        assert first.result(timeout=10).status == "succeeded"
    assert inspect.call_count == 1
    assert Observation.objects.count() == 1


def test_concurrent_submissions_share_one_active_run(monitor, user):
    barrier = threading.Barrier(2)
    def submit():
        barrier.wait(timeout=10)
        return checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(in_thread, submit) for _ in range(2)]
        ids = [future.result(timeout=10) for future in futures]
    assert ids[0] == ids[1]
    assert CheckRun.objects.count() == 1


@pytest.mark.parametrize("value", [str(uuid.uuid4()), "invalid"])
def test_unknown_task_is_ignored(value):
    assert checks.execute_check(value) is None
