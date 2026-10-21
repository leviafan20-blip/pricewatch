"""Exact numeric analytics on PostgreSQL, bounded charts, and owner-scoped HTML."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.monitoring import analytics
from apps.monitoring.forms import HistoryFilterForm
from apps.monitoring.models import CheckRun, Monitor, Observation
from apps.monitoring.templatetags.monitoring import amount, percent_amount

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def add_observations(monitor, values, *, currency="RUB", start=None, step=timedelta(hours=1)):
    """Controlled data only: completed attempts, no URL fetch or broker publication."""
    start = start or NOW - timedelta(days=2)
    runs = [CheckRun(monitor=monitor, status="succeeded", queued_at=start + step * i,
                     started_at=start + step * i, finished_at=start + step * i,
                     url_snapshot=monitor.url) for i in range(len(values))]
    CheckRun.objects.bulk_create(runs)
    return Observation.objects.bulk_create([
        Observation(check_run=run, title=monitor.name, price=Decimal(value) if value is not None else None,
                    currency=currency if value is not None else "", availability="in_stock" if value is not None else "out_of_stock",
                    source="json_ld", observed_at=run.finished_at)
        for run, value in zip(runs, values, strict=True)
    ])


def analyze(monitor, currency="RUB", **data):
    window = analytics.history_window({"period": "all", **data}, now=NOW)
    return analytics.analyze_monitors([monitor], window, currency)[0]


def chart_json(response):
    return json.loads(re.search(r'<script id="price-chart-data" type="application/json">(.*?)</script>', response.content.decode(), re.S)[1])


@pytest.fixture
def owner_client(client, user, monkeypatch):
    client.force_login(user)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    return client


@pytest.mark.parametrize("prices,minimum,maximum,mean,change,percent", [
    (["100", "125", "150"], "100", "150", "125", "50", "50"),
    (["200", "150", "100"], "100", "200", "150", "-100", "-50"),
    (["0.1", "0.2"], "0.1", "0.2", "0.15", "0.1", "100"),
    (["25", "25"], "25", "25", "25", "0", "0"),
    (["100", "80", "100"], "80", "100", None, "0", "0"),
    (["0", "2"], "0", "2", "1", "2", None),
    (["0"], "0", "0", "0", "0", None),
    (["7.1234"], "7.1234", "7.1234", "7.1234", "0", "0"),
    (["999999999999999999.0001", "999999999999999999.0003"], "999999999999999999.0001", "999999999999999999.0003", "999999999999999999.0002", "0.0002", None),
])
def test_decimal_statistics(monitor, prices, minimum, maximum, mean, change, percent):
    add_observations(monitor, prices)
    stats = analyze(monitor)["statistics"]
    assert stats["count"] == len(prices)
    assert stats["minimum"] == Decimal(minimum) and stats["maximum"] == Decimal(maximum)
    assert stats["change"] == Decimal(change)
    if mean is not None:
        assert stats["average"] == Decimal(mean)
    if percent is not None:
        assert stats["percent"] == Decimal(percent)
    elif Decimal(prices[0]) == 0:
        assert stats["percent"] is None
    assert all(not isinstance(value, float) for value in stats.values())


def test_empty_and_unpriced_history(monitor):
    assert analyze(monitor)["statistics"]["count"] == 0
    add_observations(monitor, [None])
    result = analyze(monitor)
    assert result["statistics"]["latest"] is None and result["points"][0]["price"] is None
    assert analyze(monitor, currency="")["statistics"]["count"] == 0


def test_failure_is_not_a_price_and_null_breaks_line(monitor):
    add_observations(monitor, ["100", None, "80"])
    CheckRun.objects.create(monitor=monitor, status="failed", queued_at=NOW, finished_at=NOW,
                            url_snapshot=monitor.url, error_code="timeout", error_message="Controlled failure")
    result = analyze(monitor)
    assert result["statistics"]["average"] == Decimal("90")
    assert [point["price"] for point in result["points"]] == ["100.0000000000", None, "80.0000000000"]
    assert result["statistics"]["percent"] == Decimal("-20")


@pytest.mark.parametrize("currency", ["RUB", "USD", "EUR", "JPY"])
def test_currency_isolation(monitor, currency):
    add_observations(monitor, ["100", "80"], currency=currency)
    add_observations(monitor, ["9999"], currency="GBP")
    assert analyze(monitor, currency)["statistics"]["maximum"] == Decimal("100")
    assert analyze(monitor, currency="")["statistics"]["count"] == 0


def test_order_is_observation_time_then_pk(monitor):
    older = add_observations(monitor, ["100"], start=NOW - timedelta(days=1))[0]
    newest = add_observations(monitor, ["70", "80"], start=NOW, step=timedelta(0))
    # Queued times and UUIDs must not override observation ordering.
    result = analyze(monitor)
    assert result["statistics"]["first"]["pk"] == older.pk
    assert result["statistics"]["latest"]["pk"] == newest[-1].pk
    assert [point["price"] for point in result["points"]] == ["100.0000000000", "70.0000000000", "80.0000000000"]


@pytest.mark.parametrize("period", ["7", "30", "90", "all"])
def test_period_boundaries_and_future_exclusion(monitor, period):
    days = 90 if period == "all" else int(period)
    add_observations(monitor, ["1"], start=NOW - timedelta(days=days, microseconds=1))
    add_observations(monitor, ["2"], start=NOW - timedelta(days=days))
    add_observations(monitor, ["3"], start=NOW)
    add_observations(monitor, ["999"], start=NOW + timedelta(seconds=1))
    result = analyze(monitor, period=period)
    assert result["statistics"]["count"] == (3 if period == "all" else 2)
    assert result["statistics"]["latest"]["price"] == Decimal("3")


@pytest.mark.parametrize("zone,expected_hours", [("Asia/Yekaterinburg", 24), ("America/New_York", 23)])
def test_local_inclusive_dates_and_dst(zone, expected_hours):
    with timezone.override(zone):
        window = analytics.history_window({"period": "all", "date_from": date(2026, 3, 8), "date_to": date(2026, 3, 8)}, now=NOW)
        duration = window.end.astimezone(UTC) - window.start.astimezone(UTC)
        assert duration == timedelta(hours=expected_hours)


def test_date_filter_matches_local_midnight(owner_client, monitor):
    with timezone.override("Asia/Yekaterinburg"):
        add_observations(monitor, ["1", "2", "3"], start=datetime(2026, 10, 18, 18, tzinfo=UTC))
        response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]), {"period": "all", "date_from": "2026-10-19", "date_to": "2026-10-19"})
    assert response.context["analyses"][0]["statistics"]["count"] == 2
    assert response.context["observation_page"].paginator.count == 2
    assert chart_json(response)["timezone"] == "Asia/Yekaterinburg"


@pytest.mark.parametrize("query", [{"period": "bad"}, {"currency": "ZZZ"}, {"currency": ""}, {"date_from": "yesterday"},
                                     {"date_from": "2026-10-20", "date_to": "2026-10-19"}, {"date_to": "9999-12-31"}])
def test_invalid_filters_do_not_expose_unfiltered_data(owner_client, monitor, query):
    add_observations(monitor, ["99"])
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]), query)
    assert response.status_code == 200 and response.context["history_form"].errors
    assert chart_json(response)["series"] == [] and "observation_page" not in response.context


def test_history_pagination_preserves_filters_and_separates_errors(owner_client, monitor):
    add_observations(monitor, ["10"] * 45)
    CheckRun.objects.create(monitor=monitor, status="failed", queued_at=NOW, finished_at=NOW,
                            url_snapshot=monitor.url, error_code="timeout", error_message="Test error")
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]), {"period": "all", "currency": "RUB", "date_from": "2026-10-18", "obs_page": "2", "page": "2"})
    assert len(response.context["observation_page"]) == 20
    assert response.context["observation_page"].paginator.count == 45
    content = response.content.decode()
    assert 'period=all&amp;currency=RUB&amp;date_from=2026-10-18' in content
    observations_table = content.split('id="observations-title"')[1].split('id="history-title"')[0]
    assert "timeout" not in observations_table
    assert response.context["page_obj"].paginator.count == 46


def test_mixed_history_requires_explicit_currency(owner_client, monitor):
    add_observations(monitor, ["100"], currency="RUB")
    add_observations(monitor, ["2"], currency="USD")
    url = reverse("monitoring:detail", args=[monitor.pk])
    assert owner_client.get(url).context["history_form"].errors["currency"]
    response = owner_client.get(url, {"currency": "USD"})
    assert response.context["analyses"][0]["statistics"]["latest"]["price"] == Decimal("2")
    assert response.context["observation_page"].paginator.count == 1


def test_observation_at_exact_request_time_is_included(owner_client, monitor):
    add_observations(monitor, ["123.4567"], start=NOW)
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]))
    assert response.context["currency"] == "RUB"
    assert response.context["analyses"][0]["statistics"]["latest"]["price"] == Decimal("123.4567")


def test_comparison_same_currency_and_missing_currency_error(owner_client, monitor, user):
    other = Monitor.objects.create(owner=user, name="Second", url=monitor.url)
    add_observations(monitor, ["100", "90"])
    add_observations(other, ["200", "150"])
    url = reverse("monitoring:compare")
    response = owner_client.get(url, {"monitor": [monitor.pk, other.pk]})
    assert len(chart_json(response)["series"]) == 2
    assert [item["statistics"]["change"] for item in response.context["analyses"]] == [Decimal("-10"), Decimal("-50")]
    Observation.objects.filter(check_run__monitor=other).update(currency="USD")
    response = owner_client.get(url, {"monitor": [monitor.pk, other.pk], "currency": "RUB"})
    assert response.context["history_form"].non_field_errors() and chart_json(response)["series"] == []


@pytest.mark.parametrize("ids,status", [([], 200), (["-1"], 400), (["bad"], 400), (["1"] * 6, 400), (["9" * 19], 400), (["١"], 400)])
def test_comparison_rejects_invalid_or_large_selection(owner_client, ids, status):
    response = owner_client.get(reverse("monitoring:compare"), {"monitor": ids})
    assert response.status_code == status


def test_owner_isolation_and_anonymous_access(client, monitor, django_user_model):
    url = reverse("monitoring:compare")
    assert client.get(url).status_code == 302
    other = django_user_model.objects.create_user(username="other-analytics")
    own = Monitor.objects.create(owner=other, name="Own", url=monitor.url)
    client.force_login(other)
    assert client.get(reverse("monitoring:detail", args=[monitor.pk]), {"period": "all"}).status_code == 404
    assert client.get(url, {"monitor": [monitor.pk, own.pk]}).status_code == 404
    assert client.get(url, {"monitor": [99999999, own.pk]}).status_code == 404


@pytest.mark.parametrize("prices,message", [([], "No prices for this period yet"), (["5"], "Only one observation"),
                                             (["5", "5"], "The price did not change"), (["5", "4", "5"], "there were changes within the period")])
def test_readable_states_and_safe_chart_json(owner_client, monitor, prices, message):
    monitor.name = '</script><script>alert("XSS")</script>'
    monitor.save()
    add_observations(monitor, prices)
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]))
    assert message in response.content.decode() and monitor.name not in response.content.decode()
    if prices:
        assert chart_json(response)["series"][0]["label"] == monitor.name


def test_batch_queries_do_not_grow_with_monitor_count(monitor, user):
    monitors = [monitor, *Monitor.objects.bulk_create([Monitor(owner=user, name=f"M{i}", url=monitor.url) for i in range(4)])]
    for item in monitors:
        add_observations(item, ["100", "80"])
    window = analytics.HistoryWindow(None, NOW)
    with CaptureQueriesContext(connection) as single:
        analytics.analyze_monitors(monitors[:1], window, "RUB")
    with CaptureQueriesContext(connection) as multiple:
        result = analytics.analyze_monitors(monitors, window, "RUB")
    assert len(single) == len(multiple) <= 6
    assert len(result) == 5
    with pytest.raises(ValueError):
        analytics.analyze_monitors([monitor] * 6, window, "RUB")


def test_large_history_bounded_with_endpoints_extrema_and_exact_stats(owner_client, monitor):
    prices = ["100"] * 10003
    prices[17], prices[9921], prices[-1] = "1", "999", "80"
    observations = add_observations(monitor, prices, start=NOW - timedelta(days=60), step=timedelta(minutes=1))
    with CaptureQueriesContext(connection) as queries:
        result = analyze(monitor)
    assert len(queries) <= 9 and result["sampled"]
    assert len(result["points"]) <= analytics.CHART_LIMIT
    assert result["points"][0]["at"] == observations[0].observed_at.isoformat()
    assert result["points"][-1]["at"] == observations[-1].observed_at.isoformat()
    assert {"1.0000000000", "999.0000000000", "80.0000000000"} <= {item["price"] for item in result["points"]}
    assert result["statistics"]["count"] == 10003
    assert result["statistics"]["minimum"] == Decimal("1")
    response = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]), {"period": "all"})
    assert len(response.context["observation_page"]) == 20
    assert len(chart_json(response)["series"][0]["points"]) <= 400
    assert "Showing up to 400 sampled points" in response.content.decode()


@pytest.mark.parametrize("value,expected", [(Decimal("-123"), "-123"), (Decimal("-1234.5678"), "-1,234.5678"), ("", "—")])
def test_signed_exact_amounts(value, expected):
    assert amount(value) == expected


def test_decimal_display_rounds_only_at_output():
    assert percent_amount(Decimal("-33.333333")) == "-33.33"
    assert percent_amount(None) == "—"
