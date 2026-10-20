from decimal import Decimal

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
import pytest

from apps.monitoring.models import Monitor
from apps.notifications.models import Notification
from tests.notifications.test_notifications import check, email_notice

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner_client(client, user):
    client.force_login(user)
    return client


def test_monitor_form_saves_notification_settings(owner_client, user):
    response = owner_client.post(reverse("monitoring:create"), {
        "name": "Lamp", "url": "https://shop.example/item", "notification_mode": "threshold",
        "notification_threshold": "99.1234", "notification_currency": "RUB",
    })
    assert response.status_code == 302
    monitor = Monitor.objects.get()
    assert monitor.notification_threshold == Decimal("99.1234")
    page = owner_client.get(reverse("monitoring:edit", args=[monitor.pk]))
    assert page.context["form"].initial["notification_mode"] == "threshold"
    response = owner_client.post(reverse("monitoring:edit", args=[monitor.pk]), {
        "name": "Lamp", "url": monitor.url, "notification_mode": "off",
    })
    assert response.status_code == 302
    monitor.refresh_from_db()
    assert monitor.notification_threshold is None and monitor.notification_currency == ""


@pytest.mark.parametrize("data", [{"notification_mode": "bad"}, {"notification_mode": "threshold"},
                                   {"notification_mode": "threshold", "notification_threshold": "-1", "notification_currency": "RUB"}])
def test_invalid_rule_does_not_save(owner_client, data):
    response = owner_client.post(reverse("monitoring:create"), {"name": "Lamp", "url": "https://shop.example/item", **data})
    assert response.context["form"].errors and not Monitor.objects.exists()


def test_inbox_counter_read_filter_and_owner_isolation(owner_client, client, user, monitor, monkeypatch, django_user_model):
    notice = email_notice(monitor, user, monkeypatch)
    page = owner_client.get(reverse("notifications:list"))
    assert page.context["unread_notification_count"] == 1
    assert "100 →" in page.content.decode() and "80 RUB" in page.content.decode()
    notice.refresh_from_db()
    assert notice.read_at is None  # GET never marks read.
    other = django_user_model.objects.create_user(username="other-notice")
    client.force_login(other)
    assert client.get(reverse("notifications:list")).context["unread_notification_count"] == 0
    assert client.post(reverse("notifications:read", args=[notice.pk])).status_code == 404
    client.post(reverse("notifications:read_all"))
    notice.refresh_from_db()
    assert notice.read_at is None
    client.force_login(user)
    assert client.get(reverse("notifications:read", args=[notice.pk])).status_code == 405
    client.post(reverse("notifications:read", args=[notice.pk]))
    notice.refresh_from_db()
    read_at = notice.read_at
    client.post(reverse("notifications:read", args=[notice.pk]))
    notice.refresh_from_db()
    assert notice.read_at == read_at
    assert not client.get(reverse("notifications:list") + "?state=unread").context["page_obj"]


def test_email_preferences_validate_address_and_cancel_pending(owner_client, user, monitor, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    url = reverse("notifications:settings")
    response = owner_client.post(url, {"email_notifications_enabled": "on", "email": "bad"})
    assert response.context["form"].errors
    response = owner_client.post(url, {"email": user.email})
    assert response.status_code == 302
    notice.refresh_from_db()
    user.refresh_from_db()
    assert not user.email_notifications_enabled and notice.email_status == "cancelled"
    owner_client.post(url, {"email_notifications_enabled": "on", "email": "new@example.test", "is_staff": "1"})
    user.refresh_from_db()
    assert user.email_notifications_enabled and user.email == "new@example.test" and not user.is_staff
    notice.refresh_from_db()
    assert notice.email_status == "cancelled"


@pytest.mark.parametrize("route", ["list", "settings", "read_all"])
def test_anonymous_pages_require_login(client, route):
    response = client.get(reverse("notifications:" + route))
    assert response.status_code == 302


def test_csrf_required_for_read_and_settings(user, monitor, monkeypatch):
    notice = email_notice(monitor, user, monkeypatch)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    for url in [reverse("notifications:read", args=[notice.pk]), reverse("notifications:read_all"), reverse("notifications:settings")]:
        assert client.post(url, {}).status_code == 403


def test_pagination_no_n_plus_one_and_escaped_title(owner_client, user, monitor, monkeypatch):
    monitor.name = "<script>alert(1)</script>"
    monitor.save()
    for i in range(23):
        check(monitor, user, monkeypatch, str(100 - i))
    with CaptureQueriesContext(connection) as queries:
        page = owner_client.get(reverse("notifications:list"))
    assert len(page.context["page_obj"]) == 20 and len(queries) <= 6
    assert monitor.name not in page.content.decode()
    response = owner_client.get(reverse("notifications:list") + "?page=2&state=unread")
    assert len(response.context["page_obj"]) == 2
    owner_client.post(reverse("notifications:read_all"))
    assert Notification.objects.filter(read_at=None).count() == 0
