
from django import forms
from django.utils.translation import gettext_lazy as _


class EmailPreferencesForm(forms.Form):
    email = forms.EmailField(label=_("Notification email address"), max_length=254, required=False, widget=forms.EmailInput(attrs={"class": "form-control"}))
    email_notifications_enabled = forms.BooleanField(label=_("Receive email notifications"), required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def clean(self):
        data = super().clean()
        if data.get("email_notifications_enabled") and not data.get("email"):
            self.add_error("email", _("Enter a valid email address to enable email notifications."))
        return data
