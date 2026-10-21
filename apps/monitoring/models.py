"""User-owned monitors, durable check attempts and their successful observations."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class SiteAccessState(models.Model):
    hostname = models.CharField(max_length=253, primary_key=True)
    next_request_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)


class Monitor(models.Model):
    class NotificationMode(models.TextChoices):
        OFF = "off", _("Off")
        ANY = "any", _("On any price change")
        DECREASE = "decrease", _("On a price decrease")
        THRESHOLD = "threshold", _("On a downward threshold crossing")

    class Interval(models.IntegerChoices):
        HOUR = 1, _("Every hour")
        THREE_HOURS = 3, _("Every 3 hours")
        SIX_HOURS = 6, _("Every 6 hours")
        TWELVE_HOURS = 12, _("Every 12 hours")
        DAY = 24, _("Every 24 hours")

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="monitors")
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=2048)
    extraction_config = models.JSONField(default=dict, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    auto_enabled = models.BooleanField(default=False)
    check_interval_hours = models.PositiveSmallIntegerField(choices=Interval.choices, default=Interval.SIX_HOURS)
    next_check_at = models.DateTimeField(null=True, blank=True)
    last_scheduled_at = models.DateTimeField(null=True, blank=True)
    notification_mode = models.CharField(max_length=12, choices=NotificationMode.choices, default=NotificationMode.OFF)
    notification_threshold = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    notification_currency = models.CharField(max_length=3, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["owner", "archived_at"], name="monitor_owner_archived_idx"),
            models.Index(fields=["next_check_at"], condition=Q(auto_enabled=True, archived_at__isnull=True), name="monitor_next_due_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(notification_mode__in=["off", "any", "decrease", "threshold"]), name="monitor_valid_notification_mode"),
            models.CheckConstraint(condition=(
                Q(notification_mode="threshold", notification_threshold__gte=0, notification_threshold__isnull=False) & ~Q(notification_currency="")
                | ~Q(notification_mode="threshold") & Q(notification_threshold__isnull=True, notification_currency="")
            ), name="monitor_notification_threshold"),
            models.CheckConstraint(condition=Q(check_interval_hours__in=[1, 3, 6, 12, 24]), name="monitor_valid_interval"),
            models.CheckConstraint(condition=(
                Q(auto_enabled=True, next_check_at__isnull=False, archived_at__isnull=True)
                | Q(auto_enabled=False, next_check_at__isnull=True)
            ), name="monitor_schedule_consistent"),
        ]

    def __str__(self):
        return self.name


class CheckRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Error")
        CANCELLED = "cancelled", _("Cancelled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    monitor = models.ForeignKey(Monitor, on_delete=models.PROTECT, related_name="check_runs")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    url_snapshot = models.URLField(max_length=2048)
    extraction_config_snapshot = models.JSONField(default=dict, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    next_delivery_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveSmallIntegerField(default=0)
    execution_attempts = models.PositiveSmallIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["-queued_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["monitor"], condition=Q(status__in=["queued", "running"]), name="one_active_check_per_monitor"),
            models.UniqueConstraint(fields=["monitor", "scheduled_for"], condition=Q(scheduled_for__isnull=False), name="one_scheduled_run_per_slot"),
            models.CheckConstraint(condition=Q(next_delivery_at__isnull=True) | Q(status__in=["queued", "running"]), name="check_delivery_only_active"),
            models.CheckConstraint(condition=(
                Q(status="queued", started_at__isnull=True, finished_at__isnull=True, claim_token__isnull=True)
                | Q(status="running", started_at__isnull=False, finished_at__isnull=True, claim_token__isnull=False)
                | Q(status="succeeded", started_at__isnull=False, finished_at__isnull=False, claim_token__isnull=True)
                | Q(status__in=["failed", "cancelled"], finished_at__isnull=False, claim_token__isnull=True)
            ), name="check_run_state_consistent"),
            models.CheckConstraint(condition=(Q(status="failed") & ~Q(error_code="") & ~Q(error_message="")) | (~Q(status="failed") & Q(error_code="", error_message="")), name="check_run_error_consistent"),
            models.CheckConstraint(condition=Q(started_at__isnull=True) | Q(started_at__gte=F("queued_at")), name="check_run_started_after_queue"),
            models.CheckConstraint(condition=Q(finished_at__isnull=True) | Q(finished_at__gte=F("queued_at")), name="check_run_finished_after_queue"),
            models.CheckConstraint(condition=Q(finished_at__isnull=True) | Q(started_at__isnull=True) | Q(finished_at__gte=F("started_at")), name="check_run_finished_after_start"),
        ]

    def save(self, *args, **kwargs):
        # Snapshot fields describe what this attempt actually checked, including
        # after its monitor was edited. Services never bulk-update these fields.
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values("url_snapshot", "extraction_config_snapshot", "scheduled_for").first()
            if original and any(original[field] != getattr(self, field) for field in original):
                raise ValidationError(_("An existing check's settings cannot be changed."))
        return super().save(*args, **kwargs)


class Observation(models.Model):
    class Availability(models.TextChoices):
        IN_STOCK = "in_stock", _("In stock")
        OUT_OF_STOCK = "out_of_stock", _("Out of stock")
        PREORDER = "preorder", _("Preorder")
        UNKNOWN = "unknown", _("Unknown")

    check_run = models.OneToOneField(CheckRun, on_delete=models.PROTECT, related_name="observation")
    title = models.CharField(max_length=1000)
    price = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    availability = models.CharField(max_length=20, choices=Availability.choices)
    source = models.CharField(max_length=20)
    diagnostics = models.JSONField(default=list, blank=True)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-observed_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(price__isnull=True) | Q(price__gte=0), name="observation_nonnegative_price"),
            models.CheckConstraint(condition=Q(price__isnull=False) | Q(availability="out_of_stock"), name="observation_price_or_out_of_stock"),
            models.CheckConstraint(condition=Q(price__isnull=True) | ~Q(currency=""), name="observation_price_has_currency"),
            models.CheckConstraint(condition=Q(availability__in=["in_stock", "out_of_stock", "preorder", "unknown"]), name="observation_known_availability"),
        ]
