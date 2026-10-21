"""Bounded exports of already owner-scoped observations. No network or tasks."""

import csv
from decimal import Decimal
from io import TextIOWrapper
from tempfile import TemporaryFile
import unicodedata

from django.utils import timezone
import xlsxwriter

MAX_EXPORT_ROWS = 50_000
CHUNK_SIZE = 1_000
HEADERS = (
    "Monitor ID", "Monitor", "Product", "URL", "Observed at", "Timezone",
    "Price", "Price exact", "Currency", "Availability", "Source", "Check ID",
)
FIELDS = (
    "check_run__monitor_id", "check_run__monitor__name", "title", "check_run__url_snapshot",
    "observed_at", "price", "currency", "availability", "source", "check_run_id",
)
CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ExportTooLarge(Exception):
    pass


def safe_text(value):
    """Keep Unicode; neutralize spreadsheet expressions even after invisible prefixes."""
    text = "".join(char if ord(char) >= 32 or char in "\t\r\n" else "\ufffd" for char in str(value))
    candidate = text
    while candidate and (candidate[0].isspace() or unicodedata.category(candidate[0]) in {"Cc", "Cf"}):
        candidate = candidate[1:]
    if text.startswith(("\t", "\r", "\n")) or candidate.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def numeric_price(value):
    # Excel only guarantees 15 significant digits. Do not normalize Decimal using
    # the process precision (the database supports up to 30 digits).
    digits = "".join(str(digit) for digit in value.as_tuple().digits).rstrip("0")
    # Match XlsxWriter's Decimal formatting in number XML without converting
    # through float, which can introduce a rounding digit before serialization.
    return len(digits) <= 15 and Decimal(format(value, ".16G")) == value


def export_rows(rows):
    for index, row in enumerate(rows.order_by("observed_at", "pk").values_list(*FIELDS).iterator(chunk_size=CHUNK_SIZE)):
        if index >= MAX_EXPORT_ROWS:
            # Also protects against backdated insertions after the preflight query.
            raise ExportTooLarge
        monitor_id, name, title, url, observed_at, price, currency, availability, source, check_id = row
        exact = format(price, "f") if price is not None else ""
        yield [str(monitor_id), name, title, url, timezone.localtime(observed_at),
               timezone.get_current_timezone_name(), price, exact, currency, availability, source, str(check_id)]


def write_csv(output, rows):
    # A real file avoids retaining the entire response or a DB cursor while the
    # client downloads. UTF-8 BOM and semicolons suit Excel's Cyrillic locales.
    text = TextIOWrapper(output, encoding="utf-8-sig", newline="")
    try:
        writer = csv.writer(text, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerow(HEADERS)
        for row in export_rows(rows):
            row[4] = row[4].isoformat(timespec="microseconds")
            row[6] = row[7]
            writer.writerow([safe_text(value) for value in row])
        text.flush()
    finally:
        text.detach()  # The caller owns the binary file, including on failure.


def write_xlsx(output, rows):
    with xlsxwriter.Workbook(output, {"constant_memory": True, "strings_to_formulas": False,
                                       "strings_to_urls": False}) as workbook:
        sheet = workbook.add_worksheet("Price history")
        heading = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#243653"})
        date_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        money_format = workbook.add_format({"num_format": "#,##0.00########"})
        exact_format = workbook.add_format({"num_format": "@"})
        sheet.freeze_panes(1, 0)
        sheet.set_row(0, 28)
        for column, width in enumerate((14, 28, 36, 52, 23, 24, 24, 34, 12, 17, 14, 38)):
            sheet.set_column(column, column, width)
            sheet.write_string(0, column, HEADERS[column], heading)
        last_row = 0
        for last_row, values in enumerate(export_rows(rows), 1):
            for column, value in enumerate(values):
                if column == 4:
                    sheet.write_datetime(last_row, column, value.replace(tzinfo=None), date_format)
                elif column == 6 and value is not None:
                    if numeric_price(value):
                        sheet.write_number(last_row, column, value, money_format)
                    else:
                        sheet.write_string(last_row, column, values[7], exact_format)
                else:
                    # Explicit strings, including exact decimal prices: never formulas,
                    # hyperlinks, or automatic number conversion.
                    sheet.write_string(last_row, column, safe_text(value if value is not None else ""),
                                       exact_format if column == 7 else None)
        sheet.autofilter(0, 0, last_row, len(HEADERS) - 1)


def build_export(rows, file_format):
    """Return a rewound file; caller transfers ownership to Django FileResponse."""
    if file_format not in CONTENT_TYPES:
        raise ValueError("Unsupported export format")
    # Bounded SQL preflight, not COUNT(*) or materialization of all model objects.
    if rows.order_by("pk").values("pk")[MAX_EXPORT_ROWS:MAX_EXPORT_ROWS + 1].exists():
        raise ExportTooLarge
    output = TemporaryFile(mode="w+b")
    try:
        {"csv": write_csv, "xlsx": write_xlsx}[file_format](output, rows)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
