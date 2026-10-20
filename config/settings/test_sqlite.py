"""Explicit offline fallback. PostgreSQL remains mandatory in CI and Compose."""

from .test import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
