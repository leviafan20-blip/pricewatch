"""Explicit owner-facing fields; authorization stays in the domain services."""

from datetime import date

from django import forms
from django.core.exceptions import ValidationError

from .checks import validate_monitor_input, validate_notification_settings
from .models import Monitor
from .scraping.normalization import SUPPORTED_CURRENCIES


class MonitorForm(forms.Form):
    notification_mode = forms.ChoiceField(label="Уведомления о цене", choices=Monitor.NotificationMode.choices, required=False, initial="off")
    notification_threshold = forms.DecimalField(label="Порог цены", required=False, max_digits=30, decimal_places=10, min_value=0)
    notification_currency = forms.ChoiceField(label="Валюта порога", required=False, choices=[("", "Выберите валюту"), *((code, code) for code in sorted(SUPPORTED_CURRENCIES))])
    name = forms.CharField(label="Название мониторинга", max_length=200)
    url = forms.CharField(
        label="URL товара", max_length=2048,
        help_text="HTTP(S)-адрес на разрешённом сайте. Сохранение не запускает проверку.",
        widget=forms.URLInput(attrs={"placeholder": "https://shop.example/product"}),
    )
    title_selector = forms.CharField(label="Название товара", max_length=500, required=False)
    price_selector = forms.CharField(label="Цена", max_length=500, required=False)
    currency_selector = forms.CharField(label="Валюта", max_length=500, required=False)
    availability_selector = forms.CharField(label="Наличие", max_length=500, required=False)
    currency_hint = forms.ChoiceField(
        label="Явная валюта", required=False,
        choices=[("", "Определить по странице")] + [(code, code) for code in sorted(SUPPORTED_CURRENCIES)],
        help_text="Нужна, например, для неоднозначного символа $. Не пересчитывает цену.",
    )
    title_attribute = forms.CharField(label="Атрибут названия", max_length=100, required=False)
    price_attribute = forms.CharField(label="Атрибут цены", max_length=100, required=False)
    currency_attribute = forms.CharField(label="Атрибут валюты", max_length=100, required=False)
    availability_attribute = forms.CharField(label="Атрибут наличия", max_length=100, required=False)

    config_fields = (
        "title_selector", "price_selector", "currency_selector", "availability_selector",
        "currency_hint", "title_attribute", "price_attribute", "currency_attribute",
        "availability_attribute",
    )

    def __init__(self, *args, monitor=None, **kwargs):
        self.monitor = monitor
        if monitor is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "name": monitor.name, "url": monitor.url, **monitor.extraction_config,
                "notification_mode": monitor.notification_mode, "notification_threshold": monitor.notification_threshold,
                "notification_currency": monitor.notification_currency,
            }
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["aria-describedby"] = f"id_{name}_help"
        self.fields["name"].widget.attrs["autofocus"] = True
        for field_name in ("title_selector", "price_selector", "currency_selector", "availability_selector"):
            self.fields[field_name].widget.attrs["placeholder"] = ".product-price" if field_name == "price_selector" else "CSS-селектор"
        for field_name in ("title_attribute", "price_attribute", "currency_attribute", "availability_attribute"):
            self.fields[field_name].widget.attrs["placeholder"] = "Текст элемента (по умолчанию)"

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        config = {key: cleaned[key] for key in self.config_fields if cleaned.get(key)}
        # Preserve already configured availability vocabularies when editing basic fields.
        if self.monitor is not None:
            for key in ("in_stock_values", "out_of_stock_values", "preorder_values"):
                if key in self.monitor.extraction_config:
                    config[key] = self.monitor.extraction_config[key]
        try:
            cleaned["notification_settings"] = validate_notification_settings(
                notification_mode=cleaned.get("notification_mode") or "off",
                notification_threshold=cleaned.get("notification_threshold"),
                notification_currency=cleaned.get("notification_currency") or "",
            )
            config = validate_monitor_input(
                name=cleaned["name"], url=cleaned["url"], extraction_config=config,
            )
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                for key, errors in error.message_dict.items():
                    self.add_error(key if key in self.fields else None, errors)
            else:
                self.add_error(None, error)
        else:
            cleaned.update(config)
        return cleaned


class ScheduleForm(forms.Form):
    auto_enabled = forms.BooleanField(
        label="Автоматические проверки", required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "role": "switch"}),
    )
    check_interval_hours = forms.TypedChoiceField(
        label="Интервал проверки", choices=Monitor.Interval.choices, coerce=int,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, monitor, **kwargs):
        kwargs.setdefault("initial", {
            "auto_enabled": monitor.auto_enabled,
            "check_interval_hours": monitor.check_interval_hours,
        })
        super().__init__(*args, **kwargs)


class HistoryFilterForm(forms.Form):
    period = forms.ChoiceField(label="Период", choices=[("7", "7 дней"), ("30", "30 дней"), ("90", "90 дней"), ("all", "Всё время")])
    date_from = forms.DateField(label="С даты", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), input_formats=["%Y-%m-%d"])
    date_to = forms.DateField(label="По дату", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), input_formats=["%Y-%m-%d"])
    currency = forms.ChoiceField(label="Валюта", required=False)

    def __init__(self, *args, currencies=(), allow_all_currencies=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].choices = [("", "Все валюты" if allow_all_currencies else "Выберите валюту"), *((value, value) for value in currencies)]
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"

    def clean(self):
        data = super().clean()
        start, end = data.get("date_from"), data.get("date_to")
        if end == date.max:
            self.add_error("date_to", "Выберите дату до 31.12.9999.")
        if start and end and start > end:
            self.add_error("date_to", "Конец периода должен быть не раньше начала.")
        return data

