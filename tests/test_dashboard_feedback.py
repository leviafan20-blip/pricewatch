"""Contextual onboarding and accurate email wording in both interface languages."""

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.monitoring.models import Monitor
from apps.notifications.services import create_price_notification
from tests.monitoring.test_analytics import add_observations

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("state", ["empty", "active", "archived", "other_owner"])
def test_dashboard_only_shows_onboarding_without_own_monitors(client, user, django_user_model, language, state):
    if state != "empty":
        owner = django_user_model.objects.create_user(username="other-dashboard-owner") if state == "other_owner" else user
        Monitor.objects.create(owner=owner, name="Existing product", url="https://shop.example/item",
                               archived_at=timezone.now() if state == "archived" else None)
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    before = list(Monitor.objects.order_by("pk").values())

    response = client.get(reverse("core:dashboard"))

    assert response.status_code == 200
    expected_existing = state in {"active", "archived"}
    assert response.context["has_monitors"] is expected_existing
    content = response.content.decode()
    first = "Start with your first observation" if language == "en" else "Всё начинается с первого наблюдения"
    existing = "Your monitors and price history" if language == "en" else "Ваши мониторинги и история цен"
    assert (existing if expected_existing else first) in content
    assert (first if expected_existing else existing) not in content
    primary = reverse("monitoring:list" if expected_existing else "monitoring:create")
    assert f'class="btn btn-primary" href="{primary}"' in content
    assert list(Monitor.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("sent", [True, False])
def test_notification_email_wording_does_not_claim_delivery(client, user, language, sent):
    monitor = Monitor.objects.create(owner=user, name="Product", url="https://shop.example/item", notification_mode="any")
    observations = add_observations(monitor, ["100", "90"], currency="USD")
    notice = create_price_notification(monitor, observations[-1])
    if sent:
        now = timezone.now()
        notice.email_status = "sent"
        notice.email_started_at = now
        notice.email_sent_at = now
        notice.save(update_fields=["email_status", "email_started_at", "email_sent_at"])
    client.force_login(user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    before = type(notice).objects.values().get(pk=notice.pk)

    response = client.get(reverse("notifications:list"))

    assert response.status_code == 200
    content = response.content.decode()
    new_text = ("Passed to the mail system (delivery not confirmed)" if language == "en"
                else "Передано почтовой системе (доставка не подтверждена)")
    if sent:
        assert new_text in content
        assert "Accepted by the email backend" not in content
        assert "Передано почтовому backend" not in content
        assert ("Email: Delivered" if language == "en" else "Email: Доставлено") not in content
    else:
        assert new_text not in content
        assert ("Not requested" if language == "en" else "Не запрошено") in content
    assert type(notice).objects.values().get(pk=notice.pk) == before
