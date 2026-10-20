"""Local development only. Docker Compose supplies the .env values."""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ALLOWED_HOSTS or ["localhost", "127.0.0.1", "[::1]"]  # noqa: F405

if not SECRET_KEY:  # noqa: F405
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY; see .env.example and README.md.")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")  # noqa: F405
    if origin.strip()
]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
