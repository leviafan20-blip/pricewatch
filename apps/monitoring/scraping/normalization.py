from django.utils.translation import gettext_noop
"""Conservative parsing: ambiguity produces an error, never a guessed price."""

import re
from decimal import Decimal, InvalidOperation

from .errors import ScrapingError


# An explicit supported subset, rather than silently accepting arbitrary tokens.
SUPPORTED_CURRENCIES = frozenset({
    "RUB", "USD", "EUR", "GBP", "CNY", "JPY", "KZT", "BYN", "UAH", "CHF",
    "CAD", "AUD", "NZD", "HKD", "SGD", "INR", "PLN", "TRY", "AED", "SEK",
    "NOK", "DKK", "CZK", "GEL", "AMD", "UZS", "KRW", "BRL", "ILS",
})
_CURRENCY_NAMES = {
    "₽": "RUB", "руб": "RUB", "руб.": "RUB", "рубль": "RUB",
    "рубля": "RUB", "рублей": "RUB", "€": "EUR", "£": "GBP",
    "₸": "KZT", "₴": "UAH", "₹": "INR", "₺": "TRY", "₩": "KRW",
    "₪": "ILS", "ca$": "CAD", "a$": "AUD", "nz$": "NZD",
    "hk$": "HKD", "s$": "SGD", "us$": "USD", "zł": "PLN",
}
_DOLLAR_CURRENCIES = {"USD", "CAD", "AUD", "NZD", "HKD", "SGD"}
_AMBIGUOUS_SYMBOLS = {"$": _DOLLAR_CURRENCIES, "¥": {"JPY", "CNY"},
                      "kr": {"SEK", "NOK", "DKK"}}
_TOKEN_RE = re.compile(
    r"CA\$|US\$|NZ\$|HK\$|A\$|S\$|руб(?:лей|ля|ль|\.)?|zł|kr|"
    r"[₽€£₸₴₹₺₩₪$¥]|\b[A-Za-z]{3}\b",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.replace("\u200b", "")).strip()


def normalize_currency(value: str, hint: str | None = None) -> str:
    token = normalize_text(value).casefold()
    code = token.upper()
    if code in SUPPORTED_CURRENCIES:
        return code
    if token in _CURRENCY_NAMES:
        return _CURRENCY_NAMES[token]
    if token in _AMBIGUOUS_SYMBOLS:
        resolved_hint = normalize_currency(hint) if hint else None
        if resolved_hint in _AMBIGUOUS_SYMBOLS[token]:
            return resolved_hint
        if resolved_hint:
            raise ScrapingError("currency_mismatch", gettext_noop("Символ цены не соответствует валюте."))
        raise ScrapingError("ambiguous_currency", gettext_noop("Неоднозначный символ валюты: укажите её код."))
    raise ScrapingError("unsupported_currency", gettext_noop("Валюта не поддерживается или указана неверно."))


def _positive_decimal(number: str) -> Decimal:
    try:
        value = Decimal(number)
    except InvalidOperation as exc:
        raise ScrapingError("invalid_price", gettext_noop("Цена имеет неверный формат.")) from exc
    if (
        not value.is_finite() or value < 0
        or len(value.as_tuple().digits) > 18 or value.adjusted() >= 18
    ):
        raise ScrapingError("invalid_price", gettext_noop("Цена должна быть конечным неотрицательным числом."))
    if value.as_tuple().exponent < -4:
        raise ScrapingError("invalid_price", gettext_noop("Цена содержит более четырёх десятичных знаков."))
    return value


def _normalize_number(raw: str, *, machine_format: bool) -> Decimal:
    number = normalize_text(raw).replace("’", "'")
    if machine_format and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", number):
        return _positive_decimal(number)
    if not re.fullmatch(r"[0-9][0-9 ,.']*", number):
        raise ScrapingError("invalid_price", gettext_noop("Цена содержит диапазон или посторонний текст."))

    # Spaces and apostrophes can only be complete groups of three integer digits.
    if " " in number or "'" in number:
        grouped = re.fullmatch(r"([0-9]{1,3})([ '])([0-9]{3}(?:\2[0-9]{3})*)([.,][0-9]{1,4})?", number)
        if not grouped:
            raise ScrapingError("invalid_price", gettext_noop("Неверное разделение разрядов цены."))
        number = number.replace(" ", "").replace("'", "")
        if grouped.group(4):
            number = number.replace(",", ".")
        return _positive_decimal(number)

    if "." in number and "," in number:
        decimal_separator = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_separator = "," if decimal_separator == "." else "."
        integer, fraction = number.rsplit(decimal_separator, 1)
        if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(thousands_separator) + r"[0-9]{3})+", integer):
            raise ScrapingError("invalid_price", gettext_noop("Неверное разделение разрядов цены."))
        if not re.fullmatch(r"[0-9]{1,4}", fraction):
            raise ScrapingError("invalid_price", gettext_noop("Неверная дробная часть цены."))
        return _positive_decimal(integer.replace(thousands_separator, "") + "." + fraction)

    separator = "." if "." in number else "," if "," in number else None
    if separator:
        parts = number.split(separator)
        if len(parts) > 2:
            if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(separator) + r"[0-9]{3})+", number):
                raise ScrapingError("invalid_price", gettext_noop("Неверный формат цены."))
            number = "".join(parts)
        elif len(parts[1]) == 3:
            raise ScrapingError("ambiguous_price", gettext_noop("Разделитель цены неоднозначен; используйте явный формат."))
        elif not 1 <= len(parts[1]) <= 4:
            raise ScrapingError("invalid_price", gettext_noop("Неверная дробная часть цены."))
        else:
            number = ".".join(parts)
    return _positive_decimal(number)


def normalize_price(
    value: str | int | Decimal,
    currency_hint: str | None = None,
    *,
    machine_format: bool = False,
) -> tuple[Decimal, str | None]:
    """Return an exact amount and optional currency, checking conflicting tokens.

    JSON-LD numeric fields use machine_format; visible HTML is locale-sensitive.
    Missing currency is checked by the extractor after combining explicit fields.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ScrapingError("invalid_price", gettext_noop("Цена должна быть числом или строкой с числом."))
    raw = normalize_text(str(value))
    if not raw:
        raise ScrapingError("missing_price", gettext_noop("Цена отсутствует или выбранный элемент пуст."))
    if len(raw) > 200:
        raise ScrapingError("invalid_price", gettext_noop("Строка цены слишком длинная."))
    hint = normalize_currency(currency_hint) if currency_hint else None
    matches = list(_TOKEN_RE.finditer(raw))
    for match in matches:
        if re.search(r"[0-9]", raw[:match.start()]) and re.search(r"[0-9]", raw[match.end():]):
            # Removing a currency between two amounts would concatenate prices:
            # "100 ₽ 200 ₽" must not become the plausible amount "100 200".
            raise ScrapingError("ambiguous_price", gettext_noop("Выбранный элемент содержит несколько цен."))
    tokens = [match.group() for match in matches]
    explicit = {
        normalize_currency(token) for token in tokens
        if token.casefold() not in _AMBIGUOUS_SYMBOLS
    }
    if len(explicit) > 1 or (hint and explicit and explicit != {hint}):
        raise ScrapingError("currency_mismatch", gettext_noop("В цене и настройках указаны разные валюты."))
    currency = next(iter(explicit), hint)
    for token in tokens:
        resolved = normalize_currency(token, currency)
        if currency and resolved != currency:
            raise ScrapingError("currency_mismatch", gettext_noop("В цене указаны разные валюты."))
        currency = resolved
    numeric = _TOKEN_RE.sub("", raw).strip()
    return _normalize_number(numeric, machine_format=machine_format), currency


_SCHEMA_AVAILABILITY = {
    "instock": "in_stock", "limitedavailability": "in_stock",
    "outofstock": "out_of_stock", "soldout": "out_of_stock", "discontinued": "out_of_stock",
    "preorder": "preorder", "presale": "preorder", "backorder": "preorder",
}


def normalize_availability(
    value: str | None,
    *,
    in_stock_values: tuple[str, ...] = (),
    out_of_stock_values: tuple[str, ...] = (),
    preorder_values: tuple[str, ...] = (),
) -> str:
    if value is None:
        return "unknown"
    text = normalize_text(value).casefold()
    if text.startswith(("https://schema.org/", "http://schema.org/")):
        return _SCHEMA_AVAILABILITY.get(text.rsplit("/", 1)[-1], "unknown")
    for values, result in (
        (in_stock_values, "in_stock"),
        (out_of_stock_values, "out_of_stock"),
        (preorder_values, "preorder"),
    ):
        if text in {normalize_text(item).casefold() for item in values}:
            return result
    return _SCHEMA_AVAILABILITY.get(text, "unknown")
