"""Recover existing manual attempts without rewriting their snapshots or history."""

from django.db import migrations, models
from django.utils import timezone


def prepare_active_runs(apps, schema_editor):
    runs = apps.get_model("monitoring", "CheckRun").objects.using(schema_editor.connection.alias)
    runs.filter(status__in=["queued", "running"], scheduled_for__isnull=True,
                next_delivery_at__isnull=True).update(next_delivery_at=timezone.now())
    # A persisted running claim has already consumed at least one execution.
    runs.filter(status="running").update(execution_attempts=1)


def restore_manual_delivery(apps, schema_editor):
    apps.get_model("monitoring", "CheckRun").objects.using(schema_editor.connection.alias).filter(
        scheduled_for__isnull=True,
    ).update(next_delivery_at=None, delivery_attempts=0)


class Migration(migrations.Migration):
    dependencies = [("monitoring", "0005_monitor_notification_currency_and_more")]

    operations = [
        migrations.RemoveConstraint(model_name="checkrun", name="check_delivery_only_scheduled"),
        migrations.AddField(
            model_name="checkrun", name="execution_attempts",
            field=models.PositiveSmallIntegerField(default=0, editable=False),
        ),
        migrations.RunPython(prepare_active_runs, restore_manual_delivery),
    ]
