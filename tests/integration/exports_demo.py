"""Controlled browser fixtures in the isolated test database, without HTTP checks."""

from datetime import timedelta
import os

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.integration"

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.monitoring.models import Monitor, Observation  # noqa: E402
from tests.monitoring.test_analytics import add_observations  # noqa: E402


def main():
    if os.environ.get("PRICEWATCH_INTEGRATION") != "1" or connection.settings_dict["NAME"] != "test_pricewatch":
        raise RuntimeError("Export demo requires the isolated test_pricewatch database")
    username = "stage7_export_demo"
    if get_user_model().objects.filter(username=username).exists():
        raise RuntimeError("Demo user exists; refusing to modify existing data")
    now = timezone.now()
    with transaction.atomic():
        user = get_user_model().objects.create_user(username=username, password="PriceWatch-demo-7!")
        monitor = Monitor.objects.create(owner=user, name="Кофемашина · Export demo", url="http://shop.pricewatch.example/export-demo")
        observations = add_observations(monitor, ["1299.95"] * 26 + ["99999999999999999999.1234567890", None], start=now - timedelta(hours=30))
        Observation.objects.filter(pk=observations[0].pk).update(title="=1+1")
        archived = Monitor.objects.create(owner=user, name="Archived USD demo", url=monitor.url, archived_at=now)
        add_observations(archived, ["25.1234567890"], currency="USD", start=now - timedelta(hours=1))
    print(f"Demo user: {username}; monitor: {monitor.pk}; archive: {archived.pk}; observations: 29")


if __name__ == "__main__":
    main()
