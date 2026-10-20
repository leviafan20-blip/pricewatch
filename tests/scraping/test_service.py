"""The public inspection service composes loading and extraction without persistence."""

import logging
import socket
from decimal import Decimal

import pytest

from apps.monitoring import services
from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.fetcher import FetchedPage
from apps.monitoring.scraping.types import ExtractionConfig


def install_loader(monkeypatch, *, html="", failure=None):
    state = {"closed": False}

    class LocalLoader:
        def __init__(self, policy):
            state["policy"] = policy

        def __enter__(self):
            return self

        def __exit__(self, *args):
            state["closed"] = True

        def fetch(self, url):
            state["url"] = url
            if failure is not None:
                raise failure
            return FetchedPage(url, html, 200, len(html.encode("utf-8")))

    monkeypatch.setattr(services, "PageLoader", LocalLoader)
    return state


def test_inspection_uses_configured_hosts_and_extracts_loaded_html(settings, monkeypatch):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example", "other.example")
    state = install_loader(
        monkeypatch,
        html='<h1>Демо чайник</h1><span class="price">1290,50 ₽</span>'
        '<span class="stock">В наличии</span>',
    )
    result = services.inspect_url(
        "https://shop.example/product",
        ExtractionConfig(
            title_selector="h1", price_selector=".price", availability_selector=".stock"
        ),
    )
    assert state["policy"].allowed_hosts == ("shop.example", "other.example")
    assert state["url"] == "https://shop.example/product"
    assert state["closed"] is True
    assert result.title == "Демо чайник"
    assert result.price == Decimal("1290.50")
    assert result.currency == "RUB"
    assert result.availability == "in_stock"


def test_empty_allowed_hosts_deny_before_dns_or_http(settings, monkeypatch):
    settings.SCRAPING_ALLOWED_HOSTS = ()

    def unexpected_dns(*args, **kwargs):
        raise AssertionError("An unconfigured domain must not trigger DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(ScrapingError) as caught:
        services.inspect_url("https://shop.example/product")
    assert caught.value.code == "host_not_allowed"


@pytest.mark.parametrize(
    "code", ["timeout", "network_error", "robots_unavailable", "unsafe_address", "dns_error"]
)
def test_expected_load_failure_preserves_code_closes_client_and_logs_no_url(
    settings, monkeypatch, caplog, code
):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example",)
    error = ScrapingError(code, "Не удалось безопасно получить страницу.")
    state = install_loader(monkeypatch, failure=error)
    with caplog.at_level(logging.WARNING, logger="apps.monitoring.services"):
        with pytest.raises(ScrapingError) as caught:
            services.inspect_url("https://shop.example/product?token=private-service-secret")
    assert caught.value is error
    assert state["closed"] is True
    assert [record.getMessage() for record in caplog.records] == [
        f"Product inspection failed: {code}"
    ]
    assert "private-service-secret" not in caplog.text
    assert "shop.example" not in caplog.text


def test_parser_failure_is_reported_after_client_is_closed(settings, monkeypatch, caplog):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example",)
    state = install_loader(monkeypatch, html="<h1>Товар без цены</h1>")
    with caplog.at_level(logging.WARNING, logger="apps.monitoring.services"):
        with pytest.raises(ScrapingError) as caught:
            services.inspect_url("https://shop.example/product?token=private-service-secret")
    assert caught.value.code == "missing_price"
    assert state["closed"] is True
    assert "missing_price" in caplog.text
    assert "private-service-secret" not in caplog.text


def test_unexpected_failure_is_not_disguised_as_missing_product(settings, monkeypatch):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example",)
    state = install_loader(monkeypatch, failure=RuntimeError("programming failure"))
    with pytest.raises(RuntimeError, match="programming failure"):
        services.inspect_url("https://shop.example/product")
    assert state["closed"] is True
