"""Small immutable input/output records; independent of Django and transport."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExtractionConfig:
    title_selector: str | None = None
    price_selector: str | None = None
    currency_selector: str | None = None
    availability_selector: str | None = None
    currency_hint: str | None = None
    title_attribute: str | None = None
    price_attribute: str | None = None
    currency_attribute: str | None = None
    availability_attribute: str | None = None
    in_stock_values: tuple[str, ...] = (
        "in stock", "available", "в наличии", "есть в наличии", "доступен",
    )
    out_of_stock_values: tuple[str, ...] = (
        "out of stock", "sold out", "unavailable", "нет в наличии", "распродано",
    )
    preorder_values: tuple[str, ...] = (
        "preorder", "pre-order", "предзаказ", "под заказ",
    )


@dataclass(frozen=True)
class ProductData:
    title: str
    price: Decimal | None
    currency: str | None
    availability: str
    source: str
    diagnostics: tuple[str, ...] = ()
