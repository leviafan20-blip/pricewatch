"""Owner-scoped HTML views; network work is dispatched to the worker."""

from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.utils.translation import gettext as _

from . import analytics
from .checks import CheckQueueError, archive_monitor, create_monitor, request_check, update_monitor
from .exports import CONTENT_TYPES, MAX_EXPORT_ROWS, ExportTooLarge, build_export
from .forms import MonitorForm, ScheduleForm
from .history import history_filters, history_rows
from .models import CheckRun, Monitor, Observation
from .scheduling import configure_schedule
from .tasks import publish_check


def _owned_monitor(request, pk):
    return get_object_or_404(Monitor, pk=pk, owner=request.user)


def _display_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


@login_required
@never_cache
@require_GET
def monitor_list(request):
    archived = request.GET.get("state") == "archived"
    observations = Observation.objects.filter(check_run__monitor=OuterRef("pk"))
    priced = observations.exclude(price=None).order_by("-observed_at", "-pk")
    latest_runs = CheckRun.objects.filter(monitor=OuterRef("pk")).order_by("-queued_at", "-pk")
    monitors = Monitor.objects.filter(owner=request.user, archived_at__isnull=not archived).annotate(
        last_price=Subquery(priced.values("price")[:1]),
        last_currency=Subquery(priced.values("currency")[:1]),
        last_check_status=Subquery(latest_runs.values("status")[:1]),
        last_check_at=Subquery(latest_runs.values("queued_at")[:1]),
    ).order_by("-created_at", "-pk")
    page = Paginator(monitors, 20).get_page(request.GET.get("page"))
    return render(request, "monitoring/list.html", {"page_obj": page, "archived": archived})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def monitor_create(request):
    form = MonitorForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            monitor = create_monitor(
                user=request.user, name=form.cleaned_data["name"], url=form.cleaned_data["url"],
                extraction_config=form.cleaned_data["extraction_config"],
                notification_settings=form.cleaned_data["notification_settings"],
            )
        except ValidationError as error:
            form.add_error(None, error.messages)
        else:
            messages.success(request, _("The monitor has been created. You can now start its first check."))
            return redirect("monitoring:detail", pk=monitor.pk)
    return render(request, "monitoring/form.html", {
        "form": form, "editing": False, "allowed_hosts": settings.SCRAPING_ALLOWED_HOSTS,
    })


def _detail_context(monitor, *, page=None, schedule_form=None, query=None):
    observations = Observation.objects.filter(check_run__monitor=monitor).order_by("-observed_at", "-pk")
    runs = monitor.check_runs.order_by("-queued_at", "-pk")
    context = {
        "monitor": monitor,
        "display_url": _display_url(monitor.url),
        "latest_observation": observations.select_related("check_run").first(),
        "latest_price": observations.select_related("check_run").exclude(price=None).first(),
        "latest_run": runs.first(),
        "pending_run": runs.filter(status__in=[CheckRun.Status.QUEUED, CheckRun.Status.RUNNING]).first(),
        "page_obj": Paginator(runs, 20).get_page(page),
        "schedule_form": schedule_form or ScheduleForm(monitor=monitor),
    }
    context.update(_history_context([monitor], query or {}))
    if context.get("history_window"):
        history = history_rows([monitor], context["history_window"], context["currency"])
        context["observation_page"] = Paginator(history.order_by("-observed_at", "-pk"), 20).get_page((query or {}).get("obs_page"))
    return context


def _history_context(monitors, query, *, comparing=False):
    """Normalize bounded GET filters before accessing analytics; invalid input shows no data."""
    context = history_filters(monitors, query)
    context.update(analyses=[], chart_data=analytics.chart_payload([]))
    form = context["history_form"]
    if "history_window" not in context:
        return context
    analyses = analytics.analyze_monitors(monitors, context["history_window"], context["currency"])
    if comparing and analyses and any(not item["statistics"]["count"] for item in analyses):
        form.add_error(None, _("Each selected monitor must have a price in the same currency during the selected period to compare them."))
        return context
    context.update(analyses=analyses,
                   chart_data=analytics.chart_payload(analyses), has_prices=any(item["statistics"]["count"] for item in analyses))
    return context


@login_required
@never_cache
@require_GET
def monitor_detail(request, pk):
    monitor = _owned_monitor(request, pk)
    return render(request, "monitoring/detail.html", _detail_context(monitor, page=request.GET.get("page"), query=request.GET))


@login_required
@never_cache
@require_GET
def export_history(request, pk=None):
    monitor = _owned_monitor(request, pk) if pk is not None else None
    monitors = [monitor] if monitor else Monitor.objects.filter(owner=request.user)
    context = history_filters(monitors, request.GET, require_currency=monitor is not None)
    context.update(monitor=monitor, export_limit=MAX_EXPORT_ROWS)
    form = context["history_form"]
    file_format = request.GET.get("format")
    if file_format is not None:
        if file_format not in CONTENT_TYPES:
            form.add_error(None, _("Select CSV or XLSX."))
        if not form.errors:
            rows = history_rows(monitors, context["history_window"], context["currency"])
            try:
                output = build_export(rows, file_format)
            except ExportTooLarge:
                form.add_error(None, _("Exports are limited to %(limit)s observations. Choose a shorter period or specific dates.") % {"limit": MAX_EXPORT_ROWS})
            else:
                scope = str(monitor.pk) if monitor else "history"
                return FileResponse(output, as_attachment=True, filename=f"pricewatch-{scope}.{file_format}",
                                    content_type=CONTENT_TYPES[file_format])
    return render(request, "monitoring/export.html", context, status=400 if form.errors else 200)


@login_required
@never_cache
@require_GET
def monitor_compare(request):
    # Selection is drawn from a paginated owner-scoped list, not an unbounded select.
    ids = request.GET.getlist("monitor")
    if len(ids) > analytics.COMPARISON_LIMIT or any(not value.isascii() or not value.isdecimal() or len(value) > 18 for value in ids):
        return render(request, "monitoring/compare.html", {"selection_error": _("Select 2 to 5 monitors from the list.")}, status=400)
    ids = list(dict.fromkeys(int(value) for value in ids))
    monitors = list(Monitor.objects.filter(owner=request.user, pk__in=ids).order_by("pk"))
    if len(monitors) != len(ids):
        raise Http404
    context = {"selected_monitors": monitors}
    if len(monitors) < 2:
        context["selection_error"] = _("Select 2 to 5 monitors from the list.")
    else:
        context.update(_history_context(monitors, request.GET, comparing=True))
    return render(request, "monitoring/compare.html", context)


@login_required
@never_cache
@require_POST
def monitor_schedule(request, pk):
    monitor = _owned_monitor(request, pk)
    if monitor.archived_at is not None:
        raise Http404
    form = ScheduleForm(request.POST, monitor=monitor)
    if form.is_valid():
        try:
            configure_schedule(
                pk, user=request.user, enabled=form.cleaned_data["auto_enabled"],
                interval_hours=form.cleaned_data["check_interval_hours"],
            )
        except Monitor.DoesNotExist:
            raise Http404 from None
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Schedule saved.") if form.cleaned_data["auto_enabled"] else _("Automatic monitoring is paused."))
            return redirect("monitoring:detail", pk=pk)
    return render(request, "monitoring/detail.html", _detail_context(monitor, schedule_form=form), status=400)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def monitor_edit(request, pk):
    monitor = _owned_monitor(request, pk)
    if monitor.archived_at is not None:
        messages.warning(request, _("Archived monitors are read-only."))
        return redirect("monitoring:detail", pk=pk)
    form = MonitorForm(request.POST if request.method == "POST" else None, monitor=monitor)
    if request.method == "POST" and form.is_valid():
        try:
            update_monitor(
                pk, user=request.user, name=form.cleaned_data["name"], url=form.cleaned_data["url"],
                extraction_config=form.cleaned_data["extraction_config"],
                notification_settings=form.cleaned_data["notification_settings"],
            )
        except Monitor.DoesNotExist:
            raise Http404 from None
        except ValidationError as error:
            form.add_error(None, error.messages)
        else:
            messages.success(request, _("Monitor settings have been saved."))
            return redirect("monitoring:detail", pk=pk)
    return render(request, "monitoring/form.html", {
        "form": form, "monitor": monitor, "editing": True, "allowed_hosts": settings.SCRAPING_ALLOWED_HOSTS,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def monitor_archive(request, pk):
    monitor = _owned_monitor(request, pk)
    if request.method == "POST":
        try:
            archive_monitor(pk, user=request.user)
        except Monitor.DoesNotExist:
            raise Http404 from None
        messages.success(request, _("The monitor has been archived. Check history is preserved."))
        return redirect("monitoring:detail", pk=pk)
    return render(request, "monitoring/archive.html", {"monitor": monitor})


@login_required
@never_cache
@require_POST
def monitor_check(request, pk):
    _owned_monitor(request, pk)
    try:
        result = request_check(pk, user=request.user, publish=publish_check)
    except Monitor.DoesNotExist:
        raise Http404 from None
    except CheckQueueError as error:
        if error.run.status == CheckRun.Status.FAILED:
            messages.error(request, _("Could not send the task to the queue. The error is saved in the check log; try again later."))
        else:
            messages.warning(request, _("The queue connection was interrupted. The accepted check's state is preserved; refresh the page before resending."))
    except ValidationError as error:
        messages.warning(request, " ".join(error.messages))
    else:
        if result.created:
            messages.success(request, _("The check has been queued. Refresh the page to see the result."))
        else:
            messages.info(request, _("The existing task has been resent. No new check was created.") if result.published else _("The task has already been sent. Wait for the result; no new check was created."))
    return redirect("monitoring:detail", pk=pk)
