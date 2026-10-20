"""Monitoring fixtures have no live HTTP or broker dependency."""

from decimal import Decimal

import pytest

from apps.monitoring.checks import create_monitor
from apps.monitoring.scraping.types import ProductData


@pytest.fixture(autouse=True)
def monitor_hosts(settings):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example",)


@pytest.fixture
def monitor(user):
    return create_monitor(user=user, name="Учебный товар", url="https://shop.example/item")


@pytest.fixture
def product():
    return ProductData("Лампа", Decimal("1299.95"), "USD", "in_stock", "json_ld")
