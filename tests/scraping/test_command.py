"""The diagnostic command processes local fixtures without a database or network."""

import json
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.monitoring.management.commands import parse_product
from apps.monitoring.scraping.errors import ScrapingError


def test_command_parses_local_html_with_selectors_and_preserves_decimal(tmp_path):
    html = tmp_path / "product.html"
    html.write_text(
        '<h1>Учебный чайник</h1><span class="price" data-price="1290.50"></span>',
        encoding="utf-8",
    )
    output = StringIO()
    call_command(
        "parse_product",
        str(html),
        title_selector="h1",
        price_selector=".price",
        price_attribute="data-price",
        currency_hint="RUB",
        stdout=output,
    )
    result = json.loads(output.getvalue())
    assert result["title"] == "Учебный чайник"
    assert Decimal(result["price"]) == Decimal("1290.50")
    assert isinstance(result["price"], str)
    assert result["currency"] == "RUB"
    assert result["source"] == "manual"
    assert "Учебный чайник" in output.getvalue()


def test_command_uses_json_ld_without_manual_selectors(tmp_path):
    html = tmp_path / "product.html"
    html.write_text(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Demo kettle","offers":{"@type":"Offer",'
        '"price":"42.25","priceCurrency":"EUR",'
        '"availability":"https://schema.org/InStock"}}</script>',
        encoding="utf-8",
    )
    output = StringIO()
    call_command("parse_product", str(html), stdout=output)
    result = json.loads(output.getvalue())
    assert result["price"] == "42.25"
    assert result["currency"] == "EUR"
    assert result["availability"] == "in_stock"
    assert result["source"] == "json_ld"


@pytest.mark.parametrize(
    "filename", ["https://shop.example/product", "file:///tmp/product.html", "//host/share/x", r"\\host\share\x"]
)
def test_command_rejects_urls_and_network_paths(filename, monkeypatch):
    def unexpected_open(*args, **kwargs):
        raise AssertionError("Network-like paths must not be opened")

    monkeypatch.setattr(parse_product.Path, "resolve", unexpected_open)
    with pytest.raises(CommandError, match="file_required"):
        call_command("parse_product", filename)


@pytest.mark.parametrize("kind", ["missing", "directory", "encoding", "oversized"])
def test_command_reports_file_errors(tmp_path, kind):
    path = tmp_path / "input.html"
    if kind == "directory":
        path.mkdir()
    elif kind == "encoding":
        path.write_bytes(b"\xff\xfe")
    elif kind == "oversized":
        path.write_bytes(b"x" * (parse_product.MAX_HTML_BYTES + 1))
    expected = {
        "missing": "file_unreadable",
        "directory": "file_required",
        "encoding": "file_encoding",
        "oversized": "file_too_large",
    }[kind]
    with pytest.raises(CommandError, match=expected):
        call_command("parse_product", str(path))


def test_command_propagates_safe_extraction_error(tmp_path, monkeypatch):
    path = tmp_path / "input.html"
    path.write_text("<h1>Demo</h1>", encoding="utf-8")

    def fail_extract(*args, **kwargs):
        raise ScrapingError("invalid_selector", "Некорректный CSS-селектор.")

    monkeypatch.setattr(parse_product, "extract_product", fail_extract)
    with pytest.raises(CommandError, match="invalid_selector: Некорректный CSS-селектор"):
        call_command("parse_product", str(path))


def test_command_reads_at_most_limit_plus_one_byte(tmp_path, monkeypatch):
    path = tmp_path / "input.html"
    path.write_text("large", encoding="utf-8")
    reads = []

    class BoundedFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            reads.append(size)
            return b"x" * size

    monkeypatch.setattr(parse_product.Path, "open", lambda *args, **kwargs: BoundedFile())
    with pytest.raises(CommandError, match="file_too_large"):
        call_command("parse_product", str(path))
    assert reads == [parse_product.MAX_HTML_BYTES + 1]


def test_command_outputs_confirmed_out_of_stock_without_price_as_null(tmp_path):
    path = tmp_path / "input.html"
    path.write_text(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Demo kettle","offers":{"@type":"Offer",'
        '"availability":"https://schema.org/OutOfStock"}}</script>',
        encoding="utf-8",
    )
    output = StringIO()
    call_command("parse_product", str(path), stdout=output)
    result = json.loads(output.getvalue())
    assert result["price"] is None
    assert result["availability"] == "out_of_stock"
    assert result["diagnostics"] == ["price_unavailable_out_of_stock"]
