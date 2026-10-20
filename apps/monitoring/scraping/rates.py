"""One host lease shared by processes; no database transaction surrounds network I/O."""

from contextlib import contextmanager
from datetime import timedelta
import math
import uuid

from django.db import transaction
from django.utils import timezone

from ..models import SiteAccessState
from .errors import ScrapingError


class DomainRateLimiter:
    @contextmanager
    def slot(self, host, min_interval, lease_seconds):
        now = timezone.now()
        token = uuid.uuid4()
        with transaction.atomic():
            SiteAccessState.objects.get_or_create(hostname=host, defaults={"next_request_at": now})
            state = SiteAccessState.objects.select_for_update().get(hostname=host)
            ready = max(state.next_request_at, state.lease_until or now)
            if ready > now:
                raise ScrapingError(
                    "rate_limited", "Ограничение частоты запросов к сайту.",
                    details={"retry_after_seconds": max(0.01, (ready - now).total_seconds())},
                )
            state.lease_until = now + timedelta(seconds=lease_seconds)
            state.lease_token = token
            state.next_request_at = now + timedelta(seconds=min_interval)
            state.save()
        try:
            yield
        finally:
            with transaction.atomic():
                state = SiteAccessState.objects.select_for_update().get(hostname=host)
                if state.lease_token == token:
                    state.next_request_at = max(
                        state.next_request_at, timezone.now() + timedelta(seconds=min_interval)
                    )
                    state.lease_token = None
                    state.lease_until = None
                    state.save()

    def defer(self, host, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            return
        with transaction.atomic():
            SiteAccessState.objects.get_or_create(hostname=host)
            state = SiteAccessState.objects.select_for_update().get(hostname=host)
            state.next_request_at = max(state.next_request_at, timezone.now() + timedelta(seconds=seconds))
            state.save(update_fields=["next_request_at"])
