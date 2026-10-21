"""Manual check lifecycle. The network is always outside database transactions."""

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import timedelta
import hashlib
import logging
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
import soupsieve

from . import services
from .models import CheckRun, Monitor, Observation
from .scraping.errors import ScrapingError
from .scraping.normalization import normalize_currency
from .scraping.safety import validate_url
from .scraping.types import ExtractionConfig

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = (CheckRun.Status.QUEUED, CheckRun.Status.RUNNING)
DELIVERY_RETRY_SECONDS = 300
MAX_DELIVERY_ATTEMPTS = 3
MAX_EXECUTION_ATTEMPTS = 3
EXECUTION_LIMIT_ERROR = (
    "execution_attempts_exhausted",
    "Проверка прервана после трёх запусков. Повторите проверку позднее.",
)


@dataclass(frozen=True)
class CheckRequest:
    run: CheckRun
    created: bool
    published: bool


class CheckQueueError(Exception):
    code = "queue_unavailable"
    message = "Не удалось отправить проверку в очередь. Повторите попытку позднее."

    def __init__(self, run):
        super().__init__(self.message)
        self.run = run


def validate_monitor_input(*, name, url, extraction_config=None):
    """Validate form/service inputs without DNS or HTTP in the web process."""
    errors = {}
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        errors["name"] = "Укажите название до 200 символов."
    try:
        normalized_url = validate_url(url.strip() if isinstance(url, str) else url, settings.SCRAPING_ALLOWED_HOSTS).url
    except ScrapingError as error:
        errors["url"] = error.message
        normalized_url = None
    config = extraction_config if extraction_config is not None else {}
    normalized = {}
    if not isinstance(config, dict) or set(config) - {field.name for field in fields(ExtractionConfig)}:
        errors["extraction_config"] = "Неизвестные настройки извлечения."
    else:
        for key, value in config.items():
            if key.endswith("_values"):
                if not isinstance(value, (list, tuple)) or len(value) > 50 or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in value):
                    errors["extraction_config"] = "Значения наличия должны быть списком непустых строк."
                else:
                    normalized[key] = list(value)
                continue
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                errors["extraction_config"] = "Селекторы, атрибуты и валюта должны быть строками."
                continue
            value = value.strip()
            try:
                if key.endswith("_selector"):
                    if not value or len(value) > 500:
                        raise ValueError
                    soupsieve.compile(value)
                elif key.endswith("_attribute"):
                    if len(value) > 100 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", value):
                        raise ValueError
                elif key == "currency_hint":
                    value = normalize_currency(value)
                normalized[key] = value
            except (ValueError, soupsieve.SelectorSyntaxError, ScrapingError):
                errors["extraction_config"] = "Проверьте CSS-селекторы, атрибуты и код валюты."
        for key in normalized:
            if key.endswith("_attribute") and not normalized.get(key.replace("_attribute", "_selector")):
                errors["extraction_config"] = "Для чтения атрибута укажите CSS-селектор этого поля."
    if errors:
        raise ValidationError(errors)
    return {"name": name.strip(), "url": normalized_url, "extraction_config": normalized}


def validate_notification_settings(*, notification_mode="off", notification_threshold=None, notification_currency=""):
    if notification_mode not in Monitor.NotificationMode.values:
        raise ValidationError({"notification_mode": "Выберите режим уведомлений."})
    if notification_mode != "threshold":
        return {"notification_mode": notification_mode, "notification_threshold": None, "notification_currency": ""}
    try:
        if isinstance(notification_threshold, (float, bool)):
            raise ValidationError("Use Decimal for money")
        threshold = Monitor._meta.get_field("notification_threshold").clean(notification_threshold, None)
        if threshold is None or threshold < 0:
            raise ValidationError("Укажите неотрицательную денежную сумму.")
    except (ValidationError, TypeError):
        raise ValidationError({"notification_threshold": "Укажите неотрицательный порог: до 20 целых и 10 дробных знаков."}) from None
    try:
        currency = normalize_currency(notification_currency)
    except ScrapingError:
        raise ValidationError({"notification_currency": "Укажите валюту порога."}) from None
    return {"notification_mode": notification_mode, "notification_threshold": threshold, "notification_currency": currency}


def create_monitor(*, user, name, url, extraction_config=None, notification_settings=None):
    clean = validate_monitor_input(name=name, url=url, extraction_config=extraction_config)
    if not user.is_authenticated:
        raise Monitor.DoesNotExist
    return Monitor.objects.create(owner=user, **clean, **validate_notification_settings(**(notification_settings or {})))


def update_monitor(monitor_id, *, user, name, url, extraction_config=None, notification_settings=None):
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor_id, owner=user, archived_at__isnull=True)
        clean = validate_monitor_input(name=name, url=url, extraction_config=extraction_config)
        if notification_settings is not None:
            clean.update(validate_notification_settings(**notification_settings))
        for field, value in clean.items():
            setattr(monitor, field, value)
        monitor.save(update_fields=[*clean, "updated_at"])
    return monitor


def archive_monitor(monitor_id, *, user):
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor_id, owner=user)
        if monitor.archived_at is None:
            monitor.archived_at = timezone.now()
            monitor.auto_enabled = False
            monitor.next_check_at = None
            monitor.save(update_fields=["archived_at", "auto_enabled", "next_check_at", "updated_at"])
            monitor.check_runs.filter(status=CheckRun.Status.QUEUED).update(
                status=CheckRun.Status.CANCELLED, finished_at=monitor.archived_at,
                next_delivery_at=None,
            )
    return monitor


def _get_or_create_run_locked(monitor, *, scheduled_for=None, now=None):
    """Caller owns the Monitor row lock; both manual and scheduled paths use this."""
    run = monitor.check_runs.filter(status__in=ACTIVE_STATUSES).first()
    if run is not None:
        return run, False
    if scheduled_for is not None:
        previous = monitor.check_runs.filter(scheduled_for=scheduled_for).first()
        if previous is not None:
            return previous, False
    now = now or timezone.now()
    return CheckRun.objects.create(
        monitor=monitor, queued_at=now, url_snapshot=monitor.url,
        extraction_config_snapshot=monitor.extraction_config,
        scheduled_for=scheduled_for,
        next_delivery_at=now,
    ), True


def _reserve_delivery_locked(run, now):
    """Persist the reservation BEFORE contacting Redis; recovery reuses this UUID."""
    if run.status not in ACTIVE_STATUSES or run.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
        return False
    if run.next_delivery_at is not None and run.next_delivery_at > now:
        return False
    run.next_delivery_at = now + timedelta(seconds=DELIVERY_RETRY_SECONDS)
    run.delivery_attempts += 1
    run.save(update_fields=["next_delivery_at", "delivery_attempts"])
    return True


def _publish_run(run, publish):
    """Shared post-commit publication and safe failure handling."""
    try:
        publish(str(run.pk))
    except Exception:
        with transaction.atomic():
            Monitor.objects.select_for_update().get(pk=run.monitor_id)
            CheckRun.objects.filter(pk=run.pk, status=CheckRun.Status.QUEUED).update(
                status=CheckRun.Status.FAILED, finished_at=timezone.now(),
                error_code=CheckQueueError.code, error_message=CheckQueueError.message,
                next_delivery_at=None,
            )
        run.refresh_from_db()
        logger.warning("Check dispatch failed: queue_unavailable")
        raise CheckQueueError(run) from None
    run.refresh_from_db()
    return run


def request_check(monitor_id, *, user, publish):
    """Reserve a durable delivery, then publish outside the transaction.

    Every delivery uses the same active run id. Broker acknowledgement cannot be
    atomic with PostgreSQL, so repeat delivery is explicitly safe at the worker.
    """
    with transaction.atomic(durable=True):
        monitor = Monitor.objects.select_for_update().get(pk=monitor_id, owner=user, archived_at__isnull=True)
        run, created = _get_or_create_run_locked(monitor)
        if not _reserve_delivery_locked(run, timezone.now()):
            return CheckRequest(run=run, created=created, published=False)
    _publish_run(run, publish)
    return CheckRequest(run=run, created=created, published=True)


@contextmanager
def _monitor_execution_lock(monitor_id):
    """Session lock survives short commits but is released when a worker dies.

    Keep this Django connection open until unlock. A row transaction must never
    span slow HTTP. Dedicated PostgreSQL sessions are required (no transaction
    pooling). The stable namespace avoids Python's randomized hash().
    """
    key = int.from_bytes(hashlib.blake2b(f"pricewatch:monitor:{monitor_id}".encode(), digest_size=8).digest(), "big", signed=True)
    if connection.vendor != "postgresql":
        raise RuntimeError("Manual monitoring requires PostgreSQL session locks")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def _claim_run(run_id, monitor_id):
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().get(pk=monitor_id)
        run = CheckRun.objects.select_for_update().get(pk=run_id)
        if run.status not in ACTIVE_STATUSES:
            return run, None
        if monitor.archived_at is not None or not monitor.owner.is_active or (
            run.scheduled_for is not None and not monitor.auto_enabled
        ):
            run.status = CheckRun.Status.CANCELLED
            run.finished_at = timezone.now()
            run.claim_token = None
            run.next_delivery_at = None
            run.save(update_fields=["status", "finished_at", "claim_token", "next_delivery_at"])
            return run, None
        if run.execution_attempts >= MAX_EXECUTION_ATTEMPTS:
            run.status = CheckRun.Status.FAILED
            run.finished_at = timezone.now()
            run.claim_token = None
            run.next_delivery_at = None
            run.error_code, run.error_message = EXECUTION_LIMIT_ERROR
            run.save(update_fields=["status", "finished_at", "claim_token", "next_delivery_at", "error_code", "error_message"])
            return run, None
        # Running + acquired session lock means the old worker no longer owns
        # its session. Reclaim this same attempt, rather than make a new result.
        token = uuid.uuid4()
        run.status = CheckRun.Status.RUNNING
        run.started_at = timezone.now()
        run.claim_token = token
        run.execution_attempts += 1
        run.next_delivery_at = run.started_at + timedelta(seconds=DELIVERY_RETRY_SECONDS)
        run.save(update_fields=["status", "started_at", "claim_token", "next_delivery_at", "execution_attempts"])
        return run, token


def _finish_run(run_id, monitor_id, token, *, product=None, error=None):
    with transaction.atomic():
        monitor = Monitor.objects.select_related("owner").select_for_update(of=("self",)).get(pk=monitor_id)
        run = CheckRun.objects.select_for_update().get(pk=run_id)
        if run.status != CheckRun.Status.RUNNING or run.claim_token != token:
            return run
        now = timezone.now()
        if error is not None:
            run.status = CheckRun.Status.FAILED
            run.error_code, run.error_message = error
        else:
            observation = Observation(
                check_run=run, title=product.title, price=product.price,
                currency=product.currency or "", availability=product.availability,
                source=product.source, diagnostics=list(product.diagnostics), observed_at=now,
            )
            observation.full_clean()
            observation.save()
            from apps.notifications.services import create_price_notification
            create_price_notification(monitor, observation)
            run.status = CheckRun.Status.SUCCEEDED
        run.finished_at = now
        run.claim_token = None
        run.next_delivery_at = None
        run.save(update_fields=["status", "finished_at", "claim_token", "error_code", "error_message", "next_delivery_at"])
        return run


def execute_check(run_id):
    """Idempotently execute one frozen attempt. Duplicate concurrent work skips."""
    try:
        initial = CheckRun.objects.only("monitor_id").get(pk=run_id)
    except (CheckRun.DoesNotExist, ValidationError, ValueError):
        return None
    with _monitor_execution_lock(initial.monitor_id) as acquired:
        if not acquired:
            return None
        run, token = _claim_run(run_id, initial.monitor_id)
        if token is None:
            return run
        try:
            config = ExtractionConfig(**run.extraction_config_snapshot)
            product = services.inspect_url(run.url_snapshot, config)
            return _finish_run(run.pk, run.monitor_id, token, product=product)
        except ScrapingError as error:
            code = error.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error.code) else "inspection_error"
            failure = (code, error.message[:500] or "Не удалось проверить страницу товара.")
        except Exception as error:
            code = "worker_timeout" if type(error).__name__ == "SoftTimeLimitExceeded" else "internal_error"
            failure = (code, "Проверка прервана по времени." if code == "worker_timeout" else "Внутренняя ошибка проверки. Повторите попытку позднее.")
            # Exception text, URL, HTML and traceback can contain page secrets.
            logger.error("Check execution failed: %s", code)
        return _finish_run(run.pk, run.monitor_id, token, error=failure)
