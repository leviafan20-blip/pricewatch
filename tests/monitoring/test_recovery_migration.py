"""Exercise the data migration against legacy queued/running manual attempts."""

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_existing_manual_attempts_are_recoverable_and_history_is_unchanged(user):
    old = ("monitoring", "0005_monitor_notification_currency_and_more")
    new = ("monitoring", "0006_checkrun_delivery_recovery")
    executor = MigrationExecutor(connection)
    executor.migrate([old])
    try:
        apps = executor.loader.project_state([old]).apps
        Monitor = apps.get_model("monitoring", "Monitor")
        Run = apps.get_model("monitoring", "CheckRun")
        now = timezone.now()
        runs = []
        for status in ("queued", "running", "succeeded"):
            monitor = Monitor.objects.create(owner_id=user.pk, name=status, url="https://shop.example/legacy")
            values = dict(monitor=monitor, status=status, queued_at=now, url_snapshot=monitor.url,
                          extraction_config_snapshot={"price_selector": ".legacy"})
            if status != "queued":
                values["started_at"] = now
            if status == "running":
                values["claim_token"] = uuid.uuid4()
            if status == "succeeded":
                values["finished_at"] = now
            runs.append(Run.objects.create(**values))
        executor = MigrationExecutor(connection)
        executor.migrate([new])
        Run = executor.loader.project_state([new]).apps.get_model("monitoring", "CheckRun")
        for original in runs:
            migrated = Run.objects.get(pk=original.pk)
            assert migrated.status == original.status
            assert migrated.url_snapshot == original.url_snapshot
            assert migrated.extraction_config_snapshot == original.extraction_config_snapshot
            assert migrated.started_at == original.started_at and migrated.finished_at == original.finished_at
            assert migrated.claim_token == original.claim_token
            assert migrated.execution_attempts == (1 if original.status == "running" else 0)
            assert (migrated.next_delivery_at is not None) == (original.status != "succeeded")
            assert migrated.delivery_attempts == 0
    finally:
        MigrationExecutor(connection).migrate([new])
