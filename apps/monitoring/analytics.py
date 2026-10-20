"""Bounded, read-only price analytics. Money stays Decimal until browser drawing."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import ceil

from django.db.models import Avg, Case, Count, F, IntegerField, Max, Min, Q, Value, When, Window
from django.db.models.functions import Mod, RowNumber
from django.utils import timezone

from .models import Observation
from .money import price_change

CHART_LIMIT = 400
COMPARISON_LIMIT = 5


@dataclass(frozen=True)
class HistoryWindow:
    start: datetime | None
    end: datetime


def history_window(data, *, now=None):
    """Dates are inclusive in the active Django timezone; SQL bounds are half-open."""
    now = now or timezone.now()
    start = None if data["period"] == "all" else now - timedelta(days=int(data["period"]))
    end = now + timedelta(microseconds=1)
    zone = timezone.get_current_timezone()
    if data.get("date_from"):
        boundary = timezone.make_aware(datetime.combine(data["date_from"], time.min), zone)
        start = max(start, boundary) if start else boundary
    if data.get("date_to"):
        # Form validation excludes date.max, so adding one day cannot overflow.
        boundary = timezone.make_aware(datetime.combine(data["date_to"] + timedelta(days=1), time.min), zone)
        end = min(end, boundary)
    return HistoryWindow(start, end)


def observations_in_window(monitors, window):
    """Callers supply already owner-scoped monitor instances, never arbitrary IDs."""
    rows = Observation.objects.filter(check_run__monitor__in=monitors, observed_at__lt=window.end)
    if window.start:
        rows = rows.filter(observed_at__gte=window.start)
    return rows


def available_currencies(rows):
    # A hard bound also protects the select from malformed legacy/imported data.
    return list(rows.exclude(price=None).order_by("currency").values_list("currency", flat=True).distinct()[:256])


def _statistics(aggregate=None, first=None, latest=None):
    result = dict(aggregate or {"count": 0, "minimum": None, "maximum": None, "average": None})
    result.update(first=first, latest=latest, change=None, percent=None, unchanged=False)
    if first is None:
        return result
    change, percent = price_change(first["price"], latest["price"])
    result.update(change=change, percent=percent, unchanged=result["minimum"] == result["maximum"])
    return result


def analyze_monitors(monitors, window, currency):
    """Batch all monitor queries, with at most 400 points per series.

    PostgreSQL numeric AVG is per observation, not time weighted. Window ranks
    sample on the database; endpoints and global extrema are always retained.
    Large sampled series are drawn without lines to avoid hiding omitted gaps.
    """
    if len(monitors) > COMPARISON_LIMIT:
        raise ValueError("Too many monitors for one chart")
    rows = observations_in_window(monitors, window)
    if currency:
        rows = rows.filter(Q(currency=currency) | Q(price=None))
        priced = rows.filter(currency=currency).exclude(price=None)
    else:
        rows = rows.filter(price=None)
        priced = rows.none()
    key = "check_run__monitor_id"
    totals = dict(rows.order_by().values(key).annotate(total=Count("pk")).values_list(key, "total"))
    aggregates = {
        item[key]: item for item in priced.order_by().values(key).annotate(
            count=Count("pk"), minimum=Min("price"), maximum=Max("price"), average=Avg("price"),
        )
    }

    def boundaries(query, *order):
        return {item[key]: item for item in query.order_by(key, *order).distinct(key).values(key, "pk", "price", "observed_at")}

    first = boundaries(priced, "observed_at", "pk")
    latest = boundaries(priced, "-observed_at", "-pk")
    landmarks = [item["pk"] for item in [*first.values(), *latest.values()]]
    if any(total > CHART_LIMIT for total in totals.values()):
        for query, order in [(rows, ("observed_at", "pk")), (rows, ("-observed_at", "-pk")),
                             (priced, ("price", "observed_at", "pk")), (priced, ("-price", "observed_at", "pk"))]:
            landmarks.extend(item["pk"] for item in boundaries(query, *order).values())
    # Each small series uses stride 1; each large series reserves six landmarks.
    stride = Case(*[
        When(check_run__monitor_id=pk, then=Value(ceil(total / (CHART_LIMIT - 6)) if total > CHART_LIMIT else 1))
        for pk, total in totals.items()
    ], default=Value(1), output_field=IntegerField())
    selected = rows.annotate(chart_row=Window(
        RowNumber(), partition_by=[F(key)], order_by=[F("observed_at").asc(), F("pk").asc()],
    )).annotate(chart_remainder=Mod(F("chart_row") - 1, stride)).filter(
        Q(chart_remainder=0) | Q(pk__in=landmarks),
    ).order_by(key, "observed_at", "pk")
    points = {monitor.pk: [] for monitor in monitors}
    for row in selected.values(key, "observed_at", "price")[:CHART_LIMIT * len(monitors)]:
        points[row[key]].append({"at": row["observed_at"].isoformat(), "price": str(row["price"]) if row["price"] is not None else None})
    return [{
        "monitor": monitor, "currency": currency,
        "statistics": _statistics(aggregates.get(monitor.pk), first.get(monitor.pk), latest.get(monitor.pk)),
        "points": points[monitor.pk], "sampled": totals.get(monitor.pk, 0) > CHART_LIMIT, "total": totals.get(monitor.pk, 0),
    } for monitor in monitors]


def chart_payload(analyses):
    return {
        "timezone": timezone.get_current_timezone_name(),
        "currency": analyses[0]["currency"] if analyses else "",
        "series": [{"label": item["monitor"].name, "points": item["points"], "sampled": item["sampled"]} for item in analyses],
    }
