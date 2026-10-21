"""One immutable price-change event per observation, with durable email state."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.monitoring.money import price_change


class Notification(models.Model):
    class EmailStatus(models.TextChoices):
        OFF = "off", _("Not requested")
        PENDING = "pending", _("Waiting to send")
        SENDING = "sending", _("Sending")
        SENT = "sent", _("Accepted by the email backend")
        FAILED = "failed", _("Sending failed")
        UNKNOWN = "unknown", _("Sending result unknown")
        CANCELLED = "cancelled", _("Sending cancelled")

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="price_notifications")
    observation = models.OneToOneField("monitoring.Observation", on_delete=models.PROTECT, related_name="price_notification")
    previous_observation = models.ForeignKey("monitoring.Observation", on_delete=models.PROTECT, related_name="+")
    rule = models.CharField(max_length=12)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    email_status = models.CharField(max_length=12, choices=EmailStatus.choices, default=EmailStatus.OFF)
    next_email_at = models.DateTimeField(null=True, blank=True)
    email_started_at = models.DateTimeField(null=True, blank=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error_code = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["owner", "-created_at", "-id"], name="notice_owner_created_idx"),
            models.Index(fields=["owner"], condition=Q(read_at__isnull=True), name="notice_owner_unread_idx"),
            models.Index(fields=["next_email_at"], condition=Q(email_status="pending"), name="notice_email_due_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(rule__in=["any", "decrease", "threshold"]), name="notice_valid_rule"),
            models.CheckConstraint(condition=(
                Q(email_status="pending", next_email_at__isnull=False, email_started_at__isnull=True, email_sent_at__isnull=True)
                | Q(email_status__in=["off", "cancelled"], next_email_at__isnull=True, email_started_at__isnull=True, email_sent_at__isnull=True)
                | Q(email_status__in=["sending", "failed", "unknown"], next_email_at__isnull=True, email_started_at__isnull=False, email_sent_at__isnull=True)
                | Q(email_status="sent", next_email_at__isnull=True, email_started_at__isnull=False, email_sent_at__isnull=False)
            ), name="notice_email_state_consistent"),
        ]

    @property
    def change(self):
        return price_change(self.previous_observation.price, self.observation.price)[0]

    @property
    def percent(self):
        return price_change(self.previous_observation.price, self.observation.price)[1]
