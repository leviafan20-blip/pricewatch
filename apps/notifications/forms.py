from django import forms


class EmailPreferencesForm(forms.Form):
    email = forms.EmailField(label="Email для уведомлений", max_length=254, required=False, widget=forms.EmailInput(attrs={"class": "form-control"}))
    email_notifications_enabled = forms.BooleanField(label="Получать уведомления по email", required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def clean(self):
        data = super().clean()
        if data.get("email_notifications_enabled") and not data.get("email"):
            self.add_error("email", "Для включения email-уведомлений укажите корректный адрес.")
        return data
