"""Shared history filters for HTML, analytics, and downloads."""

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from . import analytics
from .forms import HistoryFilterForm


def history_filters(monitors, query, *, require_currency=True):
    now = timezone.now()
    all_rows = analytics.observations_in_window(monitors, analytics.history_window({"period": "all"}, now=now))
    currencies = analytics.available_currencies(all_rows)
    data = query.copy()
    data.setdefault("period", "30")
    data.setdefault("currency", currencies[0] if len(currencies) == 1 else "")
    form = HistoryFilterForm(data, currencies=currencies, allow_all_currencies=not require_currency)
    context = {"history_form": form, "currency": ""}
    if not form.is_valid():
        return context
    currency = form.cleaned_data["currency"]
    if require_currency and currencies and not currency:
        form.add_error("currency", _("Select one currency from the history; currencies are not converted."))
        return context
    context.update(history_window=analytics.history_window(form.cleaned_data, now=now), currency=currency)
    return context


def history_rows(monitors, window, currency):
    rows = analytics.observations_in_window(monitors, window)
    # Match the detail table: confirmed out-of-stock observations have no price.
    return rows.filter(Q(currency=currency) | Q(price=None)) if currency else rows
