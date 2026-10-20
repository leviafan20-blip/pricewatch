"""Opt-in, repeatable restart evidence for the isolated Docker integration DB.

Run prepare/wait-first, stop the integration services, run overdue, restart those
services, then verify. This never imports the worker's test transport in Django.
"""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import time
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.integration")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
import redis  # noqa: E402

from apps.monitoring.checks import create_monitor  # noqa: E402
from apps.monitoring.models import CheckRun, Monitor, Observation  # noqa: E402
from apps.monitoring.scheduling import configure_schedule  # noqa: E402
from tests.integration.http_server import counter_key  # noqa: E402

STATE_PATH = settings.BASE_DIR / "artifacts" / "stage4-restart.json"


def guard():
    if (os.environ.get("PRICEWATCH_INTEGRATION") != "1"
            or settings.SETTINGS_MODULE != "config.settings.integration"
            or connection.settings_dict["NAME"] != "test_pricewatch"):
        raise RuntimeError("Restart probe is restricted to the isolated integration database")


def save(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def wait_success(monitor, count):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if monitor.check_runs.filter(status=CheckRun.Status.SUCCEEDED).count() >= count:
            return
        time.sleep(0.1)
    raise RuntimeError("Automatic check did not finish within 30 seconds")


def run(phase):
    guard()
    if phase == "prepare":
        token = uuid.uuid4().hex
        user = get_user_model().objects.create_user(username="restart-" + token[:16])
        monitor = create_monitor(
            user=user, name="Проверка перезапуска расписания",
            url=f"http://shop.pricewatch.example/products/{token}/success",
        )
        configure_schedule(monitor.pk, user=user, enabled=True, interval_hours=1)
        state = {"monitor_id": monitor.pk, "owner_id": user.pk, "token": token}
    else:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        monitor = Monitor.objects.get(pk=state["monitor_id"], owner_id=state["owner_id"])
        if phase == "wait-first":
            wait_success(monitor, 1)
            observation = Observation.objects.get(check_run__monitor=monitor)
            state["checks_before_restart"] = monitor.check_runs.count()
            state["previous_observation_id"] = observation.pk
            state["previous_price"] = str(observation.price)
        elif phase == "overdue":
            due = timezone.now() - timedelta(days=7)
            Monitor.objects.filter(pk=monitor.pk).update(next_check_at=due)
            state["overdue_slot_utc"] = due.isoformat()
        elif phase == "verify":
            wait_success(monitor, state["checks_before_restart"] + 1)
            time.sleep(3)  # Several more real Beat ticks must not create a backlog.
            monitor.refresh_from_db()
            previous = Observation.objects.get(pk=state["previous_observation_id"])
            assert str(previous.price) == state["previous_price"]
            state["checks_after_restart"] = monitor.check_runs.count()
            state["observations_after_restart"] = Observation.objects.filter(check_run__monitor=monitor).count()
            assert state["checks_after_restart"] == state["checks_before_restart"] + 1
            assert state["observations_after_restart"] == state["checks_after_restart"]
            state["last_scheduled_at_utc"] = monitor.last_scheduled_at.isoformat()
            state["next_check_at_utc"] = monitor.next_check_at.isoformat()
            state["interval_seconds"] = (monitor.next_check_at - monitor.last_scheduled_at).total_seconds()
            assert state["interval_seconds"] == 3600
            latest = monitor.check_runs.first()
            assert latest.scheduled_for.isoformat() == state["overdue_slot_utc"]
            with connection.cursor() as cursor:
                cursor.execute("SHOW TIME ZONE")
                state["postgres_timezone"] = cursor.fetchone()[0]
            with redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True) as client:
                state["http_requests"] = int(client.get(counter_key(state["token"], "requests")))
                state["overlap"] = bool(client.get(counter_key(state["token"], "overlap")))
            assert state["http_requests"] == state["checks_after_restart"] and not state["overlap"]
            configure_schedule(monitor.pk, user=monitor.owner, enabled=False, interval_hours=1)
            state["paused_after_verification"] = True
    state["phase"] = phase
    save(state)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "wait-first", "overdue", "verify"])
    run(parser.parse_args().phase)
