"""Create controlled analytics fixtures in the isolated Docker test DB only.

Run after the test suites, before starting integration-web. No fetching, queued
jobs, scheduler changes, deletion, or updates to existing users are performed.
"""

from datetime import timedelta
import json
import os

# Compose's web service normally exports local settings. This utility always
# selects the isolated profile, including its test-only password hasher.
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.integration"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.monitoring.models import CheckRun, Monitor  # noqa: E402
from tests.monitoring.test_analytics import add_observations  # noqa: E402


def main():
    if os.environ.get("PRICEWATCH_INTEGRATION") != "1" or connection.settings_dict["NAME"] != "test_pricewatch":
        raise RuntimeError("Analytics demo requires the isolated test_pricewatch DB")
    username = "stage5_chart_demo"
    if get_user_model().objects.filter(username=username).exists():
        raise RuntimeError("Demo user already exists; refusing to modify existing data")
    now = timezone.now()
    with transaction.atomic():
        user = get_user_model().objects.create_user(username=username, password="PriceWatch-demo-5!")
        definitions = [
            ("Кофемашина · Demo One", [str(12990 - (i // 8) * 300 + (500 if i % 13 == 0 else 0)) for i in range(59)] + ["10990"], "RUB"),
            ("Кофемашина · Demo Two", [str(13990 - (i // 10) * 350) if i != 25 else None for i in range(59)] + ["11990"], "RUB"),
            ("Фильтр · без изменений", ["1250"] * 5, "RUB"),
            ("Весы · первое наблюдение", ["2490"], "RUB"),
            ("Товар · ожидает проверки", [], "RUB"),
            ("Другая валюта · USD", ["99", "89"], "USD"),
            ("Длинная история · 10 003 наблюдения", [str(100 + i % 70) for i in range(10003)], "RUB"),
        ]
        monitors = []
        for index, (name, values, currency) in enumerate(definitions):
            monitor = Monitor.objects.create(owner=user, name=name, url=f"https://shop.pricewatch.example/analytics/{index}")
            step = timedelta(hours=12 if index < 2 else 1)
            start = now - step * max(len(values), 1)
            add_observations(monitor, values, start=start, step=step, currency=currency)
            monitors.append(monitor)
        CheckRun.objects.create(monitor=monitors[0], status="failed", queued_at=now, finished_at=now,
                                url_snapshot=monitors[0].url, error_code="timeout", error_message="Контролируемая ошибка: сайт не ответил. Предыдущие цены сохранены.")
    path = settings.BASE_DIR / "artifacts" / "stage5-demo.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"user": username, "monitors": [{"id": item.pk, "name": item.name} for item in monitors]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
