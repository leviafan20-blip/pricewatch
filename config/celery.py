"""Celery entry point shared by the web producer and background worker."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("pricewatch")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
