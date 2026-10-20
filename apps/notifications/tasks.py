from celery import shared_task

from config.celery import app
from .services import dispatch_emails, send_notification_email


@shared_task(name="notifications.send_email", ignore_result=True, acks_late=True, reject_on_worker_lost=True, soft_time_limit=25, time_limit=30)
def send_email_task(notification_id):
    send_notification_email(notification_id)


def publish_email(notification_id):
    with app.connection_for_write(connect_timeout=1) as connection:
        send_email_task.apply_async(args=[notification_id], task_id=f"notification-email-{notification_id}", connection=connection, retry=False, expires=55)


@shared_task(name="notifications.dispatch_emails", ignore_result=True, soft_time_limit=45, time_limit=50)
def dispatch_emails_task():
    dispatch_emails(publish=publish_email)
