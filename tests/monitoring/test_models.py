"""Database constraints provide the last line of defense against duplicate state."""

from decimal import Decimal
import uuid

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
import pytest

from apps.monitoring.models import CheckRun, Observation

pytestmark = pytest.mark.django_db


def test_only_one_queued_or_running_attempt(monitor):
    run = CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url)
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url)
    CheckRun.objects.filter(pk=run.pk).update(status="running", started_at=timezone.now(), claim_token=uuid.uuid4())
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url)


@pytest.mark.parametrize("changes", [{"status": "running"}, {"status": "succeeded", "finished_at": timezone.now()}, {"status": "failed", "finished_at": timezone.now()}, {"error_code": "failure", "error_message": "bad"}, {"status": "unknown"}])
def test_inconsistent_state_rejected(monitor, changes):
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url, **changes)


def test_observation_unique_per_run_and_history_protected(monitor):
    now = timezone.now()
    run = CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url, status="succeeded", queued_at=now, started_at=now, finished_at=now)
    values = dict(check_run=run, title="A", price=Decimal("10.12"), currency="USD", availability="in_stock", source="json_ld")
    Observation.objects.create(**values)
    with pytest.raises(IntegrityError), transaction.atomic():
        Observation.objects.create(**values)
    with pytest.raises(ProtectedError):
        run.delete()
    with pytest.raises(ProtectedError):
        monitor.delete()


@pytest.mark.parametrize("price,currency,availability", [(Decimal("-1"), "USD", "in_stock"), (None, "", "in_stock"), (Decimal("10"), "", "in_stock"), (Decimal("10"), "USD", "wrong")])
def test_invalid_observations_rejected(monitor, price, currency, availability):
    run = CheckRun.objects.create(monitor=monitor, url_snapshot=monitor.url)
    with pytest.raises(IntegrityError), transaction.atomic():
        Observation.objects.create(check_run=run, title="A", price=price, currency=currency, availability=availability, source="json_ld")
