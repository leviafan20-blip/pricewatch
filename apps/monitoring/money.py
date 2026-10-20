"""Shared exact change calculation for analytics and notifications."""

from decimal import Decimal, localcontext


def price_change(previous, current):
    with localcontext() as context:
        context.prec = 60
        change = current - previous
        percent = change / previous * Decimal(100) if previous else None
    return change, percent
