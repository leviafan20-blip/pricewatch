"""Real PostgreSQL lifecycle, Decimal rules, idempotence, and isolated email I/O."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import threading
from unittest.mock import Mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.monitoring import checks
from apps.monitoring.models import Monitor, Observation
from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.types import ProductData
from apps.notifications import services, tasks
from apps.notifications.models import Notification
from tests.monitoring.test_checks import in_thread

pytestmark = pytest.mark.django_db(transaction=True)


def check(monitor, user, monkeypatch, value, currency="RUB"):
    product = ProductData("Лампа", Decimal(value) if value is not None else None,
                          currency if value is not None else None, "in_stock" if value is not None else "out_of_stock", "json_ld")
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    return checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)


def email_notice(monitor, user, monkeypatch):
    user.email_notifications_enabled = True
    user.save(update_fields=["email_notifications_enabled"])
    check(monitor, user, monkeypatch, "100")
    check(monitor, user, monkeypatch, "80")
    return Notification.objects.get()


@pytest.mark.parametrize("mode,prices,expected", [
    ("off", ["100", "80"], 0), ("any", ["100"], 0), ("any", ["100", "100"], 0),
    ("any", ["100", "120", "120", "90"], 2), ("decrease", ["100", "110", "90", "90"], 1),
    ("threshold", ["120", "100", "99", "90", "95", "110", "98"], 2),
    ("threshold", ["80", "70", "60"], 0), ("threshold", ["120", "100"], 0),
])
def test_rules_first_price_and_threshold_crossing(monitor, user, monkeypatch, mode, prices, expected):
    settings = {"notification_mode": mode}
    if mode == "threshold":
        settings.update(notification_threshold=Decimal("100"), notification_currency="RUB")
    monitor = checks.update_monitor(monitor.pk, user=user, name=monitor.name, url=monitor.url, notification_settings=settings)
    for price in prices:
        check(monitor, user, monkeypatch, price)
    assert Notification.objects.count() == expected
    assert Observation.objects.count() == len(prices)


def test_decimal_change_and_zero_base(monitor, user, monkeypatch):
    check(monitor, user, monkeypatch, "99999999999999.0001")
    run = check(monitor, user, monkeypatch, "99999999999999.0002")
    notice = Notification.objects.select_related("previous_observation", "observation").get()
    assert notice.change == Decimal("0.0001") and isinstance(notice.percent, Decimal)
    # Re-running the same task cannot duplicate the observation or event.
    checks.execute_check(run.pk)
    assert Notification.objects.count() == 1 and Observation.objects.count() == 2
    check(monitor, user, monkeypatch, "0")
    check(monitor, user, monkeypatch, "2")
    notice = Notification.objects.latest("created_at")
    assert notice.change == Decimal("2") and notice.percent is None


def test_failures_and_unpriced_observations_do_not_notify(monitor, user, monkeypatch):
    check(monitor, user, monkeypatch, "100")
    check(monitor, user, monkeypatch, None)
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=ScrapingError("timeout", "Controlled timeout")))
    checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    assert not Notification.objects.exists()
    check(monitor, user, monkeypatch, "80")
    notice = Notification.objects.get()
    assert notice.previous_observation.price == Decimal("100") and notice.observation.price == Decimal("80")


def test_currency_switch_is_new_baseline(monitor, user, monkeypatch):
    check(monitor, user, monkeypatch, "100", "RUB")
    check(monitor, user, monkeypatch, "2", "USD")
    assert not Notification.objects.exists()
    check(monitor, user, monkeypatch, "1", "USD")
    assert Notification.objects.get().previous_observation.currency == "USD"
    check(monitor, user, monkeypatch, "90", "RUB")
    assert Notification.objects.count() == 1


def test_wrong_threshold_currency_does_not_notify(monitor, user, monkeypatch):
    checks.update_monitor(monitor.pk, user=user, name=monitor.name, url=monitor.url, notification_settings={
        "notification_mode": "threshold", "notification_threshold": Decimal("100"), "notification_currency": "USD",
    })
    check(monitor, user, monkeypatch, "120")
    check(monitor, user, monkeypatch, "80")
    assert not Notification.objects.exists()


def test_url_switch_and_obsolete_inflight_result_do_not_notify(monitor, user, monkeypatch):
    check(monitor, user, monkeypatch, "100")
    run = checks.request_check(monitor.pk, user=user, publish=Mock()).run
    checks.update_monitor(monitor.pk, user=user, name=monitor.name, url="https://shop.example/new")
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: ProductData("Old", Decimal("80"), "RUB", "in_stock", "json_ld"))
    checks.execute_check(run.pk)
    assert not Notification.objects.exists()
    monitor.refresh_from_db()
    check(monitor, user, monkeypatch, "60")
    assert not Notification.objects.exists()
    check(monitor, user, monkeypatch, "50")
    assert Notification.objects.count() == 1


def test_creation_is_atomic_with_observation_and_one_to_one(monitor, user, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    with pytest.raises(IntegrityError), transaction.atomic():
        Notification.objects.create(owner=user, observation=notice.observation, previous_observation=notice.previous_observation, rule="any")
    assert Notification.objects.count() == 1
    # No SMTP/Redis was contacted by execute_check; email remains in durable outbox.
    assert notice.email_status == "pending" and not getattr(mail, "outbox", [])


@pytest.mark.parametrize("values", [
    {"notification_mode": "bad"},
    {"notification_mode": "threshold"},
    {"notification_mode": "threshold", "notification_threshold": Decimal("-1"), "notification_currency": "RUB"},
    {"notification_mode": "threshold", "notification_threshold": Decimal("1"), "notification_currency": "ZZZ"},
    {"notification_mode": "threshold", "notification_threshold": Decimal("NaN"), "notification_currency": "RUB"},
    {"notification_mode": "threshold", "notification_threshold": 1.1, "notification_currency": "RUB"},
])
def test_settings_validation(values):
    with pytest.raises(ValidationError):
        checks.validate_notification_settings(**values)


def test_email_is_async_and_duplicate_delivery_sends_once(monitor, user, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    assert services.send_notification_email(notice.pk)
    assert not services.send_notification_email(notice.pk)
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [user.email]
    assert "100 RUB" in message.body and "80 RUB" in message.body and "-20 %" in message.body
    assert f"/monitors/{monitor.pk}/" in message.body
    notice.refresh_from_db()
    assert notice.email_status == "sent" and notice.email_sent_at


@pytest.mark.parametrize("failure", [OSError("smtp=password-secret"), 0])
def test_send_failure_preserves_observation_and_internal_event(monitor, user, monkeypatch, failure):
    notice = email_notice(monitor, user, monkeypatch)
    sender = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(services.EmailMessage, "send", sender)
    assert not services.send_notification_email(notice.pk)
    notice.refresh_from_db()
    assert notice.email_status == "failed" and notice.email_error_code == "email_send_failed"
    assert Observation.objects.count() == 2 and Notification.objects.count() == 1
    assert not services.send_notification_email(notice.pk) and sender.call_count == 1


@pytest.mark.parametrize("setting", ["disabled", "inactive", "no_email", "archived", "rule_off"])
def test_optout_and_archiving_cancel_queued_email(monitor, user, monkeypatch, setting):
    notice = email_notice(monitor, user, monkeypatch)
    if setting == "disabled":
        user.email_notifications_enabled = False
    elif setting == "inactive":
        user.is_active = False
    elif setting == "no_email":
        user.email = ""
    elif setting == "archived":
        checks.archive_monitor(monitor.pk, user=user)
    else:
        checks.update_monitor(monitor.pk, user=user, name=monitor.name, url=monitor.url, notification_settings={"notification_mode": "off"})
    user.save()
    assert not services.send_notification_email(notice.pk)
    notice.refresh_from_db()
    assert notice.email_status == "cancelled" and not getattr(mail, "outbox", [])


def test_email_sender_concurrency_does_not_hold_transaction(monitor, user, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    started, release = threading.Event(), threading.Event()
    def send(*_, **kwargs):
        from django.db import connection
        assert not connection.in_atomic_block
        started.set()
        assert release.wait(5)
        return 1
    sender = Mock(side_effect=send)
    monkeypatch.setattr(services.EmailMessage, "send", sender)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_thread, lambda: services.send_notification_email(notice.pk))
        assert started.wait(5)
        try:
            assert pool.submit(in_thread, lambda: services.send_notification_email(notice.pk)).result(timeout=3) is False
        finally:
            release.set()
        assert first.result(timeout=5)
    assert sender.call_count == 1


def test_publication_recovery_and_concurrent_dispatch(monitor, user, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    now = timezone.now()
    publish = Mock(side_effect=OSError("Redis down"))
    assert services.dispatch_emails(publish=publish, now=now) == 1
    assert services.dispatch_emails(publish=publish, now=now) == 0
    notice.refresh_from_db()
    assert notice.email_status == "pending"
    publish = Mock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(in_thread, lambda: services.dispatch_emails(publish=publish, now=now + timedelta(seconds=61))) for _ in range(2)]
        assert sum(item.result(timeout=5) for item in futures) == 1
    assert publish.call_count == 1


def test_crashed_sender_is_unknown_and_never_retried(monitor, user, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    now = timezone.now()
    Notification.objects.filter(pk=notice.pk).update(email_status="sending", next_email_at=None, email_started_at=now - timedelta(minutes=3))
    publish = Mock()
    services.dispatch_emails(publish=publish, now=now)
    notice.refresh_from_db()
    assert notice.email_status == "unknown" and notice.email_error_code == "sender_interrupted"
    assert not publish.called and not services.send_notification_email(notice.pk)
    assert services.send_notification_email(99999999) is False


def test_task_adapters_and_safe_local_backend(monkeypatch, settings):
    send = Mock()
    monkeypatch.setattr(tasks, "send_notification_email", send)
    tasks.send_email_task.run(5)
    send.assert_called_once_with(5)
    dispatcher = Mock()
    monkeypatch.setattr(tasks, "dispatch_emails", dispatcher)
    tasks.dispatch_emails_task.run()
    dispatcher.assert_called_once_with(publish=tasks.publish_email)
    assert tasks.send_email_task.acks_late and tasks.send_email_task.time_limit < 120


def test_email_publisher_bounds_message_lifetime(monkeypatch):
    connection = Mock()
    context = Mock(__enter__=Mock(return_value=connection), __exit__=Mock(return_value=False))
    monkeypatch.setattr(tasks.app, "connection_for_write", Mock(return_value=context))
    publish = Mock()
    monkeypatch.setattr(tasks.send_email_task, "apply_async", publish)
    tasks.publish_email(42)
    assert publish.call_args.kwargs == {
        "args": [42], "task_id": "notification-email-42", "connection": connection,
        "retry": False, "expires": 55,
    }
