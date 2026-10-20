"""Isolated Docker integration environment; never use for deployed services.

The worker's test-only network adapter lives under tests/, not application code.
The caller must explicitly select this settings module and integration mode.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .test import *  # noqa: F403

if os.environ.get("PRICEWATCH_INTEGRATION") != "1":
    raise ImproperlyConfigured("Integration settings require PRICEWATCH_INTEGRATION=1")

DATABASES["default"]["TEST"] = {  # noqa: F405
    "NAME": os.environ.get("PRICEWATCH_INTEGRATION_DB", "test_pricewatch")
}
SCRAPING_ALLOWED_HOSTS = ("shop.pricewatch.example",)
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/15")
CELERY_TASK_DEFAULT_QUEUE = "pricewatch-integration"
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_IGNORE_RESULT = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DEBUG = True
# Only the wake-up frequency changes; user intervals remain 1/3/6/12/24 hours.
CELERY_BEAT_SCHEDULE = {
    "dispatch-notification-emails": {
        "task": "notifications.dispatch_emails", "schedule": 2.0, "options": {"expires": 1.5},
    },
    "dispatch-due-monitors": {
        "task": "monitoring.dispatch_due_checks",
        "schedule": 2.0,
        "options": {"expires": 1.5},
    },
}
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = BASE_DIR / "artifacts" / "stage6-mail"  # noqa: F405
