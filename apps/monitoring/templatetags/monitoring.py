"""Display exact decimal amounts without rounding the stored price."""

from decimal import Decimal, localcontext

from django import template
from django.utils import formats
from django.utils.translation import gettext, gettext_noop

register = template.Library()


@register.filter
def translated_diagnostic(value):
    """Translate known system diagnostics without rewriting stored history."""
    diagnostics = {
        "availability_unknown": gettext_noop("Availability could not be determined."),
        "price_unavailable_out_of_stock": gettext_noop("The product is out of stock and has no price."),
    }
    return gettext(diagnostics.get(str(value), str(value)))


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
    result = formats.get_format("THOUSAND_SEPARATOR").join(reversed(groups))
    fraction = fraction.rstrip("0")
    return sign + result + (formats.get_format("DECIMAL_SEPARATOR") + fraction if fraction else "")
