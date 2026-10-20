from .models import Notification


def unread_notifications(request):
    return {"unread_notification_count": Notification.objects.filter(owner=request.user, read_at=None).count() if request.user.is_authenticated else 0}
