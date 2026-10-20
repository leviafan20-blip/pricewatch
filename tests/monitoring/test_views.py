"""Owner isolation, HTML flows, forms and CSRF without a broker or a store."""

from decimal import Decimal
from unittest.mock import Mock

from django.test import Client
from django.urls import reverse
from django.utils.html import escape
import pytest

from apps.monitoring import checks
from apps.monitoring.forms import MonitorForm
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.templatetags.monitoring import amount

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner_client(client, user):
    client.force_login(user)
    return client


def test_create_form_saves_only_explicit_fields(owner_client, user, django_user_model):
    other = django_user_model.objects.create_user(username="other")
    response = owner_client.post(reverse("monitoring:create"), {
        "name": " Лампа ", "url": "https://shop.example/lamp", "price_selector": ".price", "currency_hint": "RUB", "owner": other.pk,
    })
    monitor = Monitor.objects.get()
    assert response.status_code == 302 and response.url == reverse("monitoring:detail", args=[monitor.pk])
    assert monitor.owner == user and monitor.name == "Лампа"
    assert monitor.extraction_config == {"price_selector": ".price", "currency_hint": "RUB"}
    assert not CheckRun.objects.exists()


@pytest.mark.parametrize("data", [{"name": "", "url": "https://shop.example/a"}, {"name": "A", "url": "http://localhost/"}, {"name": "A", "url": "https://other.example/a"}, {"name": "A", "url": "https://shop.example/a", "price_selector": "["}, {"name": "A", "url": "https://shop.example/a", "currency_hint": "ZZZ"}])
def test_invalid_form_is_shown_without_save(owner_client, data):
    response = owner_client.post(reverse("monitoring:create"), data)
    assert response.status_code == 200 and response.context["form"].errors
    assert not Monitor.objects.exists()


def test_edit_and_clear_selectors(owner_client, monitor):
    monitor.extraction_config = {"price_selector": ".old", "in_stock_values": ["Да"]}
    monitor.save()
    response = owner_client.get(reverse("monitoring:edit", args=[monitor.pk]))
    assert response.context["form"].initial["price_selector"] == ".old"
    response = owner_client.post(reverse("monitoring:edit", args=[monitor.pk]), {"name": "Новое имя", "url": "https://shop.example/new"})
    assert response.status_code == 302
    monitor.refresh_from_db()
    assert monitor.name == "Новое имя" and monitor.url.endswith("/new")
    assert monitor.extraction_config == {"in_stock_values": ["Да"]}


def test_check_post_enqueues_and_reuses_existing(owner_client, monitor, monkeypatch):
    publish = Mock()
    monkeypatch.setattr("apps.monitoring.views.publish_check", publish)
    url = reverse("monitoring:check", args=[monitor.pk])
    assert owner_client.get(url).status_code == 405
    assert owner_client.post(url).status_code == 302
    run = CheckRun.objects.get()
    assert run.status == "queued"
    assert owner_client.post(url).status_code == 302
    assert CheckRun.objects.count() == 1
    assert publish.call_count == 2
    assert publish.call_args.args == (str(run.pk),)
    page = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]))
    assert "Повторить отправку" in page.content.decode()


def test_queue_failure_is_visible_and_safe(owner_client, monitor, monkeypatch):
    monkeypatch.setattr("apps.monitoring.views.publish_check", Mock(side_effect=OSError("secret=password")))
    response = owner_client.post(reverse("monitoring:check", args=[monitor.pk]), follow=True)
    content = response.content.decode()
    assert response.status_code == 200
    assert "queue_unavailable" in content and "secret=password" not in content
    assert CheckRun.objects.get().status == "failed"


def test_detail_preserves_price_after_failure_and_unpriced_out_of_stock(owner_client, monitor, user, product, monkeypatch):
    from apps.monitoring.scraping.errors import ScrapingError
    from apps.monitoring.scraping.types import ProductData
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    first = checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: ProductData("Лампа", None, None, "out_of_stock", "json_ld"))
    checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=ScrapingError("timeout", "Сайт не ответил.")))
    checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]))
    assert response.context["latest_price"].pk == first.observation.pk
    assert response.context["latest_observation"].availability == "out_of_stock"
    assert response.context["latest_run"].error_code == "timeout"
    assert "1\N{NO-BREAK SPACE}299,95" in response.content.decode()
    assert "Нет в наличии" in response.content.decode()


def test_archive_confirmation_get_does_not_mutate(owner_client, monitor, user):
    run = checks.request_check(monitor.pk, user=user, publish=Mock()).run
    url = reverse("monitoring:archive", args=[monitor.pk])
    assert owner_client.get(url).status_code == 200
    monitor.refresh_from_db()
    assert monitor.archived_at is None
    assert owner_client.post(url).status_code == 302
    monitor.refresh_from_db()
    run.refresh_from_db()
    assert monitor.archived_at is not None and run.status == "cancelled"
    assert owner_client.post(reverse("monitoring:check", args=[monitor.pk])).status_code == 404
    assert owner_client.get(reverse("monitoring:edit", args=[monitor.pk])).status_code == 302
    assert monitor.name not in owner_client.get(reverse("monitoring:list")).content.decode()
    assert monitor.name in owner_client.get(reverse("monitoring:list") + "?state=archived").content.decode()


def test_archived_previous_price_does_not_offer_forbidden_check(owner_client, monitor, user, product, monkeypatch):
    monkeypatch.setattr(checks.services, "inspect_url", lambda *_: product)
    checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    checks.update_monitor(monitor.pk, user=user, name=monitor.name, url="https://shop.example/changed")
    checks.archive_monitor(monitor.pk, user=user)
    content = owner_client.get(reverse("monitoring:detail", args=[monitor.pk])).content.decode()
    assert "Цена получена для предыдущего URL." in content
    assert "Запустите проверку нового адреса" not in content
    assert "Последнее известное наличие" in content and "Проверено" in content
    assert "Проверить сейчас" not in content


@pytest.mark.parametrize("route,method", [("detail", "get"), ("edit", "get"), ("edit", "post"), ("archive", "get"), ("archive", "post"), ("check", "post")])
def test_other_user_cannot_access_or_change_monitor(client, monitor, django_user_model, route, method, monkeypatch):
    other = django_user_model.objects.create_user(username="outsider")
    client.force_login(other)
    publish = Mock()
    monkeypatch.setattr("apps.monitoring.views.publish_check", publish)
    response = getattr(client, method)(reverse("monitoring:" + route, args=[monitor.pk]), {"name": "Stolen", "url": monitor.url})
    assert response.status_code == 404
    assert monitor.name not in response.content.decode()
    monitor.refresh_from_db()
    assert monitor.name == "Учебный товар" and monitor.archived_at is None
    assert not CheckRun.objects.exists() and not publish.called


def test_list_shows_only_owned_data(owner_client, monitor, django_user_model):
    other = django_user_model.objects.create_user(username="outsider")
    checks.create_monitor(user=other, name="Чужой секретный товар", url=monitor.url)
    content = owner_client.get(reverse("monitoring:list")).content.decode()
    assert monitor.name in content
    assert "Чужой секретный товар" not in content


@pytest.mark.parametrize("route", ["list", "create", "detail", "edit", "archive"])
def test_anonymous_pages_require_login(client, monitor, route):
    args = [] if route in {"list", "create"} else [monitor.pk]
    response = client.get(reverse("monitoring:" + route, args=args))
    assert response.status_code == 302 and response.url.startswith("/accounts/login/")


@pytest.mark.parametrize("route", ["create", "edit", "archive", "check"])
def test_mutations_require_csrf(user, monitor, route):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    args = [] if route == "create" else [monitor.pk]
    assert client.post(reverse("monitoring:" + route, args=args), {"name": "A", "url": monitor.url}).status_code == 403
    monitor.refresh_from_db()
    assert monitor.archived_at is None and not CheckRun.objects.exists()


def test_monitor_title_and_error_are_escaped(owner_client, monitor, user, monkeypatch):
    from apps.monitoring.scraping.errors import ScrapingError
    script = "<script>alert(1)</script>"
    monitor.name = script
    monitor.save()
    monkeypatch.setattr(checks.services, "inspect_url", Mock(side_effect=ScrapingError("missing_price", script)))
    checks.execute_check(checks.request_check(monitor.pk, user=user, publish=Mock()).run.pk)
    content = owner_client.get(reverse("monitoring:detail", args=[monitor.pk])).content.decode()
    assert script not in content and escape(script) in content


def test_list_and_history_are_paginated(owner_client, monitor, user):
    Monitor.objects.bulk_create([Monitor(owner=user, name=f"Товар {i}", url=monitor.url) for i in range(21)])
    response = owner_client.get(reverse("monitoring:list") + "?page=2")
    assert response.context["page_obj"].number == 2
    assert len(response.context["page_obj"]) == 2
    for _ in range(21):
        try:
            checks.request_check(monitor.pk, user=user, publish=Mock(side_effect=OSError()))
        except checks.CheckQueueError:
            pass
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]) + "?page=2")
    assert len(response.context["page_obj"]) == 1


def test_empty_allowlist_explained_and_form_denies_url(settings):
    settings.SCRAPING_ALLOWED_HOSTS = ()
    form = MonitorForm({"name": "A", "url": "https://shop.example/"})
    assert not form.is_valid() and "url" in form.errors


@pytest.mark.parametrize("value,expected", [(None, "—"), (Decimal("0"), "0"), (Decimal("1299.9500000000"), "1\N{NO-BREAK SPACE}299,95"), (Decimal("1.2345"), "1,2345"), (Decimal("1000000"), "1\N{NO-BREAK SPACE}000\N{NO-BREAK SPACE}000")])
def test_amount_preserves_decimal_precision(value, expected):
    assert amount(value) == expected
