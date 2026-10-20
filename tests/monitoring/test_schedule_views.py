"""Schedule forms, owner access and local display of UTC timestamps."""

from datetime import UTC, datetime

from django.test import Client
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.monitoring.forms import ScheduleForm
from apps.monitoring.models import Monitor

pytestmark = pytest.mark.django_db


def test_schedule_toggle_interval_and_pause(client, user, monitor):
    client.force_login(user)
    url = reverse("monitoring:schedule", args=[monitor.pk])
    assert client.get(url).status_code == 405
    assert client.post(url, {"auto_enabled": "on", "check_interval_hours": "3"}).status_code == 302
    monitor.refresh_from_db()
    assert monitor.auto_enabled and monitor.check_interval_hours == 3 and monitor.next_check_at is not None
    assert client.post(url, {"check_interval_hours": "12"}).status_code == 302
    monitor.refresh_from_db()
    assert not monitor.auto_enabled and monitor.next_check_at is None and monitor.check_interval_hours == 12
    assert client.post(url, {"auto_enabled": "on", "check_interval_hours": "24"}).status_code == 302
    monitor.refresh_from_db()
    assert monitor.auto_enabled and monitor.check_interval_hours == 24


@pytest.mark.parametrize("hours", ["2", "0", "bad", "", "25"])
def test_invalid_interval_returns_form_errors_without_change(client, user, monitor, hours):
    client.force_login(user)
    response = client.post(reverse("monitoring:schedule", args=[monitor.pk]), {"auto_enabled": "on", "check_interval_hours": hours})
    assert response.status_code == 400 and response.context["schedule_form"].errors
    monitor.refresh_from_db()
    assert not monitor.auto_enabled


def test_schedule_post_owner_and_csrf(client, user, monitor, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    url = reverse("monitoring:schedule", args=[monitor.pk])
    client.force_login(other)
    assert client.post(url, {"auto_enabled": "on", "check_interval_hours": "1"}).status_code == 404
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(user)
    assert protected.post(url, {"auto_enabled": "on", "check_interval_hours": "1"}).status_code == 403
    monitor.refresh_from_db()
    assert not monitor.auto_enabled


def test_archived_monitor_cannot_enable_schedule(client, user, monitor):
    from apps.monitoring.checks import archive_monitor
    archive_monitor(monitor.pk, user=user)
    client.force_login(user)
    assert client.post(reverse("monitoring:schedule", args=[monitor.pk]), {"auto_enabled": "on", "check_interval_hours": "1"}).status_code == 404
    content = client.get(reverse("monitoring:detail", args=[monitor.pk])).content.decode()
    assert "Следующая плановая проверка" in content and "Не запланирована" in content
    assert "Сохранить расписание" not in content


def test_readonly_schedule_fields_cannot_be_forged(client, user, monitor):
    client.force_login(user)
    before = timezone.now()
    client.post(reverse("monitoring:schedule", args=[monitor.pk]), {
        "auto_enabled": "on", "check_interval_hours": "1", "owner": "999999",
        "next_check_at": "2000-01-01", "last_scheduled_at": "2000-01-01",
    })
    monitor.refresh_from_db()
    assert monitor.owner == user and monitor.next_check_at >= before
    assert monitor.last_scheduled_at is None


def test_schedule_times_are_rendered_in_user_timezone(client, user, monitor):
    utc_time = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    Monitor.objects.filter(pk=monitor.pk).update(auto_enabled=True, next_check_at=utc_time, last_scheduled_at=utc_time)
    client.force_login(user)
    with timezone.override("Asia/Yekaterinburg"):
        content = client.get(reverse("monitoring:detail", args=[monitor.pk])).content.decode()
    assert "20.10.2026 15:00:00" in content
    assert "Asia/Yekaterinburg" in content
    assert "20.10.2026 10:00:00" not in content


def test_form_offers_only_supported_intervals(monitor):
    form = ScheduleForm(monitor=monitor)
    assert [value for value, _ in form.fields["check_interval_hours"].choices] == [1, 3, 6, 12, 24]
