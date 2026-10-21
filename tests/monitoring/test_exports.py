"""Real PostgreSQL selection and independent inspection of CSV and XLSX files."""

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
import pytest

from apps.monitoring import exports
from apps.monitoring.models import CheckRun, Monitor, Observation
from tests.monitoring.test_analytics import NOW, add_observations

pytestmark = pytest.mark.django_db
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def owner_client(client, user, monkeypatch):
    client.force_login(user)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    return client


def download(client, monitor=None, **query):
    url = reverse("monitoring:export", args=[monitor.pk]) if monitor else reverse("monitoring:export_all")
    return client.get(url, {"format": "csv", "period": "all", **query})


def body(response):
    # Django's test client closes the response when this iterator is exhausted.
    # Closing it twice also fires request_finished twice inside pytest's transaction.
    return b"".join(response.streaming_content)


def csv_rows(response):
    content = body(response)
    assert content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(StringIO(content.decode("utf-8-sig"), newline=""), delimiter=";"))


def worksheet(response):
    with ZipFile(BytesIO(body(response))) as archive:
        assert archive.testzip() is None
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert not sheet.findall(".//s:f", NS)
        assert not sheet.findall(".//s:hyperlinks", NS)
        return sheet, ET.fromstring(archive.read("xl/styles.xml"))


def cell_value(cell):
    if cell is None:
        return None
    return "".join(cell.itertext())


@pytest.mark.parametrize("route", ["export", "export_all"])
def test_login_required(client, monitor, route):
    url = reverse("monitoring:" + route, args=[monitor.pk] if route == "export" else [])
    assert client.get(url, {"format": "csv"}).status_code == 302


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_foreign_monitor_is_404_even_for_staff(owner_client, user, django_user_model, file_format):
    other = django_user_model.objects.create_user(username="other-export")
    foreign = Monitor.objects.create(owner=other, name="Foreign", url="https://shop.example/private")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    assert download(owner_client, foreign, format=file_format).status_code == 404
    assert owner_client.get(reverse("monitoring:export", args=[999999]), {"format": file_format}).status_code == 404


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_empty_exports_and_private_response_headers(owner_client, monitor, file_format):
    response = download(owner_client, monitor, format=file_format)
    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]
    assert response["Content-Disposition"].endswith(f'pricewatch-{monitor.pk}.{file_format}"')
    assert "private" in response["Cache-Control"] and "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Type"] == exports.CONTENT_TYPES[file_format]
    if file_format == "csv":
        assert csv_rows(response) == []
    else:
        sheet, _ = worksheet(response)
        assert len(sheet.findall("s:sheetData/s:row", NS)) == 1


def test_csv_unicode_decimal_snapshot_and_no_failed_runs(owner_client, monitor):
    observation = add_observations(monitor, ["1234.1234567890"])[0]
    observation.title = 'Лампа; "Desk"\n日本語'
    observation.save(update_fields=["title"])
    CheckRun.objects.filter(pk=observation.check_run_id).update(url_snapshot="https://shop.example/old?variant=2")
    CheckRun.objects.create(monitor=monitor, status="failed", queued_at=NOW, finished_at=NOW,
                            url_snapshot=monitor.url, error_code="timeout", error_message="Do not export me")
    rows = csv_rows(download(owner_client, monitor))
    assert len(rows) == 1 and list(rows[0]) == list(exports.HEADERS)
    row = rows[0]
    assert row["Product"] == observation.title
    assert row["Monitor"] == monitor.name
    assert row["URL"] == "https://shop.example/old?variant=2"
    assert row["Price"] == row["Price exact"] == "1234.1234567890"
    assert row["Currency"] == "RUB" and row["Source"] == "json_ld"
    assert row["Availability"] == "in_stock"
    assert datetime.fromisoformat(row["Observed at"]) == observation.observed_at
    assert row["Timezone"] == "Asia/Yekaterinburg"
    assert row["Check ID"] == str(observation.check_run_id)


def test_filters_match_detail_and_ignore_pagination(owner_client, monitor):
    add_observations(monitor, ["10"] * 45, start=NOW - timedelta(days=5))
    add_observations(monitor, ["20"], currency="USD", start=NOW - timedelta(days=4))
    query = {"period": "7", "currency": "RUB", "date_from": "2026-10-16", "date_to": "2026-10-16", "obs_page": "2", "page": "8"}
    detail = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]), query)
    expected = detail.context["observation_page"].paginator.count
    rows = csv_rows(download(owner_client, monitor, **query))
    assert len(rows) == expected and expected > 20
    assert {row["Currency"] for row in rows} == {"RUB"}
    assert [datetime.fromisoformat(row["Observed at"]) for row in rows] == sorted(datetime.fromisoformat(row["Observed at"]) for row in rows)


@pytest.mark.parametrize("period,expected", [("7", 1), ("30", 2), ("90", 3), ("all", 4)])
def test_periods(owner_client, monitor, period, expected):
    for days in (1, 15, 45, 120):
        add_observations(monitor, [str(days)], start=NOW - timedelta(days=days))
    assert len(csv_rows(download(owner_client, monitor, period=period))) == expected


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_inclusive_dates_in_active_timezone(owner_client, monitor, file_format):
    start = datetime(2026, 10, 18, 19, tzinfo=UTC)  # Local midnight on October 19.
    for instant in (start - timedelta(microseconds=1), start, start + timedelta(days=1) - timedelta(microseconds=1), start + timedelta(days=1)):
        add_observations(monitor, ["1"], start=instant)
    with timezone.override("Asia/Yekaterinburg"):
        response = download(owner_client, monitor, format=file_format, date_from="2026-10-19", date_to="2026-10-19")
        if file_format == "csv":
            rows = csv_rows(response)
            assert len(rows) == 2
            assert all(datetime.fromisoformat(row["Observed at"]).day == 19 for row in rows)
        else:
            sheet, styles = worksheet(response)
            assert len(sheet.findall("s:sheetData/s:row", NS)) == 3
            assert sheet.find(".//s:c[@r='E2']", NS).attrib.get("t") != "inlineStr"
            assert any(item.attrib.get("formatCode") == "yyyy-mm-dd hh:mm:ss" for item in styles.findall("s:numFmts/s:numFmt", NS))


@pytest.mark.parametrize("query", [
    {"period": "8"}, {"date_from": "broken"}, {"date_to": "9999-12-31"},
    {"date_from": "2026-10-20", "date_to": "2026-10-01"}, {"currency": "BAD"}, {"format": "exe"},
])
def test_invalid_filters_never_fall_back_to_all_data(owner_client, monitor, query):
    add_observations(monitor, ["1"])
    response = download(owner_client, monitor, **query)
    assert response.status_code == 400 and not response.streaming
    assert response.context["history_form"].errors


def test_single_monitor_requires_currency_but_all_history_can_export_mixed(owner_client, monitor):
    add_observations(monitor, ["1"], currency="RUB")
    add_observations(monitor, ["2"], currency="USD")
    assert download(owner_client, monitor).status_code == 400
    assert {row["Currency"] for row in csv_rows(download(owner_client))} == {"RUB", "USD"}
    assert [row["Currency"] for row in csv_rows(download(owner_client, currency="USD"))] == ["USD"]


def test_all_history_is_owner_scoped_and_includes_archive(owner_client, monitor, user, django_user_model):
    add_observations(monitor, ["1"])
    archived = Monitor.objects.create(owner=user, name="Archived", url=monitor.url, archived_at=NOW)
    add_observations(archived, ["2"])
    foreign = Monitor.objects.create(owner=django_user_model.objects.create_user(username="foreign"), name="Foreign", url=monitor.url)
    add_observations(foreign, ["999"])
    rows = csv_rows(download(owner_client, user=foreign.owner_id, owner=foreign.owner_id))
    assert {row["Monitor"] for row in rows} == {monitor.name, "Archived"}
    assert len(csv_rows(download(owner_client, archived))) == 1


@pytest.mark.parametrize("value", ["=1+2", "+SUM(A1)", "-1+2", "@SUM(A1)", " \t=1", "\n=1", "\ufeff\u200b=1"])
@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_untrusted_fields_cannot_be_formulas(owner_client, monitor, value, file_format):
    Monitor.objects.filter(pk=monitor.pk).update(name=value)
    observation = add_observations(monitor, ["1"])[0]
    Observation.objects.filter(pk=observation.pk).update(title=value, source=value)
    CheckRun.objects.filter(pk=observation.check_run_id).update(url_snapshot=value)
    response = download(owner_client, monitor, format=file_format)
    if file_format == "csv":
        row = csv_rows(response)[0]
        for key in ("Monitor", "Product", "URL", "Source"):
            assert row[key] == "'" + value
    else:
        sheet, _ = worksheet(response)
        for coordinate in ("B2", "C2", "D2", "K2"):
            cell = sheet.find(f".//s:c[@r='{coordinate}']", NS)
            assert cell.attrib["t"] == "inlineStr"
            assert cell_value(cell) == "'" + value


def test_xlsx_typed_values_and_exact_high_precision_fallback(owner_client, monitor):
    values = ["0", "1299.95", "0.1234567890", "12345.1234567890", "123456.1234567891", "99999999999999999999.1234567890", "86786.6348349487", None]
    add_observations(monitor, values)
    sheet, _ = worksheet(download(owner_client, monitor, format="xlsx"))
    for index, value in enumerate(values, 2):
        price = sheet.find(f".//s:c[@r='G{index}']", NS)
        exact = sheet.find(f".//s:c[@r='H{index}']", NS)
        if value is None:
            assert not cell_value(price) and not cell_value(exact)
        else:
            assert exact.attrib["t"] == "inlineStr" and Decimal(cell_value(exact)) == Decimal(value)
            assert Decimal(cell_value(price)) == Decimal(value)
            assert (price.attrib.get("t") == "inlineStr") == (index in (6, 7))
    assert sheet.find("s:sheetViews/s:sheetView/s:pane", NS).attrib["ySplit"] == "1"
    assert sheet.find("s:autoFilter", NS).attrib["ref"] == "A1:L9"
    assert len(sheet.findall("s:cols/s:col", NS)) > 1


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_empty_filtered_history_and_missing_price(owner_client, monitor, file_format):
    add_observations(monitor, [None])
    response = download(owner_client, monitor, format=file_format)
    if file_format == "csv":
        row = csv_rows(response)[0]
        assert row["Price"] == row["Price exact"] == row["Currency"] == ""
        assert row["Availability"] == "out_of_stock"
    else:
        sheet, _ = worksheet(response)
        assert cell_value(sheet.find(".//s:c[@r='J2']", NS)) == "out_of_stock"
    response = download(owner_client, monitor, format=file_format, date_to="2020-01-01")
    if file_format == "csv":
        assert csv_rows(response) == []
    else:
        assert len(worksheet(response)[0].findall("s:sheetData/s:row", NS)) == 1


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_row_limit_does_not_silently_truncate(owner_client, monitor, monkeypatch, file_format):
    monkeypatch.setattr(exports, "MAX_EXPORT_ROWS", 3)
    add_observations(monitor, ["1"] * 3)
    accepted = download(owner_client, monitor, format=file_format)
    assert accepted.status_code == 200
    body(accepted)
    add_observations(monitor, ["2"])
    response = download(owner_client, monitor, format=file_format)
    assert response.status_code == 400
    assert "Choose a shorter period" in str(response.context["history_form"].non_field_errors())


def test_large_export_is_not_chart_sampled_and_has_no_n_plus_one(monitor):
    add_observations(monitor, ["1.01"] * 2005)
    rows = Observation.objects.filter(check_run__monitor=monitor)
    with CaptureQueriesContext(connection) as queries:
        with exports.build_export(rows, "csv") as output:
            result = list(csv.DictReader(StringIO(output.read().decode("utf-8-sig")), delimiter=";"))
    assert len(result) == 2005 and len(queries) == 2
    assert rows._result_cache is None


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
def test_runtime_limit_and_file_cleanup(monitor, monkeypatch, file_format):
    add_observations(monitor, ["1"] * 3)
    monkeypatch.setattr(exports, "MAX_EXPORT_ROWS", 2)
    rows = Observation.objects.filter(check_run__monitor=monitor)
    created_files = []
    original_tempfile = exports.TemporaryFile

    def tracked_file(*args, **kwargs):
        output = original_tempfile(*args, **kwargs)
        created_files.append(output)
        return output

    monkeypatch.setattr(exports, "TemporaryFile", tracked_file)
    # Model a row arriving after preflight without introducing nondeterministic threads.
    monkeypatch.setattr(type(rows), "exists", lambda self: False)
    with pytest.raises(exports.ExportTooLarge):
        exports.build_export(rows, file_format)
    assert len(created_files) == 1 and created_files[0].closed


def test_export_forms_and_methods(owner_client, monitor):
    url = reverse("monitoring:export_all")
    response = owner_client.get(url)
    assert response.status_code == 200
    assert 'value="csv"' in response.content.decode() and 'value="xlsx"' in response.content.decode()
    assert owner_client.post(url).status_code == 405
    detail = owner_client.get(reverse("monitoring:detail", args=[monitor.pk]))
    assert f'formaction="{reverse("monitoring:export", args=[monitor.pk])}"' in detail.content.decode()
    listing = owner_client.get(reverse("monitoring:list"))
    assert f'href="{url}"' in listing.content.decode()
