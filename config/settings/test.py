"""Tests use PostgreSQL by default, matching the application."""

from .base import *  # noqa: F403

# Ordinary pytest must never reuse the integration/browser-demo database.
DATABASES["default"]["TEST"] = {"NAME": "test_pricewatch_pytest"}  # noqa: F405

SECRET_KEY = "pricewatch-tests-only-not-a-runtime-secret"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
