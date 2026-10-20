"""Deterministic fixture tests: no real stores, DNS or HTTP requests."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from apps.monitoring.scraping.errors import ScrapingError
from apps.monitoring.scraping.extractors import extract_product
from apps.monitoring.scraping.normalization import (
    normalize_availability,
    normalize_currency,
    normalize_price,
)
from apps.monitoring.scraping.types import ExtractionConfig


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scraping"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def json_page(product):
    return '<script type="application/ld+json">' + json.dumps(product) + "</script>"


def product_with_offer(**offer):
    return {"@type": "Product", "name": "Example", "offers": {"@type": "Offer", **offer}}


def test_manual_product_ignores_old_price_and_normalizes_russian_text():
    product = extract_product(fixture("manual_product.html"), ExtractionConfig(
        title_selector="h1", price_selector=".price", availability_selector=".availability"
    ))
    assert product.title == "Кофемолка PriceWatch"
    assert product.price == Decimal("12499.90")
    assert isinstance(product.price, Decimal)
    assert product.currency == "RUB"
    assert product.availability == "in_stock"
    assert product.source == "manual"
    assert product.diagnostics == ()


def test_json_ld_product_offer():
    product = extract_product(fixture("json_ld_product.html"))
    assert product.title == "PriceWatch Desk Lamp"
    assert product.price == Decimal("1299.95")
    assert product.currency == "USD"
    assert product.availability == "in_stock"
    assert product.source == "json_ld"


def test_json_ld_graph_and_offer_reference_preserve_decimal():
    product = extract_product(fixture("json_ld_graph.html"))
    assert product.price == Decimal("39.9")
    assert product.currency == "EUR"
    assert product.availability == "preorder"


def test_out_of_stock_price_is_none_instead_of_zero():
    product = extract_product(fixture("out_of_stock.html"))
    assert product.price is None
    assert product.currency == "GBP"
    assert product.availability == "out_of_stock"
    assert "price_unavailable_out_of_stock" in product.diagnostics


def test_manual_confirmed_out_of_stock_can_have_no_price_or_currency():
    product = extract_product('<h1>Sold item</h1><p>Нет в наличии</p>', ExtractionConfig(availability_selector="p"))
    assert product.price is None
    assert product.currency is None
    assert product.availability == "out_of_stock"


def test_product_metadata_is_supported():
    product = extract_product(fixture("metadata_product.html"))
    assert product.title == "Metadata headphones"
    assert product.price == Decimal("79.90")
    assert product.currency == "EUR"
    assert product.source == "metadata"


@pytest.mark.parametrize("config,code", [
    (None, "missing_price"),
    (ExtractionConfig(price_selector=".price"), "missing_price"),
    (ExtractionConfig(price_selector=".missing"), "selector_not_found"),
    (ExtractionConfig(price_selector="["), "invalid_selector"),
    (ExtractionConfig(price_selector=" "), "invalid_selector"),
    (ExtractionConfig(price_attribute="content"), "invalid_configuration"),
])
def test_missing_price_and_invalid_configuration_have_safe_diagnostics(config, code):
    with pytest.raises(ScrapingError) as error:
        extract_product(fixture("missing_price.html"), config)
    assert error.value.code == code
    assert str(error.value)
    assert "<html" not in str(error.value)


def test_selector_matching_multiple_prices_is_rejected_even_with_equal_values():
    html = '<h1>Example</h1><b class="price">99 EUR</b><b class="price">99 EUR</b>'
    with pytest.raises(ScrapingError, match="несколько") as error:
        extract_product(html, ExtractionConfig(price_selector=".price"))
    assert error.value.code == "ambiguous_selector"
    assert error.value.details == {"field": "price"}


@pytest.mark.parametrize("inner", [
    "<del>100</del><ins>200</ins> EUR",
    "<span>100 EUR</span><span>200 EUR</span>",
    "<span>12</span><span>499</span> RUB",
])
def test_price_container_cannot_merge_multiple_numeric_text_nodes(inner):
    with pytest.raises(ScrapingError) as error:
        extract_product('<h1>Product</h1><b id="price">' + inner + '</b>', ExtractionConfig(price_selector="#price"))
    assert error.value.code == "ambiguous_price"


def test_exact_price_attribute_can_override_ambiguous_numeric_children():
    html = '<h1>Product</h1><b data-price="200.00"><del>100</del><ins>200</ins> EUR</b>'
    result = extract_product(html, ExtractionConfig(price_selector="b", price_attribute="data-price", currency_hint="EUR"))
    assert result.price == Decimal("200.00")


def test_currency_symbol_in_separate_element_is_not_a_second_price():
    html = '<h1>Product</h1><b><span>$</span><span>200.00</span></b>'
    result = extract_product(html, ExtractionConfig(price_selector="b", currency_hint="USD"))
    assert result.price == Decimal("200.00")


def test_broken_manual_selector_does_not_fall_back_to_valid_json_ld():
    with pytest.raises(ScrapingError) as error:
        extract_product(fixture("json_ld_product.html"), ExtractionConfig(price_selector=".missing"))
    assert error.value.code == "selector_not_found"


def test_explicit_manual_price_wins_over_ambiguous_structured_data():
    html = fixture("ambiguous_offers.html") + '<h1>Exact variant</h1><b id="selected">88 EUR</b>'
    product = extract_product(html, ExtractionConfig(price_selector="#selected"))
    assert product.price == Decimal("88")
    assert product.source == "manual"


def test_manual_attributes_and_currency_selector():
    html = '<h1 data-title=" Attribute product ">UI</h1><b id="price" data-price="79.90">Call us</b><i data-currency="CHF"></i><em data-stock="ready"></em>'
    product = extract_product(html, ExtractionConfig(
        title_selector="h1", title_attribute="data-title",
        price_selector="#price", price_attribute="data-price",
        currency_selector="i", currency_attribute="data-currency",
        availability_selector="em", availability_attribute="data-stock",
        in_stock_values=("ready",),
    ))
    assert product.title == "Attribute product"
    assert product.currency == "CHF"
    assert product.availability == "in_stock"


def test_explicit_missing_attribute_does_not_read_text():
    with pytest.raises(ScrapingError) as error:
        extract_product('<h1>Title</h1><b>100 EUR</b>', ExtractionConfig(price_selector="b", price_attribute="data-price"))
    assert error.value.code == "selector_attribute_missing"


@pytest.mark.parametrize("text,currency,expected", [
    ("12\u00a0499,90 ₽", "RUB", "12499.90"),
    ("12\u202f499,90 руб.", "RUB", "12499.90"),
    ("1,299.95 USD", "USD", "1299.95"),
    ("EUR 1.299,95", "EUR", "1299.95"),
    ("1'299.95 CHF", "CHF", "1299.95"),
    ("1,299,000 EUR", "EUR", "1299000"),
    ("1.299.000 EUR", "EUR", "1299000"),
    ("1000 JPY", "JPY", "1000"),
    ("0,00 €", "EUR", "0.00"),
    ("USD $100.20", "USD", "100.20"),
    ("CA$100.20", "CAD", "100.20"),
])
def test_locale_prices(text, currency, expected):
    assert normalize_price(text) == (Decimal(expected), currency)


@pytest.mark.parametrize("text,code", [
    ("1,299 EUR", "ambiguous_price"),
    ("1.299 EUR", "ambiguous_price"),
    ("10–20 EUR", "invalid_price"),
    ("от 100 ₽", "invalid_price"),
    ("100 ₽ / месяц", "invalid_price"),
    ("-100 EUR", "invalid_price"),
    ("1 29 EUR", "invalid_price"),
    ("1,29,90 EUR", "invalid_price"),
    ("NaN", "unsupported_currency"),
    ("1.23456 EUR", "invalid_price"),
    ("9999999999999999999 EUR", "invalid_price"),
    ("100 BTC", "unsupported_currency"),
    ("100 USD EUR", "currency_mismatch"),
    ("$100", "ambiguous_currency"),
    ("¥100", "ambiguous_currency"),
    ("100 ₽ 200 ₽", "ambiguous_price"),
    ("100€200€", "ambiguous_price"),
    ("100 USD 200 USD", "ambiguous_price"),
])
def test_unsafe_or_ambiguous_money_is_not_guessed(text, code):
    with pytest.raises(ScrapingError) as error:
        normalize_price(text)
    assert error.value.code == code


@pytest.mark.parametrize("symbol,hint,expected", [("$", "CAD", "CAD"), ("¥", "CNY", "CNY"), ("руб.", None, "RUB")])
def test_currency_symbols_need_correct_context(symbol, hint, expected):
    assert normalize_currency(symbol, hint) == expected


@pytest.mark.parametrize("text,hint", [("100 USD", "EUR"), ("$100", "RUB"), ("€100", "USD")])
def test_currency_hint_is_cross_checked(text, hint):
    with pytest.raises(ScrapingError) as error:
        normalize_price(text, hint)
    assert error.value.code == "currency_mismatch"


def test_manual_price_requires_currency():
    with pytest.raises(ScrapingError) as error:
        extract_product('<h1>Title</h1><b>10.00</b>', ExtractionConfig(price_selector="b"))
    assert error.value.code == "missing_currency"


def test_manual_currency_hint_resolves_dollar():
    product = extract_product('<h1>Title</h1><b>$10.00</b>', ExtractionConfig(price_selector="b", currency_hint="USD"))
    assert product.price == Decimal("10.00")
    assert product.currency == "USD"


def test_unknown_availability_is_not_out_of_stock():
    product = extract_product('<h1>Title</h1><b>100 EUR</b><p>Ask seller</p>', ExtractionConfig(price_selector="b", availability_selector="p"))
    assert product.availability == "unknown"
    assert product.diagnostics == ("availability_unknown",)


@pytest.mark.parametrize("value,expected", [
    ("http://schema.org/InStock", "in_stock"),
    ("https://schema.org/OutOfStock", "out_of_stock"),
    ("https://schema.org/BackOrder", "preorder"),
    ("https://schema.org/UnknownValue", "unknown"),
    ("https://schema.org/OnlineOnly", "unknown"),
    (None, "unknown"),
])
def test_schema_availability(value, expected):
    assert normalize_availability(value) == expected


def test_multiple_differing_offers_are_not_collapsed_to_cheapest():
    with pytest.raises(ScrapingError) as error:
        extract_product(fixture("ambiguous_offers.html"))
    assert error.value.code == "ambiguous_offers"


def test_duplicate_identical_offers_do_not_create_ambiguity():
    offer = {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR"}
    product = extract_product(json_page({"@type": "Product", "name": "Same offer", "offers": [offer, offer]}))
    assert product.price == Decimal("10.00")


@pytest.mark.parametrize("difference", [{"sku": "B"}, {"url": "/different-variant"}, {"seller": {"name": "Other seller"}}])
def test_equal_price_different_offer_identity_is_ambiguous(difference):
    offer = {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR", "sku": "A"}
    other = {**offer, **difference}
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page({"@type": "Product", "name": "Product", "offers": [offer, other]}))
    assert error.value.code == "ambiguous_offers"


def test_different_product_variants_are_ambiguous():
    first = product_with_offer(price="10.00", priceCurrency="EUR")
    second = {**first, "name": "Different variant"}
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page([first, second]))
    assert error.value.code == "ambiguous_products"


def test_equal_prices_do_not_merge_distinct_product_variants():
    first = {**product_with_offer(price="10.00", priceCurrency="EUR"), "sku": "A"}
    second = {**first, "sku": "B"}
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page([first, second]))
    assert error.value.code == "ambiguous_products"


def test_arbitrary_type_url_is_not_assumed_to_be_schema_product():
    item = {**product_with_offer(price="10.00", priceCurrency="EUR"), "@type": "https://unrelated.invalid/Product"}
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page(item) + '<h1>Product</h1>')
    assert error.value.code == "missing_price"


@pytest.mark.parametrize("offers", [
    {"@type": "AggregateOffer", "lowPrice": "10", "highPrice": "20", "priceCurrency": "EUR"},
    {"@type": "Offer", "price": "10", "lowPrice": "10", "highPrice": "20", "priceCurrency": "EUR"},
])
def test_aggregate_ranges_are_not_prices(offers):
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page({"@type": "Product", "name": "Range", "offers": offers}))
    assert error.value.code == "unsupported_pricing"


def test_installments_are_not_full_product_price():
    with pytest.raises(ScrapingError) as error:
        extract_product(fixture("installment_price.html"))
    assert error.value.code == "unsupported_pricing"


def test_corrupt_json_ld_reports_diagnostic():
    with pytest.raises(ScrapingError) as error:
        extract_product('<h1>Product</h1><script type="application/ld+json">{"@type":</script>')
    assert error.value.code == "invalid_json_ld"


@pytest.mark.parametrize("payload", [
    '{"@type":"Product","name":"Title","offers":{"price":"10","price":"20","priceCurrency":"EUR"}}',
    '{"@type":"Product","name":"Title","offers":{"price":NaN,"priceCurrency":"EUR"}}',
    '{"@type":"Product","name":"Title","offers":{"price":Infinity,"priceCurrency":"EUR"}}',
])
def test_nonstandard_or_duplicate_json_values_are_rejected(payload):
    with pytest.raises(ScrapingError) as error:
        extract_product('<script type="application/ld+json">' + payload + "</script>")
    assert error.value.code == "invalid_json_ld"


def test_missing_offer_reference_reports_diagnostic():
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page({"@type": "Product", "name": "Product", "offers": {"@id": "#missing"}}))
    assert error.value.code == "invalid_json_ld"


@pytest.mark.parametrize("offer,code", [
    ({"price": True, "priceCurrency": "EUR"}, "invalid_price"),
    ({"price": [10, 20], "priceCurrency": "EUR"}, "invalid_price"),
    ({"price": "10", "priceCurrency": ["EUR"]}, "invalid_json_ld"),
    ({"price": "10", "priceCurrency": "XYZ"}, "unsupported_currency"),
    ({"price": "10"}, "missing_currency"),
    ({"priceCurrency": "EUR", "availability": "https://schema.org/InStock"}, "missing_price"),
])
def test_invalid_json_ld_values_are_not_guessed(offer, code):
    with pytest.raises(ScrapingError) as error:
        extract_product(json_page(product_with_offer(**offer)))
    assert error.value.code == code


def test_json_numeric_values_preserve_decimal_precision():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Precision","offers":{"@type":"Offer","price":12.345,"priceCurrency":"EUR"}}</script>'
    assert extract_product(html).price == Decimal("12.345")


def test_json_scientific_numeric_format_is_exact_and_bounded():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Exponent","offers":{"price":1e3,"priceCurrency":"EUR"}}</script>'
    assert extract_product(html).price == Decimal("1000")
    with pytest.raises(ScrapingError) as error:
        extract_product(html.replace("1e3", "1e100"))
    assert error.value.code == "invalid_price"


def test_manual_title_override_and_json_price():
    html = fixture("json_ld_product.html") + '<p id="name">Custom title</p>'
    assert extract_product(html, ExtractionConfig(title_selector="#name")).title == "Custom title"


def test_manual_fields_override_broken_automatic_title_and_availability():
    item = product_with_offer(price="10.00", priceCurrency="EUR", availability=["invalid"])
    item["name"] = ["invalid"]
    html = json_page(item) + '<h1>Chosen name</h1><p>В наличии</p>'
    result = extract_product(html, ExtractionConfig(title_selector="h1", availability_selector="p"))
    assert result.title == "Chosen name"
    assert result.availability == "in_stock"


def test_metadata_multiple_distinct_prices_are_ambiguous():
    html = fixture("metadata_product.html") + '<meta itemprop="price" content="1.00">'
    with pytest.raises(ScrapingError) as error:
        extract_product(html)
    assert error.value.code == "ambiguous_price"


def test_visible_price_is_not_guessed_without_selector():
    with pytest.raises(ScrapingError) as error:
        extract_product(fixture("manual_product.html"))
    assert error.value.code == "missing_price"


@pytest.mark.parametrize("html,code", [("", "empty_html"), (" " * (2 * 1024 * 1024 + 1) + "x", "html_too_large")])
def test_parser_input_limits(html, code):
    with pytest.raises(ScrapingError) as error:
        extract_product(html)
    assert error.value.code == code


def test_missing_or_ambiguous_title_requires_configuration():
    config = ExtractionConfig(price_selector="b")
    for html, code in [('<b>100 EUR</b>', "missing_title"), ('<h1>A</h1><h1>B</h1><b>100 EUR</b>', "ambiguous_title")]:
        with pytest.raises(ScrapingError) as error:
            extract_product(html, config)
        assert error.value.code == code


def test_conflicting_custom_availability_mapping_is_rejected():
    with pytest.raises(ScrapingError) as error:
        extract_product('<h1>Title</h1><b>100 EUR</b>', ExtractionConfig(price_selector="b", in_stock_values=("same",), out_of_stock_values=("same",)))
    assert error.value.code == "invalid_configuration"
