"""Tests use PostgreSQL by default, matching the application."""

from .base import *  # noqa: F403

SECRET_KEY = "pricewatch-tests-only-not-a-runtime-secret"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
