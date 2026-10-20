"""Shared settings. Secrets are supplied by the process environment."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.core.apps.CoreConfig",
    "apps.monitoring.apps.MonitoringConfig",
    "apps.notifications.apps.NotificationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.notifications.context_processors.unread_notifications",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "pricewatch"),
        "USER": os.environ.get("POSTGRES_USER", "pricewatch"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {"connect_timeout": 5},
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Yekaterinburg"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Exact hostnames only. An empty allowlist deliberately disables remote fetching.
SCRAPING_ALLOWED_HOSTS = tuple(
    host.strip()
    for host in os.environ.get("SCRAPING_ALLOWED_HOSTS", "").split(",")
    if host.strip()
)
# PostgreSQL is the source of truth for task state/results; Redis is only a broker.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_TASK_DEFAULT_QUEUE = "pricewatch"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BROKER_POOL_LIMIT = 0
CELERY_BROKER_CONNECTION_TIMEOUT = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_connect_timeout": 1,
    "socket_timeout": 1,
    "retry_on_timeout": False,
    "max_retries": 0,
    # Must exceed the task's hard time limit. Late deliveries are idempotent.
    "visibility_timeout": 300,
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_CANCEL_LONG_RUNNING_TASKS_ON_CONNECTION_LOSS = True
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULE = {
    "dispatch-notification-emails": {
        "task": "notifications.dispatch_emails", "schedule": 60.0, "options": {"expires": 55},
    },
    "dispatch-due-monitors": {
        "task": "monitoring.dispatch_due_checks",
        "schedule": 60.0,
        # Old wake-ups are discarded rather than replayed after an outage.
        "options": {"expires": 55},
    },
}
SESSION_COOKIE_HTTPONLY = True
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = "PriceWatch <noreply@pricewatch.local>"
PRICEWATCH_BASE_URL = os.environ.get("PRICEWATCH_BASE_URL", "http://127.0.0.1:8000")
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"}
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "httpcore": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False}
    },
}
