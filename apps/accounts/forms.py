from django.contrib.auth.forms import (
    AdminUserCreationForm as DjangoAdminUserCreationForm,
    AuthenticationForm,
    UserChangeForm,
)

from .models import User


class LoginForm(AuthenticationForm):
    """Keep Django's credential checks, add presentation attributes only."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "username", "autofocus": True}
        )
        self.fields["password"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "current-password"}
        )


class AdminUserCreationForm(DjangoAdminUserCreationForm):
    class Meta(DjangoAdminUserCreationForm.Meta):
        model = User
        fields = ("username", "email")


class AdminUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
