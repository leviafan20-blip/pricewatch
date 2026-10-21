"""Inspect the actual XLSX serialization without a database or HTTP request."""

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from apps.monitoring import exports

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.mark.parametrize("value,numeric", [
    ("71044502683.9356", True),  # Regression: percent formatting passed through float.
    ("71044502683.9356000000", True),  # The same price at the database's scale.
    ("86786.6348349487", True),
    ("9769786215272.06", True),
    ("0.1234567890", True),
    ("0.0000000001", True),
    ("0.0000000000", True),
    ("99999.9999999999", True),  # 15 significant digits.
    ("999999.9999999999", False),  # 16 significant digits.
    ("10000000000000000000.0000000000", True),
    ("123456.1234567891", False),
    ("99999999999999999999.1234567890", False),
])
def test_xlsx_decimal_serialization_and_exact_price(monkeypatch, value, numeric):
    price = Decimal(value)
    row = ["1", "Test monitor", "Test product", "https://shop.example/product",
           datetime(2026, 10, 21, tzinfo=UTC), "UTC", price, value,
           "RUB", "in_stock", "json_ld", "test-check"]
    monkeypatch.setattr(exports, "export_rows", lambda rows: iter([row]))
    output = BytesIO()

    exports.write_xlsx(output, None)

    with ZipFile(output) as archive:
        assert archive.testzip() is None
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cell = sheet.find(".//s:c[@r='G2']", NS)
    exact = sheet.find(".//s:c[@r='H2']", NS)
    assert (cell.attrib.get("t") != "inlineStr") is numeric
    assert Decimal("".join(cell.itertext())) == price
    assert exact.attrib["t"] == "inlineStr"
    assert "".join(exact.itertext()) == value
    assert not sheet.findall(".//s:f", NS)
