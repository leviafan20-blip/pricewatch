import pytest

from apps.monitoring.checks import create_monitor


@pytest.fixture(autouse=True)
def allowed_sites(settings):
    settings.SCRAPING_ALLOWED_HOSTS = ("shop.example",)


@pytest.fixture
def monitor(user):
    return create_monitor(user=user, name="Лампа", url="https://shop.example/item", notification_settings={"notification_mode": "any"})
