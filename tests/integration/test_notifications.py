"""Real Beat -> Redis -> worker -> Django file email backend, no real SMTP."""

from decimal import Decimal
from email.parser import BytesParser
import os
import time
import uuid

from django.conf import settings
from django.db import connection
from django.utils import timezone
import pytest
import redis

from apps.monitoring import checks
from apps.monitoring.models import Observation
from apps.monitoring.tasks import publish_check
from apps.notifications.models import Notification
from apps.notifications.tasks import publish_email
from tests.integration.http_server import counter_key
from tests.integration.test_worker import completed, fixture_url, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True),
              pytest.mark.skipif(os.environ.get("PRICEWATCH_RUN_INTEGRATION") != "1", reason="requires real Docker integration services")]


@pytest.fixture(autouse=True)
def environment():
    assert settings.SETTINGS_MODULE == "config.settings.integration"
    assert connection.settings_dict["NAME"] == "test_pricewatch"
    yield
    Notification.objects.filter(email_status="pending").update(email_status="cancelled", next_email_at=None)
    wait_for(lambda: not Notification.objects.filter(email_status="sending").exists())


def change_price(monitor, user, broker, token, price=None):
    if price is not None:
        broker.set(counter_key(token, "price"), price, ex=600)
    run = completed(checks.request_check(monitor.pk, user=user, publish=publish_check).run)
    assert run.status == "succeeded", run.error_code
    return run


def messages_for(notice):
    expected = f"<pricewatch-notification-{notice.pk}-{notice.observation.check_run_id}@pricewatch.local>"
    # Explicit test directory; UUID in Message-ID isolates reused test DB IDs.
    found = []
    for path in settings.EMAIL_FILE_PATH.glob("*.log"):
        message = BytesParser().parsebytes(path.read_bytes())
        if "".join(str(message.get("Message-ID", "")).split()) == expected:
            found.append(message)
    return found


def test_actual_worker_beat_email_and_duplicate_tasks(user):
    token = uuid.uuid4().hex
    with redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True) as broker:
        user.email_notifications_enabled = True
        user.save()
        monitor = checks.create_monitor(user=user, name="Email lamp", url=fixture_url(token), notification_settings={"notification_mode": "decrease"})
        change_price(monitor, user, broker, token)
        assert not Notification.objects.exists()
        run = change_price(monitor, user, broker, token, "1099.95")
        notice = wait_for(lambda: Notification.objects.filter(observation__check_run=run, email_status="sent").select_related("observation").first())
        messages = messages_for(notice)
        assert len(messages) == 1
        body = messages[0].get_payload(decode=True).decode(messages[0].get_content_charset() or "utf-8")
        assert "1\N{NO-BREAK SPACE}299,95 USD" in body and "1\N{NO-BREAK SPACE}099,95 USD" in body
        publish_email(notice.pk)
        publish_email(notice.pk)
        publish_check(str(run.pk))
        time.sleep(1)
        assert len(messages_for(notice)) == 1
        assert Notification.objects.count() == 1 and Observation.objects.filter(check_run__monitor=monitor).count() == 2
        assert int(broker.get(counter_key(token, "requests"))) == 2


def test_real_sender_failure_does_not_change_successful_check(user):
    token = uuid.uuid4().hex
    user.email_notifications_enabled = True
    user.email = "invalid-recipient"
    user.save()
    with redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True) as broker:
        monitor = checks.create_monitor(user=user, name="Failure lamp", url=fixture_url(token), notification_settings={"notification_mode": "any"})
        change_price(monitor, user, broker, token)
        run = change_price(monitor, user, broker, token, "999.95")
        notice = wait_for(lambda: Notification.objects.filter(observation__check_run=run, email_status="failed").select_related("observation").first())
        run.refresh_from_db()
        assert run.status == "succeeded" and notice.observation.price == Decimal("999.95")
        assert notice.email_error_code == "email_send_failed" and not messages_for(notice)


def test_real_threshold_crosses_twice_and_not_each_below_threshold(user):
    token = uuid.uuid4().hex
    with redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True) as broker:
        monitor = checks.create_monitor(user=user, name="Threshold lamp", url=fixture_url(token), notification_settings={
            "notification_mode": "threshold", "notification_threshold": Decimal("1000"), "notification_currency": "USD",
        })
        for value, expected in [("1299.95", 0), ("999.95", 1), ("899.95", 1), ("1299.95", 1), ("999.95", 2)]:
            change_price(monitor, user, broker, token, value)
            assert Notification.objects.count() == expected
        assert not Notification.objects.exclude(email_status="off").exists()
