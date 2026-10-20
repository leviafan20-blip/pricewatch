"""Owner-scoped HTML views; network work is dispatched to the worker."""

from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import analytics
from .checks import CheckQueueError, archive_monitor, create_monitor, request_check, update_monitor
from .forms import HistoryFilterForm, MonitorForm, ScheduleForm
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
            messages.success(request, "Мониторинг создан. Теперь можно запустить первую проверку.")
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
        history = analytics.observations_in_window([monitor], context["history_window"])
        if context["currency"]:
            history = history.filter(Q(currency=context["currency"]) | Q(price=None))
        context["observation_page"] = Paginator(history.order_by("-observed_at", "-pk"), 20).get_page((query or {}).get("obs_page"))
    return context


def _history_context(monitors, query, *, comparing=False):
    """Normalize bounded GET filters before accessing analytics; invalid input shows no data."""
    now = timezone.now()
    all_rows = analytics.observations_in_window(monitors, analytics.history_window({"period": "all"}, now=now))
    currencies = analytics.available_currencies(all_rows)
    data = query.copy()
    data.setdefault("period", "30")
    data.setdefault("currency", currencies[0] if len(currencies) == 1 else "")
    form = HistoryFilterForm(data, currencies=currencies)
    context = {"history_form": form, "analyses": [], "currency": "", "chart_data": analytics.chart_payload([])}
    if not form.is_valid():
        return context
    currency = form.cleaned_data["currency"]
    if currencies and not currency:
        form.add_error("currency", "Выберите одну валюту из истории; конвертация не выполняется.")
        return context
    window = analytics.history_window(form.cleaned_data, now=now)
    analyses = analytics.analyze_monitors(monitors, window, currency)
    if comparing and analyses and any(not item["statistics"]["count"] for item in analyses):
        form.add_error(None, "Для сравнения у каждого выбранного мониторинга должна быть цена в одной валюте за выбранный период.")
        return context
    context.update(history_window=window, currency=currency, analyses=analyses,
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
def monitor_compare(request):
    # Selection is drawn from a paginated owner-scoped list, not an unbounded select.
    ids = request.GET.getlist("monitor")
    if len(ids) > analytics.COMPARISON_LIMIT or any(not value.isascii() or not value.isdecimal() or len(value) > 18 for value in ids):
        return render(request, "monitoring/compare.html", {"selection_error": "Выберите от 2 до 5 мониторингов в списке."}, status=400)
    ids = list(dict.fromkeys(int(value) for value in ids))
    monitors = list(Monitor.objects.filter(owner=request.user, pk__in=ids).order_by("pk"))
    if len(monitors) != len(ids):
        raise Http404
    context = {"selected_monitors": monitors}
    if len(monitors) < 2:
        context["selection_error"] = "Выберите от 2 до 5 мониторингов в списке."
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
            messages.success(request, "Расписание сохранено." if form.cleaned_data["auto_enabled"] else "Автоматический мониторинг приостановлен.")
            return redirect("monitoring:detail", pk=pk)
    return render(request, "monitoring/detail.html", _detail_context(monitor, schedule_form=form), status=400)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def monitor_edit(request, pk):
    monitor = _owned_monitor(request, pk)
    if monitor.archived_at is not None:
        messages.warning(request, "Архивный мониторинг доступен только для просмотра.")
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
            messages.success(request, "Настройки мониторинга сохранены.")
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
        messages.success(request, "Мониторинг архивирован. История проверок сохранена.")
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
            messages.error(request, "Не удалось отправить задание в очередь. Ошибка сохранена в истории; повторите проверку позднее.")
        else:
            messages.warning(request, "Связь с очередью прервана. Состояние уже принятой проверки сохранено; обновите страницу перед повторной отправкой.")
    except ValidationError as error:
        messages.warning(request, " ".join(error.messages))
    else:
        if result.created:
            messages.success(request, "Проверка поставлена в очередь. Обновите страницу, чтобы увидеть результат.")
        else:
            messages.info(request, "Повторно отправлено существующее задание. Новая проверка не создана." if result.published else "Задание уже отправлено. Дождитесь результата; новая проверка не создана.")
    return redirect("monitoring:detail", pk=pk)
