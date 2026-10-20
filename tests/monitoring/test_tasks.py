"""Only adapter wiring is mocked here; real delivery has an opt-in suite."""

from unittest.mock import Mock
import uuid

from apps.monitoring import tasks


def test_task_delegates_to_existing_lifecycle(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(tasks, "execute_check", execute)
    run_id = str(uuid.uuid4())
    tasks.execute_check_task.run(run_id)
    execute.assert_called_once_with(run_id)


def test_publisher_uses_stable_task_id_and_no_retry(monkeypatch):
    connection = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tasks.app, "connection_for_write", Mock(return_value=context))
    publish = Mock()
    monkeypatch.setattr(tasks.execute_check_task, "apply_async", publish)
    run_id = str(uuid.uuid4())
    tasks.publish_check(run_id)
    publish.assert_called_once_with(args=[run_id], task_id=run_id, connection=connection, retry=False)


def test_delivery_configuration():
    assert tasks.execute_check_task.acks_late
    assert tasks.execute_check_task.reject_on_worker_lost
    assert tasks.execute_check_task.soft_time_limit < tasks.execute_check_task.time_limit < 300
    assert tasks.app.conf.task_always_eager is False
    assert tasks.app.conf.result_backend is None


def test_beat_uses_expiring_utc_dispatch_task():
    assert tasks.app.conf.timezone == "UTC" and tasks.app.conf.enable_utc
    schedule = tasks.app.conf.beat_schedule["dispatch-due-monitors"]
    assert schedule["task"] == "monitoring.dispatch_due_checks"
    assert schedule["options"]["expires"] < schedule["schedule"]


def test_periodic_task_delegates_to_database_scheduler(monkeypatch):
    from apps.monitoring import scheduling
    dispatch = Mock()
    monkeypatch.setattr(scheduling, "dispatch_due_checks", dispatch)
    tasks.dispatch_scheduled_checks_task.run()
    dispatch.assert_called_once_with(publish=tasks.publish_check)
