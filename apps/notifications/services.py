"""Explicit event creation and a small PostgreSQL email outbox; no signals."""

from datetime import timedelta
import logging

from django.conf import settings
from django.utils.translation import gettext
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.monitoring.models import Monitor, Observation
from .models import Notification

logger = logging.getLogger(__name__)
EMAIL_RESERVATION = timedelta(seconds=60)


def create_price_notification(monitor, observation):
    """Caller holds the Monitor row lock inside the observation transaction."""
    if (monitor.notification_mode == Monitor.NotificationMode.OFF or observation.price is None
            or not monitor.owner.is_active or monitor.archived_at is not None
            or observation.check_run.url_snapshot != monitor.url):
        return None
    previous = Observation.objects.filter(check_run__monitor=monitor, price__isnull=False).exclude(
        pk=observation.pk,
    ).order_by("-observed_at", "-pk").select_related("check_run").first()
    if previous is None or previous.currency != observation.currency or previous.check_run.url_snapshot != observation.check_run.url_snapshot:
        return None
    old, new = previous.price, observation.price
    mode = monitor.notification_mode
    if old == new or (mode == "decrease" and new >= old):
        return None
    if mode == "threshold" and (monitor.notification_currency != observation.currency or not old >= monitor.notification_threshold > new):
        return None
    pending = monitor.owner.email_notifications_enabled and bool(monitor.owner.email)
    notice, _ = Notification.objects.get_or_create(observation=observation, defaults={
        "owner": monitor.owner, "previous_observation": previous, "rule": mode,
        "email_status": "pending" if pending else "off", "next_email_at": timezone.now() if pending else None,
    })
    return notice


def dispatch_emails(*, publish, now=None, limit=20):
    """Reserve bounded pending rows before Redis; repeats reuse the event ID."""
    now = now or timezone.now()
    # A crashed sender may have transmitted a message: never resend it blindly.
    Notification.objects.filter(email_status="sending", email_started_at__lt=now - timedelta(minutes=2)).update(
        email_status="unknown", email_error_code="sender_interrupted",
    )
    with transaction.atomic(durable=True):
        pending = list(Notification.objects.select_for_update(skip_locked=True).filter(
            email_status="pending", next_email_at__lte=now,
        ).order_by("next_email_at", "pk")[:limit])
        for notice in pending:
            notice.next_email_at = now + EMAIL_RESERVATION
            notice.save(update_fields=["next_email_at"])
    for notice in pending:
        try:
            publish(notice.pk)
        except Exception:
            # The committed event is the outbox. Beat will repair publication.
            logger.warning("Email publication unavailable for notification %s", notice.pk)
    return len(pending)


def send_notification_email(notification_id):
    """Claim before external I/O. Duplicate deliveries never send twice.

    Once sending has started, transport exceptions or worker death are not
    automatically retried: SMTP acknowledgement is not a database transaction.
    """
    with transaction.atomic(durable=True):
        notice = Notification.objects.select_for_update(of=("self",)).select_related(
            "owner", "observation__check_run__monitor", "previous_observation",
        ).filter(pk=notification_id).first()
        if notice is None or notice.email_status != "pending":
            return False
        owner = notice.owner
        monitor = notice.observation.check_run.monitor
        if not owner.email_notifications_enabled or not owner.is_active or not owner.email or monitor.archived_at or monitor.notification_mode == "off":
            notice.email_status = "cancelled"
            notice.next_email_at = None
            notice.save(update_fields=["email_status", "next_email_at"])
            return False
        notice.email_status = "sending"
        notice.email_started_at = timezone.now()
        notice.next_email_at = None
        notice.save(update_fields=["email_status", "email_started_at", "next_email_at"])
    try:
        validate_email(owner.email)
        link = settings.PRICEWATCH_BASE_URL.rstrip("/") + reverse("monitoring:detail", args=[monitor.pk])
        message = EmailMessage(
            subject=gettext("PriceWatch: price change — %(name)s") % {"name": ' '.join(monitor.name.split())[:200]},
            body=render_to_string("notifications/email.txt", {"notice": notice, "monitor": monitor, "link": link}),
            from_email=settings.DEFAULT_FROM_EMAIL, to=[owner.email],
            headers={"Message-ID": f"<pricewatch-notification-{notice.pk}-{notice.observation.check_run_id}@pricewatch.local>"},
        )
        if message.send(fail_silently=False) != 1:
            raise RuntimeError("Backend did not accept the message")
    except Exception:
        Notification.objects.filter(pk=notice.pk, email_status="sending").update(email_status="failed", email_error_code="email_send_failed")
        logger.warning("Email sending failed for notification %s", notice.pk)
        return False
    Notification.objects.filter(pk=notice.pk, email_status="sending").update(email_status="sent", email_sent_at=timezone.now())
    return True
