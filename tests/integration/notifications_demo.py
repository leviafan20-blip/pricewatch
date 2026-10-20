"""Controlled browser fixtures, restricted to the isolated test DB and queue."""

from datetime import timedelta
from decimal import Decimal
import os
import uuid

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.integration"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.monitoring.checks import create_monitor  # noqa: E402
from apps.monitoring.models import CheckRun, Monitor, Observation  # noqa: E402
from apps.notifications.models import Notification  # noqa: E402
from apps.notifications.services import create_price_notification  # noqa: E402


def main():
    if (os.environ.get("PRICEWATCH_INTEGRATION") != "1" or connection.settings_dict["NAME"] != "test_pricewatch"
            or not settings.CELERY_BROKER_URL.endswith("/15")):
        raise RuntimeError("Use only test_pricewatch and Redis integration DB 15")
    if get_user_model().objects.filter(username="stage6_demo").exists():
        raise RuntimeError("Refusing to modify existing demo data")
    now = timezone.now()
    with transaction.atomic():
        user = get_user_model().objects.create_user(username="stage6_demo", password="PriceWatch-demo-6!", email="stage6@example.test", email_notifications_enabled=True)
        definitions = [
            ("Настольная лампа · Demo", "any", ["1299.95", "1099.95", "1199.95", "1199.95", "999.95"]),
            ("Кофемашина · Demo", "threshold", ["1299.95", "999.95", "899.95", "1299.95", "999.95"]),
        ]
        for name, mode, prices in definitions:
            options = {"notification_mode": mode}
            if mode == "threshold":
                options.update(notification_threshold=Decimal("1000"), notification_currency="USD")
            monitor = create_monitor(user=user, name=name, url=f"http://shop.pricewatch.example/products/{uuid.uuid4().hex}/success", notification_settings=options)
            monitor = Monitor.objects.select_related("owner").select_for_update(of=("self",)).get(pk=monitor.pk)
            for index, price in enumerate(prices):
                observed_at = now - timedelta(days=len(prices) - index)
                run = CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url, status="succeeded", queued_at=observed_at, started_at=observed_at, finished_at=observed_at)
                observation = Observation.objects.create(check_run=run, title=name, price=Decimal(price), currency="USD", availability="in_stock", source="json_ld", observed_at=observed_at)
                notice = create_price_notification(monitor, observation)
                if notice:
                    Notification.objects.filter(pk=notice.pk).update(created_at=observed_at)
    print(f"Created stage6_demo, monitors=2, observations=10, notifications={Notification.objects.filter(owner=user).count()}")


if __name__ == "__main__":
    main()
