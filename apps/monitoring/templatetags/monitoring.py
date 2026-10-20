"""Display exact decimal amounts without rounding the stored price."""

from decimal import Decimal, localcontext

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def history_query(context, parameter, value):
    """Keep period, dates, currency and multi-value comparison IDs while paging."""
    query = context["request"].GET.copy()
    query[parameter] = value
    return "?" + query.urlencode()


@register.filter
def percent_amount(value):
    if value is None or value == "":
        return "—"
    with localcontext() as context:
        context.prec = 60
        return amount(value.quantize(Decimal("0.01")))


@register.filter
def amount(value):
    if value is None or value == "":
        return "—"
    integer, _, fraction = format(value, "f").partition(".")
    sign = "-" if integer.startswith("-") else ""
    integer = integer.removeprefix("-")
    groups = []
    while integer:
        groups.append(integer[-3:])
        integer = integer[:-3]
    result = "\N{NO-BREAK SPACE}".join(reversed(groups))
    fraction = fraction.rstrip("0")
    return sign + result + ("," + fraction if fraction else "")
