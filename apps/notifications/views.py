"""Owner-scoped HTML inbox. Reading never changes state; all writes use POST."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.utils.translation import gettext as _

from .forms import EmailPreferencesForm
from .models import Notification


@login_required
@never_cache
@require_GET
def notification_list(request):
    unread_only = request.GET.get("state") == "unread"
    notices = Notification.objects.filter(owner=request.user).select_related("observation__check_run__monitor", "previous_observation")
    if unread_only:
        notices = notices.filter(read_at=None)
    page = Paginator(notices, 20).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page_obj": page, "unread_only": unread_only})


@login_required
@never_cache
@require_POST
def mark_read(request, pk):
    notice = get_object_or_404(Notification, pk=pk, owner=request.user)
    Notification.objects.filter(pk=notice.pk, owner=request.user, read_at=None).update(read_at=timezone.now())
    return redirect("notifications:list")


@login_required
@never_cache
@require_POST
def mark_all_read(request):
    Notification.objects.filter(owner=request.user, read_at=None).update(read_at=timezone.now())
    return redirect("notifications:list")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def email_preferences(request):
    form = EmailPreferencesForm(request.POST if request.method == "POST" else None, initial={
        "email": request.user.email, "email_notifications_enabled": request.user.email_notifications_enabled,
    })
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            get_user_model().objects.filter(pk=request.user.pk).update(**form.cleaned_data)
            if not form.cleaned_data["email_notifications_enabled"]:
                Notification.objects.filter(owner=request.user, email_status="pending").update(email_status="cancelled", next_email_at=None)
        messages.success(request, _("Email preferences have been saved. Previous notifications will not be resent."))
        return redirect("notifications:settings")
    return render(request, "notifications/settings.html", {"form": form})
