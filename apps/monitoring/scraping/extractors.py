from django.utils.translation import gettext_noop
"""Extract one unambiguous product without executing scripts or making requests."""

import json
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

from .errors import ScrapingError
from .normalization import (
    normalize_availability,
    normalize_currency,
    normalize_price,
    normalize_text,
)
from .types import ExtractionConfig, ProductData


MAX_HTML_CHARACTERS = 2 * 1024 * 1024
MAX_JSON_NODES = 10_000
_FIELDS = ("title", "price", "currency", "availability")


def _availability(value: str | None, config: ExtractionConfig) -> str:
    return normalize_availability(
        value,
        in_stock_values=config.in_stock_values,
        out_of_stock_values=config.out_of_stock_values,
        preorder_values=config.preorder_values,
    )


def _manual_fields(soup: BeautifulSoup, config: ExtractionConfig) -> dict[str, str]:
    result = {}
    groups = (
        config.in_stock_values, config.out_of_stock_values, config.preorder_values
    )
    normalized_groups = [{normalize_text(item).casefold() for item in group} for group in groups]
    if any(normalized_groups[a] & normalized_groups[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise ScrapingError("invalid_configuration", gettext_noop("Одно значение наличия настроено для разных состояний."))
    for field in _FIELDS:
        selector = getattr(config, f"{field}_selector")
        attribute = getattr(config, f"{field}_attribute")
        if attribute and not selector:
            raise ScrapingError("invalid_configuration", gettext_noop("Атрибут требует соответствующего CSS-селектора."))
        if selector is None:
            continue
        if not selector.strip() or len(selector) > 500:
            raise ScrapingError("invalid_selector", gettext_noop("CSS-селектор пуст или слишком длинный."), details={"field": field})
        try:
            elements = soup.select(selector, limit=2)
        except (SelectorSyntaxError, NotImplementedError, RecursionError) as exc:
            raise ScrapingError("invalid_selector", gettext_noop("Неверный CSS-селектор."), details={"field": field}) from exc
        if not elements:
            raise ScrapingError("selector_not_found", gettext_noop("CSS-селектор не нашёл элемент."), details={"field": field})
        if len(elements) != 1:
            raise ScrapingError("ambiguous_selector", gettext_noop("CSS-селектор нашёл несколько элементов."), details={"field": field})
        element = elements[0]
        if attribute:
            value = element.get(attribute)
            if value is None or not isinstance(value, str):
                raise ScrapingError("selector_attribute_missing", gettext_noop("Атрибут выбранного элемента отсутствует."), details={"field": field})
        elif element.name == "meta":
            value = element.get("content", "")
        else:
            if field == "price":
                monetary_fragments = 0
                for fragment in element.stripped_strings:
                    if not any("0" <= character <= "9" for character in fragment):
                        continue
                    try:
                        normalize_price(fragment, config.currency_hint)
                    except ScrapingError as exc:
                        if exc.code != "ambiguous_currency":
                            continue
                    monetary_fragments += 1
                    if monetary_fragments > 1:
                        raise ScrapingError(
                            "ambiguous_price",
                            gettext_noop("Элемент содержит несколько числовых частей; уточните селектор или атрибут цены."),
                        )
            value = element.get_text(" ", strip=True)
        result[field] = normalize_text(value)
        if not result[field]:
            code = "missing_price" if field == "price" else "empty_selector_value"
            raise ScrapingError(code, gettext_noop("Выбранный элемент пуст."), details={"field": field})
    return result


def _unique_text(values: list[str], *, code: str) -> str | None:
    unique = {normalize_text(value) for value in values if normalize_text(value)}
    if len(unique) > 1:
        raise ScrapingError(code, gettext_noop("На странице обнаружено несколько разных значений."))
    return next(iter(unique), None)


def _metadata(soup: BeautifulSoup, keys: tuple[str, ...], *, code: str) -> str | None:
    values = []
    for element in soup.find_all("meta"):
        if element.get("property") in keys or element.get("itemprop") in keys:
            value = element.get("content")
            if isinstance(value, str):
                values.append(value)
    return _unique_text(values, code=code)


def _title(soup: BeautifulSoup) -> str | None:
    title = _metadata(soup, ("og:title",), code="ambiguous_title")
    if title:
        return title
    return _unique_text(
        [element.get_text(" ", strip=True) for element in soup.find_all("h1")],
        code="ambiguous_title",
    )


def _schema_type(node: dict[str, Any], name: str) -> bool:
    types = node.get("@type", [])
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return False
    supported = {name, f"https://schema.org/{name}", f"http://schema.org/{name}"}
    return any(isinstance(item, str) and item in supported for item in types)


def _json_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes = []
    pending = []
    for script in soup.find_all("script"):
        if str(script.get("type", "")).strip().casefold() != "application/ld+json":
            continue
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            value = json.loads(
                raw, parse_float=Decimal, parse_constant=_invalid_json_constant,
                object_pairs_hook=_unique_json_keys,
            )
        except (ValueError, RecursionError) as exc:
            raise ScrapingError("invalid_json_ld", gettext_noop("Блок JSON-LD содержит некорректные данные.")) from exc
        pending.append(value)
    visited = 0
    while pending:
        value = pending.pop()
        visited += 1
        if visited > MAX_JSON_NODES:
            raise ScrapingError("json_ld_too_complex", gettext_noop("Структура JSON-LD слишком большая."))
        if isinstance(value, dict):
            nodes.append(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return nodes


def _invalid_json_constant(value: str) -> None:
    # Python's JSON decoder accepts NaN/Infinity by default; JSON-LD must not.
    raise ValueError("Non-finite JSON constant")


def _unique_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            # Picking the last duplicate price would silently resolve ambiguity.
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _resolve_reference(value: Any, index: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict) and set(value) == {"@id"}:
        reference = value.get("@id")
        if isinstance(reference, str) and reference in index:
            return index[reference]
        raise ScrapingError("invalid_json_ld", gettext_noop("Ссылка JSON-LD на предложение не найдена."))
    if isinstance(value, str):
        if value in index:
            return index[value]
        raise ScrapingError("invalid_json_ld", gettext_noop("Ссылка JSON-LD на предложение не найдена."))
    return value


def _json_product(
    soup: BeautifulSoup,
    config: ExtractionConfig,
    currency_override: str | None,
    availability_override: str | None,
) -> tuple[str | None, Decimal | None, str | None, str] | None:
    nodes = _json_nodes(soup)
    products = []
    index = {}
    for node in nodes:
        identity = node.get("@id")
        if isinstance(identity, str) and len(node) > 1:
            if identity in index and index[identity] != node:
                raise ScrapingError("invalid_json_ld", gettext_noop("Идентификатор JSON-LD содержит разные определения."))
            index[identity] = node
        if _schema_type(node, "Product") and node not in products:
            products.append(node)
    if len(products) > 1:
        raise ScrapingError("ambiguous_products", gettext_noop("JSON-LD описывает несколько разных товаров."))
    if not products:
        return None
    product = products[0]
    name = product.get("name") if not config.title_selector else None
    if name is not None and not isinstance(name, str):
        raise ScrapingError("invalid_json_ld", gettext_noop("Название товара в JSON-LD имеет неверный формат."))
    offers = product.get("offers", [])
    if not isinstance(offers, list):
        offers = [offers]
    if not offers:
        availability = product.get("availability")
        return name, None, currency_override, (
            availability_override if availability_override is not None else _availability(
                availability if isinstance(availability, str) else None, config
            )
        )
    unique_offers = []
    for raw_offer in offers:
        offer = _resolve_reference(raw_offer, index)
        if not isinstance(offer, dict):
            raise ScrapingError("invalid_json_ld", gettext_noop("Предложение JSON-LD имеет неверный формат."))
        if offer not in unique_offers:
            unique_offers.append(offer)
    if len(unique_offers) != 1:
        raise ScrapingError("ambiguous_offers", gettext_noop("У товара несколько разных предложений; уточните источник цены."))
    offer = unique_offers[0]
    if (
        _schema_type(offer, "AggregateOffer")
        or "lowPrice" in offer or "highPrice" in offer
        or "priceSpecification" in offer
    ):
        raise ScrapingError("unsupported_pricing", gettext_noop("Диапазон, составная цена или условия оплаты требуют отдельной настройки."))
    if "@type" in offer and not _schema_type(offer, "Offer"):
        raise ScrapingError("unsupported_pricing", gettext_noop("Тип предложения JSON-LD не поддерживается."))
    currency_raw = offer.get("priceCurrency")
    if currency_raw is not None and not isinstance(currency_raw, str):
        raise ScrapingError("invalid_json_ld", gettext_noop("Валюта в JSON-LD имеет неверный формат."))
    currency = normalize_currency(currency_raw, currency_override) if currency_raw else None
    if currency_override and currency and currency != currency_override:
        raise ScrapingError("currency_mismatch", gettext_noop("В JSON-LD и настройках указаны разные валюты."))
    currency = currency_override or currency
    availability_raw = offer.get("availability") if availability_override is None else None
    if availability_raw is not None and not isinstance(availability_raw, str):
        raise ScrapingError("invalid_json_ld", gettext_noop("Наличие в JSON-LD имеет неверный формат."))
    availability = availability_override if availability_override is not None else _availability(availability_raw, config)
    price_raw = offer.get("price")
    price = None
    if price_raw is not None and price_raw != "":
        price, currency = normalize_price(price_raw, currency, machine_format=True)
    return name, price, currency, availability


def extract_product(html: str, config: ExtractionConfig | None = None) -> ProductData:
    """Extract exactly one product; configured selectors are mandatory overrides.

    An explicit price selector chooses the manual mode and ignores JSON-LD.
    Otherwise JSON-LD Product/Offer precedes product price metadata. Only a
    confirmed out-of-stock result may have no price. No network calls occur here.
    """
    if not isinstance(html, str) or not html.strip():
        raise ScrapingError("empty_html", gettext_noop("HTML-страница пуста."))
    if len(html) > MAX_HTML_CHARACTERS:
        raise ScrapingError("html_too_large", gettext_noop("HTML-страница превышает допустимый размер."))
    config = config or ExtractionConfig()
    soup = BeautifulSoup(html, "html.parser")
    manual = _manual_fields(soup, config)
    hint = normalize_currency(config.currency_hint) if config.currency_hint else None
    selected_currency = normalize_currency(manual["currency"], hint) if "currency" in manual else hint
    if hint and selected_currency and selected_currency != hint:
        raise ScrapingError("currency_mismatch", gettext_noop("Выбранная валюта не соответствует настройке."))

    title = None
    price = None
    currency = selected_currency
    availability = "unknown"
    source = "manual" if "price" in manual else "metadata"
    if "price" in manual:
        price, currency = normalize_price(manual["price"], currency)
    else:
        structured = _json_product(
            soup, config, currency,
            _availability(manual["availability"], config) if "availability" in manual else None,
        )
        if structured is not None:
            title, price, currency, availability = structured
            source = "json_ld"
        else:
            meta_currency = _metadata(
                soup, ("product:price:currency", "og:price:currency", "priceCurrency"),
                code="ambiguous_currency",
            )
            if meta_currency:
                parsed_currency = normalize_currency(meta_currency, currency)
                if currency and parsed_currency != currency:
                    raise ScrapingError("currency_mismatch", gettext_noop("Метаданные и настройки содержат разные валюты."))
                currency = parsed_currency
            meta_price = _metadata(
                soup, ("product:price:amount", "og:price:amount", "price"),
                code="ambiguous_price",
            )
            if meta_price:
                price, currency = normalize_price(meta_price, currency, machine_format=True)
    if "availability" in manual:
        availability = _availability(manual["availability"], config)
    elif source != "json_ld":
        availability = _availability(
            _metadata(soup, ("product:availability", "og:availability", "availability"),
                      code="ambiguous_availability"), config
        )
    title = manual.get("title") or (normalize_text(title) if title else None) or _title(soup)
    if not title:
        raise ScrapingError("missing_title", gettext_noop("Не удалось определить название товара."))
    if len(title) > 1000:
        raise ScrapingError("invalid_title", gettext_noop("Название товара слишком длинное."))
    if price is None and availability != "out_of_stock":
        raise ScrapingError("missing_price", gettext_noop("Цена не найдена; настройте CSS-селектор или проверьте страницу."))
    if price is not None and currency is None:
        raise ScrapingError("missing_currency", gettext_noop("Валюта цены не определена; укажите её код."))
    diagnostics = ("availability_unknown",) if availability == "unknown" else ()
    if price is None:
        diagnostics += ("price_unavailable_out_of_stock",)
    return ProductData(title, price, currency, availability, source, diagnostics)
