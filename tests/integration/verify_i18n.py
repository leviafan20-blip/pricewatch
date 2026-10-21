"""Opt-in, read-only checks of existing demo pages; never seeds or flushes data.

Run with: docker compose exec -T integration-web python -m tests.integration.verify_i18n
Unlike the transactional integration pytest suite, this preserves the browser demo.
"""

import hashlib
import json
import os
import re

if os.environ.get("PRICEWATCH_INTEGRATION") != "1":
    raise RuntimeError("Read-only verification requires PRICEWATCH_INTEGRATION=1")
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.integration"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from apps.core.middleware import LocaleMiddleware  # noqa: E402
from apps.core.views import dashboard  # noqa: E402
from apps.monitoring import views  # noqa: E402
from apps.monitoring.models import CheckRun, Monitor, Observation  # noqa: E402
from apps.notifications import views as notification_views  # noqa: E402
from apps.notifications.models import Notification  # noqa: E402


def snapshot():
    """Hash bounded batches without exposing user fields or loading all rows."""
    result = {}
    for model in (get_user_model(), Monitor, CheckRun, Observation, Notification):
        digest = hashlib.sha256()
        count = 0
        for row in model.objects.order_by("pk").values().iterator(chunk_size=500):
            digest.update(json.dumps(row, default=str, sort_keys=True).encode())
            count += 1
        result[model._meta.label] = {"count": count, "sha256": digest.hexdigest()}
    return result


def main():
    if connection.settings_dict["NAME"] != "test_pricewatch":
        raise RuntimeError("Read-only verification requires the existing test_pricewatch demo")
    checks = 0
    factory = RequestFactory()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        before = snapshot()
        users = list(get_user_model().objects.filter(
            username__in=["stage5_chart_demo", "stage6_demo", "stage7_export_demo"],
        ))
        if not users:
            raise RuntimeError("No existing demo accounts; this verifier never creates data")
        for user in users:
            monitors = list(Monitor.objects.filter(owner=user).order_by("pk"))
            for language in ("en", "ru"):
                def check(view, path, *args, query=None):
                    nonlocal checks
                    request = factory.get(path, query or {})
                    # Exercise owner-scoped views without login writes or sessions.
                    request.user = user
                    request.session = {}
                    request.COOKIES[settings.LANGUAGE_COOKIE_NAME] = language
                    response = LocaleMiddleware(lambda req: view(req, *args))(request)
                    if response.status_code != 200 or f'<html lang="{language}">' not in response.content.decode():
                        raise AssertionError(f"Page verification failed: {path}, {language}, {response.status_code}")
                    checks += 1
                    return response

                home = check(dashboard, "/").content.decode()
                if monitors:
                    expected = "Your monitors and price history" if language == "en" else "Ваши мониторинги и история цен"
                    if expected not in home:
                        raise AssertionError("Existing demo monitors must not show first-use onboarding")
                check(views.monitor_list, "/monitors/")
                check(views.monitor_create, "/monitors/new/")
                check(views.export_history, "/monitors/history/export/", query={"period": "all"})
                inbox = check(notification_views.notification_list, "/notifications/").content.decode()
                if "sent" in Notification.objects.filter(owner=user).values_list("email_status", flat=True)[:20]:
                    expected = ("Passed to the mail system (delivery not confirmed)" if language == "en"
                                else "Передано почтовой системе (доставка не подтверждена)")
                    if expected not in inbox:
                        raise AssertionError("Email status must distinguish handoff from delivery")
                check(notification_views.email_preferences, "/notifications/settings/")
                for monitor in monitors:
                    response = check(views.monitor_detail, f"/monitors/{monitor.pk}/", monitor.pk, query={"period": "all"})
                    payload = json.loads(re.search(
                        r'<script id="price-chart-data" type="application/json">(.*?)</script>', response.content.decode(), re.S,
                    )[1])
                    if any(len(series["points"]) > 400 for series in payload["series"]):
                        raise AssertionError("Chart point limit was exceeded")
                    if not monitor.archived_at:
                        check(views.monitor_edit, f"/monitors/{monitor.pk}/edit/", monitor.pk)
        after = snapshot()
        if before != after:
            raise AssertionError("Application data changed during read-only verification")
    print(json.dumps({"database": connection.settings_dict["NAME"], "page_checks": checks,
                      "accounts_checked": len(users), "unchanged": True, "application_data": after}, indent=2))


if __name__ == "__main__":
    main()
